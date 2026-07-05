r"""erb500k_scored.py -- ERB 500-question scored run for the G2 prose gate
(issue #93). Chain stage S4 helper.

Runs the EnterpriseRAG-Bench 500-question set against the sharded 500K fixture
and scores it with the goal-gates trinary judge protocol
(docs/specs/2026-07-01-goal-gates-hallucination-visibility.md sec.4 area +
docs/specs/2026-07-01-abstain-search-escalation.md sec.4).

Per question:
  (a) POST /context/packet -> RECORD the know / miss block verbatim.
  (b) Generate an answer with Claude Sonnet (claude -p, packet context +
      question), context injected as an appended system prompt.
  (c) Judge with the trinary protocol: reference-guided single-answer grading,
      CORRECT / INCORRECT / ABSTAINED, judge = Sonnet.
  (d) Sample ~10% of judged items for a second-opinion AUDIT with Opus.

Grounding (all read on the rig, file:line in the chain report):
  * ERB question schema + gold resolution + prefix-tolerant delivered-path
    matching: benchmarks/bench_enterprise_rag.py load_needles / helix_context /
    make_gold_index / match_delivered_to_gold (worktree copy; fields:
    q["question"], q.get("gold_answer"), q.get("expected_doc_ids"), mapped via
    generated_data/uuid_index.json -> generated_data/sources/<rel>).
  * claude -p convention: scripts/bench_claude_matrix.py (json output,
    --max-budget-usd cap, short model aliases sonnet/opus) and
    bench_enterprise_rag.run_claude (--append-system-prompt-file to dodge the
    32K arg-line limit + an empty --mcp-config).
  * /context/packet contract: helix_context/server/routes_context.py:546 --
    body {query, task_type, max_genes}; top-level "know" {found, confidence,
    gene_id_match, ...} XOR "miss" {reason, escalate_to|refresh_targets, ...}.
    Evidence lists are verified / stale_risk / contradictions of ContextItem
    (schemas.py:239-275; item body is `content`, path is `source_id`).
  * Sharded fixture is served by the caller (.ps1) with HELIX_USE_SHARDS=1 and
    HELIX_GENOME_PATH pointing at .../enterprise_rag_500k/main.genome.db
    (helix_context/sharding.open_read_source detects the basename).

RESUMABLE: the output JSONL is append-only, one line per finished question,
keyed by question id. On restart, ids already present are skipped. 500 questions
x several LLM calls each WILL be interrupted at least once.

Published baselines for comparison (from the goal-gates spec):
  BM25 68.8 / Vector 51.4 / Onyx+GPT-4 72.4.

Stdlib + repo-adjacent imports + httpx only.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Published prose baselines (goal-gates spec sec. "Accuracy source of truth").
BASELINES = {"BM25": 68.8, "Vector": 51.4, "Onyx_GPT4": 72.4}


# ---------------------------------------------------------------------------
# ERB question loading (schema per benchmarks/bench_enterprise_rag.load_needles)
# ---------------------------------------------------------------------------

def load_needles(erb_root, max_questions=None, types=None):
    """Parse questions.jsonl defensively and resolve gold dsids to paths.

    Field names confirmed from bench_enterprise_rag.load_needles:
      q["question"], q.get("gold_answer",""), q.get("expected_doc_ids",[]),
      q.get("question_type"), q["question_id"].
    Everything is accessed with .get() and skipped if malformed so a single
    bad row cannot abort a 500-question run.
    """
    questions_path = erb_root / "questions.jsonl"
    uuid_index_path = erb_root / "generated_data" / "uuid_index.json"
    corpus_root = erb_root / "generated_data" / "sources"

    uuid_index = {}
    if uuid_index_path.is_file():
        try:
            uuid_index = json.loads(
                uuid_index_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            uuid_index = {}

    needles = []
    if not questions_path.is_file():
        return needles
    with questions_path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                q = json.loads(line)
            except Exception:
                continue
            qid = q.get("question_id") or q.get("id")
            qtext = q.get("question")
            if not qid or not qtext:
                continue
            qtype = q.get("question_type") or q.get("type") or "unknown"
            if types and qtype not in types:
                continue
            gold_paths = []
            for dsid in q.get("expected_doc_ids") or []:
                rel = uuid_index.get(str(dsid))
                if rel:
                    gold_paths.append(str(corpus_root / rel))
            needles.append({
                "id": str(qid),
                "type": qtype,
                "question": str(qtext),
                "gold_answer": q.get("gold_answer", "") or q.get("answer", "") or "",
                "expected_doc_ids": q.get("expected_doc_ids", []) or [],
                "gold_paths": gold_paths,
            })
    if max_questions is not None:
        needles = needles[:max_questions]
    return needles


# ---------------------------------------------------------------------------
# Prefix-tolerant delivered-path -> gold matching
# (copied from bench_enterprise_rag: the <GENE src> prefix-strip bug fix)
# ---------------------------------------------------------------------------

def _rel_after_sources(p):
    n = str(p).replace("\\", "/")
    if "/sources/" in n:
        return n.split("/sources/", 1)[1]
    if n.startswith("sources/"):
        return n[len("sources/"):]
    return n


def make_gold_index(gold_paths):
    canonicals = set()
    index = {}
    for p in gold_paths or ():
        canonical = _rel_after_sources(p)
        if not canonical:
            continue
        canonicals.add(canonical)
        index[canonical] = canonical
        first_slash = canonical.find("/")
        if first_slash > 0:
            index.setdefault(canonical[first_slash + 1:], canonical)
    return index, canonicals


def match_delivered(delivered_path, gold_index, gold_canonicals):
    if not delivered_path:
        return None
    rel = _rel_after_sources(delivered_path)
    if not rel:
        return None
    if rel in gold_canonicals:
        return rel
    return gold_index.get(rel)


# ---------------------------------------------------------------------------
# /context/packet -- record know/miss verbatim + assemble injectable context
# ---------------------------------------------------------------------------

def fetch_packet(helix_url, query, max_genes, timeout_s=40.0):
    """POST /context/packet. Returns the full packet dict (know/miss verbatim)
    plus a derived {context_text, delivered_paths} for answer injection.

    The packet carries evidence items (freshness-labeled). We build a compact
    context blob from the ContextItem bodies (`content`) across the
    verified / stale_risk / contradictions lists; if none are present (packet
    is navigation-first) we fall back to /context content.
    """
    out = {"status": "ok", "know": None, "miss": None,
           "context_text": "", "delivered_paths": [], "raw_keys": []}
    try:
        resp = httpx.post(
            helix_url + "/context/packet",
            json={"query": query, "task_type": "explain",
                  "max_genes": max_genes, "include_raw": True},
            timeout=timeout_s,
        )
    except Exception as exc:
        out["status"] = "error:{}".format(exc)
        return out
    if resp.status_code != 200:
        out["status"] = "http_{}".format(resp.status_code)
        return out
    try:
        packet = resp.json()
    except Exception as exc:
        out["status"] = "json_error:{}".format(exc)
        return out

    out["raw_keys"] = sorted(list(packet.keys())) if isinstance(packet, dict) else []
    if isinstance(packet, dict):
        out["know"] = packet.get("know")
        out["miss"] = packet.get("miss")
        # ContextPacket carries evidence as verified / stale_risk /
        # contradictions lists of ContextItem (schemas.py:265-275; item body is
        # `content`, path is `source_id`). Extra keys are harmless supersets.
        pieces = []
        paths = []
        for key in ("verified", "stale_risk", "contradictions",
                    "items", "evidence", "genes"):
            arr = packet.get(key)
            if isinstance(arr, list):
                for it in arr:
                    if not isinstance(it, dict):
                        continue
                    src = (it.get("source_id") or it.get("source")
                           or it.get("path") or it.get("title") or "")
                    body = (it.get("content") or it.get("body")
                            or it.get("text") or it.get("raw") or "")
                    if src:
                        paths.append(str(src).replace("\\", "/"))
                    if body:
                        pieces.append("[{}]\n{}".format(src, body))
        out["delivered_paths"] = paths
        out["context_text"] = "\n\n".join(pieces)[:12000]

    # Fallback: if the packet gave us no usable context body, pull /context.
    if not out["context_text"]:
        try:
            r2 = httpx.post(helix_url + "/context",
                            json={"query": query, "decoder_mode": "none"},
                            timeout=timeout_s)
            if r2.status_code == 200:
                raw = r2.json()
                d = raw[0] if isinstance(raw, list) and raw else (
                    raw if isinstance(raw, dict) else {})
                ctx = d.get("content", "") or ""
                out["context_text"] = ctx[:12000]
                for m in re.finditer(r'<GENE\s+src="([^"]+)"', ctx):
                    out["delivered_paths"].append(m.group(1).replace("\\", "/"))
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# claude -p (answer + judge + audit)
# ---------------------------------------------------------------------------

_ANSWER_SYS = (
    "You are answering a single factual question about an internal company "
    "knowledge base. Use ONLY the context provided below to answer. If the "
    "answer is not in the context, say exactly: I don't know.\n\n"
    "<CONTEXT>\n{ctx}\n</CONTEXT>"
)
_ANSWER_SYS_COLD = (
    "You are answering a single factual question about an internal company "
    "knowledge base. If you don't have the specific information being asked "
    "for, say exactly: I don't know. Do not make up details."
)


def _claude(prompt, model, max_usd, system_text="", timeout_s=180):
    """One claude -p call. json output, cost-capped, no local MCP.

    system_text (when given) is written to a temp file and passed via
    --append-system-prompt-file to dodge the 32K arg-line limit (the
    bench_enterprise_rag pattern).
    """
    clean_cwd = Path(tempfile.gettempdir()) / "helix-bench-clean-cwd"
    clean_cwd.mkdir(parents=True, exist_ok=True)
    empty_cfg = clean_cwd / "_empty_mcp.json"
    if not empty_cfg.exists():
        empty_cfg.write_text('{"mcpServers":{}}', encoding="utf-8")

    sp_name = None
    cmd = [
        "claude", "-p",
        "--model", model,
        "--tools", "",
        "--strict-mcp-config",
        "--mcp-config", str(empty_cfg),
        "--max-budget-usd", str(max_usd),
        "--output-format", "json",
    ]
    try:
        if system_text:
            sp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8")
            sp.write(system_text)
            sp.close()
            sp_name = sp.name
            cmd += ["--append-system-prompt-file", sp_name]
        cmd += ["--", prompt]

        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=str(clean_cwd),
                timeout=timeout_s, creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "text": "", "cost_usd": None,
                    "elapsed_s": timeout_s}
        elapsed = time.perf_counter() - t0
        if proc.returncode != 0:
            return {"status": "exit_nonzero", "rc": proc.returncode,
                    "stderr": (proc.stderr or "")[-300:], "text": "",
                    "cost_usd": None, "elapsed_s": elapsed}
        try:
            data = json.loads(proc.stdout)
        except Exception:
            return {"status": "json_error",
                    "stdout_tail": (proc.stdout or "")[-300:], "text": "",
                    "cost_usd": None, "elapsed_s": elapsed}
        return {"status": "ok", "text": data.get("result", "") or "",
                "cost_usd": float(data.get("total_cost_usd", 0) or 0),
                "elapsed_s": elapsed}
    finally:
        if sp_name:
            try:
                os.unlink(sp_name)
            except Exception:
                pass


def generate_answer(question, ctx_text, model, max_usd):
    system_text = (_ANSWER_SYS.format(ctx=ctx_text) if ctx_text
                   else _ANSWER_SYS_COLD)
    return _claude(question, model, max_usd, system_text=system_text)


# ---- Judge protocol (goal-gates spec sec.4) --------------------------------

_JUDGE_INSTRUCTIONS = (
    "You are a strict grader for a retrieval-augmented QA system. You will be "
    "given a QUESTION, a REFERENCE answer (ground truth), and a candidate "
    "ANSWER. Grade the candidate ANSWER using single-answer reference-guided "
    "grading. Output EXACTLY one token on the first line, one of:\n"
    "  CORRECT   - the answer conveys the same key fact(s) as the reference\n"
    "  INCORRECT - the answer states something that conflicts with, or is "
    "absent from, the reference (a confident wrong answer)\n"
    "  ABSTAINED - the answer declines to answer (e.g. says it does not know, "
    "or that the context lacks the information)\n"
    "Judge ONLY factual agreement with the reference; ignore style and length. "
    "After the verdict token, you may add one short line of justification."
)


def _judge_prompt(question, reference, answer):
    return (
        "QUESTION:\n{}\n\n".format(question)
        + "REFERENCE (ground truth):\n{}\n\n".format(
            reference or "(no reference provided)")
        + "CANDIDATE ANSWER:\n{}\n\n".format(answer)
        + "Verdict (CORRECT / INCORRECT / ABSTAINED) then one line why:"
    )


_VERDICT_RE = re.compile(r"\b(CORRECT|INCORRECT|ABSTAINED)\b", re.IGNORECASE)


def parse_verdict(text):
    """Extract the trinary verdict; default ABSTAINED if unparseable so an
    unreadable judge output never counts as a hallucination."""
    if not text:
        return "ABSTAINED"
    m = _VERDICT_RE.search(text)
    if not m:
        return "ABSTAINED"
    return m.group(1).upper()


def judge_answer(question, reference, answer, model, max_usd):
    r = _claude(_judge_prompt(question, reference, answer), model, max_usd,
                system_text=_JUDGE_INSTRUCTIONS)
    verdict = parse_verdict(r.get("text", "")) if r.get("status") == "ok" else "ERROR"
    return {"verdict": verdict, "raw": r.get("text", "")[:400],
            "status": r.get("status"), "cost_usd": r.get("cost_usd"),
            "elapsed_s": r.get("elapsed_s")}


# ---------------------------------------------------------------------------
# Resume: read completed ids from the output jsonl
# ---------------------------------------------------------------------------

def load_done_ids(out_path):
    done = set()
    if not out_path.is_file():
        return done
    with out_path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            qid = rec.get("id")
            if qid:
                done.add(str(qid))
    return done


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--erb-root", default=os.environ.get(
        "ERB_ROOT", r"F:\Projects\EnterpriseRAG-Bench-main"))
    ap.add_argument("--helix-url", default=os.environ.get(
        "HELIX_URL", "http://127.0.0.1:11437"))
    ap.add_argument("--out", required=True, help="Per-question output JSONL.")
    ap.add_argument("--summary-out", required=True, help="Summary JSON path.")
    ap.add_argument("--answer-model", default="sonnet",
                    help="claude -p model for answer + judge (Sonnet class).")
    ap.add_argument("--audit-model", default="opus",
                    help="claude -p model for the 10 pct audit rung (Opus class).")
    ap.add_argument("--answer-max-usd", type=float, default=0.20)
    ap.add_argument("--judge-max-usd", type=float, default=0.10)
    ap.add_argument("--audit-max-usd", type=float, default=0.40)
    ap.add_argument("--audit-fraction", type=float, default=0.10)
    ap.add_argument("--max-genes", type=int, default=8)
    ap.add_argument("--max-questions", type=int, default=None,
                    help="Cap questions (smoke). Default: all 500.")
    ap.add_argument("--types", default="",
                    help="Comma-separated question_type filter.")
    ap.add_argument("--seed", type=int, default=93,
                    help="RNG seed for the deterministic audit subsample.")
    args = ap.parse_args(argv)

    erb_root = Path(args.erb_root)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path = Path(args.summary_out)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    types = [t.strip() for t in args.types.split(",") if t.strip()] or None
    needles = load_needles(erb_root, max_questions=args.max_questions, types=types)
    if not needles:
        summary_path.write_text(json.dumps({
            "benchmark": "erb500k_scored", "issue": "#93",
            "error": ("no questions loaded from {} -- check --erb-root exists "
                      "on the rig".format(erb_root / "questions.jsonl")),
            "baselines": BASELINES,
        }, indent=2), encoding="utf-8")
        print("ERROR: no questions from {}".format(erb_root), file=sys.stderr)
        return 2

    # Deterministic audit subsample by id (stable across resumes).
    rng = random.Random(args.seed)
    ids = [nd["id"] for nd in needles]
    k_audit = max(1, int(round(len(ids) * args.audit_fraction)))
    audit_ids = set(rng.sample(ids, min(k_audit, len(ids))))

    done = load_done_ids(out_path)
    print("[erb500k] {} questions; {} already done; audit subsample={}".format(
        len(needles), len(done), len(audit_ids)))

    # Health check (fail fast if the sharded server isn't up / genes=0).
    try:
        h = httpx.get(args.helix_url + "/health", timeout=10).json()
        genes = h.get("genes") or h.get("document_count")
        print("[erb500k] helix health: genes={} pid={}".format(
            genes, h.get("pid")))
        if genes is not None and int(genes) == 0:
            print("[erb500k] WARN: server reports genes=0 -- sharded fixture "
                  "may not be mounted (HELIX_USE_SHARDS?)", file=sys.stderr)
    except Exception as exc:
        summary_path.write_text(json.dumps({
            "benchmark": "erb500k_scored", "issue": "#93",
            "error": "helix /health unreachable at {}: {}".format(
                args.helix_url, exc),
            "baselines": BASELINES,
        }, indent=2), encoding="utf-8")
        print("ERROR: helix unreachable: {}".format(exc), file=sys.stderr)
        return 2

    processed = 0
    with out_path.open("a", encoding="utf-8") as ofh:
        for i, nd in enumerate(needles, 1):
            qid = nd["id"]
            if qid in done:
                continue

            # (a) packet + verbatim know/miss
            packet = fetch_packet(args.helix_url, nd["question"], args.max_genes)
            gold_index, gold_canon = make_gold_index(nd["gold_paths"])
            gold_delivered = any(
                match_delivered(p, gold_index, gold_canon) is not None
                for p in packet.get("delivered_paths", [])
            )
            know_block = packet.get("know")
            miss_block = packet.get("miss")
            emitted = ("know" if know_block is not None
                       else "miss" if miss_block is not None else "neither")

            # (b) answer with Sonnet
            ans = generate_answer(nd["question"], packet.get("context_text", ""),
                                  args.answer_model, args.answer_max_usd)
            answer_text = ans.get("text", "")

            # (c) judge with Sonnet (trinary, reference-guided)
            jr = judge_answer(nd["question"], nd["gold_answer"], answer_text,
                              args.answer_model, args.judge_max_usd)
            verdict = jr["verdict"]

            # (d) 10% audit with Opus
            audit = None
            if qid in audit_ids:
                ar = judge_answer(nd["question"], nd["gold_answer"], answer_text,
                                  args.audit_model, args.audit_max_usd)
                audit = {"verdict": ar["verdict"], "raw": ar["raw"],
                         "status": ar["status"], "model": args.audit_model,
                         "cost_usd": ar["cost_usd"]}

            rec = {
                "id": qid,
                "type": nd["type"],
                "question": nd["question"][:500],
                "gold_answer": (nd["gold_answer"] or "")[:400],
                "expected_doc_ids": nd["expected_doc_ids"],
                "packet": {
                    "status": packet.get("status"),
                    "emitted": emitted,
                    "know": know_block,   # verbatim
                    "miss": miss_block,   # verbatim
                    "gold_delivered": gold_delivered,
                    "n_delivered": len(packet.get("delivered_paths", [])),
                    "raw_keys": packet.get("raw_keys"),
                },
                "answer": {
                    "text": answer_text[:2000],
                    "status": ans.get("status"),
                    "cost_usd": ans.get("cost_usd"),
                    "elapsed_s": round(ans.get("elapsed_s", 0), 2),
                },
                "judge": {
                    "verdict": verdict,
                    "raw": jr["raw"],
                    "model": args.answer_model,
                    "status": jr["status"],
                    "cost_usd": jr["cost_usd"],
                },
                "audit": audit,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            ofh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            ofh.flush()
            processed += 1
            if processed % 10 == 0:
                print("[erb500k] +{} (idx {}/{}) last: emitted={} verdict={}".format(
                    processed, i, len(needles), emitted, verdict))

    # ------------------------------------------------------------------
    # Summarize from the full jsonl (all rows, including prior-run rows).
    # ------------------------------------------------------------------
    summ = summarize(out_path, audit_ids)
    summ.update({
        "benchmark": "erb500k_scored",
        "issue": "#93",
        "fixture": "genomes/bench/matrix-sharded/enterprise_rag_500k",
        "helix_url": args.helix_url,
        "answer_model": args.answer_model,
        "audit_model": args.audit_model,
        "n_questions_total": len(needles),
        "audit_fraction": args.audit_fraction,
        "baselines": BASELINES,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    })
    summary_path.write_text(json.dumps(summ, indent=2), encoding="utf-8")
    print("=" * 60)
    print("[erb500k] rows={} answered={} correctness_among_answered={} "
          "hallucination_rate={} coverage={}".format(
              summ["n_rows"], summ["answered"],
              summ["correctness_among_answered"],
              summ["hallucination_rate"], summ["coverage"]))
    print("[erb500k] know-vs-judged agreement={}".format(
        summ["know_vs_judged_agreement"]))
    print("-> {}".format(out_path))
    print("-> {}".format(summary_path))
    return 0


def summarize(out_path, audit_ids):
    """Compute the gate metrics from the per-question jsonl.

    Metrics (goal-gates spec sec.4):
      correctness among answered = CORRECT / (CORRECT + INCORRECT)
      hallucination rate         = INCORRECT / (CORRECT + INCORRECT)
      coverage                   = answered / total   (abstains -> coverage loss)
      know-vs-judged agreement   = fraction of rows where (packet emitted 'know')
                                   agrees with (judge said CORRECT) -- the
                                   calibration signal: know should predict correct.
      audit agreement            = fraction of audited rows where the Opus audit
                                   verdict == the Sonnet judge verdict.
    """
    n_rows = 0
    correct = incorrect = abstained = judge_err = 0
    know_emitted = 0
    know_and_correct = 0
    know_rows = 0            # rows where packet emitted 'know'
    judged_rows = 0          # rows with a parseable trinary verdict
    agree_cells = 0          # know<->correct agreement over judged rows
    audit_n = audit_agree = 0

    if out_path.is_file():
        with out_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                n_rows += 1
                emitted = (rec.get("packet") or {}).get("emitted")
                is_know = (emitted == "know")
                if is_know:
                    know_emitted += 1
                v = (rec.get("judge") or {}).get("verdict")
                if v == "CORRECT":
                    correct += 1
                elif v == "INCORRECT":
                    incorrect += 1
                elif v == "ABSTAINED":
                    abstained += 1
                else:
                    judge_err += 1

                if v in ("CORRECT", "INCORRECT", "ABSTAINED"):
                    judged_rows += 1
                    judged_correct = (v == "CORRECT")
                    if is_know:
                        know_rows += 1
                        if judged_correct:
                            know_and_correct += 1
                    if (is_know and judged_correct) or (
                            not is_know and not judged_correct):
                        agree_cells += 1

                au = rec.get("audit")
                if isinstance(au, dict) and au.get("verdict") in (
                        "CORRECT", "INCORRECT", "ABSTAINED"):
                    audit_n += 1
                    if au["verdict"] == v:
                        audit_agree += 1

    answered = correct + incorrect
    result = {
        "n_rows": n_rows,
        "correct": correct,
        "incorrect": incorrect,
        "abstained": abstained,
        "judge_error": judge_err,
        "answered": answered,
        "know_emitted": know_emitted,
        "correctness_among_answered": round(correct / answered, 4) if answered else 0.0,
        "hallucination_rate": round(incorrect / answered, 4) if answered else 0.0,
        "coverage": round(answered / n_rows, 4) if n_rows else 0.0,
        "know_precision_correct": round(know_and_correct / know_rows, 4) if know_rows else 0.0,
        "know_vs_judged_agreement": round(agree_cells / judged_rows, 4) if judged_rows else 0.0,
        "audit_n": audit_n,
        "audit_agreement": round(audit_agree / audit_n, 4) if audit_n else 0.0,
        "audit_agreement_target": 0.80,
    }
    return result


if __name__ == "__main__":
    raise SystemExit(main())

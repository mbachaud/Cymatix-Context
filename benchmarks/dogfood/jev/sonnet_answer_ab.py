"""sonnet_answer_ab.py — does Jev help Sonnet 5.5 answer ERB questions correctly?

EXPERIMENTAL. Answer-level follow-up to ``jev_erb_packet_know.py``: that run
measured whether Jev can tell a packet holds gold (AUROC 0.906) and rank gold
first (MRR 0.921), but never checked an answer. Here Claude Sonnet 5.5 answers
from the SAME delivered packets and Claude Opus 5.5 grades each answer against
ERB's ``gold_answer`` + ``answer_facts``. No retrieval reruns and no Jev calls:
packets are the gene ids the Jev receipt recorded (text read from the bed,
read-only) and Jev's per-chunk / per-packet probabilities come from that
receipt.

Cohorts (seeded, recorded in ``sample.json``):
  crater  50  semantic+basic needles c4000 classcap did NOT deliver (gold
              absent from the packet), 25 per type, disjoint from the
              2026-10-19 strat-40 rephrase set
  ranked  50  delivered needles where Jev's top chunk is gold, allocated to
              ERB types in proportion to that pool

Arms (each needle answered twice, one request per arm):
  cymatix  documents in cymatix rank order, no scores
  jev      documents reordered by Jev chunk ``answers`` probability, each
           tagged with it, plus Jev's packet-level probability
Offline arm: the cymatix answers with a Jev gate (abstain when the packet
probability is below t) — no extra model calls.

Grading is blind to arm and cohort; an exact ``NOT_FOUND`` reply is labelled
not_found locally, everything else goes to the grader.

Model calls go through ``claude -p`` (subscription auth; this host has no
working API key, so the Batch API is unavailable) in a clean room: empty cwd,
``--setting-sources local``, no tools, no MCP, no slash commands, CLAUDE.md
and auto-memory disabled, custom system prompt. 2026-10-23 probes of that
configuration saw only the system prompt, the CLI's user-email/date reminder
and (CLI 2.1.308) a ~1k-token environment block; no CLAUDE.md, memory, hooks,
skills or tools. Both arms and the grader run on the same CLI binary. One
request at a time, >= 300 ms apart.

Stages (resumable per request; ``<run-dir>/run.lock`` holds the runner PID):
  select  -> sample.json
  answer  -> answers.jsonl
  grade   -> graded.jsonl
  report  -> receipt.json

Usage::

    python -I benchmarks/dogfood/jev/sonnet_answer_ab.py --run-dir F:/tmp/sonnet_jev_answer_2026-10-23 --stage all
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INPUTS_COMMIT = "f1a23ae3"   # claude/jev-packet-assembly-test-138065 head: Jev receipt + ERB banks
JEV_RECEIPT = "benchmarks/dogfood/jev/jev_erb_packet_know_c4000_classcap_2026-10-23.json"
NEEDLE_META = "benchmarks/dogfood/erb/needles_grain4000_meta.json"
STRAT40 = "benchmarks/dogfood/grain_ladder/crater_rephrase_strat40_graded_2026-10-19.json"
QUESTIONS = Path(r"F:\Projects\EnterpriseRAG-Bench-main\questions.jsonl")
BED = Path(r"F:\tmp\grain_erb_full_c4000_2026-10-18\erb_full_c4000_lean.db")
MAX_CHUNK_CHARS = 60_000     # the Jev harness's cap; receipt chars are checked against it
SEED = 20261023
ANSWER_MODEL = "claude-sonnet-5-5"
GRADER_MODEL = "claude-opus-5-5"
EFFORT = "high"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CLAUDE_BIN = ["claude"]   # --claude-bin: Opus 5.5 needs CLI >= 2.1.280

ANSWER_SYSTEM = (
    "You answer questions about a company's internal documents using only the retrieved "
    "documents provided. The documents may include near-duplicates and similar-looking "
    "records about other projects, teams or customers; answer only from a document that "
    "matches the question's specifics. If the documents answer the question, reply with "
    "one line: ANSWER: <concise answer with the specific values, names or facts asked "
    "for>. If they do not contain the answer, reply with exactly: NOT_FOUND. No other text."
)
JEV_NOTE = (
    "A relevance judge scored each document with judge_p, its probability that the "
    "document answers the question, and scored the whole set with packet_p, its "
    "probability that the documents together contain the answer. Documents are ordered "
    "by judge_p, highest first. Treat the scores as evidence, not ground truth."
)
GRADER_SYSTEM = (
    "You are a strict grader. You get a question, the reference gold answer, its key "
    "answer facts, and a candidate answer. Label the candidate: 'correct' = states the "
    "key facts the question asks for (wording may differ) and contradicts none; "
    "'partial' = some key facts right but others missing or wrong; 'incorrect' = a "
    "substantive answer that is wrong, unrelated, or contradicts the facts; 'not_found' "
    "= the candidate declines or says the information was not found. Reply with exactly "
    "one line: LABEL: <correct|partial|incorrect|not_found> | <reason, at most 20 words>"
)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                          encoding="utf-8", creationflags=NO_WINDOW).stdout.strip()


def git_json(path: str):
    raw = subprocess.run(["git", "show", f"{INPUTS_COMMIT}:{path}"], cwd=REPO_ROOT,
                         capture_output=True, check=True, creationflags=NO_WINDOW).stdout
    return json.loads(raw.decode("utf-8"))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------------------------------------------------------- model calls

def call_claude(run: Path, system: str, prompt: str, model: str, last_call: list) -> dict:
    """One clean-room ``claude -p`` request; prompt on stdin (packets reach ~48k chars)."""
    room = run / "cleanroom"
    room.mkdir(exist_ok=True)
    env = {**os.environ, "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    cmd = [CLAUDE_BIN[0], "-p", "--tools", "", "--strict-mcp-config", "--setting-sources", "local",
           "--disable-slash-commands", "--no-session-persistence", "--system-prompt", system,
           "--model", model, "--effort", EFFORT, "--output-format", "json"]
    wait = 0.3 - (time.monotonic() - last_call[0])
    if wait > 0:
        time.sleep(wait)
    for attempt in range(3):
        t0 = time.perf_counter()
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                               cwd=room, env=env, timeout=600, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            rec = {"status": "error", "error": "timeout 600 s"}
        else:
            try:
                j = json.loads(p.stdout)
            except json.JSONDecodeError:
                rec = {"status": "error", "error": f"rc={p.returncode} {p.stdout[:200]!r} {p.stderr[:200]!r}"}
            else:
                u = j.get("usage") or {}
                rec = {"status": "error" if j.get("is_error") else "succeeded",
                       "error": j.get("result") if j.get("is_error") else None,
                       "models": sorted((j.get("modelUsage") or {}).keys()),
                       "stop_reason": j.get("stop_reason"),
                       "text": (j.get("result") or "").strip(),
                       "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                       "cache_read": u.get("cache_read_input_tokens"),
                       "cache_write": u.get("cache_creation_input_tokens"),
                       "cost_usd": j.get("total_cost_usd")}
        rec["wall_s"] = round(time.perf_counter() - t0, 1)
        last_call[0] = time.monotonic()
        if rec["status"] == "succeeded":
            return rec
        print(f"  attempt {attempt + 1} failed: {str(rec.get('error'))[:200]}", flush=True)
        time.sleep(5.0 * (attempt + 1))
    return rec


# ---------------------------------------------------------------- select

def proportional(counts: dict, total: int) -> dict:
    """Largest-remainder allocation of ``total`` across ``counts``."""
    n = sum(counts.values())
    raw = {k: total * v / n for k, v in counts.items()}
    out = {k: int(x) for k, x in raw.items()}
    for k in sorted(raw, key=lambda k: (-(raw[k] - out[k]), k))[: total - sum(out.values())]:
        out[k] += 1
    return out


def stage_select(run: Path, n_crater: int, n_ranked: int) -> None:
    rec = git_json(JEV_RECEIPT)
    meta = git_json(NEEDLE_META)["needles"]
    strat40 = {r["needle"] for r in git_json(STRAT40)["rows"]}
    qs = {}
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            q = json.loads(line)
            qs[q["question_id"]] = q
    chunks = {}
    for c in rec["chunks"]:
        chunks.setdefault(c["needle"], []).append(c)

    rows = {r["needle"]: r for r in rec["needles"]}
    rng = random.Random(SEED)
    crater_pool = sorted(n for n, r in rows.items() if not r["truth_know"]
                         and r["erb_type"] in ("semantic", "basic") and n not in strat40)
    crater = []
    for t in ("semantic", "basic"):
        pool = [n for n in crater_pool if rows[n]["erb_type"] == t]
        crater += rng.sample(pool, n_crater // 2)

    def jev_top_is_gold(n):
        cs = sorted(chunks[n], key=lambda c: (-c["jev"]["answers"], c["rank"]))
        return cs[0]["is_gold"]

    ranked_pool = sorted(n for n, r in rows.items() if r["truth_know"] and jev_top_is_gold(n))
    alloc = proportional(Counter(rows[n]["erb_type"] for n in ranked_pool), n_ranked)
    ranked = []
    for t in sorted(alloc):
        ranked += rng.sample([n for n in ranked_pool if rows[n]["erb_type"] == t], alloc[t])

    conn = sqlite3.connect(f"file:{BED}?mode=ro", uri=True)
    items = []
    for cohort, names in (("crater", sorted(crater)), ("ranked", sorted(ranked))):
        for n in names:
            r = rows[n]
            q = qs[meta[n]["question_id"]]
            if q["question"].strip() != r["query"].strip():   # bank-offset hazard: join must hold by text
                raise SystemExit(f"{n}: question text mismatch for {meta[n]['question_id']}")
            docs = []
            for c in sorted(chunks[n], key=lambda c: c["rank"]):
                src, content = conn.execute("SELECT source_id, content FROM genes WHERE gene_id=?",
                                            (c["gene_id"],)).fetchone()
                text = (content or "")[:MAX_CHUNK_CHARS]
                if len(text) != c["chars"]:
                    raise SystemExit(f"{n}/{c['gene_id']}: {len(text)} chars, Jev saw {c['chars']}")
                docs.append({"rank": c["rank"], "gene_id": c["gene_id"], "source": src or "",
                             "text": text, "is_gold": c["is_gold"],
                             "jev_answers": c["jev"]["answers"]})
            items.append({
                "needle": n, "cohort": cohort, "erb_type": r["erb_type"],
                "question_id": q["question_id"], "question": q["question"],
                "gold_answer": q["gold_answer"], "answer_facts": q["answer_facts"],
                "cymatix_gold_rank": r["gold_rank"], "jev_packet": r["jev_packet_answers"],
                "docs": docs,
            })
    conn.close()
    (run / "sample.json").write_text(json.dumps({
        "seed": SEED, "inputs_commit": INPUTS_COMMIT,
        "pools": {"crater": len(crater_pool), "ranked": len(ranked_pool)},
        "excluded_strat40": len(strat40), "ranked_alloc": alloc, "items": items,
    }, indent=1), encoding="utf-8")
    print(f"selected crater={len(crater)} (pool {len(crater_pool)}) "
          f"ranked={len(ranked)} (pool {len(ranked_pool)}) alloc={alloc}", flush=True)


# ---------------------------------------------------------------- answer

def doc_block(i: int, d: dict, with_jev: bool) -> str:
    attrs = f'index="{i}" source="{d["source"]}"'
    if with_jev:
        attrs += f' judge_p="{d["jev_answers"]:.2f}"'
    return f"<document {attrs}>\n{d['text']}\n</document>"


def answer_prompt(item: dict, arm: str) -> str:
    if arm == "cymatix":
        docs = sorted(item["docs"], key=lambda d: d["rank"])
        head = "Retrieved documents, in retrieval rank order (most relevant first):"
    else:
        docs = sorted(item["docs"], key=lambda d: (-d["jev_answers"], d["rank"]))
        head = f"{JEV_NOTE}\n\npacket_p = {item['jev_packet']:.2f}\n\nRetrieved documents:"
    body = "\n\n".join(doc_block(i, d, arm == "jev") for i, d in enumerate(docs, 1))
    return f"Question: {item['question']}\n\n{head}\n\n{body}"


def stage_answer(run: Path) -> None:
    items = json.loads((run / "sample.json").read_text(encoding="utf-8"))["items"]
    out = run / "answers.jsonl"
    done = {(r["needle"], r["arm"]) for r in read_jsonl(out) if r["status"] == "succeeded"}
    todo = [(it, arm) for it in items for arm in ("cymatix", "jev") if (it["needle"], arm) not in done]
    print(f"answer: {len(done)} done, {len(todo)} to go", flush=True)
    last_call = [0.0]
    for k, (it, arm) in enumerate(todo, 1):
        prompt = answer_prompt(it, arm)
        rec = call_claude(run, ANSWER_SYSTEM, prompt, ANSWER_MODEL, last_call)
        row = {"needle": it["needle"], "arm": arm, "prompt_chars": len(prompt),
               "prompt_sha256": sha256_bytes(prompt.encode("utf-8")), **rec}
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        print(f"[{len(done) + k}/{len(items) * 2}] {it['needle']} {arm:7s} {rec['status']} "
              f"{rec.get('wall_s')}s {(rec.get('text') or rec.get('error') or '')[:90]!r}", flush=True)


# ---------------------------------------------------------------- grade

def candidate(text: str) -> str:
    t = (text or "").strip()
    return t.split(":", 1)[1].strip() if t.upper().startswith("ANSWER:") else t


def is_not_found(text: str) -> bool:
    return (text or "").strip().rstrip(".").upper() == "NOT_FOUND"


def latest_answers(run: Path) -> dict:
    """(needle, arm) -> last record, a success preferred over a later failure."""
    out = {}
    for r in read_jsonl(run / "answers.jsonl"):
        k = (r["needle"], r["arm"])
        if k not in out or r["status"] == "succeeded" or out[k]["status"] != "succeeded":
            out[k] = r
    return out


def stage_grade(run: Path) -> None:
    items = {it["needle"]: it for it in
             json.loads((run / "sample.json").read_text(encoding="utf-8"))["items"]}
    answers = latest_answers(run)
    out = run / "graded.jsonl"
    done = {(r["needle"], r["arm"]) for r in read_jsonl(out)
            if r["label"] not in ("grader_error", "answer_error")}
    # The grader sees only question, gold and candidate; order is shuffled so
    # arms of one needle are not graded back to back.
    keys = sorted(k for k in answers if k not in done)
    random.Random(SEED).shuffle(keys)
    print(f"grade: {len(done)} done, {len(keys)} to go", flush=True)
    last_call = [0.0]
    for k, key in enumerate(keys, 1):
        a, it = answers[key], items[key[0]]
        row = {"needle": key[0], "arm": key[1], "candidate": candidate(a.get("text"))}
        if a["status"] != "succeeded":
            row.update(label="answer_error", why=str(a.get("error"))[:300], grader=None)
        elif is_not_found(a["text"]):
            row.update(label="not_found", why="exact NOT_FOUND (local)", grader=None)
        else:
            prompt = json.dumps({"question": it["question"], "gold_answer": it["gold_answer"],
                                 "answer_facts": it["answer_facts"],
                                 "candidate": row["candidate"] or "(no answer)"},
                                ensure_ascii=False, indent=1)
            g = call_claude(run, GRADER_SYSTEM, prompt, GRADER_MODEL, last_call)
            m = re.match(r"\s*LABEL:\s*(correct|partial|incorrect|not_found)\s*\|?\s*(.*)",
                         g.get("text") or "", re.I | re.S)
            row.update(label=m.group(1).lower() if m else "grader_error",
                       why=(m.group(2).strip() if m else (g.get("text") or g.get("error") or ""))[:300],
                       grader=g)
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        print(f"[{len(done) + k}/{len(answers)}] graded {row['label']}", flush=True)


# ---------------------------------------------------------------- report

def auroc(scores: list, labels: list):
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 4)


LABELS = ("correct", "partial", "incorrect", "not_found")


def tally(rows: list) -> dict:
    c = Counter(r["label"] for r in rows)
    n = len(rows)
    return {"n": n, **{k: c.get(k, 0) for k in LABELS},
            "errors": n - sum(c.get(k, 0) for k in LABELS),
            "correct_rate": round(c.get("correct", 0) / n, 3) if n else None,
            "wrong_rate": round((c.get("partial", 0) + c.get("incorrect", 0)) / n, 3) if n else None}


def stage_report(run: Path) -> None:
    sample = json.loads((run / "sample.json").read_text(encoding="utf-8"))
    items = {it["needle"]: it for it in sample["items"]}
    answers = latest_answers(run)
    graded = {}
    for g in read_jsonl(run / "graded.jsonl"):   # last label per (needle, arm) wins
        graded[(g["needle"], g["arm"])] = g
    rows = []
    for (n, arm), g in sorted(graded.items()):
        it = items[n]
        rows.append({**g, "cohort": it["cohort"], "erb_type": it["erb_type"],
                     "jev_packet": it["jev_packet"], "cymatix_gold_rank": it["cymatix_gold_rank"],
                     "answer": answers[(n, arm)]})
    by = {(g["needle"], g["arm"]): g for g in rows}

    summary = {}
    for cohort in ("crater", "ranked", "all"):
        rs = [g for g in rows if cohort == "all" or g["cohort"] == cohort]
        s = {arm: tally([g for g in rs if g["arm"] == arm]) for arm in ("cymatix", "jev")}
        for arm in ("cymatix", "jev"):
            s[arm]["by_erb_type"] = {t: tally([g for g in rs if g["arm"] == arm and g["erb_type"] == t])
                                     for t in sorted({g["erb_type"] for g in rs})}
        cym = [g for g in rs if g["arm"] == "cymatix"]
        s["jev_gate_on_cymatix_arm"] = {}
        for t in (0.3, 0.5, 0.7):
            kept = [g for g in cym if g["jev_packet"] >= t and g["label"] != "not_found"]
            s["jev_gate_on_cymatix_arm"][str(t)] = {
                "answered": len(kept),
                "correct": sum(g["label"] == "correct" for g in kept),
                "wrong": sum(g["label"] in ("partial", "incorrect") for g in kept),
                "abstained": len(cym) - len(kept)}
        s["auroc_jev_packet_vs_correct"] = {arm: auroc(
            [g["jev_packet"] for g in rs if g["arm"] == arm],
            [g["label"] == "correct" for g in rs if g["arm"] == arm]) for arm in ("cymatix", "jev")}
        names = sorted({g["needle"] for g in rs if (g["needle"], "cymatix") in by and (g["needle"], "jev") in by})
        s["paired_cymatix_to_jev"] = dict(Counter(
            f"{by[(n, 'cymatix')]['label']}->{by[(n, 'jev')]['label']}" for n in names))
        summary[cohort] = s
    rk = [g for g in rows if g["cohort"] == "ranked"]
    summary["ranked_by_cymatix_gold_rank"] = {
        bucket: {arm: tally([g for g in rk if g["arm"] == arm and
                             ((g["cymatix_gold_rank"] == 1) == (bucket == "rank1"))])
                 for arm in ("cymatix", "jev")} for bucket in ("rank1", "rank_gt1")}

    def spend(recs):
        recs = [r for r in recs if r and r.get("status") == "succeeded"]
        return {"calls": len(recs),
                "input_tokens": sum((r.get("input_tokens") or 0) + (r.get("cache_read") or 0)
                                    + (r.get("cache_write") or 0) for r in recs),
                "output_tokens": sum(r.get("output_tokens") or 0 for r in recs),
                "cli_cost_usd": round(sum(r.get("cost_usd") or 0 for r in recs), 3),
                "models": sorted({m for r in recs for m in r.get("models") or []}),
                "wall_s": round(sum(r.get("wall_s") or 0 for r in recs), 1)}

    receipt = {
        "experimental": True,
        "harness": "benchmarks/dogfood/jev/sonnet_answer_ab.py",
        "commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "behind_master": git("rev-list", "--count", "HEAD..origin/master"),
        "inputs_commit": INPUTS_COMMIT, "jev_receipt": JEV_RECEIPT, "needle_meta": NEEDLE_META,
        "questions": str(QUESTIONS), "questions_sha256": sha256_bytes(QUESTIONS.read_bytes()),
        "bed": str(BED), "seed": SEED, "pools": sample["pools"], "ranked_alloc": sample["ranked_alloc"],
        "transport": "claude -p clean room (see module docstring)",
        "claude_bin": CLAUDE_BIN[0],
        "claude_version": subprocess.run([CLAUDE_BIN[0], "--version"], capture_output=True, text=True,
                                         creationflags=NO_WINDOW).stdout.strip(),
        "answer_model": ANSWER_MODEL, "grader_model": GRADER_MODEL, "effort": EFFORT,
        "spend": {"answer": spend(answers.values()), "grade": spend(g.get("grader") for g in rows)},
        "prompts": {"answer_system": ANSWER_SYSTEM, "jev_note": JEV_NOTE, "grader_system": GRADER_SYSTEM},
        "summary": summary, "rows": rows,
    }
    (run / "receipt.json").write_text(json.dumps(receipt, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("crater", "ranked", "ranked_by_cymatix_gold_rank")}, indent=1))
    print(json.dumps(receipt["spend"]))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # answers carry non-cp1252 text
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--stage", choices=("select", "answer", "grade", "report", "all"), default="all")
    ap.add_argument("--n-crater", type=int, default=50)
    ap.add_argument("--n-ranked", type=int, default=50)
    ap.add_argument("--claude-bin", default="claude")
    a = ap.parse_args()
    CLAUDE_BIN[0] = a.claude_bin
    version = subprocess.run([a.claude_bin, "--version"], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout.strip()
    print(f"claude bin: {a.claude_bin} ({version})", flush=True)
    run = Path(a.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    lock = run / "run.lock"
    if lock.exists():
        other = int(lock.read_text().strip() or 0)
        if other and other != os.getpid() and pid_alive(other):
            print(f"ERROR: {lock} held by live PID {other}", file=sys.stderr)
            return 2
    lock.write_text(str(os.getpid()))
    try:
        stages = ("select", "answer", "grade", "report") if a.stage == "all" else (a.stage,)
        for st in stages:
            if st == "select":
                if (run / "sample.json").exists() and a.stage == "all":
                    print("select: sample.json exists, reusing", flush=True)
                    continue
                stage_select(run, a.n_crater, a.n_ranked)
            else:
                {"answer": stage_answer, "grade": stage_grade, "report": stage_report}[st](run)
    finally:
        lock.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

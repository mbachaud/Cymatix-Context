r"""EnterpriseRAG-Bench harness for helix-context.

Loads questions from
``F:/Projects/EnterpriseRAG-Bench-main/questions.jsonl`` and runs them
through helix's /context (M2) or a no-context cold-prompt (M0), against
either Haiku 4.5 (via claude -p) or Gemma 4 e4b (via ollama).

Emits two artifacts per run:
  * native bench JSONL — per-needle full record (compatible with our
    existing analysis scripts)
  * Onyx-compatible answers JSONL — ``{"question_id":...,"answer":...,
    "document_ids":[...]}`` — feed straight into ``src/scripts/
    answer_evaluation/metrics_based_eval.py`` for leaderboard-comparable
    4-metric scoring.

Modes:
  --mode none    M0 anchor — raw question, no context
  --mode helix   M2 — helix /context injected as system prompt

Models:
  --model haiku  claude -p with Haiku 4.5
  --model gemma  ollama with gemma4:e4b

Examples:
  # M2 Haiku on full 500 questions
  python benchmarks/bench_enterprise_rag.py --mode helix --model haiku

  # M0 Gemma smoke (10 questions)
  python benchmarks/bench_enterprise_rag.py --mode none --model gemma --max-questions 10
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


REPO = Path(r"F:/Projects/EnterpriseRAG-Bench-main")
QUESTIONS_PATH = REPO / "questions.jsonl"
UUID_INDEX_PATH = REPO / "generated_data" / "uuid_index.json"
CORPUS_ROOT = REPO / "generated_data" / "sources"
RESULTS_ROOT = Path(__file__).resolve().parent.parent / "benchmarks" / "results"
HELIX_URL = "http://127.0.0.1:11437"
OLLAMA_URL = "http://localhost:11434"
CLAUDE_TIMEOUT_S = 120
OLLAMA_TIMEOUT_S = 300
CONTEXT_TIMEOUT_S = int(os.environ.get("BENCH_CONTEXT_TIMEOUT_S", "1200"))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Injected-context cap. Context is passed via --append-system-prompt-file (a
# file, not the arg-line), so this is a soft cap, not an OS limit. Env-override
# BENCH_MAX_CTX_CHARS lets a depth run lift it so dynamic's fuller body reaches
# the model (and is counted) instead of being truncated at the legacy 12K.
MAX_CTX_CHARS = int(os.environ.get("BENCH_MAX_CTX_CHARS", "12000"))

SYSTEM_PROMPT_INJECTED = (
    "You are answering a single factual question about an internal company "
    "knowledge base. Use ONLY the context provided below to answer. "
    "If the answer is not in the context, say exactly: I don't know.\n\n"
    "<CONTEXT>\n{ctx}\n</CONTEXT>"
)
SYSTEM_PROMPT_COLD = (
    "You are answering a single factual question about an internal company "
    "knowledge base. If you don't have the specific information being asked "
    "for, say exactly: I don't know. Do not make up details."
)


# ── Needle loading ────────────────────────────────────────────────────

def load_needles(max_questions: int | None = None,
                 question_types: list[str] | None = None,
                 per_type: int | None = None) -> list[dict]:
    """Load questions.jsonl and resolve gold dsids to corpus paths.

    ``per_type`` overrides ``max_questions`` and takes the first N matching
    each type — used for stratified smoke runs across all 10 categories,
    which the file's type-sorted layout otherwise makes painful.
    """
    uuid_index = json.loads(UUID_INDEX_PATH.read_text(encoding="utf-8"))
    needles = []
    per_type_counts: dict[str, int] = {}
    with QUESTIONS_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            q = json.loads(line)
            qtype = q["question_type"]
            if question_types and qtype not in question_types:
                continue
            if per_type is not None:
                if per_type_counts.get(qtype, 0) >= per_type:
                    continue
                per_type_counts[qtype] = per_type_counts.get(qtype, 0) + 1
            gold_paths = []
            for dsid in q.get("expected_doc_ids") or []:
                rel = uuid_index.get(dsid)
                if rel:
                    gold_paths.append(str(CORPUS_ROOT / rel))
            needles.append({
                "id": q["question_id"],
                "type": qtype,
                "question": q["question"],
                "gold_answer": q.get("gold_answer", ""),
                "answer_facts": q.get("answer_facts", []),
                "source_types": q.get("source_types", []),
                "expected_doc_ids": q.get("expected_doc_ids", []),
                "gold_paths": gold_paths,
            })
    if per_type is None and max_questions is not None:
        needles = needles[:max_questions]
    return needles


# ── Helix /context ────────────────────────────────────────────────────

def helix_context(query: str, gold_paths: list[str], session_id: str,
                  log: logging.Logger, query_type: str | None = None) -> tuple[str, dict]:
    """Fetch helix /context for ``query`` and return (text, meta).

    ``query_type`` (semantic-wiring arm) is sent only when provided, so the
    fixed-pipeline capture can exercise the /context arm (broaden + semantic
    dense weight). Omitted by the main bench loop -> body byte-identical.
    """
    body = {
        "query": query,
        "max_genes": int(os.environ.get("BENCH_MAX_GENES", "8")),
        "session_id": session_id,
        "ignore_delivered": True,  # bench: no session-delivery elision (skews scores)
    }
    if query_type:
        body["query_type"] = query_type
    t0 = time.perf_counter()
    try:
        resp = httpx.post(f"{HELIX_URL}/context", json=body,
                          timeout=CONTEXT_TIMEOUT_S)
        elapsed = time.perf_counter() - t0
    except Exception as exc:
        return "", {"status": "error", "error": str(exc),
                    "elapsed_s": time.perf_counter() - t0, "chars": 0}
    if resp.status_code != 200:
        return "", {"status": "http_error", "http": resp.status_code,
                    "elapsed_s": elapsed, "chars": 0}
    raw = resp.json()
    # Helix returns a list-of-one-dict; unwrap it.
    if isinstance(raw, list) and raw:
        data = raw[0]
    elif isinstance(raw, dict):
        data = raw
    else:
        return "", {"status": "bad_response", "raw_type": type(raw).__name__,
                    "elapsed_s": elapsed, "chars": 0}
    ctx_text = data.get("content", "") or data.get("context", "") or ""
    if len(ctx_text) > MAX_CTX_CHARS:
        ctx_text = ctx_text[:MAX_CTX_CHARS] + "\n…[truncated]"

    # Delivered paths come from <GENE src="..."> tags embedded in content.
    # source ids are relative paths like "sources/github/pr-18421-...json".
    delivered_paths = set()
    for m in re.finditer(r'<GENE\s+src="([^"]+)"', ctx_text):
        delivered_paths.add(m.group(1).replace("\\", "/"))

    # Use the prefix-tolerant matcher to compare delivered paths against
    # gold paths. Without this, helix's GENE-src rendering bug (~30 % of
    # confluence paths come back without their ``confluence/`` prefix)
    # caused 5-7 pp of every arm's ``gold_delivered=False`` rate to be
    # measurement contamination rather than real misses. Quantified
    # 2026-05-23; see tests/test_bench_path_match_prefix_stripped.py.
    gold_index, gold_canonicals = make_gold_index(gold_paths)
    gold_hit = any(
        match_delivered_to_gold(p, gold_index, gold_canonicals) is not None
        for p in delivered_paths
    )
    return ctx_text, {
        "status": "ok",
        "elapsed_s": elapsed,
        "chars": len(ctx_text),
        "n_genes": len(delivered_paths),
        "gold_delivered": gold_hit,
        "delivered_paths_sample": sorted(delivered_paths)[:8],
        "context_health": data.get("context_health"),
    }


# ── Claude -p invocation ──────────────────────────────────────────────

def run_claude(prompt: str, ctx_text: str, model_id: str,
               log: logging.Logger) -> dict:
    """Invoke claude -p with optional context as system prompt."""
    clean_cwd = Path(r"F:/tmp/bench-clean-cwd")
    clean_cwd.mkdir(parents=True, exist_ok=True)
    empty_cfg = clean_cwd / "_empty.json"
    if not empty_cfg.exists():
        empty_cfg.write_text('{"mcpServers":{}}', encoding="utf-8")

    sys_prompt = (SYSTEM_PROMPT_INJECTED.format(ctx=ctx_text)
                  if ctx_text else SYSTEM_PROMPT_COLD)
    # Always use --append-system-prompt-file to dodge 32K arg-line limit
    sp_tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    )
    sp_tmp.write(sys_prompt); sp_tmp.close()
    try:
        cmd = [
            "claude", "-p",
            "--model", model_id,
            "--tools", "",
            "--strict-mcp-config",
            "--mcp-config", str(empty_cfg),
            "--append-system-prompt-file", sp_tmp.name,
            "--output-format", "json",
            "--",
            prompt,
        ]
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=str(clean_cwd),
                timeout=CLAUDE_TIMEOUT_S, creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "answer": "",
                    "elapsed_s": CLAUDE_TIMEOUT_S}
        elapsed = time.perf_counter() - t0
        if proc.returncode != 0:
            return {"status": "error", "rc": proc.returncode,
                    "stderr": proc.stderr[:400], "elapsed_s": elapsed,
                    "answer": ""}
        try:
            data = json.loads(proc.stdout)
        except Exception:
            return {"status": "json_error",
                    "stdout_preview": proc.stdout[:400],
                    "elapsed_s": elapsed, "answer": ""}
        return {
            "status": "ok",
            "answer": data.get("result", "") or "",
            "tokens": {
                "input": data.get("usage", {}).get("input_tokens", 0),
                "output": data.get("usage", {}).get("output_tokens", 0),
            },
            "cost_usd": float(data.get("total_cost_usd", 0) or 0),
            "elapsed_s": elapsed,
        }
    finally:
        try: os.unlink(sp_tmp.name)
        except Exception: pass


# ── Ollama invocation ─────────────────────────────────────────────────

def run_ollama(prompt: str, ctx_text: str, model_id: str,
               log: logging.Logger) -> dict:
    sys_prompt = (SYSTEM_PROMPT_INJECTED.format(ctx=ctx_text)
                  if ctx_text else SYSTEM_PROMPT_COLD)
    body = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 600},
    }
    t0 = time.perf_counter()
    try:
        resp = httpx.post(f"{OLLAMA_URL}/api/chat", json=body,
                          timeout=OLLAMA_TIMEOUT_S)
    except Exception as exc:
        return {"status": "error", "error": str(exc),
                "elapsed_s": time.perf_counter() - t0, "answer": ""}
    elapsed = time.perf_counter() - t0
    if resp.status_code != 200:
        return {"status": "http_error", "http": resp.status_code,
                "elapsed_s": elapsed, "answer": ""}
    data = resp.json()
    msg = (data.get("message") or {}).get("content", "")
    return {"status": "ok", "answer": msg, "elapsed_s": elapsed,
            "cost_usd": 0.0,
            "tokens": {"input": data.get("prompt_eval_count", 0),
                       "output": data.get("eval_count", 0)}}


# ── Onyx-format dsid extraction from retrieved context ────────────────

DSID_RE = re.compile(r"dsid_[a-f0-9]{32}")


def _rel_after_sources(p: str) -> str:
    """Extract path-relative-to-sources/ from either an absolute path
    (F:/.../sources/foo) or a relative one (sources/foo or just foo).

    A path with no ``sources/`` marker is ALREADY relative to the corpus root
    (e.g. ``linear/design/X.json``) and is returned unchanged. The prior
    ``return None`` here silently zeroed gold matches in the recall sweep for
    every doc-type stored without the prefix (linear/slack/eng-sre); see
    tests/test_bench_enterprise_rag.py. Mirrors gate_analysis.canon and the
    nested rel_after_sources in helix_context (which already passed through)."""
    n = str(p).replace("\\", "/")
    if "/sources/" in n:
        return n.split("/sources/", 1)[1]
    if n.startswith("sources/"):
        return n[len("sources/"):]
    return n


def make_gold_index(gold_paths):
    """Build a robust path → canonical-gold-key index for delivered-path
    matching.

    Returns ``(index, canonicals)`` where:

    - ``canonicals`` is the set of canonical gold keys (post-
      :func:`_rel_after_sources`), e.g. ``confluence/architecture-and-
      standards/adr-015.json``.
    - ``index`` is a dict mapping BOTH the canonical key AND the source-
      prefix-stripped form to the canonical key. The stripped form lets
      a delivered path that lost its source prefix (e.g.
      ``architecture-and-standards/adr-015.json``) still resolve to the
      gold key (``confluence/architecture-and-standards/adr-015.json``).

    Works around the bench-measurement bug quantified 2026-05-23: helix's
    ``<GENE src="...">`` tag delivers ~30 % of ``confluence`` paths (and
    a smaller fraction of other sources) without the source prefix. The
    canonical-only matcher then reported ``gold_delivered=False`` on
    successful deliveries, depressing headline gd rates by 5-7 pp on
    every arm we have run. See
    ``tests/test_bench_path_match_prefix_stripped.py``.
    """
    canonicals: set[str] = set()
    index: dict[str, str] = {}
    for p in gold_paths or ():
        canonical = _rel_after_sources(p)
        if not canonical:
            continue
        canonicals.add(canonical)
        index[canonical] = canonical
        first_slash = canonical.find("/")
        if first_slash > 0:
            stripped = canonical[first_slash + 1:]
            # `setdefault` so a later gold that happens to alias an
            # earlier canonical's stripped form doesn't overwrite it.
            index.setdefault(stripped, canonical)
    return index, canonicals


def match_delivered_to_gold(delivered_path, gold_index, gold_canonicals):
    """Given a delivered path (any shape: absolute Windows path,
    ``sources/``-prefixed, bare relative ``<src>/<rest>``, or source-
    prefix-stripped ``<sub>/<rest>``) return the canonical gold key if a
    match is found, else ``None``.

    Lookup is O(1) per call via the index built by :func:`make_gold_index`.
    """
    if not delivered_path:
        return None
    rel = _rel_after_sources(delivered_path)
    if not rel:
        return None
    if rel in gold_canonicals:
        return rel
    if rel in gold_index:
        return gold_index[rel]
    return None


def make_uuid_reverse_with_stripped(uuid_index):
    """Build a robust ``path → dsid`` reverse map for
    :func:`extract_dsids`.

    For each ``(dsid, path)`` in ``uuid_index.json`` (where the path is
    the canonical ``<src>/<rest>``), the returned dict carries:

    - the canonical key ``<src>/<rest>`` → dsid (primary)
    - the source-prefix-stripped key ``<rest>`` → dsid (fallback,
      ``setdefault`` so an existing canonical mapping is never
      overwritten)

    This is the dsid-extraction counterpart of :func:`make_gold_index`
    and addresses the same ``<GENE src="...">`` prefix-strip bug:
    without the stripped-form key, every confluence doc that comes back
    without its ``confluence/`` prefix produces an empty
    ``predicted_doc_ids`` and reads as a doc-recall miss in the Onyx
    scorer.
    """
    out: dict[str, str] = {}
    for dsid, path in (uuid_index or {}).items():
        canonical = str(path).replace("\\", "/")
        out[canonical] = dsid
    # Second pass for stripped forms — done after canonical pass so a
    # later dsid's stripped form cannot overwrite an earlier dsid's
    # canonical mapping (the collision-safety pinned by the test).
    for dsid, path in (uuid_index or {}).items():
        canonical = str(path).replace("\\", "/")
        first_slash = canonical.find("/")
        if first_slash > 0:
            stripped = canonical[first_slash + 1:]
            out.setdefault(stripped, dsid)
    return out


def extract_dsids(ctx_text: str, delivered_paths: list[str],
                  uuid_index_reverse: dict[str, str]) -> list[str]:
    """Find dsid_xxx mentions in context + reverse-lookup delivered paths
    (whether absolute, sources/relative, or already a rel-path) via the
    uuid_index."""
    found = set(DSID_RE.findall(ctx_text))
    for p in delivered_paths:
        rel = _rel_after_sources(p) or str(p).replace("\\", "/")
        dsid = uuid_index_reverse.get(rel)
        if dsid:
            found.add(dsid)
    return sorted(found)


# ── Main run loop ─────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mode", choices=["none", "helix"], required=True)
    parser.add_argument("--model", choices=["haiku", "sonnet", "gemma"], required=True)
    parser.add_argument("--max-questions", type=int, default=None)
    parser.add_argument("--per-type", type=int, default=None,
                        help="Take first N per question_type (stratified). "
                             "Overrides --max-questions when set.")
    parser.add_argument("--types", help="Comma-separated question types")
    parser.add_argument(
        "--helix-url", default=HELIX_URL,
        help=f"Helix server URL (default {HELIX_URL}). Used when --mode helix.",
    )
    parser.add_argument(
        "--external-helix", action="store_true",
        help="Don't try to start helix; assume it's already running at --helix-url",
    )
    parser.add_argument(
        "--resume-dir",
        help="Resume into an existing run dir: skip question_ids already present "
             "in its onyx_answers.jsonl and append the remainder in place.",
    )
    args = parser.parse_args()

    types_filter = (
        [t.strip() for t in args.types.split(",")] if args.types else None
    )
    needles = load_needles(
        max_questions=args.max_questions,
        question_types=types_filter,
        per_type=args.per_type,
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    resuming = bool(args.resume_dir)
    if resuming:
        run_dir = Path(args.resume_dir)
        done_ids: set[str] = set()
        _onyx_done = run_dir / "onyx_answers.jsonl"
        if _onyx_done.exists():
            with _onyx_done.open(encoding="utf-8") as dfh:
                for line in dfh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        done_ids.add(json.loads(line)["question_id"])
                    except Exception:
                        pass
        _before = len(needles)
        needles = [n for n in needles if n["id"] not in done_ids]
        print(f"resume: {run_dir} has {len(done_ids)} answered; "
              f"{_before - len(needles)} skipped, {len(needles)} to run")
    else:
        run_dir = RESULTS_ROOT / f"enterprise_rag_{args.mode}_{args.model}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "run.log"

    logger = logging.getLogger("bench.enterprise_rag")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt); logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt); logger.addHandler(sh)

    logger.info("=== EnterpriseRAG-Bench harness ===")
    logger.info("mode=%s model=%s n_questions=%d",
                args.mode, args.model, len(needles))
    logger.info("run_dir=%s", run_dir)

    model_id = {"haiku": "haiku", "sonnet": "sonnet", "gemma": "gemma4:e4b"}[args.model]
    run_fn = run_ollama if args.model == "gemma" else run_claude

    if args.mode == "helix":
        # Validate helix is responsive
        try:
            # /health enumerates per-shard state on sharded fixtures; on v2's
            # 100-shard topology a single response takes ~6s. 5s was too tight.
            h = httpx.get(f"{args.helix_url}/health", timeout=30).json()
            logger.info("helix OK: genes=%s pid=%s",
                        h.get("genes"), h.get("pid"))
        except Exception as exc:
            logger.error("helix /health unreachable at %s: %s",
                         args.helix_url, exc)
            return 2

    # Reverse uuid_index for dsid extraction. The robust variant adds a
    # source-prefix-stripped fallback for each entry so confluence paths
    # delivered without their ``confluence/`` prefix still resolve to
    # the right dsid (the bench-measurement bug quantified 2026-05-23,
    # see tests/test_bench_path_match_prefix_stripped.py).
    uuid_idx = json.loads(UUID_INDEX_PATH.read_text(encoding="utf-8"))
    uuid_reverse = make_uuid_reverse_with_stripped(uuid_idx)
    logger.info("uuid_index reverse loaded: %d entries", len(uuid_reverse))

    native_path = run_dir / "needles.jsonl"
    onyx_path = run_dir / "onyx_answers.jsonl"

    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode, "model": model_id,
        "n_questions": len(needles),
        "total_cost_usd": 0.0,
        "ok": 0, "err": 0, "gold_delivered": 0,
    }

    _open_mode = "a" if resuming else "w"
    with native_path.open(_open_mode, encoding="utf-8") as nfh, \
         onyx_path.open(_open_mode, encoding="utf-8") as ofh:
        for i, needle in enumerate(needles, 1):
            logger.info("[%d/%d %s %s]", i, len(needles),
                        needle["type"], needle["id"])
            if args.mode == "helix":
                session_id = f"bench-{needle['id']}-{int(time.time()*1e9)}"
                ctx_text, ctx_meta = helix_context(
                    needle["question"], needle["gold_paths"],
                    session_id, logger,
                )
            else:
                ctx_text, ctx_meta = "", {
                    "status": "skipped", "chars": 0,
                    "gold_delivered": False, "n_genes": 0,
                    "delivered_paths_sample": [],
                }
            llm_r = run_fn(needle["question"], ctx_text, model_id, logger)
            answer = llm_r.get("answer", "")
            dsids = extract_dsids(
                ctx_text, ctx_meta.get("delivered_paths_sample", []),
                uuid_reverse,
            )
            record = {
                "id": needle["id"], "type": needle["type"],
                "question": needle["question"],
                "gold_answer": needle["gold_answer"][:300],
                "expected_doc_ids": needle["expected_doc_ids"],
                "ctx": ctx_meta,
                "llm": {k: v for k, v in llm_r.items() if k != "stderr"},
                "answer": answer[:2000],
                "predicted_doc_ids": dsids,
                "cost_usd": llm_r.get("cost_usd", 0.0),
            }
            nfh.write(json.dumps(record) + "\n"); nfh.flush()
            ofh.write(json.dumps({
                "question_id": needle["id"],
                "answer": answer,
                "document_ids": dsids,
            }) + "\n"); ofh.flush()
            summary["total_cost_usd"] += llm_r.get("cost_usd", 0.0)
            if llm_r.get("status") == "ok": summary["ok"] += 1
            else: summary["err"] += 1
            if ctx_meta.get("gold_delivered"):
                summary["gold_delivered"] += 1

            if i % 25 == 0:
                logger.info(
                    "  CHECKPOINT @%d: ok=%d err=%d gold=%d cost=$%.4f",
                    i, summary["ok"], summary["err"],
                    summary["gold_delivered"], summary["total_cost_usd"],
                )

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["total_cost_usd"] = round(summary["total_cost_usd"], 4)
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )

    logger.info("=" * 60)
    logger.info("DONE  ok=%d/%d  errors=%d  gold_delivered=%d  cost=$%.4f",
                summary["ok"], len(needles), summary["err"],
                summary["gold_delivered"], summary["total_cost_usd"])
    logger.info("native: %s", native_path)
    logger.info("onyx:   %s", onyx_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

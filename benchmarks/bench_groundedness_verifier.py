r"""M2-V groundedness verifier — second-pass classification of bench answers.

Given a context-injected bench JSONL (e.g. injected_helix_*/medium.jsonl),
re-grade each needle's (question, retrieved_context, answer_text) tuple
with an independent verifier LLM call that classifies into four buckets:

  GROUNDED:    the answer is explicitly derivable from the provided context
  UNSUPPORTED: the answer may be correct but is NOT in the context (i.e. the
               LLM answered from training-data priors or made a lucky guess
               that happens to match the accept-list)
  CONTRADICTED: the answer is wrong AND the context contains different info
  INSUFFICIENT_CONTEXT: the model abstained ("I don't know") and the context
                        does not contain the answer (correct abstention)

Cross-tabulation against the original score gives the publishable
decomposition that protects the M2 headline from the reviewer attack
"how many of the 22 correct answers were actually grounded in retrieved
context, vs. lucky training-data fallbacks?"

This is the M2-V cell from seat 3 of the 2026-05-20 council review.

Verifier model: Sonnet 4.6 by default (smarter than Haiku — separate
blind spots from the answerer, important for honest grading). Use
--verifier-model haiku for cost-only runs.

Usage:
  python benchmarks/bench_groundedness_verifier.py <input_jsonl> [...]

  python benchmarks/bench_groundedness_verifier.py \
      F:/Projects/helix-context/benchmarks/results/injected_helix_20260520T204934Z/medium.jsonl

Output:
  Writes verifier_<UTC-timestamp>.jsonl + verifier_summary.json into the
  SAME run dir as the input. Each record contains:
    - input_record (the original answerer record)
    - verifier_class (GROUNDED / UNSUPPORTED / CONTRADICTED / INSUFFICIENT_CONTEXT)
    - verifier_reason (short rationale from the verifier)
    - cost_usd (verifier API cost for this record)
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


CLAUDE_TIMEOUT_S = 120
VERIFIER_CLASSES = ("GROUNDED", "UNSUPPORTED", "CONTRADICTED", "INSUFFICIENT_CONTEXT")
CLEAN_CWD = Path(r"F:\tmp\bench-clean-cwd")
MAX_CTX_CHARS = 60_000  # match the answerer's cap


VERIFIER_SYSTEM_PROMPT = """You are a strict, impartial grader. You judge whether a model's ANSWER is grounded in the CONTEXT it was given.

You will be shown:
  - QUESTION: a single factual question
  - CONTEXT: the system context the answering model received (may be empty)
  - ANSWER: the answering model's response
  - EXPECTED: the gold answer string (and accept-list of equivalents)

Classify the ANSWER into EXACTLY ONE of these four classes:

GROUNDED
  The answer is explicitly derivable from the CONTEXT. The exact value or its paraphrase
  appears in the CONTEXT, and the ANSWER cites or restates it. Even if the answer is wrong,
  if it is faithfully reflecting what the CONTEXT said, classify as GROUNDED.

UNSUPPORTED
  The answer's substantive claim is NOT present in the CONTEXT. The model answered from its
  own prior knowledge, training data, or guesswork. The answer may happen to be correct, but
  the CONTEXT does not contain the answer. (For empty CONTEXT, any non-abstain answer is
  UNSUPPORTED.)

CONTRADICTED
  The CONTEXT contains information that DISAGREES with the answer. The model gave an answer
  that the CONTEXT refutes. (This is the most serious failure mode — helix-baited
  hallucination.)

INSUFFICIENT_CONTEXT
  The model abstained ("I don't know" or equivalent) AND the CONTEXT does not contain the
  answer. This is a correct refusal.

Edge cases:
  - If the model abstained but the CONTEXT DOES contain the answer, that is UNSUPPORTED
    (specifically: "abstained despite support" — the answerer missed a known signal).
  - If the model gave a multi-part answer where some parts are GROUNDED and some are
    UNSUPPORTED, pick the class that fits the SUBSTANTIVE claim — i.e. the part that
    matches/misses the EXPECTED answer.
  - Paraphrasing is fine. "Six steps" and "6 stages" are GROUNDED to the same fact.

Output format — EXACTLY this shape, no markdown, no prose preamble:
  CLASS: <one of GROUNDED|UNSUPPORTED|CONTRADICTED|INSUFFICIENT_CONTEXT>
  REASON: <one short sentence explaining the classification>
"""


def build_verifier_prompt(record: dict, ctx_text: str) -> str:
    """Construct the user-message text passed to the verifier."""
    expected = record.get("expected", "")
    accept = record.get("accept", [])
    accept_str = ", ".join(f'"{a}"' for a in accept) if accept else "—"
    answer = (record.get("answer_text") or "")[:4000]
    ctx_preview = ctx_text[:30_000]  # cap to keep verifier prompt under cwLimits
    truncated_note = (
        f"\n\n[CONTEXT TRUNCATED from {len(ctx_text)} to {len(ctx_preview)} chars]"
        if len(ctx_text) > len(ctx_preview) else ""
    )
    return (
        f"QUESTION: {record.get('query', '')}\n\n"
        f"EXPECTED: {expected!r}  (accept-list: {accept_str})\n\n"
        f"CONTEXT:\n```\n{ctx_preview}\n```{truncated_note}\n\n"
        f"ANSWER:\n```\n{answer}\n```\n\n"
        f"Now classify the ANSWER. Output exactly two lines:\n"
        f"CLASS: <one word>\nREASON: <short sentence>"
    )


CLASS_RE = re.compile(r"^\s*CLASS:\s*(\w+)\s*$", re.IGNORECASE | re.MULTILINE)
REASON_RE = re.compile(r"^\s*REASON:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_verifier_output(text: str) -> tuple[str, str]:
    """Pull (class, reason) out of the verifier response."""
    cls = "PARSE_ERROR"
    reason = ""
    m = CLASS_RE.search(text)
    if m:
        candidate = m.group(1).upper()
        if candidate in VERIFIER_CLASSES:
            cls = candidate
        else:
            cls = f"PARSE_ERROR:{candidate}"
    m = REASON_RE.search(text)
    if m:
        reason = m.group(1)[:500]
    return cls, reason


def run_verifier(
    record: dict,
    verifier_model: str,
    max_usd: float,
    log: logging.Logger,
) -> dict:
    """Call claude -p as the verifier with strict tool/MCP isolation."""
    # Reconstruct the context the answerer saw. The answerer stored
    # context metadata but not the literal text — for the helix and
    # oracle modes, we re-fetch from the source. For 'none' mode, ctx
    # is empty.
    ctx_text = ""
    ctx_meta = record.get("context", {}) or {}
    ctx_source = ctx_meta.get("source", "none")

    # Re-derive the context the answerer saw. This is approximate for
    # helix mode (we re-call /context, which may return different
    # content if session_delivery state changed) but exact for oracle.
    if ctx_source == "oracle":
        gs = record.get("gold_source") or []
        if gs:
            path = Path(r"F:\Projects") / gs[0].replace("/", os.sep)
            if path.is_file():
                try:
                    ctx_text = path.read_text(encoding="utf-8", errors="replace")[:MAX_CTX_CHARS]
                except Exception:
                    ctx_text = ""
    elif ctx_source == "helix":
        # Re-call /context with a fresh session_id to reconstruct what
        # the answerer saw. Helix retrieval is deterministic for a given
        # query (verified during 2026-05-20 bench debugging: gold% and
        # MRR identical across cells), and fresh session_id bypasses
        # session_delivery elision so we get the full content. The
        # reconstructed context is functionally identical to what the
        # answerer saw at run time.
        import httpx as _httpx
        session_id = f"verifier-{record.get('needle','?')}-{int(time.time_ns())}"
        helix_url = os.environ.get("HELIX_URL", "http://127.0.0.1:11437")
        try:
            resp = _httpx.post(
                f"{helix_url}/context",
                json={
                    "query": record.get("query", ""),
                    "decoder_mode": "condensed",
                    "session_id": session_id,
                },
                timeout=30.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                entry = data[0] if isinstance(data, list) and data else {}
                ctx_text = (entry.get("content") or "")[:MAX_CTX_CHARS]
        except Exception:
            citations = record.get("retrieval", {}).get("delivered_sources", []) or []
            ctx_text = (
                "[helix mode — re-fetch failed, falling back to citation list]\n\n"
                + "\n".join(f"  - {s}" for s in citations[:20])
            )
    # else: ctx_source == "none" — ctx_text stays empty

    prompt_text = build_verifier_prompt(record, ctx_text)

    # Write system prompt + user prompt to temp file (Windows arg cap)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".verifier-sys.txt", delete=False
    ) as sys_fh:
        sys_fh.write(VERIFIER_SYSTEM_PROMPT)
        sys_prompt_path = sys_fh.name

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".verifier-user.txt", delete=False
    ) as user_fh:
        user_fh.write(prompt_text)
        user_prompt_path = user_fh.name

    cmd = [
        "claude", "-p",
        "--tools", "",
        "--output-format", "json",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
        "--model", verifier_model,
        "--max-budget-usd", str(max_usd),
        "--system-prompt-file", sys_prompt_path,
        "--",
    ]
    # The user prompt is passed as the positional arg — read from the
    # file we just wrote, since prompts can also be long.
    cmd.append(Path(user_prompt_path).read_text(encoding="utf-8"))

    t0 = time.perf_counter()
    try:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT_S,
                cwd=str(CLEAN_CWD),
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {
                "verifier_class": "TIMEOUT",
                "verifier_reason": "",
                "cost_usd": None,
                "elapsed_s": CLAUDE_TIMEOUT_S,
                "ctx_source": ctx_source,
                "ctx_chars_seen_by_verifier": len(ctx_text),
            }
        except Exception as exc:
            return {
                "verifier_class": "ERROR",
                "verifier_reason": str(exc)[:200],
                "cost_usd": None,
                "elapsed_s": time.perf_counter() - t0,
                "ctx_source": ctx_source,
                "ctx_chars_seen_by_verifier": len(ctx_text),
            }
    finally:
        for p in (sys_prompt_path, user_prompt_path):
            try:
                os.unlink(p)
            except OSError:
                pass

    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        return {
            "verifier_class": "EXIT_NONZERO",
            "verifier_reason": (proc.stderr or "")[:200],
            "cost_usd": None,
            "elapsed_s": elapsed,
            "ctx_source": ctx_source,
            "ctx_chars_seen_by_verifier": len(ctx_text),
        }

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "verifier_class": "JSON_DECODE_ERROR",
            "verifier_reason": (proc.stdout or "")[:200],
            "cost_usd": None,
            "elapsed_s": elapsed,
            "ctx_source": ctx_source,
            "ctx_chars_seen_by_verifier": len(ctx_text),
        }

    answer_text = result.get("result") or ""
    cls, reason = parse_verifier_output(answer_text)
    cost_usd = (
        result.get("total_cost_usd")
        or result.get("total_cost")
        or result.get("cost_usd")
    )
    log.info(
        "  %-22s class=%s ctx=%dc cost=$%s",
        record.get("needle", "?"),
        cls,
        len(ctx_text),
        f"{cost_usd:.4f}" if cost_usd is not None else "?",
    )
    return {
        "verifier_class": cls,
        "verifier_reason": reason,
        "cost_usd": cost_usd,
        "elapsed_s": elapsed,
        "ctx_source": ctx_source,
        "ctx_chars_seen_by_verifier": len(ctx_text),
        "raw_verifier_output": answer_text[:600],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_jsonl", type=Path,
                        help="Path to a *.jsonl from bench_context_injected.py")
    parser.add_argument("--verifier-model", default="sonnet",
                        help="Verifier model: sonnet (default) or haiku")
    parser.add_argument("--max-usd", type=float, default=0.10,
                        help="Per-verifier-call budget cap")
    parser.add_argument("--limit", type=int, default=None,
                        help="Optional cap on # records (smoke runs)")
    args = parser.parse_args()

    if not args.input_jsonl.exists():
        print(f"ERROR: input jsonl not found: {args.input_jsonl}", file=sys.stderr)
        return 2

    run_dir = args.input_jsonl.parent
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_jsonl = run_dir / f"verifier_{stamp}.jsonl"
    out_summary = run_dir / f"verifier_summary_{stamp}.json"
    log_path = run_dir / f"verifier_{stamp}.log"

    logger = logging.getLogger("bench.verifier")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8"); fh.setFormatter(fmt); logger.addHandler(fh)
    sh = logging.StreamHandler(); sh.setFormatter(fmt); logger.addHandler(sh)

    logger.info("VERIFIER RUN START")
    logger.info("  input:   %s", args.input_jsonl)
    logger.info("  model:   %s", args.verifier_model)
    logger.info("  max-usd: %.2f per record", args.max_usd)
    logger.info("  output:  %s", out_jsonl)

    records = []
    with args.input_jsonl.open(encoding="utf-8") as fh:
        for line in fh:
            records.append(json.loads(line))
    if args.limit:
        records = records[:args.limit]
    logger.info("  records: %d", len(records))

    out_records = []
    cross_tab: dict[tuple[int, str], int] = {}
    total_cost = 0.0
    with out_jsonl.open("w", encoding="utf-8") as out_fh:
        for i, rec in enumerate(records, 1):
            logger.info("[%d/%d] %s (orig score=%+d)",
                        i, len(records), rec.get("needle"), rec.get("score", 0))
            verifier = run_verifier(rec, args.verifier_model, args.max_usd, logger)
            out_rec = {
                "needle": rec.get("needle"),
                "query": rec.get("query"),
                "expected": rec.get("expected"),
                "answerer_score": rec.get("score"),
                "answerer_score_reason": rec.get("score_reason"),
                "retrieval_gold_delivered": (rec.get("retrieval") or {}).get("gold_delivered"),
                **verifier,
            }
            out_records.append(out_rec)
            out_fh.write(json.dumps(out_rec) + "\n")
            out_fh.flush()
            key = (rec.get("score", 0), verifier["verifier_class"])
            cross_tab[key] = cross_tab.get(key, 0) + 1
            if verifier.get("cost_usd"):
                total_cost += verifier["cost_usd"]

    # Summary
    by_class: dict[str, int] = {}
    for r in out_records:
        by_class[r["verifier_class"]] = by_class.get(r["verifier_class"], 0) + 1

    summary = {
        "input_jsonl": str(args.input_jsonl),
        "verifier_model": args.verifier_model,
        "records": len(out_records),
        "total_cost_usd": round(total_cost, 4),
        "by_verifier_class": by_class,
        "cross_tab_score_x_class": {
            f"score={s},class={c}": n for (s, c), n in cross_tab.items()
        },
    }
    out_summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info("=" * 60)
    logger.info("DONE.")
    logger.info("  classes: %s", by_class)
    logger.info("  cost:    $%.4f", total_cost)
    logger.info("  output:  %s", out_summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

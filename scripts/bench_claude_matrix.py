r"""Run the 10-needle bench against all 6 frozen fixtures via ``claude -p``.

Per fixture:
  1. POST /admin/swap-db pointing at the fixture's primary .db
  2. For each needle, invoke ``claude -p --output-format json`` so the
     model answers via helix-context MCP (which now hits the swapped db)
  3. Also call /context directly per needle for retrieval-only metrics
  4. Score answer ∈ {-1, 0, +1} via word-boundary accept-match
  5. Write per-fixture JSONL into a timestamped results dir

Pre-conditions:
  * Helix server running on http://127.0.0.1:11437.
  * For sharded fixtures, server must be started with HELIX_USE_SHARDS=1
    so the auto-detect path in helix_context.sharding.open_read_source
    promotes main.genome.db to a ShardedGenomeAdapter on swap. (Required
    by the path/filename heuristic since /admin/swap-db today is path-only.)
  * Claude Code CLI available on PATH and the helix-context MCP wired up
    in the user's settings/.mcp.json. New ``claude -p`` subprocesses
    discover the MCP automatically.

Output (no overwrite — per-run subdir):
  benchmarks/results/claude_matrix_<UTC-timestamp>/
    summary.json
    small.jsonl
    medium.jsonl
    large.jsonl
    xl.jsonl
    medium-sharded.jsonl
    xl-sharded.jsonl
    run.log

Usage:
  python scripts/bench_claude_matrix.py
  python scripts/bench_claude_matrix.py --only small,medium
  python scripts/bench_claude_matrix.py --skip xl-sharded
  python scripts/bench_claude_matrix.py --model sonnet --max-usd 0.20
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


# ── Configuration ────────────────────────────────────────────────────────

HELIX_URL = os.environ.get("HELIX_URL", "http://127.0.0.1:11437")
MANIFEST = Path(r"F:\Projects\helix-context\genomes\bench\matrix\frozen.json")
RESULTS_ROOT = Path(r"F:\Projects\helix-context\benchmarks\results")

CLAUDE_TIMEOUT_S = 300        # 5 min per question — generous for MCP + reasoning
SWAP_TIMEOUT_S = 60           # SPLADE rebuild on swap can take a moment
CONTEXT_TIMEOUT_S = 30        # /context retrieval-only call


# ── Needles (copied from benchmarks/bench_needle.py so this script is
#     standalone and the runner doesn't depend on the bench dir on PYTHONPATH).

NEEDLES = [
    {"name": "helix_port",
     "query": "What port does the Helix proxy server listen on?",
     "expected": "11437", "accept": ["11437"],
     "gold_source": ["helix-context/helix.toml"]},
    {"name": "scorerift_threshold",
     "query": "What is the divergence threshold that triggers alerts in ScoreRift?",
     "expected": "0.15", "accept": ["0.15", ".15"],
     "gold_source": ["two-brain-audit/README.md"]},
    {"name": "biged_skills_count",
     "query": "How many skills does the BigEd fleet have?",
     "expected": "125", "accept": ["125", "129"],
     "gold_source": ["Education/CLAUDE.md"]},
    {"name": "bookkeeper_monetary",
     "query": "What type should be used for monetary values in BookKeeper instead of float?",
     "expected": "Decimal", "accept": ["decimal", "Decimal"],
     "gold_source": ["BookKeeper/CLAUDE.md"]},
    {"name": "helix_pipeline_steps",
     "query": "How many steps are in the Helix expression pipeline?",
     "expected": "6", "accept": ["6", "six"],
     "gold_source": ["helix-context/CLAUDE.md", "helix-context/README.md"]},
    {"name": "biged_rust_binary_size",
     "query": "What is the binary size of the Rust BigEd build in MB?",
     "expected": "11", "accept": ["11", "11mb", "11 mb"],
     "gold_source": ["Education/biged-rs/README.md"]},
    {"name": "genome_compression_target",
     "query": "What is the target compression ratio for Helix Context?",
     "expected": "5x", "accept": ["5x", "5:1", "5 to 1"],
     "gold_source": ["helix-context/README.md", "helix-context/docs"]},
    {"name": "scorerift_preset_dimensions",
     "query": "How many dimensions does the Python preset in ScoreRift check?",
     "expected": "8", "accept": ["8", "eight"],
     "gold_source": ["two-brain-audit/README.md"]},
    {"name": "helix_ribosome_budget",
     "query": "How many tokens are allocated for the ribosome decoder prompt?",
     "expected": "3000", "accept": ["3000", "3k", "3,000"],
     "gold_source": ["helix-context/helix.toml", "helix-context/README.md"]},
    {"name": "biged_default_model",
     "query": "What is the default local model used by BigEd for conductor tasks?",
     "expected": "qwen3", "accept": ["qwen3", "qwen3:4b", "qwen"],
     "gold_source": ["Education/CLAUDE.md"]},
]


# Markers that indicate the model abstained rather than guessed.
ABSTAIN_MARKERS = [
    r"\bi (?:don't|cannot|can't|am unable to|do not) (?:find|know|determine)\b",
    r"\bno (?:relevant )?information\b",
    r"\bnot (?:available|found|present)\b",
    r"\binsufficient (?:context|information|data)\b",
    r"\bunable to (?:find|determine|answer)\b",
    r"\bcouldn't find\b",
]
ABSTAIN_RE = re.compile("|".join(ABSTAIN_MARKERS), re.IGNORECASE)


# ── Setup logging ────────────────────────────────────────────────────────

def make_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_ROOT / f"claude_matrix_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def setup_logger(run_dir: Path) -> logging.Logger:
    log_path = run_dir / "run.log"
    logger = logging.getLogger("bench.claude_matrix")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# ── Swap helper ──────────────────────────────────────────────────────────

def swap_db(target_path: str, log: logging.Logger) -> dict:
    """POST /admin/swap-db. Returns the response body or {"error": ...}."""
    log.info("swap-db -> %s", target_path)
    try:
        resp = httpx.post(
            f"{HELIX_URL}/admin/swap-db",
            json={"path": target_path},
            timeout=SWAP_TIMEOUT_S,
        )
    except Exception as exc:
        return {"error": f"swap-db request failed: {exc}"}
    if resp.status_code != 200:
        return {"error": f"swap-db HTTP {resp.status_code}: {resp.text[:200]}"}
    body = resp.json()
    log.info("  swapped: %d genes in %s ms", body.get("genes", -1), body.get("elapsed_ms"))
    return body


# ── Retrieval-only probe via /context ────────────────────────────────────

def retrieval_probe(query: str, gold_sources: list[str]) -> dict:
    """Direct /context call to get retrieval signal independent of the model."""
    t0 = time.perf_counter()
    try:
        resp = httpx.post(
            f"{HELIX_URL}/context",
            json={"query": query, "decoder_mode": "none"},
            timeout=CONTEXT_TIMEOUT_S,
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc),
                "latency_s": time.perf_counter() - t0}
    latency = time.perf_counter() - t0
    if resp.status_code != 200:
        return {"status": "error", "http": resp.status_code,
                "latency_s": latency}
    data = resp.json()
    entry = data[0] if isinstance(data, list) and data else {}
    content = entry.get("content", "") or ""

    delivered_sources = []
    for m in re.finditer(r'<GENE src="([^"]+)"', content):
        delivered_sources.append(m.group(1))

    gold_hit = False
    for src in delivered_sources:
        norm = src.replace("\\", "/").lower()
        if any(gs.replace("\\", "/").lower() in norm for gs in gold_sources):
            gold_hit = True
            break

    return {
        "status": "ok",
        "latency_s": latency,
        "delivered_count": len(delivered_sources),
        "delivered_sources": delivered_sources[:20],
        "gold_delivered": gold_hit,
        "ellipticity": entry.get("context_health", {}).get("ellipticity"),
    }


# ── Claude -p invocation ─────────────────────────────────────────────────

def run_claude(query: str, model: str, max_usd: float,
               cwd: str, log: logging.Logger) -> dict:
    """Run a single ``claude -p`` subprocess and capture structured output."""
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
        "--model", model,
        "--max-budget-usd", str(max_usd),
        query,
    ]
    log.info("  claude -p (model=%s, budget=$%.2f): %s", model, max_usd, query[:60])
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_S,
            cwd=cwd,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "elapsed_s": CLAUDE_TIMEOUT_S}
    except Exception as exc:
        return {"status": "error", "error": str(exc),
                "elapsed_s": time.perf_counter() - t0}
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        return {
            "status": "exit_nonzero",
            "returncode": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-400:],
            "stdout_tail": (proc.stdout or "")[-400:],
            "elapsed_s": elapsed,
        }
    raw = proc.stdout.strip()
    try:
        result = json.loads(raw)
    except Exception:
        return {
            "status": "parse_error",
            "stdout_tail": raw[-400:],
            "elapsed_s": elapsed,
        }
    return {"status": "ok", "result": result, "elapsed_s": elapsed,
            "stderr_tail": (proc.stderr or "")[-200:] if proc.stderr else ""}


# ── Scoring ──────────────────────────────────────────────────────────────

def score_answer(answer_text: str, accept_substrings: list[str]) -> dict:
    """Return {-1, 0, +1} score plus diagnostics.

    +1: word-boundary match of any accept substring (correct)
     0: model abstained (says "I don't know" or similar)
    -1: confident answer but no accept-substring match (likely wrong)
    """
    text = (answer_text or "").strip()
    if not text:
        return {"score": 0, "reason": "empty"}

    for a in accept_substrings:
        if re.search(rf"\b{re.escape(a)}\b", text, re.IGNORECASE):
            return {"score": 1, "reason": f"accept-match:{a}",
                    "matched_token": a}

    if ABSTAIN_RE.search(text):
        return {"score": 0, "reason": "abstain"}

    return {"score": -1, "reason": "no-match-and-confident"}


# ── Main loop ────────────────────────────────────────────────────────────

def parse_filter(spec: str | None, all_keys: list[str]) -> set[str]:
    if not spec:
        return set()
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    bad = [p for p in parts if p not in all_keys]
    if bad:
        raise SystemExit(f"unknown profile(s): {bad}; available: {all_keys}")
    return set(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="Comma-separated profiles to run")
    parser.add_argument("--skip", help="Comma-separated profiles to skip")
    parser.add_argument("--model", default="sonnet",
                        help="claude -p --model arg (sonnet/opus/haiku/full id)")
    parser.add_argument("--max-usd", type=float, default=0.30,
                        help="Per-question budget cap (sonnet smoke run averaged $0.11)")
    parser.add_argument("--cwd", default=r"F:\Projects\helix-context",
                        help="cwd for claude -p subprocess")
    args = parser.parse_args()

    if not MANIFEST.exists():
        print(f"!! manifest not found at {MANIFEST}; run freeze_matrix_manifest.py first",
              file=sys.stderr)
        return 2

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    all_keys = list(manifest["targets"].keys())
    only = parse_filter(args.only, all_keys)
    skip = parse_filter(args.skip, all_keys)
    keys = [k for k in all_keys
            if (not only or k in only) and k not in skip]

    run_dir = make_run_dir()
    log = setup_logger(run_dir)
    log.info("RUN START run_dir=%s", run_dir)
    log.info("model=%s max_usd_per_question=%.2f", args.model, args.max_usd)
    log.info("profiles in run: %s", keys)

    # Sanity: server reachable?
    try:
        r = httpx.get(f"{HELIX_URL}/stats", timeout=10)
        log.info("server /stats ok: %d genes", r.json().get("total_genes", -1))
    except Exception as exc:
        log.error("server not reachable at %s: %s", HELIX_URL, exc)
        return 3

    overall: dict = {
        "run_dir": str(run_dir),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "max_usd_per_question": args.max_usd,
        "profiles": {},
    }

    for key in keys:
        target = manifest["targets"][key]
        log.info("=" * 60)
        log.info("PROFILE: %s (mode=%s)", key, target.get("mode"))

        if target.get("status") == "missing":
            log.warning("  target missing on disk, skipping")
            continue

        swap_result = swap_db(target["path"], log)
        if "error" in swap_result:
            log.error("  swap failed: %s", swap_result["error"])
            overall["profiles"][key] = {"swap_error": swap_result["error"]}
            continue

        profile_path = run_dir / f"{key}.jsonl"
        per_needle: list[dict] = []
        with open(profile_path, "w", encoding="utf-8") as f:
            for i, n in enumerate(NEEDLES):
                log.info("[%s %d/%d] %s", key, i + 1, len(NEEDLES), n["name"])

                # 1. Retrieval-only probe
                retr = retrieval_probe(n["query"], n["gold_source"])

                # 2. Full answer via claude -p
                claude_r = run_claude(
                    n["query"], args.model, args.max_usd, args.cwd, log,
                )

                # 3. Extract answer + tokens + score
                answer_text = ""
                tokens = {}
                cost_usd = None
                if claude_r["status"] == "ok":
                    res = claude_r["result"]
                    answer_text = (
                        res.get("result")
                        or res.get("answer")
                        or res.get("content")
                        or ""
                    )
                    # Token fields vary by claude version — capture broadly
                    for k in ("total_tokens", "input_tokens", "output_tokens",
                              "cache_creation_input_tokens",
                              "cache_read_input_tokens"):
                        if k in res:
                            tokens[k] = res[k]
                    # nested usage field if present
                    if "usage" in res and isinstance(res["usage"], dict):
                        for k, v in res["usage"].items():
                            tokens.setdefault(k, v)
                    cost_usd = (
                        res.get("total_cost_usd")
                        or res.get("total_cost")
                        or res.get("cost_usd")
                    )

                score = score_answer(answer_text, n["accept"])

                record = {
                    "profile": key,
                    "needle": n["name"],
                    "query": n["query"],
                    "expected": n["expected"],
                    "accept": n["accept"],
                    "gold_source": n["gold_source"],
                    "retrieval": retr,
                    "claude_status": claude_r["status"],
                    "claude_elapsed_s": claude_r.get("elapsed_s"),
                    "answer_text": answer_text[:2000],
                    "tokens": tokens,
                    "cost_usd": cost_usd,
                    "score": score["score"],
                    "score_reason": score["reason"],
                    "score_matched_token": score.get("matched_token"),
                }
                if claude_r["status"] != "ok":
                    record["claude_debug"] = {
                        k: v for k, v in claude_r.items() if k != "result"
                    }

                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                per_needle.append(record)
                in_tok = tokens.get("input_tokens", 0)
                out_tok = tokens.get("output_tokens", 0)
                cache_r = tokens.get("cache_read_input_tokens", 0)
                cache_w = tokens.get("cache_creation_input_tokens", 0)
                cost_s = f"${cost_usd:.4f}" if isinstance(cost_usd, (int, float)) else "?"
                log.info(
                    "  retr=%s score=%+d cost=%s in=%s out=%s cache_r=%s cache_w=%s",
                    retr.get("gold_delivered"), score["score"],
                    cost_s, in_tok, out_tok, cache_r, cache_w,
                )

        # Profile summary
        total_score = sum(r["score"] for r in per_needle)
        n_correct = sum(1 for r in per_needle if r["score"] == 1)
        n_abstain = sum(1 for r in per_needle if r["score"] == 0)
        n_wrong = sum(1 for r in per_needle if r["score"] == -1)
        n_retr_hit = sum(1 for r in per_needle
                         if r["retrieval"].get("gold_delivered"))
        total_cost = sum(
            (r["cost_usd"] or 0.0) for r in per_needle
        )
        prof_summary = {
            "needles_run": len(per_needle),
            "answers": {
                "correct": n_correct,
                "abstain": n_abstain,
                "wrong": n_wrong,
                "score_sum": total_score,
                "score_normalized": (total_score / len(NEEDLES))
                if NEEDLES else 0,
            },
            "retrieval": {
                "gold_delivered_count": n_retr_hit,
                "gold_delivered_rate": n_retr_hit / len(NEEDLES),
            },
            "total_cost_usd": round(total_cost, 4),
        }
        overall["profiles"][key] = prof_summary
        log.info(
            "  PROFILE %s: correct=%d abstain=%d wrong=%d "
            "retr_hit=%d/%d cost=$%.4f",
            key, n_correct, n_abstain, n_wrong, n_retr_hit, len(NEEDLES),
            total_cost,
        )

    overall["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(overall, indent=2), encoding="utf-8")
    log.info("=" * 60)
    log.info("RUN DONE. Summary at %s", summary_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

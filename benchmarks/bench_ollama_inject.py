r"""Lite context-injected bench for ollama-served local models.

Mirrors bench_context_injected.py's structure but calls ollama /api/chat
directly instead of claude -p. Used to test whether helix-served context
is consumable by a smaller local model.

Modes:
  --mode none  : M0 anchor — no system context, raw user question
  --mode helix : M2 — helix /context injected as system prompt

Reuses NEEDLES, score_answer (extended-abstain), retrieval_probe,
context_helix from the sibling scripts. No tools, no MCP, no Read/Grep
access — pure model-with-context-and-question runs.

Cost is $0 (ollama is local). Time depends on model size and corpus.

Usage:
  python benchmarks/bench_ollama_inject.py --mode none --only medium --no-server
  python benchmarks/bench_ollama_inject.py --mode helix --only medium,medium-sharded \
      --model gemma4:e4b --num-predict 600

Output:
  benchmarks/results/ollama_<model>_<mode>_<UTC-timestamp>/
    summary.json
    <profile>.jsonl
    run.log
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_orchestrator import BenchServer, Fixture  # noqa: E402
from bench_claude_matrix import (  # noqa: E402
    NEEDLES,
    retrieval_probe,
    HELIX_URL,
    CONTEXT_TIMEOUT_S,
    UNTRUSTED_PROFILES,
    MANIFEST,
)
from bench_context_injected import (  # noqa: E402
    context_none,
    context_helix,
    context_oracle,
    score_answer,             # extended-abstain wrapper
    SYSTEM_PROMPT_INJECTED,
    MAX_CTX_CHARS,
)


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
RESULTS_ROOT = Path(r"F:\Projects\helix-context\benchmarks\results")
OLLAMA_TIMEOUT_S = 300  # 5 min per generation — local models are slower


CONTEXT_FNS = {
    "none": context_none,
    "helix": context_helix,
    "oracle": context_oracle,
}


def run_ollama(
    query: str,
    ctx_text: str,
    model: str,
    num_predict: int,
    log: logging.Logger,
) -> dict:
    """POST to ollama /api/chat with optional system context. Returns dict
    matching the existing bench-record shape (status / result / elapsed_s)."""
    messages = []
    if ctx_text:
        sys_prompt = SYSTEM_PROMPT_INJECTED.format(ctx=ctx_text)
        messages.append({"role": "system", "content": sys_prompt})
    messages.append({"role": "user", "content": query})

    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.0,    # deterministic for benching
            "num_predict": num_predict,
        },
    }
    log.info(
        "  ollama POST model=%s ctx_chars=%d q=%s",
        model, len(ctx_text), query[:60],
    )
    t0 = time.perf_counter()
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json=body,
            timeout=OLLAMA_TIMEOUT_S,
        )
    except httpx.TimeoutException:
        return {"status": "timeout", "elapsed_s": OLLAMA_TIMEOUT_S}
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "elapsed_s": time.perf_counter() - t0,
        }
    elapsed = time.perf_counter() - t0
    if resp.status_code != 200:
        return {
            "status": "http_error",
            "http": resp.status_code,
            "body": resp.text[:300],
            "elapsed_s": elapsed,
        }
    try:
        data = resp.json()
    except Exception:
        return {"status": "json_error", "elapsed_s": elapsed}
    return {
        "status": "ok",
        "result": data,
        "elapsed_s": elapsed,
    }


def run_one_needle(
    needle: dict,
    profile_key: str,
    mode: str,
    model: str,
    num_predict: int,
    helix_url: str,
    log: logging.Logger,
) -> dict:
    log.info("[%s %s] %s", profile_key, mode, needle["name"])

    retr = retrieval_probe(
        needle["query"], needle["gold_source"], helix_url=helix_url,
    )

    ctx_fn = CONTEXT_FNS[mode]
    ctx_text, ctx_meta = ctx_fn(needle, helix_url)

    ollama_r = run_ollama(needle["query"], ctx_text, model, num_predict, log)

    answer_text = ""
    tokens: dict = {}
    if ollama_r["status"] == "ok":
        res = ollama_r["result"]
        msg = res.get("message", {}) or {}
        answer_text = msg.get("content", "") or ""
        tokens = {
            "input_tokens": res.get("prompt_eval_count", 0),
            "output_tokens": res.get("eval_count", 0),
            "prompt_eval_duration_ns": res.get("prompt_eval_duration", 0),
            "eval_duration_ns": res.get("eval_duration", 0),
        }

    score = score_answer(answer_text, needle["accept"])

    record = {
        "profile": profile_key,
        "mode": mode,
        "model": model,
        "needle": needle["name"],
        "query": needle["query"],
        "expected": needle["expected"],
        "accept": needle["accept"],
        "gold_source": needle["gold_source"],
        "retrieval": retr,
        "context": ctx_meta,
        "ollama_status": ollama_r["status"],
        "ollama_elapsed_s": ollama_r.get("elapsed_s"),
        "answer_text": answer_text[:2000],
        "tokens": tokens,
        "cost_usd": 0.0,  # local model, no API cost
        "score": score["score"],
        "score_reason": score["reason"],
        "score_matched_token": score.get("matched_token"),
    }
    if ollama_r["status"] != "ok":
        record["ollama_debug"] = {
            k: v for k, v in ollama_r.items() if k != "result"
        }

    in_tok = tokens.get("input_tokens", 0)
    out_tok = tokens.get("output_tokens", 0)
    elapsed = ollama_r.get("elapsed_s", 0)
    log.info(
        "  retr=%s ctx=%d score=%+d in=%d out=%d elapsed=%.1fs",
        ("#%d" % retr["gold_rank"]) if retr.get("gold_delivered") else (
            "miss" if retr.get("status") == "ok" else "skip"
        ),
        ctx_meta.get("chars", 0),
        score["score"],
        in_tok, out_tok, elapsed,
    )
    return record


def run_profile(
    fixture: Fixture | None,
    profile_key: str,
    mode: str,
    model: str,
    num_predict: int,
    helix_url: str,
    run_dir: Path,
    log: logging.Logger,
    max_needles: int | None = None,
) -> dict:
    log.info("  running profile=%s ctx_mode=%s model=%s%s",
             profile_key, mode, model,
             f" (helix-served from {fixture.db})" if fixture else " (no helix)")

    profile_jsonl = run_dir / f"{profile_key}.jsonl"
    records = []
    needles = NEEDLES if max_needles is None else NEEDLES[:max_needles]

    with profile_jsonl.open("w", encoding="utf-8") as fh:
        for idx, needle in enumerate(needles, 1):
            log.info("[%s %s %d/%d] %s",
                     profile_key, mode, idx, len(needles), needle["name"])
            rec = run_one_needle(needle, profile_key, mode, model, num_predict, helix_url, log)
            records.append(rec)
            fh.write(json.dumps(rec) + "\n")
            fh.flush()

    correct = sum(1 for r in records if r["score"] == 1)
    abstain = sum(1 for r in records if r["score"] == 0)
    wrong = sum(1 for r in records if r["score"] == -1)
    gold_delivered = sum(1 for r in records
                          if r["retrieval"].get("gold_delivered"))
    mrrs = [
        1.0 / (r["retrieval"]["gold_rank"] + 1)
        for r in records
        if r["retrieval"].get("gold_delivered")
        and r["retrieval"].get("gold_rank") is not None
    ]
    mrr = round(sum(mrrs) / len(records), 4) if records else 0.0
    ctx_chars_avg = (
        sum(r["context"].get("chars", 0) for r in records) / len(records)
        if records else 0
    )
    total_elapsed = sum(r.get("ollama_elapsed_s") or 0.0 for r in records)

    log.info(
        "  PROFILE %s: correct=%d abstain=%d wrong=%d retr_hit=%d/%d mrr=%.3f "
        "ctx_avg=%.0fc elapsed=%.0fs",
        profile_key, correct, abstain, wrong,
        gold_delivered, len(records), mrr, ctx_chars_avg, total_elapsed,
    )

    return {
        "needles_run": len(records),
        "answers": {
            "correct": correct, "abstain": abstain, "wrong": wrong,
            "score_sum": correct - wrong,
            "score_normalized": round((correct - wrong) / max(len(records), 1), 4),
        },
        "retrieval": {
            "gold_delivered_count": gold_delivered,
            "gold_delivered_rate": round(gold_delivered / max(len(records), 1), 4),
            "mrr": mrr,
        },
        "context": {"mode": mode, "avg_chars": round(ctx_chars_avg, 0)},
        "total_elapsed_s": round(total_elapsed, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=list(CONTEXT_FNS.keys()), required=True)
    parser.add_argument("--only", help="Comma-separated profiles to run")
    parser.add_argument("--skip", help="Comma-separated profiles to skip")
    parser.add_argument("--include-untrusted", action="store_true")
    parser.add_argument("--model", default="gemma4:e4b")
    parser.add_argument("--num-predict", type=int, default=512,
                        help="Max tokens to generate per response")
    parser.add_argument("--max-needles", type=int, default=None)
    parser.add_argument("--no-server", action="store_true")
    parser.add_argument("--external-server", action="store_true")
    parser.add_argument("--helix-url", default=HELIX_URL)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=11437, type=int)
    parser.add_argument("--health-timeout", default=180.0, type=float)
    args = parser.parse_args()

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    skip = [s.strip() for s in args.skip.split(",")] if args.skip else None

    if not MANIFEST.exists():
        print(f"ERROR: manifest not found at {MANIFEST}", file=sys.stderr)
        return 2

    # Set up run dir + logger
    model_safe = re.sub(r'[^\w-]+', '_', args.model)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_ROOT / f"ollama_{model_safe}_{args.mode}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "run.log"

    logger = logging.getLogger("bench.ollama")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8"); fh.setFormatter(fmt); logger.addHandler(fh)
    sh = logging.StreamHandler(); sh.setFormatter(fmt); logger.addHandler(sh)

    logger.info("RUN START run_dir=%s", run_dir)
    logger.info("mode=%s model=%s num_predict=%d", args.mode, args.model, args.num_predict)
    logger.info("ollama_url=%s helix_url=%s", OLLAMA_URL, args.helix_url)

    # Verify ollama reachable + model present
    try:
        tags = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5).json()
        models = [m.get("name") for m in tags.get("models", [])]
        if args.model not in models:
            logger.error("model %s not in ollama: %s", args.model, models)
            return 2
        logger.info("ollama reachable; model %s present", args.model)
    except Exception as exc:
        logger.error("ollama probe failed at %s: %s", OLLAMA_URL, exc)
        return 2

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    targets = manifest["targets"]
    all_keys = list(targets.keys())
    if only:
        profile_keys = [k for k in all_keys if k in only]
    else:
        profile_keys = list(all_keys)
    if skip:
        profile_keys = [k for k in profile_keys if k not in skip]
    if not args.include_untrusted:
        profile_keys = [k for k in profile_keys if k not in UNTRUSTED_PROFILES or (only and k in only)]
    if not profile_keys:
        logger.error("No profiles selected")
        return 2
    logger.info("profiles in run: %s", profile_keys)

    needs_server = (args.mode in {"helix", "both"}) and not (args.no_server or args.external_server)

    summary = {
        "run_dir": str(run_dir),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "model": args.model,
        "num_predict": args.num_predict,
        "ollama_url": OLLAMA_URL,
        "helix_url": args.helix_url,
        "profiles": {},
    }

    def _run_one_profile(key, fixture):
        try:
            summary["profiles"][key] = run_profile(
                fixture=fixture, profile_key=key, mode=args.mode,
                model=args.model, num_predict=args.num_predict,
                helix_url=args.helix_url, run_dir=run_dir, log=logger,
                max_needles=args.max_needles,
            )
        except Exception:
            logger.exception("profile %s failed", key)

    try:
        if needs_server:
            uvicorn_log = run_dir / "uvicorn.log"
            logger.info("Starting BenchServer — log=%s", uvicorn_log)
            with BenchServer(
                host=args.host, port=args.port,
                health_timeout_s=args.health_timeout, log_to=uvicorn_log,
            ) as srv:
                for key in profile_keys:
                    target = targets[key]
                    logger.info("=" * 60)
                    logger.info("PROFILE: %s (mode=%s)", key, target.get("mode"))
                    if target.get("status") == "missing":
                        logger.warning("  target missing on disk, skipping"); continue
                    fixture = Fixture(
                        name=key,
                        db=str(target["path"]).replace("\\", "/"),
                        sharded=(target.get("mode") == "sharded"),
                        read_only=True,
                    )
                    try:
                        swap = srv.switch(fixture)
                    except Exception as exc:
                        logger.error("  switch failed: %s", exc)
                        summary["profiles"][key] = {"swap_error": str(exc)}
                        continue
                    logger.info("  %s in %.2fs (genes=%d, pid=%s)",
                                swap.mechanism, swap.elapsed_s, swap.genes, swap.server_pid)
                    _run_one_profile(key, fixture)
        else:
            for key in profile_keys:
                target = targets[key]
                logger.info("=" * 60)
                logger.info("PROFILE: %s (mode=%s, no server)", key, target.get("mode"))
                if target.get("status") == "missing":
                    logger.warning("  target missing on disk, skipping"); continue
                _run_one_profile(key, None)
    finally:
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary_path = run_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        logger.info("=" * 60)
        logger.info("RUN DONE. summary: %s", summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

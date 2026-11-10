"""probe_ollama_concurrency.py — gemma call latency at 1 vs 2 concurrent callers.

The retrieval-side scaling receipt (latency_scaling_1to2.json) measured the
CPU/encoder path at 1.48x median under a second worker. Ribosome-backed
turns add an LLM call per query, and that leg contends on the GPU instead —
this probe measures that leg in isolation: N workers firing the 18 needle
queries as query-expansion calls (same system prompt as
``_expand_query_intent``, same ~100-token output shape) straight at Ollama.

Per model, per concurrency level: median/p95 call latency, aggregate
throughput, and the scaling ratio vs c=1. One untimed warmup pins the model
(keep_alive) before any timed call; workers are barrier-started.

Usage::

    python benchmarks/dogfood/probe_ollama_concurrency.py
    python benchmarks/dogfood/probe_ollama_concurrency.py --models gemma4:e2b
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import threading
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SYSTEM = ("You are a query intent expander. Given a user's question, output a "
          "single line of SPACE-SEPARATED KEYWORDS that capture the question's "
          "intent plus likely synonyms and domain terms. Include the original "
          "key words. No prose, no punctuation, just lowercase keywords "
          "separated by spaces. Maximum 15 words.")


def _pct(vals: list[float], p: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, math.ceil(p * (len(s) - 1)))]


def _call(client: httpx.Client, base: str, model: str, query: str) -> float:
    t0 = time.time()
    r = client.post(f"{base}/api/generate", json={
        "model": model, "prompt": f"Query: {query}\n\nKeywords:",
        "system": SYSTEM, "stream": False, "keep_alive": "30m",
        "options": {"temperature": 0.0},
    }, timeout=180)
    r.raise_for_status()
    return (time.time() - t0) * 1000


def _run_level(base: str, model: str, queries: list[str], workers: int,
               rounds: int) -> dict:
    results: list[list[float]] = [[] for _ in range(workers)]
    errors: list[str] = []
    start_evt = threading.Event()

    def worker(idx: int) -> None:
        client = httpx.Client()
        start_evt.wait()
        for _ in range(rounds):
            for q in queries:
                try:
                    results[idx].append(_call(client, base, model, q))
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))
        client.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    t0 = time.time()
    start_evt.set()
    for t in threads:
        t.join()
    wall = time.time() - t0

    lat = [x for r in results for x in r]
    if not lat:
        raise RuntimeError(f"{model} c={workers}: all calls failed ({errors[:2]})")
    return {
        "concurrency": workers,
        "calls": len(lat),
        "errors": len(errors),
        "median_ms": round(statistics.median(lat), 1),
        "mean_ms": round(statistics.mean(lat), 1),
        "p95_ms": round(_pct(lat, 0.95), 1),
        "wall_seconds": round(wall, 1),
        "throughput_cps": round(len(lat) / wall, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://localhost:11434")
    ap.add_argument("--models", default="gemma4:e2b,gemma4:e4b")
    ap.add_argument("--concurrency", default="1,2")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--resolved", default="benchmarks/dogfood/needles_resolved.json")
    ap.add_argument("--out", default="benchmarks/dogfood/ollama_concurrency.json")
    args = ap.parse_args()

    queries = [n["query"] for n in json.loads(
        (REPO_ROOT / args.resolved).read_text(encoding="utf-8"))["needles"]]
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    levels = [int(c) for c in args.concurrency.split(",") if c.strip()]

    out_models = []
    client = httpx.Client()
    for model in models:
        # Untimed warmup: pin the model before any timed call.
        _call(client, args.base, model, "warmup")
        rows = []
        for c in levels:
            row = _run_level(args.base, model, queries, c, args.rounds)
            rows.append(row)
            print(f"  {model:12s} c={c}: median {row['median_ms']}ms  "
                  f"p95 {row['p95_ms']}ms  {row['throughput_cps']} calls/s  "
                  f"errors {row['errors']}", flush=True)
        base_row = next((r for r in rows if r["concurrency"] == 1), None)
        scaling = [{
            "concurrency": r["concurrency"],
            "median_x": round(r["median_ms"] / base_row["median_ms"], 2),
            "throughput_x": round(r["throughput_cps"] / base_row["throughput_cps"], 2),
        } for r in rows] if base_row else []
        out_models.append({"model": model, "levels": rows, "scaling_vs_c1": scaling})

    payload = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": ("LLM-leg contention in isolation: expansion-shaped calls "
                 "straight at Ollama; no retrieval pipeline; no OTel"),
        "queries": len(queries),
        "rounds": args.rounds,
        "models": out_models,
    }
    out = REPO_ROOT / args.out
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

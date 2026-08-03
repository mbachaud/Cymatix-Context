"""run_cold_start.py — anatomy of request #1 on a cold server.

Every latency number on this bed so far was taken on a warm process — encoders
resident, caches primed. But the number a user actually feels is request #1
after boot: uvicorn import, first /context, BGE-M3 + SPLADE load, first-query
caches. This measures exactly that — boot a fresh server, fire three
sequential /context requests at the same needle, read the perf-slice-1
pipeline ring, and report what request #1 cost, what fraction of it was model
load, and what a warmup endpoint would have saved.

Worker model: one ``BenchServer`` uvicorn per iteration (bench_orchestrator
lifecycle: spawn -> healthy -> /admin/swap-db to the dogfood genome, read-only).
The server is stopped and its whole process tree killed between iterations
(issue #127), so every iteration is a true cold boot — fresh process, fresh
encoders. Requests are sequential over plain urllib; nothing is concurrent.

Honesty guardrails, same spirit as the rest of the bed:

* **No untimed warmup.** Request #1 being cold IS the measurement — the exact
  inverse of `run_latency_scaling.py`'s convention. Requests #2 and #3 are the
  warm reference.
* **Attribution sanity gate.** ``model_load_ms`` rides the ring's express
  entries and is process-cumulative; a fresh process per iteration means the
  map is exactly that iteration's load bill, and per-request deltas fall out
  of consecutive express entries. The gate PASSes when
  ``first_express_ms - model_load_delta(request #1)`` lands within 50% of the
  warm express (request #3). FAIL fires only when a cold first request
  recorded ZERO model-load ms — timers dead or misplaced — and the cold-start
  attribution must not be quoted. WARN means loads were recorded but a cold
  residual remains (``cold_residual_ms``): first-forward kernel warmup, OS
  page-cache misses and the first FTS scan are real cold costs that are not
  model-weight load and are deliberately untimed.
* **Gold rank recorded per request** (from ``agent.citations`` in the
  /context response, list envelope unwrapped). A cold server that stopped
  retrieving gold would otherwise read as a latency story with no asterisk.
  Note: /context continue-mode ignores per-request ``max_genes``; ``--k`` is
  only the recall cutoff for this guard.
* **Ring is a tail sample.** ``/debug/pipeline/recent`` holds ~128 entries ~
  the last ~12 requests; the embedded aggregate is labeled
  ``ring_tail_sample``. With only 3 requests per fresh process the ring covers
  the whole iteration, but the label keeps the receipt honest if that changes.

Usage::

    python benchmarks/dogfood/run_cold_start.py                 # 5 iterations
    python benchmarks/dogfood/run_cold_start.py --iterations 3
    python benchmarks/dogfood/run_cold_start.py --port 11492 --out /tmp/cs.json
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "benchmarks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_orchestrator import BenchServer, Fixture  # noqa: E402
from _stage_receipts import aggregate, fetch_recent  # noqa: E402

log = logging.getLogger("bench.cold_start")

# The spawned server loads BGE-M3+SPLADE lazily on first query, not at boot,
# but torch imports alone can eat tens of seconds on a cold filesystem cache.
HEALTH_TIMEOUT_S = 120.0


# ------------------------------------------------------------------ http ----

def _post_context(base_url: str, query: str, session_id: str,
                  timeout_s: float) -> dict:
    """POST /context and unwrap the Continue-style single-element list."""
    body = json.dumps({
        "query": query,
        "ignore_delivered": True,   # bypass session working-set elision
        "read_only": True,          # never mutate the bed genome
        "session_id": session_id,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/context", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    # routes_context.context_endpoint returns [response]; unwrap it.
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    return payload


def _gold_rank(payload: dict, gold: set) -> int | None:
    """1-based rank of the first gold gene in agent.citations, else None."""
    cites = (payload.get("agent") or {}).get("citations") or []
    ordered = [c.get("gene_id") for c in cites]
    return next((i for i, g in enumerate(ordered, 1) if g in gold), None)


# ------------------------------------------------------------------ ring ----

def _express_entries(entries: list[dict]) -> list[dict]:
    """Express-stage ring entries in timestamp order (one per request)."""
    ex = [e for e in entries if e.get("stage") == "express"
          and e.get("ms") is not None]
    ex.sort(key=lambda e: e.get("ts", 0))
    return ex


def _load_total_ms(entry: dict) -> float:
    return sum(float(v) for v in (entry.get("model_load_ms") or {}).values())


def _attribution_gate(express: list[dict]) -> dict:
    """Attribution check for request #1's express time.

    - FAIL only when the timers are demonstrably dead: a cold first request
      whose ring recorded ZERO model-load ms (instrumentation missing or
      misplaced — the alarm this gate exists for).
    - WARN when loads were recorded but a large cold residual remains
      (``cold_residual_ms``): known-untimed cold costs — first-forward
      kernel warmup, OS page-cache misses on the bed file, first FTS scan —
      are real and are NOT model-weight load; the receipt reports them
      honestly instead of crying "misplaced timer".
    - PASS when (first express − request-#1 model load) lands within 50% of
      warm express (request #3).
    - INCONCLUSIVE when the ring yielded too few express entries to judge.
    """
    if len(express) < 2:
        return {"verdict": "INCONCLUSIVE",
                "reason": f"only {len(express)} express entries in ring"}
    first, warm = express[0], express[-1]
    load_req1 = _load_total_ms(first)  # cumulative through request #1
    first_ms = float(first["ms"])
    warm_ms = float(warm["ms"])
    adjusted = first_ms - load_req1
    gate: dict = {
        "first_express_ms": round(first_ms, 1),
        "model_load_req1_ms": round(load_req1, 1),
        "adjusted_first_express_ms": round(adjusted, 1),
        "warm_express_ms": round(warm_ms, 1),
    }
    if warm_ms <= 0:
        gate.update(verdict="INCONCLUSIVE", reason="warm express ms <= 0")
        return gate
    gate["adjusted_over_warm"] = round(adjusted / warm_ms, 3)
    gate["cold_residual_ms"] = round(max(0.0, adjusted - warm_ms), 1)
    cold_first = first_ms > 3.0 * warm_ms
    if cold_first and load_req1 <= 0.0:
        gate["verdict"] = "FAIL"
        gate["reason"] = ("cold first request but zero model-load ms "
                          "recorded — timers dead or misplaced")
    elif abs(adjusted - warm_ms) <= 0.5 * warm_ms:
        gate["verdict"] = "PASS"
    else:
        gate["verdict"] = "WARN"
        gate["reason"] = ("model loads recorded but a cold residual "
                          "remains (kernel warmup / page cache / first "
                          "FTS scan are untimed by design)")
    return gate


# ------------------------------------------------------------- iteration ----

def _run_iteration(idx: int, args: argparse.Namespace, needle: dict,
                   gold: set, run_dir: Path) -> dict:
    server = BenchServer(port=args.port, repo_root=REPO_ROOT,
                         health_timeout_s=HEALTH_TIMEOUT_S,
                         log_to=run_dir / f"server_iter{idx}.log")
    try:
        swap = server.start(Fixture(name="dogfood-cold",
                                    db=str(REPO_ROOT / args.genome),
                                    read_only=True))
        session_id = f"cold-start-i{idx}-{int(time.time() * 1000)}"
        wall_ms, ranks = [], []
        for _ in range(3):
            q0 = time.time()
            payload = _post_context(server.url, needle["query"], session_id,
                                    args.request_timeout)
            wall_ms.append(round((time.time() - q0) * 1000, 1))
            ranks.append(_gold_rank(payload, gold))

        ring_error = None
        try:
            entries = fetch_recent(server.url, limit=128)
            ring = aggregate(entries)
        except Exception as exc:  # noqa: BLE001 — a dead ring is a datapoint
            log.warning("iteration %d: ring fetch failed: %r", idx, exc)
            entries, ring, ring_error = [], None, repr(exc)

        express = _express_entries(entries)
        # Per-request model-load deltas from the cumulative maps.
        express_by_request, prev_load = [], 0.0
        for e in express:
            cum = _load_total_ms(e)
            express_by_request.append({
                "request_id": e.get("request_id"),
                "express_ms": round(float(e["ms"]), 1),
                "model_load_delta_ms": round(cum - prev_load, 1),
            })
            prev_load = cum

        return {
            "iteration": idx,
            "boot_s": swap.elapsed_s,
            "boot_mechanism": swap.mechanism,
            "genes": swap.genes,
            "first_context_ms": wall_ms[0],
            "second_context_ms": wall_ms[1],
            "third_context_ms": wall_ms[2],
            "gold_rank_by_request": ranks,
            # Fresh process per iteration: the last cumulative map IS this
            # iteration's total model-load bill.
            "model_load_ms": dict(express[-1].get("model_load_ms") or {})
            if express else {},
            "express_ms_by_request": express_by_request,
            "n_express_entries": len(express),
            "attribution_gate": _attribution_gate(express),
            "ring_tail_sample": ring,
            "ring_error": ring_error,
        }
    finally:
        server.stop()  # true cold boot next iteration (issue #127 tree-kill)


# ---------------------------------------------------------------- parent ----

def _median(vals: list[float], nd: int = 1) -> float | None:
    return round(statistics.median(vals), nd) if vals else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--genome",
                    default="F:/Projects/cymatix-context/genomes/dogfood/genome.db",
                    help="bed genome (READ-ONLY; worktrees don't carry it, "
                         "so the default is absolute)")
    ap.add_argument("--resolved", default="benchmarks/dogfood/needles_resolved.json")
    ap.add_argument("--gold",
                    default="F:/Projects/cymatix-context/benchmarks/dogfood/"
                            "gene_ids/gold_by_needle.json")
    ap.add_argument("--iterations", type=int, default=5,
                    help="cold boots to measure (default 5)")
    ap.add_argument("--k", type=int, default=12,
                    help="recall cutoff for the gold-rank guard (default 12)")
    ap.add_argument("--port", type=int, default=11491,
                    help="non-default port so a user server on 11437 is safe")
    ap.add_argument("--out", default="benchmarks/dogfood/cold_start.json")
    ap.add_argument("--request-timeout", type=float, default=120,
                    help="per-request timeout in s; request #1 pays the "
                         "encoder load (15-60s typical), so keep generous")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    resolved = json.loads((REPO_ROOT / args.resolved).read_text(encoding="utf-8"))
    needle = resolved["needles"][0]  # same needle every request, by design
    gold_path = REPO_ROOT / args.gold
    gold = set(json.loads(gold_path.read_text(encoding="utf-8"))
               .get(needle["name"], []))

    # Bed identity lives next to the gold pack (both gitignored, absolute).
    summary_path = gold_path.parent / "summary.json"
    identity = (json.loads(summary_path.read_text(encoding="utf-8"))
                .get("identity_sha256") if summary_path.exists() else None)

    run_dir = REPO_ROOT / "benchmarks/dogfood/logs/cold_start" / \
        time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"cold start: {args.iterations} iterations, needle "
          f"{needle['name']!r}, port {args.port}, bed "
          f"{identity[:16] if identity else 'UNKNOWN'}\n")

    iters: list[dict] = []
    for i in range(args.iterations):
        t0 = time.time()
        try:
            it = _run_iteration(i, args, needle, gold, run_dir)
        except Exception as exc:  # noqa: BLE001 — a dead iteration is a datapoint
            log.warning("iteration %d failed", i, exc_info=True)
            iters.append({"iteration": i, "error": repr(exc)})
            continue
        iters.append(it)
        gate = it["attribution_gate"]
        print(f"  iter {i}: boot {it['boot_s']}s  "
              f"#1 {it['first_context_ms']}ms  #2 {it['second_context_ms']}ms  "
              f"#3 {it['third_context_ms']}ms  "
              f"load {round(sum(it['model_load_ms'].values()), 1)}ms  "
              f"gate {gate.get('verdict')}  ranks {it['gold_rank_by_request']}  "
              f"({time.time() - t0:.0f}s)", flush=True)

    ok = [it for it in iters if "error" not in it]
    if not ok:
        print("no successful iterations — receipt not written", file=sys.stderr)
        return 1

    firsts = [it["first_context_ms"] for it in ok]
    thirds = [it["third_context_ms"] for it in ok]
    loads = [sum(it["model_load_ms"].values()) for it in ok]
    gates = [it["attribution_gate"] for it in ok]
    first_ex = [g["first_express_ms"] for g in gates if "first_express_ms" in g]
    warm_ex = [g["warm_express_ms"] for g in gates if "warm_express_ms" in g]
    adj_ex = [g["adjusted_first_express_ms"] for g in gates
              if "adjusted_first_express_ms" in g]
    ranks_all = [r for it in ok for r in it["gold_rank_by_request"]]

    first_med = _median(firsts)
    third_med = _median(thirds)
    load_med = _median(loads)
    rollup = {
        "boot_s_median": _median([it["boot_s"] for it in ok], nd=3),
        "first_context_ms_median": first_med,
        "second_context_ms_median": _median(
            [it["second_context_ms"] for it in ok]),
        "third_context_ms_median": third_med,
        "model_load_total_ms_median": load_med,
        "first_express_ms_median": _median(first_ex),
        "warm_express_ms_median": _median(warm_ex),
        "adjusted_first_express_ms_median": _median(adj_ex),
        # What request #1 would cost if a warmup endpoint pre-paid the cold
        # tax: the warm request is the floor, the delta is the saving.
        "warmup_endpoint_would_save_ms_median": _median(
            [f - t for f, t in zip(firsts, thirds)]),
        "cold_over_warm_x": (round(first_med / third_med, 2)
                             if first_med and third_med else None),
        "model_load_fraction_of_first": (round(load_med / first_med, 3)
                                         if load_med and first_med else None),
        f"recall@{args.k}": (round(sum(1 for r in ranks_all
                                       if r and r <= args.k)
                                   / len(ranks_all), 4)
                             if ranks_all else None),
    }

    # Overall verdict, bench_plr_smoke style: any FAIL (dead timers) or
    # errored iteration fails the run; WARN propagates (loads recorded but
    # cold residual remains — informational, see _attribution_gate); a run
    # with missing ring evidence is INCONCLUSIVE, not PASS.
    verdicts = [g.get("verdict") for g in gates]
    if any(v == "FAIL" for v in verdicts) or len(ok) < len(iters):
        verdict = "FAIL"
    elif any(v == "INCONCLUSIVE" for v in verdicts):
        verdict = "INCONCLUSIVE"
    elif any(v == "WARN" for v in verdicts):
        verdict = "WARN"
    else:
        verdict = "PASS"

    payload = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "genome": str(REPO_ROOT / args.genome),
        "bed_identity_sha256": identity,
        "port": args.port,
        "iterations": args.iterations,
        "k": args.k,
        "needle": needle["name"],
        "request_timeout_s": args.request_timeout,
        "worker_model": "one BenchServer uvicorn per iteration, fresh process "
                        "= fresh encoders, 3 sequential /context requests, "
                        "read-only fixture, tree-kill between iterations",
        "note": "no untimed warmup — request #1 cold IS the measurement; "
                "model_load_ms is process-cumulative so a fresh process per "
                "iteration makes the map that iteration's load bill; "
                "attribution gate catches a misplaced timer; ring aggregate "
                "is a ~128-entry tail sample (ring_tail_sample); gold ranks "
                "recorded as a fast-because-broken guard",
        "iterations_detail": iters,
        "rollup": rollup,
        "attribution_gate_criterion": "PASS iff |adjusted_first_express - "
                                      "warm_express| <= 0.5*warm_express; "
                                      "FAIL only when a cold first request "
                                      "recorded ZERO model-load ms (timers "
                                      "dead); WARN = loads recorded, cold "
                                      "residual remains (cold_residual_ms)",
        "verdict": verdict,
        "worker_logs": str(run_dir),
    }
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"\nmedians: boot {rollup['boot_s_median']}s  "
          f"#1 {rollup['first_context_ms_median']}ms  "
          f"#3 {rollup['third_context_ms_median']}ms  "
          f"load {rollup['model_load_total_ms_median']}ms  "
          f"warmup would save {rollup['warmup_endpoint_would_save_ms_median']}ms  "
          f"verdict {verdict}")
    print(f"\nwrote {out}")
    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""run_latency_scaling.py — latency under concurrent workers on one bed.

Every dogfood number so far was measured single-file: one process, one query
at a time. Real usage is a handful of agent sessions hitting the same local
genome at once, so the open question is what a second concurrent worker does
to per-query latency. This measures exactly that — same genome path, same 18
needles, concurrency 1 then 2 (or any ``--concurrency`` list), and reports the
scaling ratio.

Worker model: one OS subprocess per worker, each with its own
``CymatixContextManager`` opened read-only on the **same** genome file. That
is the in-process harness convention (`run_baseline.py`), not the HTTP server
path — it measures device contention (CPU, memory bandwidth, SQLite reads),
with each worker paying its own encoder load. A server-mode variant would
measure FastAPI queueing on shared encoders instead; that is a different
question and a different script.

Honesty guardrails, same spirit as the rest of the bed:

* **Warmup is untimed.** The first query loads BGE-M3/SPLADE and can take
  15-60s; folding it into the samples would swamp the effect being measured.
* **Workers are barrier-synced.** Each signals READY after warmup and waits
  for GO, so timed windows actually overlap. The receipt records the overlap
  fraction — a low value means the "concurrent" claim is weak and the run
  should not be quoted.
* **Rank is recorded per query.** A fast worker that stopped retrieving gold
  would otherwise read as a latency win. recall@12 per level must match the
  single-worker baseline (0.667 on the clean bed) or the run is suspect.

Usage::

    python benchmarks/dogfood/run_latency_scaling.py                # 1,2
    python benchmarks/dogfood/run_latency_scaling.py --concurrency 1,2,4
    python benchmarks/dogfood/run_latency_scaling.py --limit 3 --rounds 1
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- worker ----

def worker_main(args: argparse.Namespace) -> int:
    """Runs inside each spawned subprocess: warmup, barrier, timed rounds."""
    os.environ["CYMATIX_GENOME_PATH"] = str(REPO_ROOT / args.genome)
    os.environ["CYMATIX_DISABLE_LEARN"] = "1"

    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager

    resolved = json.loads((REPO_ROOT / args.resolved).read_text(encoding="utf-8"))
    needles = resolved["needles"][: args.limit] if args.limit else resolved["needles"]
    gold_by_needle = {k: set(v) for k, v in json.loads(
        (REPO_ROOT / args.gold).read_text(encoding="utf-8")).items()}

    cfg = load_config()
    cfg.genome.path = str(REPO_ROOT / args.genome)
    manager = CymatixContextManager(cfg)

    # Untimed warmup: encoder load + first-query caches.
    manager.build_context(needles[0]["query"], read_only=True,
                          ignore_delivered=True, max_genes=args.k)

    sync = Path(args.sync_dir)
    (sync / f"ready_{args.worker_idx}").write_text("ready", encoding="utf-8")
    deadline = time.time() + 600
    go = sync / "go"
    while not go.exists():
        if time.time() > deadline:
            print(f"worker {args.worker_idx}: no GO within 600s", file=sys.stderr)
            return 3
        time.sleep(0.05)

    samples = []
    t_start = time.time()
    for rnd in range(args.rounds):
        for nd in needles:
            gold = gold_by_needle.get(nd["name"], set())
            q0 = time.time()
            try:
                manager.build_context(nd["query"], read_only=True,
                                      ignore_delivered=True, max_genes=args.k)
            except Exception as exc:  # noqa: BLE001 - a failing query is a datapoint
                samples.append({"needle": nd["name"], "round": rnd,
                                "error": repr(exc)})
                continue
            latency_ms = (time.time() - q0) * 1000
            raw = manager.genome.last_query_scores or {}
            ordered = [g for g, _ in sorted(raw.items(),
                                            key=lambda kv: kv[1], reverse=True)][: args.k]
            rank = next((i for i, g in enumerate(ordered, 1) if g in gold), None)
            samples.append({"needle": nd["name"], "round": rnd,
                            "latency_ms": round(latency_ms, 1), "rank": rank})
    t_end = time.time()

    Path(args.worker_out).write_text(json.dumps({
        "worker": args.worker_idx,
        "t_start": t_start,
        "t_end": t_end,
        "samples": samples,
    }, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------------------------------------------------------------- parent ----

def _percentile(vals: list[float], p: float) -> float:
    # Ceiling index: at small n a floor index can land *below* the median
    # (p95 of 2 samples must be the max, not the min).
    s = sorted(vals)
    return s[min(len(s) - 1, math.ceil(p * (len(s) - 1)))]


def _run_level(concurrency: int, args: argparse.Namespace, run_dir: Path) -> dict:
    """Spawn `concurrency` workers against the same genome, aggregate samples."""
    sync = run_dir / f"sync_c{concurrency}"
    sync.mkdir(parents=True, exist_ok=True)

    procs, outs, logs = [], [], []
    for i in range(concurrency):
        out = run_dir / f"worker_c{concurrency}_{i}.json"
        log = open(run_dir / f"worker_c{concurrency}_{i}.log", "w", encoding="utf-8")
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker",
               "--worker-idx", str(i), "--worker-out", str(out),
               "--sync-dir", str(sync),
               "--genome", args.genome, "--resolved", args.resolved,
               "--gold", args.gold, "--k", str(args.k),
               "--rounds", str(args.rounds), "--limit", str(args.limit)]
        procs.append(subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=log, stderr=log,
                                      creationflags=NO_WINDOW))
        outs.append(out)
        logs.append(log)

    try:
        # Barrier: all workers warmed up before any timed query runs.
        deadline = time.time() + args.level_timeout
        while sum((sync / f"ready_{i}").exists() for i in range(concurrency)) < concurrency:
            if any(p.poll() not in (None, 0) for p in procs):
                raise RuntimeError("a worker died during warmup — see its .log")
            if time.time() > deadline:
                raise TimeoutError(f"workers not ready within {args.level_timeout}s")
            time.sleep(0.2)
        (sync / "go").write_text("go", encoding="utf-8")

        for p in procs:
            p.wait(timeout=args.level_timeout)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        for log in logs:
            log.close()

    workers = [json.loads(o.read_text(encoding="utf-8")) for o in outs]
    ok = [s for w in workers for s in w["samples"] if "latency_ms" in s]
    errors = [s for w in workers for s in w["samples"] if "error" in s]
    if not ok:
        raise RuntimeError(f"c={concurrency}: no successful samples ({len(errors)} errors)")

    lat = [s["latency_ms"] for s in ok]
    ranks = [s["rank"] for s in ok]
    wall = max(w["t_end"] for w in workers) - min(w["t_start"] for w in workers)
    # Overlap fraction: shared timed window over the widest worker span. 1.0
    # means fully concurrent; low values mean the level barely overlapped and
    # its numbers should not be quoted as concurrency.
    overlap = ((min(w["t_end"] for w in workers) - max(w["t_start"] for w in workers))
               / wall if concurrency > 1 and wall > 0 else 1.0)

    return {
        "concurrency": concurrency,
        "samples": len(ok),
        "errors": len(errors),
        "median_latency_ms": round(statistics.median(lat), 1),
        "mean_latency_ms": round(statistics.mean(lat), 1),
        "p95_latency_ms": round(_percentile(lat, 0.95), 1),
        "max_latency_ms": round(max(lat), 1),
        "per_worker_median_ms": [round(statistics.median(
            [s["latency_ms"] for s in w["samples"] if "latency_ms" in s]), 1)
            for w in workers],
        "recall@12": round(sum(1 for r in ranks if r and r <= 12) / len(ranks), 4),
        "wall_seconds": round(wall, 1),
        "overlap_fraction": round(max(0.0, overlap), 3),
        "throughput_qps": round(len(ok) / wall, 2) if wall > 0 else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--genome", default="genomes/dogfood/genome.db")
    ap.add_argument("--resolved", default="benchmarks/dogfood/needles_resolved.json")
    ap.add_argument("--gold", default="benchmarks/dogfood/gene_ids/gold_by_needle.json")
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--rounds", type=int, default=2,
                    help="timed passes over the needle set per worker (default 2)")
    ap.add_argument("--limit", type=int, default=0,
                    help="use only the first N needles (0 = all; for smoke tests)")
    ap.add_argument("--concurrency", default="1,2",
                    help="comma list of worker counts, each level run to completion")
    ap.add_argument("--level-timeout", type=int, default=900)
    ap.add_argument("--out", default="benchmarks/dogfood/latency_scaling.json")
    # Internal worker-mode flags (parent spawns this same file).
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--worker-idx", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--worker-out", default="", help=argparse.SUPPRESS)
    ap.add_argument("--sync-dir", default="", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker:
        return worker_main(args)

    levels_req = [int(c) for c in args.concurrency.split(",") if c.strip()]
    run_dir = REPO_ROOT / "benchmarks/dogfood/logs/latency_scaling" / \
        time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    summary_path = REPO_ROOT / "benchmarks/dogfood/gene_ids/summary.json"
    identity = (json.loads(summary_path.read_text(encoding="utf-8"))
                .get("identity_sha256") if summary_path.exists() else None)

    print(f"latency scaling: concurrency {levels_req}, rounds={args.rounds}, "
          f"k={args.k}, bed {identity[:16] if identity else 'UNKNOWN'}\n")

    levels = []
    for c in levels_req:
        t0 = time.time()
        lvl = _run_level(c, args, run_dir)
        levels.append(lvl)
        print(f"  c={c}: median {lvl['median_latency_ms']}ms  "
              f"p95 {lvl['p95_latency_ms']}ms  r@12 {lvl['recall@12']}  "
              f"overlap {lvl['overlap_fraction']}  qps {lvl['throughput_qps']}  "
              f"({time.time() - t0:.0f}s incl. warmup)", flush=True)

    base = next((l for l in levels if l["concurrency"] == 1), None)
    scaling = [{
        "concurrency": l["concurrency"],
        "median_latency_x": round(l["median_latency_ms"] / base["median_latency_ms"], 2),
        "p95_latency_x": round(l["p95_latency_ms"] / base["p95_latency_ms"], 2),
        "throughput_x": round(l["throughput_qps"] / base["throughput_qps"], 2)
        if base["throughput_qps"] and l["throughput_qps"] else None,
    } for l in levels] if base else []

    payload = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "genome": str(REPO_ROOT / args.genome),
        "bed_identity_sha256": identity,
        "k": args.k,
        "rounds": args.rounds,
        "needles": args.limit or "all",
        "cpu_count": os.cpu_count(),
        "worker_model": "subprocess per worker, own manager + encoders, "
                        "same genome file, read-only",
        "note": "warmup untimed; workers barrier-synced; ranks recorded as a "
                "fast-because-broken guard",
        "levels": levels,
        "scaling_vs_c1": scaling,
        "worker_logs": str(run_dir),
    }
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if scaling:
        print("\nscaling vs c=1: " + "   ".join(
            f"c={s['concurrency']}: {s['median_latency_x']}x median, "
            f"{s['throughput_x']}x qps" for s in scaling if s["concurrency"] != 1))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

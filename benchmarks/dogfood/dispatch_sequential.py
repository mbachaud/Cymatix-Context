"""dispatch_sequential.py — run a queue of dogfood bench series, one at a time.

Sequential on purpose: every series except the latency-scaling one is a
single-worker measurement, and letting two of them share the box would put
scheduler noise into numbers that are quoted to a tenth of an MRR point.
Concurrency is a *measured variable* (run_latency_scaling.py), never an
accident of dispatch.

The default queue is the clean-bed follow-up called for by
docs/benchmarks/2026-07-29-clean-bed-rerun.md: the dense-weight and
rerank-combinator nulls were measured under contamination and must be repeated
before the feature-isolation campaign treats them as settled. The depth null
is exempt (it follows from recall@50 = 1.000, which still holds on the clean
bed). Plus the first latency-scaling series, 1 -> 2 workers.

Each series gets its own log file; a manifest is rewritten after every series
so a crashed or interrupted dispatch still leaves an inspectable record. A
failing series is recorded and the queue continues — one broken arm should
not cost a night of bench time.

Usage::

    python benchmarks/dogfood/dispatch_sequential.py --dry-run
    python benchmarks/dogfood/dispatch_sequential.py
    python benchmarks/dogfood/dispatch_sequential.py --only latency_scaling_1to2
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

QUEUE: list[dict] = [
    {
        "name": "ladder_reorder_clean",
        "why": "re-run contaminated-era fusion/rerank/dense nulls on the clean bed",
        "cmd": ["benchmarks/dogfood/run_ladder.py",
                "--axes", "baseline,fusion,rerank,dense", "--k", "50",
                "--out", "benchmarks/dogfood/ladder_reorder_clean.json"],
        "receipt": "benchmarks/dogfood/ladder_reorder_clean.json",
    },
    {
        "name": "ladder_densecross_clean",
        "why": "fusion-conditional control: dense weight under additive vs rrf, clean bed",
        "cmd": ["benchmarks/dogfood/run_ladder.py",
                "--axes", "densecross", "--k", "50",
                "--out", "benchmarks/dogfood/ladder_densecross_clean.json"],
        "receipt": "benchmarks/dogfood/ladder_densecross_clean.json",
    },
    {
        "name": "latency_scaling_1to2",
        "why": "latency scaling 1 -> 2 concurrent workers, same genome path",
        "cmd": ["benchmarks/dogfood/run_latency_scaling.py",
                "--concurrency", "1,2",
                "--out", "benchmarks/dogfood/latency_scaling_1to2.json"],
        "receipt": "benchmarks/dogfood/latency_scaling_1to2.json",
    },
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", default="",
                    help="comma list of series names to run (default: all)")
    ap.add_argument("--timeout", type=int, default=3600, help="per-series seconds")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    only = {n.strip() for n in args.only.split(",") if n.strip()}
    queue = [s for s in QUEUE if not only or s["name"] in only]
    unknown = only - {s["name"] for s in QUEUE}
    if unknown:
        print(f"ERROR: unknown series: {sorted(unknown)}", file=sys.stderr)
        return 2

    if args.dry_run:
        for s in queue:
            print(f"{s['name']:24s} {' '.join(s['cmd'])}\n"
                  f"{'':24s} -> {s['receipt']}   ({s['why']})")
        return 0

    log_dir = REPO_ROOT / "benchmarks/dogfood/logs/dispatch" / \
        time.strftime("%Y%m%d-%H%M%S")
    log_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = REPO_ROOT / "benchmarks/dogfood/dispatch_manifest.json"
    manifest = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "log_dir": str(log_dir), "series": []}

    print(f"dispatch: {len(queue)} series, sequential, logs in {log_dir}\n")
    for s in queue:
        entry = {"name": s["name"], "why": s["why"],
                 "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        log_path = log_dir / f"{s['name']}.log"
        t0 = time.time()
        print(f"  -> {s['name']} ...", flush=True)
        try:
            with open(log_path, "w", encoding="utf-8") as log:
                proc = subprocess.run([sys.executable, *s["cmd"]], cwd=REPO_ROOT,
                                      stdout=log, stderr=subprocess.STDOUT,
                                      timeout=args.timeout, creationflags=NO_WINDOW)
            entry["exit_code"] = proc.returncode
        except subprocess.TimeoutExpired:
            entry["exit_code"] = None
            entry["error"] = f"timeout after {args.timeout}s"
        except Exception as exc:  # noqa: BLE001 - record and keep the queue moving
            entry["exit_code"] = None
            entry["error"] = repr(exc)
        entry["seconds"] = round(time.time() - t0, 1)
        entry["log"] = str(log_path)
        receipt = REPO_ROOT / s["receipt"]
        entry["receipt"] = str(receipt) if receipt.exists() else None
        ok = entry["exit_code"] == 0 and entry["receipt"]
        entry["ok"] = bool(ok)
        manifest["series"].append(entry)
        # Rewritten after every series so an interrupted dispatch still reports.
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n",
                                 encoding="utf-8")
        print(f"     {'ok' if ok else 'FAILED'}  {entry['seconds']}s"
              + ("" if ok else f"  (see {log_path})"), flush=True)

    failed = [e["name"] for e in manifest["series"] if not e["ok"]]
    print(f"\nmanifest: {manifest_path}")
    if failed:
        print(f"FAILED series: {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

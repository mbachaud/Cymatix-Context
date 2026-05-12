"""PLR smoke bench — does [plr] enabled=true add plr_confidence to
/context/packet responses without degrading p95 latency?

Hits /context/packet with N harvested-needle queries, captures
(latency_ms, has_plr_field, plr_confidence_value_if_present). Run
twice — once at PLR off, once at PLR on — then compare p50/p95 latency
plus presence rate.

Acceptance gate (#74):
  - p95 delta < 50 ms (on - off)
  - plr_confidence present on >= 90% of on-side responses
  - plr_confidence NEVER present on off-side responses

Usage:
  HELIX_URL=http://127.0.0.1:11437 \
  GENOME_DB=F:/.../genome-bench-2026-05-08.db \
  N=50 SEED=42 \
  OUTPUT=overnight_logs/plr_smoke_off.json \
  python benchmarks/bench_plr_smoke.py

  # Once both off + on JSONs exist:
  python benchmarks/bench_plr_smoke.py --summarize off.json on.json

Same shape as bench_needle_1000.py for query corpus: reuses
harvest_needles + build_query_blind from that module so the corpus
matches the BROAD tighten bench. Falls back to a small programmatic
corpus if the helper can't be imported (e.g. snapshot not present).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

HELIX_URL = os.environ.get("HELIX_URL", "http://127.0.0.1:11437")
GENOME_DB = os.environ.get(
    "GENOME_DB", "F:/Projects/helix-context/genome-bench-2026-05-08.db"
)
N = int(os.environ.get("N", "50"))
SEED = int(os.environ.get("SEED", "42"))
OUTPUT = os.environ.get(
    "OUTPUT", "F:/Projects/helix-context/overnight_logs/plr_smoke.json"
)
TIMEOUT_S = float(os.environ.get("TIMEOUT_S", "30"))

# Pull the same harvester the BROAD bench uses so the corpus shape lines up.
_THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS))
try:
    from bench_needle_1000 import (  # type: ignore[import-not-found]
        build_query_blind,
        harvest_needles,
    )
    _HAS_HARVESTER = True
except Exception as e:  # pragma: no cover - exercised when snapshot absent
    _HAS_HARVESTER = False
    _IMPORT_ERR = repr(e)


def _fallback_queries(n: int) -> list[dict]:
    """Minimal programmatic corpus if the harvester can't load."""
    seeds = [
        "What is the port in the helix source?",
        "What is the path mentioned in the code?",
        "What is the value of expression_tokens?",
        "What is the value of ribosome_tokens?",
        "What is the budget in the helix source?",
        "What is the name?",
        "What is the value of upstream_timeout?",
        "What is the value of cold_start_threshold?",
        "What is the value of keep_alive?",
        "What is the threshold in the helix source?",
    ]
    out = []
    for i in range(n):
        out.append({"query": seeds[i % len(seeds)], "category": "fallback"})
    return out


def _build_corpus() -> list[dict]:
    if not _HAS_HARVESTER:
        print(f"[smoke] harvester unavailable ({_IMPORT_ERR}); fallback corpus",
              file=sys.stderr)
        return _fallback_queries(N)
    if not Path(GENOME_DB).exists():
        print(f"[smoke] genome snapshot missing at {GENOME_DB}; fallback",
              file=sys.stderr)
        return _fallback_queries(N)
    needles = harvest_needles(GENOME_DB, N, SEED)
    rows = []
    for n in needles:
        rows.append({"query": build_query_blind(n), "category": n.get("category", "other")})
    return rows


def _run() -> None:
    corpus = _build_corpus()
    print(f"[smoke] {len(corpus)} queries against {HELIX_URL}/context/packet")

    rows = []
    client = httpx.Client(timeout=TIMEOUT_S)
    t_overall = time.time()
    for i, item in enumerate(corpus):
        body = {"query": item["query"], "task_type": "explain", "max_genes": 8}
        t0 = time.time()
        status = -1
        plr_present = False
        plr_value = None
        item_count = 0
        err = None
        try:
            r = client.post(f"{HELIX_URL}/context/packet", json=body)
            status = r.status_code
            try:
                data = r.json()
                if isinstance(data, dict):
                    if "plr_confidence" in data:
                        plr_present = True
                        block = data["plr_confidence"]
                        if isinstance(block, dict):
                            # The block is a small dict; capture the headline.
                            for k in ("plr_confidence", "log_odds", "prob_B"):
                                if k in block:
                                    plr_value = block[k]
                                    break
                            if plr_value is None:
                                plr_value = block
                        else:
                            plr_value = block
                    # Count evidence items for "no-evidence" exception in gate.
                    for k in ("verified", "stale_risk", "items"):
                        v = data.get(k)
                        if isinstance(v, list):
                            item_count += len(v)
            except Exception as je:
                err = f"json_parse: {je!r}"
        except Exception as e:
            err = repr(e)
        latency_ms = (time.time() - t0) * 1000.0
        rows.append({
            "i": i,
            "query": item["query"],
            "category": item.get("category"),
            "status": status,
            "latency_ms": latency_ms,
            "has_plr": plr_present,
            "plr_value": plr_value,
            "item_count": item_count,
            "err": err,
        })
        if (i + 1) % 10 == 0:
            print(f"  [{i+1:>4}/{len(corpus)}] last={latency_ms:.0f}ms plr={plr_present}",
                  file=sys.stderr)
    wall_s = time.time() - t_overall

    # Aggregate
    latencies = [r["latency_ms"] for r in rows if r["status"] == 200]
    p50 = statistics.median(latencies) if latencies else None
    p95 = (statistics.quantiles(latencies, n=20)[-1]
           if len(latencies) >= 20 else (max(latencies) if latencies else None))
    plr_count = sum(1 for r in rows if r["has_plr"])
    plr_rate = plr_count / len(rows) if rows else 0.0
    with_items = sum(1 for r in rows if r["item_count"] > 0)
    plr_rate_with_items = (
        sum(1 for r in rows if r["has_plr"] and r["item_count"] > 0) / with_items
        if with_items else 0.0
    )

    summary = {
        "helix_url": HELIX_URL,
        "n": len(rows),
        "seed": SEED,
        "wall_s": wall_s,
        "p50_ms": p50,
        "p95_ms": p95,
        "plr_present_rate": plr_rate,
        "plr_present_rate_with_items": plr_rate_with_items,
        "ok_count": len(latencies),
        "with_items_count": with_items,
    }
    print(f"[smoke] DONE wall={wall_s:.1f}s p50={p50}ms p95={p95}ms plr_rate={plr_rate:.2%}")

    out_path = Path(OUTPUT)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump({"summary": summary, "rows": rows}, f, indent=2)
    print(f"[smoke] wrote {out_path}")


def _summarize(off_path: str, on_path: str) -> None:
    with open(off_path, "r", encoding="utf-8") as f:
        off = json.load(f)
    with open(on_path, "r", encoding="utf-8") as f:
        on = json.load(f)
    off_s, on_s = off["summary"], on["summary"]
    p95_delta = (on_s["p95_ms"] or 0) - (off_s["p95_ms"] or 0)
    p50_delta = (on_s["p50_ms"] or 0) - (off_s["p50_ms"] or 0)

    # Gate checks
    off_plr_count = sum(1 for r in off["rows"] if r["has_plr"])
    on_plr_count = sum(1 for r in on["rows"] if r["has_plr"])
    gate_offside_clean = off_plr_count == 0
    gate_onside_presence_with_items = on_s.get("plr_present_rate_with_items", 0) >= 0.90
    gate_p95 = p95_delta < 50.0

    verdict = "PASS" if (gate_offside_clean and gate_onside_presence_with_items and gate_p95) else "FAIL"
    print(json.dumps({
        "off": off_s,
        "on": on_s,
        "p50_delta_ms": p50_delta,
        "p95_delta_ms": p95_delta,
        "off_plr_count": off_plr_count,
        "on_plr_count": on_plr_count,
        "gate_offside_clean": gate_offside_clean,
        "gate_onside_presence_with_items": gate_onside_presence_with_items,
        "gate_p95_under_50ms": gate_p95,
        "verdict": verdict,
    }, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summarize", nargs=2, metavar=("OFF_JSON", "ON_JSON"),
                    help="Compare off vs on smoke JSONs and emit gate verdict.")
    args = ap.parse_args()
    if args.summarize:
        _summarize(args.summarize[0], args.summarize[1])
        return
    _run()


if __name__ == "__main__":
    main()

"""bench_shard_recall.py -- Shard x dense recall A/B harness.

Reads gold needles from build_shard_gold.py, issues POST /fingerprint per
query, finds the first gold-path hit in the ranked list, and computes:

  recall@1, recall@3, recall@5, recall@10
  MRR (Mean Reciprocal Rank)

All metrics are broken out by needle type (within / cross) and written to
benchmarks/results/shard_recall_<label>_<ts>.json.

Conventions match coderag_bench_helix.py and bench_claude_matrix.py:
  - urllib (no third-party deps)
  - /fingerprint endpoint, max_results=10, score_floor=0, profile="fast"
  - path normalisation: forward-slash, lowercase, substring containment
    (same rule as bench_claude_matrix.retrieval_probe and test_bench_needles.py)
  - results/ JSON + printed table

GOLD-PATH MATCH RULE
--------------------
A fingerprint source S matches a gold_path G when:
    normalise(G) is a substring of normalise(S)
    OR
    normalise(S) is a substring of normalise(G)

where normalise(x) = x.replace("\\\\", "/").lower().strip()

This is the same bidirectional substring rule used by the existing needle
harnesses.  It means gold_paths are project-relative substrings like
"helix-context/helix_context/pipeline/stages.py".

CLI
---
python benchmarks/bench_shard_recall.py \\
    --needles benchmarks/results/shard_gold.jsonl \\
    --helix-url http://127.0.0.1:11437 \\
    --label sharded_medium

python benchmarks/bench_shard_recall.py \\
    --needles benchmarks/results/shard_gold.jsonl \\
    --helix-url http://127.0.0.1:11438 \\
    --label unsharded_medium_dense

RUN SEQUENCE (4 genome modes)
------------------------------
# 1. SHARDED / dense-disabled server (medium corpus)
python benchmarks/bench_shard_recall.py \\
    --needles benchmarks/results/shard_gold_medium.jsonl \\
    --helix-url http://127.0.0.1:11437 \\
    --label medium_sharded

# 2. UNSHARDED / dense-ON + backfilled server (medium corpus)
python benchmarks/bench_shard_recall.py \\
    --needles benchmarks/results/shard_gold_medium.jsonl \\
    --helix-url http://127.0.0.1:11438 \\
    --label medium_unsharded_dense

# 3. SHARDED / dense-disabled server (xl corpus)
python benchmarks/bench_shard_recall.py \\
    --needles benchmarks/results/shard_gold_xl.jsonl \\
    --helix-url http://127.0.0.1:11437 \\
    --label xl_sharded

# 4. UNSHARDED / dense-ON + backfilled server (xl corpus)
python benchmarks/bench_shard_recall.py \\
    --needles benchmarks/results/shard_gold_xl.jsonl \\
    --helix-url http://127.0.0.1:11438 \\
    --label xl_unsharded_dense
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

KS = (1, 3, 5, 10)


# ---------------------------------------------------------------------------
# Path normalisation (matches bench_claude_matrix / test_bench_needles)
# ---------------------------------------------------------------------------

def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").lower().strip()


def _gold_hit(source: str, gold_paths: list[str]) -> bool:
    """Return True if source matches any gold path (bidirectional substring)."""
    sn = _norm(source)
    for gp in gold_paths:
        gn = _norm(gp)
        if gn and (gn in sn or sn in gn):
            return True
    return False


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def _recall_at(rank1: int | None, k: int) -> float:
    """1.0 if rank (1-based) <= k else 0.0.  None means not retrieved."""
    if rank1 is None:
        return 0.0
    return 1.0 if rank1 <= k else 0.0


def _rr(rank1: int | None) -> float:
    """Reciprocal rank.  None (not retrieved) scores 0."""
    if rank1 is None:
        return 0.0
    return 1.0 / rank1


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ---------------------------------------------------------------------------
# /fingerprint HTTP call
# ---------------------------------------------------------------------------

def fingerprint(
    helix_url: str,
    query: str,
    max_results: int = 10,
    timeout_s: float = 30.0,
) -> tuple[list[dict], float]:
    """POST /fingerprint; return (fingerprints_list, latency_ms)."""
    url = helix_url.rstrip("/") + "/fingerprint"
    payload = json.dumps({
        "query": query,
        "max_results": max_results,
        "score_floor": 0,
        "profile": "fast",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = json.loads(resp.read())
    latency_ms = (time.time() - t0) * 1000.0
    return body.get("fingerprints", []), latency_ms


# ---------------------------------------------------------------------------
# Scoring loop
# ---------------------------------------------------------------------------

def _zero_agg() -> dict:
    a: dict = {"n": 0, "err": 0, "rr": 0.0, "latency_ms": []}
    for k in KS:
        a["recall@{}".format(k)] = 0.0
    return a


def score_needles(
    needles: list[dict],
    helix_url: str,
    max_results: int = 10,
    timeout_s: float = 30.0,
) -> tuple[dict, list[dict]]:
    """Run /fingerprint per needle; return (agg_by_type, per_needle_rows)."""
    # Aggregates keyed by needle type: "within", "cross", "all"
    agg: dict[str, dict] = collections.defaultdict(_zero_agg)
    per_rows: list[dict] = []
    n_total = len(needles)

    for qi, nd in enumerate(needles):
        ntype = nd.get("type", "within")
        if (qi + 1) % 25 == 0 or qi == 0:
            print("[shard_recall] {}/{} ...".format(qi + 1, n_total), flush=True)

        gold_paths = nd.get("gold_paths", [])

        try:
            fps, lat_ms = fingerprint(helix_url, nd["question"], max_results, timeout_s)
        except Exception as exc:
            for key in (ntype, "all"):
                agg[key]["err"] += 1
            per_rows.append({
                "id": nd.get("id"),
                "project": nd.get("project"),
                "type": ntype,
                "question": nd.get("question", "")[:120],
                "gold_paths": gold_paths,
                "rank": None,
                "error": repr(exc),
            })
            continue

        # Find rank of first gold-path hit (1-based; None if not retrieved).
        rank1: int | None = None
        for i, fp in enumerate(fps):
            src = fp.get("source") or fp.get("path") or ""
            if _gold_hit(src, gold_paths):
                rank1 = i + 1
                break

        for key in (ntype, "all"):
            a = agg[key]
            a["n"] += 1
            a["rr"] += _rr(rank1)
            for k in KS:
                a["recall@{}".format(k)] += _recall_at(rank1, k)
            a["latency_ms"].append(lat_ms)

        per_rows.append({
            "id": nd.get("id"),
            "project": nd.get("project"),
            "type": ntype,
            "file_type": nd.get("file_type"),
            "question": nd.get("question", "")[:120],
            "gold_paths": gold_paths,
            "rank": rank1,
            "latency_ms": round(lat_ms, 1),
            "n_returned": len(fps),
        })

    # Finalise.
    summary: dict = {}
    for key, a in sorted(agg.items()):
        n = a["n"]
        if n == 0:
            continue
        row: dict = {
            "n": n,
            "err": a["err"],
            "mrr": round(a["rr"] / n, 4),
            "latency": {
                "median_ms": round(_percentile(a["latency_ms"], 50), 1),
                "p90_ms": round(_percentile(a["latency_ms"], 90), 1),
            },
        }
        for k in KS:
            row["recall@{}".format(k)] = round(a["recall@{}".format(k)] / n, 4)
        summary[key] = row

    return summary, per_rows


# ---------------------------------------------------------------------------
# Pretty table
# ---------------------------------------------------------------------------

def _print_table(label: str, summary: dict) -> None:
    hdr = (
        "{:<8}  {:>5}  {:>7}  {:>7}  {:>7}  {:>7}  {:>7}  {:>8}  {:>8}".format(
            "type", "n", "MRR", "R@1", "R@3", "R@5", "R@10",
            "med_ms", "p90_ms",
        )
    )
    print()
    print("Shard recall A/B -- {}".format(label))
    print(hdr)
    print("-" * len(hdr))
    for key in ("all", "within", "cross"):
        if key not in summary:
            continue
        s = summary[key]
        lat = s.get("latency", {})
        print(
            "{:<8}  {:>5}  {:>7.4f}  {:>7.4f}  {:>7.4f}  {:>7.4f}  {:>7.4f}"
            "  {:>8.1f}  {:>8.1f}".format(
                key, s["n"],
                s["mrr"],
                s.get("recall@1", 0.0),
                s.get("recall@3", 0.0),
                s.get("recall@5", 0.0),
                s.get("recall@10", 0.0),
                lat.get("median_ms", 0.0),
                lat.get("p90_ms", 0.0),
            )
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Shard x dense recall A/B harness. "
            "Issues POST /fingerprint per needle, computes recall@k + MRR "
            "broken out by type (within / cross), writes results JSON."
        )
    )
    ap.add_argument(
        "--needles",
        default=str(RESULTS_DIR / "shard_gold.jsonl"),
        help="Path to JSONL needle file produced by build_shard_gold.py.",
    )
    ap.add_argument(
        "--helix-url",
        default="http://127.0.0.1:11437",
        dest="helix_url",
        help="Base URL of the Helix server (default: http://127.0.0.1:11437).",
    )
    ap.add_argument(
        "--label",
        default="unlabeled",
        help="Run label for the results filename, e.g. 'medium_sharded'.",
    )
    ap.add_argument(
        "--max-results",
        type=int,
        default=10,
        dest="max_results",
        help="max_results for /fingerprint (default 10; recall@10 saturates here).",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap total needles to score (0 = all).",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-request HTTP timeout in seconds (default 30).",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Override output JSON path.",
    )
    args = ap.parse_args(argv)

    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    # Load needles.
    needles_path = Path(args.needles)
    if not needles_path.exists():
        print(
            "ERROR: needles file not found: {}\n"
            "  Run: python benchmarks/build_shard_gold.py --out {}".format(
                needles_path, needles_path
            ),
            file=sys.stderr,
        )
        return 1

    needles: list[dict] = []
    with needles_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                needles.append(json.loads(line))

    if args.limit:
        needles = needles[: args.limit]

    print("[shard_recall] {} needles from {}".format(len(needles), needles_path))
    print("[shard_recall] server: {}  label: {}".format(args.helix_url, args.label))

    # Health check.
    try:
        health_url = args.helix_url.rstrip("/") + "/health"
        with urllib.request.urlopen(health_url, timeout=30) as r:
            health = json.loads(r.read())
        print("[shard_recall] server health: {} docs={}".format(
            health.get("status", "ok"), health.get("document_count", "?")))
    except Exception as exc:
        print(
            "ERROR: cannot reach Helix at {}: {}\n"
            "  Start: python -m uvicorn helix_context._asgi:app --port 11437".format(
                args.helix_url, exc
            ),
            file=sys.stderr,
        )
        return 1

    summary, per_rows = score_needles(
        needles=needles,
        helix_url=args.helix_url,
        max_results=args.max_results,
        timeout_s=args.timeout,
    )

    _print_table(args.label, summary)

    out_path = (
        Path(args.out) if args.out
        else RESULTS_DIR / "shard_recall_{}_{}.json".format(args.label, ts)
    )
    result_blob = {
        "benchmark": "shard_recall",
        "label": args.label,
        "timestamp": ts,
        "helix_url": args.helix_url,
        "needles_file": str(needles_path),
        "n_needles": len(needles),
        "max_results": args.max_results,
        "metrics": "recall@{1,3,5,10}, MRR -- by type (within / cross / all)",
        "gold_match_rule": (
            "bidirectional case-insensitive forward-slash substring: "
            "norm(gold) in norm(source) OR norm(source) in norm(gold)"
        ),
        "summary": summary,
        "per_needle_sample": per_rows[:50],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result_blob, indent=2), encoding="utf-8")
    print("\n-> {}".format(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

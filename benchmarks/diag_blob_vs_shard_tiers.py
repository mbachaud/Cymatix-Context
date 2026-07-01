"""
diag_blob_vs_shard_tiers.py -- Per-tier candidate-pool decomposition diagnostic.

Localise WHY the unified ("blob") genome out-ranks the sharded genome at
recall@1/MRR by decomposing the advantage into two failure modes:

  A. Candidate-generation loss  -- gold is NOT IN the sharded pool at all
  B. Ranking loss               -- gold IS in the sharded pool but rank > 10

For the "blob-good / shard-bad" set (blob rank <= 10, shard rank > 10 or absent)
the diagnostic reports median per-tier score contributions for blob vs sharded,
making it possible to name which tier(s) surface the gold in the blob but fail
in the sharded merge.

WIRE SCHEMA (routes_context.py L800-826):
  Each item in fingerprints[] contains:
    "rank"               int   0-based rank
    "gene_id"            str
    "score"              float final score (base + tcm_bonus)
    "source"             str   source_id / path used for gold matching
    "path"               str   metadata path (may differ from source)
    "tier_contributions" dict  {tier_name: float, ...}  -- the key field
    "preview"            str
    "domains"            list
    "entities"           list
    "chromatin"          int

  tier_contributions is built from _merge_tier_contributions(
      helix.genome.last_tier_contributions,   # lexical + co-activation tiers
      refiner_contrib,                         # tcm, cymatics, harmonic_bin
  ) and then rounded to 4dp, sorted by key (routes_context.py L817-819).

  Known tier names (non-exhaustive; server emits whatever the retrieval
  pipeline populates):
    fts5, tag_exact, tag_prefix, co_activation, dense, splade,
    sema_boost, sema_cold, lex_anchor, authority, harmonic, harmonic_bin,
    cymatics, tcm, sr (seeded-edges), ray_trace

USAGE
-----
python benchmarks/diag_blob_vs_shard_tiers.py \\
    --needles  benchmarks/results/shard_gold_medium.jsonl \\
    --blob-url     http://127.0.0.1:11437 \\
    --sharded-url  http://127.0.0.1:11438 \\
    --label    medium

Optional:
    --max-results 200      (default 200; deep golds captured)
    --blob-rank-cap 10     (default 10; "blob-good" threshold)
    --shard-bury-cap 10    (default 10; "shard-bad"  threshold)
    --limit  N             (cap needles for quick smoke-test)
    --timeout 45           (per-request HTTP timeout in seconds)
    --out  path/to.json    (override output path)
    --verbose              (print per-needle detail to stdout)

NO SERVER IS STARTED.  This script calls already-running server URLs.
No GPU, no network fetch, no /tmp writes.
"""

from __future__ import annotations

import argparse
import datetime
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Path normalisation + gold match  (identical to bench_shard_recall.py)
# ---------------------------------------------------------------------------

def _norm(path: Optional[str]) -> str:
    return str(path or "").replace("\\", "/").lower().strip()


def _gold_hit(source: Optional[str], gold_paths: List[str]) -> bool:
    """True if source matches any gold path (bidirectional case-insensitive
    forward-slash substring rule from bench_shard_recall.py)."""
    sn = _norm(source)
    if not sn:
        return False
    for gp in gold_paths:
        gn = _norm(gp)
        if gn and (gn in sn or sn in gn):
            return True
    return False


# ---------------------------------------------------------------------------
# /fingerprint HTTP call
# ---------------------------------------------------------------------------

def _fingerprint(
    helix_url: str,
    query: str,
    max_results: int = 200,
    timeout_s: float = 45.0,
) -> Tuple[List[Dict[str, Any]], float]:
    """POST /fingerprint; return (fingerprints, latency_ms).

    Request body: {query, max_results, score_floor:0.0, profile:"fast"}.
    max_results=200 so deep golds (rank > 10) are still captured for pool
    analysis.  score_floor=0.0 so nothing is filtered.
    """
    url = helix_url.rstrip("/") + "/fingerprint"
    payload = json.dumps({
        "query": query,
        "max_results": max_results,
        "score_floor": 0.0,
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
# Per-needle gold lookup
# ---------------------------------------------------------------------------

def _find_gold(
    fingerprints: List[Dict[str, Any]],
    gold_paths: List[str],
) -> Dict[str, Any]:
    """Scan fingerprints list for the first gold hit.

    The "source" field (routes_context.py L813) carries source_id which is
    what bench_shard_recall.py matches against.  "path" is the metadata path
    and may differ; we check both for robustness.

    Returns:
      in_pool  bool
      rank     int | None  (1-based; wire schema rank is 0-based -> +1)
      score    float | None
      tiers    dict        tier_contributions of the gold item (or {})
    """
    for fp in fingerprints:
        # Prefer source field; fall back to path field
        source = fp.get("source") or fp.get("path") or ""
        if _gold_hit(source, gold_paths):
            return {
                "in_pool": True,
                "rank":    int(fp["rank"]) + 1,   # convert 0-based to 1-based
                "score":   float(fp.get("score", 0.0)),
                "tiers":   dict(fp.get("tier_contributions") or {}),
            }
    return {
        "in_pool": False,
        "rank":    None,
        "score":   None,
        "tiers":   {},
    }


# ---------------------------------------------------------------------------
# Single-needle analysis
# ---------------------------------------------------------------------------

def _analyse_needle(
    needle: Dict[str, Any],
    blob_fps: List[Dict[str, Any]],
    shard_fps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build per-needle result dict."""
    gold_paths = needle.get("gold_paths", [])
    blob_gold  = _find_gold(blob_fps,  gold_paths)
    shard_gold = _find_gold(shard_fps, gold_paths)
    return {
        "id":         needle.get("id"),
        "type":       needle.get("type", "within"),
        "question":   (needle.get("question") or "")[:200],
        "gold_paths": gold_paths,
        "blob":       blob_gold,
        "sharded":    shard_gold,
    }


# ---------------------------------------------------------------------------
# Median helper
# ---------------------------------------------------------------------------

def _safe_median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return statistics.median(values)


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def _aggregate(
    per_needle: List[Dict[str, Any]],
    blob_rank_cap: int = 10,
    shard_bury_cap: int = 10,
) -> Dict[str, Any]:
    """Compute the aggregate payload focusing on blob-good / shard-bad cases.

    Classification:
      blob-good   : blob rank <= blob_rank_cap
      shard-bad   : shard rank > shard_bury_cap  OR  not in sharded pool
      divergent   : blob-good AND shard-bad

    Within divergent:
      candidate_gen_loss  -- gold NOT in sharded pool (shard cannot even see it)
      ranking_loss        -- gold IS in sharded pool but buried deep

    Tier delta for ranking-loss set: median(blob tiers) - median(shard tiers)
    per tier, sorted by delta desc -- this names which tier(s) the blob uses
    to surface the gold that the sharded merge loses.

    Tier summary for cand-gen-loss set: blob tier medians only (shard has
    no data) to show what caused the blob to include the gold at all.
    """
    total = len(per_needle)

    blob_good_set: List[Dict[str, Any]] = []
    for row in per_needle:
        b_rank = row["blob"]["rank"]
        if b_rank is not None and b_rank <= blob_rank_cap:
            blob_good_set.append(row)

    # Classify divergent
    divergent: List[Dict[str, Any]] = []
    for row in blob_good_set:
        s_rank = row["sharded"]["rank"]
        s_in   = row["sharded"]["in_pool"]
        shard_bad = (not s_in) or (s_rank is not None and s_rank > shard_bury_cap)
        if shard_bad:
            divergent.append(row)

    cand_gen_loss = [r for r in divergent if not r["sharded"]["in_pool"]]
    ranking_loss  = [r for r in divergent if r["sharded"]["in_pool"]]

    blob_pool_total  = sum(1 for r in per_needle if r["blob"]["in_pool"])
    shard_pool_total = sum(1 for r in per_needle if r["sharded"]["in_pool"])
    shard_bad_count  = sum(
        1 for r in per_needle
        if (not r["sharded"]["in_pool"]) or (
            r["sharded"]["rank"] is not None
            and r["sharded"]["rank"] > shard_bury_cap
        )
    )

    # --- Tier delta for RANKING-LOSS set ---
    rl_tier_names: set = set()
    for row in ranking_loss:
        rl_tier_names.update(row["blob"]["tiers"].keys())
        rl_tier_names.update(row["sharded"]["tiers"].keys())

    tier_delta_ranking: Dict[str, Dict[str, Any]] = {}
    for tier in sorted(rl_tier_names):
        blob_vals  = [row["blob"]["tiers"].get(tier, 0.0)    for row in ranking_loss]
        shard_vals = [row["sharded"]["tiers"].get(tier, 0.0) for row in ranking_loss]
        blob_med   = _safe_median(blob_vals)
        shard_med  = _safe_median(shard_vals)
        delta = None
        if blob_med is not None and shard_med is not None:
            delta = round(blob_med - shard_med, 5)
        tier_delta_ranking[tier] = {
            "blob_median":  round(blob_med,  5) if blob_med  is not None else None,
            "shard_median": round(shard_med, 5) if shard_med is not None else None,
            "delta":        delta,
        }

    # Sorted by delta desc (largest blob-advantage first)
    tier_ranking_summary = sorted(
        [
            {
                "tier":         t,
                "blob_median":  v["blob_median"],
                "shard_median": v["shard_median"],
                "delta":        v["delta"],
            }
            for t, v in tier_delta_ranking.items()
            if v["delta"] is not None and v["delta"] > 0.0
        ],
        key=lambda x: x["delta"],
        reverse=True,
    )

    # --- Tier summary for CANDIDATE-GEN-LOSS set ---
    cg_tier_names: set = set()
    for row in cand_gen_loss:
        cg_tier_names.update(row["blob"]["tiers"].keys())

    tier_cand_gen_summary: List[Dict[str, Any]] = []
    for tier in sorted(cg_tier_names):
        blob_vals = [row["blob"]["tiers"].get(tier, 0.0) for row in cand_gen_loss]
        blob_med  = _safe_median(blob_vals)
        tier_cand_gen_summary.append({
            "tier":       tier,
            "blob_median": round(blob_med, 5) if blob_med is not None else None,
        })
    tier_cand_gen_summary.sort(
        key=lambda x: x["blob_median"] or 0.0, reverse=True
    )

    tier_overall_delta_rank = [t["tier"] for t in tier_ranking_summary]

    return {
        "total_needles":          total,
        "blob_good_count":        len(blob_good_set),
        "shard_bad_count":        shard_bad_count,
        "divergent_count":        len(divergent),
        "candidate_gen_loss":     len(cand_gen_loss),
        "ranking_loss":           len(ranking_loss),
        "blob_pool_total":        blob_pool_total,
        "shard_pool_total":       shard_pool_total,
        "tier_ranking_summary":   tier_ranking_summary,
        "tier_cand_gen_summary":  tier_cand_gen_summary,
        "tier_overall_delta_rank": tier_overall_delta_rank,
    }


# ---------------------------------------------------------------------------
# Stdout summary
# ---------------------------------------------------------------------------

def _print_summary(agg: Dict[str, Any], label: str,
                   blob_rank_cap: int = 10, shard_bury_cap: int = 10) -> None:
    div   = agg["divergent_count"]
    cgl   = agg["candidate_gen_loss"]
    rl    = agg["ranking_loss"]
    total = agg["total_needles"]

    print()
    print("=" * 66)
    print(f"Blob-vs-Shard tier diagnostic  [{label}]")
    print("=" * 66)
    print(f"  Total needles                 : {total}")
    print(f"  Blob gold in pool (any rank)  : {agg['blob_pool_total']}/{total}")
    print(f"  Sharded gold in pool (any)    : {agg['shard_pool_total']}/{total}")
    print(f"  Blob-good  (rank <= {blob_rank_cap})        : {agg['blob_good_count']}")
    print(f"  Shard-bad  (rank >  {shard_bury_cap} or absent): {agg['shard_bad_count']}")
    print(f"  Divergent (blob-good AND shard-bad): {div}")
    if div:
        pct_cgl = 100 * cgl / div
        pct_rl  = 100 * rl  / div
        print(f"    Candidate-gen loss (NOT in shard pool): {cgl}  ({pct_cgl:.1f}%)")
        print(f"    Ranking loss (in pool but buried)     : {rl}   ({pct_rl:.1f}%)")
    print()

    if agg["tier_ranking_summary"]:
        print("  RANKING-LOSS tier delta  (blob_median - shard_median, desc):")
        print(f"    {'tier':<22}  {'blob_med':>9}  {'shard_med':>9}  {'delta':>9}")
        print("    " + "-" * 55)
        for row in agg["tier_ranking_summary"]:
            print(
                f"    {row['tier']:<22}  {row['blob_median']:>9.4f}"
                f"  {row['shard_median']:>9.4f}  {row['delta']:>9.4f}"
            )
    else:
        print("  RANKING-LOSS set: 0 cases or no tier differences.")

    print()
    if agg["tier_cand_gen_summary"]:
        print("  CAND-GEN-LOSS blob tiers  (what surfaced gold in blob only):")
        print(f"    {'tier':<22}  {'blob_med':>9}")
        print("    " + "-" * 34)
        for row in agg["tier_cand_gen_summary"]:
            print(f"    {row['tier']:<22}  {row['blob_median']:>9.4f}")
    else:
        print("  CAND-GEN-LOSS set: 0 cases or no tier data.")

    print()
    if agg["tier_overall_delta_rank"]:
        ranked = ", ".join(agg["tier_overall_delta_rank"])
        print("  ANSWER -- tiers ranked by blob-advantage over sharded")
        print(f"  (ranking-loss cases, delta desc):")
        print(f"    {ranked}")
    print("=" * 66)
    print()


# ---------------------------------------------------------------------------
# Main orchestrator (also importable for tests)
# ---------------------------------------------------------------------------

def run(
    needles: List[Dict[str, Any]],
    blob_url: str,
    sharded_url: str,
    max_results: int = 200,
    timeout_s: float = 45.0,
    blob_rank_cap: int = 10,
    shard_bury_cap: int = 10,
    verbose: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run the diagnostic against two live servers.

    Importable entry-point used by tests (which inject mock HTTP responses
    via unittest.mock.patch on urllib.request.urlopen).

    Returns (per_needle_results, aggregate).
    """
    per_needle: List[Dict[str, Any]] = []
    n = len(needles)

    for qi, needle in enumerate(needles):
        if (qi + 1) % 25 == 0 or qi == 0:
            print(f"[diag_blob_vs_shard] {qi+1}/{n} ...", flush=True)

        query      = needle.get("question", "")
        gold_paths = needle.get("gold_paths", [])

        blob_fps:  List[Dict[str, Any]] = []
        shard_fps: List[Dict[str, Any]] = []
        blob_err:  Optional[str] = None
        shard_err: Optional[str] = None

        try:
            blob_fps, _ = _fingerprint(blob_url, query, max_results, timeout_s)
        except Exception as exc:
            blob_err = repr(exc)

        try:
            shard_fps, _ = _fingerprint(sharded_url, query, max_results, timeout_s)
        except Exception as exc:
            shard_err = repr(exc)

        row = _analyse_needle(needle, blob_fps, shard_fps)
        if blob_err:
            row["blob_error"] = blob_err
        if shard_err:
            row["shard_error"] = shard_err

        if verbose:
            b = row["blob"]
            s = row["sharded"]
            _b = f"rank={b['rank']}" if b["in_pool"] else "NOT_IN_POOL"
            _s = f"rank={s['rank']}" if s["in_pool"] else "NOT_IN_POOL"
            print(f"  [{qi+1}] blob={_b}  shard={_s}  q={query[:80]}")

        per_needle.append(row)

    agg = _aggregate(per_needle, blob_rank_cap=blob_rank_cap,
                     shard_bury_cap=shard_bury_cap)
    return per_needle, agg


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Per-tier blob-vs-sharded candidate-pool decomposition diagnostic.\n"
            "Requires two already-running Helix servers (no server is started here)."
        )
    )
    ap.add_argument(
        "--needles",
        default=str(RESULTS_DIR / "shard_gold_medium.jsonl"),
        help="JSONL needle file (build_shard_gold.py output).",
    )
    ap.add_argument(
        "--blob-url",
        default="http://127.0.0.1:11437",
        dest="blob_url",
        help="Base URL of the unified-blob Helix server.",
    )
    ap.add_argument(
        "--sharded-url",
        default="http://127.0.0.1:11438",
        dest="sharded_url",
        help="Base URL of the sharded Helix server.",
    )
    ap.add_argument("--label", default="medium",
                    help="Run label for results filename.")
    ap.add_argument("--max-results", type=int, default=200, dest="max_results",
                    help="max_results for /fingerprint (default 200).")
    ap.add_argument("--blob-rank-cap", type=int, default=10, dest="blob_rank_cap",
                    help="Blob rank threshold for 'blob-good' (default 10).")
    ap.add_argument("--shard-bury-cap", type=int, default=10, dest="shard_bury_cap",
                    help="Shard rank threshold for 'shard-bad' (default 10).")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap needles for smoke-test (0 = all).")
    ap.add_argument("--timeout", type=float, default=45.0,
                    help="Per-request HTTP timeout in seconds (default 45).")
    ap.add_argument("--out", default=None,
                    help="Override output JSON path.")
    ap.add_argument("--verbose", action="store_true",
                    help="Print per-needle detail to stdout.")
    args = ap.parse_args(argv)

    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    needles_path = Path(args.needles)
    if not needles_path.exists():
        print(f"ERROR: needles file not found: {needles_path}", file=sys.stderr)
        print(
            f"  Run: python benchmarks/build_shard_gold.py --out {needles_path}",
            file=sys.stderr,
        )
        return 1

    needles: List[Dict[str, Any]] = []
    with needles_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                needles.append(json.loads(line))

    if args.limit:
        needles = needles[:args.limit]

    print(f"[diag_blob_vs_shard] {len(needles)} needles from {needles_path}")
    print(f"[diag_blob_vs_shard] blob={args.blob_url}  sharded={args.sharded_url}")

    for arm_label, url in [("blob", args.blob_url), ("sharded", args.sharded_url)]:
        try:
            health_url = url.rstrip("/") + "/health"
            with urllib.request.urlopen(health_url, timeout=10) as r:
                health = json.loads(r.read())
            doc_count = health.get("document_count", "?")
            print(f"[diag_blob_vs_shard] {arm_label} OK  docs={doc_count}")
        except Exception as exc:
            print(
                f"ERROR: cannot reach {arm_label} at {url}: {exc}",
                file=sys.stderr,
            )
            return 1

    per_needle, agg = run(
        needles=needles,
        blob_url=args.blob_url,
        sharded_url=args.sharded_url,
        max_results=args.max_results,
        timeout_s=args.timeout,
        blob_rank_cap=args.blob_rank_cap,
        shard_bury_cap=args.shard_bury_cap,
        verbose=args.verbose,
    )

    _print_summary(agg, args.label, args.blob_rank_cap, args.shard_bury_cap)

    out_path = (
        Path(args.out) if args.out
        else RESULTS_DIR / f"diag_blob_vs_shard_{args.label}_{ts}.json"
    )
    result = {
        "benchmark":    "diag_blob_vs_shard_tiers",
        "label":        args.label,
        "timestamp":    ts,
        "blob_url":     args.blob_url,
        "sharded_url":  args.sharded_url,
        "needles_file": str(needles_path),
        "n_needles":    len(needles),
        "max_results":  args.max_results,
        "blob_rank_cap":    args.blob_rank_cap,
        "shard_bury_cap":   args.shard_bury_cap,
        "gold_match_rule": (
            "bidirectional case-insensitive forward-slash substring: "
            "norm(gold) in norm(source) OR norm(source) in norm(gold)"
        ),
        # Document where tier_contributions lives in the wire response
        "fingerprint_tier_field": (
            "fingerprints[*].tier_contributions  "
            "(routes_context.py L817-819, dict {tier_name: float})"
        ),
        "aggregate":  agg,
        "per_needle": per_needle,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"-> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

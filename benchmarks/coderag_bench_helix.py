"""CodeRAG-Bench (Step-2) Helix arm: HTTP fingerprint retriever over a live
helix server (the "helix063" daemon, bench lane :11439).

Reads the per-query dump written by coderag_bench.py, issues one
POST /fingerprint per query, extracts the ranked list of doc_* paths,
resolves them back to corpus indices, and scores NDCG@10 + Recall@{1,5,10}
+ Precision@{1,5,10}. Also computes the efficiency layer (median/p90 injected
tokens from the fingerprint previews, plus per-query latency).

DESIGN NOTES
- The Helix server must have the programming-solutions corpus ingested before
  this script runs. Use coderag_bench.py to build the per-query dump, then
  ingest via 'helix ingest' or the server /ingest endpoint.
- Retrieval via POST /fingerprint with score_floor=0, profile="fast",
  max_results=50 (NDCG@10 saturates well below 50).
- The fingerprint "source" field carries the doc_{idx} path set at ingest.
- LLM-free, GPU-free (lexical profile; dense/splade OFF on bench server).

METRICS
  - NDCG@10 (primary, per CodeRAG-Bench paper)
  - Recall@{1,5,10}
  - Precision@{1,5,10}
  - Efficiency: median/p90 injected-token estimate + median/p90 latency (ms)

WRITES
  benchmarks/results/coderag_helix_{timestamp}.json

CLI
  # Requires a live Helix server at --helix-url with the corpus pre-ingested.
  python benchmarks/coderag_bench_helix.py \
      --queries benchmarks/results/coderag_queries_<ts>.json \
      --helix-url http://127.0.0.1:11439 \
      --limit 50

  # Run all queries (HumanEval + MBPP):
  python benchmarks/coderag_bench_helix.py \
      --queries benchmarks/results/coderag_queries_<ts>.json \
      --helix-url http://127.0.0.1:11439

License: CC-BY-SA-4.0 (internal measurement OK; verify before public claim).
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

_TOK = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

# k values: NDCG@10 primary; recall/precision at 1, 5, 10.
KS = (1, 5, 10)


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def ndcg_at(pos0, k):
    """Single-gold NDCG@k from 0-based rank. IDCG = 1."""
    r = pos0 + 1
    return (1.0 / math.log2(r + 1)) if r <= k else 0.0


def recall_at(pos0, k):
    """1.0 if gold in top-k else 0.0."""
    return 1.0 if (pos0 + 1) <= k else 0.0


def precision_at(pos0, k):
    """Precision@k for a single-gold query."""
    return (1.0 / k) if (pos0 + 1) <= k else 0.0


# ---------------------------------------------------------------------------
# Efficiency helpers
# ---------------------------------------------------------------------------

def _percentile(values, pct):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def efficiency_stats(token_counts, latency_ms):
    return {
        "median_injected_tokens": round(_percentile(token_counts, 50), 1),
        "p90_injected_tokens": round(_percentile(token_counts, 90), 1),
        "median_latency_ms": round(_percentile(latency_ms, 50), 1),
        "p90_latency_ms": round(_percentile(latency_ms, 90), 1),
    }


def preview_token_estimate(previews):
    """Rough token count from fingerprint preview strings."""
    words = sum(len((p or "").split()) for p in previews)
    return round(words * 1.3)


# ---------------------------------------------------------------------------
# Source-id parser: "doc_42" -> 42
# ---------------------------------------------------------------------------

def parse_doc_idx(source):
    """Parse doc_{idx} -> int, or None if pattern does not match."""
    if not source:
        return None
    m = re.match(r"doc_(\d+)$", str(source))
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# /fingerprint HTTP call
# ---------------------------------------------------------------------------

def fingerprint(helix_url, query, max_results=50, timeout_s=30.0):
    """POST /fingerprint and return (fingerprints_list, latency_ms).

    Raises urllib.error.URLError on network failure.
    """
    url = helix_url.rstrip("/") + "/fingerprint"
    payload = json.dumps({
        "query": query,
        "max_results": max_results,
        "score_floor": 0,
        "profile": "fast",
    }).encode()
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
# Core scoring loop
# ---------------------------------------------------------------------------

def score_queries(queries, helix_url, max_results=50, timeout_s=30.0, n_corpus=None):
    """Run /fingerprint for each query; return (summary, per_query_rows).

    Parameters
    ----------
    queries      : list of {"ds", "query", "gold", "gold_idx", ...}
    helix_url    : base URL of the Helix server
    max_results  : /fingerprint max_results
    timeout_s    : per-request HTTP timeout
    n_corpus     : corpus size; used as rank sentinel when gold unretrieved

    Returns
    -------
    summary        : per-dataset NDCG@10 / Recall / Precision + efficiency
    per_query_rows : per-query details for downstream analysis
    """
    def _zero_agg():
        a = {"n": 0, "err": 0, "ndcg10": 0.0, "token_counts": [], "latency_ms": []}
        for k in KS:
            a["recall@{}".format(k)] = 0.0
            a["precision@{}".format(k)] = 0.0
        return a

    agg = collections.defaultdict(_zero_agg)
    per_query_rows = []
    n_total = len(queries)
    fallback_n = n_corpus or n_total

    for qi, q in enumerate(queries):
        ds = q["ds"]
        gi = q["gold_idx"]
        a = agg[ds]

        if (qi + 1) % 50 == 0 or qi == 0:
            print("[helix] {}/{} queries scored...".format(qi + 1, n_total), flush=True)

        try:
            fps, lat_ms = fingerprint(helix_url, q["query"], max_results, timeout_s)
        except Exception as exc:
            a["err"] += 1
            per_query_rows.append({
                "ds": ds, "gold": q["gold"], "gold_idx": gi,
                "helix_rank": None, "error": repr(exc),
            })
            continue

        ranked_idxs = []
        previews = []
        for fp in fps:
            idx = parse_doc_idx(fp.get("source") or fp.get("path"))
            if idx is not None:
                ranked_idxs.append(idx)
                previews.append(fp.get("preview") or "")

        if gi in ranked_idxs:
            pos0 = ranked_idxs.index(gi)
        else:
            pos0 = fallback_n

        a["n"] += 1
        a["ndcg10"] += ndcg_at(pos0, 10)
        for k in KS:
            a["recall@{}".format(k)] += recall_at(pos0, k)
            a["precision@{}".format(k)] += precision_at(pos0, k)
        a["token_counts"].append(float(preview_token_estimate(previews[:10])))
        a["latency_ms"].append(lat_ms)

        per_query_rows.append({
            "ds": ds,
            "gold": q["gold"],
            "gold_idx": gi,
            "helix_rank": pos0,
            "n_retrieved": len(ranked_idxs),
            "latency_ms": round(lat_ms, 1),
        })

    summary = {}
    for ds, a in sorted(agg.items()):
        n = a["n"]
        if n == 0:
            continue
        eff = efficiency_stats(a["token_counts"], a["latency_ms"])
        row = {
            "n": n,
            "err": a["err"],
            "helix_ndcg@10": round(a["ndcg10"] / n, 4),
            "efficiency": eff,
        }
        for k in KS:
            row["helix_recall@{}".format(k)] = round(a["recall@{}".format(k)] / n, 4)
            row["helix_precision@{}".format(k)] = round(a["precision@{}".format(k)] / n, 4)
        summary[ds] = row

    return summary, per_query_rows


# ---------------------------------------------------------------------------
# Pretty-print table
# ---------------------------------------------------------------------------

def _print_table(summary, foils_path=None):
    foils = {}
    if foils_path and Path(foils_path).exists():
        try:
            foils = json.loads(Path(foils_path).read_text(encoding="utf-8")).get("summary", {})
        except Exception:
            pass

    header = (
        "{:<14} {:>5}  {:>9} {:>7} {:>7} {:>7}  {:>8} {:>8}  {:>8}".format(
            "dataset", "n", "ndcg@10", "r@1", "r@5", "r@10",
            "med_tok", "p90_tok", "med_ms"
        )
    )
    print("\nCodeRAG-Bench -- Helix arm (D)")
    print(header)
    print("-" * len(header))
    for ds, s in summary.items():
        eff = s.get("efficiency", {})
        print("{:<14} {:>5}  {:>9.4f} {:>7.4f} {:>7.4f} {:>7.4f}  {:>8.0f} {:>8.0f}  {:>8.1f}".format(
            "helix:" + ds, s["n"],
            s["helix_ndcg@10"], s["helix_recall@1"], s["helix_recall@5"], s["helix_recall@10"],
            eff.get("median_injected_tokens", 0), eff.get("p90_injected_tokens", 0),
            eff.get("median_latency_ms", 0),
        ))
        if ds in foils:
            f = foils[ds]
            feff = f.get("bm25_efficiency", {})
            print("{:<14} {:>5}  {:>9.4f} {:>7.4f} {:>7.4f} {:>7.4f}  {:>8.0f} {:>8.0f}  {:>8}".format(
                "BM25:" + ds, f["n"],
                f.get("bm25_ndcg@10", 0), f.get("bm25_recall@1", 0),
                f.get("bm25_recall@5", 0), f.get("bm25_recall@10", 0),
                feff.get("median_injected_tokens", 0), feff.get("p90_injected_tokens", 0),
                "n/a",
            ))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=(
            "CodeRAG-Bench (Step-2) Helix arm. "
            "Requires a live Helix server with the corpus pre-ingested. "
            "Reads the per-query dump from coderag_bench.py."
        )
    )
    ap.add_argument(
        "--queries", default=None,
        help=(
            "Path to the per-query dump JSON written by coderag_bench.py. "
            "If omitted, uses the most recent coderag_queries_*.json in "
            "benchmarks/results/."
        ),
    )
    ap.add_argument(
        "--helix-url", default="http://127.0.0.1:11439", dest="helix_url",
        help="Base URL of the Helix bench server (default: http://127.0.0.1:11439)",
    )
    ap.add_argument(
        "--max-results", type=int, default=50, dest="max_results",
        help="max_results for /fingerprint (default 50; covers NDCG@10 well)",
    )
    ap.add_argument(
        "--limit", type=int, default=0,
        help="Cap total queries to score (0 = all from dump)",
    )
    ap.add_argument(
        "--timeout", type=float, default=30.0,
        help="Per-request HTTP timeout in seconds (default 30)",
    )
    ap.add_argument(
        "--foils", default=None,
        help="Path to foils summary JSON from coderag_bench.py (enables side-by-side table)",
    )
    ap.add_argument(
        "--out", default=None,
        help="Override output JSON path (default: benchmarks/results/coderag_helix_{ts}.json)",
    )
    args = ap.parse_args()

    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    if args.queries:
        queries_path = Path(args.queries)
    else:
        candidates = sorted(RESULTS_DIR.glob("coderag_queries_*.json"))
        if not candidates:
            print(
                "ERROR: no coderag_queries_*.json in benchmarks/results/.\n"
                "  Run coderag_bench.py first: python benchmarks/coderag_bench.py",
                file=sys.stderr,
            )
            sys.exit(1)
        queries_path = candidates[-1]

    if not queries_path.exists():
        print("ERROR: queries file not found: {}".format(queries_path), file=sys.stderr)
        sys.exit(1)

    print("[helix] Loading queries from {}".format(queries_path), flush=True)
    queries = json.loads(queries_path.read_text(encoding="utf-8"))
    if args.limit:
        queries = queries[:args.limit]
    print("[helix] {} queries to score against {}".format(len(queries), args.helix_url),
          flush=True)

    # Health check.
    try:
        health_url = args.helix_url.rstrip("/") + "/health"
        with urllib.request.urlopen(health_url, timeout=5) as r:
            health = json.loads(r.read())
        print("[helix] server health: {} docs={}".format(
              health.get("status", "ok"), health.get("document_count", "?")), flush=True)
    except Exception as exc:
        print(
            "ERROR: cannot reach Helix server at {}: {}\n"
            "  Start it: python -m uvicorn helix_context._asgi:app --port 11439".format(
                args.helix_url, exc),
            file=sys.stderr,
        )
        sys.exit(1)

    summary, per_query_rows = score_queries(
        queries=queries,
        helix_url=args.helix_url,
        max_results=args.max_results,
        timeout_s=args.timeout,
    )

    _print_table(summary, foils_path=args.foils)

    out_path = Path(args.out) if args.out else RESULTS_DIR / "coderag_helix_{}.json".format(ts)

    result_blob = {
        "benchmark": "coderag_bench",
        "arm": "helix_fingerprint",
        "timestamp": ts,
        "helix_url": args.helix_url,
        "queries_file": str(queries_path),
        "limit": args.limit,
        "max_results": args.max_results,
        "license": "CC-BY-SA-4.0 (internal measurement; verify before public claim)",
        "metrics": "NDCG@10 (primary), Recall@{1,5,10}, Precision@{1,5,10}",
        "efficiency": "median/p90 injected tokens (preview-based) + latency",
        "summary": summary,
        "per_query_sample": per_query_rows[:20],
    }

    out_path.write_text(json.dumps(result_blob, indent=2), encoding="utf-8")
    print("\n-> {}".format(out_path))


if __name__ == "__main__":
    main()

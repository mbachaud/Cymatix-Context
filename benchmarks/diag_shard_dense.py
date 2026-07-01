"""
diag_shard_dense.py — Gold-free retrieval diagnostic: tier-contribution + dense-fired flag.

Usage:
    python benchmarks/diag_shard_dense.py --helix-url http://127.0.0.1:11437 --label unsharded
    python benchmarks/diag_shard_dense.py --helix-url http://127.0.0.1:11438 --label sharded

For each query the script calls:
    POST /fingerprint  {query, max_results:10, score_floor:0, profile:"fast"}
    POST /context/packet {query}

From the responses it extracts per-query contributing tiers from
``fingerprints[*].tier_contributions`` (the primary, per-result field,
present in every /fingerprint response at routes_context.py L817-819) and
from ``agent.tier_totals`` (aggregate, routes_context.py L880).

The dense-tier signal uses the exact set of tier names used by
know_decision._DENSE_TIERS (scoring/know_decision.py L222-228):
    {"dense", "splade", "sema_boost", "sema_cold"}

The canonical "BGE-M3 dense recall fired" check is:
    tier_contributions for ANY result contains key "dense" with value > 0
    (knowledge_store.py L1993/L2021/L2024; shard_router.py L508/L612)

The /context/packet response is used to cross-check via KnowBlock.lexical_dense_agree
(schemas.py L535) which is True only when dense and lexical tiers agree on >= 1 doc.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Dense-tier names (mirrors scoring/know_decision.py _DENSE_TIERS L222-228)
# ---------------------------------------------------------------------------
_DENSE_TIERS: frozenset[str] = frozenset({"dense", "splade", "sema_boost", "sema_cold"})

# The canonical BGE-M3 dense-recall tier key written by knowledge_store.py
# at L1993: tier_contrib[gid]["dense"] = float(cosine)
# and propagated by shard_router.py at L508/L612.
_BGE_DENSE_TIER = "dense"

# ---------------------------------------------------------------------------
# Default query set (~16 queries, tuned to medium/xl CODE projects)
#
# Projects represented in the medium/xl corpus:
#   BookKeeper, CosmicTasha, Education, helix-context, MaxExpressKit,
#   two-brain-audit
#
# "within" queries are natural-language paraphrases of project-specific
# facts with LOW literal token overlap (tests semantic recall).
# "cross" queries span multiple projects (tests cross-project reasoning).
# ---------------------------------------------------------------------------
DEFAULT_QUERIES: list[dict[str, str]] = [
    # -- within: helix-context --
    {
        "id": "w01",
        "type": "within",
        "query": (
            "How does Helix decide which tier of the retrieval budget "
            "to place a response in — the very narrow set versus the "
            "broader wider set?"
        ),
    },
    {
        "id": "w02",
        "type": "within",
        "query": (
            "Where in the pipeline does the system decide whether "
            "it actually found the answer versus needs to escalate?"
        ),
    },
    {
        "id": "w03",
        "type": "within",
        "query": (
            "What mechanism tracks which documents have already been "
            "sent to a session so they are not repeated on subsequent turns?"
        ),
    },
    {
        "id": "w04",
        "type": "within",
        "query": (
            "Which component is responsible for giving each document a "
            "condensed shorter representation to reduce token cost?"
        ),
    },
    {
        "id": "w05",
        "type": "within",
        "query": (
            "How is the freshness of the top-ranked retrieved document "
            "validated against the file it came from?"
        ),
    },
    # -- within: BookKeeper --
    {
        "id": "w06",
        "type": "within",
        "query": (
            "What approach does BookKeeper use to ensure only authorised "
            "users can access particular routes or resources?"
        ),
    },
    {
        "id": "w07",
        "type": "within",
        "query": (
            "How are amounts and financial figures stored internally to "
            "avoid floating-point rounding problems in BookKeeper?"
        ),
    },
    # -- within: CosmicTasha --
    {
        "id": "w08",
        "type": "within",
        "query": (
            "What rendering approach does CosmicTasha use for displaying "
            "celestial objects and trajectories in real time?"
        ),
    },
    # -- within: Education --
    {
        "id": "w09",
        "type": "within",
        "query": (
            "How does the Education project track learner progress through "
            "a module and record completion status?"
        ),
    },
    # -- within: MaxExpressKit --
    {
        "id": "w10",
        "type": "within",
        "query": (
            "What is the middleware chain order in MaxExpressKit and how "
            "are errors from handlers surfaced back to callers?"
        ),
    },
    # -- cross-project --
    {
        "id": "c01",
        "type": "cross",
        "query": (
            "Which of the projects in this corpus store and query data "
            "using a local embedded SQL database?"
        ),
    },
    {
        "id": "c02",
        "type": "cross",
        "query": (
            "Compare how task ordering or job scheduling is handled "
            "across the different projects in this corpus."
        ),
    },
    {
        "id": "c03",
        "type": "cross",
        "query": (
            "Which projects expose a REST or HTTP API and what "
            "authentication scheme do they rely on?"
        ),
    },
    {
        "id": "c04",
        "type": "cross",
        "query": (
            "Across the projects here, which ones write structured "
            "log output and what format or library do they use?"
        ),
    },
    {
        "id": "c05",
        "type": "cross",
        "query": (
            "Identify projects that have a concept of user sessions "
            "or identity and explain how they manage them."
        ),
    },
    {
        "id": "c06",
        "type": "cross",
        "query": (
            "Which projects in the corpus have automated test suites, "
            "and what testing framework is each one using?"
        ),
    },
]


# ---------------------------------------------------------------------------
# Tier extraction helpers
# ---------------------------------------------------------------------------

def _extract_tiers_from_fingerprint(fp_response: dict) -> tuple[list[str], bool]:
    """Return (tiers_fired, dense_fired) from a /fingerprint response.

    Primary signal: fingerprints[*].tier_contributions
        - routes_context.py L817-819: each result row carries
          ``tier_contributions: {tier_name: score}``
        - A tier is "fired" if it appears with a non-zero score in
          at least one result.

    Secondary / aggregate signal: agent.tier_totals
        - routes_context.py L880: sum of all per-result tier scores.

    ``dense_fired`` = True if the "dense" key appears with value > 0
    in ANY result's tier_contributions (canonical BGE-M3 signal).
    """
    all_tiers: set[str] = set()
    dense_fired = False

    fingerprints = fp_response.get("fingerprints") or []
    for fp in fingerprints:
        tc = fp.get("tier_contributions") or {}
        for tier, score in tc.items():
            score_f = float(score) if score is not None else 0.0
            if score_f > 0:
                all_tiers.add(tier)
                if tier == _BGE_DENSE_TIER:
                    dense_fired = True

    # Cross-check with agent.tier_totals (aggregate, covers refiner tiers
    # like "tcm" that may not appear per-fingerprint)
    agent = fp_response.get("agent") or {}
    tier_totals = agent.get("tier_totals") or {}
    for tier, score in tier_totals.items():
        score_f = float(score) if score is not None else 0.0
        if score_f > 0:
            all_tiers.add(tier)
            if tier == _BGE_DENSE_TIER:
                dense_fired = True

    return sorted(all_tiers), dense_fired


def _extract_from_packet(packet_response: dict) -> dict[str, Any]:
    """Extract supplementary signals from a /context/packet response.

    Returns:
        lexical_dense_agree: from KnowBlock.lexical_dense_agree (schemas.py L535)
        know_found: True if a KnowBlock was returned
        confidence: KnowBlock.confidence if present
        top_sources: list of source paths from verified items
    """
    result: dict[str, Any] = {
        "lexical_dense_agree": None,
        "know_found": False,
        "confidence": None,
        "top_sources": [],
    }
    know = packet_response.get("know")
    if know:
        result["know_found"] = bool(know.get("found", False))
        result["confidence"] = know.get("confidence")
        result["lexical_dense_agree"] = know.get("lexical_dense_agree")

    verified = packet_response.get("verified") or []
    for item in verified[:5]:
        src = item.get("source_id") or item.get("title") or ""
        if src:
            result["top_sources"].append(src)

    return result


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(client: httpx.Client, url: str, payload: dict, timeout: float = 30.0) -> dict:
    resp = client.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Core diagnostic runner
# ---------------------------------------------------------------------------

def run_diagnostic(
    helix_url: str,
    queries: list[dict],
    *,
    label: str = "run",
    verbose: bool = False,
) -> dict:
    """Run the diagnostic against a live Helix server.

    Returns the full result dict (per-query rows + aggregate).
    """
    base = helix_url.rstrip("/")
    fp_url = f"{base}/fingerprint"
    packet_url = f"{base}/context/packet"

    rows: list[dict] = []
    errors: list[dict] = []

    with httpx.Client() as client:
        for q in queries:
            qid = q["id"]
            qtext = q["query"]
            qtype = q.get("type", "unknown")

            if verbose:
                print(f"  [{qid}] {qtype}: {qtext[:80]}...", flush=True)

            row: dict[str, Any] = {
                "id": qid,
                "type": qtype,
                "query": qtext,
                "tiers_fired": [],
                "dense_fired": False,
                "lexical_dense_agree": None,
                "know_found": None,
                "confidence": None,
                "top_sources": [],
                "fp_count": 0,
                "fp_error": None,
                "packet_error": None,
            }

            # -- /fingerprint -------------------------------------------------
            try:
                fp_resp = _post(client, fp_url, {
                    "query": qtext,
                    "max_results": 10,
                    "score_floor": 0,
                    "profile": "fast",
                })
                tiers, dense = _extract_tiers_from_fingerprint(fp_resp)
                row["tiers_fired"] = tiers
                row["dense_fired"] = dense
                row["fp_count"] = fp_resp.get("count", 0)
            except Exception as exc:
                row["fp_error"] = str(exc)
                if verbose:
                    print(f"    /fingerprint error: {exc}", flush=True)
                errors.append({"id": qid, "endpoint": "fingerprint", "error": str(exc)})

            # -- /context/packet ----------------------------------------------
            try:
                pkt_resp = _post(client, packet_url, {"query": qtext})
                pkt_info = _extract_from_packet(pkt_resp)
                row.update(pkt_info)
            except Exception as exc:
                row["packet_error"] = str(exc)
                if verbose:
                    print(f"    /context/packet error: {exc}", flush=True)
                errors.append({"id": qid, "endpoint": "packet", "error": str(exc)})

            rows.append(row)

            if verbose:
                dense_marker = "[DENSE]" if row["dense_fired"] else "      "
                tiers_short = ", ".join(row["tiers_fired"][:6])
                print(f"    {dense_marker} tiers={tiers_short}", flush=True)

    # -- Aggregate ------------------------------------------------------------
    n = len(rows)
    n_dense = sum(1 for r in rows if r["dense_fired"])
    pct_dense = round(100.0 * n_dense / n, 1) if n else 0.0

    by_type: dict[str, dict] = {}
    for r in rows:
        t = r["type"]
        if t not in by_type:
            by_type[t] = {"n": 0, "dense_fired": 0}
        by_type[t]["n"] += 1
        if r["dense_fired"]:
            by_type[t]["dense_fired"] += 1
    for t, d in by_type.items():
        d["pct_dense"] = round(100.0 * d["dense_fired"] / d["n"], 1) if d["n"] else 0.0

    # Tier frequency across all queries
    tier_freq: dict[str, int] = {}
    for r in rows:
        for tier in r["tiers_fired"]:
            tier_freq[tier] = tier_freq.get(tier, 0) + 1

    return {
        "label": label,
        "helix_url": base,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "queries": rows,
        "aggregate": {
            "n": n,
            "n_dense_fired": n_dense,
            "pct_dense_fired": pct_dense,
            "by_type": by_type,
            "tier_frequency": dict(sorted(tier_freq.items(), key=lambda kv: -kv[1])),
            "errors": errors,
        },
    }


# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------

def _print_table(result: dict) -> None:
    label = result["label"]
    agg = result["aggregate"]
    rows = result["queries"]

    print(f"\n{'='*72}")
    print(f"  Helix Shard*Dense Diagnostic  |  label={label}")
    print(f"  URL: {result['helix_url']}")
    print(f"  Run: {result['timestamp']}")
    print(f"{'='*72}")

    # Per-query table
    col_w = [5, 6, 58, 7, 8]
    hdr = (
        f"{'ID':<{col_w[0]}} {'TYPE':<{col_w[1]}} "
        f"{'QUERY':<{col_w[2]}} {'DENSE':>{col_w[3]}} {'FP_CNT':>{col_w[4]}}"
    )
    print(f"\n{hdr}")
    print("-" * (sum(col_w) + len(col_w)))
    for r in rows:
        q_short = r["query"][:55] + "..." if len(r["query"]) > 58 else r["query"]
        dense_mark = "YES" if r["dense_fired"] else "no"
        fp_cnt = str(r.get("fp_count", "?"))
        print(
            f"{r['id']:<{col_w[0]}} "
            f"{r['type']:<{col_w[1]}} "
            f"{q_short:<{col_w[2]}} "
            f"{dense_mark:>{col_w[3]}} "
            f"{fp_cnt:>{col_w[4]}}"
        )

    # Tier frequency
    print(f"\n  Tier frequency (n queries with non-zero contribution):")
    tf = agg.get("tier_frequency", {})
    for tier, cnt in sorted(tf.items(), key=lambda kv: -kv[1]):
        bar = "#" * cnt
        marker = " <-- BGE-M3 dense" if tier == _BGE_DENSE_TIER else ""
        print(f"    {tier:<20} {cnt:>3}  {bar}{marker}")

    # Aggregate
    print(f"\n  Aggregate:")
    print(f"    Total queries      : {agg['n']}")
    print(f"    Dense tier fired   : {agg['n_dense_fired']} / {agg['n']}  ({agg['pct_dense_fired']}%)")
    for t, d in agg.get("by_type", {}).items():
        print(f"    by_type [{t}]     : {d['dense_fired']}/{d['n']} dense ({d['pct_dense']}%)")
    if agg.get("errors"):
        print(f"\n  Errors ({len(agg['errors'])}):")
        for e in agg["errors"]:
            print(f"    [{e['id']}] {e['endpoint']}: {e['error']}")
    print(f"{'='*72}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Gold-free retrieval diagnostic: tier-contribution + dense-fired."
    )
    parser.add_argument(
        "--helix-url",
        default="http://127.0.0.1:11437",
        help="Base URL of the Helix server (default: http://127.0.0.1:11437)",
    )
    parser.add_argument(
        "--queries",
        default=None,
        metavar="FILE",
        help=(
            "JSON file with query list. Each entry: "
            "{id, type, query}. Defaults to the embedded 16-query set."
        ),
    )
    parser.add_argument(
        "--label",
        default="run",
        help="Short label embedded in the output filename and summary (e.g. 'unsharded', 'sharded').",
    )
    parser.add_argument(
        "--out",
        default=None,
        help=(
            "Output JSON path. Default: "
            "benchmarks/results/diag_shard_dense_<label>_<ts>.json"
        ),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print per-query progress.",
    )
    args = parser.parse_args(argv)

    # Load queries
    if args.queries:
        with open(args.queries, encoding="utf-8") as f:
            queries = json.load(f)
    else:
        queries = DEFAULT_QUERIES

    # Output path
    if args.out:
        out_path = Path(args.out)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(__file__).parent / "results"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"diag_shard_dense_{args.label}_{ts}.json"

    print(
        f"Running diagnostic: label={args.label!r} url={args.helix_url} "
        f"queries={len(queries)}",
        flush=True,
    )

    result = run_diagnostic(
        args.helix_url,
        queries,
        label=args.label,
        verbose=args.verbose,
    )

    _print_table(result)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"Results written: {out_path}", flush=True)


if __name__ == "__main__":
    main()

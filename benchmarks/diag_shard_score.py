"""diag_shard_score.py -- localize cross-shard gold score depression (#181).

In-process diagnostic for the sharded retrieval path. For each gold needle
it runs the SAME query path the server uses (``open_read_source`` with
``HELIX_USE_SHARDS=1`` -> ``ShardedGenomeAdapter.query_docs`` ->
``ShardRouter.query_genes``), with ``HELIX_SHARD_SCORE_DEBUG=1`` so the
router populates ``ShardRouter.last_score_breakdown`` for EVERY merged
candidate (including golds that fell below the merge truncation).

For each needle it reports:
  - the gold gene(s) (matched to a gold_path by the bidirectional substring
    rule from bench_shard_recall.py) with rank + score breakdown
    {shard, raw, m_shard, doc_type_boost, corrected, rrf}
  - the NON-gold genes ranked ABOVE the gold (the "wrong high-ranking"
    incumbents), with their breakdowns, so it is visible whether the gold's
    ``corrected`` is depressed by a low ``m_shard`` (the IDF clip), a low
    ``raw`` (within-shard scoring), or the doc-type boost.

Aggregate across needles:
  - median gold m_shard
  - fraction of golds whose m_shard hit the clip floor (IDF_CLIP_LO=0.5)
    or ceiling (IDF_CLIP_HI=3.0)
  - median (wrong_top_corrected - gold_corrected) gap

No server, no network, no GPU. The sharded adapter does not load the dense
codec at the adapter level (each shard handles its own lexical scoring), and
this harness passes ``sema_codec=None`` and leaves dense default-off, so the
lexical cross-shard scoring under test is exercised exactly as the server's
default sharded config exercises it.

CLI
---
python benchmarks/diag_shard_score.py \\
    --genome genomes/main/main.genome.db \\
    --needles benchmarks/results/shard_gold_medium.jsonl \\
    --helix-config helix.toml \\
    --limit 8 \\
    --out benchmarks/results/diag_shard_score_medium.json

GOLD-PATH MATCH RULE (same as bench_shard_recall.py)
----------------------------------------------------
A candidate's source_id S matches a gold_path G when normalise(G) is a
substring of normalise(S) OR normalise(S) is a substring of normalise(G),
where normalise(x) = x.replace("\\\\", "/").lower().strip().
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Make the package importable when run from the repo root or benchmarks/.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Path normalisation + gold match (identical to bench_shard_recall.py)
# ---------------------------------------------------------------------------

def _norm(path: Optional[str]) -> str:
    return str(path or "").replace("\\", "/").lower().strip()


def _gold_hit(source: Optional[str], gold_paths: List[str]) -> bool:
    """True if source matches any gold path (bidirectional substring)."""
    sn = _norm(source)
    if not sn:
        return False
    for gp in gold_paths:
        gn = _norm(gp)
        if gn and (gn in sn or sn in gn):
            return True
    return False


# ---------------------------------------------------------------------------
# In-process retrieval wiring
# ---------------------------------------------------------------------------

def _genome_kwargs_from_config(config: Any) -> Dict[str, Any]:
    """Build the retrieval-affecting kwargs the server forwards to each shard.

    Mirrors ``HelixContextManager.__init__``'s ``open_read_source`` call
    (context_manager.py ~530) for the keys that drive scoring/fusion, but
    forces ``sema_codec=None`` and leaves dense default-off so no model is
    loaded (no-GPU constraint). The cross-shard lexical scoring under test
    is unaffected by these two omissions.
    """
    r = config.retrieval
    ing = config.ingestion
    kwargs: Dict[str, Any] = {
        "synonym_map": getattr(config, "synonym_map", None),
        "sema_codec": None,  # no model load
        "splade_enabled": getattr(ing, "splade_enabled", False),
        "entity_graph": getattr(ing, "entity_graph", False),
        "sr_enabled": getattr(r, "sr_enabled", False),
        "sr_gamma": getattr(r, "sr_gamma", 0.85),
        "sr_k_steps": getattr(r, "sr_k_steps", 4),
        "sr_weight": getattr(r, "sr_weight", 1.5),
        "sr_cap": getattr(r, "sr_cap", 3.0),
        "seeded_edges_enabled": getattr(r, "seeded_edges_enabled", False),
        "fusion_mode": getattr(r, "fusion_mode", "additive"),
        "entity_graph_retrieval_enabled": getattr(
            r, "entity_graph_retrieval_enabled", False
        ),
        # dense left default-off (no GPU); the bug is in lexical cross-shard
        # scoring, and the server's default sharded config runs dense off too.
        "dense_embedding_enabled": False,
    }
    # Optional scoring weights / knobs — pass through only if present so a
    # config schema drift doesn't crash the harness.
    for opt in (
        "filename_anchor_enabled", "filename_anchor_weight",
        "bm25_shortlist_enabled", "bm25_shortlist_size",
        "bm25_prefilter_enabled", "bm25_prefilter_size",
        "rrf_k", "fts5_weight", "tag_exact_weight", "tag_prefix_weight",
        "sema_cold_weight", "lex_anchor_weight", "harmonic_weight",
        "entity_graph_weight",
    ):
        if hasattr(r, opt):
            kwargs[opt] = getattr(r, opt)
    return kwargs


def _open_router_source(genome_path: str, config: Any):
    """Open the sharded read source exactly as the server would.

    Returns the ``ShardedGenomeAdapter`` (or a bare ``Genome`` if the path
    isn't a routing DB / sharding disabled — in which case there's nothing
    to diagnose and we say so).
    """
    os.environ["HELIX_USE_SHARDS"] = "1"
    os.environ["HELIX_SHARD_SCORE_DEBUG"] = "1"
    from helix_context.sharding import open_read_source

    kwargs = _genome_kwargs_from_config(config)
    return open_read_source(genome_path=genome_path, **kwargs)


def _router_of(source: Any):
    """Return the underlying ShardRouter from a ShardedGenomeAdapter, or None."""
    return getattr(source, "_router", None)


# ---------------------------------------------------------------------------
# Per-needle diagnosis
# ---------------------------------------------------------------------------

def _run_needle(
    source: Any,
    router: Any,
    question: str,
    gold_paths: List[str],
    max_genes: int,
) -> Dict[str, Any]:
    """Run one query; extract gold rank + score-depression breakdown."""
    from helix_context.accel import extract_query_signals

    domains, entities = extract_query_signals(question)

    genes = source.query_docs(domains, entities, max_genes=max_genes)

    breakdown: Dict[str, dict] = dict(getattr(router, "last_score_breakdown", {}) or {})
    shard_mults: Dict[str, float] = dict(getattr(router, "last_shard_multipliers", {}) or {})

    # Returned ranking (post-merge, post-truncation, post-coactivation).
    ranked = []
    for g in genes:
        ranked.append({
            "gene_id": getattr(g, "gene_id", None),
            "source_id": getattr(g, "source_id", None),
        })

    # Locate gold(s) in the RETURNED ranking (1-based rank; None if not
    # returned). Also keep the gene's full breakdown if present.
    gold_entries: List[Dict[str, Any]] = []
    gold_gene_ids = set()
    for idx, row in enumerate(ranked):
        if _gold_hit(row["source_id"], gold_paths):
            gid = row["gene_id"]
            gold_gene_ids.add(gid)
            bd = breakdown.get(gid, {})
            gold_entries.append({
                "rank": idx + 1,
                "gene_id": gid,
                "source_id": row["source_id"],
                "breakdown": bd,
            })

    # If gold never reached the returned ranking, look for it in the FULL
    # candidate breakdown (it may have been admitted to the pool but cut by
    # the merge truncation) — that's exactly the depression we want to see.
    if not gold_entries:
        for gid, bd in breakdown.items():
            # breakdown rows don't carry source_id; resolve via returned set
            # is impossible here, so we can only flag "in pool but not
            # returned" when the caller already knows the gold gene_id. We
            # surface the whole pool size for context instead.
            pass

    # Non-gold incumbents ranked ABOVE the best gold (the "wrong high-ranking"
    # docs). Use the best (lowest-rank) gold as the cutoff.
    best_gold_rank = min((e["rank"] for e in gold_entries), default=None)
    wrong_above: List[Dict[str, Any]] = []
    if best_gold_rank is not None:
        for idx, row in enumerate(ranked):
            rank = idx + 1
            if rank >= best_gold_rank:
                break
            if row["gene_id"] in gold_gene_ids:
                continue
            wrong_above.append({
                "rank": rank,
                "gene_id": row["gene_id"],
                "source_id": row["source_id"],
                "breakdown": breakdown.get(row["gene_id"], {}),
            })

    return {
        "question": question,
        "gold_paths": gold_paths,
        "n_returned": len(ranked),
        "n_pool": len(breakdown),
        "shard_multipliers": shard_mults,
        "gold": gold_entries,
        "best_gold_rank": best_gold_rank,
        "wrong_above_gold": wrong_above,
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _aggregate(per_needle: List[Dict[str, Any]]) -> Dict[str, Any]:
    from helix_context.shard_router import IDF_CLIP_LO, IDF_CLIP_HI

    gold_ms: List[float] = []
    floor_hits = 0
    ceil_hits = 0
    gold_m_total = 0
    gaps: List[float] = []
    golds_found = 0
    golds_not_returned = 0

    eps = 1e-9
    for nd in per_needle:
        golds = nd.get("gold", [])
        if not golds:
            golds_not_returned += 1
            continue
        golds_found += 1
        # Use the best-ranked gold for the per-needle stats.
        best = min(golds, key=lambda e: e["rank"])
        bd = best.get("breakdown", {})
        m = bd.get("m_shard")
        if isinstance(m, (int, float)):
            gold_ms.append(float(m))
            gold_m_total += 1
            if abs(float(m) - IDF_CLIP_LO) <= eps:
                floor_hits += 1
            if abs(float(m) - IDF_CLIP_HI) <= eps:
                ceil_hits += 1
        gold_corr = bd.get("corrected")
        wrong = nd.get("wrong_above_gold", [])
        if wrong and isinstance(gold_corr, (int, float)):
            # gap = top wrong incumbent's corrected - gold corrected
            wrong_top = wrong[0].get("breakdown", {}).get("corrected")
            if isinstance(wrong_top, (int, float)):
                gaps.append(float(wrong_top) - float(gold_corr))

    def _median(xs: List[float]) -> Optional[float]:
        return statistics.median(xs) if xs else None

    return {
        "n_needles": len(per_needle),
        "golds_found_in_ranking": golds_found,
        "golds_not_returned": golds_not_returned,
        "idf_clip_lo": IDF_CLIP_LO,
        "idf_clip_hi": IDF_CLIP_HI,
        "median_gold_m_shard": _median(gold_ms),
        "frac_gold_m_at_floor": (floor_hits / gold_m_total) if gold_m_total else None,
        "frac_gold_m_at_ceiling": (ceil_hits / gold_m_total) if gold_m_total else None,
        "median_wrong_top_minus_gold_corrected": _median(gaps),
        "n_gaps_measured": len(gaps),
    }


# ---------------------------------------------------------------------------
# Needle loading
# ---------------------------------------------------------------------------

def _load_needles(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            nd = json.loads(line)
            q = nd.get("question") or nd.get("query")
            gp = nd.get("gold_paths") or nd.get("gold_path")
            if isinstance(gp, str):
                gp = [gp]
            if not q or not gp:
                continue
            out.append({"question": q, "gold_paths": gp})
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Localize cross-shard gold score depression (#181). In-process; "
            "no server, no network, no GPU."
        ),
    )
    ap.add_argument("--genome", required=True,
                    help="path to the sharded routing DB (main.genome.db).")
    ap.add_argument("--needles", required=True,
                    help="jsonl with question/gold_paths (build_shard_gold.py output).")
    ap.add_argument("--helix-config", default=None,
                    help="helix.toml path (default: load_config's default search).")
    ap.add_argument("--limit", type=int, default=8,
                    help="max_genes passed to query_docs (default 8).")
    ap.add_argument("--out", default=None,
                    help="write the full JSON report here.")
    ap.add_argument("--max-needles", type=int, default=0,
                    help="cap needles processed (0 = all).")
    args = ap.parse_args(argv)

    from helix_context.config import load_config
    config = load_config(args.helix_config)

    source = _open_router_source(args.genome, config)
    router = _router_of(source)
    if router is None:
        print(
            "ERROR: opened source is not a sharded adapter (no _router). "
            "Either HELIX_USE_SHARDS didn't take or --genome is not a "
            "main.genome.db routing DB. Nothing to diagnose.",
            file=sys.stderr,
        )
        return 2

    needles = _load_needles(args.needles)
    if args.max_needles > 0:
        needles = needles[: args.max_needles]
    if not needles:
        print("ERROR: no needles loaded.", file=sys.stderr)
        return 2

    per_needle: List[Dict[str, Any]] = []
    for nd in needles:
        try:
            res = _run_needle(
                source, router, nd["question"], nd["gold_paths"], args.limit,
            )
        except Exception as exc:  # noqa: BLE001 — diagnostic, keep going
            res = {
                "question": nd["question"],
                "gold_paths": nd["gold_paths"],
                "error": repr(exc),
            }
        per_needle.append(res)

    agg = _aggregate([n for n in per_needle if "error" not in n])

    report = {
        "genome": args.genome,
        "needles_file": args.needles,
        "limit": args.limit,
        "aggregate": agg,
        "per_needle": per_needle,
    }

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"wrote {args.out}")

    # Compact summary to stdout.
    print("\n=== cross-shard gold score-depression summary ===")
    print(f"needles              : {agg['n_needles']}")
    print(f"golds found in rank  : {agg['golds_found_in_ranking']}")
    print(f"golds NOT returned   : {agg['golds_not_returned']}")
    print(f"median gold m_shard  : {agg['median_gold_m_shard']}")
    print(f"clip range           : [{agg['idf_clip_lo']}, {agg['idf_clip_hi']}]")
    print(f"frac gold m @ floor  : {agg['frac_gold_m_at_floor']}")
    print(f"frac gold m @ ceiling: {agg['frac_gold_m_at_ceiling']}")
    print(f"median (wrong_top.corrected - gold.corrected): "
          f"{agg['median_wrong_top_minus_gold_corrected']} "
          f"(n={agg['n_gaps_measured']})")

    # Show the 3 worst-depressed needles (largest positive gap).
    scored = [
        (n, n.get("wrong_above_gold", [None])[0])
        for n in per_needle
        if n.get("gold") and n.get("wrong_above_gold")
    ]

    def _gap(n: Dict[str, Any]) -> float:
        g = n.get("gold")
        w = n.get("wrong_above_gold")
        if not g or not w:
            return float("-inf")
        gc = min(g, key=lambda e: e["rank"])["breakdown"].get("corrected")
        wc = w[0]["breakdown"].get("corrected")
        if not isinstance(gc, (int, float)) or not isinstance(wc, (int, float)):
            return float("-inf")
        return wc - gc

    worst = sorted(per_needle, key=_gap, reverse=True)[:3]
    if worst:
        print("\n-- worst-depressed needles (top wrong incumbent vs gold) --")
        for n in worst:
            if not n.get("gold") or not n.get("wrong_above_gold"):
                continue
            g = min(n["gold"], key=lambda e: e["rank"])
            w = n["wrong_above_gold"][0]
            print(f"\nQ: {n['question'][:90]}")
            print(f"  gold  rank={g['rank']} {g['breakdown']}")
            print(f"  wrong rank={w['rank']} {w['breakdown']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

r"""Offline A/B desk-test for the post-fusion rerank combinator (Issue #255).

In-process, retrieval-only, GPU-free. For each bed x combinator-cell x needle it
runs ``build_context(read_only=True, ignore_delivered=True)`` on the lexical
probe stack, then measures how the rerank combinator perturbs the fused ranking.
The beds are opened READ-ONLY and never mutated.

PRIMARY metric is the DEFECT-1 signature (docs/research/2026-07-08-scoring-invariance-audit.md
§3): the exact inversion count — top-K pairs whose emitted order disagrees with
the pure fused order (from ``last_fused_scores``). The additive combinator (the
shipped default) is the baseline that reproduces DEFECT-1; the alternatives
(fused_tier / eps_band / off) are the candidates that should drive it toward 0
WITHOUT losing gold delivery.

Cells per bed: ``additive``, ``fused_tier@<w>`` (per --tier-weights),
``eps_band@<δ>`` (per --deltas), ``off``.

Usage:
    python benchmarks/ab_rerank_combinator.py \
        --bed-dbs genomes/bench/matrix/xl.db,genomes/bench/matrix/xl_clean.db \
        --combinators additive,fused_tier,eps_band,off \
        --deltas 0.02,0.05,0.10 --tier-weights 1.0 \
        --topk 12 --json-out benchmarks/results/ab_rerank.json

Design record: docs/research/2026-07-09-scoring-combinator-exploration.md.
Template: benchmarks/diag_rrf_tier_breadth.py.

CAVEAT on strict-dominance inversions: ``last_tier_contributions`` carries the
ADDITIVE-mode per-tier magnitudes (capped/weighted), NOT the RRF rank
contributions — so the strict-dominance count is a coarse, capped-tier
diagnostic, not the authoritative signal. The exact-inversion count (built from
``last_fused_scores``) is the one to trust.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "benchmarks"))

import bench_needle  # noqa: E402
from helix_context.config import load_config  # noqa: E402
from helix_context.context_manager import HelixContextManager  # noqa: E402
from helix_context.retrieval.rerank_combinators import combine_rerank  # noqa: E402

# The four post-fusion rerank classes — excluded from the fused-tier vector
# used by the strict-dominance diagnostic.
_RERANK_CLASSES = frozenset(
    {"authority", "sema_boost", "party_attr", "access_rate"}
)


# ── cell model ────────────────────────────────────────────────────────
@dataclass
class Cell:
    name: str
    combinator: str
    tier_weight: float = 1.0
    delta: float = 0.05


def _build_cells(combinators: List[str], deltas: List[float],
                 tier_weights: List[float]) -> List[Cell]:
    cells: List[Cell] = []
    for c in combinators:
        if c == "fused_tier":
            for w in tier_weights:
                cells.append(Cell(f"fused_tier@{w:g}", "fused_tier", tier_weight=w))
        elif c == "eps_band":
            for d in deltas:
                cells.append(Cell(f"eps_band@{d:g}", "eps_band", delta=d))
        else:  # additive / off — no parameter axis
            cells.append(Cell(c, c))
    return cells


# ── gold gene-id ground truth (diag_rrf_tier_breadth approach) ────────
def _answer_genes(conn, needle) -> set:
    gs = needle.get("gold_source", [])
    accept = [a.lower() for a in needle.get("accept", [])]
    out: set = set()
    for src in (gs if isinstance(gs, list) else [gs]):
        for gid, content in conn.execute(
            "SELECT gene_id, content FROM genes WHERE source_id LIKE ?",
            ('%' + src.split('/')[-1],),
        ):
            if any(a in (content or "").lower() for a in accept):
                out.add(gid)
    return out


# ── emitted ordering (faithful to the store's ranked_ids) ─────────────
def _emitted_order(genome, cell: Cell) -> List[str]:
    """Reconstruct the store's emitted ordering from the debug hooks.

    For additive / fused_tier / off, ``last_query_scores`` IS the final score
    (fused + contribution), so sorting it by ``(-score, gene_id)`` — the Fuser
    tie-break — reproduces ranked_ids exactly. eps_band leaves
    ``last_query_scores`` at pure fused, so the band walk must be replayed via
    ``combine_rerank`` over the (fused, rerank) debug hooks.
    """
    final = dict(genome.last_query_scores or {})
    if cell.combinator == "eps_band":
        fused = dict(genome.last_fused_scores or {})
        rr = dict(genome.last_rerank_additive or {})
        _f, order = combine_rerank(
            "eps_band", fused, rr, {},
            getattr(genome, "_rrf_k", 60), cell.tier_weight, cell.delta, 0,
        )
        return order
    return sorted(final, key=lambda g: (-final[g], g))


# ── inversion metrics ─────────────────────────────────────────────────
def _exact_inversions(topk_ids: List[str], fused: Dict[str, float]) -> int:
    """Top-K unordered pairs with DIFFERENT fused scores whose emitted order
    disagrees with fused order (-fused, gid). The DEFECT-1 signature."""
    inv = 0
    n = len(topk_ids)
    for i in range(n):
        a = topk_ids[i]
        fa = fused.get(a, 0.0)
        for j in range(i + 1, n):
            b = topk_ids[j]
            fb = fused.get(b, 0.0)
            if fa == fb:
                continue
            # a is emitted before b; fused order wants the higher-fused first.
            if fb > fa:
                inv += 1
    return inv


def _gold_inversions(emitted: List[str], topk_ids: List[str],
                     fused: Dict[str, float], gold: set) -> tuple:
    """(best_gold_rank_1based | None, count of non-gold docs ranked above the
    best gold within top-K despite STRICTLY lower fused score)."""
    gold_in_order = [g for g in emitted if g in gold]
    if not gold_in_order:
        return None, 0
    best_gold = gold_in_order[0]
    best_rank = emitted.index(best_gold) + 1  # 1-based, full emitted order
    bg_fused = fused.get(best_gold, 0.0)
    gold_inv = 0
    for g in topk_ids:
        if g == best_gold:
            break
        if g not in gold and fused.get(g, 0.0) < bg_fused:
            gold_inv += 1
    return best_rank, gold_inv


def _fused_tier_vec(contribs: Dict[str, float]) -> Dict[str, float]:
    return {t: v for t, v in contribs.items() if t not in _RERANK_CLASSES}


def _strict_dom_inversions(topk_ids: List[str],
                           tiers: Dict[str, Dict[str, float]]) -> int:
    """Count top-K pairs where the LOWER-ranked doc strictly dominates the
    higher-ranked one on the fused-class tiers (>= on all fired tiers, > on
    at least one). Capped-tier diagnostic — see module caveat."""
    def dominates(va: Dict[str, float], vb: Dict[str, float]) -> bool:
        keys = set(va) | set(vb)
        ge = all(va.get(t, 0.0) >= vb.get(t, 0.0) for t in keys)
        gt = any(va.get(t, 0.0) > vb.get(t, 0.0) for t in keys)
        return ge and gt

    inv = 0
    n = len(topk_ids)
    for i in range(n):
        va = _fused_tier_vec(tiers.get(topk_ids[i], {}))
        for j in range(i + 1, n):
            vb = _fused_tier_vec(tiers.get(topk_ids[j], {}))
            if dominates(vb, va):  # lower-ranked (j) dominates higher (i)
                inv += 1
    return inv


# ── per-needle probe ──────────────────────────────────────────────────
def _probe_needle(mgr, conn, needle, cell: Cell, topk: int) -> dict:
    gold = _answer_genes(conn, needle)
    win = mgr.build_context(needle["query"], read_only=True,
                            ignore_delivered=True)
    genome = mgr.genome
    fused = dict(genome.last_fused_scores or {})
    tiers = getattr(genome, "last_tier_contributions", {}) or {}

    emitted = _emitted_order(genome, cell)
    topk_ids = emitted[:topk]

    expressed = list(win.expressed_gene_ids or [])
    assembled = win.expressed_context or ""

    delivery = bench_needle.check_gold_delivery(
        assembled, needle.get("gold_source", []), needle.get("accept", []),
    )
    gold_delivered_id = any(g in set(expressed) for g in gold)

    best_gold_rank, gold_inv = _gold_inversions(emitted, topk_ids, fused, gold)

    return {
        "gold_answer_genes": len(gold),
        "pool": len(fused),
        "n_expressed": len(expressed),
        "gold_delivered_text": bool(delivery["gold_delivered"]),
        "content_has_answer": bool(delivery["content_has_answer"]),
        "gold_delivered_id": bool(gold_delivered_id),
        "best_gold_rank": best_gold_rank,
        "exact_inversions": _exact_inversions(topk_ids, fused),
        "gold_inversions": gold_inv,
        "strict_dom_inversions": _strict_dom_inversions(topk_ids, tiers),
    }


# ── aggregation ───────────────────────────────────────────────────────
def _mean(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _aggregate(per_needle: Dict[str, dict],
               additive_ranks: Dict[str, Optional[int]],
               off_ranks: Dict[str, Optional[int]]) -> dict:
    n = len(per_needle)
    disp_add, disp_off = [], []
    for name, m in per_needle.items():
        r = m["best_gold_rank"]
        ra, ro = additive_ranks.get(name), off_ranks.get(name)
        if r is not None and ra is not None:
            disp_add.append(r - ra)
        if r is not None and ro is not None:
            disp_off.append(r - ro)
    exact = [m["exact_inversions"] for m in per_needle.values()]
    return {
        "n_needles": n,
        "total_exact_inversions": sum(exact),
        "mean_exact_inversions": _mean(exact),
        "total_gold_inversions": sum(m["gold_inversions"] for m in per_needle.values()),
        "total_strict_dom_inversions": sum(m["strict_dom_inversions"] for m in per_needle.values()),
        "gold_delivered_text_rate": _mean([1.0 if m["gold_delivered_text"] else 0.0 for m in per_needle.values()]),
        "gold_delivered_id_rate": _mean([1.0 if m["gold_delivered_id"] else 0.0 for m in per_needle.values()]),
        "content_has_answer_rate": _mean([1.0 if m["content_has_answer"] else 0.0 for m in per_needle.values()]),
        "mean_best_gold_rank": _mean([m["best_gold_rank"] for m in per_needle.values()]),
        "mean_displacement_vs_additive": _mean(disp_add),
        "mean_displacement_vs_off": _mean(disp_off),
    }


def _fmt(x: Optional[float], nd: int = 2) -> str:
    return "  n/a" if x is None else f"{x:.{nd}f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bed-dbs",
                    default="genomes/bench/matrix/xl.db,genomes/bench/matrix/xl_clean.db",
                    help="comma-separated bed DB paths (opened read-only)")
    ap.add_argument("--base-config",
                    default="docs/benchmarks/helix_probe_lexical.toml")
    ap.add_argument("--combinators", default="additive,fused_tier,eps_band,off")
    ap.add_argument("--deltas", default="0.02,0.05,0.10")
    ap.add_argument("--tier-weights", default="1.0")
    ap.add_argument("--topk", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0,
                    help="probe only the first N needles (0 = all)")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    beds = [b.strip() for b in args.bed_dbs.split(",") if b.strip()]
    combinators = [c.strip() for c in args.combinators.split(",") if c.strip()]
    deltas = [float(x) for x in args.deltas.split(",") if x.strip()]
    tier_weights = [float(x) for x in args.tier_weights.split(",") if x.strip()]
    cells = _build_cells(combinators, deltas, tier_weights)

    needles = bench_needle.NEEDLES
    if args.limit > 0:
        needles = needles[: args.limit]

    report: dict = {"config": {
        "base_config": args.base_config, "topk": args.topk,
        "n_needles": len(needles),
        "cells": [c.name for c in cells],
    }, "beds": {}}

    for bed in beds:
        bed_path = str(Path(bed).resolve())
        print(f"\n{'=' * 72}\nBED: {bed_path}\n{'=' * 72}")
        conn = sqlite3.connect(f"file:{bed_path}?mode=ro", uri=True)

        # First pass: run every cell, collect per-needle metrics.
        cell_needle: Dict[str, Dict[str, dict]] = {}
        for cell in cells:
            cfg = load_config(args.base_config)
            cfg.genome.path = bed_path
            cfg.retrieval.fusion_mode = "rrf"  # combinator only lives under RRF
            cfg.retrieval.rerank_combinator = cell.combinator
            cfg.retrieval.rerank_band_delta = cell.delta
            cfg.retrieval.rerank_tier_weight = cell.tier_weight
            mgr = HelixContextManager(cfg)
            per_needle: Dict[str, dict] = {}
            for nd in needles:
                try:
                    per_needle[nd["name"]] = _probe_needle(mgr, conn, nd, cell, args.topk)
                except Exception as exc:  # keep the sweep alive on a bad needle
                    print(f"  [warn] {cell.name} / {nd['name']}: {exc}")
                    per_needle[nd["name"]] = {
                        "gold_answer_genes": 0, "pool": 0, "n_expressed": 0,
                        "gold_delivered_text": False, "content_has_answer": False,
                        "gold_delivered_id": False, "best_gold_rank": None,
                        "exact_inversions": 0, "gold_inversions": 0,
                        "strict_dom_inversions": 0,
                    }
            cell_needle[cell.name] = per_needle
            mgr.close()

        additive_ranks = {k: v["best_gold_rank"]
                          for k, v in cell_needle.get("additive", {}).items()}
        off_ranks = {k: v["best_gold_rank"]
                     for k, v in cell_needle.get("off", {}).items()}

        bed_report = {}
        for cell in cells:
            agg = _aggregate(cell_needle[cell.name], additive_ranks, off_ranks)
            bed_report[cell.name] = {
                "combinator": cell.combinator,
                "tier_weight": cell.tier_weight,
                "delta": cell.delta,
                "agg": agg,
                "needles": cell_needle[cell.name],
            }
        report["beds"][bed_path] = bed_report
        conn.close()

        # Human-readable per-cell table.
        print(f"\n{'cell':<16}{'exactInv':>10}{'goldInv':>9}{'strictInv':>10}"
              f"{'gd_text':>9}{'gd_id':>8}{'ans':>7}{'mRank':>8}{'dAdd':>7}{'dOff':>7}")
        print("-" * 92)
        for cell in cells:
            a = bed_report[cell.name]["agg"]
            print(f"{cell.name:<16}"
                  f"{a['total_exact_inversions']:>10}"
                  f"{a['total_gold_inversions']:>9}"
                  f"{a['total_strict_dom_inversions']:>10}"
                  f"{_fmt(a['gold_delivered_text_rate']):>9}"
                  f"{_fmt(a['gold_delivered_id_rate']):>8}"
                  f"{_fmt(a['content_has_answer_rate']):>7}"
                  f"{_fmt(a['mean_best_gold_rank'], 1):>8}"
                  f"{_fmt(a['mean_displacement_vs_additive'], 1):>7}"
                  f"{_fmt(a['mean_displacement_vs_off'], 1):>7}")
        print("\n(strictInv is a capped-tier diagnostic; trust exactInv — see "
              "module docstring.)")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nJSON written: {out.resolve()}")


if __name__ == "__main__":
    main()

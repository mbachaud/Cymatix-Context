r"""Diagnose the xl_clean gold-block deficit under RRF (tier-breadth bias).

SIKE Run-2 (docs/benchmarks/2026-07-06-rrf-default-rebaseline.md) found
RRF wins answerability on xl_clean (+6-8pp content_has_answer) but
delivers FEWER gold blocks than additive (0.46-0.48 vs 0.50-0.54). This
probe explains the mechanism, in-process, on the lexical probe stack
(matching the sweep that measured the deficit).

For each target needle it runs build_context(read_only) under additive
and rrf, finds the answer-bearing gold chunk (a gold_source gene whose
content holds the accept string), and reports its rank in
last_query_scores plus the tier-breadth of the documents that outrank
it. The finding: RRF's reciprocal-rank-over-tiers rewards tier BREADTH,
so a gold chunk that is rank-1 in the literal-match tiers (fts5 /
tag_exact) but fires in fewer tiers is demoted below broad-but-answerless
documents that fire in more tiers.

Usage:
    python benchmarks/diag_rrf_tier_breadth.py \
        --bed-db genomes/bench/matrix/xl_clean.db \
        --needles biged_ram_ceiling,biged_smoke_tests,cosmictasha_postgres_version
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "benchmarks"))

import bench_needle  # noqa: E402
from cymatix_context.config import load_config  # noqa: E402
from cymatix_context.context_manager import HelixContextManager  # noqa: E402


def _answer_genes(conn, needle):
    gs = needle.get("gold_source", [])
    accept = [a.lower() for a in needle.get("accept", [])]
    out = set()
    for src in (gs if isinstance(gs, list) else [gs]):
        for gid, content in conn.execute(
            "SELECT gene_id, content FROM genes WHERE source_id LIKE ?",
            ('%' + src.split('/')[-1],),
        ):
            if any(a in (content or "").lower() for a in accept):
                out.add(gid)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bed-db", default="genomes/bench/matrix/xl_clean.db")
    ap.add_argument("--base-config",
                    default="docs/benchmarks/helix_probe_lexical.toml")
    ap.add_argument("--needles",
                    default="biged_ram_ceiling,biged_smoke_tests,"
                            "cosmictasha_postgres_version")
    ap.add_argument("--top", type=int, default=6)
    args = ap.parse_args()

    targets = args.needles.split(",")
    by_name = {n["name"]: n for n in bench_needle.NEEDLES}
    conn = sqlite3.connect(f"file:{Path(args.bed_db).resolve()}?mode=ro", uri=True)

    for mode in ("additive", "rrf"):
        cfg = load_config(args.base_config)
        cfg.genome.path = str(Path(args.bed_db).resolve())
        cfg.retrieval.fusion_mode = mode
        mgr = HelixContextManager(cfg)
        print(f"\n===== fusion_mode = {mode} =====")
        for t in targets:
            nd = by_name[t]
            gold = _answer_genes(conn, nd)
            win = mgr.build_context(nd["query"], read_only=True,
                                    ignore_delivered=True)
            scores = mgr.genome.last_query_scores or {}
            tiers = getattr(mgr.genome, "last_tier_contributions", {}) or {}
            ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            order = [g for g, _ in ranked]
            gold_ranks = sorted(order.index(g) + 1 for g in gold if g in order)
            delivered = set(win.expressed_gene_ids or [])
            gtiers = sorted({tn for g in gold if g in tiers for tn in tiers[g]})
            print(f"  {t}: answer_genes={len(gold)} pool={len(order)} "
                  f"best_gold_rank={gold_ranks[0] if gold_ranks else None} "
                  f"gold_delivered={sum(g in delivered for g in gold)} "
                  f"gold_tiers={len(gtiers)}{gtiers}")
            for gid, sc in ranked[:args.top]:
                nt = len(tiers.get(gid, {}))
                mark = "*GOLD" if gid in gold else "     "
                print(f"      {mark} score={sc:.3f} tiers={nt}")
        mgr.close()
    conn.close()


if __name__ == "__main__":
    main()

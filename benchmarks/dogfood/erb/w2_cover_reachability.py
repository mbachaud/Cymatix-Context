"""Wave-2 pre-flight falsifier 1: COVER-edge reachability of semantic gold.

The wave-2 skeptic review (docs/research/2026-08-28-wave2-semantic-ranking-
graph-research.md) gates the COVER-walk rescue lane (Lane 1) on a zero-serve
read-only question: for the semantic needles whose gold is NOT delivered
under the graduated combo config, is the gold within 1-2 hops of the
retrieval head in the ingest-built `gene_relations` COVER graph
(relation=5, entity-overlap kNN, storage/co_activation.py:29-63)?

Kill rule (pre-registered by the skeptic): if fewer than ~30% of the
STATIC cohort's gold docs are 2-hop-reachable from the head, the lane's
ceiling collapses to the stalled cohort (~+0.16 semantic r@12 max) and it
must beat the cheaper entity-overlap lane on cost to proceed.

Method notes (honest limits):
- Seeds are a PROXY: raw FTS5 bm25 OR-queries over the needle's content
  words, top-50 (and the top-12 slice). At shipped defaults the scored map
  is exactly the bm25-shortlist survivor set (wave-1 code fact 1), so the
  bm25 top-50 is a superset of any fused head a walk would seed from; the
  production Stage-1 keyword extraction differs from this tokenizer, so
  per-needle seed sets are approximate. Reachability rates are therefore an
  upper-bound-flavored estimate for top-50 seeding and indicative for
  top-12.
- BFS is bidirectional over the COVER edge set (the walk lane would read
  both directions), with a hub guard: nodes whose total degree exceeds
  --degree-cap (default 1000) are never EXPANDED (they may still be
  reached). This mirrors the constrained-propagation condition the skeptic
  imposed (Salton & Buckley 1988 / Crestani; seeded_edges bucket-cap
  precedent).
- Read-only direct SQL: no manager-mediated bed access, no epi/access_rate
  writes — cold-bed rule untouched.

Usage:
  python benchmarks/dogfood/erb/w2_cover_reachability.py \
    --genome F:/tmp/erb_blob_v09x.db \
    --receipt benchmarks/dogfood/erb/receipts/ladder_v09x_w1c_k20_eps_all_full_2026-08-28.json \
    --gold benchmarks/dogfood/erb/gold_by_needle_v09x.json \
    --needles benchmarks/dogfood/erb/needles_resolved_v09x_full.json \
    --out benchmarks/dogfood/erb/receipts/w2_cover_reachability_preflight_2026-08-28.json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

STOP = frozenset(
    "the a an and or for of to in on at by with from into over under is are was "
    "were be been being do does did done what when where which who whom whose why "
    "how that this these those there their they them then than it its as if but "
    "not no nor so such via per about after before during between across each "
    "any all some most more less many much new old only own same other another "
    "out up down off again further once here very can will just should now "
    "through both few too our your his her had has have having get got".split()
)

TOKEN = re.compile(r"[a-z0-9]+")


def terms_of(query: str) -> list[str]:
    seen: list[str] = []
    for t in TOKEN.findall(query.lower()):
        if len(t) > 2 and t not in STOP and t not in seen:
            seen.append(t)
    return seen


def fts_top(conn: sqlite3.Connection, terms: list[str], limit: int) -> list[str]:
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms)
    rows = conn.execute(
        "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH ? ORDER BY rank LIMIT ?",
        (match, limit),
    ).fetchall()
    return [r[0] for r in rows]


def git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=Path(__file__).resolve().parents[3], timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip() or None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--genome", required=True)
    ap.add_argument("--receipt", required=True,
                    help="w1c combo receipt (per_query cohorts + pairing reference)")
    ap.add_argument("--gold", required=True)
    ap.add_argument("--needles", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--degree-cap", type=int, default=1000)
    ap.add_argument("--max-hops", type=int, default=2)
    args = ap.parse_args()

    t0 = time.time()
    receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    per_query = receipt["arms"][0]["per_query"]
    gold_by_needle = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    needles = {n["name"]: n for n in
               json.loads(Path(args.needles).read_text(encoding="utf-8"))["needles"]}

    # Cohorts recomputed from the committed receipt (no hardcoded lists):
    # semantic + not delivered, split stalled (in map, rank>12) vs static
    # (absent from map). Cat (b) (rank<=12, not delivered) is a budget
    # problem, tracked separately for completeness.
    cohorts: dict[str, list[str]] = {"stalled": [], "static": [], "budget_cut": []}
    for rec in per_query:
        name = rec["needle"]
        if needles.get(name, {}).get("question_type") != "semantic":
            continue
        if rec.get("delivered_gold"):
            continue
        rank = rec.get("rank_of_first_gold")
        if rank is None:
            cohorts["static"].append(name)
        elif rank > 12:
            cohorts["stalled"].append(name)
        else:
            cohorts["budget_cut"].append(name)

    conn = sqlite3.connect(f"file:{Path(args.genome).as_posix()}?mode=ro", uri=True)
    try:
        edge_n = conn.execute(
            "SELECT COUNT(*) FROM gene_relations WHERE relation=5").fetchone()[0]

        # Load the COVER graph once, int-mapped, bidirectional.
        idx: dict[str, int] = {}
        adj: defaultdict[int, list[int]] = defaultdict(list)

        def iid(g: str) -> int:
            v = idx.get(g)
            if v is None:
                v = idx[g] = len(idx)
            return v

        cur = conn.execute(
            "SELECT gene_id_a, gene_id_b FROM gene_relations WHERE relation=5")
        while True:
            rows = cur.fetchmany(200_000)
            if not rows:
                break
            for a, b in rows:
                ia, ib = iid(a), iid(b)
                adj[ia].append(ib)
                adj[ib].append(ia)
        load_s = round(time.time() - t0, 1)

        cap = args.degree_cap
        results = []
        for cohort, names in cohorts.items():
            for name in names:
                gold = set(gold_by_needle.get(name) or [])
                gold_ids = {idx[g] for g in gold if g in idx}
                q = needles[name]["query"]
                seeds50 = fts_top(conn, terms_of(q), 50)
                seed_sets = {"top12": seeds50[:12], "top50": seeds50}
                row = {
                    "needle": name, "cohort": cohort,
                    "gold": sorted(gold),
                    "gold_in_graph": len(gold_ids),
                    "gold_degree": sorted(len(adj.get(idx[g], []))
                                          for g in gold if g in idx),
                    "n_seeds": len(seeds50),
                    "gold_in_seeds_top50": bool(gold & set(seeds50)),
                }
                for label, seeds in seed_sets.items():
                    frontier = {idx[s] for s in seeds if s in idx}
                    visited = set(frontier)
                    hop_found = 0 if (gold_ids & frontier) else None
                    hop_sizes = []
                    for hop in range(1, args.max_hops + 1):
                        nxt: set[int] = set()
                        for node in frontier:
                            neigh = adj.get(node, [])
                            if len(neigh) > cap:
                                continue  # hub guard: never expand hubs
                            nxt.update(neigh)
                        nxt -= visited
                        if hop_found is None and (gold_ids & nxt):
                            hop_found = hop
                        visited |= nxt
                        frontier = nxt
                        hop_sizes.append(len(nxt))
                    row[f"reach_{label}"] = hop_found
                    # Discrimination denominator: reachable is meaningless if
                    # the frontier is the whole corpus — record how many
                    # candidates each hop adds.
                    row[f"frontier_{label}"] = hop_sizes
                results.append(row)

        def agg(cohort: str, label: str) -> dict:
            rows = [r for r in results if r["cohort"] == cohort]
            n = len(rows)
            h1 = sum(1 for r in rows if r[f"reach_{label}"] in (0, 1))
            h2 = sum(1 for r in rows if r[f"reach_{label}"] is not None)
            med = lambda xs: (sorted(xs)[len(xs) // 2] if xs else None)
            return {"n": n, "reach_le_1hop": h1, "reach_le_2hop": h2,
                    "rate_2hop": round(h2 / n, 4) if n else None,
                    "median_frontier_1hop": med(
                        [r[f"frontier_{label}"][0] for r in rows
                         if r.get(f"frontier_{label}")]),
                    "median_frontier_2hop": med(
                        [sum(r[f"frontier_{label}"]) for r in rows
                         if r.get(f"frontier_{label}")])}

        payload = {
            "tool": "benchmarks/dogfood/erb/w2_cover_reachability.py",
            "stamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "git_sha": git_sha(),
            "bed": str(args.genome),
            "bed_bytes": Path(args.genome).stat().st_size,
            "cover_edges_rel5": edge_n,
            "graph_nodes_seen": len(idx),
            "graph_load_s": load_s,
            "degree_cap": cap,
            "max_hops": args.max_hops,
            "cohort_source": str(args.receipt),
            "cohort_sizes": {k: len(v) for k, v in cohorts.items()},
            "method_note": (
                "Seeds are PROXY heads: raw FTS5 bm25 OR-queries over the "
                "needle's content words (stopword-filtered, len>2), top-50 and "
                "its top-12 slice. At shipped defaults the scored map is the "
                "bm25-shortlist survivor set, so top-50 is a superset of any "
                "fused head; production Stage-1 keyword extraction differs from "
                "this tokenizer. BFS bidirectional over gene_relations "
                "relation=5; nodes with degree > degree_cap are reached but "
                "never expanded. reach_* = hop at which any gold gene entered "
                "the frontier (0 = already in seeds), null = not within "
                "max_hops. Read-only direct SQL; no manager-mediated access."),
            "aggregates": {
                c: {label: agg(c, label) for label in ("top12", "top50")}
                for c in cohorts
            },
            "per_needle": results,
        }
    finally:
        conn.close()

    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: payload[k] for k in
                      ("cohort_sizes", "aggregates", "cover_edges_rel5",
                       "graph_load_s")}, indent=1))
    print(f"wrote {args.out} in {round(time.time() - t0, 1)}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

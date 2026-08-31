"""Receipt: entity_autolink_hub_cutoff A/B on a live bed (read-only).

Three parts, all read-only against an existing genome DB:

1. **Hub-cardinality probe** — the posting-count distribution of
   ``entity_graph`` (top hubs, share of postings above candidate
   cutoffs). Justifies that a posting-count cutoff removes a tiny
   fraction of distinct entities but the dominant share of swept rows.

2. **A/B edge replay** — for a seeded sample of genes, run the exact
   ``auto_link_by_entity`` SELECT (arm A, legacy) and the cutoff-
   filtered variant (arm B, per cutoff) and diff the relation=5 COVER
   edge sets they would form (peer, shared, confidence). No writes —
   the linker's INSERT is deterministic from the SELECT rows.

3. **Perf receipt** — wall ms per link call for both arms (arm A run
   twice so a warm-cache number is reported alongside the first run;
   arm B includes its LIMIT-bounded count probes).

Caveats recorded in the output: the replay reads each gene's probe set
back from ``entity_graph`` (deduplicated lowercase, insertion order
lost — order does not affect the SELECT or the confidence denominator,
dedup only affects genes that carried case-duplicate entities), and it
replays against the FINAL bed state, whereas real edges form against
the state at each gene's insert time. Both arms replay identically, so
the A/B delta is like-for-like. ORDER BY shared DESC LIMIT 10 tie
order is unspecified in SQLite — edge sets are compared as sets, and
real ingest is already order-nondeterministic under --parallel
(benchmarks/dogfood/receipts/ingest_equivalence_enronqa.json).

Usage:
    python scripts/receipt_entity_hub_cutoff.py \
        --db F:/tmp/enronqa_padded_bed/enronqa_padded.db \
        --out benchmarks/dogfood/receipts/entity_hub_cutoff_ab_enronqa_2026-08-30.json
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sqlite3
import statistics
import subprocess
import time
from pathlib import Path

log = logging.getLogger("receipt_entity_hub_cutoff")

TOPK = 10          # the linker's LIMIT
MIN_SHARED = 2     # the linker's HAVING shared >= 2


def _open_ro(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _git_describe(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        log.warning("git rev-parse failed", exc_info=True)
        return "unknown"


# ── part 1: hub-cardinality probe ────────────────────────────────────

def hub_probe(cur: sqlite3.Cursor, thresholds: list[int]) -> dict:
    t0 = time.perf_counter()
    counts = cur.execute(
        "SELECT entity, COUNT(*) AS n FROM entity_graph GROUP BY entity"
    ).fetchall()
    scan_s = time.perf_counter() - t0

    n_by_entity = sorted((r["n"] for r in counts), reverse=True)
    total_postings = sum(n_by_entity)
    total_entities = len(n_by_entity)
    top = sorted(counts, key=lambda r: r["n"], reverse=True)[:20]

    ladder = {}
    for th in thresholds:
        above = [n for n in n_by_entity if n > th]
        ladder[str(th)] = {
            "entities_above": len(above),
            "entities_above_pct": round(100.0 * len(above) / total_entities, 4),
            "postings_held": sum(above),
            "postings_held_pct": round(100.0 * sum(above) / total_postings, 2),
        }

    return {
        "scan_wall_s": round(scan_s, 2),
        "distinct_entities": total_entities,
        "total_postings": total_postings,
        "median_postings_per_entity": statistics.median(n_by_entity),
        "top20": {r["entity"]: r["n"] for r in top},
        "cutoff_ladder": ladder,
    }


# ── part 2+3: A/B edge replay + perf ─────────────────────────────────

def _link_select(cur, gene_id: str, entities: list[str]):
    ph = ",".join("?" * len(entities))
    return cur.execute(
        f"SELECT gene_id, COUNT(*) as shared "
        f"FROM entity_graph "
        f"WHERE entity IN ({ph}) AND gene_id != ? "
        f"GROUP BY gene_id "
        f"HAVING shared >= {MIN_SHARED} "
        f"ORDER BY shared DESC "
        f"LIMIT {TOPK}",
        entities + [gene_id],
    ).fetchall()


def _edges(rows, denom: int) -> dict:
    return {
        r["gene_id"]: (r["shared"], round(min(r["shared"] / denom, 1.0), 6))
        for r in rows
    }


def _filter_hubs(cur, entities: list[str], cutoff: int) -> list[str]:
    kept = []
    for e in entities:
        n = cur.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT 1 FROM entity_graph WHERE entity = ? LIMIT ?)",
            (e, cutoff + 1),
        ).fetchone()[0]
        if n <= cutoff:
            kept.append(e)
    return kept


def ab_replay(cur, sample_ids: list[str], cutoffs: list[int]) -> dict:
    per_cutoff = {
        c: {
            "edges_a": 0, "edges_b": 0, "lost": 0, "gained": 0,
            "kept_same": 0, "kept_conf_changed": 0,
            "genes_skipped_lt2_survivors": 0, "genes_edge_set_identical": 0,
            "entities_filtered_total": 0,
            "t_b_ms": [],
        }
        for c in cutoffs
    }
    t_a1, t_a2 = [], []
    n_probed = 0
    swept_rows = []

    for gid in sample_ids:
        ents = [
            r["entity"]
            for r in cur.execute(
                "SELECT entity FROM entity_graph WHERE gene_id = ?", (gid,)
            ).fetchall()
        ]
        if len(ents) < 2:
            continue
        n_probed += 1

        swept_rows.append(sum(
            cur.execute(
                "SELECT COUNT(*) FROM entity_graph WHERE entity = ?", (e,)
            ).fetchone()[0]
            for e in ents
        ))

        t0 = time.perf_counter()
        rows_a = _link_select(cur, gid, ents)
        t_a1.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        rows_a = _link_select(cur, gid, ents)
        t_a2.append((time.perf_counter() - t0) * 1000)
        edges_a = _edges(rows_a, len(ents))

        for c in cutoffs:
            s = per_cutoff[c]
            t0 = time.perf_counter()
            kept = _filter_hubs(cur, ents, c)
            edges_b: dict = {}
            if len(kept) >= 2:
                edges_b = _edges(_link_select(cur, gid, kept), len(kept))
            else:
                s["genes_skipped_lt2_survivors"] += 1
            s["t_b_ms"].append((time.perf_counter() - t0) * 1000)

            s["entities_filtered_total"] += len(ents) - len(kept)
            s["edges_a"] += len(edges_a)
            s["edges_b"] += len(edges_b)
            s["lost"] += len(set(edges_a) - set(edges_b))
            s["gained"] += len(set(edges_b) - set(edges_a))
            for peer in set(edges_a) & set(edges_b):
                if edges_a[peer][1] == edges_b[peer][1]:
                    s["kept_same"] += 1
                else:
                    s["kept_conf_changed"] += 1
            if edges_a == edges_b:
                s["genes_edge_set_identical"] += 1

    def _ms(xs):
        if not xs:
            return {}
        xs_sorted = sorted(xs)
        return {
            "mean": round(statistics.mean(xs), 2),
            "median": round(statistics.median(xs), 2),
            "p95": round(xs_sorted[int(0.95 * (len(xs_sorted) - 1))], 2),
        }

    out = {
        "genes_probed": n_probed,
        "posting_rows_swept_per_call": _ms([float(x) for x in swept_rows]),
        "arm_a_ms_run1": _ms(t_a1),
        "arm_a_ms_run2_warm": _ms(t_a2),
        "cutoffs": {},
    }
    for c in cutoffs:
        s = per_cutoff[c]
        out["cutoffs"][str(c)] = {
            "arm_b_ms": _ms(s.pop("t_b_ms")),
            "entities_filtered_mean": round(
                s.pop("entities_filtered_total") / max(n_probed, 1), 2
            ),
            **s,
        }
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample", type=int, default=500)
    ap.add_argument("--seed", type=int, default=53416)
    ap.add_argument("--cutoffs", default="100,200,500,1000")
    ap.add_argument(
        "--thresholds", default="50,100,200,500,1000,5000,10000,50000"
    )
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    conn = _open_ro(args.db)
    cur = conn.cursor()

    genes = cur.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
    eg_rows = cur.execute("SELECT COUNT(*) FROM entity_graph").fetchone()[0]
    rel_rows = cur.execute("SELECT COUNT(*) FROM gene_relations").fetchone()[0]
    log.info("bed: %s genes, %s entity_graph rows", genes, eg_rows)

    thresholds = [int(x) for x in args.thresholds.split(",")]
    cutoffs = [int(x) for x in args.cutoffs.split(",")]

    log.info("part 1: hub-cardinality probe ...")
    probe = hub_probe(cur, thresholds)

    log.info("sampling %d linkable genes (seed %d) ...", args.sample, args.seed)
    # Linking population = genes with >= 2 entity postings.
    linkable = [
        r["gene_id"]
        for r in cur.execute(
            "SELECT gene_id FROM entity_graph "
            "GROUP BY gene_id HAVING COUNT(*) >= 2"
        ).fetchall()
    ]
    rng = random.Random(args.seed)
    sample_ids = rng.sample(linkable, min(args.sample, len(linkable)))

    log.info("part 2+3: A/B replay over %d genes ...", len(sample_ids))
    t0 = time.perf_counter()
    ab = ab_replay(cur, sample_ids, cutoffs)
    log.info("replay done in %.1fs", time.perf_counter() - t0)

    genes_after = cur.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
    conn.close()

    receipt = {
        "tool": "scripts/receipt_entity_hub_cutoff.py (read-only SQL replay)",
        "stamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "subject": {
            "db": args.db,
            "code": _git_describe(repo_root),
            "genes": genes,
            "genes_after_run": genes_after,
            "bed_stable_during_run": genes == genes_after,
            "entity_graph_rows": eg_rows,
            "gene_relations_rows": rel_rows,
            "sample": args.sample,
            "seed": args.seed,
            "linkable_genes": len(linkable),
        },
        "method_caveats": [
            "probe sets read back from entity_graph: deduplicated lowercase, "
            "insertion order lost (order-invariant for the SELECT and the "
            "confidence denominator; dedup only differs for genes that "
            "carried case-duplicate entities)",
            "replay against FINAL bed state; real edges form against "
            "insert-time state (both arms replay identically, so the A/B "
            "delta is like-for-like)",
            "ORDER BY shared DESC LIMIT 10 tie order is unspecified; edge "
            "sets compared as sets, and real ingest is already "
            "order-nondeterministic under --parallel "
            "(ingest_equivalence_enronqa.json)",
            "no writes: edges computed in Python from the SELECT rows "
            "(the linker's INSERT is deterministic from them)",
        ],
        "hub_probe": probe,
        "ab_replay": ab,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=1))
    log.info("wrote %s", out)


if __name__ == "__main__":
    main()

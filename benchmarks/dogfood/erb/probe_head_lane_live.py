"""ARM-3 stage L -- LIVE lane attribution for the head-chunk monopoly (2026-09-01).

Replays miss queries through the shipped pipeline read-only and captures, per
query, the full per-lane contribution map (Genome.last_tier_contributions), the
final eligible/scored set (last_query_scores), the fused map, and the FTS5
top-50 bm25 shortlist -- so the head-chunk monopoly can be attributed to a LANE
rather than asserted.

Bed opened read-only by the pipeline's own reader. No writes.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
ERB = ROOT / "benchmarks/dogfood/erb"
BED = "F:/tmp/erb_blob_v09x.db"
MINE = ERB / "receipts/interloper_mine_erb947k_2026-08-31.json"
OUT = ERB / "receipts/head_lane_live_2026-09-01.json"
CACHE = Path(os.environ.get("ARM3_CACHE", str(ERB / "receipts")))

N_QUERIES = int(os.environ.get("ARM3_LIVE_N", "40"))


def main() -> int:
    os.environ["CYMATIX_DISABLE_LEARN"] = "1"
    from cymatix_context.accel import extract_query_signals
    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager

    mine = json.loads(MINE.read_text(encoding="utf-8"))
    misses = mine["misses"][:N_QUERIES]

    cfg = load_config(str(ROOT / "cymatix.toml"))
    cfg.genome.path = BED
    mgr = CymatixContextManager(cfg)
    g = mgr.genome

    inventory = {
        "bm25_shortlist_enabled": bool(getattr(g, "_bm25_shortlist_enabled", False)),
        "bm25_shortlist_size": int(getattr(g, "_bm25_shortlist_size", 0)),
        "bm25_prefilter_enabled": bool(getattr(g, "_bm25_prefilter_enabled", False)),
        "fts5_candidate_depth_cfg": int(getattr(g, "_fts5_candidate_depth", 0)),
        "fts_fetch_depth_effective": int(getattr(g, "_fts5_candidate_depth", 0)) or (12 * 2 * 2),
        "rrf_k": int(getattr(g, "_rrf_k", -1)),
        "fusion_mode": getattr(g, "_fusion_mode", "?"),
        "filename_anchor_enabled": bool(getattr(g, "_filename_anchor_enabled", False)),
        "pki_enabled": bool(getattr(g, "_pki_enabled", False)),
        "splade_enabled": bool(getattr(g, "_splade_enabled", False)),
        "dense_enabled": bool(getattr(g, "_dense_embedding_enabled", False)),
        "rerank_enabled": bool(getattr(g, "_rerank_enabled", False)),
    }
    print(json.dumps(inventory, indent=1), flush=True)

    rows = []
    t0 = time.perf_counter()
    for i, m in enumerate(misses):
        q = m["query"]
        try:
            w = mgr.build_context(q, read_only=True, ignore_delivered=True, max_genes=12)
        except Exception as exc:  # noqa: BLE001
            rows.append({"needle": m["needle"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        final = dict(g.last_query_scores or {})
        fused = dict(getattr(g, "last_fused_scores", {}) or {})
        tc = dict(getattr(g, "last_tier_contributions", {}) or {})
        d, e = extract_query_signals(q)
        qterms = list(d) + list(e)
        fts_terms = [t for t in qterms if len(t) > 2]
        fts_q = " OR ".join('"%s"' % t for t in fts_terms)
        cur = g.read_conn.cursor()
        short50 = [r[0] for r in cur.execute(
            "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH ? ORDER BY rank LIMIT 50",
            (fts_q,)).fetchall()]
        deep = [(r[0], r[1]) for r in cur.execute(
            "SELECT gene_id, rank FROM genes_fts WHERE genes_fts MATCH ? ORDER BY rank LIMIT 400",
            (fts_q,)).fetchall()]
        cur.close()
        lane_counts = {}
        for _gid, lanes in tc.items():
            for ln in lanes:
                lane_counts[ln] = lane_counts.get(ln, 0) + 1
        ordered = [x for x, _ in sorted(final.items(), key=lambda kv: (-kv[1], kv[0]))]
        rows.append({
            "needle": m["needle"],
            "n_qterms": len(qterms),
            "n_tier_contrib_universe": len(tc),
            "lane_universe_counts": lane_counts,
            "n_final": len(final),
            "n_fused": len(fused),
            "final_order": ordered,
            "final_scores": {k: round(v, 6) for k, v in final.items()},
            "tier_contrib_final": {k: {a: round(b, 4) for a, b in tc.get(k, {}).items()}
                                   for k in final},
            "fts_top50": short50,
            "fts_deep400": [[a, round(b, 6)] for a, b in deep],
            "eligible_subset_of_fts50": set(final).issubset(set(short50)),
            "n_eligible_not_in_fts50": len(set(final) - set(short50)),
            "delivered": list(getattr(w, "expressed_gene_ids", None) or ()),
        })
        if (i + 1) % 5 == 0:
            el = time.perf_counter() - t0
            print("live %d/%d  %.0fs (%.1fs/q)" % (i + 1, len(misses), el, el / (i + 1)), flush=True)

    OUT.write_text(json.dumps({
        "stamp": "2026-09-01",
        "stage": "L (live lane attribution)",
        "bed": BED,
        "n_queries": len(rows),
        "inventory": inventory,
        "per_query": rows,
        "wall_s": round(time.perf_counter() - t0, 1),
    }, indent=1), encoding="utf-8")
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Issue #164 / #204 SPLADE-value scale curve.

Builds a SPLADE-on / SPLADE-off recall comparison across the configured
genome sizes (1K -> 850K) and reports recall@10, MRR, p95 latency, and
disk-bytes-per-gene. Output is the curve the issue body asks for so the
two ``splade_auto_*_genes`` thresholds can be set from data rather than
from the regime table.

Status: live harness (2026-07-13, #204). Two critical fixes over the
original scaffold:

1. **The on-arm now actually runs SPLADE at query time.** Every earlier
   version constructed ``Genome(path=..., dense_embedding_enabled=False)``
   without threading ``splade_enabled`` -- and the ``KnowledgeStore``
   constructor default is ``splade_enabled=False`` (the #256-family
   layer-default disagreement: config default True, constructor default
   False). Tier 3.5 is gated on ``self._splade_enabled``
   (knowledge_store.py), so the "on" arm was byte-identical to the "off"
   arm at query time, and every zero-delta this script ever reported
   (including the 2026-07-11 overnight P9 smoke) was an A/A receipt, not
   a SPLADE ablation. The on-arm is now constructed with
   ``splade_enabled=True`` and each arm's metrics carry a **firing
   receipt** (``splade_fire``: encode / query_splade call counts + total
   hits) so the artifact self-certifies that the mechanism engaged.

2. **``--query-shape raw|extracted``.** ``raw`` (default, back-compat)
   splits the full query text into ``domains`` terms -- stopwords and
   all, a query shape the serving pipeline never produces (absolute
   recall levels from raw runs are NOT serving-representative). And
   serving SPLADE encodes ``" ".join(query_terms)`` -- the *extracted*
   keyword bag -- not the raw question. ``extracted`` maps each query
   through the stage-1 extractor (``accel.extract_query_signals``) into
   ``(domains, entities)`` exactly as ``context_manager`` does: the
   serving-faithful shape for the lexical tiers AND the SPLADE query
   encoder. Use ``extracted`` for anything you intend to compare against
   pipeline-level numbers.

SPLADE-off twins are cheap: ``benchmarks/build_striptwins.py`` (copy bed
-> DROP TABLE splade_terms -> VACUUM, ~1 min per scale point; receipt in
``docs/research/2026-07-11-overnight-bench-results.md`` P9). The on-arm
warms the SPLADE encoder once before the timing loop so one-time model
load does not pollute the first query's latency (serving keeps the
encoder resident).

Usage:
    # Walk one size, compare on/off twins by db path
    python benchmarks/sweep_splade_scale_curve.py \\
        --on-genome F:/tmp/splade204/erag10k_on.db \\
        --off-genome F:/tmp/splade204/erag10k_off.db \\
        --label erag10k --query-shape extracted \\
        --queries benchmarks/_splade_curve_queries.json

    # Full curve (writes one JSON per size to --out-dir)
    python benchmarks/sweep_splade_scale_curve.py --curve --out-dir \\
        benchmarks/results/splade_scale_curve_$(date +%Y%m%d)
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# Default scale points. The 50K+ rows reference fixtures that may not
# exist locally; the runner skips a row whose .db is missing rather
# than failing.
SCALE_POINTS = [
    {"label": "small",   "approx_genes": 1_000,
     "on_db": "genomes/bench/matrix/small.db",
     "off_db": "genomes/bench/matrix/small_no_splade.db"},
    {"label": "medium",  "approx_genes": 17_000,
     "on_db": "genomes/bench/matrix/medium.db",
     "off_db": "genomes/bench/matrix/medium_no_splade.db"},
    {"label": "large",   "approx_genes": 28_000,
     "on_db": "genomes/bench/matrix/large.db",
     "off_db": "genomes/bench/matrix/large_no_splade.db"},
    {"label": "xl",      "approx_genes": 47_000,
     "on_db": "genomes/bench/matrix/xl.db",
     "off_db": "genomes/bench/matrix/xl_no_splade.db"},
    {"label": "erag10k", "approx_genes": 10_000,
     "on_db": "genomes/bench/matrix/enterprise_rag_10k_batched.db",
     "off_db": "genomes/bench/matrix/enterprise_rag_10k_no_splade.db"},
    {"label": "erag50k", "approx_genes": 50_000,
     "on_db": "genomes/bench/matrix/enterprise_rag_50k_batched.db",
     "off_db": "genomes/bench/matrix/enterprise_rag_50k_no_splade.db"},
]


def _disk_bytes_per_gene(db_path: str, gene_count: int) -> float:
    """Total .db + -wal size / gene_count. Issue #164's disk lens."""
    if gene_count <= 0 or not os.path.exists(db_path):
        return -1.0
    total = os.path.getsize(db_path)
    for suffix in ("-wal", "-shm"):
        s = db_path + suffix
        if os.path.exists(s):
            total += os.path.getsize(s)
    return round(total / gene_count, 3)


def _eval_genome(db_path: str, queries: list[dict], topk: int,
                 splade_query_enabled: bool = False,
                 query_shape: str = "raw") -> dict:
    """Run ``queries`` against the genome and return aggregate metrics
    plus per-query latencies for p95.

    ``splade_query_enabled`` threads into ``Genome(splade_enabled=...)``
    so tier 3.5 (query-side SPLADE) actually runs on the on-arm. When
    true, ``splade_backend.encode`` / ``query_splade`` are wrapped with
    counters for the firing receipt (``splade_fire`` in the returned
    metrics) -- an on-arm result with ``query_splade_calls == 0`` is an
    A/A run, not a SPLADE ablation, and must not be read as one.

    ``query_shape``: "raw" passes ``query_text.split()`` as domains
    (legacy behaviour); "extracted" passes the stage-1 extractor's
    ``(domains, entities)`` -- the serving shape.
    """
    from helix_context.genome import Genome

    fire = {"encode_calls": 0, "query_splade_calls": 0, "hits_total": 0}
    _patched = False
    _orig_encode = _orig_query = None
    if splade_query_enabled:
        try:
            from helix_context.backends import splade_backend

            _orig_encode = splade_backend.encode
            _orig_query = splade_backend.query_splade

            def _counting_encode(text, *a, **kw):
                fire["encode_calls"] += 1
                return _orig_encode(text, *a, **kw)

            def _counting_query(conn, sparse, *a, **kw):
                out = _orig_query(conn, sparse, *a, **kw)
                fire["query_splade_calls"] += 1
                fire["hits_total"] += len(out or [])
                return out

            splade_backend.encode = _counting_encode
            splade_backend.query_splade = _counting_query
            _patched = True
            # Warm the encoder outside the timing loop: one-time model
            # load (~5-10s CPU) would otherwise land on query 0's latency.
            try:
                _orig_encode("warmup")
            except Exception:
                pass
        except Exception:
            pass

    extract_signals = None
    if query_shape == "extracted":
        from helix_context.accel import extract_query_signals as extract_signals

    g = Genome(path=db_path, dense_embedding_enabled=False,
               splade_enabled=splade_query_enabled)
    try:
        n = len(queries)
        hits_at_k = 0
        rr_sum = 0.0
        latencies = []

        total_genes = g.conn.execute(
            "SELECT COUNT(*) FROM genes"
        ).fetchone()[0]
        has_splade_table = g.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='splade_terms'"
        ).fetchone()[0]
        splade_rows = 0
        if has_splade_table:
            splade_rows = g.conn.execute(
                "SELECT COUNT(*) FROM splade_terms"
            ).fetchone()[0]

        for q in queries:
            query_text = q["query"]
            gold = set(q["gold_ids"])
            if extract_signals is not None:
                domains, entities = extract_signals(query_text)
            else:
                domains, entities = query_text.split(), []
            t0 = time.monotonic()
            try:
                docs = g.query_docs(
                    domains=domains,
                    entities=entities,
                    max_genes=max(topk, 20),
                )
            except Exception:
                continue
            latencies.append(time.monotonic() - t0)
            ids = [d.gene_id for d in docs]
            rank = 0
            for i, gid in enumerate(ids[:topk], start=1):
                if gid in gold:
                    rank = i
                    break
            if rank > 0:
                hits_at_k += 1
                rr_sum += 1.0 / rank

        latencies.sort()
        p95 = latencies[max(int(len(latencies) * 0.95) - 1, 0)] if latencies else None

        return {
            "n_queries": n,
            "recall_at_k": hits_at_k / max(n, 1),
            "mrr": rr_sum / max(n, 1),
            "p95_s": round(p95, 4) if p95 else None,
            "mean_s": round(statistics.mean(latencies), 4) if latencies else None,
            "total_genes": total_genes,
            "splade_rows": splade_rows,
            "splade_present": bool(has_splade_table) and splade_rows > 0,
            "disk_bytes_per_gene": _disk_bytes_per_gene(db_path, total_genes),
            "query_shape": query_shape,
            # Post-construction truth: Genome soft-fails splade_enabled to
            # False when the backend can't load, so read it back rather
            # than echoing the requested value.
            "splade_query_enabled": bool(getattr(g, "_splade_enabled", False)),
            "splade_fire": dict(fire),
        }
    finally:
        g.close()
        if _patched:
            from helix_context.backends import splade_backend
            splade_backend.encode = _orig_encode
            splade_backend.query_splade = _orig_query


def _evaluate_pair(label: str, on_db: str, off_db: str,
                   queries: list[dict], topk: int,
                   query_shape: str = "raw") -> dict:
    """Return the per-scale-point row the curve plot needs.

    The on-arm runs with ``splade_enabled=True`` (query-side tier 3.5
    live); the off-arm with ``splade_enabled=False`` against the
    stripped twin -- a full SPLADE ablation on both the storage and the
    query side.
    """
    arms = {}
    for arm_name, db in (("on", on_db), ("off", off_db)):
        if not os.path.exists(db):
            arms[arm_name] = {"missing": db}
            continue
        arms[arm_name] = _eval_genome(
            db, queries, topk,
            splade_query_enabled=(arm_name == "on"),
            query_shape=query_shape,
        )

    on_arm = arms.get("on") or {}
    off_arm = arms.get("off") or {}
    delta = {}
    if "recall_at_k" in on_arm and "recall_at_k" in off_arm:
        delta["recall_at_k_delta"] = round(
            on_arm["recall_at_k"] - off_arm["recall_at_k"], 4
        )
    if "p95_s" in on_arm and "p95_s" in off_arm and on_arm["p95_s"] and off_arm["p95_s"]:
        delta["p95_s_delta"] = round(on_arm["p95_s"] - off_arm["p95_s"], 4)
    if ("disk_bytes_per_gene" in on_arm
            and "disk_bytes_per_gene" in off_arm
            and on_arm["disk_bytes_per_gene"] > 0
            and off_arm["disk_bytes_per_gene"] > 0):
        delta["disk_bytes_per_gene_delta"] = round(
            on_arm["disk_bytes_per_gene"] - off_arm["disk_bytes_per_gene"], 3
        )

    return {
        "label": label,
        "query_shape": query_shape,
        "on": on_arm,
        "off": off_arm,
        "splade_value_signal": delta,
    }


def _auto_queries(db_path: str, n: int) -> list[dict]:
    """Synthesize ``n`` queries from genome content tokens (cheap smoke).

    For a real curve, supply a question set via ``--queries`` -- this
    auto-synth is a smoke-only fallback so the harness runs end-to-end
    without a pre-built fixture pack.
    """
    from helix_context.genome import Genome
    g = Genome(path=db_path, dense_embedding_enabled=False)
    try:
        rows = g.conn.execute(
            "SELECT gene_id, content FROM genes "
            "WHERE content IS NOT NULL AND length(content) >= 80 "
            "ORDER BY random() LIMIT ?",
            (n,),
        ).fetchall()
        out = []
        for r in rows:
            gid = r["gene_id"]
            toks = r["content"].split()[:8]
            if len(toks) < 4:
                continue
            out.append({"query": " ".join(toks), "gold_ids": [gid]})
        return out
    finally:
        g.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--on-genome", default=None,
                        help="Path to SPLADE-on genome (.db)")
    parser.add_argument("--off-genome", default=None,
                        help="Path to SPLADE-off genome (.db)")
    parser.add_argument("--label", default="ad-hoc",
                        help="Scale-curve label for the report row")
    parser.add_argument("--queries", default=None,
                        help="JSON list of {query, gold_ids} (auto-synth if omitted)")
    parser.add_argument("--auto-query-count", type=int, default=30,
                        help="Auto-synthesized query count when --queries omitted")
    parser.add_argument("--topk", type=int, default=10)
    parser.add_argument("--query-shape", default="raw",
                        choices=("raw", "extracted"),
                        help="'raw' = query_text.split() as domains (legacy; "
                             "NOT serving-representative). 'extracted' = "
                             "stage-1 extract_query_signals (serving shape)")
    parser.add_argument("--out", default=None,
                        help="JSON output path (stdout if omitted)")
    parser.add_argument("--curve", action="store_true",
                        help="Walk the canonical scale points (skips missing .db)")
    parser.add_argument("--out-dir", default=None,
                        help="Output directory for --curve mode")
    args = parser.parse_args(argv)

    if args.curve:
        out_dir = args.out_dir or "benchmarks/results/splade_scale_curve"
        os.makedirs(out_dir, exist_ok=True)
        rows = []
        for pt in SCALE_POINTS:
            if not os.path.exists(pt["on_db"]):
                print(f"[curve] skip {pt['label']} (missing {pt['on_db']})",
                      file=sys.stderr)
                continue
            if args.queries:
                with open(args.queries, "r", encoding="utf-8") as f:
                    queries = json.load(f)
            else:
                queries = _auto_queries(pt["on_db"], args.auto_query_count)
            row = _evaluate_pair(
                pt["label"], pt["on_db"], pt["off_db"],
                queries, args.topk, query_shape=args.query_shape,
            )
            row["approx_genes"] = pt["approx_genes"]
            rows.append(row)
            print(f"[curve] {pt['label']}: recall_delta="
                  f"{row['splade_value_signal'].get('recall_at_k_delta')}",
                  file=sys.stderr)
            with open(os.path.join(out_dir, f"{pt['label']}.json"), "w",
                      encoding="utf-8") as f:
                json.dump(row, f, indent=2, default=str)
        with open(os.path.join(out_dir, "curve.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"rows": rows, "topk": args.topk}, f, indent=2, default=str)
        print(f"[curve] wrote {out_dir}/curve.json", file=sys.stderr)
        return 0

    if not args.on_genome or not args.off_genome:
        parser.error("--on-genome and --off-genome required outside --curve mode")
    if args.queries:
        with open(args.queries, "r", encoding="utf-8") as f:
            queries = json.load(f)
    else:
        queries = _auto_queries(args.on_genome, args.auto_query_count)
    row = _evaluate_pair(
        args.label, args.on_genome, args.off_genome, queries, args.topk,
        query_shape=args.query_shape,
    )
    out_text = json.dumps(row, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out_text)
        print(f"[sweep] wrote {args.out}", file=sys.stderr)
    else:
        print(out_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

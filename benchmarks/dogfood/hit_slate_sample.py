"""Hit-slate score sampler — the control for the miss-slate score question.

For a sample of DELIVERED-gold needles, replay read-only and record the same
score fields the interloper miner recorded for misses: per-seat scores, gold
rank/score. Comparing the two answers: does a miss slate look score-normal
(indistinguishable from a hit slate, so no abstain signal exists) or
score-depressed (a gate signal is available)?

Usage mirrors mine_interlopers.py; --sample N caps the hit sample.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

log = logging.getLogger("hit_slate_sample")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--bed", required=True)
    ap.add_argument("--needles", required=True)
    ap.add_argument("--gold", required=True)
    ap.add_argument("--receipt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default="cymatix.toml")
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--sample", type=int, default=100)
    args = ap.parse_args()

    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager

    nraw = json.loads(Path(args.needles).read_text(encoding="utf-8"))
    nlist = nraw["needles"] if isinstance(nraw, dict) else nraw
    queries = {n["name"]: n for n in nlist}
    gold_by = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    rec = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    pq = rec["arms"][0]["per_query"]
    hits = sorted((r for r in pq if r.get("delivered_gold")), key=lambda r: r["needle"])
    step = max(1, len(hits) // args.sample)
    hits = hits[::step][: args.sample]
    log.info("%s: sampling %d hits", args.tag, len(hits))

    cfg = load_config(args.config)
    cfg.genome.path = args.bed
    manager = CymatixContextManager(cfg)

    rows = []
    t0 = time.perf_counter()
    for i, r in enumerate(hits):
        name = r["needle"]
        q = (queries.get(name) or {}).get("query", "")
        gold = set(gold_by.get(name) or [])
        try:
            window = manager.build_context(q, read_only=True, ignore_delivered=True,
                                           max_genes=args.k)
        except Exception as exc:  # noqa: BLE001 — one bad replay must not kill the sample
            log.warning("%s replay failed: %s", name, exc)
            continue
        scores = dict(manager.genome.last_query_scores or {})
        ordered = [g for g, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]
        delivered = list(getattr(window, "expressed_gene_ids", None) or ())
        d_scores = [round(scores.get(g, 0.0), 4) for g in delivered]
        g_ranks = sorted(i + 1 for i, g in enumerate(ordered) if g in gold)
        rows.append({
            "needle": name,
            "class": (queries.get(name) or {}).get("question_type", "unknown"),
            "delivered_count": len(delivered),
            "delivered_scores": d_scores,
            "gold_rank": g_ranks[0] if g_ranks else None,
            "gold_score": round(max((scores.get(g, 0.0) for g in gold), default=0.0), 4),
            "gold_delivered": int(bool(set(delivered) & gold)),
        })
        if (i + 1) % 25 == 0:
            log.info("%s: %d/%d (%.1fs/q)", args.tag, i + 1, len(hits),
                     (time.perf_counter() - t0) / (i + 1))
    out = {"stamp": "2026-08-31", "tag": args.tag, "bed": args.bed,
           "basis_receipt": Path(args.receipt).name, "hits": rows}
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    log.info("%s: wrote %s (%d rows, %.0fs)", args.tag, args.out, len(rows),
             time.perf_counter() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())

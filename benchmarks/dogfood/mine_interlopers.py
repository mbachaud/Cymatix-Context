"""Interloper miner — what WINS when a needle's gold is not delivered.

For every needle whose baseline receipt row has ``delivered_gold == 0``,
replay the query read-only at shipped config and capture BOTH bases:

  * ``delivered`` — the ordered post-budget slice (``window.expressed_gene_ids``),
    i.e. the response we actually provide instead of gold;
  * ``map_top`` — the score-map head, i.e. who is winning the pool even when
    delivery-side trims/floors reshuffle the tail.

Each captured doc carries score, rank, tags, a content-head snippet, length,
and distinct-query-keyword coverage, so downstream classification can say
WHAT the interlopers are (boilerplate, hubs, near-duplicates, wrong-doc
same-concept, ...), not merely that gold lost.

Per-needle validation: the live score-map rank of first gold is checked
against the receipt's ``rank_of_first_gold`` (``rank_match``), so drifted
replays are self-flagging.

Usage (one bed per invocation; run beds in parallel — separate SQLite files):
  python benchmarks/dogfood/mine_interlopers.py --tag muloc \
      --bed F:/tmp/muloc_bed/muloc.db \
      --needles benchmarks/dogfood/muloc/needles_resolved_muloc_full.json \
      --gold benchmarks/dogfood/muloc/gold_by_needle_muloc.json \
      --receipt benchmarks/dogfood/muloc/receipts/ladder_muloc_baseline_cold_2026-08-31.json \
      --out benchmarks/dogfood/muloc/receipts/interloper_mine_muloc_2026-08-31.json
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

log = logging.getLogger("mine_interlopers")

# Run from any cwd: import cymatix_context from this script's repo checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SNIPPET = 220
MAP_TOP = 15

_TERM_RE = re.compile(r"[a-z0-9_/\-]+")


def terms(text: str) -> set:
    return {t for t in _TERM_RE.findall(text.lower()) if len(t) > 2}


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
    ap.add_argument("--limit", type=int, default=0, help="0 = all misses")
    args = ap.parse_args()

    from cymatix_context.accel import extract_query_signals
    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager

    nraw = json.loads(Path(args.needles).read_text(encoding="utf-8"))
    nlist = nraw["needles"] if isinstance(nraw, dict) else nraw
    queries = {n["name"]: n for n in nlist}
    gold_by = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    rec = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    per_query = rec["arms"][0]["per_query"]
    misses = [r for r in per_query if not r.get("delivered_gold")]
    if args.limit:
        misses = misses[: args.limit]
    log.info("%s: %d misses of %d needles", args.tag, len(misses), len(per_query))

    cfg = load_config(args.config)
    cfg.genome.path = args.bed
    manager = CymatixContextManager(cfg)

    def kw(query: str) -> list:
        d, e = extract_query_signals(query)
        return sorted(set(d) | set(e))

    def doc_info(gid: str, rank: int, scores: dict, ks: list) -> dict:
        row = manager.genome.get_doc(gid)
        if row is None:
            return {"gene_id": gid, "rank": rank, "missing": True}
        content = getattr(row, "content", "") or ""
        p = getattr(row, "promoter", None)
        tags = (list(getattr(p, "domains", []) or []) +
                list(getattr(p, "entities", []) or [])) if p else []
        dterms = terms(content)
        return {
            "gene_id": gid,
            "rank": rank,
            "score": round(scores.get(gid, 0.0), 4),
            "kw_cov": sum(1 for t in ks if t in dterms),
            "n_chars": len(content),
            "tags": tags[:8],
            "head": content[:SNIPPET].replace("\n", " "),
        }

    out = {
        "stamp": "2026-08-31",
        "tag": args.tag,
        "bed": args.bed,
        "config": args.config,
        "basis_receipt": Path(args.receipt).name,
        "k": args.k,
        "n_misses": len(misses),
        "misses": [],
    }
    rank_match = replay_errors = 0
    t_start = time.perf_counter()
    for i, row in enumerate(misses):
        name = row["needle"]
        meta = queries.get(name) or {}
        q = meta.get("query", "")
        gold = set(gold_by.get(name) or [])
        entry = {
            "needle": name,
            "class": meta.get("question_type", "unknown"),
            "query": q,
            "receipt_rank": row.get("rank_of_first_gold"),
            "receipt_delivered_count": row.get("delivered_count"),
        }
        try:
            window = manager.build_context(q, read_only=True, ignore_delivered=True,
                                           max_genes=args.k)
        except Exception as exc:  # noqa: BLE001 — one bad replay must not kill the mine
            log.warning("%s replay failed: %s: %s", name, type(exc).__name__, exc)
            entry["error"] = f"{type(exc).__name__}: {exc}"
            out["misses"].append(entry)
            replay_errors += 1
            continue

        scores = dict(manager.genome.last_query_scores or {})
        ordered = [g for g, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]
        rank_of = {g: i for i, g in enumerate(ordered, 1)}
        gold_ranks = sorted(rank_of[g] for g in gold if g in rank_of)
        live_rank = gold_ranks[0] if gold_ranks else None
        delivered = list(getattr(window, "expressed_gene_ids", None) or ())

        ks = kw(q)
        gold_terms: set = set()
        gold_info = None
        for gid in sorted(gold):
            g = manager.genome.get_doc(gid)
            if g is None:
                continue
            gold_terms |= terms(getattr(g, "content", "") or "")
            if gold_info is None:
                gold_info = doc_info(gid, rank_of.get(gid, 0), scores, ks)
        entry.update({
            "live_rank": live_rank,
            "rank_match": live_rank == row.get("rank_of_first_gold"),
            "n_kw": len(ks),
            "gold_n": len(gold),
            "gold_kw_cov": sum(1 for t in ks if t in gold_terms),
            "gold_first": gold_info,
            "delivered": [doc_info(g, i + 1, scores, ks) for i, g in enumerate(delivered)],
            "map_top": [doc_info(g, i + 1, scores, ks) for i, g in enumerate(ordered[:MAP_TOP])],
        })
        rank_match += entry["rank_match"]
        out["misses"].append(entry)
        if (i + 1) % 25 == 0:
            el = time.perf_counter() - t_start
            log.info("%s: %d/%d mined (%.0fs, %.1fs/q, rank_match %d)",
                     args.tag, i + 1, len(misses), el, el / (i + 1), rank_match)

    out["summary"] = {
        "n_mined": len(out["misses"]),
        "replay_errors": replay_errors,
        "rank_match": rank_match,
        "wall_s": round(time.perf_counter() - t_start, 1),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    log.info("%s: wrote %s (%s)", args.tag, args.out, json.dumps(out["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

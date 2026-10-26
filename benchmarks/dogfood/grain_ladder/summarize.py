"""Summarize the grain ladder: ranking (BEIR NDCG@10) + what the packet carried.

Per arm: NDCG@10 (BEIR-comparable), r@k, delivered-gold rate, and the
delivered packet's size — chars, chunks, distinct source documents, and
chars per delivered gold hit. Paired delivered-gold wins/losses vs the
c4000 control are counted per needle.

Usage: python -P benchmarks/dogfood/grain_ladder/summarize.py [--stamp 2026-10-15]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent
TAGS = ["beir_scifact", "beir_nfcorpus"]
GRAINS = [4000, 1824, 580]


def load(tag: str, grain: int, stamp: str, variant: str = ""):
    bed_arm = f"{tag}_c{grain}"
    arm = bed_arm + (f"_{variant}" if variant else "")
    rec = HERE / bed_arm / "receipts"
    lad = rec / f"ladder_{arm}_seed0_{stamp}.json"
    nd = rec / f"beir_ndcg_{arm}_{stamp}.json"
    if not (lad.exists() and nd.exists()):
        return None
    return json.loads(lad.read_text(encoding="utf-8")), json.loads(nd.read_text(encoding="utf-8"))


def doc_of(db: str) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT gene_id, source_id FROM genes"))
    finally:
        con.close()


PACKET_CHARS = 18000  # expression_tokens 4500 x 4 chars/token


def miss_breakdown(pq: list, k: int) -> dict:
    """Split every needle into delivered vs why gold did not ship.

    Pre-pack rank = ``final_rank_of_first_gold`` (post-rerank retrieval order,
    before tiering / budget trim); post-pack rank = ``delivered_gold_rank``.
      cut_by_packing   gold in the pre-pack top-k but not delivered (a tier or
                       budget cap removed it)
      ranked_below_cap gold ranked, but past k (never eligible for the packet)
      never_ranked     no gold anywhere in the ranked pool
      abstain          the abstain gate returned no packet
    """
    c = {"delivered": 0, "cut_by_packing": 0, "ranked_below_cap": 0, "never_ranked": 0, "abstain": 0}
    pre, post, util = [], [], []
    for r in pq:
        util.append(r.get("delivered_chars", 0) / PACKET_CHARS)
        fr = r.get("final_rank_of_first_gold")
        if r.get("delivered_gold"):
            c["delivered"] += 1
            if fr is not None:
                pre.append(fr)
            post.append(r["delivered_gold_rank"])
        elif r.get("budget_tier") == "abstain":
            c["abstain"] += 1
        elif fr is None:
            c["never_ranked"] += 1
        elif fr <= k:
            c["cut_by_packing"] += 1
        else:
            c["ranked_below_cap"] += 1
    c["util_mean"] = round(st.mean(util), 3) if util else 0.0
    c["util_p50"] = round(st.median(util), 3) if util else 0.0
    c["pre_pack_rank_p50"] = st.median(pre) if pre else None
    c["post_pack_rank_p50"] = st.median(post) if post else None
    return c


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default="2026-10-15")
    ap.add_argument("--variant", default="", help='"" or "charcap"')
    args = ap.parse_args()
    rows, out = [], {}
    for tag in TAGS:
        base = None
        for g in GRAINS:
            got = load(tag, g, args.stamp, args.variant)
            if got is None:
                continue
            lad, nd = got
            arm = lad["arms"][0]
            pq = arm["per_query"]
            src = doc_of(lad["bed"])
            chars = [r.get("delivered_chars", 0) for r in pq]
            chunks = [r.get("delivered_count", 0) for r in pq]
            docs = [len({src.get(x) for x in r.get("delivered_gene_ids", [])}) for r in pq]
            hits = {r["needle"]: r.get("delivered_gold", 0) for r in pq}
            if base is None:
                base = hits
            win = sum(1 for n, h in hits.items() if h and not base.get(n))
            loss = sum(1 for n, h in hits.items() if not h and base.get(n))
            dg = arm["delivered_gold_rate"]
            cell = {
                "grain": g, "genes": len(src), "ndcg10": nd["beir_comparable_ndcg@10"],
                "r_at_k": arm["recall_at_k"], "k": lad["k"], "delivered_gold": dg,
                "paired_vs_c4000": f"+{win}/-{loss}",
                "chars_mean": round(st.mean(chars)), "chars_p50": int(st.median(chars)),
                "chunks_mean": round(st.mean(chunks), 1), "docs_mean": round(st.mean(docs), 1),
                "chars_per_gold_hit": round(sum(chars) / max(1, sum(hits.values()))),
                "p50_ms": arm["latency_ms"].get("p50") if isinstance(arm["latency_ms"], dict) else None,
                "git_sha": lad["git_sha"], "config": lad["config"],
                "misses": miss_breakdown(pq, lad["k"]),
            }
            out[f"{tag}_c{g}"] = cell
            rows.append((tag, cell))
    hdr = "tag | grain | genes | NDCG@10 | r@k (k) | deliv gold | paired | chars mean/p50 | chunks | docs | chars/hit | p50ms"
    print(hdr)
    for tag, c in rows:
        print(f"{tag} | {c['grain']} | {c['genes']} | {c['ndcg10']:.4f} | {c['r_at_k']:.3f} ({c['k']}) | "
              f"{c['delivered_gold']:.3f} | {c['paired_vs_c4000']} | {c['chars_mean']}/{c['chars_p50']} | "
              f"{c['chunks_mean']} | {c['docs_mean']} | {c['chars_per_gold_hit']} | {c['p50_ms']}")
    print()
    print("tag | grain | delivered | cut_by_packing | ranked_below_cap | never_ranked | abstain | util mean/p50 | gold rank pre/post p50")
    for tag, c in rows:
        m = c["misses"]
        print(f"{tag} | {c['grain']} | {m['delivered']} | {m['cut_by_packing']} | {m['ranked_below_cap']} | "
              f"{m['never_ranked']} | {m['abstain']} | {m['util_mean']}/{m['util_p50']} | "
              f"{m['pre_pack_rank_p50']}/{m['post_pack_rank_p50']}")
    sfx = f"_{args.variant}" if args.variant else ""
    (HERE / f"summary_{args.stamp}{sfx}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

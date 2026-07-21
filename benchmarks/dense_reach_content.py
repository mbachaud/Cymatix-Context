"""Content-dense REACH test (thread 2) — quantify the semantic ceiling mechanism
using ONLY stored vectors (no re-embed).

The cosine diagnostic showed content and complement align with semantic queries
equally (~0.56), so the lever is NOT strand choice. This isolates the DENSE tier:
where does the semantic gold actually RANK among all 80k docs under content-dense
(embedding_dense_v2, what ships today)? If gold is buried past top-200, the reach
failure lives in the dense tier's resolution — a better embedding model is the
lever, not a strand/threshold tweak.

Pure numpy over stored little-endian f4 blobs (vec_to_blob format) + BGE-M3 query
encodes. Dedup genes -> dsid (doc level, matching the canonical metric). Local.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

BED = str(_REPO / "genomes/bench/matrix/enterprise_rag_50k_batched.db")
DSID_MAP = str(_REPO / "benchmarks" / "results" / "dsid_map_enterprise_rag_50k.json")
QUESTIONS = "F:/tmp/ext_ct_helixbench/questions/onyx_500.jsonl"


def _norm(p):
    return os.path.normcase(os.path.normpath(p))


def main():
    from cymatix_context.backends.bgem3_codec import BGEM3Codec

    dsid_map = json.load(open(DSID_MAP, encoding="utf-8"))  # _norm(path)->dsid

    sem = [json.loads(l) for l in open(QUESTIONS, encoding="utf-8")]
    sem = [q for q in sem if q.get("question_type") == "semantic" and q.get("expected_doc_ids")]

    # Load stored content-dense + map each gene to its dsid.
    print("loading stored content-dense vectors...", file=sys.stderr)
    c = sqlite3.connect(f"file:{BED}?mode=ro", uri=True)
    vecs, gene_dsid = [], []
    dim = None
    try:
        for src, blob in c.execute(
            "SELECT source_id, embedding_dense_v2 FROM genes WHERE embedding_dense_v2 IS NOT NULL"
        ):
            v = np.frombuffer(blob, dtype="<f4")
            if dim is None:
                dim = v.shape[0]
            if v.shape[0] != dim:
                continue
            dsid = dsid_map.get(_norm(src))
            if not dsid:
                continue
            vecs.append(v)
            gene_dsid.append(dsid)
    finally:
        c.close()
    M = np.vstack(vecs).astype(np.float32)          # (N, dim)
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    gene_dsid = np.array(gene_dsid)
    print(f"  {M.shape[0]} gene vectors, dim={dim}, "
          f"{len(set(gene_dsid))} distinct docs", file=sys.stderr)

    codec = BGEM3Codec()

    def dsid_ranking(qv):
        """doc-level ranking: max gene score per dsid, sorted desc -> [dsids]."""
        scores = M @ qv                              # (N,)
        best = {}
        for s, d in zip(scores, gene_dsid):
            if s > best.get(d, -2.0):
                best[d] = float(s)
        return sorted(best, key=best.get, reverse=True), best

    ks = (10, 50, 200)
    hit = {k: 0 for k in ks}
    gold_ranks, gold_cos, gold_pctile = [], [], []
    n = 0
    for i, q in enumerate(sem):
        qv = np.asarray(codec.encode(q["question"], task="query"), dtype=np.float32)
        qv /= (np.linalg.norm(qv) + 1e-9)
        ranked, best = dsid_ranking(qv)
        gold = set(q["expected_doc_ids"])
        # best rank among gold dsids present
        ranks = [ranked.index(g) + 1 for g in gold if g in best]
        if not ranks:
            n += 1
            gold_ranks.append(None)
            continue
        r = min(ranks)
        gold_ranks.append(r)
        gc = max(best[g] for g in gold if g in best)
        gold_cos.append(gc)
        # percentile of gold score among all doc scores (1.0 = top)
        allv = np.array(list(best.values()))
        gold_pctile.append(float((allv < gc).mean()))
        for k in ks:
            if r <= k:
                hit[k] += 1
        n += 1
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(sem)}", file=sys.stderr)

    print("\n=== CONTENT-DENSE REACH (semantic, doc-level, dense tier only) ===")
    print(f"N={n}")
    for k in ks:
        print(f"  dense-only recall@{k:<3} = {hit[k]}/{n} = {hit[k]/n:.3f}")
    found = [r for r in gold_ranks if r is not None]
    print(f"  gold in-corpus (has a vector): {len(found)}/{n}")
    if gold_cos:
        print(f"  mean gold cosine   = {np.mean(gold_cos):.4f}")
        print(f"  mean gold pctile   = {np.mean(gold_pctile):.4f}  "
              f"(1.0=top; how far above the distractor cloud)")
        import statistics
        med = statistics.median([r for r in found])
        print(f"  median gold rank   = {med}  (of {len(set(gene_dsid))} docs)")
    json.dump({"N": n, "recall": {k: hit[k]/n for k in ks},
               "mean_gold_cosine": float(np.mean(gold_cos)) if gold_cos else None,
               "mean_gold_pctile": float(np.mean(gold_pctile)) if gold_pctile else None,
               "gold_ranks": gold_ranks},
              open((_REPO / "benchmarks" / "results" / "dense_reach_content.json"), "w"), indent=2)
    print(f"-> {_HERE / 'dense_reach_content.json'}")


if __name__ == "__main__":
    main()

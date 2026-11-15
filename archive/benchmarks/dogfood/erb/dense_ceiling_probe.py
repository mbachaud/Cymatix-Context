"""Dense-only ceiling probe for the gated-dense-fallback hypothesis (2026-08-31).

Question: when LEXICAL retrieval misses gold (the M1 vocabulary-gap regime),
would a dense lane have reached it? This is the kill-or-commit gate for the
"condition-gated dense on low-anchor queries" proposal — measured on an
existing dense-populated bed, zero backfill hours.

Bed: F:/tmp/erb_carve/erb_829k_nosplade.db (829,131 genes, embedding_dense_v2
BGE-M3 1024-d fp32 fully populated at build time — pre-#371 era bed).
Lexical miss set: the no_pki arm (= shipped default since #370) of
postflip_default_confirm_829k.json, n=469 per-query rows, delivered .5629.
DISCLOSED: that arm ran post-flip v0.9.0-era defaults (rrf_k=60, pre-wave-1);
the v0.9.1 miss roster on this bed would be somewhat smaller. The probe
measures the query<->gold vocabulary relationship (config-independent), so
the pre-wave-1 roster is a valid, slightly conservative target population.

Design: dense-ONLY ranking (cosine over all 829k vectors, exhaustive — no
shortlist, no fusion), BGEM3Codec task="query" to match the retrieval path
(knowledge_store.py:4469). For every lexical MISS and a 100-needle HIT
control sample: rank of first gold in the pure dense ordering. The headline
is P(dense rank <= 12 | lexical miss), by class — the rescue ceiling a
perfectly-gated fallback could reach at k=12.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

NE = Path(r"F:/Projects/cymatix-context/.claude/worktrees/nostalgic-elion-5304a1/benchmarks/dogfood/erb")
BED = r"F:/tmp/erb_carve/erb_829k_nosplade.db"
RECEIPT = NE / "receipts/postflip_default_confirm_829k.json"
OUT = Path(__file__).resolve().parents[0] / "receipts/dense_ceiling_probe_829k_2026-08-31.json"
DIM = 1024
HIT_SAMPLE = 100


def main() -> int:
    t0 = time.perf_counter()
    needles_raw = json.loads((NE / "needles_resolved_blob.json").read_text(encoding="utf-8"))
    nlist = needles_raw["needles"] if isinstance(needles_raw, dict) else needles_raw
    meta = {n["name"]: n for n in nlist}
    gold_by = json.loads((NE / "gold_by_needle_blob.json").read_text(encoding="utf-8"))
    rec = json.loads(RECEIPT.read_text(encoding="utf-8"))
    arm = next(a for a in rec["arms"] if a["arm"] == "no_pki")
    pq = arm["per_query"]
    misses = sorted((r for r in pq if not r.get("delivered_gold")), key=lambda r: r["needle"])
    hits = sorted((r for r in pq if r.get("delivered_gold")), key=lambda r: r["needle"])[:HIT_SAMPLE]
    targets = [(r, "miss") for r in misses] + [(r, "hit_control") for r in hits]
    print(f"targets: {len(misses)} misses + {len(hits)} hit controls", flush=True)

    # ── load the full dense matrix ─────────────────────────────────────
    con = sqlite3.connect(f"file:{BED}?mode=ro", uri=True)
    cur = con.cursor()
    ids: list = []
    chunks: list = []
    t = time.perf_counter()
    for gid, blob in cur.execute(
            "SELECT gene_id, embedding_dense_v2 FROM genes "
            "WHERE embedding_dense_v2 IS NOT NULL"):
        ids.append(gid)
        chunks.append(np.frombuffer(blob, dtype=np.float32))
    con.close()
    M = np.vstack(chunks)
    del chunks
    idx = {g: i for i, g in enumerate(ids)}
    norms = np.linalg.norm(M[:1000], axis=1)
    print(f"matrix {M.shape} loaded in {time.perf_counter()-t:.0f}s; "
          f"sample norms mean {norms.mean():.4f} (1.0 = stored normalized)", flush=True)
    if abs(float(norms.mean()) - 1.0) > 0.01:
        row_norms = np.linalg.norm(M, axis=1, keepdims=True)
        row_norms[row_norms == 0] = 1.0
        M = M / row_norms
        print("re-normalized rows", flush=True)

    # ── encode queries ─────────────────────────────────────────────────
    from cymatix_context.backends.bgem3_codec import BGEM3Codec
    device = "cpu"
    try:
        import torch
        if torch.cuda.is_available():
            device = "cuda"
    except Exception:  # noqa: BLE001 — torch probe only; cpu fallback is fine
        pass
    codec = BGEM3Codec(dim=DIM, device=device)
    queries = [meta[r["needle"]]["query"] for r, _ in targets]
    t = time.perf_counter()
    Q = np.asarray(codec.encode_batch(queries, task="query"), dtype=np.float32)
    print(f"encoded {len(queries)} queries on {device} in {time.perf_counter()-t:.0f}s", flush=True)

    # ── exhaustive dense ranks ─────────────────────────────────────────
    rows = []
    t = time.perf_counter()
    S = M @ Q.T  # (829k, n_targets)
    print(f"matmul done in {time.perf_counter()-t:.0f}s", flush=True)
    for j, (r, split) in enumerate(targets):
        name = r["needle"]
        gold = [g for g in (gold_by.get(name) or []) if g in idx]
        s = S[:, j]
        if not gold:
            rows.append({"needle": name, "split": split, "error": "no gold vectors in bed"})
            continue
        g_best = max(float(s[idx[g]]) for g in gold)
        rank = int((s > g_best).sum()) + 1
        rows.append({
            "needle": name,
            "split": split,
            "class": meta[name].get("question_type", "unknown"),
            "lexical_rank": r.get("rank_of_first_gold"),
            "dense_rank": rank,
            "dense_score": round(g_best, 4),
        })

    def summarize(split: str):
        sel = [x for x in rows if x.get("split") == split and "error" not in x]
        by_class: dict = {}
        for x in sel:
            by_class.setdefault(x["class"], []).append(x["dense_rank"])
        return {
            "n": len(sel),
            "dense_le12": sum(1 for x in sel if x["dense_rank"] <= 12),
            "dense_le50": sum(1 for x in sel if x["dense_rank"] <= 50),
            "dense_le100": sum(1 for x in sel if x["dense_rank"] <= 100),
            "median_rank": int(np.median([x["dense_rank"] for x in sel])) if sel else None,
            "by_class": {c: {
                "n": len(v),
                "le12": sum(1 for k in v if k <= 12),
                "le50": sum(1 for k in v if k <= 50),
                "median": int(np.median(v)),
            } for c, v in sorted(by_class.items())},
        }

    out = {
        "stamp": "2026-08-31",
        "bed": BED,
        "encoder": {"model": codec.model_name, "dim": DIM, "device": device, "task": "query"},
        "basis_receipt": RECEIPT.name + " (no_pki arm = shipped default post-#370; PRE-wave-1 defaults, disclosed)",
        "design": "dense-ONLY exhaustive cosine over all 829k vectors; rank of first gold; no shortlist, no fusion",
        "miss_summary": summarize("miss"),
        "hit_control_summary": summarize("hit_control"),
        "per_query": rows,
        "wall_s": round(time.perf_counter() - t0, 1),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({"miss": out["miss_summary"], "hit_control": {
        k: v for k, v in out["hit_control_summary"].items() if k != "by_class"}}, indent=1))
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

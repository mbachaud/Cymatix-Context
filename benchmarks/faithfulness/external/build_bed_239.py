"""#239 stage 1 (HELIX env) — build the graded-distractor bed and dump, per gold
needle: the 5 know-features, the continuous know-confidence, the KnowBlock/MissBlock
verdict, and whether the gold answer SURVIVED into expressed_context (the cheap
upper bound on causal_use). Facts are written to real files first (fresh), then
ingested with source_id=abspath so freshness_min=1.0 (no synthetic 'stale').

Tight budget (max_genes_per_turn, expression_tokens) is the survival lever: when
same-entity distractors out-compete the gold under fusion, the gold drops out of
the expressed_context -> answer_survived=False -> a genuine causal_use=0 case.

Output -> np-graph/needles_239_stage1.json  (consumed by the graph env stage 2).
Helix env, read-only retrieval, learn disabled, no graphs.
"""
import os, sys, json, shutil, statistics, argparse
os.environ.setdefault("HELIX_DISABLE_LEARN", "1")
from pathlib import Path

_REPO = Path("f:/Projects/helix-context")
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "benchmarks"))
sys.path.insert(0, "f:/Projects/np-graph")

from needles_239 import NEEDLES_239
from located_n1000 import features_for_query
from cymatix_context.config import load_config
from cymatix_context.context_manager import HelixContextManager
from cymatix_context.server.helpers import _compute_know_or_miss_block
from cymatix_context.scoring.know_calibration import compute_confidence, calibration_from_config

import tempfile
CORPUS = Path(tempfile.gettempdir()) / "bed_239_corpus"
BED = str(Path(tempfile.gettempdir()) / "bed_239.db")
OUT = "f:/Projects/np-graph/needles_239_stage1.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-genes", type=int, default=3)
    ap.add_argument("--expr-tokens", type=int, default=300)
    args = ap.parse_args()

    if os.path.exists(BED):
        os.remove(BED)
    if CORPUS.exists():
        shutil.rmtree(CORPUS)
    CORPUS.mkdir(parents=True, exist_ok=True)

    _cfg_path = _REPO / "cymatix.toml" if (_REPO / "cymatix.toml").exists() else _REPO / "helix.toml"
    cfg = load_config(str(_cfg_path))
    cfg.genome.path = BED
    cfg.budget.max_genes_per_turn = args.max_genes
    cfg.budget.expression_tokens = args.expr_tokens
    mgr = HelixContextManager(cfg)
    cal = calibration_from_config(cfg.know)

    # --- ingest: one real file per fact (gold + distractors), fresh ---
    gold_gene_ids = {}
    n_docs = 0
    for nd in NEEDLES_239:
        bdir = CORPUS / nd["bucket"]
        bdir.mkdir(parents=True, exist_ok=True)
        gp = bdir / f"{nd['id']}__gold.md"
        gp.write_text(nd["gold"] + "\n", encoding="utf-8")
        gids = mgr.ingest(nd["gold"], metadata={"source_id": str(gp.resolve())})
        gold_gene_ids[nd["id"]] = set(gids)
        n_docs += 1
        for j, dtext in enumerate(nd["distractors"]):
            dp = bdir / f"{nd['id']}__d{j:02d}.md"
            dp.write_text(dtext + "\n", encoding="utf-8")
            mgr.ingest(dtext, metadata={"source_id": str(dp.resolve())})
            n_docs += 1
    print(f"ingested {n_docs} docs ({len(NEEDLES_239)} golds) into {BED}", flush=True)
    print(f"budget: max_genes={args.max_genes} expr_tokens={args.expr_tokens} "
          f"fusion={cfg.retrieval.fusion_mode} emit_floor={getattr(cfg.know,'emit_floor',None)}", flush=True)

    rows = []
    for nd in NEEDLES_239:
        f = features_for_query(mgr, nd["q"])
        raw = compute_confidence(
            top_score=f["top_score"], score_gap=f["score_gap"],
            lexical_dense_agree=f["lexical_dense_agree"],
            coordinate_confidence=f["coordinate_confidence"],
            calibration=cal, freshness_min=f["freshness_min"])
        w = mgr.build_context(nd["q"], read_only=True, ignore_delivered=True)
        exp = w.expressed_context or ""
        block = _compute_know_or_miss_block(helix=mgr, window=w, query=nd["q"])
        kind = type(block).__name__
        gset = gold_gene_ids[nd["id"]]
        ranked = f.get("ranked_ids", [])
        gold_rank = next((i + 1 for i, g in enumerate(ranked) if g in gset), -1)
        survived = nd["ans"].strip().lower() in exp.lower()
        rows.append({
            "id": nd["id"], "bucket": nd["bucket"], "q": nd["q"], "ans": nd["ans"],
            "k_distractors": nd["k_distractors"],
            "top_score": round(f["top_score"], 4), "score_gap": round(f["score_gap"], 4),
            "lexical_dense_agree": bool(f["lexical_dense_agree"]),
            "coordinate_confidence": round(float(f["coordinate_confidence"]), 4),
            "freshness_min": f["freshness_min"],
            "raw_confidence": round(float(raw), 4),
            "block_kind": kind,
            "block_confidence": getattr(block, "confidence", None),
            "block_reason": getattr(block, "reason", None),
            "block_found": getattr(block, "found", None),
            "gold_rank": gold_rank,
            "answer_survived": survived,
            "expressed_context": exp,
            "expressed_len": len(exp),
        })
        print(f"  {nd['id']:<9} K={nd['k_distractors']:>2} rank={gold_rank:>2} "
              f"conf={raw:.3f} {kind:<9} top={f['top_score']:.2f} gap={f['score_gap']:.2f} "
              f"agree={f['lexical_dense_agree']} exp_len={len(exp):>4} survived={survived}", flush=True)

    json.dump(rows, open(OUT, "w", encoding="utf-8"), indent=2)

    confs = [r["raw_confidence"] for r in rows]
    surv = [r for r in rows if r["answer_survived"]]
    knowb = [r for r in rows if r["block_kind"] == "KnowBlock"]
    print(f"\n=== STAGE-1 VARIANCE SUMMARY (n={len(rows)}) ===")
    print(f"raw_confidence: min={min(confs):.3f} max={max(confs):.3f} "
          f"mean={statistics.mean(confs):.3f} stdev={statistics.pstdev(confs):.3f}")
    print(f"answer_survived (>= label-1 candidates): {len(surv)}/{len(rows)}")
    print(f"KnowBlock emitted                       : {len(knowb)}/{len(rows)}")
    print(f"gold_rank==1                            : {sum(r['gold_rank']==1 for r in rows)}/{len(rows)}")
    print(f"gold not retrieved (rank -1)            : {sum(r['gold_rank']==-1 for r in rows)}/{len(rows)}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()

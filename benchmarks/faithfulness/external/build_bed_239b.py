"""#239 delivery-balanced stage 1 (HELIX env). Ingest the 3-cell bed (real files
-> fresh), then per needle dump the 5 know-features + confidence + answer_survived
(gold) + competitor_survived + cell. GO/NO-GO checks printed:
  - label balance (answerable survive / heldout drop / competition split)
  - FEATURE SEPARATION: does top_score/gap separate answerable from heldout?
    (if not, the logistic can't discriminate -> redesign before graphs)
Output -> np-graph/needles_239b_stage1.json
"""
import os, sys, json, shutil, statistics, argparse, tempfile
os.environ.setdefault("HELIX_DISABLE_LEARN", "1")
from pathlib import Path

_REPO = Path("f:/Projects/helix-context")
sys.path.insert(0, str(_REPO)); sys.path.insert(0, str(_REPO / "benchmarks"))
sys.path.insert(0, "f:/Projects/np-graph")

from needles_239b import NEEDLES_239B
from located_n1000 import features_for_query
from cymatix_context.config import load_config
from cymatix_context.context_manager import HelixContextManager
from cymatix_context.server.helpers import _compute_know_or_miss_block
from cymatix_context.scoring.know_calibration import compute_confidence, calibration_from_config

CORPUS = Path(tempfile.gettempdir()) / "bed_239b_corpus"
BED = str(Path(tempfile.gettempdir()) / "bed_239b.db")
OUT = "f:/Projects/np-graph/needles_239b_stage1.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-genes", type=int, default=6)
    ap.add_argument("--expr-tokens", type=int, default=600)
    ap.add_argument("--abstain", action="store_true",
                    help="leave the abstain ratio-gate ON (default off) to quantify how many "
                         "answerable rank-1 golds it suppresses to <helix:no_match>")
    args = ap.parse_args()

    if os.path.exists(BED):
        os.remove(BED)
    if CORPUS.exists():
        shutil.rmtree(CORPUS)
    CORPUS.mkdir(parents=True, exist_ok=True)
    cfg = load_config(str(_REPO / "helix.toml"))
    cfg.genome.path = BED
    cfg.budget.max_genes_per_turn = args.max_genes
    cfg.budget.expression_tokens = args.expr_tokens
    # Decouple the abstain RATIO-gate (top_score/mean<1.8) from this study: it
    # otherwise suppresses ALL context (<helix:no_match>) on low-ratio queries —
    # even when the gold is rank-1 — so we could not label what retrieval found.
    # (That suppression is itself a finding; recorded separately. Here we need
    # deliveries to measure causal use of the KNOW-CONFIDENCE signal.)
    cfg.budget.abstain_enabled = bool(args.abstain)
    mgr = HelixContextManager(cfg)
    cal = calibration_from_config(cfg.know)

    gold_gene_ids = {}
    n_docs = 0
    for nd in NEEDLES_239B:
        bdir = CORPUS / nd["cell"]; bdir.mkdir(parents=True, exist_ok=True)
        for j, doc in enumerate(nd["ingest"]):
            fp = bdir / f"{nd['id']}__{j:02d}.md"
            fp.write_text(doc + "\n", encoding="utf-8")
            gids = mgr.ingest(doc, metadata={"source_id": str(fp.resolve())})
            if nd["cell"] in ("answerable", "competition") and doc == nd["gold"]:
                gold_gene_ids[nd["id"]] = set(gids)
            n_docs += 1
    print(f"ingested {n_docs} docs ({len(NEEDLES_239B)} needles) budget mg={args.max_genes} "
          f"fusion={cfg.retrieval.fusion_mode} floor={getattr(cfg.know,'emit_floor',None)}", flush=True)

    rows = []
    for nd in NEEDLES_239B:
        f = features_for_query(mgr, nd["q"])
        raw = compute_confidence(top_score=f["top_score"], score_gap=f["score_gap"],
                                 lexical_dense_agree=f["lexical_dense_agree"],
                                 coordinate_confidence=f["coordinate_confidence"],
                                 calibration=cal, freshness_min=f["freshness_min"])
        w = mgr.build_context(nd["q"], read_only=True, ignore_delivered=True)
        exp = (w.expressed_context or "")
        block = _compute_know_or_miss_block(helix=mgr, window=w, query=nd["q"])
        gset = gold_gene_ids.get(nd["id"], set())
        ranked = f.get("ranked_ids", [])
        gold_rank = next((i + 1 for i, g in enumerate(ranked) if g in gset), -1)
        survived = nd["ans"].strip().lower() in exp.lower()
        abstained = ("no_match" in exp) or ('abstain' in exp)
        comp_surv = (nd["competitor_ans"].strip().lower() in exp.lower()) if nd["competitor_ans"] else None
        rows.append({
            "id": nd["id"], "cell": nd["cell"], "bucket": nd["bucket"], "q": nd["q"],
            "ans": nd["ans"], "competitor_ans": nd["competitor_ans"], "k_distractors": nd["k_distractors"],
            "top_score": round(f["top_score"], 4), "score_gap": round(f["score_gap"], 4),
            "lexical_dense_agree": bool(f["lexical_dense_agree"]),
            "coordinate_confidence": round(float(f["coordinate_confidence"]), 4),
            "freshness_min": f["freshness_min"], "raw_confidence": round(float(raw), 4),
            "block_kind": type(block).__name__, "block_reason": getattr(block, "reason", None),
            "gold_rank": gold_rank, "answer_survived": survived, "abstained": abstained,
            "competitor_survived": comp_surv,
            "expressed_context": exp, "expressed_len": len(exp),
        })
    out_path = OUT if not args.abstain else OUT.replace(".json", "_abstainON.json")
    json.dump(rows, open(out_path, "w", encoding="utf-8"), indent=2)

    def sub(cell):
        return [r for r in rows if r["cell"] == cell]
    ans, held, comp = sub("answerable"), sub("heldout"), sub("competition")
    def stat(rs, key):
        vs = [r[key] for r in rs]
        return f"{statistics.mean(vs):.2f}±{statistics.pstdev(vs):.2f}[{min(vs):.2f},{max(vs):.2f}]"
    print(f"\n=== BALANCE (n={len(rows)}) ===")
    print(f"answerable : survived {sum(r['answer_survived'] for r in ans)}/{len(ans)}  (want ~all)")
    print(f"heldout    : survived {sum(r['answer_survived'] for r in held)}/{len(held)}  (want ~0)")
    print(f"competition: gold-surv {sum(r['answer_survived'] for r in comp)}/{len(comp)}  "
          f"comp-surv {sum(bool(r['competitor_survived']) for r in comp)}/{len(comp)}")
    would_causal = sum(r['answer_survived'] for r in ans) + 0  # competition decided by graph
    print(f"would-be-causal (answerable-survived): {would_causal}; heldout negatives: {len(held)}; "
          f"competition (graph-decided): {len(comp)}")
    print(f"\n=== FEATURE SEPARATION (the GO/NO-GO) ===")
    print(f"top_score  answerable {stat(ans,'top_score')}  vs heldout {stat(held,'top_score')}")
    print(f"score_gap  answerable {stat(ans,'score_gap')}  vs heldout {stat(held,'score_gap')}")
    print(f"agree      answerable {sum(r['lexical_dense_agree'] for r in ans)}/{len(ans)}  "
          f"vs heldout {sum(r['lexical_dense_agree'] for r in held)}/{len(held)}")
    print(f"raw_conf   answerable {stat(ans,'raw_confidence')}  vs heldout {stat(held,'raw_confidence')}")
    if args.abstain:
        print(f"\n=== ABSTAIN SUPPRESSION (abstain gate ON) ===")
        for cell, rs in (("answerable", ans), ("heldout", held), ("competition", comp)):
            ab = sum(r["abstained"] for r in rs)
            r1 = sum(r["abstained"] and r["gold_rank"] == 1 for r in rs)
            print(f"  {cell:12s}: abstained {ab}/{len(rs)}  (of which gold was rank-1: {r1})")
    print(f"-> {out_path}")


if __name__ == "__main__":
    main()

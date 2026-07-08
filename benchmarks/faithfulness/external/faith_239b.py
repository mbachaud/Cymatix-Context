"""#239 delivery-balanced stage 2 (GRAPH env, Qwen3-4B, resumable).

Reads needles_239b_stage1.json and graphs condition B (expressed_context + q) to
label causal_use of the GOLD answer. Cells:
  answerable  : graph a stratified sample -> validate delivered⇒causal; impute 1 for the rest
  heldout     : graph a stratified sample -> validate answer-absent⇒NOT causal; impute 0 for the rest
  competition : graph ALL (gold + wrong sibling both delivered) -> which does the model use?
                also records pB(competitor) so we can see delivered-but-ignored (causal_gold=0).

Non-graphed rows are imputed by cell (flagged). Resume reuses measured graphs.
  ./venv/Scripts/python.exe -X utf8 faith_239b.py --mfn 256 --bs 24 --ids "id1,id2,..."
"""
import os, sys, json, time, argparse
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
from pathlib import Path
import torch
from circuit_tracer import ReplacementModel, attribute
from circuit_tracer.utils import create_graph_files

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, "f:/Projects/helix-context/benchmarks/faithfulness")
from faithfulness_circuit_tracer import answer_logit_node, answer_prob  # noqa: E402
from needle_faithfulness_experiment import find_answer_ctx, retargeted_input_attr  # noqa: E402

IN = HERE / "needles_239b_stage1.json"
OUT = HERE / "needles_239b_faith.json"
_TMP = Path(os.path.join(os.environ.get("TEMP", "."), "ct239b"))
MODEL = None


def build_gen(model_id, transcoders, mfn, bs, node=0.8, edge=0.85):
    global MODEL
    t0 = time.time()
    print(f"loading {model_id} + {transcoders} (bf16)...", flush=True)
    MODEL = ReplacementModel.from_pretrained(model_id, transcoders, dtype=torch.bfloat16,
                                             device="cuda", lazy_encoder=True)
    print(f"  loaded {time.time()-t0:.0f}s | free {torch.cuda.mem_get_info()[0]/1e9:.2f}GB", flush=True)

    def gen(prompt, slug):
        g = attribute(prompt, MODEL, offload="cpu", batch_size=bs, max_feature_nodes=mfn)
        d = _TMP / slug; d.mkdir(parents=True, exist_ok=True)
        create_graph_files(g, slug=slug, output_path=str(d), node_threshold=node, edge_threshold=edge)
        out = None
        for f in d.glob("*.json"):
            try:
                j = json.loads(f.read_bytes().decode("utf-8", errors="replace"))
            except Exception:
                continue
            if isinstance(j, dict) and "nodes" in j:
                out = j; break
        del g
        import gc; gc.collect()
        torch.cuda.empty_cache()
        if out is None:
            raise RuntimeError(f"no graph json in {d}")
        return out
    return gen


def _causal(B, ans):
    """causal_use of `ans` in graph B (pA assumed 0 -> lift=pB)."""
    pB = answer_prob(B, ans)
    node = answer_logit_node(B, ans)
    faith, top = 0.0, False
    if node is not None:
        by_ctx, toks = retargeted_input_attr(B, node)
        span = find_answer_ctx(toks, ans)
        tot = sum(by_ctx.values()) or 1e-9
        faith = sum(by_ctx.get(c, 0.0) for c in span) / tot
        ranked = sorted(by_ctx.items(), key=lambda x: x[1], reverse=True)
        top = bool(ranked and ranked[0][0] in span)
    causal = bool(node is not None and pB >= 0.30 and top)
    return pB, node is not None, round(faith, 4), top, causal


def score(gen, row):
    B = gen(row["expressed_context"] + "\n" + row["q"], f"b-{row['id']}")
    pB, ing, faith, top, causal = _causal(B, row["ans"])
    out = {"id": row["id"], "cell": row["cell"], "gold": row["ans"],
           "pB": round(pB, 4), "in_graph": ing, "faith": faith,
           "answer_is_top_driver": top, "causal_use": causal,
           "expressed_len": row["expressed_len"]}
    if row.get("competitor_ans"):
        cpB, cing, cfaith, ctop, ccausal = _causal(B, row["competitor_ans"])
        out["competitor_ans"] = row["competitor_ans"]
        out["competitor_pB"] = round(cpB, 4)
        out["competitor_causal"] = ccausal
        out["used_competitor_not_gold"] = bool(ccausal and not causal)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3-4B")
    ap.add_argument("--transcoders", default="mwhanna/qwen3-4b-transcoders")
    ap.add_argument("--mfn", type=int, default=256)
    ap.add_argument("--bs", type=int, default=24)
    ap.add_argument("--ids", default="", help="comma-separated ids to graph; others imputed by cell")
    args = ap.parse_args()

    rows = json.load(open(IN, encoding="utf-8"))
    graph_ids = set(x.strip() for x in args.ids.split(",") if x.strip())

    existing = {}
    if OUT.exists():
        try:
            for r in json.load(open(OUT, encoding="utf-8")):
                if r.get("id") and isinstance(r.get("pB"), (int, float)) and not r.get("imputed"):
                    existing[r["id"]] = r
        except Exception:
            pass
    if existing:
        print(f"RESUME: reusing {len(existing)} measured graphs", flush=True)

    gen = build_gen(args.model, args.transcoders, args.mfn, args.bs)

    res = []
    for r in rows:
        gid = r["id"]
        if gid in existing:
            res.append(existing[gid]); json.dump(res, open(OUT, "w"), indent=2); continue
        if graph_ids and gid not in graph_ids:
            if r["cell"] == "competition":
                # competition is graph-DECIDED (gold vs competitor vs neither) — never
                # impute; leave unlabeled so it can't silently enter the refit as a 0.
                res.append({"id": gid, "cell": r["cell"], "gold": r["ans"],
                            "needs_graph": True, "in_graph": None, "pB": None})
                continue
            # impute by cell: answerable delivered=>causal 1; heldout answer-absent=>0
            imp = (r["cell"] == "answerable")
            res.append({"id": gid, "cell": r["cell"], "gold": r["ans"],
                        "causal_use": bool(imp), "imputed": True, "in_graph": None,
                        "pB": None, "faith": None, "expressed_len": r["expressed_len"]})
            continue
        t0 = time.time()
        try:
            m = score(gen, r)
            extra = (f" comp_pB={m.get('competitor_pB')} usedComp={m.get('used_competitor_not_gold')}"
                     if 'competitor_pB' in m else "")
            print(f"[{m['cell'][:4]} {gid:<14}] pB={m['pB']} in_graph={m['in_graph']} "
                  f"faith={m['faith']} top={m['answer_is_top_driver']} CAUSAL={m['causal_use']}{extra} "
                  f"({time.time()-t0:.0f}s)", flush=True)
            res.append(m)
        except Exception as e:
            import traceback; traceback.print_exc()
            res.append({"id": gid, "cell": r["cell"], "error": str(e)})
        json.dump(res, open(OUT, "w"), indent=2)

    # summary
    g = [r for r in res if isinstance(r.get("pB"), (int, float)) and not r.get("imputed")]
    for cell in ("answerable", "heldout", "competition"):
        cg = [r for r in g if r["cell"] == cell]
        if cg:
            c = sum(r["causal_use"] for r in cg)
            print(f"{cell:12s}: graphed {len(cg)} | causal {c}/{len(cg)}")
    comp = [r for r in g if r["cell"] == "competition"]
    if comp:
        uc = sum(r.get("used_competitor_not_gold", False) for r in comp)
        print(f"competition delivered-but-ignored (used competitor not gold): {uc}/{len(comp)}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()

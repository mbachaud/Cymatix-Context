"""#239 stage 2 (GRAPH env) — faithfulness label on the graded-distractor bed.

Reads needles_239_stage1.json (helix env), and for each needle whose gold answer
SURVIVED into helix's expressed_context, runs local circuit-tracer graphs on
A (question only) vs B (expressed_context + question) with Qwen3-4B (the stronger
instrument: scaled causal 23/24). Emits per-needle causal_use — the LABEL the
know-logistic should be calibrated against (#239).

Non-survivors are NOT graphed: the gold answer is absent from context and pA~0
for these synthetic Redwood facts, so the model cannot produce it -> causal_use=0
by construction. We spot-check pA~0 on a sample via --validate-prior.

  ./venv/Scripts/python.exe -X utf8 faith_239.py --mfn 1024 --bs 24
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

IN = HERE / "needles_239_stage1.json"
OUT = HERE / "needles_239_faith.json"
_TMP = Path(os.path.join(os.environ.get("TEMP", "."), "ct239"))
MODEL = None


def build_gen(model_id, transcoders, mfn, bs, node=0.8, edge=0.85):
    global MODEL
    t0 = time.time()
    print(f"loading {model_id} + {transcoders} (bf16)...", flush=True)
    MODEL = ReplacementModel.from_pretrained(model_id, transcoders, dtype=torch.bfloat16,
                                             device="cuda", lazy_encoder=True)
    print(f"  loaded in {time.time()-t0:.0f}s | free {torch.cuda.mem_get_info()[0]/1e9:.2f}GB", flush=True)

    def gen(prompt, slug):
        g = attribute(prompt, MODEL, offload="cpu", batch_size=bs, max_feature_nodes=mfn)
        d = _TMP / slug
        d.mkdir(parents=True, exist_ok=True)
        create_graph_files(g, slug=slug, output_path=str(d), node_threshold=node, edge_threshold=edge)
        out = None
        for f in d.glob("*.json"):
            try:
                j = json.loads(f.read_bytes().decode("utf-8", errors="replace"))
            except Exception:
                continue
            if isinstance(j, dict) and "nodes" in j:
                out = j
                break
        del g
        torch.cuda.empty_cache()
        if out is None:
            raise RuntimeError(f"no graph json in {d}")
        return out
    return gen


def score(gen, row, mfn, bs, run_a=True):
    q, ans, exp = row["q"], row["ans"], row["expressed_context"]
    pA = 0.0
    if run_a:
        A = gen(q, f"a-{row['id']}")
        pA = answer_prob(A, ans)
    B = gen(exp + "\n" + q, f"b-{row['id']}")
    pB = answer_prob(B, ans)
    ans_node = answer_logit_node(B, ans)
    faith, top, span = 0.0, False, []
    if ans_node is not None:
        by_ctx, toks = retargeted_input_attr(B, ans_node)
        span = find_answer_ctx(toks, ans)
        tot = sum(by_ctx.values()) or 1e-9
        faith = sum(by_ctx.get(c, 0.0) for c in span) / tot
        ranked = sorted(by_ctx.items(), key=lambda x: x[1], reverse=True)
        top = bool(ranked and ranked[0][0] in span)
    lift = pB - pA
    return {"id": row["id"], "gold": ans, "expressed_len": row["expressed_len"],
            "pA": round(pA, 4), "pB": round(pB, 4), "lift": round(lift, 4),
            "in_graph": ans_node is not None, "faith": round(faith, 4),
            "answer_is_top_driver": top,
            "causal_use": bool(ans_node is not None and lift >= 0.15 and pB >= 0.30 and top)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3-4B")
    ap.add_argument("--transcoders", default="mwhanna/qwen3-4b-transcoders")
    ap.add_argument("--mfn", type=int, default=1024)
    ap.add_argument("--bs", type=int, default=24)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="", help="comma-separated survivor ids to graph; "
                    "un-graphed survivors get causal_use=1 imputed (flagged)")
    ap.add_argument("--no-a", action="store_true", help="skip A-condition; assume pA=0 (Redwood facts)")
    ap.add_argument("--validate-prior", type=int, default=0,
                    help="run A on the first N NON-survivors to confirm pA~0")
    args = ap.parse_args()

    rows = json.load(open(IN, encoding="utf-8"))
    if args.limit:
        rows = rows[:args.limit]
    survivors = [r for r in rows if r["answer_survived"]]
    nonsurv = [r for r in rows if not r["answer_survived"]]
    print(f"{len(rows)} needles | {len(survivors)} survivors (graph) | "
          f"{len(nonsurv)} non-survivors (causal_use=0 by construction)", flush=True)

    # --- RESUME: reuse graphs already measured in a prior (crashed) run ---
    existing = {}
    if OUT.exists():
        try:
            for r in json.load(open(OUT, encoding="utf-8")):
                if (r.get("id") and isinstance(r.get("pB"), (int, float))
                        and not r.get("imputed") and not r.get("skipped_graph")
                        and "error" not in r):
                    existing[r["id"]] = r
        except Exception:
            pass
    if existing:
        print(f"RESUME: reusing {len(existing)} already-measured graphs: "
              f"{sorted(existing)}", flush=True)

    gen = build_gen(args.model, args.transcoders, args.mfn, args.bs)

    # optional: confirm the model cannot guess the answer without context
    # (skip on resume — already validated in the original run)
    if args.validate_prior and not existing:
        print("--- prior check (pA on non-survivors) ---", flush=True)
        for r in nonsurv[:args.validate_prior]:
            A = gen(r["q"], f"prior-{r['id']}")
            print(f"  {r['id']:<9} pA={answer_prob(A, r['ans']):.4f}", flush=True)

    graph_ids = set(x.strip() for x in args.ids.split(",") if x.strip())

    res = []
    for r in rows:
        if not r["answer_survived"]:
            res.append({"id": r["id"], "answer_survived": False,
                        "expressed_len": r["expressed_len"], "causal_use": False,
                        "in_graph": False, "pB": 0.0, "lift": 0.0, "faith": 0.0,
                        "answer_is_top_driver": False, "skipped_graph": True})
            continue
        if graph_ids and r["id"] not in graph_ids:
            # survivor not in the graphed subset -> impute causal_use=1
            # (licensed by the graphed stratified sample; delivered => used)
            res.append({"id": r["id"], "answer_survived": True,
                        "expressed_len": r["expressed_len"], "causal_use": True,
                        "in_graph": None, "pB": None, "lift": None, "faith": None,
                        "answer_is_top_driver": None, "imputed": True})
            continue
        if r["id"] in existing:                     # RESUME: already graphed
            res.append(existing[r["id"]])
            json.dump(res, open(OUT, "w"), indent=2)
            continue
        t0 = time.time()
        try:
            m = score(gen, r, args.mfn, args.bs, run_a=not args.no_a)
            m["answer_survived"] = True
            print(f"[{r['id']:<9}] len={r['expressed_len']:>4} pA={m['pA']} pB={m['pB']} "
                  f"lift={m['lift']} in_graph={m['in_graph']} faith={m['faith']} "
                  f"top={m['answer_is_top_driver']} CAUSAL={m['causal_use']} ({time.time()-t0:.0f}s)", flush=True)
            res.append(m)
        except Exception as e:
            import traceback; traceback.print_exc()
            res.append({"id": r["id"], "answer_survived": True, "error": str(e)})
        json.dump(res, open(OUT, "w"), indent=2)

    scored = [r for r in res if "causal_use" in r and "error" not in r]
    causal = [r for r in scored if r["causal_use"]]
    graphed = [r for r in scored if not r.get("skipped_graph")]
    print(f"\n=== #239 FAITHFULNESS LABELS ({args.model}) ===")
    print(f"needles labeled     : {len(scored)}")
    print(f"causal_use = 1       : {len(causal)}/{len(scored)} = {len(causal)/max(1,len(scored)):.2f}")
    if graphed:
        gc = [r for r in graphed if r['causal_use']]
        faiths = [r['faith'] for r in graphed if isinstance(r.get('faith'), (int, float))]
        print(f"graphed survivors    : {len(graphed)} | causal among graphed {len(gc)}/{len(graphed)} = {len(gc)/len(graphed):.2f}")
        if faiths:
            print(f"mean faith (graphed) : {sum(faiths)/len(faiths):.3f}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()

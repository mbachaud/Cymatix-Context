"""Scaled faithfulness runner — parametrized by model so we can run the same
needle set on gemma-2-2b OR Qwen3-4B (stronger instrument). Reuses the validated
scoring (nfe.score_needle) via a monkeypatched local gen_graph.

  python -X utf8 faith_scaled.py --model Qwen/Qwen3-4B \
      --transcoders mwhanna/qwen3-4b-transcoders --needles scaled --mfn 1024 --bs 24
"""
import os, sys, json, time, argparse, tempfile
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
from pathlib import Path
import torch
from circuit_tracer import ReplacementModel, attribute
from circuit_tracer.utils import create_graph_files

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, "f:/Projects/helix-context/benchmarks/faithfulness")
import faithfulness_circuit_tracer as fct      # noqa: E402
import needle_faithfulness_experiment as nfe   # noqa: E402
from scaled_needles import SCALED_NEEDLES       # noqa: E402

_OUT = Path(tempfile.mkdtemp(prefix="ctscaled_"))
MODEL = None


def build_gen(model_id, transcoders, mfn, bs, node=0.8, edge=0.85):
    global MODEL
    t0 = time.time()
    print(f"loading {model_id} + {transcoders} (bf16)...", flush=True)
    MODEL = ReplacementModel.from_pretrained(model_id, transcoders, dtype=torch.bfloat16,
                                             device="cuda", lazy_encoder=True)
    print(f"  loaded in {time.time()-t0:.0f}s | free {torch.cuda.mem_get_info()[0]/1e9:.2f}GB", flush=True)

    def gen(prompt, slug, *a, **k):
        g = attribute(prompt, MODEL, offload="cpu", batch_size=bs, max_feature_nodes=mfn)
        d = _OUT / slug
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3-4B")
    ap.add_argument("--transcoders", default="mwhanna/qwen3-4b-transcoders")
    ap.add_argument("--needles", choices=("scaled", "pilot"), default="scaled")
    ap.add_argument("--mfn", type=int, default=1024)
    ap.add_argument("--bs", type=int, default=24)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--label", default="scaled")
    args = ap.parse_args()

    gen = build_gen(args.model, args.transcoders, args.mfn, args.bs)
    fct.gen_graph = gen
    nfe.gen_graph = gen

    needles = SCALED_NEEDLES if args.needles == "scaled" else nfe.NEEDLES
    if args.limit:
        needles = needles[:args.limit]
    out_path = str(HERE / f"scaled_results_{args.label}.json")

    results = []
    for i, nd in enumerate(needles):
        t0 = time.time()
        try:
            r = nfe.score_needle(nd)
        except Exception as e:
            import traceback; traceback.print_exc()
            r = {"id": nd["id"], "error": str(e)}
        results.append(r)
        json.dump(results, open(out_path, "w"), indent=2)
        if "error" not in r:
            print(f"[{i+1}/{len(needles)} {r['id']}] pA={r['pA']} pB={r['pB']} lift={r['lift']} "
                  f"argmax={r['b_argmax_answer']} in_graph={r['in_graph']} faith={r['faith']} "
                  f"top={r['answer_is_top_driver']} CAUSAL={r['causal_use']} ({time.time()-t0:.0f}s)", flush=True)

    ok = [r for r in results if "error" not in r]
    ing = [r for r in ok if r["in_graph"]]
    informative = [r for r in ing if r["pA"] < 0.2]          # model didn't already know it
    causal = [r for r in ok if r["causal_use"]]
    print(f"\n=== SCALED SUMMARY [{args.model}] ({args.label}) ===")
    print(f"needles                : {len(ok)}/{len(needles)}")
    print(f"answer in-graph         : {len(ing)}/{len(ok)}")
    print(f"informative (pA<0.2)    : {len(informative)}/{len(ing)}")
    if ing:
        print(f"answer-is-top-driver    : {sum(r['answer_is_top_driver'] for r in ing)}/{len(ing)}")
        print(f"causal-use rate         : {len(causal)}/{len(ok)} = {len(causal)/len(ok):.2f}")
        print(f"mean P-lift (in-graph)  : {sum(r['lift'] for r in ing)/len(ing):.3f}")
        print(f"mean faith  (in-graph)  : {sum(r['faith'] for r in ing)/len(ing):.3f}")
    print(f"-> {out_path}")


if __name__ == "__main__":
    main()

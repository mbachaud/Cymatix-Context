"""Local faithfulness pipeline — gemma-2-2b graphs via circuit-tracer (no
Neuronpedia HTTP, no rate limit, no egress).

create_graph_files emits the EXACT Neuronpedia JSON schema our validated harness
parses, so we monkeypatch a local gen_graph into the committed scoring code and
reuse backward_influence / answer_logit_node / faithfulness / score_needle
verbatim. Loads the model once, then reproduces the 6-needle ideal-context pilot.
"""
import os, sys, json, time, tempfile, argparse
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
from pathlib import Path
import torch

# reuse the committed, validated metric + needle set
HARNESS = Path("f:/Projects/helix-context/benchmarks/faithfulness")
sys.path.insert(0, str(HARNESS))
import faithfulness_circuit_tracer as fct        # noqa: E402
import needle_faithfulness_experiment as nfe     # noqa: E402

from circuit_tracer import ReplacementModel, attribute      # noqa: E402
from circuit_tracer.utils import create_graph_files         # noqa: E402

_OUT = Path(tempfile.mkdtemp(prefix="ctgraphs_"))
_MODEL = None


def load_model():
    global _MODEL
    if _MODEL is None:
        t0 = time.time()
        print("loading gemma-2-2b (bf16, lazy encoders)...", flush=True)
        _MODEL = ReplacementModel.from_pretrained(
            "google/gemma-2-2b", "gemma", dtype=torch.bfloat16,
            device="cuda", lazy_encoder=True)
        print(f"  loaded in {time.time()-t0:.0f}s", flush=True)
    return _MODEL


def local_gen_graph(prompt, slug, model="gemma-2-2b",
                    node_threshold=0.8, edge_threshold=0.85,
                    max_feature_nodes=4096, batch_size=48):
    """Drop-in for fct.gen_graph: returns the graph JSON dict (nodes/links/metadata)."""
    m = load_model()
    g = attribute(prompt, m, offload="cpu", batch_size=batch_size,
                  max_feature_nodes=max_feature_nodes)
    d = _OUT / slug
    d.mkdir(parents=True, exist_ok=True)
    create_graph_files(g, slug=slug, output_path=str(d),
                       node_threshold=node_threshold, edge_threshold=edge_threshold)
    def robust_load(path):
        raw = path.read_bytes()
        for enc in ("utf-8", "cp1252"):  # create_graph_files writes Windows-default cp1252
            try:
                return json.loads(raw.decode(enc))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return json.loads(raw.decode("utf-8", errors="replace"))

    graph_json = None
    for f in d.glob("*.json"):
        try:
            j = robust_load(f)
        except Exception:
            continue
        if isinstance(j, dict) and "nodes" in j:
            graph_json = j
            break
    del g
    torch.cuda.empty_cache()
    if graph_json is None:
        raise RuntimeError(f"no graph json produced in {d}")
    return graph_json


# monkeypatch the local generator into BOTH modules' namespaces
fct.gen_graph = local_gen_graph
nfe.gen_graph = local_gen_graph


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(_OUT.parent / "faith_local_results.json"))
    args = ap.parse_args()

    load_model()
    needles = nfe.NEEDLES[:args.limit] if args.limit else nfe.NEEDLES
    results = []
    for nd in needles:
        print(f"[{nd['id']}] A+B ...", flush=True)
        t0 = time.time()
        try:
            r = nfe.score_needle(nd)   # validated scoring, now on local graphs
        except Exception as e:
            import traceback; traceback.print_exc()
            r = {"id": nd["id"], "error": str(e)}
        results.append(r)
        json.dump(results, open(args.out, "w"), indent=2)
        if "error" not in r:
            print(f"  pA={r['pA']} pB={r['pB']} lift={r['lift']} argmax={r['b_argmax_answer']} "
                  f"faith={r['faith']} top_driver_is_answer={r['answer_is_top_driver']} "
                  f"CAUSAL={r['causal_use']}  ({time.time()-t0:.0f}s)", flush=True)

    ok = [r for r in results if "error" not in r]
    ing = [r for r in ok if r["in_graph"]]
    causal = [r for r in ok if r["causal_use"]]
    print("\n=== LOCAL PILOT SUMMARY (gemma-2-2b, circuit-tracer, no egress) ===")
    print(f"needles run           : {len(ok)}/{len(needles)}")
    print(f"answer in-graph        : {len(ing)}/{len(ok)}")
    if ok:
        print(f"causal-use rate        : {len(causal)}/{len(ok)} = {len(causal)/len(ok):.2f}")
    if ing:
        print(f"mean P-lift (in-graph) : {sum(r['lift'] for r in ing)/len(ing):.3f}")
        print(f"mean faith  (in-graph) : {sum(r['faith'] for r in ing)/len(ing):.3f}")
        print(f"answer-is-top-driver   : {sum(r['answer_is_top_driver'] for r in ing)}/{len(ing)}")
    print(f"results -> {args.out}")


if __name__ == "__main__":
    main()

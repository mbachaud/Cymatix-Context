"""Stage 2 of local real-helix faithfulness (np-graph venv).

Consumes expressed_contexts.json (dumped by dump_expressed.py in the helix env)
and runs local circuit-tracer graphs on A (question) vs B (helix's REAL
expressed_context + question), computing the retargeted faithfulness metric.
Completes the #239 payoff that was blocked on the Neuronpedia rate limit.

Real-helix prompts are long (~350 tok) so max_feature_nodes/batch_size are
smaller than the short-needle pilot; tune with --mfn/--bs if VRAM is tight.
"""
import os, sys, json, time, argparse
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import faith_local  # noqa: E402  (monkeypatch + model + local_gen_graph)
from faith_local import local_gen_graph, load_model  # noqa: E402

sys.path.insert(0, "f:/Projects/helix-context/benchmarks/faithfulness")
from faithfulness_circuit_tracer import answer_logit_node, answer_prob  # noqa: E402
from needle_faithfulness_experiment import find_answer_ctx, retargeted_input_attr  # noqa: E402

EXP = "f:/Projects/np-graph/expressed_contexts.json"
OUT = "f:/Projects/np-graph/realhelix_local_results.json"


def score(nd, expressed, mfn, bs):
    A = local_gen_graph(nd["q"], f"rh-a-{nd['id']}", max_feature_nodes=mfn, batch_size=bs)
    B = local_gen_graph(expressed + "\n" + nd["q"], f"rh-b-{nd['id']}",
                        max_feature_nodes=mfn, batch_size=bs)
    ans = nd["ans"]
    pA, pB = answer_prob(A, ans), answer_prob(B, ans)
    b_argmax = any(n.get("is_target_logit") and ans.strip().lower() in str(n.get("clerp", "")).lower()
                   for n in B["nodes"] if n.get("feature_type") == "logit")
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
    return {"id": nd["id"], "gold": ans, "expressed_len": nd["expressed_len"],
            "pA": round(pA, 4), "pB": round(pB, 4), "lift": round(lift, 4),
            "b_argmax_answer": bool(b_argmax), "in_graph": ans_node is not None,
            "faith": round(faith, 4), "answer_is_top_driver": top,
            "causal_use": bool(ans_node is not None and lift >= 0.15 and pB >= 0.30 and top)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mfn", type=int, default=1024)
    ap.add_argument("--bs", type=int, default=24)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    data = json.load(open(EXP, encoding="utf-8"))
    if args.limit:
        data = data[:args.limit]
    load_model()
    res = []
    for nd in data:
        if not nd["answer_survived"]:
            print(f"[{nd['id']}] answer did NOT survive retrieval; skip graph", flush=True)
            res.append({**{k: nd[k] for k in ('id', 'expressed_len')}, "answer_survived": False, "skipped": True})
            continue
        t0 = time.time()
        try:
            r = score(nd, nd["expressed_context"], args.mfn, args.bs)
            print(f"[{nd['id']}] len={nd['expressed_len']} pA={r['pA']} pB={r['pB']} "
                  f"lift={r['lift']} argmax={r['b_argmax_answer']} faith={r['faith']} "
                  f"top={r['answer_is_top_driver']} CAUSAL={r['causal_use']} ({time.time()-t0:.0f}s)", flush=True)
            res.append(r)
        except Exception as e:
            import traceback; traceback.print_exc()
            res.append({"id": nd["id"], "error": str(e)})
        json.dump(res, open(OUT, "w"), indent=2)

    surv = [r for r in res if not r.get("skipped")]
    scored = [r for r in surv if "faith" in r]
    causal = [r for r in scored if r.get("causal_use")]
    print("\n=== REAL-HELIX LOCAL SUMMARY (gemma-2-2b, circuit-tracer, no egress) ===")
    print(f"answer survived retrieval : {len(surv)}/{len(res)}")
    if scored:
        print(f"causal-use | survived      : {len(causal)}/{len(scored)} = {len(causal)/len(scored):.2f}")
        print(f"mean lift  | survived      : {sum(r['lift'] for r in scored)/len(scored):.3f}")
        print(f"mean faith | survived      : {sum(r['faith'] for r in scored)/len(scored):.3f}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()

"""N-needle faithfulness experiment via Neuronpedia Circuit-Tracer.

Scales the single-needle probe to a needle SET, with two fixes learned from
diagnosis (diag_target/diag_notrail/diag_retarget):
  * NO trailing space  -> the answer token (leading-space) is the natural
    continuation; a lone trailing space makes gemma-2-2b emit markup (<strong>).
  * RETARGET to the answer logit -> gemma-2-2b's prior often outranks the
    injected answer (predicts "red" though context says "teal"), but the answer
    logit is still in the top-k graph WITH incoming edges, so we attribute FROM
    it directly. Decouples the faithfulness measurement from the 2B model's
    prior-competition weakness.

Design: document-style context + natural question (mirrors real helix output,
not trivial verbatim Q/A). Prior-free distinctive single-token answers so the
answer logit lands in-graph. Egress = synthetic "Redwood Inference" only.

PER-NEEDLE METRICS:
  pA, pB          : P(answer token) with question-only vs context+question
  lift            : pB - pA            (behavioral: did context raise the answer?)
  b_argmax        : is the answer the top predicted token in B?
  faith           : fraction of the ANSWER logit's input attribution (retargeted)
                    localized to the answer token in the injected context
  answer_is_top   : is that context answer-token the #1 input driver? (copy signal)
  in_graph        : answer logit present in B's graph (mechanistic metric defined)

CAUSAL USE (per needle) := in_graph AND lift >= 0.15 AND pB >= 0.30 AND answer_is_top
  i.e. context both RAISED the answer to a real probability AND the answer logit
  mechanistically READS the injected answer token.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from faithfulness_circuit_tracer import (
    gen_graph, backward_influence, answer_logit_node, answer_prob, _STRUCTURAL,
)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "needle_faith_results.json")

# Synthetic "Redwood Inference" facts. Distinctive, prior-free, single-token
# answers (otter/mango/platinum/walrus/cobalt) so the answer logit lands in-graph;
# 'teal' kept as a deliberate prior-competition stress case for the retarget path.
NEEDLES = [
    {"id": "beacon_mascot",
     "ctx": "Redwood Inference wiki entry: the Beacon module team mascot is the otter.",
     "q": "According to Redwood Inference, the Beacon module team mascot is the",
     "ans": "otter"},
    {"id": "prism_codename",
     "ctx": "Redwood Inference release notes: the Prism analytics engine is codenamed mango.",
     "q": "In Redwood Inference, the Prism analytics engine is codenamed",
     "ans": "mango"},
    {"id": "sentinel_tier",
     "ctx": "Redwood Inference SLA doc: the Sentinel service runs on the platinum support tier.",
     "q": "At Redwood Inference, the Sentinel service runs on the support tier called",
     "ans": "platinum"},
    {"id": "cascade_codename",
     "ctx": "Redwood Inference runbook: the Cascade ingestion module is codenamed raven.",
     "q": "In the Redwood Inference runbook, the Cascade ingestion module is codenamed",
     "ans": "raven"},
    {"id": "harbor_zone",
     "ctx": "Redwood Inference network map: the Harbor gateway sits in the availability zone named cobalt.",
     "q": "At Redwood Inference, the Harbor gateway sits in the availability zone named",
     "ans": "cobalt"},
    {"id": "atlas_color",
     "ctx": "Redwood Inference ops note: the Atlas service status dashboard is colored teal.",
     "q": "In Redwood Inference, the Atlas service status dashboard is colored",
     "ans": "teal"},
]


def find_answer_ctx(toks, ans):
    """Positions whose detokenized token == ans (the answer appears only in the
    context; questions are written to exclude it)."""
    a = ans.strip().lower()
    return [i for i, t in enumerate(toks) if str(t).strip().lower() == a]


def retargeted_input_attr(g, ans_node):
    """Per-ctx-idx input attribution of the ANSWER logit (structural excluded)."""
    toks = g["metadata"].get("prompt_tokens") or []
    nodes, infl, _ = backward_influence(g, targets=[ans_node])
    by_ctx = defaultdict(float)
    for nid, n in nodes.items():
        if n.get("feature_type") != "embedding":
            continue
        c = n.get("ctx_idx")
        if c is None or str(toks[c] if c < len(toks) else "") in _STRUCTURAL:
            continue
        by_ctx[c] += infl.get(nid, 0.0)
    return by_ctx, toks


def score_needle(nd):
    ts = int(time.time()) % 100000
    A = gen_graph(nd["q"], f"nf-a-{nd['id']}-{ts}")               # question only
    B = gen_graph(nd["ctx"] + " " + nd["q"], f"nf-b-{nd['id']}-{ts}")  # context + question

    ans = nd["ans"]
    pA, pB = answer_prob(A, ans), answer_prob(B, ans)
    b_argmax = any(n.get("is_target_logit") and ans.strip().lower()
                   in str(n.get("clerp", "")).lower()
                   for n in B["nodes"] if n.get("feature_type") == "logit")

    ans_node = answer_logit_node(B, ans)
    in_graph = ans_node is not None
    faith, answer_is_top, span, drivers = 0.0, False, [], []
    if in_graph:
        by_ctx, toks = retargeted_input_attr(B, ans_node)
        span = find_answer_ctx(toks, ans)
        tot = sum(by_ctx.values()) or 1e-9
        faith = sum(by_ctx.get(c, 0.0) for c in span) / tot
        ranked = sorted(by_ctx.items(), key=lambda x: x[1], reverse=True)
        answer_is_top = bool(ranked and ranked[0][0] in span)
        drivers = [(c, str(toks[c]) if c < len(toks) else "?", round(v / tot, 3))
                   for c, v in ranked[:5]]

    lift = pB - pA
    causal_use = in_graph and lift >= 0.15 and pB >= 0.30 and answer_is_top
    return {
        "id": nd["id"], "gold": ans,
        "pA": round(pA, 4), "pB": round(pB, 4), "lift": round(lift, 4),
        "b_argmax_answer": bool(b_argmax),
        "in_graph": in_graph, "answer_ctx_span": span,
        "faith": round(faith, 4), "answer_is_top_driver": answer_is_top,
        "causal_use": bool(causal_use), "b_top_drivers": drivers,
    }


def main():
    results = []
    for nd in NEEDLES:
        print(f"[{nd['id']}] A+B ...", flush=True)
        try:
            r = score_needle(nd)
        except Exception as e:
            print(f"  FAILED: {e}", flush=True)
            r = {"id": nd["id"], "error": str(e)}
        results.append(r)
        with open(OUT, "w") as f:
            json.dump(results, f, indent=2)
        if "error" not in r:
            print(f"  pA={r['pA']} pB={r['pB']} lift={r['lift']} argmax={r['b_argmax_answer']} "
                  f"faith={r['faith']} top_driver_is_answer={r['answer_is_top_driver']} "
                  f"CAUSAL={r['causal_use']}", flush=True)

    ok = [r for r in results if "error" not in r]
    ing = [r for r in ok if r["in_graph"]]
    causal = [r for r in ok if r["causal_use"]]
    print("\n=== SUMMARY ===")
    print(f"needles run           : {len(ok)}/{len(NEEDLES)}")
    print(f"answer in-graph        : {len(ing)}/{len(ok)}")
    if ok:
        print(f"causal-use rate        : {len(causal)}/{len(ok)} = {len(causal)/len(ok):.2f}")
    if ing:
        print(f"mean P-lift (in-graph) : {sum(r['lift'] for r in ing)/len(ing):.3f}")
        print(f"mean faith  (in-graph) : {sum(r['faith'] for r in ing)/len(ing):.3f}")
        print(f"answer-is-top-driver   : {sum(r['answer_is_top_driver'] for r in ing)}/{len(ing)}")
    print(f"results -> {OUT}")


if __name__ == "__main__":
    main()

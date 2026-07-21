"""ACTION 3 — faithfulness on helix's REAL output.

Instead of hand-written context, condition B injects helix's actual
`build_context().expressed_context` (shipped config: dense+SPLADE on, rrf,
splice fix). This is the true "test helix's output" step. It separates two
failure modes the know/miss contract conflates:

  RETRIEVAL-PRESERVATION : did the answer token survive retrieve->splice->assemble
                           into expressed_context at all? (helix's job)
  FAITHFULNESS           : given it survived, does the model causally read it?
                           (measured mechanistically, retargeted answer logit)

Pipeline: fresh tiny bed -> ingest the 6 synthetic facts -> per needle,
build_context(read_only) -> expressed_context -> A/B graphs -> metric suite.

Egress: synthetic "Redwood Inference" facts only (same as the hand-context run).
Read-only + HELIX_DISABLE_LEARN so the bench bed is never mutated.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HELIX_DISABLE_LEARN", "1")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from faithfulness_circuit_tracer import (  # noqa: E402
    gen_graph, answer_logit_node, answer_prob,
)
from needle_faithfulness_experiment import (  # noqa: E402
    NEEDLES, find_answer_ctx, retargeted_input_attr,
)

from cymatix_context.config import load_config  # noqa: E402
from cymatix_context.context_manager import HelixContextManager  # noqa: E402

OUT = str(_REPO / "benchmarks" / "results" / "real_helix_faith_results.json")
import tempfile
BED = str(Path(tempfile.gettempdir()) / "faith_needle_bed.db")


def build_bed():
    """Fresh bed with only the 6 synthetic facts -> short expressed_context."""
    if os.path.exists(BED):
        os.remove(BED)
    _cfg_path = _REPO / "cymatix.toml" if (_REPO / "cymatix.toml").exists() else _REPO / "helix.toml"
    cfg = load_config(str(_cfg_path))
    cfg.genome.path = BED
    mgr = HelixContextManager(cfg)
    for nd in NEEDLES:
        mgr.ingest(nd["ctx"], metadata={"source_id": nd["id"]})
    return mgr


def score(nd, expressed):
    ts = int(time.time()) % 100000
    A = gen_graph(nd["q"], f"rh-a-{nd['id']}-{ts}")
    B = gen_graph(expressed + "\n" + nd["q"], f"rh-b-{nd['id']}-{ts}")
    ans = nd["ans"]
    pA, pB = answer_prob(A, ans), answer_prob(B, ans)
    b_argmax = any(n.get("is_target_logit") and ans.strip().lower()
                   in str(n.get("clerp", "")).lower()
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
    return {
        "pA": round(pA, 4), "pB": round(pB, 4), "lift": round(lift, 4),
        "b_argmax_answer": bool(b_argmax), "in_graph": ans_node is not None,
        "faith": round(faith, 4), "answer_is_top_driver": top,
        "answer_ctx_span": span,
        "causal_use": bool(ans_node is not None and lift >= 0.15 and pB >= 0.30 and top),
    }


def main():
    print("building fresh needle bed + ingesting 6 facts (loads dense/SPLADE) ...", flush=True)
    mgr = build_bed()
    results = []
    for nd in NEEDLES:
        w = mgr.build_context(nd["q"], read_only=True, ignore_delivered=True)
        expressed = w.expressed_context or ""
        answer_survived = nd["ans"].strip().lower() in expressed.lower()
        rec = {"id": nd["id"], "gold": nd["ans"],
               "expressed_len": len(expressed),
               "answer_survived_retrieval": answer_survived,
               "expressed_preview": expressed[:280]}
        print(f"[{nd['id']}] expressed_len={len(expressed)} survived={answer_survived}", flush=True)
        if answer_survived:
            try:
                rec.update(score(nd, expressed))
                print(f"   pA={rec['pA']} pB={rec['pB']} lift={rec['lift']} "
                      f"argmax={rec['b_argmax_answer']} faith={rec['faith']} "
                      f"top_driver_is_answer={rec['answer_is_top_driver']} CAUSAL={rec['causal_use']}",
                      flush=True)
            except Exception as e:
                rec["error"] = str(e)
                print(f"   graph FAILED: {e}", flush=True)
        results.append(rec)
        with open(OUT, "w") as f:
            json.dump(results, f, indent=2)

    surv = [r for r in results if r.get("answer_survived_retrieval")]
    scored = [r for r in surv if "faith" in r]
    causal = [r for r in scored if r.get("causal_use")]
    print("\n=== REAL-HELIX SUMMARY ===")
    print(f"answer survived retrieval : {len(surv)}/{len(results)}")
    if scored:
        print(f"causal-use | survived      : {len(causal)}/{len(scored)} = {len(causal)/len(scored):.2f}")
        print(f"mean lift  | survived      : {sum(r['lift'] for r in scored)/len(scored):.3f}")
        print(f"mean faith | survived      : {sum(r['faith'] for r in scored)/len(scored):.3f}")
    print(f"results -> {OUT}")


if __name__ == "__main__":
    main()

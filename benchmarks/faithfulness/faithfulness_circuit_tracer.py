"""Helix faithfulness probe via Neuronpedia's Circuit-Tracer (Attribution Graph) API.

PROVEN 2026-07-06 (single-needle, real helix fact port=11437):
  Condition A (question only)      -> gemma-2-2b predicts "8" (p=.42, hallucinated)
  Condition B (helix ctx injected) -> predicts "1" (p=.75) = first digit of 11437,
                                      and the answer traces to ctx 11 = the '1' of
                                      "11437" in the injected fact.

WHY: measures whether a model CAUSALLY USES helix's injected context (faithfulness)
vs pattern-matches from weights — the mechanistic ground truth #239's know/miss
"found=true" contract has never had. This instrument is the yardstick the retrieval
work (complement/DNA-pair, ANN threshold) will be measured by when we return to it.

API: POST https://www.neuronpedia.org/api/graph/generate {modelId,"gemma-2-2b", prompt, slug}
     -> {s3url, numNodes, numLinks}; fetch s3url -> graph JSON.
     Anonymous works (no key) BUT saves to a PUBLIC S3 bucket. For private/rate-limited
     runs set NEURONPEDIA_API_KEY env var and pass header x-api-key (NEVER hardcode).
     Egress authorized: low-sensitivity only (public repo facts + synthetic ERB).

Graph JSON: nodes[{node_id, feature_type, ctx_idx, is_target_logit, influence, clerp}],
     links[{source, target, weight}] (~54k), metadata.prompt_tokens (token list).
     feature_type: "embedding"=input tokens (E_ctx), "cross layer transcoder"=features,
     "mlp reconstruction error", "logit". Target = node with is_target_logit=True.

METRIC: backward-propagate influence from the target-logit over links -> per-input-token
     attribution; faithfulness = fraction of input attribution localized to the injected
     context span (by ctx_idx). REFINEMENTS TODO: exclude structural tokens (<bos>,
     trailing space dominate raw attribution — artifact); prefer single-token-answer
     needles (gemma splits "11437" into digits so target is only "1"); consider the
     A->B attribution DELTA as the cleaner score.

NEXT ACTIONS (thread 1):
  1. Add trailing-space prompt shaping so target logit = answer token (done here).
  2. Add structural-token exclusion to the localization metric.
  3. Build a NEEDLE set (question, injected_context, answer) from SIKE helix-internal
     facts (public) + synthetic ERB; then swap injected_context for REAL helix
     build_context() outputs.
  4. Run N needles -> causal-use rate; write a faithfulness note feeding #239.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict

NP = "https://www.neuronpedia.org/api/graph/generate"


def gen_graph(prompt: str, slug: str, model: str = "gemma-2-2b",
              max_retries: int = 7, pace_s: float = 5.0) -> dict:
    """Generate an attribution graph. Retries on HTTP 429 (anonymous tier is
    rate-limited) with exponential backoff (capped 120s); paces every call by
    pace_s so batch runs don't trip the limiter. The anonymous quota is a long
    rolling window (~15-20 calls exhaust it) — set NEURONPEDIA_API_KEY for a
    much higher limit; batch faithfulness runs (12+ calls) effectively need it."""
    body = json.dumps({"modelId": model, "prompt": prompt, "slug": slug}).encode()
    headers = {"content-type": "application/json"}
    key = os.environ.get("NEURONPEDIA_API_KEY")
    if key:
        headers["x-api-key"] = key  # private/account storage + higher rate limits
    for attempt in range(max_retries):
        req = urllib.request.Request(NP, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                back = min(120.0, pace_s * (2 ** attempt))  # 5,10,20,40,80,120s
                time.sleep(back)
                continue
            raise
    with urllib.request.urlopen(d["s3url"], timeout=90) as r2:
        g = json.load(r2)
    time.sleep(pace_s)  # be polite to the shared anonymous endpoint
    return g


def answer_logit_node(g: dict, answer: str):
    """The logit node whose output token == answer (leading space stripped,
    case-insensitive), preferring highest token_prob. None if not in graph.
    Lets us RETARGET attribution to the answer even when it isn't argmax —
    essential for weak-induction cases (gemma-2-2b's prior often outranks the
    injected answer, but the answer logit still carries incoming edges)."""
    a = answer.strip().lower()
    cands = []
    for n in g["nodes"]:
        if n.get("feature_type") != "logit":
            continue
        tok = str(n.get("clerp", ""))
        # clerp form: 'Output " otter" (p=0.794)'  -> extract the quoted token
        if '"' in tok:
            inner = tok.split('"')[1].strip().lower()
        else:
            inner = tok.strip().lower()
        if inner == a:
            cands.append(n)
    if not cands:
        return None
    return max(cands, key=lambda n: n.get("token_prob") or 0.0)


def answer_prob(g: dict, answer: str) -> float:
    n = answer_logit_node(g, answer)
    return float(n.get("token_prob") or 0.0) if n else 0.0


def backward_influence(g: dict, targets=None):
    """Propagate influence from target logit(s) back over links (abs weight,
    normalized per target). Returns (nodes_by_id, influence_by_id, target_nodes).

    targets: optional explicit list of target nodes (dicts) to seed influence
    from — e.g. a retargeted answer logit. Default (None) uses the argmax
    is_target_logit node(s), preserving the proven single-needle behavior."""
    nodes = {n["node_id"]: n for n in g["nodes"]}
    incoming = defaultdict(list)
    outsum = defaultdict(float)
    for lk in g["links"]:
        s, t, w = lk["source"], lk["target"], abs(lk["weight"])
        incoming[t].append((s, w))
        outsum[t] += w
    tgt = targets if targets is not None else [n for n in g["nodes"] if n.get("is_target_logit")]
    infl = {t["node_id"]: 1.0 for t in tgt}
    for _ in range(6):  # relaxation passes; DAG-ish, converges fast
        new = {t["node_id"]: 1.0 for t in tgt}
        for t, srcs in incoming.items():
            it = infl.get(t, 0.0)
            if it == 0 or outsum[t] == 0:
                continue
            for s, w in srcs:
                new[s] = new.get(s, 0.0) + it * (w / outsum[t])
        infl = new
    return nodes, infl, tgt


_STRUCTURAL = {"<bos>", " ", "", "\n"}


def faithfulness(g: dict, ctx_span, exclude_structural: bool = True, target_node=None):
    """ctx_span = (lo, hi) inclusive ctx_idx range of the injected context.
    Returns dict with predicted answer + fraction of input attribution on ctx_span.

    target_node: optional retargeted logit node (e.g. answer_logit_node(g, ans))
    to attribute FROM instead of the argmax — needed when gemma's prior outranks
    the injected answer but we still want the answer logit's context-dependence."""
    toks = g["metadata"].get("prompt_tokens") or []
    nodes, infl, tgt = backward_influence(g, targets=[target_node] if target_node else None)
    by_ctx = defaultdict(float)
    for nid, n in nodes.items():
        if n.get("feature_type") != "embedding":
            continue
        c = n.get("ctx_idx")
        if c is None:
            continue
        if exclude_structural and str(toks[c] if c < len(toks) else "") in _STRUCTURAL:
            continue
        by_ctx[c] += infl.get(nid, 0.0)
    tot = sum(by_ctx.values()) or 1e-9
    lo, hi = ctx_span
    in_ctx = sum(v for c, v in by_ctx.items() if lo <= c <= hi)
    ranked = sorted(by_ctx.items(), key=lambda x: x[1], reverse=True)
    return {
        "answer": tgt[0].get("clerp") if tgt else None,
        "faithfulness": in_ctx / tot,
        "top_drivers": [(c, toks[c] if c < len(toks) else "?", round(v / tot, 3))
                        for c, v in ranked[:6]],
    }


if __name__ == "__main__":
    ts = int(time.time()) % 100000
    A = gen_graph("The Helix proxy server default port number is ", f"faithA-{ts}")
    B = gen_graph("Config: the Helix proxy server listens on port 11437 by default. "
                  "The Helix proxy server default port number is ", f"faithB-{ts}")
    bt = B["metadata"]["prompt_tokens"]
    fs = [i for i, t in enumerate(bt) if str(t).strip() in ("1", "4", "3", "7") and i < 19]
    print("A:", faithfulness(A, (0, 0)))          # no fact span
    print("B:", faithfulness(B, (min(fs), max(fs))))

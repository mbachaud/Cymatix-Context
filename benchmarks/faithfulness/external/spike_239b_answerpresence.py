"""
#239 answer-presence SPIKE — does an answer-presence scorer discriminate causal-use?

Offline, read-only. No helix code/config touched, no graphs, no ingestion.
Inputs (join on id):
  f:/Projects/np-graph/needles_239b_stage1.json  (q, expressed_context, 5 feats, ans[label-only])
  f:/Projects/np-graph/needles_239b_faith.json    (causal_use, pB, cell)

Sets:
  G24 = rows where faith.pB is not None (24: 7 answerable/causal, 5 heldout/non, 12 competition 5/7)
  C12 = cell=='competition' (the decisive non-circular test)

Scorers (decision priority):
  1. ms_marco_ce   cross-encoder/ms-marco-MiniLM-L-6-v2  (purpose-built (q,passage) relevance; hot-path candidate)
  2. nli_*         DeBERTa-v3-small NLI entailment, BOTH orders  (helix's training/models/nli ABSENT -> public substitute)
  3. minilm_cos    all-MiniLM-L6-v2 cosine  (naive baseline floor)

Never feed the gold token. query=q, span=expressed_context.
"""
import json, time, sys, os
import numpy as np

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "0")
import torch
torch.manual_seed(0)
DEVICE = "cpu"  # honest CPU timing for the hot-path decision; models are tiny

from sklearn.metrics import roc_auc_score
from scipy.stats import mannwhitneyu

STAGE1 = "f:/Projects/np-graph/needles_239b_stage1.json"
FAITH  = "f:/Projects/np-graph/needles_239b_faith.json"
OUT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spike_239b_results.json")

# ---------------------------------------------------------------- load / build sets
s = {r["id"]: r for r in json.load(open(STAGE1, encoding="utf-8"))}
f = {r["id"]: r for r in json.load(open(FAITH, encoding="utf-8"))}
G24 = [i for i in f if f[i].get("pB") is not None]
# stable order: answerable, heldout, competition, then id
cell_ord = {"answerable": 0, "heldout": 1, "competition": 2}
G24.sort(key=lambda i: (cell_ord.get(f[i]["cell"], 9), i))
C12 = [i for i in G24 if f[i]["cell"] == "competition"]
y = {i: bool(f[i]["causal_use"]) for i in G24}
assert len(G24) == 24 and len(C12) == 12, (len(G24), len(C12))

pairs = {i: (s[i]["q"], s[i]["expressed_context"]) for i in G24}  # (query, span)

print(f"G24={len(G24)}  C12={len(C12)}  "
      f"C12 causal={sum(y[i] for i in C12)}/{len(C12)}  "
      f"heldout={[i for i in G24 if f[i]['cell']=='heldout']}")

scores = {}   # scorer_name -> {id: float}
cost   = {}   # scorer_name -> {params, ms_per_pair}

def timed_single(fn, ids, warmup=3):
    """Run fn(id) over ids one-at-a-time; return {id:score}, mean ms/pair (post-warmup)."""
    out = {}
    order = list(ids)
    for i in order[:warmup]:
        fn(i)
    t0 = time.perf_counter()
    for i in order:
        out[i] = float(fn(i))
    ms = (time.perf_counter() - t0) * 1000.0 / len(order)
    return out, ms

# ---------------------------------------------------------------- 1. MS-MARCO cross-encoder
from sentence_transformers import CrossEncoder
print("\n[load] cross-encoder/ms-marco-MiniLM-L-6-v2 ...", flush=True)
ce = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", device=DEVICE, max_length=512)
ce_params = sum(p.numel() for p in ce.model.parameters())
def _ms(i):
    q, span = pairs[i]
    return ce.predict([(q, span)])[0]
scores["ms_marco_ce"], ms = timed_single(_ms, G24)
cost["ms_marco_ce"] = {"params": ce_params, "ms_per_pair": ms}
print(f"  params={ce_params/1e6:.1f}M  {ms:.1f} ms/pair (CPU)")

# ---------------------------------------------------------------- 2. NLI entailment (substitute)
NLI_LOCAL = "f:/Projects/helix-context/training/models/nli"
NLI_PUBLIC = "cross-encoder/nli-deberta-v3-small"
nli_source = None
try:
    from transformers import AutoTokenizer, AutoModelForSequenceClassification
    if os.path.isdir(NLI_LOCAL):
        nli_path, nli_source = NLI_LOCAL, "helix-local (7-class MacCartney)"
    else:
        nli_path, nli_source = NLI_PUBLIC, "PUBLIC SUBSTITUTE (helix training/models/nli ABSENT)"
    print(f"\n[load] NLI: {nli_path}  <- {nli_source}", flush=True)
    tok = AutoTokenizer.from_pretrained(nli_path)
    nli = AutoModelForSequenceClassification.from_pretrained(nli_path).to(DEVICE)
    nli.eval()
    nli_params = sum(p.numel() for p in nli.parameters())
    id2label = {int(k): v for k, v in nli.config.id2label.items()}
    # locate entailment column robustly (helix local: ENTAILMENT==0; public: read label map)
    ent_idx = next((k for k, v in id2label.items() if "entail" in str(v).lower()), 0)
    print(f"  id2label={id2label}  entailment_idx={ent_idx}  params={nli_params/1e6:.1f}M")

    def _ent(premise, hypothesis):
        enc = tok(premise, hypothesis, truncation=True, max_length=256,
                  padding="max_length", return_tensors="pt").to(DEVICE)
        with torch.no_grad():
            logits = nli(**enc).logits
            p = torch.softmax(logits, dim=-1).squeeze(0)
        return p[ent_idx].item()

    # order A: premise=span, hypothesis=query ; order B: premise=query, hypothesis=span
    def _nli_sq(i):
        q, span = pairs[i]; return _ent(span, q)
    def _nli_qs(i):
        q, span = pairs[i]; return _ent(q, span)
    scores["nli_span_q"], ms_a = timed_single(_nli_sq, G24)
    scores["nli_q_span"], ms_b = timed_single(_nli_qs, G24)
    cost["nli_span_q"] = {"params": nli_params, "ms_per_pair": ms_a, "source": nli_source}
    cost["nli_q_span"] = {"params": nli_params, "ms_per_pair": ms_b, "source": nli_source}
    print(f"  span->q {ms_a:.1f} ms/pair | q->span {ms_b:.1f} ms/pair (CPU)")
except Exception as e:
    print(f"  NLI SCORER UNAVAILABLE: {type(e).__name__}: {e}")

# ---------------------------------------------------------------- 3. MiniLM bi-encoder cosine
from sentence_transformers import SentenceTransformer
print("\n[load] sentence-transformers/all-MiniLM-L6-v2 ...", flush=True)
st = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device=DEVICE)
st_params = sum(p.numel() for p in st.parameters())
def _cos(i):
    q, span = pairs[i]
    ev = st.encode([q, span], normalize_embeddings=True)
    return float(np.dot(ev[0], ev[1]))
scores["minilm_cos"], ms = timed_single(_cos, G24)
cost["minilm_cos"] = {"params": st_params, "ms_per_pair": ms}
print(f"  params={st_params/1e6:.1f}M  {ms:.1f} ms/pair (CPU)")

# ---------------------------------------------------------------- metrics
def auc_on(ids, sc):
    yt = np.array([y[i] for i in ids], dtype=int)
    ys = np.array([sc[i] for i in ids], dtype=float)
    if len(set(yt)) < 2:
        return float("nan")
    return roc_auc_score(yt, ys)

def bootstrap_ci_c12(sc, B=5000, seed=12345):
    rng = np.random.default_rng(seed)
    yt = np.array([y[i] for i in C12], dtype=int)
    ys = np.array([sc[i] for i in C12], dtype=float)
    n = len(C12)
    aucs = []
    for _ in range(B):
        idx = rng.integers(0, n, n)
        if len(set(yt[idx])) < 2:
            continue
        aucs.append(roc_auc_score(yt[idx], ys[idx]))
    aucs = np.array(aucs)
    lo, hi = np.percentile(aucs, [2.5, 97.5])
    return float(lo), float(hi), float(hi - lo), len(aucs)

def mwu_greater(ids, sc):
    pos = [sc[i] for i in ids if y[i]]
    neg = [sc[i] for i in ids if not y[i]]
    if not pos or not neg:
        return float("nan")
    return float(mannwhitneyu(pos, neg, alternative="greater").pvalue)

def heldout_rank(sc):
    """Rank heldout (non-causal) rows within G24. Lower score = better (answer-absent).
    Returns (mean percentile of heldout in [0,1], n of 5 in bottom quartile=bottom-6)."""
    hid = [i for i in G24 if f[i]["cell"] == "heldout"]
    allv = np.array([sc[i] for i in G24])
    pcts = []
    for i in hid:
        pct = float((allv < sc[i]).sum()) / (len(G24) - 1)  # frac of others below
        pcts.append(pct)
    order = sorted(G24, key=lambda j: sc[j])          # ascending
    bottom6 = set(order[:6])
    n_bot = sum(1 for i in hid if i in bottom6)
    return float(np.mean(pcts)), n_bot, len(hid)

rows = []
for name, sc in scores.items():
    c12 = auc_on(C12, sc)
    g24 = auc_on(G24, sc)
    lo, hi, w, nb = bootstrap_ci_c12(sc)
    p_c12 = mwu_greater(C12, sc)
    p_g24 = mwu_greater(G24, sc)
    hpct, hbot, hn = heldout_rank(sc)
    rows.append(dict(scorer=name, c12_auc=c12, g24_auc=g24,
                     c12_ci_lo=lo, c12_ci_hi=hi, c12_ci_w=w, ci_valid=nb,
                     p_c12=p_c12, p_g24=p_g24,
                     heldout_meanpct=hpct, heldout_bot6=f"{hbot}/{hn}"))

# ---------------------------------------------------------------- print table
print("\n" + "=" * 108)
print(f"{'scorer':<14}{'C12 AUC':>9}{'G24 AUC':>9}{'C12 95%CI':>18}{'CIw':>7}"
      f"{'p(C12)':>9}{'p(G24)':>9}{'held %ile':>10}{'held bot6':>10}")
print("-" * 108)
for r in rows:
    print(f"{r['scorer']:<14}{r['c12_auc']:>9.3f}{r['g24_auc']:>9.3f}"
          f"{'['+format(r['c12_ci_lo'],'.2f')+','+format(r['c12_ci_hi'],'.2f')+']':>18}"
          f"{r['c12_ci_w']:>7.2f}{r['p_c12']:>9.3f}{r['p_g24']:>9.3f}"
          f"{r['heldout_meanpct']:>10.2f}{r['heldout_bot6']:>10}")
print("=" * 108)

# ---------------------------------------------------------------- pre-registered verdict
def scorer_go(r):
    return (r["c12_auc"] >= 0.70 and r["g24_auc"] >= 0.70 and r["c12_ci_lo"] > 0.5)
def scorer_partial(r):
    return (r["g24_auc"] >= 0.75 and 0.40 <= r["c12_auc"] <= 0.60
            and r["heldout_bot6"] in ("4/5", "5/5"))

# shippable candidates only drive the gate (NLI substitute is scientific-only)
shippable = [r for r in rows if r["scorer"] in ("ms_marco_ce", "minilm_cos")]
go = [r["scorer"] for r in shippable if scorer_go(r)]
partial = [r["scorer"] for r in shippable if scorer_partial(r)]
best_c12 = max(r["c12_auc"] for r in shippable)
best_g24 = max(r["g24_auc"] for r in shippable)
nogo = (best_c12 < 0.60 and best_g24 < 0.60)

if go:
    verdict = f"GO (scorer(s): {', '.join(go)})"
elif partial:
    verdict = f"PARTIAL-GO (abstain/answer-absence gate; scorer(s): {', '.join(partial)})"
elif nogo:
    verdict = "NO-GO"
else:
    verdict = (f"INCONCLUSIVE-BETWEEN-TIERS (best shippable C12={best_c12:.3f} "
               f"G24={best_g24:.3f}; not GO, not clean-PARTIAL, not NO-GO)")

print(f"\nVERDICT (shippable-driven): {verdict}")
print(f"  best shippable C12 AUC={best_c12:.3f}  G24 AUC={best_g24:.3f}")
print(f"  NLI source: {nli_source}")

json.dump({"rows": rows, "cost": cost, "verdict": verdict,
           "nli_source": nli_source, "best_c12": best_c12, "best_g24": best_g24,
           "scores": {k: {i: scores[k][i] for i in G24} for k in scores},
           "labels": {i: y[i] for i in G24},
           "cells": {i: f[i]["cell"] for i in G24}},
          open(OUT, "w", encoding="utf-8"), indent=2)
print(f"\n[wrote] {OUT}")

"""#239 stage 3 — refit the know-logistic against the FAITHFULNESS (causal-use)
label and compare to the shipped fit (which was trained on the retrieval proxy
top1==planted, with beta1(top_score) NEGATIVE).

Merges needles_239_stage1.json (5 features + shipped raw_confidence + gold_rank)
with needles_239_faith.json (causal_use). Builds the SAME squashed feature vector
compute_confidence uses (shipped s_ref/g_ref), then:

  baseline  : shipped confidence vs causal_use          (AUC, ECE, KnowBlocks emitted)
  refit-C   : LogisticRegression(features -> causal_use) (LOOCV AUC/ECE; new betas)
  refit-R   : LogisticRegression(features -> retrieval)  (PR#249 label, on this bed)
              then scored AGAINST causal_use -> shows the proxy underperforms.

Helix env (sklearn + know_calibration). No graphs, no network.
"""
import os, sys, json, math, argparse
from pathlib import Path
import numpy as np

_REPO = Path("f:/Projects/helix-context")
sys.path.insert(0, str(_REPO))
from cymatix_context.config import load_config
from cymatix_context.scoring.know_calibration import calibration_from_config

from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import LeaveOneOut, cross_val_predict
from sklearn.metrics import roc_auc_score

STAGE1 = "f:/Projects/np-graph/needles_239_stage1.json"
FAITH = "f:/Projects/np-graph/needles_239_faith.json"


def squash(top, gap, agree, coord, fresh, s_ref, g_ref):
    return [
        math.tanh(float(top) / s_ref),
        math.tanh(float(gap) / g_ref),
        1.0 if agree else 0.0,
        max(0.0, min(1.0, float(coord))),
        max(0.0, min(1.0, float(fresh if fresh is not None else 0.0))),
    ]


def ece(probs, labels, n_bins=10):
    probs = np.asarray(probs); labels = np.asarray(labels)
    edges = np.linspace(0, 1, n_bins + 1)
    e, N = 0.0, len(probs)
    for i in range(n_bins):
        m = (probs >= edges[i]) & (probs < edges[i + 1] if i < n_bins - 1 else probs <= edges[i + 1])
        if m.sum() == 0:
            continue
        e += (m.sum() / N) * abs(labels[m].mean() - probs[m].mean())
    return e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-floor", type=float, default=None)
    ap.add_argument("--stage1", default=STAGE1)
    ap.add_argument("--faith", default=FAITH)
    args = ap.parse_args()

    cfg = load_config(str(_REPO / "helix.toml"))
    cal = calibration_from_config(cfg.know)
    s_ref, g_ref = cal.s_ref, cal.g_ref
    floor = args.emit_floor if args.emit_floor is not None else cal.emit_floor
    print(f"shipped betas={list(cal.betas)}  s_ref={s_ref} g_ref={g_ref} emit_floor={floor}")

    s1 = {r["id"]: r for r in json.load(open(args.stage1, encoding="utf-8"))}
    fa = {r["id"]: r for r in json.load(open(args.faith, encoding="utf-8"))}
    ids = [i for i in s1 if i in fa]

    X, conf, y_causal, y_retr = [], [], [], []
    for i in ids:
        r, f = s1[i], fa[i]
        X.append(squash(r["top_score"], r["score_gap"], r["lexical_dense_agree"],
                        r["coordinate_confidence"], r["freshness_min"], s_ref, g_ref))
        conf.append(r["raw_confidence"])
        y_causal.append(int(bool(f.get("causal_use"))))
        y_retr.append(int(r["gold_rank"] == 1))
    X = np.asarray(X); conf = np.asarray(conf)
    y_causal = np.asarray(y_causal); y_retr = np.asarray(y_retr)
    n = len(ids)
    n_graphed = sum(1 for i in ids if fa[i].get("imputed") is not True and not fa[i].get("skipped_graph"))
    n_imputed = sum(1 for i in ids if fa[i].get("imputed") is True)
    n_zero = sum(1 for i in ids if fa[i].get("skipped_graph"))
    print(f"\nn={n} | causal_use=1: {y_causal.sum()} | retrieval-top1=1: {y_retr.sum()} | "
          f"disagree(causal!=retr): {(y_causal != y_retr).sum()}")
    print(f"label provenance: {n_graphed} graphed (measured) | {n_imputed} imputed causal=1 | "
          f"{n_zero} non-survivor causal=0")

    def auc(y, p):
        return roc_auc_score(y, p) if len(set(y)) > 1 else float("nan")

    # ---- baseline: shipped confidence vs causal_use ----
    # point-biserial corr shows the DIRECTION cleanly even under imbalance.
    from numpy import corrcoef
    r_pb = float(corrcoef(conf, y_causal)[0, 1])
    base_auc = auc(y_causal, conf)
    base_ece = ece(conf, y_causal)
    recov = int(((conf >= floor) & (y_causal == 1)).sum())
    print(f"\n[shipped]   AUC(conf->causal)={base_auc:.3f}  ECE={base_ece:.3f}  "
          f"corr(conf,causal)={r_pb:+.3f}")
    print(f"            conf range [{conf.min():.3f},{conf.max():.3f}]  "
          f"KnowBlocks (conf>=floor)={int((conf>=floor).sum())}/{n}  "
          f"causal facts RECOVERED={recov}/{int(y_causal.sum())}")

    def fit_eval(ytrain, tag, note):
        # class_weight balanced so the minority class is not ignored under
        # the 94%-positive skew; LOOCV AUC (unstable at this n) + in-sample
        # separability ceiling; betas give the correction DIRECTION.
        clf = LogisticRegression(C=1.0, max_iter=5000, class_weight="balanced")
        p_loo = cross_val_predict(clf, X, ytrain, cv=LeaveOneOut(),
                                  method="predict_proba")[:, 1]
        fit = LogisticRegression(C=1.0, max_iter=5000, class_weight="balanced").fit(X, ytrain)
        p_in = fit.predict_proba(X)[:, 1]
        beta = [round(float(fit.intercept_[0]), 4)] + [round(float(b), 4) for b in fit.coef_[0]]
        rec = int(((p_in >= 0.5) & (y_causal == 1)).sum())
        print(f"\n[{tag}] {note}")
        print(f"            betas={beta}   b1/top_score sign: {'+' if beta[1] > 0 else '-'}")
        print(f"            AUC(->causal): in-sample={auc(y_causal,p_in):.3f} "
              f"LOOCV={auc(y_causal,p_loo):.3f}   ECE(in)={ece(p_in,y_causal):.3f}")
        print(f"            causal facts RECOVERED (p>=0.5)={rec}/{int(y_causal.sum())}")
        return beta

    betaC = fit_eval(y_causal, "refit-C", "features -> CAUSAL-USE label")
    if len(set(y_retr)) > 1:
        fit_eval(y_retr, "refit-R", f"features -> RETRIEVAL-top1 label "
                 f"(mislabels {int(((y_retr==0)&(y_causal==1)).sum())} causal facts as neg)")

    print("\n--- DELIVERABLE: causal-calibrated betas (keep shipped s_ref/g_ref) ---")
    print(f"betas = {betaC}")
    print(f"# was   {list(cal.betas)}   (intercept {cal.betas[0]}-> {betaC[0]}, "
          f"b1(top_score) {cal.betas[1]}-> {betaC[1]})")


if __name__ == "__main__":
    main()

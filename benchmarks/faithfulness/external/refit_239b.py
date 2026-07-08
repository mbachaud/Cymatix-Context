"""#239 delivery-balanced stage 3 — the PRODUCTION-relevant refit the §3 bed
couldn't support. Merges needles_239b_stage1.json (5 features + shipped
confidence) with needles_239b_faith.json (causal_use: graph-measured or imputed
by cell), then compares the shipped logistic to a refit against causal-use on a
BALANCED bed (answerable causal=1 / heldout causal=0 / competition graph-decided).

Reports: ROC-AUC and ECE of shipped confidence vs causal; refit AUC (LOOCV +
train/test), betas (does coord get up-weighted, does b1 flip); recall at the
shipped emit_floor vs at a matched operating point.
Helix env (sklearn). No graphs.
"""
import os, sys, json, math, argparse
from pathlib import Path
import numpy as np

_REPO = Path("f:/Projects/helix-context")
sys.path.insert(0, str(_REPO))
from helix_context.config import load_config
from helix_context.scoring.know_calibration import calibration_from_config
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import LeaveOneOut, cross_val_predict, StratifiedKFold
from sklearn.metrics import roc_auc_score

STAGE1 = "f:/Projects/np-graph/needles_239b_stage1.json"
FAITH = "f:/Projects/np-graph/needles_239b_faith.json"
FEATS = ["top_score", "score_gap", "lexical_dense_agree", "coordinate_confidence", "freshness_min"]


def squash(r, s, g):
    return [math.tanh(r["top_score"] / s), math.tanh(r["score_gap"] / g),
            1.0 if r["lexical_dense_agree"] else 0.0,
            max(0.0, min(1.0, r["coordinate_confidence"])),
            max(0.0, min(1.0, float(r["freshness_min"] or 0)))]


def ece(p, y, nb=10):
    p = np.asarray(p); y = np.asarray(y); e = 0.0; N = len(p)
    for i in range(nb):
        lo, hi = i / nb, (i + 1) / nb
        m = (p >= lo) & (p < hi if i < nb - 1 else p <= hi)
        if m.sum():
            e += m.sum() / N * abs(y[m].mean() - p[m].mean())
    return e


def auc(y, p):
    return roc_auc_score(y, p) if len(set(y)) > 1 else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage1", default=STAGE1)
    ap.add_argument("--faith", default=FAITH)
    args = ap.parse_args()
    cfg = load_config(str(_REPO / "helix.toml"))
    cal = calibration_from_config(cfg.know)
    s, g, floor = cal.s_ref, cal.g_ref, cal.emit_floor
    print(f"shipped betas={list(cal.betas)} s_ref={s} g_ref={g} floor={floor}")

    s1 = {r["id"]: r for r in json.load(open(args.stage1, encoding="utf-8"))}
    fa = {r["id"]: r for r in json.load(open(args.faith, encoding="utf-8"))}
    ids = [i for i in s1 if i in fa and "error" not in fa[i]
           and not fa[i].get("needs_graph") and fa[i].get("causal_use") is not None]

    X, conf, y, cells, prov = [], [], [], [], []
    for i in ids:
        r, f = s1[i], fa[i]
        X.append(squash(r, s, g)); conf.append(r["raw_confidence"])
        y.append(int(bool(f.get("causal_use")))); cells.append(r["cell"])
        prov.append("imputed" if f.get("imputed") else "measured")
    X = np.asarray(X); conf = np.asarray(conf); y = np.asarray(y)
    cells = np.asarray(cells); prov = np.asarray(prov)
    n = len(ids)
    print(f"\nn={n} | causal=1: {y.sum()} causal=0: {(y==0).sum()} | "
          f"measured {int((prov=='measured').sum())} imputed {int((prov=='imputed').sum())}")
    for c in ("answerable", "heldout", "competition"):
        mk = cells == c
        print(f"  {c:12s}: n={mk.sum()} causal={y[mk].sum()} "
              f"(measured {int((prov[mk]=='measured').sum())})")
    comp_ignored = [i for i in ids if fa[i].get("used_competitor_not_gold")]
    print(f"  delivered-but-ignored (competition, gold delivered but model used competitor): {len(comp_ignored)}")

    # ---- shipped ----
    print(f"\n[shipped]  AUC(conf->causal)={auc(y,conf):.3f}  ECE={ece(conf,y):.3f}  "
          f"conf[{conf.min():.3f},{conf.max():.3f}]")
    print(f"           recall @ floor {floor}: {int(((conf>=floor)&(y==1)).sum())}/{int(y.sum())} causal "
          f"| KnowBlocks total {int((conf>=floor).sum())}/{n}")

    # ---- refit ----
    clf = LogisticRegression(C=1.0, max_iter=5000, class_weight="balanced")
    p_loo = cross_val_predict(clf, X, y, cv=LeaveOneOut(), method="predict_proba")[:, 1]
    # stratified 5-fold as a second estimate
    p_cv = cross_val_predict(LogisticRegression(C=1.0, max_iter=5000, class_weight="balanced"),
                             X, y, cv=StratifiedKFold(5, shuffle=True, random_state=0),
                             method="predict_proba")[:, 1]
    fit = LogisticRegression(C=1.0, max_iter=5000, class_weight="balanced").fit(X, y)
    beta = [round(float(fit.intercept_[0]), 4)] + [round(float(b), 4) for b in fit.coef_[0]]
    p_in = fit.predict_proba(X)[:, 1]
    print(f"\n[refit-C]  AUC(->causal): LOOCV={auc(y,p_loo):.3f}  5fold={auc(y,p_cv):.3f}  "
          f"in-sample={auc(y,p_in):.3f}  ECE(LOOCV)={ece(p_loo,y):.3f}")
    print(f"           betas={beta}")
    print(f"           feature weights: " + "  ".join(f"{FEATS[k]}={beta[k+1]:+.2f}" for k in range(5)))
    print(f"           b1(top_score) {cal.betas[1]:+.2f}->{beta[1]:+.2f}  "
          f"b4(coord) {cal.betas[4]:+.2f}->{beta[4]:+.2f}  intercept {cal.betas[0]:+.2f}->{beta[0]:+.2f}")
    print(f"           recall @ p>=0.5 (LOOCV): {int(((p_loo>=0.5)&(y==1)).sum())}/{int(y.sum())} causal "
          f"| false-emits {int(((p_loo>=0.5)&(y==0)).sum())}/{int((y==0).sum())}")

    print(f"\nSUMMARY: shipped AUC {auc(y,conf):.3f} -> refit LOOCV {auc(y,p_loo):.3f} "
          f"(+{auc(y,p_loo)-auc(y,conf):.3f}); coord weight {cal.betas[4]:+.2f}->{beta[4]:+.2f} is the lever.")


if __name__ == "__main__":
    main()

"""Fit the [know] model = "lanes" logistic from know_abstain_replay receipts (#482).

Reads replay receipts recorded with ``lane_signals`` (2026-10-05 or later),
builds the served feature vector for every query with
``cymatix_context.scoring.know_lanes.feature_values`` (the exact function the
server uses, so there is no train/serve skew), and fits a standardised
logistic regression on label = gold at score-map rank 1. The scaler is folded
into raw betas, so the served formula is a plain intercept + sum(beta * x).

It reports leave-one-corpus-out AUC per held-out corpus, pooled out-of-fold
AUC, and precision/coverage at several thresholds (including emit_floor), then
fits on every row and writes the ``[know]`` block to ``--toml-out``. It never
edits cymatix.toml: paste the block in (or load it as an arm config) only
after the receipt-gated A/B.

Usage:
  python scripts/fit_know_lanes.py --receipt erb_947k=PATH [--receipt ...] \
      --out report.json --toml-out know_lanes.toml [--emit-floor 0.45] [--C 1.0]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cymatix_context.scoring.know_lanes import CE_FEATURES, FEATURE_NAMES, feature_values  # noqa: E402


def label(row: Mapping) -> int:
    return 1 if row.get("rank_of_first_gold") == 1 else 0


def load_rows(specs: Sequence[str], arm: str) -> List[dict]:
    rows: List[dict] = []
    for spec in specs:
        corpus, _, path = spec.partition("=")
        if not path:
            raise SystemExit(f"--receipt must be CORPUS=PATH, got {spec!r}")
        receipt = json.loads(Path(path).read_text(encoding="utf-8"))
        a = next((x for x in receipt["arms"] if x["arm"] == arm), None)
        if a is None:
            raise SystemExit(f"{path}: no arm {arm!r}")
        got = [dict(r, corpus=corpus) for r in a["per_query"] if r.get("top_score") is not None]
        if got and not any("lane_signals" in r for r in got):
            raise SystemExit(f"{path}: rows carry no lane_signals (replay predates 2026-10-05); re-run the replay")
        rows += got
    return rows


def _matrix(feats: Sequence[Mapping[str, float]], names: Sequence[str] = FEATURE_NAMES) -> List[List[float]]:
    return [[float(f[n]) for n in names] for f in feats]


def fit_pipeline(feats: Sequence[Mapping[str, float]], labels: Sequence[int], C: float = 1.0,
                 names: Sequence[str] = FEATURE_NAMES):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    pipe = make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=2000))
    pipe.fit(_matrix(feats, names), list(labels))
    return pipe


def fold(pipe, names: Sequence[str] = FEATURE_NAMES) -> Dict:
    """Standardised coefficients -> raw-scale intercept + betas."""
    scaler, clf = pipe.steps[0][1], pipe.steps[1][1]
    coefs, means, scales = clf.coef_[0], scaler.mean_, scaler.scale_
    betas, intercept = {}, float(clf.intercept_[0])
    for name, c, m, s in zip(names, coefs, means, scales):
        s = s if s > 0 else 1.0
        betas[name] = float(c / s)
        intercept -= float(c * m / s)
    return {"intercept": intercept, "betas": {k: v for k, v in betas.items() if v != 0.0}}


def _auc(probs: Sequence[float], labels: Sequence[int]):
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(labels, probs)) if len(set(labels)) == 2 else None


def _curve(probs, labels, thresholds):
    out = []
    for th in thresholds:
        kept = [y for p, y in zip(probs, labels) if p >= th]
        out.append({"threshold": th, "coverage": len(kept) / len(labels) if labels else None,
                    "precision": (sum(kept) / len(kept)) if kept else None})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--receipt", action="append", required=True, metavar="CORPUS=PATH")
    ap.add_argument("--arm", default="postflip_default")
    ap.add_argument("--emit-floor", type=float, default=0.45)
    ap.add_argument("--C", type=float, default=1.0)
    ap.add_argument("--features", choices=("base", "ce"), default="base",
                    help="ce adds the cross-encoder top-1 signals; the TOML then sets [know] lanes_ce_model from "
                         "the receipts' ce_model header so the server computes the same features")
    ap.add_argument("--out", required=True)
    ap.add_argument("--toml-out", required=True)
    a = ap.parse_args(argv)

    with_ce = a.features == "ce"
    names = FEATURE_NAMES + (CE_FEATURES if with_ce else ())
    rows = load_rows(a.receipt, a.arm)
    if with_ce and not any("ce" in r for r in rows):
        raise SystemExit("--features ce: rows carry no ce signals; re-run know_abstain_replay with --ce-model")
    feats = [feature_values(r, with_ce=with_ce) for r in rows]
    labels = [label(r) for r in rows]
    corpora = sorted({r["corpus"] for r in rows})
    thresholds = sorted({0.3, 0.4, a.emit_floor, 0.5, 0.6, 0.7, 0.8})

    folds, oof_p, oof_y = [], [], []
    for held in corpora:
        tr = [i for i, r in enumerate(rows) if r["corpus"] != held]
        te = [i for i, r in enumerate(rows) if r["corpus"] == held]
        if len({labels[i] for i in tr}) < 2 or not te:
            continue
        pipe = fit_pipeline([feats[i] for i in tr], [labels[i] for i in tr], a.C, names)
        p = [float(v) for v in pipe.predict_proba(_matrix([feats[i] for i in te], names))[:, 1]]
        yt = [labels[i] for i in te]
        at_floor = _curve(p, yt, [a.emit_floor])[0]
        folds.append({"held_out": held, "n": len(te), "base_rate": sum(yt) / len(yt), "auc": _auc(p, yt),
                      "max_prob": max(p), "emit_rate_at_floor": at_floor["coverage"],
                      "precision_at_floor": at_floor["precision"]})
        oof_p += p
        oof_y += yt

    final = fit_pipeline(feats, labels, a.C, names)
    folded = fold(final, names)
    report = {
        "tool": "scripts/fit_know_lanes.py", "issue": "#482",
        "fitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "arm": a.arm, "C": a.C,
        "label": "gold at score-map rank 1", "n_rows": len(rows),
        "corpora": {c: sum(1 for r in rows if r["corpus"] == c) for c in corpora},
        "receipts": a.receipt, "features": a.features, "feature_names": list(names),
        "leave_one_corpus_out": {
            "folds": folds, "pooled_auc": _auc(oof_p, oof_y) if oof_y else None,
            "precision_coverage": _curve(oof_p, oof_y, thresholds) if oof_y else [],
        },
        "fit_all": folded,
    }
    Path(a.out).write_text(json.dumps(report, indent=1), encoding="utf-8")

    pooled = report["leave_one_corpus_out"]["pooled_auc"]
    ce_line = ""
    if with_ce:
        ce_models = {json.loads(Path(s.partition("=")[2]).read_text(encoding="utf-8")).get("ce_model")
                     for s in a.receipt}
        ce_models.discard(None)
        if len(ce_models) != 1:
            raise SystemExit(f"--features ce: receipts must share one ce_model header, got {sorted(ce_models)}")
        ce_line = f"lanes_ce_model = {json.dumps(ce_models.pop())}\n"
    betas = ", ".join(f"{k} = {v:.6g}" for k, v in folded["betas"].items())
    Path(a.toml_out).write_text(
        "# [know] model = \"lanes\", fit by scripts/fit_know_lanes.py (#482).\n"
        f"# {len(rows)} queries over {len(corpora)} corpora; leave-one-corpus-out pooled AUC "
        f"{pooled if pooled is None else round(pooled, 4)}; report {a.out}\n"
        "[know]\n"
        "model = \"lanes\"\n"
        f"emit_floor = {a.emit_floor}\n"
        f"lanes_intercept = {folded['intercept']:.6g}\n"
        f"lanes_betas = {{ {betas} }}\n"
        f"{ce_line}",
        encoding="utf-8",
    )
    print(f"{len(rows)} rows, {len(corpora)} corpora, LOCO pooled AUC {pooled}; -> {a.out}, {a.toml_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

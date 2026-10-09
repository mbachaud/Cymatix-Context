"""Dense-free know/miss feature probe over know_abstain_replay receipts.

Issue #482. The shipped [know] logistic takes top_score, score_gap,
lexical_dense_agree, coordinate_confidence and freshness_min. With dense
retrieval off (the shipped default since 2026-08-15) lexical_dense_agree is
always False, and freshness_min is constant on bench beds, so the curve runs on
inputs the shipped path never delivers. This probe asks which inputs the
shipped path DOES deliver separate hits from misses on corpora the model never
saw, before any change to compute_confidence.

Feature sets (all read only what the served pipeline produces per query; the
gold-derived replay fields in GOLD_FIELDS are never read):
  shipped_like  top_score, score_gap, coordinate_confidence (the shipped
                inputs minus the two dead ones; raw scale)
  scale_free    log(top1/top2 ratio), score_gap / top_score,
                coordinate_confidence, log(pool_size) -- invariant to the
                per-corpus RRF score scale
  all_served    scale_free + top_score, score_gap, log1p(delivered_count) +
                one-hot budget_tier, classifier_class, health_status
Labels:
  rank1         gold at score-map rank 1 (calibrate_know_confidence.py)
  delivered     gold inside the delivered context
Models: standardised logistic regression (deployable as a fixed formula) and
gradient-boosted trees (a ceiling: how much signal the inputs carry at all).
Evaluation: leave-one-corpus-out, per-fold AUC plus pooled out-of-fold AUC,
ECE and a precision/coverage table. Reads receipts only; writes one JSON.

Usage:
  python benchmarks/dogfood/know/know_feature_probe.py --glob "benchmarks/dogfood/**/receipts/know_replay_*_2026-10-04.json" --out <probe.json>
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import re
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

GOLD_FIELDS = {"rank_of_first_gold", "gold_ranks", "delivered_gold", "delivered_gold_rank",
               "recall_hit", "final_rank_of_first_gold", "empty_window_gold_rank1", "gene_id_match",
               "confidence", "confidence_raw", "verdict", "miss_reason"}
LEVELS = {
    "budget_tier": ["broad", "focused", "tight", "abstain"],
    "classifier_class": ["default", "factual", "multi_hop", "procedural", "arithmetic"],
    "health_status": ["sparse", "aligned", "denatured", "abstain"],
}
FEATURE_SETS = ("shipped_like", "scale_free", "all_served", "agreement", "all_with_agreement")
# Dense-free agreement + coverage signals (lane_signals.py), recorded by the
# replay under row["lane_signals"]. Each enters as value + "<name>_missing"
# indicator so an unmeasurable signal is never read as 0 evidence.
LANE_KEYS = ("lanes_fired", "top1_lanes", "lanes_top3_agree", "frac_lanes_agree",
             "fts5_top1_is_fused_top1", "query_term_coverage")


def _lane_features(row: Mapping) -> List[float]:
    sig = row.get("lane_signals") or {}
    out = []
    for key in LANE_KEYS:
        v = sig.get(key)
        out += [0.0 if v is None else float(v), 1.0 if v is None else 0.0]
    return out


def _lane_names() -> List[str]:
    return [n for key in LANE_KEYS for n in (key, f"{key}_missing")]


def _f(row: Mapping, key: str) -> float:
    v = row.get(key)
    return float(v) if v is not None else 0.0


def feature_names(name: str) -> List[str]:
    if name == "shipped_like":
        return ["top_score", "score_gap", "coordinate_confidence"]
    base = ["log_ratio_top2", "rel_gap", "coordinate_confidence", "log_pool_size"]
    if name == "scale_free":
        return base
    if name in ("all_served", "all_with_agreement"):
        extra = ["top_score", "score_gap", "log1p_delivered_count"]
        onehot = [f"{k}={lv}" for k, levels in LEVELS.items() for lv in levels]
        return base + extra + onehot + (_lane_names() if name == "all_with_agreement" else [])
    if name == "agreement":
        return base + _lane_names()
    raise ValueError(name)


def feature_vector(row: Mapping, name: str) -> List[float]:
    top, gap = _f(row, "top_score"), _f(row, "score_gap")
    if name == "shipped_like":
        return [top, gap, _f(row, "coordinate_confidence")]
    ratio = _f(row, "ratio_top2") or 1.0
    base = [math.log(max(ratio, 1e-9)), (gap / top) if top > 0 else 0.0,
            _f(row, "coordinate_confidence"), math.log(max(_f(row, "pool_size"), 1.0))]
    if name == "scale_free":
        return base
    if name in ("all_served", "all_with_agreement"):
        extra = [top, gap, math.log1p(_f(row, "delivered_count"))]
        onehot = [1.0 if row.get(k) == lv else 0.0 for k, levels in LEVELS.items() for lv in levels]
        return base + extra + onehot + (_lane_features(row) if name == "all_with_agreement" else [])
    if name == "agreement":
        return base + _lane_features(row)
    raise ValueError(name)


def label(row: Mapping, kind: str) -> int:
    if kind == "rank1":
        return 1 if row.get("rank_of_first_gold") == 1 else 0
    if kind == "delivered":
        return 1 if row.get("delivered_gold") else 0
    raise ValueError(kind)


def _auc(probs: Sequence[float], labels: Sequence[int]):
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(labels, probs)) if len(set(labels)) == 2 else None


def _ece(probs: Sequence[float], labels: Sequence[int], n_bins: int = 10) -> float:
    bins: Dict[int, List[int]] = {}
    for i, p in enumerate(probs):
        bins.setdefault(min(int(p * n_bins), n_bins - 1), []).append(i)
    return sum(abs(sum(probs[i] for i in idx) / len(idx) - sum(labels[i] for i in idx) / len(idx))
               * len(idx) / len(probs) for idx in bins.values()) if probs else 0.0


def _model(kind: str):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if kind == "logistic":
        return make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=2000))
    if kind == "gbt":
        return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, random_state=0)
    raise ValueError(kind)


def loco(rows: Sequence[Mapping], feature_set: str, label_kind: str, model: str = "logistic") -> dict:
    corpora = sorted({r["corpus"] for r in rows})
    X = [feature_vector(r, feature_set) for r in rows]
    y = [label(r, label_kind) for r in rows]
    folds, oof_p, oof_y = [], [], []
    for held in corpora:
        tr = [i for i, r in enumerate(rows) if r["corpus"] != held]
        te = [i for i, r in enumerate(rows) if r["corpus"] == held]
        if len({y[i] for i in tr}) < 2:
            continue
        clf = _model(model)
        clf.fit([X[i] for i in tr], [y[i] for i in tr])
        p = [float(v) for v in clf.predict_proba([X[i] for i in te])[:, 1]]
        yt = [y[i] for i in te]
        folds.append({"held_out": held, "n": len(te), "base_rate": sum(yt) / len(yt), "auc": _auc(p, yt)})
        oof_p += p
        oof_y += yt
    curve = []
    for th in (0.3, 0.4, 0.45, 0.5, 0.6, 0.7, 0.8, 0.9):
        kept = [t for p, t in zip(oof_p, oof_y) if p >= th]
        curve.append({"threshold": th, "coverage": len(kept) / len(oof_y),
                      "precision": (sum(kept) / len(kept)) if kept else None})
    aucs = [f["auc"] for f in folds if f["auc"] is not None]
    return {"feature_set": feature_set, "label": label_kind, "model": model,
            "pooled_auc": _auc(oof_p, oof_y), "pooled_ece": _ece(oof_p, oof_y),
            "fold_auc_median": sorted(aucs)[len(aucs) // 2] if aucs else None,
            "folds_ge_0_7": sum(1 for a in aucs if a >= 0.7), "n_folds": len(folds),
            "base_rate": sum(oof_y) / len(oof_y) if oof_y else None,
            "folds": folds, "precision_coverage": curve}


def load_rows(pattern: str, arm: str = "postflip_default") -> List[dict]:
    rows = []
    for path in sorted(glob.glob(pattern, recursive=True)):
        corpus = re.sub(r"^know_replay_(.+)_\d{4}-\d{2}-\d{2}\.json$", r"\1", Path(path).name)
        receipt = json.loads(Path(path).read_text(encoding="utf-8"))
        a = next(x for x in receipt["arms"] if x["arm"] == arm)
        rows += [dict(r, corpus=corpus) for r in a["per_query"] if r.get("top_score") is not None]
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--glob", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    rows = load_rows(args.glob)
    results = []
    for label_kind in ("rank1", "delivered"):
        for fs in FEATURE_SETS:
            for model in ("logistic", "gbt"):
                res = loco(rows, fs, label_kind, model)
                results.append(res)
                print(f"{label_kind:9s} {fs:13s} {model:8s} pooled AUC {res['pooled_auc']:.4f} "
                      f"ECE {res['pooled_ece']:.4f} fold median {res['fold_auc_median']:.4f} "
                      f">=0.7 {res['folds_ge_0_7']}/{res['n_folds']}")
    out = {"n_rows": len(rows), "corpora": sorted({r["corpus"] for r in rows}),
           "gold_fields_excluded": sorted(GOLD_FIELDS), "results": results}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

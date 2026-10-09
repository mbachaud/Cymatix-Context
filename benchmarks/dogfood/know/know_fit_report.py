"""Cross-corpus know/miss calibration report from know_abstain_replay receipts.

Issue #482 tuning campaign. ``scripts/calibrate_know_confidence.py`` fits the
``[know]`` logistic on one labelled file with a random 20% holdout and writes
cymatix.toml. This report answers the question that has to come first: does a
refit generalise across corpora? It

* turns each ``know_abstain_replay.py`` per-needle row into a calibration row
  (label = gold at score-map rank 1, the calibration script's
  ``planted_gene_id == retrieved_top1`` definition; rows the replay could not
  score are dropped),
* evaluates the SHIPPED ``[know]`` calibration on every row (AUC, ECE, emit
  rate and precision at its emit_floor, per corpus and pooled),
* refits on all rows (in-sample, for the betas), and
* scores leave-one-corpus-out: fit on every other corpus, test on the held-out
  one, plus pooled out-of-fold probabilities for a precision/coverage table.

Feature construction, the fitter (sklearn when importable, else the
pure-Python gradient descent) and the AUC are imported from the calibration
script so the numbers match what an operator run would produce. It NEVER
writes cymatix.toml.

Usage:
  python benchmarks/dogfood/know/know_fit_report.py \\
      --receipt <corpus>=<know_abstain_replay receipt> [...] \\
      --arm postflip_default --config cymatix.toml --out <report.json>
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[3]


def _calibrate_module():
    spec = importlib.util.spec_from_file_location(
        "calibrate_know_confidence", ROOT / "scripts" / "calibrate_know_confidence.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


cal_mod = _calibrate_module()


def replay_rows_to_calibration(rows: Sequence[Mapping], corpus: str) -> List[dict]:
    """Replay per-needle rows -> calibration rows (label = gold at rank 1)."""
    out = []
    for r in rows:
        if r.get("top_score") is None:
            continue
        out.append({
            "corpus": corpus,
            "needle": r.get("needle"),
            "top_score": float(r["top_score"]),
            "score_gap": float(r.get("score_gap") or 0.0),
            "lexical_dense_agree": bool(r.get("lexical_dense_agree")),
            "coordinate_confidence": float(r.get("coordinate_confidence") or 0.0),
            "freshness_min": r.get("freshness_min"),
            "label": 1 if r.get("rank_of_first_gold") == 1 else 0,
        })
    return out


def probabilities(cal: Mapping, rows: Sequence[Mapping]) -> List[float]:
    """Confidence of each row under a calibration ({betas, s_ref, g_ref})."""
    betas = cal["betas"]
    out = []
    for r in rows:
        feat, _ = cal_mod._row_to_features(dict(r), s_ref=cal["s_ref"], g_ref=cal["g_ref"])
        z = betas[0] + sum(b * x for b, x in zip(betas[1:], feat))
        out.append(1.0 / (1.0 + math.exp(-z)))
    return out


def expected_calibration_error(probs: Sequence[float], labels: Sequence[int], n_bins: int = 10) -> float:
    """Equal-width-bin ECE: sum over bins of |mean prob - hit rate| x bin share."""
    if not probs:
        return 0.0
    bins: Dict[int, List[int]] = defaultdict(list)
    for i, p in enumerate(probs):
        bins[min(int(p * n_bins), n_bins - 1)].append(i)
    total = 0.0
    for idx in bins.values():
        conf = sum(probs[i] for i in idx) / len(idx)
        acc = sum(labels[i] for i in idx) / len(idx)
        total += abs(conf - acc) * len(idx) / len(probs)
    return total


def fit(rows: Sequence[Mapping]) -> dict:
    """Fit the [know] logistic the way calibrate_know_confidence.py does."""
    s_ref = max(cal_mod._median([float(r["top_score"]) for r in rows]), 1e-3)
    g_ref = max(cal_mod._median([float(r["score_gap"]) for r in rows]), 1e-3)
    feats, labels = [], []
    for r in rows:
        f, y = cal_mod._row_to_features(dict(r), s_ref=s_ref, g_ref=g_ref)
        feats.append(f)
        labels.append(y)
    sk = cal_mod._try_sklearn()
    if sk is not None and len(set(labels)) == 2:
        clf = sk(penalty="l2", C=1.0, max_iter=1000, solver="lbfgs")
        clf.fit(feats, labels)
        betas = [float(clf.intercept_[0]), *(float(c) for c in clf.coef_[0])]
        fitter = "sklearn.LogisticRegression"
    else:
        betas = list(cal_mod.fit_betas_from_features(
            feats, labels, n_features=len(feats[0]), lr=0.1, epochs=500, l2=1e-4))
        fitter = "fit_betas_from_features"
    return {"betas": betas, "s_ref": s_ref, "g_ref": g_ref, "fitter": fitter}


def _metrics(probs: Sequence[float], labels: Sequence[int], emit_floor: Optional[float] = None) -> dict:
    out = {"n": len(labels), "base_rate": (sum(labels) / len(labels)) if labels else None,
           "auc": cal_mod.compute_auc(list(probs), list(labels)),
           "ece": expected_calibration_error(probs, labels),
           "max_prob": max(probs) if probs else None}
    if emit_floor is not None:
        emitted = [y for p, y in zip(probs, labels) if p >= emit_floor]
        out["emit_floor"] = emit_floor
        out["emit_rate"] = len(emitted) / len(labels) if labels else None
        out["emit_precision"] = (sum(emitted) / len(emitted)) if emitted else None
    return out


def leave_one_corpus_out(rows: Sequence[Mapping]) -> List[dict]:
    """Fit on all other corpora, score the held-out one; one fold per corpus."""
    corpora = sorted({r["corpus"] for r in rows})
    folds = []
    for held in corpora:
        train = [r for r in rows if r["corpus"] != held]
        test = [r for r in rows if r["corpus"] == held]
        cal = fit(train)
        probs = probabilities(cal, test)
        m = _metrics(probs, [r["label"] for r in test])
        folds.append({"held_out": held, "n_train": len(train), "n_test": len(test),
                      **{k: m[k] for k in ("auc", "ece", "base_rate", "max_prob")},
                      "probs": probs})
    return folds


def build_report(rows: Sequence[Mapping], shipped: Mapping) -> dict:
    labels = [r["label"] for r in rows]
    by_corpus: Dict[str, List[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        by_corpus[r["corpus"]].append(i)

    shipped_probs = probabilities(shipped, rows)
    shipped_m = _metrics(shipped_probs, labels, shipped.get("emit_floor"))
    shipped_m["per_corpus"] = {
        c: _metrics([shipped_probs[i] for i in idx], [labels[i] for i in idx], shipped.get("emit_floor"))
        for c, idx in sorted(by_corpus.items())}

    refit = fit(rows)
    refit_m = _metrics(probabilities(refit, rows), labels)

    folds = leave_one_corpus_out(rows) if len(by_corpus) > 1 else []
    oof_probs, oof_labels = [], []
    for f in folds:
        oof_probs += f["probs"]
        oof_labels += [r["label"] for r in rows if r["corpus"] == f["held_out"]]
    curve = []
    for th in (0.3, 0.4, 0.45, 0.5, 0.6, 0.7, 0.8, 0.9):
        kept = [y for p, y in zip(oof_probs, oof_labels) if p >= th]
        curve.append({"threshold": th, "coverage": len(kept) / len(oof_labels) if oof_labels else None,
                      "precision": (sum(kept) / len(kept)) if kept else None})
    return {
        "n_rows": len(rows),
        "corpora": {c: len(idx) for c, idx in sorted(by_corpus.items())},
        "label": "gold at score-map rank 1 (calibrate_know_confidence.py definition)",
        "shipped": {"calibration": {k: shipped[k] for k in ("betas", "s_ref", "g_ref", "emit_floor")
                                    if k in shipped}, **shipped_m},
        "refit_all": {"calibration": refit, "in_sample": refit_m},
        "leave_one_corpus_out": {
            "folds": [{k: v for k, v in f.items() if k != "probs"} for f in folds],
            "pooled_out_of_fold": _metrics(oof_probs, oof_labels) if oof_labels else None,
            "precision_coverage": curve,
        },
    }


def shipped_calibration(config_path: Path) -> dict:
    know = tomllib.loads(config_path.read_text(encoding="utf-8"))["know"]
    return {"betas": list(know["betas"]), "s_ref": float(know["s_ref"]),
            "g_ref": float(know["g_ref"]), "emit_floor": float(know["emit_floor"])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--receipt", action="append", required=True, metavar="CORPUS=PATH")
    ap.add_argument("--arm", default="postflip_default")
    ap.add_argument("--config", default=str(ROOT / "cymatix.toml"),
                    help="read-only source of the shipped [know] calibration")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    rows: List[dict] = []
    sources = {}
    for spec in args.receipt:
        corpus, path = spec.split("=", 1)
        receipt = json.loads(Path(path).read_text(encoding="utf-8"))
        arm = next(a for a in receipt["arms"] if a["arm"] == args.arm)
        rows += replay_rows_to_calibration(arm["per_query"], corpus)
        sources[corpus] = {"receipt": path, "git_sha": receipt.get("git_sha"), "bed": receipt.get("bed")}
    report = build_report(rows, shipped_calibration(Path(args.config)))
    report["sources"] = sources
    report["arm"] = args.arm
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    s, lo = report["shipped"], report["leave_one_corpus_out"]["pooled_out_of_fold"]
    print(f"rows {report['n_rows']} over {len(report['corpora'])} corpora; shipped AUC {s['auc']} "
          f"emit {s.get('emit_rate')}; LOCO pooled AUC {lo and lo['auc']} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

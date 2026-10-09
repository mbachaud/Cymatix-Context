"""Calibrate the [know] lanes model to ONE store from its own labelled queries (#482).

The lanes model (scripts/fit_know_lanes.py) ranks well across corpora, but its
absolute scale does not transfer between stores. This fits, for one store:

* a Platt rescale of the lanes logit, z' = a*z + b (``lanes_platt_a`` /
  ``lanes_platt_b``), on the store's labelled ``know_abstain_replay`` rows
  (label = gold at score-map rank 1), and
* the lowest ``emit_floor`` whose precision on that data meets
  ``--target-precision`` with at least ``--min-support`` queries.

It first runs k-fold cross-validation (Platt + floor chosen on k-1 folds,
scored on the held-out fold) and reports the held-out coverage and precision.
If the held-out precision misses the target (or nothing is ever emitted), it
writes the report with verdict ``no_usable_floor``, writes NO TOML, and exits 2:
an honest "this store cannot support a confident know", not a fitted floor
that only works in-sample. Otherwise it refits on all rows and writes a
``[know]`` block (the store's current lanes model + Platt pair + floor) to
``--toml-out``. It never edits cymatix.toml.

Usage:
  python scripts/calibrate_know_store.py --receipt <store replay.json> [--receipt ...] \
      --config <toml with [know] model = "lanes"> --target-precision 0.8 \
      --out report.json --toml-out know_store.toml
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cymatix_context.config import load_config  # noqa: E402
from cymatix_context.scoring.know_lanes import LanesModel, feature_values  # noqa: E402


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _logit(p: float) -> float:
    p = min(max(p, 1e-12), 1 - 1e-12)
    return math.log(p / (1 - p))


def _nll(a: float, b: float, z: Sequence[float], y: Sequence[int], l2: float) -> float:
    total = 0.5 * l2 * (a * a + b * b)
    for zi, yi in zip(z, y):
        t = a * zi + b
        # log(1 + e^t) - y*t, computed stably
        total += (t if t > 0 else 0.0) + math.log1p(math.exp(-abs(t))) - yi * t
    return total


def fit_platt(z: Sequence[float], y: Sequence[int], iters: int = 100, l2: float = 1e-6) -> Tuple[float, float]:
    """Logistic fit of y ~ sigmoid(a*z + b): Newton steps with a backtracking line search.

    Undamped Newton from (1, 0) diverged (a ~ 8e8) on ERB's CE-lanes logits
    (z ~ -3.6); every step here must lower the negative log-likelihood. Starts
    at the base-rate intercept.
    """
    base = min(max(sum(y) / len(y), 1e-6), 1 - 1e-6) if y else 0.5
    a, b = 1.0, math.log(base / (1 - base)) - (sum(z) / len(z) if z else 0.0)
    f = _nll(a, b, z, y, l2)
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for zi, yi in zip(z, y):
            p = _sigmoid(a * zi + b)
            r = p - yi
            w = p * (1 - p)
            ga += r * zi
            gb += r
            haa += w * zi * zi
            hab += w * zi
            hbb += w
        ga += l2 * a
        haa += l2
        hbb += l2
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        step = 1.0
        while step > 1e-10:
            na, nb = a - step * da, b - step * db
            nf = _nll(na, nb, z, y, l2)
            if nf <= f:
                break
            step /= 2.0
        else:
            break
        moved = abs(na - a) + abs(nb - b)
        a, b, f = na, nb, nf
        if moved < 1e-10:
            break
    return a, b


def choose_floor(p: Sequence[float], y: Sequence[int], target: float, min_support: int) -> Optional[float]:
    """Lowest threshold whose kept set has precision >= target and >= min_support rows."""
    for th in sorted(set(round(v, 6) for v in p)):
        kept = [yi for pi, yi in zip(p, y) if pi >= th]
        if len(kept) < min_support:
            break
        if sum(kept) / len(kept) >= target:
            return th
    return None


def load_rows(paths: Sequence[str], arm: str) -> List[dict]:
    rows: List[dict] = []
    for path in paths:
        receipt = json.loads(Path(path).read_text(encoding="utf-8"))
        a = next((x for x in receipt["arms"] if x["arm"] == arm), None)
        if a is None:
            raise SystemExit(f"{path}: no arm {arm!r}")
        rows += [r for r in a["per_query"] if r.get("top_score") is not None]
    if rows and not any("lane_signals" in r for r in rows):
        raise SystemExit("rows carry no lane_signals (replay predates 2026-10-05); re-run the replay")
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--receipt", action="append", required=True)
    ap.add_argument("--arm", default="postflip_default")
    ap.add_argument("--config", required=True, help="TOML whose [know] holds the lanes model to calibrate")
    ap.add_argument("--target-precision", type=float, default=0.8)
    ap.add_argument("--min-support", type=int, default=5)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--toml-out", required=True)
    a = ap.parse_args(argv)

    know = load_config(a.config).know
    if know.model != "lanes":
        raise SystemExit(f"{a.config}: [know] model is {know.model!r}, not 'lanes'")
    base = LanesModel(know.lanes_intercept, dict(know.lanes_betas))  # uncalibrated: identity Platt
    with_ce = bool(know.lanes_ce_model)
    rows = load_rows(a.receipt, a.arm)
    if with_ce and not any("ce" in r for r in rows):
        raise SystemExit("[know] lanes_ce_model is set but the rows carry no ce signals; "
                         "re-run know_abstain_replay with --ce-model")
    z = [_logit(base.confidence(feature_values(r, with_ce=with_ce))) for r in rows]
    y = [1 if r.get("rank_of_first_gold") == 1 else 0 for r in rows]

    idx = list(range(len(rows)))
    random.Random(a.seed).shuffle(idx)
    folds = [idx[i::a.folds] for i in range(a.folds)]
    kept_n = kept_pos = 0
    fold_rows = []
    for k, te in enumerate(folds):
        tes = set(te)
        tr = [i for i in idx if i not in tes]
        pa, pb = fit_platt([z[i] for i in tr], [y[i] for i in tr])
        p_tr = [_sigmoid(pa * z[i] + pb) for i in tr]
        floor = choose_floor(p_tr, [y[i] for i in tr], a.target_precision, a.min_support)
        sel = [] if floor is None else [i for i in te if _sigmoid(pa * z[i] + pb) >= floor]
        kept_n += len(sel)
        kept_pos += sum(y[i] for i in sel)
        fold_rows.append({"fold": k, "platt_a": pa, "platt_b": pb, "floor": floor,
                          "held_out_kept": len(sel), "held_out_correct": sum(y[i] for i in sel)})
    held_prec = kept_pos / kept_n if kept_n else None
    held_cov = kept_n / len(rows) if rows else 0.0
    usable = held_prec is not None and held_prec >= a.target_precision

    report = {
        "tool": "scripts/calibrate_know_store.py", "issue": "#482",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "receipts": a.receipt, "config": a.config,
        "label": "gold at score-map rank 1", "n": len(rows), "base_rate": sum(y) / len(y) if y else None,
        "target_precision": a.target_precision, "min_support": a.min_support,
        "cross_validation": {"folds": fold_rows, "held_out_coverage": held_cov,
                             "held_out_precision": held_prec, "held_out_kept": kept_n},
        "verdict": "usable" if usable else "no_usable_floor",
    }
    if usable:
        pa, pb = fit_platt(z, y)
        floor = choose_floor([_sigmoid(pa * zi + pb) for zi in z], y, a.target_precision, a.min_support)
        report["fit_all"] = {"platt_a": pa, "platt_b": pb, "emit_floor": floor}
        betas = ", ".join(f"{k} = {v!r}" for k, v in know.lanes_betas.items())
        Path(a.toml_out).write_text(
            "# [know] lanes model calibrated to ONE store by scripts/calibrate_know_store.py (#482).\n"
            f"# {len(rows)} labelled queries; {a.folds}-fold held-out precision {held_prec:.3f} at coverage "
            f"{held_cov:.3f} (target {a.target_precision}); report {a.out}\n"
            "[know]\n"
            'model = "lanes"\n'
            f"emit_floor = {floor!r}\n"
            f"lanes_intercept = {know.lanes_intercept!r}\n"
            f"lanes_betas = {{ {betas} }}\n"
            f"lanes_platt_a = {pa!r}\n"
            f"lanes_platt_b = {pb!r}\n"
            + (f"lanes_ce_model = {json.dumps(know.lanes_ce_model)}\n" if with_ce else ""),
            encoding="utf-8",
        )
    Path(a.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"{report['verdict']}: held-out precision {held_prec} at coverage {held_cov:.3f} "
          f"(target {a.target_precision}, n={len(rows)})")
    return 0 if usable else 2


if __name__ == "__main__":
    raise SystemExit(main())

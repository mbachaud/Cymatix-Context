"""Fit the KnowBlock confidence logistic from a labeled bench output.

Spec: docs/specs/2026-05-08-stage-6-know-miss-blocks.md §3, §11.

This script is OPERATOR-RUN, not auto-run. The Stage 6 PR ships with
default coefficients (see ``cymatix_context.know_calibration``); the
calibration step is a one-time post-merge action against
``located_n1000`` once Stage 1 has landed and the bench data exists.

Workflow:

    1. Run benchmarks/located_n1000.py. It produces a JSONL file with
       one row per query carrying the four feature signals plus the
       ground-truth ``planted_gene_id == retrieved_top1`` label.

    2. python scripts/calibrate_know_confidence.py \\
           --input results/located_n1000.jsonl \\
           --out helix.toml

    3. Confirm helix.toml [know] table updated; redeploy.

The script is intentionally light on dependencies. sklearn is used
when available (faster convergence; pip install scikit-learn);
without it, the pure-Python gradient descent in
``cymatix_context.know_calibration.fit_betas_from_features`` is used.

# STAGE-7-EXT: Stage 7 adds freshness_min as a fifth feature. The
# JSONL row gets a fifth column; the ``--n-features`` flag below picks
# it up automatically. Stage 7 ships a default beta5; the operator
# re-runs this script to re-fit on stale-needle-augmented bench data.

## Issue #239: hit/miss AUC trust gate

The 2026-07-05 SIKE bedsweep review found the KnowBlock confidence
*inverted* on every bed (misses scored higher than hits — AUC < 0.5,
worse than a coin flip). Because the know/miss contract is what
downstream agents branch on, this script now computes the held-out
hit/miss AUC of the fitted logistic (rank-based Mann-Whitney U
formulation — no sklearn/numpy dependency, see ``compute_auc()``
below) and **refuses to write ``[know]`` betas when that AUC is below
``--auc-floor`` (default 0.7)**. Pass ``--force`` to override (loud
WARNING logged; use only when you have already root-caused why the
signal is weak — e.g. a known-contaminated bench).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# Repo-relative import: the script lives in scripts/ next to the package.
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))

from cymatix_context.scoring.know_calibration import (  # noqa: E402
    DEFAULT_EMIT_FLOOR,
    DEFAULT_G_REF,
    DEFAULT_S_REF,
    KnowCalibration,
    N_FEATURES,
    _sigmoid,
    fit_betas_from_features,
)

log = logging.getLogger("helix.calibrate_know_confidence")

# Issue #239: minimum acceptable held-out hit/miss AUC before the script
# will write ``[know]`` betas. Below this, a downstream agent trusting
# the KnowBlock confidence would be misled worse than a coin flip in
# the worst observed case (AUC ~0.35-0.44 on the contaminated beds).
DEFAULT_AUC_FLOOR: float = 0.7


def _try_sklearn():
    try:
        from sklearn.linear_model import LogisticRegression  # type: ignore[import-not-found]
        return LogisticRegression
    except ImportError:
        return None


def _load_rows(path: Path) -> List[dict]:
    """Load JSONL rows. Tolerates blank lines and trailing whitespace."""
    rows: List[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for ln, line in enumerate(fh, 1):
            s = line.strip()
            if not s:
                continue
            try:
                rows.append(json.loads(s))
            except json.JSONDecodeError as exc:
                log.warning("skipping malformed JSONL row at line %d: %s", ln, exc)
    if not rows:
        raise SystemExit(f"No usable rows in {path}")
    return rows


def _row_to_features(
    row: dict,
    *,
    s_ref: float,
    g_ref: float,
) -> Tuple[List[float], int]:
    """Convert a bench row to (features, label).

    Expected row keys:
      top_score             float
      score_gap             float
      lexical_dense_agree   bool/int
      coordinate_confidence float in [0, 1]
      label                 0/1 (1 = ground-truth retrieval-success)
      freshness_min         float in [0, 1], optional (Stage 7, feature 4)

    Stage 7: the feature vector is always N_FEATURES (5) long. A missing
    ``freshness_min`` contributes 0.0 — byte-matching
    ``compute_confidence``'s None branch (no contribution to z), so
    training rows and inference agree on legacy data.
    """
    top_score = float(row.get("top_score", 0.0))
    score_gap = float(row.get("score_gap", 0.0))
    agree = bool(row.get("lexical_dense_agree", False))
    coord = float(row.get("coordinate_confidence", 0.0))
    label = int(bool(row.get("label", 0)))
    fresh_raw = row.get("freshness_min")
    fresh = 0.0 if fresh_raw is None else max(0.0, min(1.0, float(fresh_raw)))

    feat = [
        math.tanh(top_score / s_ref),
        math.tanh(score_gap / g_ref),
        1.0 if agree else 0.0,
        max(0.0, min(1.0, coord)),
        fresh,
    ]
    return feat, label


def _median(values: Sequence[float]) -> float:
    if not values:
        return 1.0
    sv = sorted(values)
    n = len(sv)
    mid = n // 2
    if n % 2 == 1:
        return float(sv[mid])
    return float((sv[mid - 1] + sv[mid]) / 2.0)


def _train_test_split(
    feats: Sequence[Sequence[float]],
    labels: Sequence[int],
    *,
    test_frac: float = 0.2,
    seed: int = 42,
) -> Tuple[list, list, list, list]:
    import random
    rng = random.Random(seed)
    idx = list(range(len(feats)))
    rng.shuffle(idx)
    n_test = max(1, int(len(idx) * test_frac))
    test_idx = set(idx[:n_test])
    f_train, l_train, f_test, l_test = [], [], [], []
    for i in range(len(feats)):
        if i in test_idx:
            f_test.append(list(feats[i]))
            l_test.append(int(labels[i]))
        else:
            f_train.append(list(feats[i]))
            l_train.append(int(labels[i]))
    return f_train, l_train, f_test, l_test


def _precision_at_threshold(
    probs: Sequence[float],
    labels: Sequence[int],
    threshold: float,
) -> Optional[float]:
    """Precision = TP / (TP + FP) for KnowBlock-emit gate."""
    tp = sum(1 for p, y in zip(probs, labels) if p >= threshold and y == 1)
    fp = sum(1 for p, y in zip(probs, labels) if p >= threshold and y == 0)
    if (tp + fp) == 0:
        return None
    return tp / (tp + fp)


def _pick_emit_floor(
    probs: Sequence[float],
    labels: Sequence[int],
    *,
    target_precision: float = 0.95,
) -> float:
    """Sweep thresholds; pick lowest threshold meeting target precision.

    Falls back to DEFAULT_EMIT_FLOOR if no threshold can achieve the
    target (small / heavily imbalanced calibration sets).
    """
    candidates = sorted({round(p, 3) for p in probs})
    best: Optional[float] = None
    for th in candidates:
        prec = _precision_at_threshold(probs, labels, th)
        if prec is not None and prec >= target_precision:
            best = th
            break
    return float(best) if best is not None else DEFAULT_EMIT_FLOOR


def _write_helix_toml(
    out_path: Path,
    cal: KnowCalibration,
) -> None:
    """Write/update the [know] table in ``helix.toml``.

    Strategy: read the existing file (if any) line-by-line, locate the
    [know] section, replace it; or append if absent. tomllib parses
    but does not write — to avoid a hard dep on tomli-w we hand-craft
    the table. This is a dozen-key block so the cost is trivial.
    """
    new_block = (
        f"[know]\n"
        f"emit_floor      = {cal.emit_floor}\n"
        f"s_ref           = {cal.s_ref}\n"
        f"g_ref           = {cal.g_ref}\n"
        f"betas           = {list(cal.betas)}\n"
        + (
            f'calibrated_at   = "{cal.calibrated_at}"\n'
            if cal.calibrated_at
            else ""
        )
        + (
            f"calibrated_on_n = {cal.calibrated_on_n}\n"
            if cal.calibrated_on_n is not None
            else ""
        )
    )
    if not out_path.exists():
        out_path.write_text(new_block, encoding="utf-8")
        return
    existing = out_path.read_text(encoding="utf-8").splitlines(keepends=False)
    out_lines: list[str] = []
    in_know = False
    skipped_header = False
    found_know = False
    for line in existing:
        stripped = line.strip()
        if stripped.startswith("[know]"):
            found_know = True
            in_know = True
            skipped_header = True
            continue
        if in_know:
            if stripped.startswith("[") and stripped.endswith("]"):
                in_know = False
                # Insert the new block before this section.
                out_lines.append(new_block.rstrip("\n"))
                out_lines.append("")
                out_lines.append(line)
                continue
            # eat lines that belong to the old [know] table
            continue
        out_lines.append(line)
    if found_know and skipped_header and in_know:
        # [know] was the last section in the file — append the new block.
        out_lines.append(new_block.rstrip("\n"))
    elif not found_know:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append(new_block.rstrip("\n"))
    out_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


class AUCGateError(RuntimeError):
    """Raised when the held-out hit/miss AUC fails issue #239's trust gate.

    Raised by ``gate_auc_or_raise`` / ``write_calibration_gated`` when
    ``force=False``. The CLI ``main()`` catches this at the boundary and
    turns it into a printed stderr message + non-zero exit code; tests
    exercise the gate directly with ``pytest.raises(AUCGateError)``.
    """


def compute_auc(
    scores: Sequence[float],
    labels: Sequence[int],
) -> Optional[float]:
    """Hit/miss AUC via the rank-based Mann-Whitney U formulation.

    AUC = P(score(random hit) > score(random miss)), estimated as::

        U   = sum_of_ranks(positive-class scores) - n_pos*(n_pos+1)/2
        AUC = U / (n_pos * n_neg)

    Ranks are 1-indexed and tied scores share the average rank of their
    block (the standard mid-rank tie-break), which is what makes this
    exactly equal to sklearn's ``roc_auc_score`` for the binary case —
    no sklearn or numpy import required, so the gate has zero additional
    runtime dependencies beyond what the calibration script already
    needs.

    Returns ``None`` when the sample has only one class present (no
    positives or no negatives) — AUC is undefined in that case, not 0.5;
    callers must treat ``None`` as "cannot vouch for this fit", not as
    a passing score.
    """
    n = len(scores)
    if n != len(labels):
        raise ValueError("scores and labels length mismatch")
    n_pos = sum(1 for y in labels if int(y) == 1)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return None

    order = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        # 1-indexed average rank across the tied block [i, j].
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1

    rank_sum_pos = sum(ranks[i] for i in range(n) if int(labels[i]) == 1)
    u_stat = rank_sum_pos - (n_pos * (n_pos + 1)) / 2.0
    return u_stat / (n_pos * n_neg)


def gate_auc_or_raise(
    auc: Optional[float],
    *,
    floor: float = DEFAULT_AUC_FLOOR,
    force: bool = False,
) -> None:
    """Enforce issue #239's hit/miss AUC trust gate.

    ``auc is None`` (undefined — a single-class held-out split) is
    treated as a failure: there is no basis to trust an undefined
    number. Raises ``AUCGateError`` unless ``force=True``, in which case
    a loud WARNING is logged and the function returns normally so the
    caller proceeds to write the (untrusted) betas anyway.
    """
    if auc is not None and auc >= floor:
        log.info("AUC gate PASSED: held-out hit/miss AUC=%.4f >= floor=%.2f", auc, floor)
        return

    auc_repr = f"{auc:.4f}" if auc is not None else "undefined (single-class held-out split)"
    msg = (
        f"AUC gate FAILED: held-out hit/miss AUC={auc_repr} is below the "
        f"trust floor {floor:.2f}. Refusing to write [know] betas — an "
        f"agent that trusts a KnowBlock confidence this poorly separated "
        f"(or undefined) could be actively misled (issue #239). Re-run "
        f"with --force to override only if you have already root-caused "
        f"why the signal is weak (e.g. a known-contaminated bench)."
    )
    if force:
        log.warning("AUC gate OVERRIDDEN via --force. %s", msg)
        return
    log.error(msg)
    raise AUCGateError(msg)


def write_calibration_gated(
    out_path: Path,
    cal: KnowCalibration,
    *,
    auc: Optional[float],
    floor: float = DEFAULT_AUC_FLOOR,
    force: bool = False,
) -> None:
    """Gate on hit/miss AUC, then write the ``[know]`` table if it clears.

    Thin wrapper around ``_write_helix_toml`` so tests can exercise the
    refuse/accept decision with synthetic ``auc``/``cal`` values —
    without running a real logistic fit. Raises ``AUCGateError`` (and
    does NOT touch ``out_path``) when the gate fails and ``force`` is
    False.
    """
    gate_auc_or_raise(auc, floor=floor, force=force)
    _write_helix_toml(out_path, cal)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to bench JSONL output (one row per query).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("cymatix.toml") if Path("cymatix.toml").exists() else Path("helix.toml"),
        help="Path to the cymatix.toml file to update (default: cymatix.toml, "
        "falling back to helix.toml).",
    )
    parser.add_argument(
        "--target-precision",
        type=float,
        default=0.95,
        help="Operating-point precision for emit_floor (default: 0.95).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for the train/test split (default: 42).",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "Run on a tiny synthetic fixture instead of --input. Used "
            "by the test suite to verify the script wires together; "
            "does NOT touch helix.toml."
        ),
    )
    parser.add_argument(
        "--n-features",
        type=int,
        default=N_FEATURES,
        help=(
            "Number of features (default 4 for Stage 6; Stage 7 will "
            "make this 5 by adding freshness_min)."
        ),
    )
    parser.add_argument(
        "--auc-floor",
        type=float,
        default=DEFAULT_AUC_FLOOR,
        help=(
            "Minimum held-out hit/miss AUC (issue #239) required before "
            f"[know] betas are written (default: {DEFAULT_AUC_FLOOR})."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Override the AUC gate and write [know] betas even when the "
            "held-out AUC is below --auc-floor. Logs a loud WARNING. Only "
            "use once you have root-caused why the signal is weak."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.smoke:
        # Synthesize a separable-by-design fixture. The two clusters
        # are linearly separable so any reasonable fitter recovers
        # positive coefficients on all N_FEATURES (5) features —
        # Stage 7 added freshness_min as feature index 4.
        rows: list[dict] = []
        for i in range(40):
            rows.append({
                "top_score": 2.0 + (i % 5) * 0.1,
                "score_gap": 0.8 + (i % 4) * 0.05,
                "lexical_dense_agree": True,
                "coordinate_confidence": 0.7 + (i % 3) * 0.05,
                "freshness_min": 0.85 + (i % 3) * 0.05,
                "label": 1,
            })
        for i in range(40):
            rows.append({
                "top_score": 0.05 + (i % 5) * 0.01,
                "score_gap": 0.005,
                "lexical_dense_agree": False,
                "coordinate_confidence": 0.0,
                "freshness_min": 0.05,
                "label": 0,
            })
        log.info("smoke: %d synthetic rows", len(rows))
    else:
        if not args.input.exists():
            print(f"ERROR: --input {args.input} does not exist", file=sys.stderr)
            return 2
        rows = _load_rows(args.input)
        log.info("loaded %d rows from %s", len(rows), args.input)

    # Pick s_ref / g_ref as medians of the calibration set so tanh
    # saturates around the typical retriever scale (§11 step 4).
    top_scores = [float(r.get("top_score", 0.0)) for r in rows]
    score_gaps = [float(r.get("score_gap", 0.0)) for r in rows]
    s_ref = max(_median(top_scores), 1e-3)
    g_ref = max(_median(score_gaps), 1e-3)
    log.info("s_ref=%.4f, g_ref=%.4f (medians of calibration set)", s_ref, g_ref)

    feats: list[list[float]] = []
    labels: list[int] = []
    for r in rows:
        f, y = _row_to_features(r, s_ref=s_ref, g_ref=g_ref)
        feats.append(f)
        labels.append(y)

    f_train, l_train, f_test, l_test = _train_test_split(
        feats, labels, test_frac=0.2, seed=args.seed
    )
    log.info("train=%d  test=%d", len(f_train), len(f_test))

    SkLR = _try_sklearn()
    if SkLR is not None:
        log.info("using sklearn.LogisticRegression")
        try:
            import numpy as np  # noqa: F401  (sklearn pulls it in transitively)
        except ImportError:
            np = None  # type: ignore
        clf = SkLR(penalty="l2", C=1.0, max_iter=1000, solver="lbfgs")
        clf.fit(f_train, l_train)
        # sklearn returns intercept[0] + coef_[0][i] for feature i.
        coef = clf.coef_[0]
        intercept = float(clf.intercept_[0])
        betas: tuple[float, ...] = (intercept, *(float(c) for c in coef))
    else:
        log.info("using fit_betas_from_features (pure-Python GD)")
        betas = fit_betas_from_features(
            f_train, l_train,
            n_features=args.n_features,
            lr=0.1,
            epochs=500,
            l2=1e-4,
        )

    # Probabilities on held-out test set.
    test_probs: list[float] = []
    for x in f_test:
        z = betas[0] + sum(betas[i + 1] * x[i] for i in range(len(x)))
        test_probs.append(_sigmoid(z))

    emit_floor = _pick_emit_floor(
        test_probs, l_test, target_precision=args.target_precision
    )
    log.info(
        "test precision@%0.2f: %s, picked emit_floor=%.3f",
        args.target_precision,
        _precision_at_threshold(test_probs, l_test, emit_floor),
        emit_floor,
    )

    # Issue #239: held-out hit/miss AUC — the trust gate that decides
    # whether these betas are allowed to reach helix.toml at all.
    auc = compute_auc(test_probs, l_test)
    log.info(
        "held-out hit/miss AUC: %s (floor=%.2f)",
        f"{auc:.4f}" if auc is not None else "undefined (single-class holdout)",
        args.auc_floor,
    )

    # Bundle the result.
    import datetime as _dt
    cal = KnowCalibration(
        betas=tuple(round(b, 4) for b in betas),
        s_ref=round(s_ref, 4),
        g_ref=round(g_ref, 4),
        emit_floor=round(emit_floor, 3),
        calibrated_at=_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        calibrated_on_n=len(feats),
    )

    if args.smoke:
        log.info("smoke: betas=%s emit_floor=%s (NOT writing helix.toml)",
                 cal.betas, cal.emit_floor)
        # Sanity assertions: the synthetic fixture is separable, so
        # the test-set probs should split cleanly.
        pos_probs = [p for p, y in zip(test_probs, l_test) if y == 1]
        neg_probs = [p for p, y in zip(test_probs, l_test) if y == 0]
        if pos_probs and neg_probs:
            assert min(pos_probs) > max(neg_probs), (
                f"smoke fit failed to separate: min(pos)={min(pos_probs)}, "
                f"max(neg)={max(neg_probs)}"
            )
            log.info("smoke: separation OK (min pos %.3f > max neg %.3f)",
                     min(pos_probs), max(neg_probs))
        return 0

    try:
        write_calibration_gated(
            args.out, cal, auc=auc, floor=args.auc_floor, force=args.force,
        )
    except AUCGateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    log.info("wrote [know] table to %s", args.out)
    log.info("betas=%s emit_floor=%s auc=%s", cal.betas, cal.emit_floor, auc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

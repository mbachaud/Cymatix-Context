"""KnowBlock confidence calibration — pure-function logistic.

Spec: docs/specs/2026-05-08-stage-6-know-miss-blocks.md §3, §11.

The Stage 6 contract assigns a calibrated probability to every
KnowBlock so frontier agents can branch on a real number rather than
prose health hints. The mapping is a 4-feature logistic regression:

    z = b0
      + b1 * tanh(top_score / s_ref)
      + b2 * tanh(score_gap  / g_ref)
      + b3 * (1.0 if lexical_dense_agree else 0.0)
      + b4 * coordinate_confidence
    confidence = sigmoid(z) = 1 / (1 + exp(-z))

Defaults (pre-calibration, see §3):
    betas      = (-2.0, 2.0, 1.5, 0.7, 1.8)
    s_ref      = 1.0
    g_ref      = 0.5
    emit_floor = 0.55

Calibration is an operator post-merge action via
``scripts/calibrate_know_confidence.py`` against ``located_n1000``. Until
then defaults are the contract; KnowBlock will still emit, but the
operating point may not be precision-95.

# STAGE-7-EXT: this module ships the 4-feature implementation. Stage 7
#  extends the logistic to 5 features (adds freshness_min as the fifth
#  signal with default beta5 = +1.5). Look for `# STAGE-7-EXT` markers
#  to find the exact lines that need to change. The data-class wrapper
#  ``KnowCalibration`` carries the feature count so the call site can
#  do feature-length validation without spreading magic numbers.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

log = logging.getLogger("helix.know_calibration")


# ─────────────────────────────────────────────────────────────────────
# Defaults (Stage 6 ship-time values from §3 of the spec)
# ─────────────────────────────────────────────────────────────────────

# (b0, b1, b2, b3, b4, b5)  ──  intercept + 5 feature coefficients.
# Stage 7 (2026-05-08) appended b5 = +1.5 for freshness_min: a fresh
# top-1 (decay near 1.0) adds ~+1.5 to the logit, a stale top-1
# (decay near 0.0) contributes nothing — pushing borderline-confident
# stale retrievals below ``emit_floor`` so they fall through to
# MissBlock(reason="stale") instead of emitting a soft-known answer
# the agent will treat as authoritative.
DEFAULT_BETAS: tuple[float, ...] = (-2.0, 2.0, 1.5, 0.7, 1.8, 1.5)

# Feature-scale references for tanh-squashing on top_score / score_gap.
DEFAULT_S_REF: float = 1.0
DEFAULT_G_REF: float = 0.5

# Probability floor below which a KnowBlock is not emitted; the
# decision falls through to MissBlock(reason="sparse").
DEFAULT_EMIT_FLOOR: float = 0.55

# Number of feature inputs (excluding intercept) the logistic accepts.
# Stage 7: bumped to 5 — added freshness_min as feature index 4.
N_FEATURES: int = 5


# ─────────────────────────────────────────────────────────────────────
# Data class — bundles betas + scale refs + floor; loaded from helix.toml
# ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class KnowCalibration:
    """Bundle of calibration parameters for ``compute_confidence``.

    Frozen so callers can pass it across threads without locking.
    """

    betas: tuple[float, ...] = field(default_factory=lambda: DEFAULT_BETAS)
    s_ref: float = DEFAULT_S_REF
    g_ref: float = DEFAULT_G_REF
    emit_floor: float = DEFAULT_EMIT_FLOOR
    calibrated_at: Optional[str] = None
    calibrated_on_n: Optional[int] = None

    def expected_betas_len(self) -> int:
        """Required length of the betas tuple: intercept + N_FEATURES.

        Used by validators and the calibration script.
        """
        return 1 + N_FEATURES


# ─────────────────────────────────────────────────────────────────────
# Pure-function logistic
# ─────────────────────────────────────────────────────────────────────

def _sigmoid(z: float) -> float:
    """Numerically-stable logistic.

    For very negative z the naive form ``1 / (1 + exp(-z))`` is fine;
    for very positive z the naive form is also fine. The two-branch
    split is just to avoid overflow warnings in pathological tests.
    """
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def compute_confidence(
    *,
    top_score: float,
    score_gap: float,
    lexical_dense_agree: bool,
    coordinate_confidence: float,
    calibration: Optional[KnowCalibration] = None,
    freshness_min: Optional[float] = None,
) -> float:
    """Map five signals to a calibrated KnowBlock confidence.

    All inputs are clamped/squashed before entering the linear
    combination so out-of-distribution values do not blow up the
    logit. Returns a probability in [0, 1].

    Args:
        top_score: raw rank-1 score from the retriever (post-fusion).
        score_gap: top1 - top2 score gap in the same units as top_score.
        lexical_dense_agree: True if the top-K of the lexical and the
            dense rankers intersect. Cheap binary signal.
        coordinate_confidence: blend of folder + file-grain path overlap
            in [0, 1] (see context_packet._coordinate_confidence).
        calibration: optional override; defaults to ``KnowCalibration()``.
        freshness_min: Stage 7 (spec §10) — minimum decay across the
            retrieved candidates, in [0, 1]. ``None`` is treated as
            "freshness unknown" (no contribution to z) — preserves
            back-compat for legacy rows where ``last_verified_at`` is
            NULL. With the default β5 = +1.5, a fully fresh top-K adds
            ~+1.5 to the logit; a fully stale top-K adds ~0.0,
            shaving ~0.3 off the calibrated probability and pushing
            borderline cases under emit_floor.

    Returns:
        Probability in [0, 1].
    """
    cal = calibration or KnowCalibration()
    betas = cal.betas
    if len(betas) != cal.expected_betas_len():
        log.warning(
            "know_calibration: betas length %d != expected %d; falling "
            "back to defaults",
            len(betas),
            cal.expected_betas_len(),
        )
        betas = DEFAULT_BETAS

    # Squash top_score and score_gap with tanh so saturating outliers
    # cannot dominate the logit. Reference scales come from helix.toml
    # so calibration can re-tune them to the current retriever.
    s_ref = cal.s_ref if cal.s_ref > 0 else DEFAULT_S_REF
    g_ref = cal.g_ref if cal.g_ref > 0 else DEFAULT_G_REF

    z = float(betas[0])
    z += float(betas[1]) * math.tanh(float(top_score) / s_ref)
    z += float(betas[2]) * math.tanh(float(score_gap) / g_ref)
    z += float(betas[3]) * (1.0 if lexical_dense_agree else 0.0)
    z += float(betas[4]) * max(0.0, min(1.0, float(coordinate_confidence)))
    # Stage 7 — β5 * clamp01(freshness_min). ``None`` falls through as
    # 0 contribution rather than 0.0-clamped — operationally these
    # are similar in this defaults regime, but the None branch
    # preserves the spec semantics of "freshness unknown" being
    # neutral rather than maximally-stale.
    if freshness_min is not None and len(betas) >= 6:
        z += float(betas[5]) * max(0.0, min(1.0, float(freshness_min)))

    return _sigmoid(z)


# ─────────────────────────────────────────────────────────────────────
# helix.toml [know] table loader (pure function; soft-fail to defaults)
# ─────────────────────────────────────────────────────────────────────

def load_calibration_from_toml(
    toml_path: Optional[str | Path] = None,
) -> KnowCalibration:
    """Read [know] from helix.toml; fall back to defaults on any failure.

    The table layout (§11):

        [know]
        emit_floor      = 0.55
        s_ref           = 1.0
        g_ref           = 0.5
        betas           = [-2.0, 2.0, 1.5, 0.7, 1.8]
        calibrated_at   = "2026-05-08T..."
        calibrated_on_n = 800

    A missing file, missing [know] table, or malformed entries all
    return defaults with a single ``log.warning``. This keeps the
    calibration loader from ever blowing up retrieval.
    """
    try:
        # Lazy-import tomllib (3.11+); for older Pythons callers can
        # pip install `tomli` and we'll fall back transparently.
        try:
            import tomllib  # type: ignore[import-not-found]
        except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
            import tomli as tomllib  # type: ignore[no-redef]

        if toml_path is None:
            # Default search: helix.toml at the repo root (cwd).
            toml_path = Path("helix.toml")
        else:
            toml_path = Path(toml_path)

        if not toml_path.exists():
            return KnowCalibration()

        with toml_path.open("rb") as fh:
            data = tomllib.load(fh)

        table = data.get("know")
        if not isinstance(table, dict):
            return KnowCalibration()

        betas_raw = table.get("betas", DEFAULT_BETAS)
        try:
            betas = tuple(float(b) for b in betas_raw)
        except (TypeError, ValueError):
            log.warning(
                "know_calibration: malformed betas in %s; using defaults",
                toml_path,
            )
            betas = DEFAULT_BETAS

        if len(betas) != 1 + N_FEATURES:
            log.warning(
                "know_calibration: betas length %d != expected %d in %s; "
                "using defaults",
                len(betas),
                1 + N_FEATURES,
                toml_path,
            )
            betas = DEFAULT_BETAS

        return KnowCalibration(
            betas=betas,
            s_ref=float(table.get("s_ref", DEFAULT_S_REF)),
            g_ref=float(table.get("g_ref", DEFAULT_G_REF)),
            emit_floor=float(table.get("emit_floor", DEFAULT_EMIT_FLOOR)),
            calibrated_at=(
                str(table["calibrated_at"])
                if table.get("calibrated_at") is not None
                else None
            ),
            calibrated_on_n=(
                int(table["calibrated_on_n"])
                if table.get("calibrated_on_n") is not None
                else None
            ),
        )
    except Exception:
        log.warning(
            "know_calibration: failed to load %s; using defaults",
            toml_path,
            exc_info=True,
        )
        return KnowCalibration()


# Convenience for downstream callers and tests.
def fit_betas_from_features(
    features: Sequence[Sequence[float]],
    labels: Sequence[int],
    *,
    n_features: int = N_FEATURES,
    lr: float = 0.1,
    epochs: int = 500,
    l2: float = 1e-4,
) -> tuple[float, ...]:
    """Hand-rolled gradient descent for the calibration script.

    ``features[i]`` is the per-row feature vector after the same tanh
    squashing applied at inference time:

        [tanh(top_score/s_ref), tanh(score_gap/g_ref),
         lexical_dense_agree (0/1), coordinate_confidence]

    ``labels[i]`` is 1 for ground-truth retrieval-success (a known-good
    KnowBlock target) and 0 for retrieval-miss.

    Returns ``(b0, b1, ..., b{n_features})``. Tiny implementation so
    sklearn is not a hard dep; the calibration script may swap in
    sklearn's LogisticRegression when available (gives a ~1% AUC bump
    in practice and a much faster convergence on n=800).
    """
    if len(features) != len(labels):
        raise ValueError("features and labels length mismatch")
    if any(len(row) != n_features for row in features):
        raise ValueError(f"features must have length {n_features} per row")

    weights = [0.0] * (1 + n_features)
    for _ in range(epochs):
        grad = [0.0] * (1 + n_features)
        for x, y in zip(features, labels):
            # logit
            z = weights[0] + sum(weights[i + 1] * x[i] for i in range(n_features))
            p = _sigmoid(z)
            err = p - float(y)
            grad[0] += err
            for i in range(n_features):
                grad[i + 1] += err * x[i]
        n = float(len(features)) or 1.0
        for i in range(1 + n_features):
            # L2 on non-intercept terms only
            reg = (l2 * weights[i]) if i > 0 else 0.0
            weights[i] -= lr * (grad[i] / n + reg)
    return tuple(weights)

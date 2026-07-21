"""Tests for the ABSTAIN tier — confidence-gated context attachment.

See docs/specs/2026-05-02-abstain-tier-design.md and
docs/plans/2026-05-02-abstain-tier.md.
"""

import pytest

from cymatix_context import context_manager as cm
from cymatix_context.config import (
    BudgetConfig,
    ClassifierConfig,
    GenomeConfig,
    HelixConfig,
    RetrievalConfig,
    RibosomeConfig,
)
from cymatix_context.context_manager import HelixContextManager
from tests.conftest import MockCompressorBackend


def test_abstain_marker_constant_is_exported():
    """The shared marker string is exposed at module scope so the empty-
    candidates branch and the abstain branch can ship identical bytes.

    Stage 6 (2026-05-08, §6): the prose marker is replaced with the
    structured `<helix:no_match reason="abstain" do_not_answer="true"/>`
    tag. ``_ABSTAIN_MARKER`` is preserved as a deprecated alias that
    points at the new tag for one release, so call sites migrate
    transparently.
    """
    assert cm._ABSTAIN_MARKER == cm._no_match_token("abstain")
    assert cm._ABSTAIN_MARKER == (
        '<helix:no_match reason="abstain" do_not_answer="true"/>'
    )


@pytest.mark.parametrize("value,expected", [
    ("1", True),
    ("true", True),
    ("TRUE", True),
    ("yes", True),
    ("on", True),
    ("0", False),
    ("false", False),
    ("no", False),
    ("", False),
    ("garbage", False),
])
def test_env_truthy_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("HELIX_TEST_ENV_TRUTHY", value)
    assert cm._env_truthy("HELIX_TEST_ENV_TRUTHY") is expected


def test_env_truthy_unset_is_false(monkeypatch):
    monkeypatch.delenv("HELIX_TEST_ENV_TRUTHY", raising=False)
    assert cm._env_truthy("HELIX_TEST_ENV_TRUTHY") is False


@pytest.fixture
def abstain_manager():
    """Manager with mock backend + in-memory genome + abstain on.

    Pinned to fusion_mode="additive": the tests built on this fixture
    assert the additive-scale gate contract (absolute 2.5 floor, legacy
    top/mean ratio with the 1.8 threshold, strict-< boundaries). Since
    the 2026-07-06 rrf default flip the shipped gate is ratio-only with
    the #115 baseline-normalized ratio — that path has its own explicit
    fusion_mode="rrf" coverage further down this file.
    """
    cfg = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=12, abstain_enabled=True),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        classifier=ClassifierConfig(enabled=False),
        retrieval=RetrievalConfig(fusion_mode="additive"),
    )
    mgr = HelixContextManager(cfg)
    mgr.ribosome.backend = MockCompressorBackend()
    yield mgr
    mgr.close()


def test_build_abstain_window_shape(abstain_manager):
    """The helper returns a ContextWindow with the spec-§4 shape."""
    win = abstain_manager._build_abstain_window(
        query="anything",
        effective_decoder_prompt="DECODER",
        top_score=1.5,
        ratio=1.2,
        reason="score_below_floor",
    )
    assert win.expressed_context == cm._ABSTAIN_MARKER
    assert win.ribosome_prompt == "DECODER"
    assert win.total_estimated_tokens == cm.estimate_tokens("DECODER")
    assert win.context_health.status == "abstain"
    assert win.context_health.genes_expressed == 0
    assert win.metadata["query"] == "anything"
    assert win.metadata["genes_expressed"] == 0
    assert win.metadata["budget_tier"] == "abstain"
    assert win.metadata["abstain_reason"] == "score_below_floor"
    assert win.metadata["top_score"] == 1.5
    assert win.metadata["ratio"] == 1.2
    assert win.compression_ratio == 1.0


def _stub_express(manager, *, candidates, scores):
    """Replace _express with a canned-result version for deterministic tests.

    Real _express runs the genome lookup + co-activation expansion + tier
    accumulation. For ABSTAIN tier tests we need precise top_score/ratio
    control, so we bypass the retrieval pipeline and stuff
    last_query_scores directly. We also stub _apply_candidate_refiners
    to a no-op pass-through so cymatics / harmonic-bin / TCM don't
    perturb the injected scores.
    """
    def fake_express(domains, entities, max_genes, **_kwargs):
        # Real _express has positional-or-keyword args (query_text,
        # include_cold, party_id, use_harmonic, use_sr, read_only). The
        # caller in build_context passes 4 of those by keyword;
        # **_kwargs absorbs whichever the production code happens to pass
        # so this stub stays robust if the real signature evolves.
        manager.genome.last_query_scores = dict(scores)
        return list(candidates)
    # Patch canonical and legacy names both (R3 Stage C);
    # internal callers use `_retrieve`, but `_express` is still a valid alias.
    manager._retrieve = fake_express
    manager._express = fake_express

    def fake_refiners(query, candidates, max_genes, **_kwargs):
        return list(candidates), {}
    manager._apply_candidate_refiners = fake_refiners


def _weak_setup(abstain_manager, *, top_score=1.5, ratio=1.2, n=8):
    """Seed n candidates whose scores yield (top_score, ratio).

    Solves: top = top_score, mean = top_score / ratio. We distribute
    n-1 candidates at score = (n*mean - top) / (n - 1) so the mean
    lands exactly. Returns (candidates, scores).
    """
    from tests.conftest import make_gene
    candidates = [
        make_gene(f"weak_{i}", gene_id=f"weak_gene_{i:010d}")
        for i in range(n)
    ]
    mean = top_score / ratio
    rest = (n * mean - top_score) / (n - 1)
    scores = {candidates[0].gene_id: top_score}
    for c in candidates[1:]:
        scores[c.gene_id] = rest
    return candidates, scores


def test_weak_retrieval_triggers_abstain(abstain_manager):
    """top_score < 2.5 AND ratio < 1.8 → ABSTAIN."""
    candidates, scores = _weak_setup(abstain_manager, top_score=1.5, ratio=1.2)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)

    win = abstain_manager.build_context("anything")

    assert win.metadata["budget_tier"] == "abstain"
    assert win.context_health.status == "abstain"
    assert win.expressed_context == cm._ABSTAIN_MARKER
    assert win.metadata["abstain_reason"] == "score_below_floor"
    # top_score and ratio in metadata reflect what the gate observed
    assert win.metadata["top_score"] == pytest.approx(1.5, abs=1e-6)
    assert win.metadata["ratio"] == pytest.approx(1.2, abs=1e-3)
    assert win.context_health.genes_expressed == 0


def test_focused_score_floor_constants_in_sync():
    """The ABSTAIN gate threshold mirrors the FOCUSED tier threshold.
    If one is bumped without the other, the gate's strict-less-than
    semantic and the FOCUSED tier's at-or-above threshold drift apart.

    Stage 4 (2026-05-08) moved the literal floors out of build_context
    into _floors_for / AbstainClassFloors. Both modes need pinning:
    - global mode reads _GLOBAL_FOCUSED_FLOOR / _GLOBAL_ABSTAIN_FLOOR
      from HelixContextManager;
    - per_classifier mode falls back to AbstainClassFloors defaults
      when a class lacks an explicit block.
    """
    from cymatix_context.config import AbstainClassFloors

    assert (
        cm.HelixContextManager._GLOBAL_FOCUSED_FLOOR
        == cm.HelixContextManager._GLOBAL_ABSTAIN_FLOOR
    ), "global-mode floors drifted apart"
    defaults = AbstainClassFloors()
    assert defaults.focused_top == defaults.abstain_top, (
        "AbstainClassFloors default focused_top / abstain_top drifted apart"
    )


def test_strong_signal_lands_in_tight(abstain_manager):
    """top_score=8.0, ratio=4.0 → TIGHT, ABSTAIN does not fire."""
    candidates, scores = _weak_setup(abstain_manager, top_score=8.0, ratio=4.0)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    win = abstain_manager.build_context("anything")
    assert win.metadata["budget_tier"] == "tight"
    assert win.context_health.status != "abstain"


def test_focused_eligible_lands_in_focused(abstain_manager):
    """top_score=3.5, ratio=2.0 → FOCUSED, ABSTAIN does not fire."""
    candidates, scores = _weak_setup(abstain_manager, top_score=3.5, ratio=2.0)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    win = abstain_manager.build_context("anything")
    assert win.metadata["budget_tier"] == "focused"
    assert win.context_health.status != "abstain"


def test_boundary_at_score_floor_does_not_abstain(abstain_manager):
    """top_score == 2.5 (the FOCUSED floor) does NOT trigger ABSTAIN.

    Isolates the score-axis strict-< by holding ratio = 2.5 — well above
    the abstain ratio floor (1.8) so the ratio axis cannot be the reason
    the gate fails. The only condition that can keep us out of ABSTAIN
    is the score-axis check `top_score < 2.5` failing on the boundary.
    Confirms the gate uses strict `<` (not `<=`) on score.
    """
    candidates, scores = _weak_setup(abstain_manager, top_score=2.5, ratio=2.5)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    win = abstain_manager.build_context("anything")
    assert win.metadata["budget_tier"] != "abstain"


def test_boundary_at_ratio_floor_does_not_abstain(abstain_manager):
    """ratio == 1.8 (the abstain ratio floor) does NOT trigger ABSTAIN.

    Isolates the ratio-axis strict-< by holding top_score = 1.5 — well
    below the FOCUSED floor so the score axis WOULD trigger ABSTAIN if
    it were the only check. The ratio axis must catch this and force
    fall-through to BROAD. Confirms the gate uses strict `<` (not `<=`)
    on ratio.
    """
    candidates, scores = _weak_setup(abstain_manager, top_score=1.5, ratio=1.8)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    win = abstain_manager.build_context("anything")
    assert win.metadata["budget_tier"] == "broad"


def test_abstain_disabled_via_config_falls_through_to_broad(abstain_manager):
    """abstain_enabled=False on weak retrieval → BROAD (legacy behavior)."""
    abstain_manager.config.budget.abstain_enabled = False
    candidates, scores = _weak_setup(abstain_manager, top_score=1.5, ratio=1.2)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    win = abstain_manager.build_context("anything")
    assert win.metadata["budget_tier"] == "broad"
    assert win.context_health.status != "abstain"


def test_abstain_env_override_beats_config_flag(abstain_manager, monkeypatch):
    """HELIX_ABSTAIN_DISABLE=1 forces off even when config flag is on."""
    monkeypatch.setenv("HELIX_ABSTAIN_DISABLE", "1")
    assert abstain_manager.config.budget.abstain_enabled is True   # config still on
    candidates, scores = _weak_setup(abstain_manager, top_score=1.5, ratio=1.2)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    win = abstain_manager.build_context("anything")
    assert win.metadata["budget_tier"] == "broad"
    assert win.context_health.status != "abstain"


# ── Issue #115: RRF fusion abstain calibration ──────────────────────────────
#
# PR #106 switched ShardRouter.query_genes to RRF, compressing sharded
# top-scores to ~0.26-0.40. PR #116 short-circuited the BM25-calibrated
# absolute score floor (2.5) under RRF, but the **ratio** gate
# (legacy ``top/mean`` < 1.8) is also BM25-calibrated and still tripped
# sharded queries. Measured 2026-05-14 on medium-sharded fixture:
#
#   query              legacy ratio   result
#   helix_port              1.77      abstain (just below 1.8)
#   biged_skills            1.57      abstain
#   scorerift               1.46      abstain
#
# The fix replaces the legacy ratio with a baseline-subtracted ratio under
# RRF:
#
#     norm_ratio = (top - min) / (mean - min)
#
# which is scale-invariant. Threshold 1.5 (RRF norm) clears all measured
# RRF queries while still rejecting genuinely-tied distributions; additive
# (BM25) keeps the legacy ratio + 1.8 threshold for byte-identical blob
# behaviour. Helpers below build score distributions in the SHAPES we
# actually observe in production rather than the degenerate
# "top + N identical tail" synthetic, so the tests pin the right
# invariant.


def _gradient_scores(top_score: float, low_score: float, n: int = 12):
    """Build n candidates with a linear gradient from top_score to low_score.

    Models the realistic post-refiner curve we observe on production
    queries: a strong top, a smooth descent, and a non-zero floor. The
    legacy ratio (top/mean) lands near 1.0-2.0, baseline-subtracted ratio
    lands near 2.0 — both representative of the actual sharded RRF probe.
    """
    from tests.conftest import make_gene
    candidates = [
        make_gene(f"rrf_{i}", gene_id=f"rrf_gene_{i:010d}")
        for i in range(n)
    ]
    if n == 1:
        return candidates, {candidates[0].gene_id: top_score}
    step = (top_score - low_score) / (n - 1)
    scores = {
        candidates[i].gene_id: top_score - i * step
        for i in range(n)
    }
    return candidates, scores


def _flat_with_top_scores(top_score: float, tail_score: float, n: int = 8):
    """Build candidates with the LEGACY-test synthetic shape: 1 top + N-1 tail.

    Useful for additive-path regression tests where this distribution is
    what the legacy ratio gate was tuned against. Under RRF normalization
    this shape collapses to denom=0 → ratio_for_gate=0 → abstain (the
    correct outcome since the candidate set has no informative spread
    above the floor anyway).
    """
    from tests.conftest import make_gene
    candidates = [
        make_gene(f"flat_{i}", gene_id=f"flat_gene_{i:010d}")
        for i in range(n)
    ]
    scores = {candidates[0].gene_id: top_score}
    for c in candidates[1:]:
        scores[c.gene_id] = tail_score
    return candidates, scores


# Back-compat alias for existing call-sites. The old helper produced a
# degenerate "top + N flat" shape; tests that genuinely care about score
# *distributions* should use ``_gradient_scores`` instead.
def _candidates_with_scores(top_score: float, ratio: float, n: int = 8):
    """Build n candidates whose scores yield (top_score, ratio) under legacy
    top/mean. Kept for tests that exercise the additive abstain gate's
    historical synthetic-tail behaviour; the new RRF tests below use
    ``_gradient_scores`` for realistic shapes.
    """
    from tests.conftest import make_gene
    candidates = [
        make_gene(f"rrf_{i}", gene_id=f"rrf_gene_{i:010d}")
        for i in range(n)
    ]
    mean = top_score / ratio
    rest = (n * mean - top_score) / (n - 1)
    scores = {candidates[0].gene_id: top_score}
    for c in candidates[1:]:
        scores[c.gene_id] = rest
    return candidates, scores


def test_rrf_realistic_gradient_does_not_abstain():
    """Issue #115: realistic post-refiner RRF curve (top=0.40, gradient
    down to floor=0.05) must NOT abstain. This is the SHAPE we observe on
    biged_skills / helix_port / scorerift on medium-sharded.
    """
    from cymatix_context.config import AbstainClassFloors
    from cymatix_context.pipeline.tier_logic import apply_budget_tiers
    candidates, scores = _gradient_scores(top_score=0.40, low_score=0.05, n=12)
    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(),
        abstain_enabled=True, fusion_mode="rrf",
    )
    assert result.abstain is False, (
        f"RRF gradient top=0.40 low=0.05 should NOT abstain — represents "
        f"a real biged_skills / helix_port style curve; tier={result.budget_tier}"
    )


def test_rrf_tight_top_clear_separation_does_not_abstain():
    """Issue #115: even when top is only marginally above #2 (top=0.40,
    second=0.395) under RRF, if there's a SPREAD across the candidate
    set (tail down to 0.02) the baseline-normalized ratio recognizes
    real signal. This is the biged_skills shape (top - second = 0.003
    but spread = 0.38).
    """
    from cymatix_context.config import AbstainClassFloors
    from cymatix_context.pipeline.tier_logic import apply_budget_tiers
    from tests.conftest import make_gene
    candidates = [
        make_gene(f"rrf_{i}", gene_id=f"rrf_gene_{i:010d}")
        for i in range(12)
    ]
    # Empirical biged_skills shape (rounded): two near-tied tops, smooth
    # descent, two-step floor.
    vals = [0.40, 0.395, 0.34, 0.34, 0.32, 0.30, 0.25, 0.23, 0.22, 0.19, 0.05, 0.02]
    scores = {candidates[i].gene_id: vals[i] for i in range(12)}
    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(),
        abstain_enabled=True, fusion_mode="rrf",
    )
    assert result.abstain is False, (
        "RRF biged_skills-shape (near-tied tops + 18x spread) must NOT "
        "abstain — there is real spread above the noise floor"
    )


def test_rrf_genuinely_tied_distribution_does_abstain():
    """Issue #115: under RRF, a near-flat distribution (top barely above
    tail, no spread) DOES abstain. The baseline-normalized ratio collapses
    to ~1.0-1.2 on these — below the 1.5 RRF threshold.
    """
    from cymatix_context.config import AbstainClassFloors
    from cymatix_context.pipeline.tier_logic import apply_budget_tiers
    from tests.conftest import make_gene
    candidates = [
        make_gene(f"rrf_{i}", gene_id=f"rrf_gene_{i:010d}")
        for i in range(8)
    ]
    # 7 tied at top + 1 at tail — RRF rank-1 collision shape with no
    # meaningful winner. Normalized ratio = (0.0164 - 0.0163) / (mean - min)
    # = ~1.14, below the 1.5 RRF gate threshold.
    vals = [0.0164] * 7 + [0.0163]
    scores = {candidates[i].gene_id: vals[i] for i in range(8)}
    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(),
        abstain_enabled=True, fusion_mode="rrf",
    )
    assert result.abstain is True, (
        "RRF mostly-tied distribution (no winner) must abstain"
    )


def test_rrf_all_tied_does_abstain():
    """Issue #115: degenerate all-tied case (denom=0) must abstain.
    Guards against div-by-zero and asserts the "zero information"
    interpretation of a uniform candidate set.
    """
    from cymatix_context.config import AbstainClassFloors
    from cymatix_context.pipeline.tier_logic import apply_budget_tiers
    from tests.conftest import make_gene
    candidates = [
        make_gene(f"rrf_{i}", gene_id=f"rrf_gene_{i:010d}")
        for i in range(8)
    ]
    scores = {c.gene_id: 0.0164 for c in candidates}
    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(),
        abstain_enabled=True, fusion_mode="rrf",
    )
    assert result.abstain is True, (
        "RRF all-tied (degenerate, denom=0) must abstain"
    )


def test_additive_low_score_still_abstains():
    """Issue #115 regression guard: additive mode keeps legacy
    top/mean<1.8 + top<2.5 ABSTAIN behaviour byte-for-byte. RRF
    normalization must not leak into the additive path.
    """
    from cymatix_context.config import AbstainClassFloors
    from cymatix_context.pipeline.tier_logic import apply_budget_tiers
    candidates, scores = _candidates_with_scores(top_score=0.4, ratio=1.2)
    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(),
        abstain_enabled=True, fusion_mode="additive",
    )
    assert result.abstain is True, (
        "additive top=0.4 ratio=1.2 must still abstain — RRF normalization "
        "must not affect additive scoring"
    )


def test_additive_strong_signal_still_passes():
    """Issue #115 regression guard: additive mode with realistic gradient
    + ratio>1.8 continues to NOT abstain. Pins blob-mode behaviour: the
    legacy top/mean ratio + 1.8 threshold gates additive exactly as
    pre-#115.
    """
    from cymatix_context.config import AbstainClassFloors
    from cymatix_context.pipeline.tier_logic import apply_budget_tiers
    # top/mean=3.5/1.5 = 2.33 (well above 1.8) with realistic gradient.
    candidates, scores = _gradient_scores(top_score=3.5, low_score=0.5, n=12)
    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(),
        abstain_enabled=True, fusion_mode="additive",
    )
    assert result.abstain is False, (
        "additive top=3.5 ratio≈1.92 must NOT abstain — blob mode behaviour"
    )


def test_additive_above_abstain_floor_low_ratio_does_not_abstain():
    """Companion: under additive, top=3.0 (>= floor 2.5) short-circuits
    the abstain check regardless of ratio. Pre-#115 legacy behaviour that
    must NOT regress.
    """
    from cymatix_context.config import AbstainClassFloors
    from cymatix_context.pipeline.tier_logic import apply_budget_tiers
    candidates, scores = _candidates_with_scores(top_score=3.0, ratio=1.5)
    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(),
        abstain_enabled=True, fusion_mode="additive",
    )
    assert result.abstain is False, (
        "additive top=3.0 ratio=1.5 must NOT abstain — score floor "
        "short-circuits the gate; legacy behavior must be preserved"
    )


def test_rrf_metadata_ratio_reflects_normalized_value():
    """Under RRF the gate compares the baseline-normalized ratio; the
    abstain telemetry surfaces what was actually checked so operators
    can diagnose without re-deriving the metric. Pin this so callers
    know which number lands in ``metadata["ratio"]``.
    """
    from cymatix_context.config import AbstainClassFloors
    from cymatix_context.pipeline.tier_logic import apply_budget_tiers
    from tests.conftest import make_gene
    candidates = [
        make_gene(f"rrf_{i}", gene_id=f"rrf_gene_{i:010d}")
        for i in range(8)
    ]
    # All tied → normalized ratio collapses to 0.0 (denom=0 short-circuit)
    scores = {c.gene_id: 0.0164 for c in candidates}
    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(),
        abstain_enabled=True, fusion_mode="rrf",
    )
    assert result.abstain is True
    # Normalized ratio for an all-tied set is 0.0 (denom=0 path); legacy
    # ratio for the same set is ~1.0. Confirms the gate's value is what
    # gets surfaced.
    assert result.abstain_ratio == pytest.approx(0.0, abs=1e-6), (
        f"abstain_ratio should reflect normalized gate value, got "
        f"{result.abstain_ratio}"
    )


def test_telemetry_counter_increments_with_abstain_label(
    abstain_manager, monkeypatch
):
    """Verify budget_tier_counter is called with attributes={'tier': 'abstain'}."""
    calls: list[dict] = []

    class _Recorder:
        def add(self, value, attributes=None):
            calls.append({"value": value, "attributes": dict(attributes or {})})

    monkeypatch.setattr(
        "cymatix_context.telemetry.budget_tier_counter",
        lambda: _Recorder(),
    )
    candidates, scores = _weak_setup(abstain_manager, top_score=1.5, ratio=1.2)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    abstain_manager.build_context("anything")

    # Strong invariant: on a single weak-retrieval build_context, the gate
    # short-circuits BEFORE the existing tier-counter call site, so we
    # expect EXACTLY ONE counter call total — not just one abstain call
    # plus possibly some other tier label. A regression that moves the
    # early-return below the existing emission would double-count and
    # would be caught by the strict equality on `len(calls) == 1`.
    assert len(calls) == 1
    assert calls[0]["attributes"] == {"tier": "abstain"}
    assert calls[0]["value"] == 1

"""Tests for the ABSTAIN tier — confidence-gated context attachment.

See docs/specs/2026-05-02-abstain-tier-design.md and
docs/plans/2026-05-02-abstain-tier.md.
"""

import pytest

from helix_context import context_manager as cm
from helix_context.config import (
    BudgetConfig,
    ClassifierConfig,
    GenomeConfig,
    HelixConfig,
    RibosomeConfig,
)
from helix_context.context_manager import HelixContextManager
from tests.test_pipeline import PipelineMockBackend


def test_abstain_marker_constant_is_exported():
    """The shared marker string is exposed at module scope so the empty-
    candidates branch and the abstain branch can ship identical bytes."""
    assert cm._ABSTAIN_MARKER == "(no relevant context found in genome)"


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
    """Manager with mock backend + in-memory genome + abstain on."""
    cfg = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=12, abstain_enabled=True),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        classifier=ClassifierConfig(enabled=False),
    )
    mgr = HelixContextManager(cfg)
    mgr.ribosome.backend = PipelineMockBackend()
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
        # caller in _build_context_internal passes 4 of those by keyword;
        # **_kwargs absorbs whichever the production code happens to pass
        # so this stub stays robust if the real signature evolves.
        manager.genome.last_query_scores = dict(scores)
        return list(candidates)
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
    """The ABSTAIN gate mirrors the FOCUSED_SCORE_FLOOR = 2.5 constant
    defined just below it in context_manager.py. If one is bumped
    without the other, the gate's strict-less-than semantic and the
    FOCUSED tier's threshold will drift. This test pins them together
    by extracting both literals and asserting numeric equality — robust
    to formatting changes (extra whitespace, comments, scientific
    notation) that string-matching would miss.
    """
    import inspect
    import re

    src = inspect.getsource(cm.HelixContextManager.build_context)
    matches = re.findall(
        r"FOCUSED_SCORE_FLOOR(?:_FOR_ABSTAIN)?\s*=\s*([\d.]+(?:[eE][-+]?\d+)?)",
        src,
    )
    assert len(matches) == 2, (
        f"expected exactly 2 FOCUSED_SCORE_FLOOR literals, got {matches}"
    )
    assert float(matches[0]) == float(matches[1]), (
        f"FOCUSED_SCORE_FLOOR_FOR_ABSTAIN ({matches[0]}) and "
        f"FOCUSED_SCORE_FLOOR ({matches[1]}) drifted apart"
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

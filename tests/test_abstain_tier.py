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
    assert win.context_health.status == "abstain"
    assert win.context_health.genes_expressed == 0
    assert win.metadata["genes_expressed"] == 0
    assert win.metadata["budget_tier"] == "abstain"
    assert win.metadata["abstain_reason"] == "score_below_floor"
    assert win.metadata["top_score"] == 1.5
    assert win.metadata["ratio"] == 1.2
    assert win.compression_ratio == 1.0

"""Tests for foveated-splice — rank-scaled BROAD compression schedule.

Spec: docs/specs/2026-05-03-foveated-splice-design.md
Plan: docs/plans/2026-05-05-foveated-splice.md
"""
import pytest

from helix_context.context_manager import _compute_foveated_caps


@pytest.fixture
def helix_manager_with_three_genes(tmp_path):
    """Minimal HelixContextManager with three genes of distinct scores.

    Avoids the tests.test_pipeline import (broken at collection time
    due to module-level helix_context.server.create_app() failure).
    Constructs the manager directly with model='mock' (no real backend
    load) and an in-memory genome, then seeds last_query_scores so the
    slate-path sort has something to sort by.
    """
    from helix_context.config import (
        BudgetConfig,
        ClassifierConfig,
        GenomeConfig,
        HelixConfig,
        RibosomeConfig,
    )
    from helix_context.context_manager import HelixContextManager
    from helix_context.schemas import Gene, PromoterTags

    cfg = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=12),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        classifier=ClassifierConfig(enabled=False),
    )
    mgr = HelixContextManager(cfg)
    g_low = Gene(
        gene_id="g_low",
        content="low content",
        complement="low",
        codons=["c"],
        promoter=PromoterTags(sequence_index=0),
    )
    g_mid = Gene(
        gene_id="g_mid",
        content="mid content",
        complement="mid",
        codons=["c"],
        promoter=PromoterTags(sequence_index=1),
    )
    g_high = Gene(
        gene_id="g_high",
        content="high content",
        complement="high",
        codons=["c"],
        promoter=PromoterTags(sequence_index=2),
    )
    # Seed scores so the default slate-path sort has something to sort by.
    # This is what _assemble reads at line ~1745 via self.genome.last_query_scores.
    mgr.genome.last_query_scores = {
        "g_low": 1.0,
        "g_mid": 5.0,
        "g_high": 9.0,
    }
    yield mgr, g_low, g_mid, g_high
    mgr.close()


class TestComputeFoveatedCaps:
    """Pure-function tests for the schedule-shape helper (spec §4.1)."""

    def test_alpha_one_n_twelve_matches_spec_table(self):
        """Spec §10 Test 6: caps[0]=1.0, caps[1]=0.5, caps[5]≈1/6, caps[11]=c_min."""
        caps = _compute_foveated_caps(n=12, alpha=1.0, c_min=0.15, c_max=1.0)
        assert len(caps) == 12
        assert caps[0] == 1.0
        assert caps[1] == 0.5
        assert caps[5] == pytest.approx(1 / 6, abs=1e-9)
        # rank-12 → 1/12 ≈ 0.083 < c_min=0.15, so floor wins
        assert caps[11] == 0.15

    def test_alpha_two_collapses_to_floor_by_rank_three(self):
        """Spec §10 Test 8: α=2 floors caps[5..11] at c_min."""
        caps = _compute_foveated_caps(n=12, alpha=2.0, c_min=0.15, c_max=1.0)
        # 1/3^2 = 0.111 < 0.15 → already floored at rank 3
        for i in range(2, 12):
            assert caps[i] == 0.15, f"caps[{i}]={caps[i]} expected 0.15"

    def test_alpha_half_gentle_decay(self):
        """Spec §4.2 table: α=0.5 caps[1] ≈ 0.71."""
        caps = _compute_foveated_caps(n=12, alpha=0.5, c_min=0.15, c_max=1.0)
        assert caps[0] == 1.0
        assert caps[1] == pytest.approx(1 / (2 ** 0.5), abs=1e-9)  # ≈ 0.7071
        assert caps[2] == pytest.approx(1 / (3 ** 0.5), abs=1e-9)  # ≈ 0.5774

    def test_c_min_floor_is_inclusive_lower_bound(self):
        """A custom c_min raises the floor for low-rank genes."""
        caps = _compute_foveated_caps(n=12, alpha=1.0, c_min=0.30, c_max=1.0)
        assert caps[0] == 1.0
        # rank-4 → 0.25 < c_min=0.30, floors at 0.30
        for i in range(3, 12):
            assert caps[i] == 0.30

    def test_n_one_returns_single_c_max(self):
        """Edge case: a single-candidate BROAD set returns [c_max]."""
        assert _compute_foveated_caps(n=1, alpha=1.0, c_min=0.15) == [1.0]

    def test_n_zero_returns_empty(self):
        """Edge case: empty input → empty output, never raises."""
        assert _compute_foveated_caps(n=0, alpha=1.0, c_min=0.15) == []


class TestAssembleRespectsCallerOrder:
    """When respect_caller_order=True, _assemble preserves the candidates list
    order verbatim — no score-desc or sequence-index re-sort. This is what
    makes reverse-rank placement actually reach the prompt (spec §5)."""

    def test_assemble_default_resorts_by_score_for_slate(self, helix_manager_with_three_genes):
        """Sanity: default (use_slate=True) re-sorts by score DESC."""
        m, g_low, g_mid, g_high = helix_manager_with_three_genes
        # Pass in low→high; default behavior should re-emit high→low
        window = m._assemble(
            query="q",
            candidates=[g_low, g_mid, g_high],
            spliced_map={
                g_low.gene_id: "<G>L</G>",
                g_mid.gene_id: "<G>M</G>",
                g_high.gene_id: "<G>H</G>",
            },
            relation_graph={},
            answer_slate=["k=v"],  # forces use_slate=True
        )
        # Top-score gene (g_high) should appear first in the rendered text.
        assert window.expressed_context.find("H") < window.expressed_context.find("L")

    def test_assemble_respects_caller_order_when_flagged(self, helix_manager_with_three_genes):
        """With respect_caller_order=True, the input order is preserved."""
        m, g_low, g_mid, g_high = helix_manager_with_three_genes
        # Pass in reverse-rank order (low→high → bottom-rank first, top-rank last)
        window = m._assemble(
            query="q",
            candidates=[g_low, g_mid, g_high],
            spliced_map={
                g_low.gene_id: "<G>L</G>",
                g_mid.gene_id: "<G>M</G>",
                g_high.gene_id: "<G>H</G>",
            },
            relation_graph={},
            answer_slate=["k=v"],
            respect_caller_order=True,
        )
        # L came first in input → stays first in text. H last in input → last.
        assert window.expressed_context.find("L") < window.expressed_context.find("M") < window.expressed_context.find("H")

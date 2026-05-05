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


# ---------------------------------------------------------------------------
# Fixtures + helpers for tier-gating + reverse-rank end-to-end tests.
#
# We replicate _stub_express inline (rather than importing from
# tests.test_abstain_tier) because that module transitively imports
# tests.test_pipeline, which fails at collection time due to a
# module-level helix_context.server.create_app() call without a real
# genome DB. Inline replication keeps this file collectable in
# isolation. See plan §"Step 1: Write the failing tests" notes.
# ---------------------------------------------------------------------------


def _stub_express(manager, *, candidates, scores):
    """Replicated from tests/test_abstain_tier.py:87 to avoid the broken
    test_pipeline import path. Bypasses real retrieval so tier-resolution
    tests can pin top_score/ratio precisely."""
    def fake_express(domains, entities, max_genes, **_kwargs):
        manager.genome.last_query_scores = dict(scores)
        return list(candidates)
    manager._express = fake_express

    def fake_refiners(query, candidates, max_genes, **_kwargs):
        return list(candidates), {}
    manager._apply_candidate_refiners = fake_refiners


def _make_manager_with_n_genes(n, scores_fn):
    """Build a HelixContextManager and stub _express to return n genes
    with scores supplied by scores_fn(i) for i in [0, n).

    scores_fn maps position to score. Caller picks scores to land the
    tier resolution at the desired bucket.
    """
    from helix_context.config import (
        BudgetConfig, ClassifierConfig, GenomeConfig, HelixConfig,
        RibosomeConfig,
    )
    from helix_context.context_manager import HelixContextManager
    from tests.conftest import make_gene

    cfg = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=n + 2, abstain_enabled=True),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        classifier=ClassifierConfig(enabled=False),
    )
    mgr = HelixContextManager(cfg)
    candidates = [
        make_gene(f"gene_{i} content", gene_id=f"gene_{i:02d}")
        for i in range(n)
    ]
    scores = {candidates[i].gene_id: scores_fn(i) for i in range(n)}
    _stub_express(mgr, candidates=candidates, scores=scores)
    return mgr


@pytest.fixture
def helix_manager_broad_with_twelve_genes():
    """12 genes whose scores land the dynamic budget in BROAD.

    Scores: 3.0, 2.9, 2.8, ..., 1.9 → top=3.0 (>=2.5 escapes ABSTAIN,
    <5.0 fails TIGHT). mean ≈ 2.45, ratio ≈ 1.22 (<1.8 fails FOCUSED) → BROAD.
    """
    mgr = _make_manager_with_n_genes(
        12,
        lambda i: 3.0 - i * 0.1,
    )
    yield mgr
    mgr.close()


@pytest.fixture
def helix_manager_tight():
    """4 genes — top dominates so the dynamic budget tiers as TIGHT.

    top=20.0, others=0.1 → mean≈5.075, ratio≈3.94. PASSES TIGHT
    (ratio>=3.0, top>=5.0, len>=3). The score-floor gates 3 of the 4
    out (floor = 20*0.15 = 3.0), but len(gated)<3 so candidates stays
    at 4, then TIGHT trims to candidates[:3].
    """
    mgr = _make_manager_with_n_genes(
        4,
        lambda i: 20.0 if i == 0 else 0.1,
    )
    yield mgr
    mgr.close()


@pytest.fixture
def helix_manager_focused():
    """6 genes that pass FOCUSED but fail TIGHT.

    top=4.5, others=2.0 → mean=2.42, ratio≈1.86. PASSES FOCUSED
    (ratio>=1.8, top>=2.5, len>=6). Fails TIGHT (top<5.0 and ratio<3.0).
    Score floor = 4.5*0.15 = 0.675 → all 6 survive.
    """
    mgr = _make_manager_with_n_genes(
        6,
        lambda i: 4.5 if i == 0 else 2.0,
    )
    yield mgr
    mgr.close()


@pytest.fixture
def helix_manager_abstain():
    """8 genes whose scores trigger ABSTAIN: top<2.5 AND ratio<1.8.

    top=1.5, others=1.2 → mean≈1.24, ratio≈1.21. ABSTAIN gate fires
    before BROAD/TIGHT/FOCUSED resolution.
    """
    mgr = _make_manager_with_n_genes(
        8,
        lambda i: 1.5 if i == 0 else 1.2,
    )
    yield mgr
    mgr.close()


class TestFoveatedTierGating:
    """Foveated only fires on BROAD (spec §3, §10 Tests 1-5)."""

    def test_disabled_broad_no_metadata(self, helix_manager_broad_with_twelve_genes):
        """Test 1: foveated_enabled=False on BROAD → no metadata key, uniform compression."""
        m = helix_manager_broad_with_twelve_genes
        m.config.budget.foveated_enabled = False
        window = m.build_context("a query that lands in BROAD")
        assert window.metadata.get("budget_tier") == "broad"
        assert "foveated_caps" not in window.metadata

    def test_enabled_broad_metadata_present(self, helix_manager_broad_with_twelve_genes):
        """Test 2: foveated_enabled=True on BROAD → metadata['foveated_caps'] is a list, len = N."""
        m = helix_manager_broad_with_twelve_genes
        m.config.budget.foveated_enabled = True
        window = m.build_context("a query that lands in BROAD")
        assert window.metadata.get("budget_tier") == "broad"
        caps = window.metadata.get("foveated_caps")
        assert isinstance(caps, list)
        assert len(caps) == 12
        assert window.metadata.get("foveated_alpha") == 1.0

    def test_enabled_tight_no_metadata(self, helix_manager_tight):
        """Test 3: foveated_enabled=True but tier=tight → no metadata key."""
        m = helix_manager_tight
        m.config.budget.foveated_enabled = True
        window = m.build_context("a query that lands in TIGHT")
        assert window.metadata.get("budget_tier") == "tight"
        assert "foveated_caps" not in window.metadata

    def test_enabled_focused_no_metadata(self, helix_manager_focused):
        """Test 4: foveated_enabled=True but tier=focused → no metadata key."""
        m = helix_manager_focused
        m.config.budget.foveated_enabled = True
        window = m.build_context("a query that lands in FOCUSED")
        assert window.metadata.get("budget_tier") == "focused"
        assert "foveated_caps" not in window.metadata

    def test_enabled_abstain_no_metadata(self, helix_manager_abstain):
        """Test 5: foveated_enabled=True but ABSTAIN gate fired first → no metadata key."""
        m = helix_manager_abstain
        m.config.budget.foveated_enabled = True
        window = m.build_context("a weak query that triggers ABSTAIN")
        assert window.metadata.get("budget_tier") == "abstain"
        assert "foveated_caps" not in window.metadata


class TestFoveatedReverseRankEndToEnd:
    """Top-rank gene lands LAST in the assembled prompt (spec §10 Test 7)."""

    def test_top_rank_gene_appears_last_in_window_text(
        self, helix_manager_broad_with_twelve_genes,
    ):
        m = helix_manager_broad_with_twelve_genes
        m.config.budget.foveated_enabled = True
        window = m.build_context("a query that lands in BROAD")
        # The fixture seeds gene_0..gene_11 with descending scores. With
        # foveated on, gene_00 (highest score) should be reversed to LAST.
        # Search by full gene_id (gene_00, gene_11) — the bare prefix
        # "gene_0" would also match gene_01..gene_09 and find the wrong
        # position under reverse-rank ordering.
        idx_top = window.expressed_context.find("gene_00")
        idx_bottom = window.expressed_context.find("gene_11")
        assert idx_top != -1 and idx_bottom != -1
        assert idx_top > idx_bottom, (
            "Reverse-rank failed: top-score gene should appear AFTER "
            "bottom-rank gene in the assembled window."
        )


class TestFoveatedBudgetTrim:
    """Budget-overflow trim must drop the BOTTOM-rank gene under reverse-rank,
    not the top-rank gene (spec §5 placement invariant).

    Latent under default config (default budget never overflows for 12 short
    genes), but a footgun the moment foveated_base_chars is bumped at bench
    time: parts[-1] under reverse-rank IS the top-rank gene, so a naive
    parts.pop() at the trim site would silently drop the most important
    gene first — the exact opposite of what foveated wants.

    Fix shape: when respect_caller_order=True, _assemble pops from the FRONT
    of parts (and sorted_genes in lockstep) instead of the back.
    """

    def test_top_rank_survives_budget_trim_under_reverse_rank(
        self, helix_manager_broad_with_twelve_genes,
    ):
        m = helix_manager_broad_with_twelve_genes
        m.config.budget.foveated_enabled = True
        # Force the budget-overflow trim to fire by shrinking the budget
        # below what 12 genes can fit. With 12 genes and reverse-rank
        # assembly, even short test content + the legibility headers
        # comfortably overflows a ~30-token budget.
        m.config.budget.expression_tokens = 20
        m.config.budget.ribosome_tokens = 10

        window = m.build_context("a query that lands in BROAD")

        # Sanity: the trim actually fired (we should have fewer than 12
        # genes in the final window).
        assert window.metadata.get("budget_tier") == "broad"
        assert window.metadata.get("genes_expressed", 12) < 12, (
            "Test setup did not force a budget-overflow trim — bump the "
            "budget down further or seed longer content."
        )

        # The actual invariant: the TOP-rank gene (gene_00, highest score)
        # MUST still be in the assembled context after the trim. Under
        # reverse-rank ordering parts[-1] is gene_00, so popping from the
        # back (the pre-fix behavior) would have dropped it first.
        assert "gene_00" in window.expressed_context, (
            "Foveated budget-trim dropped the top-rank gene. Under "
            "reverse-rank ordering parts[-1] is the TOP-rank gene; the "
            "trim must pop from the FRONT (lowest rank) instead."
        )

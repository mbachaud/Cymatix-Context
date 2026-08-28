"""W2.4: [budget] min_delivered_docs — the delivered-seat floor.

Wave-2 crater anatomy (docs/research/2026-08-28-wave2-semantic-ranking-
graph-research.md, cat (b)): documents the ranking already placed in the
delivered window lose their seat to the token budget because, with the
compressor off (shipped default), whole-document eviction is the trimmer's
only tool. The floor keeps seats by switching to largest-part
tail-truncation once eviction reaches ``min_delivered_docs`` parts; the
budget still always wins last (below-floor eviction fallback).

Default 0 == legacy trimmer byte-for-byte — the score-aware-trim suite
(tests/test_score_aware_budget_trim.py) is the inertness pin.
"""
from __future__ import annotations

import pytest

from cymatix_context.config import BudgetConfig

# Reuse the score-aware-trim fixture: five genes, scores [10, 1, 8, 2, 9],
# non-monotonic in sequence_index.
from tests.test_score_aware_budget_trim import (  # noqa: F401 (fixture import)
    _spliced_map,
    manager_five_genes,
)


def _assemble(mgr, genes, gene_ids, *, expression, ribosome=100):
    mgr.config.budget.expression_tokens = expression
    mgr.config.budget.ribosome_tokens = ribosome
    return mgr._assemble(
        query="q",
        candidates=genes,
        spliced_map=_spliced_map(gene_ids),
        relation_graph={},
        answer_slate=None,
    )


# ═══ Task 1: knob ══════════════════════════════════════════════════════


def test_default_is_zero_and_inert_field():
    assert BudgetConfig().min_delivered_docs == 0


def test_negative_floor_fails_loud():
    with pytest.raises(ValueError, match="min_delivered_docs"):
        BudgetConfig(min_delivered_docs=-1)


def test_toml_threads_floor(tmp_path):
    import textwrap

    from cymatix_context.config import load_config

    toml = tmp_path / "cymatix.toml"
    toml.write_text(textwrap.dedent("""
        [budget]
        min_delivered_docs = 12
    """), encoding="utf-8")
    assert load_config(str(toml)).budget.min_delivered_docs == 12


# ═══ Task 2: trimmer floor + truncation fallback ═══════════════════════


def test_floor_keeps_all_seats_by_truncation(manager_five_genes):
    mgr, genes, gene_ids, _scores = manager_five_genes
    mgr.config.budget.min_delivered_docs = 5
    window = _assemble(mgr, genes, gene_ids, expression=400)

    assert sorted(window.expressed_gene_ids) == sorted(gene_ids)
    for gid in gene_ids:
        assert gid in window.expressed_context
    budget = mgr.config.budget.ribosome_tokens + mgr.config.budget.expression_tokens
    assert window.total_estimated_tokens <= budget
    assert window.metadata["budget_truncated"] >= 1
    assert window.metadata["budget_evicted"] == 0


def test_partial_floor_keeps_top_scored_seats(manager_five_genes):
    mgr, genes, gene_ids, scores = manager_five_genes
    mgr.config.budget.min_delivered_docs = 3
    window = _assemble(mgr, genes, gene_ids, expression=400)

    delivered = set(window.expressed_gene_ids)
    assert len(delivered) >= 3
    # Eviction above the floor stays score-aware: the floor survivors are
    # the top-|delivered| by score.
    by_score = [g for g, _s in sorted(
        zip(gene_ids, scores), key=lambda x: -x[1])]
    assert delivered == set(by_score[: len(delivered)])
    budget = mgr.config.budget.ribosome_tokens + mgr.config.budget.expression_tokens
    assert window.total_estimated_tokens <= budget


def test_floor_clamps_to_candidate_count(manager_five_genes):
    mgr, genes, gene_ids, _scores = manager_five_genes
    mgr.config.budget.min_delivered_docs = 12  # only 5 candidates exist
    window = _assemble(mgr, genes, gene_ids, expression=400)
    assert sorted(window.expressed_gene_ids) == sorted(gene_ids)


def test_pathological_budget_still_respected(manager_five_genes):
    # Every part at the char floor and still over budget: the floor yields
    # to below-floor eviction — the budget is the hard contract.
    mgr, genes, gene_ids, _scores = manager_five_genes
    mgr.config.budget.min_delivered_docs = 5
    window = _assemble(mgr, genes, gene_ids, expression=50)
    budget = mgr.config.budget.ribosome_tokens + mgr.config.budget.expression_tokens
    assert (window.total_estimated_tokens <= budget
            or len(window.expressed_gene_ids) == 1)


def test_truncation_cuts_tail_and_marks(manager_five_genes):
    mgr, genes, gene_ids, _scores = manager_five_genes
    mgr.config.budget.min_delivered_docs = 5
    window = _assemble(mgr, genes, gene_ids, expression=400)
    # Every part's head (the identifying prefix _spliced_map writes) must
    # survive; at least one part carries the trim mark.
    for gid in gene_ids:
        assert f"content for {gid}:" in window.expressed_context
    assert "...[budget-trimmed]" in window.expressed_context


def test_floor_zero_never_truncates(manager_five_genes):
    mgr, genes, gene_ids, _scores = manager_five_genes
    mgr.config.budget.min_delivered_docs = 0
    window = _assemble(mgr, genes, gene_ids, expression=400)
    assert window.metadata.get("budget_truncated", 0) == 0
    assert "...[budget-trimmed]" not in window.expressed_context
    # Legacy behavior: seats were lost instead.
    assert len(window.expressed_gene_ids) < len(gene_ids)

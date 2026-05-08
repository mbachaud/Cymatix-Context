"""Sub-query decomposition tests (2026-05-08 retrieval stack upgrade, Step 2)."""
import pytest
from unittest.mock import patch, MagicMock
from helix_context.context_manager import HelixContextManager, _merge_subquery_candidates
from helix_context.config import (
    BudgetConfig,
    GenomeConfig,
    HelixConfig,
    RibosomeConfig,
)


def _make_manager() -> HelixContextManager:
    cfg = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=4),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        synonym_map={},
    )
    return HelixContextManager(cfg)


@pytest.fixture
def ctx_manager():
    mgr = _make_manager()
    yield mgr
    mgr.close()


def test_merge_subquery_candidates_cross_query_boost():
    """Gene appearing in 2/3 sub-results should rank above one with higher base score but 1/3."""
    from helix_context.schemas import Gene, PromoterTags, EpigeneticMarkers
    from helix_context.genome import Genome as _G

    def _g(txt):
        return Gene(gene_id=_G.make_gene_id(txt), content=txt, complement="",
                    codons=[], promoter=PromoterTags(), epigenetics=EpigeneticMarkers())

    gene_a = _g("aaa content")
    gene_b = _g("bbb content")
    # gene_a in 2/3 sub-results, gene_b in 1/3 but with higher base score
    merged = _merge_subquery_candidates(
        [[gene_a, gene_b], [gene_a], []],
        base_scores={gene_a.gene_id: 3.0, gene_b.gene_id: 4.0},
    )
    ids = [g.gene_id for g in merged]
    assert ids.index(gene_a.gene_id) < ids.index(gene_b.gene_id), (
        "gene_a (2 sub-query hits) must rank above gene_b (1 hit, higher base score)"
    )


def test_merge_subquery_candidates_deduplicates():
    """Same gene in multiple sub-results must appear only once in merged output."""
    from helix_context.schemas import Gene, PromoterTags, EpigeneticMarkers
    from helix_context.genome import Genome as _G

    g = Gene(gene_id=_G.make_gene_id("dup"), content="dup", complement="",
             codons=[], promoter=PromoterTags(), epigenetics=EpigeneticMarkers())
    merged = _merge_subquery_candidates([[g], [g], [g]], base_scores={g.gene_id: 1.0})
    assert len(merged) == 1


def test_decompose_disabled_returns_original(ctx_manager):
    """When query_decomposition_enabled=False and intent is UNKNOWN, returns [original_query].

    Step 3B added LLM-free heuristic routing for recognized intent classes;
    for queries that classify as UNKNOWN the passthrough is still the same.
    """
    ctx_manager.config.ribosome.query_decomposition_enabled = False
    # Use a query whose intent_class classifies as UNKNOWN (no matching heuristic keyword)
    result = ctx_manager._decompose_query("xyzzy frob quux blorp")
    assert result == ["xyzzy frob quux blorp"]


def test_decompose_is_cached(ctx_manager):
    """Two calls with the same query must only invoke the LLM backend once."""
    ctx_manager.config.ribosome.query_decomposition_enabled = True
    with patch.object(ctx_manager.ribosome.backend, "is_disabled_backend", False, create=True):
        with patch.object(
            ctx_manager.ribosome.backend, "complete",
            return_value=(
                "1. what is the density gate threshold?\n"
                "2. what triggers density gate demotion?\n"
                "3. which chromatin tier does density gate assign?"
            ),
        ) as mock:
            ctx_manager._decompose_query("how does the density gate work")
            ctx_manager._decompose_query("how does the density gate work")
    assert mock.call_count == 1, "LLM must be called only once (cached)"


def test_decompose_malformed_output_falls_back(ctx_manager):
    """Malformed LLM output (no numbered lines) must fall back to [original_query]."""
    ctx_manager.config.ribosome.query_decomposition_enabled = True
    with patch.object(ctx_manager.ribosome.backend, "complete", return_value="not numbered"):
        result = ctx_manager._decompose_query("how does the density gate work")
    assert result == ["how does the density gate work"]

"""Bugbash BUG-1 / BUG-3: request-scoped retrieval scores + post-trim health.

BUG-1 (score/result atomicity): ``_build_context_impl`` used to read
``genome.last_query_scores`` at six-plus points spread across the turn
(tiering, slate ordering, legibility, budget trim, health). The map is
instance-global mutable state republished by EVERY retrieval, so a
concurrent request could associate query A's candidates with query B's
scores — silently altering tiering, ordering, trimming, and abstention.
The fix snapshots the map once (under the store lock) right after this
request's retrieval and threads the request-scoped dict through the
rest of the pipeline (``query_scores`` parameter on the refiners,
``_assemble``, and ``_compute_health``).

BUG-3 (pre-trim health): ``_assemble`` used to compute health over the
PRE-trim candidate list, so ``genes_expressed`` / coverage / freshness
described evidence that the budget trim had already dropped from the
returned context. Health must reflect the post-trim, actually-returned
set.
"""
from __future__ import annotations

import pytest

from cymatix_context.config import (
    BudgetConfig,
    ClassifierConfig,
    GenomeConfig,
    HelixConfig,
    RibosomeConfig,
)
from cymatix_context.context_manager import HelixContextManager
from cymatix_context.schemas import Gene, PromoterTags

from tests.conftest import MockCompressorBackend, make_gene, make_helix_config


def _make_manager():
    cfg = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=12),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        classifier=ClassifierConfig(enabled=False),
    )
    return HelixContextManager(cfg)


def _make_genes(gene_ids, contents=None):
    return [
        Gene(
            gene_id=gid,
            content=(contents or {}).get(gid, f"{gid} content payload " * 20),
            complement=f"complement-{gid}",
            codons=["c"],
            promoter=PromoterTags(sequence_index=i),
        )
        for i, gid in enumerate(gene_ids)
    ]


def _spliced_map(gene_ids):
    return {
        gid: f"content for {gid}: " + ("payload " * 60)
        for gid in gene_ids
    }


SCORES = [10.0, 1.0, 8.0, 2.0, 9.0]
GENE_IDS = [f"gene_{i:02d}" for i in range(5)]


# ─────────────────────────────────────────────────────────────────────
# BUG-1: request-scoped scores
# ─────────────────────────────────────────────────────────────────────

class TestRequestScopedScores:
    def test_assemble_trim_uses_request_scoped_scores(self):
        """``_assemble(query_scores=...)`` must trim by THIS request's
        scores even when ``genome.last_query_scores`` has since been
        republished by a concurrent request with an inverted ranking."""
        mgr = _make_manager()
        try:
            genes = _make_genes(GENE_IDS)
            request_scores = {
                gid: SCORES[i] for i, gid in enumerate(GENE_IDS)
            }
            # Simulate a concurrent request B publishing its own map
            # between this request's retrieval and assembly. Ranking is
            # inverted so a genome-state read drops the WRONG gene.
            mgr.genome.last_query_scores = {
                gid: 100.0 - SCORES[i] for i, gid in enumerate(GENE_IDS)
            }
            mgr.config.budget.expression_tokens = 300
            mgr.config.budget.ribosome_tokens = 100

            window = mgr._assemble(
                query="q",
                candidates=genes,
                spliced_map=_spliced_map(GENE_IDS),
                relation_graph={},
                answer_slate=None,
                query_scores=request_scores,
            )

            # gene_00 (request score 10) must survive; gene_01 (request
            # score 1) must be the first dropped. Under the clobbered
            # genome map the ranking is exactly inverted.
            assert "gene_00" in window.expressed_context
            assert "gene_01" not in window.expressed_context
        finally:
            mgr.close()

    def test_build_context_health_immune_to_concurrent_score_clobber(self):
        """End-to-end: after the refiner stage, a concurrent request
        republishes ``genome.last_query_scores`` with a foreign map.
        Downstream stages (tiering, trim, health) must keep using this
        request's own scores."""
        config = make_helix_config(
            synonym_map={"auth": ["jwt", "login", "security"]},
        )
        mgr = HelixContextManager(config)
        mgr.ribosome.backend = MockCompressorBackend()
        try:
            seed = [
                make_gene(
                    "JWT authentication middleware",
                    domains=["auth", "security"], entities=["jwt"],
                    gene_id="auth_gene_001",
                ),
                make_gene(
                    "Database connection pooling",
                    domains=["database", "performance"], entities=["postgres"],
                    gene_id="db_gene_0001",
                ),
                make_gene(
                    "React component state management",
                    domains=["frontend", "react"], entities=["useState"],
                    gene_id="react_gene_01",
                ),
            ]
            for g in seed:
                mgr.genome.upsert_gene(g)

            real_scores: dict = {}
            original_refiners = mgr._apply_candidate_refiners

            def clobbering_refiners(*args, **kwargs):
                result = original_refiners(*args, **kwargs)
                # Snapshot what THIS request's scores actually are, then
                # simulate a concurrent request republishing the shared map.
                real_scores.update(dict(mgr.genome.last_query_scores or {}))
                mgr.genome.last_query_scores = {
                    gid: 10_000.0 + i
                    for i, gid in enumerate(real_scores)
                }
                return result

            mgr._apply_candidate_refiners = clobbering_refiners

            window = mgr.build_context("How does JWT auth work?")

            health = window.context_health
            assert health.genes_expressed >= 1, (
                "Pipeline mis-tiered/abstained under a foreign score map — "
                f"status={health.status!r}, metadata={window.metadata!r}"
            )
            # top_score_raw is computed off the score map; the request's
            # own scores are O(1-50), the foreign clobber is O(10_000).
            assert health.top_score_raw < 1_000.0, (
                "Health was computed from a concurrent request's score "
                f"map (top_score_raw={health.top_score_raw})."
            )
        finally:
            mgr.close()


# ─────────────────────────────────────────────────────────────────────
# BUG-3: health must reflect the post-trim, returned set
# ─────────────────────────────────────────────────────────────────────

class TestPostTrimHealth:
    def test_health_genes_expressed_matches_returned_set(self):
        """After a budget trim fires, ``health.genes_expressed`` must
        equal the number of documents actually in the returned context,
        not the pre-trim candidate count."""
        mgr = _make_manager()
        try:
            genes = _make_genes(GENE_IDS)
            mgr.genome.last_query_scores = {
                gid: SCORES[i] for i, gid in enumerate(GENE_IDS)
            }
            mgr.config.budget.expression_tokens = 300
            mgr.config.budget.ribosome_tokens = 100

            window = mgr._assemble(
                query="q",
                candidates=genes,
                spliced_map=_spliced_map(GENE_IDS),
                relation_graph={},
                answer_slate=None,
            )

            assert len(window.expressed_gene_ids) < len(genes), (
                "Budget did not fire a drop — lower expression_tokens to "
                "force a trim."
            )
            health = window.context_health
            assert health.genes_expressed == len(window.expressed_gene_ids)
            assert health.genes_expressed == window.metadata["genes_expressed"]
        finally:
            mgr.close()

    def test_health_coverage_excludes_trimmed_evidence(self):
        """A query term that appears ONLY in a trimmed-away document must
        not count toward coverage — the returned context does not contain
        that evidence."""
        mgr = _make_manager()
        try:
            contents = {
                "gene_01": "zzquniqueterm only lives here " * 20,
            }
            genes = _make_genes(GENE_IDS, contents=contents)
            mgr.genome.last_query_scores = {
                gid: SCORES[i] for i, gid in enumerate(GENE_IDS)
            }
            mgr.config.budget.expression_tokens = 300
            mgr.config.budget.ribosome_tokens = 100

            window = mgr._assemble(
                query="q",
                candidates=genes,
                spliced_map=_spliced_map(GENE_IDS),
                relation_graph={},
                answer_slate=None,
                query_signals=(["zzquniqueterm"], []),
            )

            # gene_01 (score=1, the global minimum) is trimmed first.
            assert "gene_01" not in window.expressed_context, (
                "Trim did not drop gene_01 — tighten the budget."
            )
            assert window.context_health.coverage == 0.0, (
                "Coverage counted a term whose only evidence was trimmed "
                "out of the returned context."
            )
        finally:
            mgr.close()

"""Sema vectorless auto-gate (council fix).

With zero non-null ``genes.embedding`` rows, the query-side ΣĒMA Tier 4
still paid one remote ``codec.encode()`` RTT per query plus a repeated
full-table cache-rebuild scan every time Mode B's undersized-pool trigger
fired — provably wasted work, since no candidate can ever carry a vector.

``KnowledgeStore._build_sema_cache`` (hot tier) and
``KnowledgeStore._build_cold_sema_cache`` (cold/heterochromatin tier) now
set a store-level "vectorless" flag the first time they find zero non-null
embedding rows. The hot flag (``_sema_vectorless``) short-circuits the
query-side ``codec.encode()`` call in ``query_docs`` Tier 4 (and the Mode B
rebuild scan that would otherwise retry every query). Both flags clear on
cache invalidation (``invalidate_sema_cache`` / ``invalidate_cold_sema_cache``)
so a bed that later gains vectors (upsert, backfill) re-enables
automatically. Exactly one "sema tier idle: no stored vectors" log.info
fires per store lifetime, shared across both tiers.
"""
from __future__ import annotations

import logging

import pytest

from cymatix_context.genome import Genome
from tests.conftest import make_gene


class FakeSemaCodec:
    """Counts encode() calls without touching a real model or daemon."""

    def __init__(self):
        self.encode_calls = 0

    def encode(self, text):
        self.encode_calls += 1
        return [0.1] * 20

    @staticmethod
    def nearest(query_vec, candidates, k=5):
        # Trivial stand-in — Mode A only runs when it finds candidates,
        # which never happens on the vectorless-bed test path. Kept simple
        # (no numpy) since it's not the behavior under test.
        return [(cid, 0.5) for cid, _ in candidates[:k]]


@pytest.fixture
def sema_genome():
    codec = FakeSemaCodec()
    g = Genome(path=":memory:", sema_codec=codec)
    g.codec = codec  # convenience handle for assertions
    yield g
    g.close()


def test_vectorless_bed_never_calls_encode(sema_genome):
    """Genes with no embeddings at all: the very first query must not
    call codec.encode(), and must log the idle line exactly once."""
    make_ids = []
    for i in range(2):
        g = make_gene(f"vectorless content {i}", domains=["nomatch"])
        sema_genome.upsert_gene(g)
        make_ids.append(g.gene_id)

    logger = logging.getLogger("cymatix_context.knowledge_store")
    with _capture(logger) as records:
        genes = sema_genome.query_genes(domains=["nomatch"], entities=[], max_genes=8)

    assert isinstance(genes, list)
    assert sema_genome.codec.encode_calls == 0, (
        "codec.encode() must never fire on a vectorless bed"
    )
    assert sema_genome._sema_vectorless is True
    idle_lines = [r for r in records if "sema tier idle: no stored vectors" in r.getMessage()]
    assert len(idle_lines) == 1, f"expected exactly one idle log line, got {len(idle_lines)}"

    # A second query must not call encode() either, and must not log again.
    with _capture(logger) as records2:
        sema_genome.query_genes(domains=["nomatch"], entities=[], max_genes=8)
    assert sema_genome.codec.encode_calls == 0
    idle_lines2 = [r for r in records2 if "sema tier idle: no stored vectors" in r.getMessage()]
    assert idle_lines2 == [], "idle log line must fire once per store lifetime, not per query"


def test_bed_with_vectors_unchanged(sema_genome):
    """A bed with vectors must still call encode() and populate the cache —
    byte-identical to pre-fix behavior."""
    g = make_gene("vectored content", domains=["hasvec"])
    g.embedding = [0.2] * 20
    sema_genome.upsert_gene(g)

    genes = sema_genome.query_genes(domains=["hasvec"], entities=[], max_genes=8)

    assert isinstance(genes, list)
    assert sema_genome.codec.encode_calls == 1
    assert sema_genome._sema_vectorless is False
    assert sema_genome._sema_cache is not None
    assert g.gene_id in sema_genome._sema_cache["gene_ids"]


def test_invalidation_reenables_encode(sema_genome):
    """A vectorless bed that later gains a vector must re-check and start
    calling encode() again after the cache is invalidated."""
    g1 = make_gene("still vectorless", domains=["reflag"])
    sema_genome.upsert_gene(g1)

    sema_genome.query_genes(domains=["reflag"], entities=[], max_genes=8)
    assert sema_genome.codec.encode_calls == 0
    assert sema_genome._sema_vectorless is True

    # Ingest path (upsert_doc) invalidates both sema caches — this must
    # clear the vectorless flag even though the new gene has no vector
    # itself; the *next build* is what re-establishes the verdict.
    g2 = make_gene("second doc, still no vector", domains=["reflag"])
    sema_genome.upsert_gene(g2)
    assert sema_genome._sema_vectorless is False, (
        "invalidate_sema_cache (called from upsert_doc) must clear the flag"
    )

    # Now actually add a vectored gene and confirm the tier re-enables.
    g3 = make_gene("finally vectored", domains=["reflag"])
    g3.embedding = [0.3] * 20
    sema_genome.upsert_gene(g3)

    sema_genome.query_genes(domains=["reflag"], entities=[], max_genes=8)
    assert sema_genome.codec.encode_calls == 1, (
        "encode() must fire again once the bed has gained a vector"
    )
    assert sema_genome._sema_vectorless is False
    assert sema_genome._sema_cache is not None


def test_invalidate_sema_cache_clears_vectorless_flag_directly():
    """Unit-level: invalidate_sema_cache() clears _sema_vectorless even
    without going through a query."""
    g = Genome(path=":memory:")
    g._sema_vectorless = True
    g.invalidate_sema_cache()
    assert g._sema_vectorless is False
    g.close()


def test_invalidate_cold_sema_cache_clears_cold_vectorless_flag_directly():
    g = Genome(path=":memory:")
    g._cold_sema_vectorless = True
    g.invalidate_cold_sema_cache()
    assert g._cold_sema_vectorless is False
    g.close()


def test_build_sema_cache_sets_flag_on_zero_rows():
    """Direct unit test of _build_sema_cache: zero non-null embedding rows
    -> _sema_vectorless flips true, _sema_cache stays None."""
    g = Genome(path=":memory:")
    gene = make_gene("no embedding here", domains=["x"])
    g.upsert_gene(gene)
    assert g._sema_vectorless is False
    g._build_sema_cache()
    assert g._sema_vectorless is True
    assert g._sema_cache is None
    g.close()


def test_build_cold_sema_cache_sets_flag_on_zero_rows():
    """Direct unit test of _build_cold_sema_cache: zero non-null embedding
    rows in the heterochromatin partition -> _cold_sema_vectorless flips
    true, _cold_sema_cache stays None."""
    g = Genome(path=":memory:")
    gene = make_gene("no embedding here either", domains=["x"])
    g.upsert_gene(gene)
    g.compress_to_heterochromatin(gene.gene_id)
    assert g._cold_sema_vectorless is False
    g._build_cold_sema_cache()
    assert g._cold_sema_vectorless is True
    assert g._cold_sema_cache is None
    g.close()


class _capture:
    """Minimal log-capture context manager independent of caplog's handler
    plumbing quirks — attaches a plain logging.Handler to the target logger
    for the duration of the block and yields the list of records emitted.
    """

    def __init__(self, logger):
        self._logger = logger
        self._handler = logging.Handler()
        self._records: list[logging.LogRecord] = []
        self._handler.emit = self._records.append  # type: ignore[method-assign]
        self._prev_level = logger.level

    def __enter__(self):
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.INFO)
        return self._records

    def __exit__(self, exc_type, exc, tb):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._prev_level)
        return False

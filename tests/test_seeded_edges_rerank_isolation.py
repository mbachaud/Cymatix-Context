"""Issue #342: Hebbian edge evidence must accrue from the PRE-rerank order.

Before the fix, ``query_docs`` passed the post-rerank ``ranked_ids`` into
``update_edge_evidence``, so with ``rerank_enabled=true`` AND
``seeded_edges_enabled=true`` an ON arm mutated ``harmonic_links``
differently from OFF on the identical query — persistent cross-arm
contamination on any shared A/B bed, and a feedback loop between the
cross-encoder's opinions and the seeded-edge graph. Both flags ship
default-off, so the hazard was dormant; this pins the fix so it stays
fixed once they flip.

Setup (deterministic, no model — the DI ``rerank_scorer`` seam from
tests/test_rerank_precap.py): 30-doc pool, ``max_genes=12`` (delivered cut
24), marker doc ``g-25`` outside the OFF-arm cut, a rerank scorer that
promotes the marker to rank 1, and an identical pre-seeded
``harmonic_links`` edge g-00 <-> g-25 in both arms.
"""

import time

import pytest

from cymatix_context.genome import Genome
from cymatix_context.retrieval import seeded_edges
from cymatix_context.schemas import (
    ChromatinState,
    EpigeneticMarkers,
    Gene,
    PromoterTags,
)

POOL = 30
MARKER = "xylophone"
MARKER_IDX = 25  # g-25: below the max_genes*2 = 24 cut in the OFF arm
MAX_GENES = 12


def _gid(i: int) -> str:
    return f"g-{i:02d}"


def _make_gene(content: str, gid: str) -> Gene:
    return Gene(
        gene_id=gid,
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=["topic"], entities=[], summary=content),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
    )


def _build_store(rerank_on: bool) -> Genome:
    store = Genome(
        path=":memory:",
        dense_embedding_enabled=False,
        fusion_mode="rrf",
        seeded_edges_enabled=True,
        rerank_enabled=rerank_on,
        rerank_depth=POOL,
        rerank_scorer=lambda q, texts: [
            1.0 if MARKER in t else 0.01 for t in texts
        ],
    )
    for i in range(POOL):
        word = MARKER if i == MARKER_IDX else f"filler{i:02d}"
        content = (
            f"Document {i:02d} covers the subject matter at hand. "
            f"It discusses {word} in enough detail that the body is "
            f"non-trivial prose rather than a stub."
        )
        store.upsert_gene(_make_gene(content, _gid(i)), apply_gate=False)
    now = int(time.time())
    store.conn.execute(
        """INSERT INTO harmonic_links
           (gene_id_a, gene_id_b, weight, updated_at, source,
            co_count, miss_count, created_at)
           VALUES (?, ?, 1.0, ?, 'seeded', 0, 0.0, ?)""",
        (_gid(0), _gid(MARKER_IDX), now, now),
    )
    store.conn.commit()
    return store


def _run_arm(monkeypatch, rerank_on: bool):
    """Return (expressed_ids seen by update_edge_evidence, delivered ids,
    harmonic_links row after the query)."""
    store = _build_store(rerank_on)
    seen = {}
    real = seeded_edges.update_edge_evidence

    def spy(genome, gene_scores, expressed_ids, *, max_genes):
        seen["expressed_ids"] = list(expressed_ids)
        return real(genome, gene_scores, expressed_ids, max_genes=max_genes)

    monkeypatch.setattr(seeded_edges, "update_edge_evidence", spy)
    try:
        docs = store.query_docs(
            ["topic"], [], max_genes=MAX_GENES,
            query_text="query text", use_harmonic=False,
        )
        delivered = [d.gene_id for d in docs]
        row = store.conn.execute(
            "SELECT gene_id_a, gene_id_b, source, co_count, miss_count "
            "FROM harmonic_links",
        ).fetchone()
        edge = dict(row) if row is not None else None
    finally:
        monkeypatch.undo()
        store.close()
    return seen, delivered, edge


def test_evidence_is_rerank_invariant(monkeypatch):
    off_seen, off_delivered, off_edge = _run_arm(monkeypatch, rerank_on=False)
    on_seen, on_delivered, on_edge = _run_arm(monkeypatch, rerank_on=True)

    marker = _gid(MARKER_IDX)

    # The rerank itself still works: the ON arm delivers the marker at
    # rank 1 while the OFF arm never delivers it.
    assert marker not in off_delivered
    assert on_delivered[0] == marker

    # #342 fix: the evidence writer sees the PRE-rerank fusion order in
    # BOTH arms — identical ids, identical order.
    assert on_seen["expressed_ids"] == off_seen["expressed_ids"]
    assert marker not in on_seen["expressed_ids"]

    # And the persistent harmonic_links mutation is byte-identical across
    # arms — the invariant shared A/B beds assume.
    assert on_edge == off_edge


def test_evidence_matches_delivered_when_rerank_off(monkeypatch):
    """OFF arm sanity: without a rerank, evidence ids == delivered ids
    (the pre-fix behaviour, preserved byte-identically)."""
    off_seen, off_delivered, _ = _run_arm(monkeypatch, rerank_on=False)
    assert off_seen["expressed_ids"] == off_delivered

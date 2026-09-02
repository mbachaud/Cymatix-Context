"""Posting-count hub cutoff for entity auto-linking.

``auto_link_by_entity()`` sweeps every ``entity_graph`` posting row for
each of the upserted gene's (up to 15) entities before its GROUP BY. On
hub-heavy corpora that sweep is O(hub-posting-list) per insert = O(N^2)
per build: on the 2026-08-30 enronqa_padded bed at 289k genes,
'content-type' (171,696 postings) and 'content-transfer-encoding'
(171,352) made the query 148.9 ms/insert — 89.5% of writer wall time —
vs 7.4 ms without the two hubs (benchmarks/dogfood/receipts/
ingest_decay_enronqa_2026-08-30.json).

The hub cutoff bounds the sweep: entities whose posting count exceeds
the bound are dropped from the probe set before the GROUP BY
(``PKI_NOISE_CUTOFF`` precedent in storage/indexes.py — "cardinality >
cutoff is hard-skipped"). The count probe itself is LIMIT-bounded, so
per-insert cost is O(n_entities * cutoff), never O(hub-posting-list).

Default 0 = disabled = current behavior, byte-identical.
"""

import pytest

from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.schemas import Gene, PromoterTags, EpigeneticMarkers
from cymatix_context.storage.co_activation import auto_link_by_entity


# ── helpers ──────────────────────────────────────────────────────────

def _seed_postings(cur, rows):
    """rows: [(entity, gene_id)] straight into entity_graph."""
    cur.executemany(
        "INSERT OR IGNORE INTO entity_graph (entity, gene_id) VALUES (?, ?)",
        rows,
    )


def _cover_edges(cur, gene_id):
    """{peer_id: confidence} for relation=5 (COVER) edges from gene_id."""
    rows = cur.execute(
        "SELECT gene_id_b, confidence FROM gene_relations "
        "WHERE gene_id_a = ? AND relation = 5",
        (gene_id,),
    ).fetchall()
    return {r["gene_id_b"]: r["confidence"] for r in rows}


def _make_gene(content, entities):
    from cymatix_context.genome import Genome

    return Gene(
        gene_id=Genome.make_gene_id(content),
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=["hubtest"], entities=entities),
        epigenetics=EpigeneticMarkers(),
    )


# ── unit level: auto_link_by_entity ──────────────────────────────────

def test_default_links_through_hub_entities(genome):
    """hub_cutoff omitted (=0 = disabled): hub entities still count
    toward the shared>=2 gate — current behavior, unchanged."""
    cur = genome.conn.cursor()
    _seed_postings(cur, [("hub", f"h{i}") for i in range(6)])
    _seed_postings(cur, [("hub", "p1"), ("alpha", "p1")])
    _seed_postings(cur, [("alpha", "p2"), ("beta", "p2")])

    auto_link_by_entity("X", ["hub", "alpha", "beta"], cur)

    edges = _cover_edges(cur, "X")
    assert set(edges) == {"p1", "p2"}
    assert edges["p1"] == pytest.approx(2 / 3)
    assert edges["p2"] == pytest.approx(2 / 3)


def test_hub_cutoff_drops_hub_dependent_edges(genome):
    """With hub_cutoff=5, 'hub' (7 postings > 5) leaves the probe set:
    a peer whose only second shared entity was the hub no longer links;
    a peer sharing 2+ non-hub entities still does."""
    cur = genome.conn.cursor()
    _seed_postings(cur, [("hub", f"h{i}") for i in range(6)])
    _seed_postings(cur, [("hub", "p1"), ("alpha", "p1")])
    _seed_postings(cur, [("alpha", "p2"), ("beta", "p2")])

    auto_link_by_entity("X", ["hub", "alpha", "beta"], cur, hub_cutoff=5)

    edges = _cover_edges(cur, "X")
    assert set(edges) == {"p2"}


def test_hub_cutoff_confidence_uses_surviving_probe_set(genome):
    """Confidence stays internally consistent: shared / |probed set|
    (the post-filter set), not the pre-filter entity count."""
    cur = genome.conn.cursor()
    _seed_postings(cur, [("hub", f"h{i}") for i in range(6)])
    _seed_postings(cur, [("alpha", "p2"), ("beta", "p2")])

    auto_link_by_entity("X", ["hub", "alpha", "beta"], cur, hub_cutoff=5)

    edges = _cover_edges(cur, "X")
    assert edges["p2"] == pytest.approx(1.0)  # 2 shared / 2 probed


def test_hub_cutoff_boundary_count_equal_to_cutoff_is_kept(genome):
    """PKI semantics: cardinality strictly GREATER than the cutoff is
    skipped; an entity with exactly cutoff postings stays."""
    cur = genome.conn.cursor()
    _seed_postings(cur, [("edge", f"h{i}") for i in range(4)])  # 4 rows
    _seed_postings(cur, [("edge", "p1"), ("alpha", "p1")])       # 5th row

    auto_link_by_entity("X", ["edge", "alpha"], cur, hub_cutoff=5)

    assert set(_cover_edges(cur, "X")) == {"p1"}


def test_hub_cutoff_fewer_than_two_survivors_forms_no_edges(genome):
    """shared>=2 needs at least 2 probe entities; if the filter leaves
    fewer, no edges can form and the GROUP BY is skipped."""
    cur = genome.conn.cursor()
    _seed_postings(cur, [("hub", f"h{i}") for i in range(6)])
    _seed_postings(cur, [("hub2", f"g{i}") for i in range(6)])
    _seed_postings(cur, [("hub", "p1"), ("alpha", "p1")])

    auto_link_by_entity("X", ["hub", "hub2", "alpha"], cur, hub_cutoff=5)

    assert _cover_edges(cur, "X") == {}


# ── store level: knob threading through upsert ───────────────────────

def _build_store(tmp_path, name, cutoff):
    store = KnowledgeStore(
        path=str(tmp_path / name),
        entity_graph=True,
        entity_autolink_hub_cutoff=cutoff,
    )
    # 6 hub carriers, then a hub+alpha peer, then an alpha+beta peer.
    for i in range(6):
        store.upsert_gene(_make_gene(f"hub carrier {i}", ["hub", f"uniq{i}"]))
    p1 = _make_gene("peer one hub alpha", ["hub", "alpha"])
    p2 = _make_gene("peer two alpha beta", ["alpha", "beta"])
    x = _make_gene("subject gene x", ["hub", "alpha", "beta"])
    for g in (p1, p2, x):
        store.upsert_gene(g)
    return store, p1.gene_id, p2.gene_id, x.gene_id


def test_store_ctor_stores_hub_cutoff_attr(tmp_path):
    store = KnowledgeStore(
        path=str(tmp_path / "attr.db"), entity_autolink_hub_cutoff=42
    )
    try:
        assert store._entity_autolink_hub_cutoff == 42
    finally:
        store.close()


def test_store_default_zero_links_through_hubs(tmp_path):
    store, p1, p2, x = _build_store(tmp_path, "off.db", 0)
    try:
        edges = _cover_edges(store.conn.cursor(), x)
        assert set(edges) == {p1, p2}
    finally:
        store.close()


def test_store_cutoff_drops_hub_dependent_edges(tmp_path):
    store, p1, p2, x = _build_store(tmp_path, "on.db", 5)
    try:
        edges = _cover_edges(store.conn.cursor(), x)
        assert set(edges) == {p2}
    finally:
        store.close()


# ── config level ─────────────────────────────────────────────────────

def test_ingestion_config_default_is_200():
    # #411 flip (2026-08-31): shipped default = 200 (PKI precedent).
    # Legacy behavior is the explicit opt-out `entity_autolink_hub_cutoff
    # = 0`; the bare KnowledgeStore kwarg stays 0 (non-config constructors
    # keep byte-identical legacy linking).
    from cymatix_context.config import CymatixConfig

    assert CymatixConfig().ingestion.entity_autolink_hub_cutoff == 200


def test_ingestion_config_parses_hub_cutoff(tmp_path):
    from cymatix_context.config import load_config

    toml = tmp_path / "cymatix.toml"
    toml.write_text("[ingestion]\nentity_autolink_hub_cutoff = 200\n")
    cfg = load_config(str(toml))
    assert cfg.ingestion.entity_autolink_hub_cutoff == 200


def test_build_genome_kwargs_carries_hub_cutoff():
    from cymatix_context.config import CymatixConfig
    from cymatix_context.sharding import build_genome_kwargs

    cfg = CymatixConfig()
    cfg.ingestion.entity_autolink_hub_cutoff = 123
    kwargs = build_genome_kwargs(cfg)
    assert kwargs["entity_autolink_hub_cutoff"] == 123

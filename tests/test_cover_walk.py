"""W2.1: COVER-edge walk — reverse index, walk module, knobs, query wiring.

Spec: docs/research/2026-08-28-wave2-semantic-ranking-graph-research.md
(lane 1) + docs/superpowers/plans/2026-08-28-w2.1-cover-walk.md. The walk
reads the ingest-built ``gene_relations`` COVER graph (relation=5,
entity-overlap kNN, storage/co_activation.py:29-63) — the one populated
gene-gene substrate on bulk-ingested beds — as a query-time ranking signal:
an eps-band-confined rerank class plus an append-below-head rescue lane.
Everything ships default-inert (``cover_walk_enabled = false``).
"""
from __future__ import annotations

import sqlite3

import pytest


def _fresh_conn() -> sqlite3.Connection:
    from cymatix_context.storage.ddl import init_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


# ═══ Task 1: reverse adjacency index ═══════════════════════════════════


def test_ddl_creates_reverse_index():
    conn = _fresh_conn()
    try:
        names = {r[1] for r in conn.execute("PRAGMA index_list('gene_relations')")}
        assert "idx_gene_relations_rel_b" in names
    finally:
        conn.close()


# ═══ Task 2: walk module ═══════════════════════════════════════════════


class _StubGenome:
    """The walk module's whole genome contract is ``.read_conn``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.read_conn = conn


def _edges(conn: sqlite3.Connection, rows) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO gene_relations "
        "(gene_id_a, gene_id_b, relation, confidence, updated_at) "
        "VALUES (?, ?, 5, ?, 0)",
        rows,
    )


def _walk(conn, seeds, **kw):
    from cymatix_context.retrieval.cover_walk import cover_walk_mass

    return cover_walk_mass(_StubGenome(conn), seeds, **kw)


def test_one_hop_mass_is_confidence_weighted():
    conn = _fresh_conn()
    try:
        _edges(conn, [("s", "a", 0.8), ("s", "b", 0.2)])
        mass, diag = _walk(conn, ["s"], hops=1, gamma=0.5)
        # seed mass 1.0; s's outgoing share is gamma * mass * conf_i / sum(conf)
        assert mass["a"] == pytest.approx(0.5 * 0.8 / 1.0)
        assert mass["b"] == pytest.approx(0.5 * 0.2 / 1.0)
        assert diag["mode"] == "bidirectional"
        assert diag["seeds_in_graph"] == 1
    finally:
        conn.close()


def test_gamma_damps_second_hop_with_backflow():
    conn = _fresh_conn()
    try:
        _edges(conn, [("s", "a", 1.0), ("a", "c", 1.0)])
        mass, _diag = _walk(conn, ["s"], hops=2, gamma=0.5)
        # hop1: a = 0.5. hop2 from a (neighbors s and c, equal conf):
        # c = 0.5 * 0.5 * (1/2) = 0.125; the s backflow is absorbed by the
        # seed and never reported.
        assert mass["a"] == pytest.approx(0.5)
        assert mass["c"] == pytest.approx(0.125)
        assert "s" not in mass
    finally:
        conn.close()


def test_seeds_never_in_result():
    conn = _fresh_conn()
    try:
        _edges(conn, [("s1", "s2", 1.0), ("s1", "x", 1.0)])
        mass, _ = _walk(conn, ["s1", "s2"], hops=2)
        assert "s1" not in mass and "s2" not in mass
        assert "x" in mass
    finally:
        conn.close()


def test_hub_reachable_but_never_expanded():
    conn = _fresh_conn()
    try:
        # hub has degree 4 (s + 3 leaves); degree_cap=3 -> hub receives mass
        # but its leaves get nothing through it.
        _edges(conn, [("s", "hub", 1.0),
                      ("hub", "l1", 1.0), ("hub", "l2", 1.0), ("hub", "l3", 1.0)])
        mass, _ = _walk(conn, ["s"], hops=2, degree_cap=3)
        assert "hub" in mass
        assert "l1" not in mass and "l2" not in mass and "l3" not in mass
    finally:
        conn.close()


def test_frontier_cap_keeps_top_mass_deterministically():
    conn = _fresh_conn()
    try:
        # Three equal-mass children each with a distinct grandchild; cap=2
        # expands only the lexicographically first two children (mass tie
        # broken by gene_id asc).
        _edges(conn, [("s", "c1", 1.0), ("s", "c2", 1.0), ("s", "c3", 1.0),
                      ("c1", "g1", 1.0), ("c2", "g2", 1.0), ("c3", "g3", 1.0)])
        mass, _ = _walk(conn, ["s"], hops=2, frontier_cap=2)
        assert "g1" in mass and "g2" in mass
        assert "g3" not in mass
    finally:
        conn.close()


def test_bidirectional_reaches_in_edges():
    conn = _fresh_conn()
    try:
        _edges(conn, [("x", "s", 0.9)])  # x links TO the seed
        mass, diag = _walk(conn, ["s"], hops=1)
        assert "x" in mass
        assert diag["mode"] == "bidirectional"
    finally:
        conn.close()


def test_forward_only_when_reverse_index_missing():
    conn = _fresh_conn()
    try:
        conn.execute("DROP INDEX idx_gene_relations_rel_b")
        _edges(conn, [("x", "s", 0.9), ("s", "y", 0.9)])
        mass, diag = _walk(conn, ["s"], hops=1)
        assert diag["mode"] == "forward_only"
        assert "y" in mass
        assert "x" not in mass
    finally:
        conn.close()


def test_missing_table_is_soft_noop():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        mass, diag = _walk(conn, ["s"], hops=2)
        assert mass == {}
        assert diag["mode"] == "absent"
    finally:
        conn.close()


def test_empty_seeds_is_noop():
    conn = _fresh_conn()
    try:
        mass, diag = _walk(conn, [], hops=2)
        assert mass == {}
    finally:
        conn.close()


# ═══ Task 3: config knobs, threaded end-to-end ═════════════════════════


def test_retrieval_config_cover_walk_defaults_inert():
    from cymatix_context.config import RetrievalConfig

    rc = RetrievalConfig()
    assert rc.cover_walk_enabled is False
    assert rc.cover_walk_seed_m == 12
    assert rc.cover_walk_hops == 2
    assert rc.cover_walk_gamma == 0.5
    assert rc.cover_walk_degree_cap == 1000
    assert rc.cover_walk_frontier_cap == 2000
    assert rc.cover_walk_append_slots == 3
    assert rc.cover_walk_append_min_mass == 0.0
    assert rc.cover_walk_band_weight == 1.0


def test_toml_threads_cover_walk_knobs(tmp_path):
    import textwrap

    from cymatix_context.config import load_config

    toml = tmp_path / "cymatix.toml"
    toml.write_text(textwrap.dedent("""
        [retrieval]
        cover_walk_enabled = true
        cover_walk_seed_m = 8
        cover_walk_hops = 1
        cover_walk_gamma = 0.7
        cover_walk_degree_cap = 500
        cover_walk_frontier_cap = 100
        cover_walk_append_slots = 2
        cover_walk_append_min_mass = 0.01
        cover_walk_band_weight = 0.5
    """), encoding="utf-8")
    r = load_config(str(toml)).retrieval
    assert r.cover_walk_enabled is True
    assert r.cover_walk_seed_m == 8
    assert r.cover_walk_hops == 1
    assert r.cover_walk_gamma == 0.7
    assert r.cover_walk_degree_cap == 500
    assert r.cover_walk_frontier_cap == 100
    assert r.cover_walk_append_slots == 2
    assert r.cover_walk_append_min_mass == 0.01
    assert r.cover_walk_band_weight == 0.5


@pytest.mark.parametrize("field,bad", [
    ("cover_walk_seed_m", 0),
    ("cover_walk_hops", 0),
    ("cover_walk_gamma", 0.0),
    ("cover_walk_gamma", 1.5),
    ("cover_walk_degree_cap", -1),
    ("cover_walk_frontier_cap", 0),
    ("cover_walk_append_slots", -1),
    ("cover_walk_append_min_mass", -0.1),
    ("cover_walk_band_weight", -1.0),
])
def test_cover_walk_knob_validation_fails_loud(field, bad):
    from cymatix_context.config import RetrievalConfig

    with pytest.raises(ValueError, match="cover_walk"):
        RetrievalConfig(**{field: bad})


def test_store_threads_cover_walk_kwargs():
    from cymatix_context.knowledge_store import KnowledgeStore

    ks = KnowledgeStore(
        path=":memory:", cover_walk_enabled=True, cover_walk_seed_m=5,
        cover_walk_hops=1, cover_walk_gamma=0.9, cover_walk_degree_cap=10,
        cover_walk_frontier_cap=50, cover_walk_append_slots=1,
        cover_walk_append_min_mass=0.5, cover_walk_band_weight=2.0,
    )
    try:
        assert ks._cover_walk_enabled is True
        assert ks._cover_walk_seed_m == 5
        assert ks._cover_walk_hops == 1
        assert ks._cover_walk_gamma == 0.9
        assert ks._cover_walk_degree_cap == 10
        assert ks._cover_walk_frontier_cap == 50
        assert ks._cover_walk_append_slots == 1
        assert ks._cover_walk_append_min_mass == 0.5
        assert ks._cover_walk_band_weight == 2.0
    finally:
        ks.close()


def test_store_cover_walk_kwarg_defaults_agree_with_config():
    import inspect

    from cymatix_context.config import RetrievalConfig
    from cymatix_context.knowledge_store import KnowledgeStore

    rc = RetrievalConfig()
    sig = inspect.signature(KnowledgeStore.__init__).parameters
    for name in (
        "cover_walk_enabled", "cover_walk_seed_m", "cover_walk_hops",
        "cover_walk_gamma", "cover_walk_degree_cap",
        "cover_walk_frontier_cap", "cover_walk_append_slots",
        "cover_walk_append_min_mass", "cover_walk_band_weight",
    ):
        assert sig[name].default == getattr(rc, name), name


def test_sharding_builder_threads_cover_walk():
    """The shared kwargs builder (sharding.py) must fan the knobs to solo
    and per-shard Genomes — the ladder flows through this builder."""
    import inspect

    from cymatix_context import sharding

    src = inspect.getsource(sharding)
    for name in (
        "cover_walk_enabled", "cover_walk_seed_m", "cover_walk_hops",
        "cover_walk_gamma", "cover_walk_degree_cap",
        "cover_walk_frontier_cap", "cover_walk_append_slots",
        "cover_walk_append_min_mass", "cover_walk_band_weight",
    ):
        assert f"{name}=retrieval.{name}" in src, name

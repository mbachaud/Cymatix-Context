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


# ═══ Task 4: query_docs wiring — append-below-head + eps-band class ════

# Store-level fixtures reuse the invariance-audit harness (single source
# of truth for corpora + the canonical 'alpha' query).
from tests.test_retrieval_invariance import _content, _doc, _fillers, _new_store, _run


def _cover_edges(g, rows) -> None:
    g.conn.executemany(
        "INSERT OR REPLACE INTO gene_relations "
        "(gene_id_a, gene_id_b, relation, confidence, updated_at) "
        "VALUES (?, ?, 5, ?, 0)",
        rows,
    )
    g.conn.commit()


def _rescue_corpus():
    """Two alpha-matching head docs + fillers + rescue_x, which matches the
    query in NO tier (unique tokens, unique domain) — the static-cohort
    analog: only a COVER edge can surface it.

    head_a fires multiple tiers (exact tag + fts rank 1); head_b fires fts
    ONLY (no alpha domain tag), putting its fused score far outside
    head_a's 5% eps band — at rrf_k=20 two ADJACENT ranks in the same tier
    are only ~4.5% apart (inside the band by construction), so an
    out-of-band fixture needs a tier-set gap, not a rank gap."""
    x_words = " ".join(f"omega{j}" for j in range(12))
    return [
        _doc("head_a", _content(3), ["w1", "w2", "w3", "alpha"]),
        _doc("head_b", _content(1), ["w4", "w5", "w6"]),
        _doc("rescue_x", x_words, ["omegadom"]),
    ] + _fillers(8)


def test_default_off_never_calls_walk(monkeypatch):
    import cymatix_context.retrieval.cover_walk as cw

    def _boom(*a, **k):  # pragma: no cover - would mean the walk ran
        raise AssertionError("cover_walk_mass called with knob off")

    monkeypatch.setattr(cw, "cover_walk_mass", _boom)
    g = _new_store(_rescue_corpus(), fusion_mode="rrf")
    try:
        _cover_edges(g, [("head_a", "rescue_x", 0.9)])
        ranked, _s, _c = _run(g)
        assert "rescue_x" not in ranked
        assert g.last_cover_walk_diag == {}
    finally:
        g.close()


def test_append_rescues_out_of_pool_doc():
    g = _new_store(
        _rescue_corpus(), fusion_mode="rrf", cover_walk_enabled=True,
    )
    try:
        _cover_edges(g, [("head_a", "rescue_x", 0.9)])
        ranked, _s, _c = _run(g)
        assert "rescue_x" in ranked  # delivered top-12 window
        assert "rescue_x" in g.last_ranked_ids[:12]
        assert g.last_cover_walk_diag["rescues"] == ["rescue_x"]
        assert g.last_cover_walk_diag["mode"] == "bidirectional"
    finally:
        g.close()


def test_displaced_doc_slides_below_not_dropped():
    # 13 alpha-matching docs fill the top-12; the rescue displaces exactly
    # one tail slot and the displaced doc survives below, not dropped.
    docs = [
        _doc(f"hit{i:02d}", _content(min(i % 5 + 1, 5)),
             [f"h{i}a", f"h{i}b", f"h{i}c", "alpha"])
        for i in range(13)
    ]
    x_words = " ".join(f"omega{j}" for j in range(12))
    docs.append(_doc("rescue_x", x_words, ["omegadom"]))
    g = _new_store(docs, fusion_mode="rrf", cover_walk_enabled=True)
    g_off = _new_store(docs, fusion_mode="rrf")
    try:
        for st in (g, g_off):
            st.conn.executemany(
                "INSERT OR REPLACE INTO gene_relations "
                "(gene_id_a, gene_id_b, relation, confidence, updated_at) "
                "VALUES (?, ?, 5, ?, 0)",
                [("hit00", "rescue_x", 0.9)],
            )
            st.conn.commit()
        _run(g)
        _run(g_off)
        on, off = list(g.last_ranked_ids), list(g_off.last_ranked_ids)
        assert "rescue_x" in on[:12] and "rescue_x" not in off
        # Everything the OFF arm ranked is still present ON — displaced
        # docs slide below the insertion point, they do not vanish.
        assert set(off) <= set(on)
        # Order above the insertion point is untouched.
        n_rescues = 1
        assert on[: 12 - g._cover_walk_append_slots] == off[: 12 - g._cover_walk_append_slots]
        assert g.last_cover_walk_diag["rescues"] == ["rescue_x"]
    finally:
        g.close()
        g_off.close()


def test_band_additive_only_under_eps_band():
    # Global combinator stays "additive" (no per-class map fired: the
    # harness query path passes no classifier override) -> the walk must
    # NOT touch the rerank maps; the append lane still fires.
    edges = [("head_a", "head_b", 0.9), ("head_a", "rescue_x", 0.8)]
    g = _new_store(
        _rescue_corpus(), fusion_mode="rrf", cover_walk_enabled=True,
        cover_walk_seed_m=1, rerank_combinator="additive",
    )
    try:
        _cover_edges(g, edges)
        _ranked, _s, contrib = _run(g)
        # No BAND contribution for eligible docs under the additive
        # combinator (the rerank maps stay untouched). The appended
        # rescue's observability tag is combinator-independent and allowed.
        assert "cover_walk" not in contrib.get("head_b", {})
        assert "cover_walk" not in contrib.get("head_a", {})
        assert "rescue_x" in g.last_ranked_ids[:12]  # append still on
        assert g.last_cover_walk_diag["band_boosted"] == 0
    finally:
        g.close()

    g2 = _new_store(
        _rescue_corpus(), fusion_mode="rrf", cover_walk_enabled=True,
        cover_walk_seed_m=1, rerank_combinator="eps_band",
    )
    try:
        _cover_edges(g2, edges)
        _ranked2, _s2, contrib2 = _run(g2)
        # head_b is eligible, outside the 1-doc seed set, and linked from
        # the seed -> it gets the banded cover_walk class.
        assert contrib2.get("head_b", {}).get("cover_walk", 0.0) > 0.0
        assert g2.last_cover_walk_diag["band_boosted"] >= 1
    finally:
        g2.close()


def test_band_mass_cannot_cross_bands():
    # Under eps_band, a clear fused loser with huge walk mass must not
    # outrank a clear fused winner: eligible relative order (rescues
    # excluded) is identical with the walk on and off.
    docs = _rescue_corpus()
    on = _new_store(
        docs, fusion_mode="rrf", cover_walk_enabled=True,
        cover_walk_seed_m=1, cover_walk_band_weight=1000.0,
        cover_walk_append_slots=0, rerank_combinator="eps_band",
    )
    off = _new_store(docs, fusion_mode="rrf", rerank_combinator="eps_band")
    try:
        for st in (on, off):
            st.conn.executemany(
                "INSERT OR REPLACE INTO gene_relations "
                "(gene_id_a, gene_id_b, relation, confidence, updated_at) "
                "VALUES (?, ?, 5, ?, 0)",
                [("head_a", "head_b", 0.9)],
            )
            st.conn.commit()
        _run(on)
        _run(off)
        # head_a leads by a clear fused margin (3 mentions + exact tag) —
        # head_b is out of its 5% band, so even a x1000 walk boost cannot
        # cross it.
        assert on.last_ranked_ids.index("head_a") < on.last_ranked_ids.index("head_b")
        assert list(on.last_ranked_ids) == list(off.last_ranked_ids)
    finally:
        on.close()
        off.close()


def test_min_mass_floor_blocks_weak_rescues():
    g = _new_store(
        _rescue_corpus(), fusion_mode="rrf", cover_walk_enabled=True,
        cover_walk_append_min_mass=999.0,
    )
    try:
        _cover_edges(g, [("head_a", "rescue_x", 0.9)])
        _run(g)
        assert "rescue_x" not in g.last_ranked_ids
        assert g.last_cover_walk_diag.get("rescues", []) == []
    finally:
        g.close()


def test_zero_slots_disables_append():
    g = _new_store(
        _rescue_corpus(), fusion_mode="rrf", cover_walk_enabled=True,
        cover_walk_append_slots=0,
    )
    try:
        _cover_edges(g, [("head_a", "rescue_x", 0.9)])
        _run(g)
        assert "rescue_x" not in g.last_ranked_ids
    finally:
        g.close()


def test_enabled_but_no_edges_is_clean_noop():
    g = _new_store(
        _rescue_corpus(), fusion_mode="rrf", cover_walk_enabled=True,
    )
    g_off = _new_store(_rescue_corpus(), fusion_mode="rrf")
    try:
        _run(g)
        _run(g_off)
        assert list(g.last_ranked_ids) == list(g_off.last_ranked_ids)
        assert g.last_cover_walk_diag["mode"] == "bidirectional"
        assert g.last_cover_walk_diag.get("rescues", []) == []
    finally:
        g.close()
        g_off.close()

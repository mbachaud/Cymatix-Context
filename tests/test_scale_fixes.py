"""Gates for the 2026-08-03 ERB-scale fixes (see
docs/benchmarks/2026-08-03-erb-step-curve.md).

Three fixes, each measured on the 829k-gene / 49GB ERB blob before landing:

1. stats() off the hot path — _compute_health/_build_abstain_window/proxy
   munge only ever used ``total_genes``, yet paid COUNT + chromatin GROUP BY
   + SUM(LENGTH(content)) + SUM(LENGTH(complement)) full scans: 11.1s warm /
   72.7s cold per request at 829k genes. New ``total_genes()`` accessor is a
   bare COUNT (29ms at 829k).
2. tag_prefix tier — planner chose SCAN genes → probe idx_promoter_gene →
   LIKE per tag (35-44s at 829k; ANALYZE does NOT fix it). Rewritten to
   drive prefix range scans on promoter_index, plus a covering
   (tag_value, gene_id) index.
3. SPLADE tier — 147M-row splade_terms with a non-covering index: 43s at
   829k; covering (term, gene_id, weight) index measured 40× (end-to-end
   receipt erb_step_full_c1_covidx.json). Plus a query-side DF cap
   (splade_df_cap_fraction, #327 tag_df_cap pattern): one real query vector
   touched 5.06M posting rows dominated by terms present in >70% of docs.
"""

from __future__ import annotations

import inspect
import sqlite3

import pytest

from cymatix_context.backends import splade_backend
from cymatix_context.context_manager import CymatixContextManager
from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.schemas import ChromatinState

from tests.conftest import make_gene


# ── Fix 1: stats() off the hot path ─────────────────────────────────


def test_total_genes_matches_stats(tmp_path):
    store = KnowledgeStore(str(tmp_path / "g.db"))
    for i in range(3):
        store.upsert_doc(make_gene(content=f"doc {i}", domains=["alpha"]))
    assert store.total_genes() == 3
    assert store.total_genes() == store.stats()["total_genes"]


def test_total_genes_sees_committed_writes(tmp_path):
    store = KnowledgeStore(str(tmp_path / "g.db"))
    assert store.total_genes() == 0
    store.upsert_doc(make_gene(content="late arrival", domains=["alpha"]))
    assert store.total_genes() == 1


def test_hot_path_does_not_call_full_stats():
    """The request path must never pay stats()'s SUM(LENGTH()) scans.

    Source-level gate (same style as the write-lock coverage meta-test):
    the three request-path call sites use total_genes(), not stats().
    """
    for fn in (
        CymatixContextManager._compute_health,
        CymatixContextManager._build_abstain_window,
    ):
        src = inspect.getsource(fn)
        assert "genome.stats()" not in src, (
            f"{fn.__name__} still calls genome.stats() on the hot path"
        )
        assert "total_genes()" in src


def test_proxy_munge_path_does_not_call_full_stats():
    from cymatix_context.server import routes_context

    src = inspect.getsource(routes_context)
    assert "genome.stats()[" not in src, (
        "routes_context still pays full stats() per proxied request"
    )


# ── Fix 2: tag_prefix tier ───────────────────────────────────────────


def test_promoter_covering_index_created(tmp_path):
    store = KnowledgeStore(str(tmp_path / "g.db"))
    names = {
        r[0]
        for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert "idx_promoter_cover" in names


def test_tag_prefix_semantics_pinned(tmp_path):
    """Equivalence gate for the SQL rewrite: match counts, chromatin
    exclusion, and weighting must be identical to the legacy tier.

    gene_a: two tags matching 'server%' -> match_count 2
    gene_b: one tag matching 'conf%'    -> match_count 1
    gene_c: no prefix match             -> no tag_prefix contribution
    gene_d: matching tag but HETEROCHROMATIN -> excluded entirely
    """
    store = KnowledgeStore(str(tmp_path / "g.db"))
    a = make_gene(content="a", domains=["serverconfig", "server_api"])
    b = make_gene(content="b", domains=["confluence"])
    c = make_gene(content="c", domains=["unrelated"])
    d = make_gene(
        content="d",
        domains=["serverfarm"],
        chromatin=ChromatinState.HETEROCHROMATIN,
    )
    for g in (a, b, c, d):
        store.upsert_doc(g)

    store.query_docs(domains=["server", "conf"], entities=[], max_genes=8)
    contrib = store.last_tier_contributions

    assert contrib[a.gene_id]["tag_prefix"] == pytest.approx(2 * 1.5)
    assert contrib[b.gene_id]["tag_prefix"] == pytest.approx(1 * 1.5)
    assert "tag_prefix" not in contrib.get(c.gene_id, {})
    assert "tag_prefix" not in contrib.get(d.gene_id, {})


def test_tag_prefix_drives_promoter_index(tmp_path):
    """Plan gate: the rewritten tier must not SCAN the genes table.

    The 829k-gene plan was `SCAN g` + per-gene probes of
    idx_promoter_gene — 35-44s. The rewrite drives prefix range scans
    from the promoter side.
    """
    store = KnowledgeStore(str(tmp_path / "g.db"))
    store.upsert_doc(make_gene(content="a", domains=["serverconfig"]))
    sql, params = store._tag_prefix_sql(["server", "conf"])
    plan = " | ".join(
        r[3] for r in store.conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
    )
    assert "SCAN g" not in plan and "SCAN genes" not in plan, plan
    # LIKE disables index range use on BINARY-collation indexes
    # (case_sensitive_like=OFF) — the branch degraded to a full covering-
    # index SCAN, 6.2s at 100k genes. Require true range SEARCHes.
    assert "SCAN promoter_index" not in plan, plan
    assert "SEARCH promoter_index" in plan and "tag_value>" in plan, plan


# ── Fix 3: SPLADE covering index + DF cap ────────────────────────────


def test_splade_covering_index_created():
    conn = sqlite3.connect(":memory:")
    splade_backend.create_splade_table(conn)
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_splade_term_cover" in names


def _seed_splade(conn, n_genes: int = 10):
    splade_backend.create_splade_table(conn)
    # 'common' appears in 9/10 genes, 'rare' in exactly one. Seed through
    # upsert_splade_terms so the splade_term_df counters are maintained —
    # the DF cap reads them (empty counters = cap inactive = legacy, the
    # safe mode for old beds that predate the table).
    for i in range(n_genes - 1):
        splade_backend.upsert_splade_terms(conn, f"g{i}", {"common": 1.0})
    splade_backend.upsert_splade_terms(conn, "g9", {"rare": 1.0})
    conn.commit()


def test_upsert_maintains_term_df():
    conn = sqlite3.connect(":memory:")
    splade_backend.create_splade_table(conn)
    splade_backend.upsert_splade_terms(conn, "g0", {"alpha": 1.0, "beta": 0.5})
    splade_backend.upsert_splade_terms(conn, "g1", {"alpha": 0.7})
    # re-upsert of the same gene must not double-count
    splade_backend.upsert_splade_terms(conn, "g0", {"alpha": 0.9})
    dfs = dict(conn.execute("SELECT term, df FROM splade_term_df"))
    assert dfs["alpha"] == 2
    assert dfs.get("beta", 0) == 0


def test_query_splade_df_cap_skips_flood_terms():
    conn = sqlite3.connect(":memory:")
    _seed_splade(conn)
    hits = splade_backend.query_splade(
        conn,
        {"common": 1.0, "rare": 1.0},
        limit=20,
        max_df_fraction=0.5,
        total_docs=10,
    )
    ids = {gid for gid, _ in hits}
    assert ids == {"g9"}, (
        "df cap 0.5 on 10 docs must drop 'common' (df=9) and keep 'rare'"
    )


def test_query_splade_df_cap_off_is_legacy():
    conn = sqlite3.connect(":memory:")
    _seed_splade(conn)
    hits = splade_backend.query_splade(
        conn, {"common": 1.0, "rare": 1.0}, limit=20,
        max_df_fraction=0.0, total_docs=10,
    )
    assert {gid for gid, _ in hits} == {f"g{i}" for i in range(10)}


def test_query_splade_cap_never_empties_results():
    """If EVERY query term is over the cap, the cap must disable itself
    rather than return nothing — an over-aggressive cap must not zero a
    tier."""
    conn = sqlite3.connect(":memory:")
    _seed_splade(conn)
    hits = splade_backend.query_splade(
        conn, {"common": 1.0}, limit=20,
        max_df_fraction=0.5, total_docs=10,
    )
    assert {gid for gid, _ in hits} == {f"g{i}" for i in range(9)}


def test_config_splade_df_cap_fraction_default_and_load(tmp_path):
    from cymatix_context.config import load_config

    p = tmp_path / "cymatix.toml"
    p.write_text("[retrieval]\nsplade_df_cap_fraction = 0.25\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.retrieval.splade_df_cap_fraction == 0.25

    p2 = tmp_path / "empty.toml"
    p2.write_text("", encoding="utf-8")
    cfg2 = load_config(str(p2))
    assert cfg2.retrieval.splade_df_cap_fraction == 0.0

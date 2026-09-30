"""Issue #431: the Tier 5 harmonic boost must not silently fail over
``SQLITE_LIMIT_VARIABLE_NUMBER``.

The tier binds the whole pre-shortlist candidate pool TWICE into
``gene_id_a IN (...) AND gene_id_b IN (...)``. On bulk beds that pool is far
larger than the bind cap (ERB 947k receipt: median ~172k candidates against
32,766), SQLite raises ``OperationalError: too many SQL variables``, and the
pre-fix bare ``except Exception: log.debug(...)`` swallowed it — the tier
never fired on any large bed and nobody was told.

Logging-only fix (this file's contract):

1. Below the probed limit the tier runs exactly as before — ranking and
   per-tier contributions are byte-identical (``test_below_limit_*``).
2. Over the limit the query is skipped and ONE ``log.warning`` fires per
   store instance, naming the candidate count and the limit
   (``test_over_limit_*``).
3. Any other failure in the tier now logs at WARNING with the traceback,
   not DEBUG (``test_other_failure_*``).

The opt-in batching path stages the entire pool and preserves cross-batch
links. Shipped defaults retain the warning until benchmark gates pass.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from cymatix_context import knowledge_store as ks_mod
from cymatix_context.genome import Genome
from cymatix_context.schemas import (
    ChromatinState, EpigeneticMarkers, Gene, PromoterTags,
)

LOGGER = "cymatix_context.knowledge_store"
QUERY_DOMAINS = ["alpha"]


def _gene(gid: str, content: str, domains) -> Gene:
    return Gene(
        gene_id=gid,
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=list(domains), entities=[]),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
    )


def make_store(**kwargs) -> Genome:
    """Three tag-matched candidates, one harmonic edge (gA <-> gB).

    Only the exact-tag tier and Tier 5 fire — no SPLADE / SEMA / dense — so
    the per-tier contributions are exact small floats with no
    reduction-order ambiguity.
    """
    g = Genome(path=kwargs.pop("path", ":memory:"), **kwargs)
    for gid, text in (
        ("gA", "alpha configures the parser pipeline and retry policy."),
        ("gB", "alpha owns the splice budget for the merge stage."),
        ("gC", "alpha gates the compaction scheduler thresholds."),
    ):
        g.upsert_gene(_gene(gid, text, ["alpha"]), apply_gate=False)
    g.conn.execute(
        "INSERT INTO harmonic_links "
        "(gene_id_a, gene_id_b, weight, updated_at, source) "
        "VALUES ('gA', 'gB', 1.0, 0.0, 'co_retrieved')"
    )
    g.conn.commit()
    return g


def run_query(g: Genome, domains=QUERY_DOMAINS):
    genes = g.query_genes(
        domains=list(domains), entities=[], max_genes=8, read_only=True,
    )
    ranked = [x.gene_id for x in genes]
    scores = dict(g.last_query_scores)
    contrib = {gid: dict(t) for gid, t in g.last_tier_contributions.items()}
    return ranked, scores, contrib


def _add_batching_edges(g):
    """Five candidates plus outsiders at either endpoint of an edge."""
    for gid, domains in (("gD", ["alpha"]), ("gE", ["alpha"]), ("outside", ["elsewhere"])):
        g.upsert_gene(_gene(gid, f"Distinct document {gid}.", domains), apply_gate=False)
    g.conn.executemany(
        "INSERT INTO harmonic_links "
        "(gene_id_a, gene_id_b, weight, updated_at, source) "
        "VALUES (?, ?, 17.0, 0.0, 'co_retrieved')",
        [("gA", "gE"), ("outside", "gB"), ("gC", "outside")],
    )
    g.conn.commit()


@pytest.mark.parametrize("fusion_mode", ["additive", "rrf"])
@pytest.mark.parametrize("weight", [0.0, 0.1, 1.0])
def test_batching_above_real_limit_preserves_full_pool_scores(fusion_mode, weight, caplog):
    """Splitting both IN lists independently loses gA--gE; OR admits outsiders."""
    g = make_store(fusion_mode=fusion_mode, harmonic_weight=weight)
    try:
        _add_batching_edges(g)
        expected = run_query(g)
        g._harmonic_batching_enabled = True
        g.read_conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)
        got = run_query(g)
        assert got == expected
        assert got[2]["gA"]["harmonic"] == weight * 2
        assert got[2]["gB"]["harmonic"] == weight
        assert got[2]["gE"]["harmonic"] == weight
        assert "harmonic" not in got[2]["gC"]
        assert "outside" not in got[1]
        assert not _warnings(caplog, "Harmonic")
    finally:
        g.close()


def test_batching_below_limit_is_identical():
    g = make_store()
    try:
        expected = run_query(g)
        g._harmonic_batching_enabled = True
        assert run_query(g) == expected
    finally:
        g.close()


def test_batching_repeated_queries_discard_old_candidate_ids():
    g = make_store()
    try:
        _add_batching_edges(g)
        for gid in ("gC", "gF", "gG", "gH", "gI"):
            domains = ["alpha", "beta"] if gid == "gC" else ["beta"]
            g.upsert_gene(_gene(gid, f"Distinct document {gid}.", domains), apply_gate=False)
        g.conn.executemany(
            "INSERT INTO harmonic_links "
            "(gene_id_a, gene_id_b, weight, updated_at, source) "
            "VALUES (?, ?, 1.0, 0.0, 'co_retrieved')",
            [("gC", "gA"), ("gC", "gI")],
        )
        g.conn.commit()
        expected = run_query(g, ["beta"])
        g._harmonic_batching_enabled = True
        g.read_conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)
        run_query(g)
        got = run_query(g, ["beta"])
        assert got == expected
        assert got[2]["gC"]["harmonic"] == 1.0
        assert got[2]["gI"]["harmonic"] == 1.0
        assert not g.read_conn.in_transaction
        assert g.read_conn.execute("SELECT name FROM sqlite_temp_master").fetchall() == []
    finally:
        g.close()


def test_batching_uses_readonly_reader_without_main_database_writes(tmp_path):
    g = make_store(path=str(tmp_path / "harmonic.db"))
    try:
        _add_batching_edges(g)
        expected = run_query(g)
        reader = g.read_conn
        assert reader is not g.conn
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("CREATE TABLE forbidden (id TEXT)")
        g._harmonic_batching_enabled = True
        reader.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)
        assert run_query(g) == expected
        assert not reader.in_transaction
        assert reader.execute("SELECT name FROM sqlite_temp_master").fetchall() == []
    finally:
        g.close()


def test_batching_failure_cleans_temp_state_and_next_query_recovers(caplog):
    g = make_store()
    try:
        _add_batching_edges(g)
        expected = run_query(g)
        g._harmonic_batching_enabled = True
        g.read_conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)

        def deny_harmonic_read(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ and table == "harmonic_links":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        g.read_conn.set_authorizer(deny_harmonic_read)
        failed = run_query(g)
        g.read_conn.set_authorizer(None)
        assert set(failed[0]) == {"gA", "gB", "gC", "gD", "gE"}
        assert not any("harmonic" in c for c in failed[2].values())
        assert len(_warnings(caplog, "Harmonic boost failed")) == 1
        assert not g.read_conn.in_transaction
        assert g.read_conn.execute("SELECT name FROM sqlite_temp_master").fetchall() == []
        assert run_query(g) == expected
    finally:
        g.conn.set_authorizer(None)
        g.close()


def test_batching_preserves_callers_pending_transaction():
    g = make_store()
    try:
        _add_batching_edges(g)
        g.conn.execute("CREATE TABLE pending_work (id INTEGER)")
        g.conn.execute("INSERT INTO pending_work VALUES (1)")
        g.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 1)
        # Exercise staging directly: other pre-existing retrieval tiers can
        # commit the writer, independently of this helper's transaction scope.
        with g._harmonic_candidate_rows(
            g.conn.cursor(), ["gA", "gB", "gC", "gD", "gE"], batched=True,
        ) as rows:
            assert {(row[0], row[1]) for row in rows} == {("gA", "gB"), ("gA", "gE")}
        assert g.conn.in_transaction
        g.conn.rollback()
        assert g.conn.execute("SELECT * FROM pending_work").fetchall() == []
    finally:
        g.close()


def test_batching_many_insertions_and_interrupted_iteration_are_clean():
    g = make_store(harmonic_batching_enabled=True)
    try:
        # More than the project's usual IN batch size; endpoints are placed
        # at opposite ends so batching candidate pairs separately fails.
        candidates = ["gA", *(f"candidate-{i}" for i in range(1200)), "gB"]
        g.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 1)
        with pytest.raises(RuntimeError, match="interrupted"):
            with g._harmonic_candidate_rows(g.conn.cursor(), candidates, batched=True) as rows:
                assert tuple(next(rows)) == ("gA", "gB", 1.0)
                raise RuntimeError("interrupted consumer")
        assert not g.conn.in_transaction
        assert g.conn.execute("SELECT name FROM sqlite_temp_master").fetchall() == []
        with g._harmonic_candidate_rows(g.conn.cursor(), candidates, batched=True) as rows:
            assert [tuple(row) for row in rows] == [("gA", "gB", 1.0)]
    finally:
        g.close()


@pytest.mark.parametrize("shared_reader", [False, True])
def test_batching_shared_connections_isolate_concurrent_pools(tmp_path, shared_reader):
    from cymatix_context.persistence import ReplicationManager

    path = str(tmp_path / "shared.db") if shared_reader else ":memory:"
    g = make_store(path=path, harmonic_batching_enabled=True)
    manager = None
    try:
        _add_batching_edges(g)
        if shared_reader:
            manager = ReplicationManager(master=path)
            g.set_replication_manager(manager)
        conn = g.read_conn
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 1)
        barrier = threading.Barrier(2)

        def query_pool(ids, expected):
            barrier.wait(timeout=5)
            for _ in range(20):
                with g._harmonic_candidate_rows(conn.cursor(), ids, batched=True) as rows:
                    assert {(row[0], row[1]) for row in rows} == expected

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(query_pool, ["gA", "gB"], {("gA", "gB")}),
                executor.submit(query_pool, ["gC", "outside"], {("gC", "outside")}),
            ]
            for future in futures:
                future.result(timeout=10)
        assert not conn.in_transaction
        assert conn.execute("SELECT name FROM sqlite_temp_master").fetchall() == []
    finally:
        if manager is not None:
            manager.close()
        g.close()


def test_batching_keeps_three_link_cap():
    g = make_store(harmonic_batching_enabled=True, harmonic_weight=0.1)
    try:
        _add_batching_edges(g)
        g.conn.executemany(
            "INSERT INTO harmonic_links "
            "(gene_id_a, gene_id_b, weight, updated_at, source) "
            "VALUES (?, ?, 1.0, 0.0, 'co_retrieved')",
            [("gA", "gC"), ("gA", "gD")],
        )
        g.conn.commit()
        expected = run_query(g)
        g.read_conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)
        got = run_query(g)
        assert got == expected
        assert got[2]["gA"]["harmonic"] == 0.30000000000000004
    finally:
        g.close()


def _warnings(caplog, needle: str):
    return [
        r for r in caplog.records
        if r.name == LOGGER and r.levelno == logging.WARNING
        and needle in r.getMessage()
    ]


# --- premise: the bind cap is real and the probe reports it ----------


def test_probe_matches_connection_getlimit_and_cap_is_enforced():
    conn = sqlite3.connect(":memory:")
    try:
        limit = ks_mod._sqlite_variable_limit(conn)
        assert limit == conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        assert limit >= 999
        # One over the cap raises the exact error the bare except hid.
        # Same IN-list shape as the harmonic statement.
        ph = ",".join("?" * (limit + 1))
        with pytest.raises(sqlite3.OperationalError, match="too many SQL"):
            conn.execute(f"SELECT 1 WHERE 0 IN ({ph})", tuple(range(limit + 1)))
        # At the cap the statement is legal.
        ph = ",".join("?" * limit)
        row = conn.execute(
            f"SELECT 1 WHERE 0 IN ({ph})", tuple(range(limit)),
        ).fetchone()
        assert row == (1,)
    finally:
        conn.close()


def test_probe_falls_back_to_999_without_getlimit():
    class NoGetLimit:  # Python < 3.11 connection shape
        pass

    assert ks_mod._sqlite_variable_limit(NoGetLimit()) == 999
    assert ks_mod._sqlite_variable_limit(None) == 999


# --- 1. below the limit: byte-identical, tier fires, nothing logged ---


def test_below_limit_tier_fires_and_ranking_is_byte_identical(
    monkeypatch, caplog,
):
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    # Reference run: the guard can never trip (limit = "infinite"), which is
    # observationally the pre-#431 path — the query, bonus arithmetic and
    # fuser.add_tier call are the same statements.
    g_ref = make_store()
    try:
        monkeypatch.setattr(
            ks_mod, "_sqlite_variable_limit", lambda conn: 10 ** 9,
        )
        ref = run_query(g_ref)
    finally:
        g_ref.close()
    monkeypatch.undo()

    # Real run: the probe reads the live connection's cap.
    g = make_store()
    try:
        got = run_query(g)
        assert g._harmonic_limit_warned is False
    finally:
        g.close()

    ranked, scores, contrib = got
    assert ranked == ref[0]
    assert scores == ref[1]      # dict equality == bit-for-bit floats
    assert contrib == ref[2]
    # Tier 5 actually fired on the linked pair and only on it.
    assert contrib["gA"]["harmonic"] == 1.0
    assert contrib["gB"]["harmonic"] == 1.0
    assert "harmonic" not in contrib["gC"]
    assert not _warnings(caplog, "Harmonic")


# --- 2. over the limit: skip + exactly one warning per store ---------


def test_over_limit_skips_tier_and_warns_once_per_store(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    # 3 candidates bind 6 parameters; a cap of 5 must trip the guard.
    monkeypatch.setattr(ks_mod, "_sqlite_variable_limit", lambda conn: 5)

    g = make_store()
    try:
        for _ in range(2):
            ranked, _scores, contrib = run_query(g)
            assert set(ranked) == {"gA", "gB", "gC"}  # other tiers intact
            assert not any("harmonic" in t for t in contrib.values())
        assert g._harmonic_limit_warned is True
    finally:
        g.close()

    skipped = _warnings(caplog, "Harmonic tier skipped")
    assert len(skipped) == 1, [r.getMessage() for r in skipped]
    msg = skipped[0].getMessage()
    assert "3 candidates" in msg
    assert "bind 6 parameters" in msg
    assert "SQLITE_LIMIT_VARIABLE_NUMBER=5" in msg
    assert "#431" in msg
    # The skip is a decision, not a failure — no swallowed-exception log.
    assert not _warnings(caplog, "Harmonic boost failed")
    assert not any(
        r.name == LOGGER and r.levelno < logging.WARNING
        and "Harmonic boost failed" in r.getMessage()
        for r in caplog.records
    )

    # A second store instance is its own warning scope.
    caplog.clear()
    g2 = make_store()
    try:
        run_query(g2)
        run_query(g2)
    finally:
        g2.close()
    assert len(_warnings(caplog, "Harmonic tier skipped")) == 1


def test_at_limit_still_runs(monkeypatch, caplog):
    """The guard is strictly greater-than: 2*N == limit is legal SQL."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    monkeypatch.setattr(ks_mod, "_sqlite_variable_limit", lambda conn: 6)
    g = make_store()
    try:
        _ranked, _scores, contrib = run_query(g)
    finally:
        g.close()
    assert contrib["gA"]["harmonic"] == 1.0
    assert not _warnings(caplog, "Harmonic")


# --- 3. any other failure logs at WARNING, with the traceback --------


def test_other_failure_logs_at_warning_not_debug(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    def boom(conn):
        raise RuntimeError("synthetic tier-5 failure")

    monkeypatch.setattr(ks_mod, "_sqlite_variable_limit", boom)
    g = make_store()
    try:
        ranked, _scores, contrib = run_query(g)
    finally:
        g.close()

    # Retrieval survives the tier failure (the except is still a guard) ...
    assert set(ranked) == {"gA", "gB", "gC"}
    assert not any("harmonic" in t for t in contrib.values())
    # ... but it is no longer silent.
    failed = _warnings(caplog, "Harmonic boost failed")
    assert len(failed) == 1
    assert failed[0].exc_info is not None
    assert failed[0].exc_info[0] is RuntimeError
    assert not any(
        r.name == LOGGER and r.levelno == logging.DEBUG
        and "Harmonic boost failed" in r.getMessage()
        for r in caplog.records
    )

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

The behavior-changing variant (batching / post-shortlist bounding so the
tier can fire on large populated beds) is receipt-gated and out of scope.
"""
from __future__ import annotations

import logging
import sqlite3

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


def make_store() -> Genome:
    """Three tag-matched candidates, one harmonic edge (gA <-> gB).

    Only the exact-tag tier and Tier 5 fire — no SPLADE / SEMA / dense — so
    the per-tier contributions are exact small floats with no
    reduction-order ambiguity.
    """
    g = Genome(path=":memory:")
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


def run_query(g: Genome):
    genes = g.query_genes(
        domains=list(QUERY_DOMAINS), entities=[], max_genes=8, read_only=True,
    )
    ranked = [x.gene_id for x in genes]
    scores = dict(g.last_query_scores)
    contrib = {gid: dict(t) for gid, t in g.last_tier_contributions.items()}
    return ranked, scores, contrib


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

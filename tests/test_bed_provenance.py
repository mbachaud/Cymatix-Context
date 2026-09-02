"""Tests for the bed provenance log (``cymatix_context.storage.provenance``)."""

from __future__ import annotations

import sqlite3

import pytest

from cymatix_context.storage import ddl, provenance


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "bed.db"))
    c.row_factory = sqlite3.Row
    ddl.init_db(c)
    yield c
    c.close()


def _add_genes(c, *ids):
    for gid in ids:
        c.execute("INSERT INTO genes (gene_id, content) VALUES (?, ?)", (gid, gid))
    c.commit()


class _Section:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Config:
    def __init__(self, **sections):
        self.__dict__.update(sections)


def _config(**ingestion_kw):
    base = dict(
        backend="cpu",
        splade_enabled=False,
        dense_embed_on_ingest=False,
        sema_embed_on_ingest=False,
        entity_graph=True,
        entity_autolink_hub_cutoff=200,
        symbol_graph=False,
        splade_content_cap=1000,
        dense_passage_char_cap=2000,
        locale_demotion_enabled=True,
        citation_path_anchors=["sources", "Projects"],
    )
    base.update(ingestion_kw)
    return _Config(
        ingestion=_Section(**base),
        cymatics=_Section(harmonic_links=True),
        genome=_Section(synchronous="NORMAL"),
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_init_db_creates_the_table(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "bed_provenance" in names


def test_create_table_is_idempotent(conn):
    cur = conn.cursor()
    provenance.create_table(cur)
    provenance.create_table(cur)
    assert provenance.read_events(conn) == []


def test_unstamped_bed_reads_as_empty(tmp_path):
    """A bed with no provenance table must read as empty, not raise."""
    c = sqlite3.connect(str(tmp_path / "bare.db"))
    try:
        assert provenance.read_events(c) == []
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Write / read
# ---------------------------------------------------------------------------

def test_record_event_roundtrip(conn):
    _add_genes(conn, "g1", "g2")
    rid = provenance.record_event(
        conn, provenance.EVENT_BUILD, config=_config(), ingest_c=1,
        corpus_id="erb500k", notes="build", with_identity=True,
    )
    assert rid is not None

    (ev,) = provenance.read_events(conn)
    assert ev["event"] == "build"
    assert ev["ingest_c"] == 1
    assert ev["corpus_id"] == "erb500k"
    assert ev["gene_count"] == 2
    assert ev["provenance_version"] == provenance.PROVENANCE_VERSION
    assert ev["cymatix_version"]
    assert ev["identity_sha256"] and len(ev["identity_sha256"]) == 64
    assert ev["config"]["ingestion.backend"] == "cpu"


def test_events_are_append_only_and_ordered(conn):
    for i in range(3):
        provenance.record_event(conn, provenance.EVENT_INGEST, notes=f"run{i}")
    events = provenance.read_events(conn)
    assert [e["notes"] for e in events] == ["run0", "run1", "run2"]
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)


def test_identity_is_opt_in(conn):
    _add_genes(conn, "g1")
    provenance.record_event(conn, provenance.EVENT_INGEST)
    (ev,) = provenance.read_events(conn)
    assert ev["identity_sha256"] is None


def test_read_events_respects_limit(conn):
    for i in range(5):
        provenance.record_event(conn, provenance.EVENT_INGEST, notes=str(i))
    assert len(provenance.read_events(conn, limit=2)) == 2


# ---------------------------------------------------------------------------
# Identity digest
# ---------------------------------------------------------------------------

def test_identity_is_stable_and_order_independent(tmp_path):
    def build(name, ids):
        c = sqlite3.connect(str(tmp_path / name))
        c.row_factory = sqlite3.Row
        ddl.init_db(c)
        _add_genes(c, *ids)
        return c

    a = build("a.db", ["g1", "g2", "g3"])
    b = build("b.db", ["g3", "g1", "g2"])  # same set, different insert order
    try:
        assert provenance.identity_sha256(a) == provenance.identity_sha256(b)
    finally:
        a.close()
        b.close()


def test_identity_changes_with_content(conn):
    _add_genes(conn, "g1")
    before = provenance.identity_sha256(conn)
    _add_genes(conn, "g2")
    assert provenance.identity_sha256(conn) != before


# ---------------------------------------------------------------------------
# Drift — the point of the whole table
# ---------------------------------------------------------------------------

def test_config_drift_names_the_changed_knob(conn):
    provenance.record_event(conn, provenance.EVENT_BUILD, config=_config())
    provenance.record_event(
        conn, provenance.EVENT_INGEST, config=_config(entity_graph=False)
    )
    (drift,) = provenance.config_drift(provenance.read_events(conn))
    assert drift["changed"] == {
        "ingestion.entity_graph": {"from": True, "to": False}
    }


def test_config_drift_is_empty_when_knobs_match(conn):
    provenance.record_event(conn, provenance.EVENT_BUILD, config=_config())
    provenance.record_event(conn, provenance.EVENT_INGEST, config=_config())
    assert provenance.config_drift(provenance.read_events(conn)) == []


def test_config_drift_skips_unconfigured_events(conn):
    """Events stamped without a config must not fabricate a drift entry."""
    provenance.record_event(conn, provenance.EVENT_BUILD, config=_config())
    provenance.record_event(conn, provenance.EVENT_STAMP)  # no config
    provenance.record_event(conn, provenance.EVENT_INGEST, config=_config())
    assert provenance.config_drift(provenance.read_events(conn)) == []


def test_snapshot_records_missing_knobs_as_none():
    """A knob absent from the config is recorded, not silently dropped."""
    snap = provenance.ingest_config_snapshot(_Config(ingestion=_Section(backend="cpu")))
    assert snap["ingestion.backend"] == "cpu"
    assert snap["ingestion.entity_graph"] is None
    assert set(snap) == {f"{s}.{k}" for s, k in provenance._INGEST_KNOBS}


def test_fully_specified_config_snapshots_every_knob():
    """``_config()`` must cover every knob in ``_INGEST_KNOBS``.

    Pins the helper to the module: a knob added to ``_INGEST_KNOBS``
    without a value in ``_config()`` shows up here as a ``None``, which
    is what a real config drifting behind the list would look like too.
    """
    snap = provenance.ingest_config_snapshot(_config())
    missing = sorted(k for k, v in snap.items() if v is None)
    assert missing == [], f"_config() does not set: {missing}"
    assert snap["ingestion.entity_autolink_hub_cutoff"] == 200


def test_config_drift_names_the_hub_cutoff():
    """#411's hub cutoff changes gene_relations edges -> it must drift."""
    events = [
        {"config": provenance.ingest_config_snapshot(_config())},
        {"config": provenance.ingest_config_snapshot(
            _config(entity_autolink_hub_cutoff=0))},
    ]
    (drift,) = provenance.config_drift(events)
    assert drift["changed"] == {
        "ingestion.entity_autolink_hub_cutoff": {"from": 200, "to": 0}
    }


# ---------------------------------------------------------------------------
# Failure containment — provenance must never break the operation it describes
# ---------------------------------------------------------------------------

def test_record_event_returns_none_on_a_broken_connection(tmp_path):
    c = sqlite3.connect(str(tmp_path / "closed.db"))
    c.close()
    assert provenance.record_event(c, provenance.EVENT_INGEST) is None


def test_counts_degrade_to_none_without_tables(tmp_path):
    c = sqlite3.connect(str(tmp_path / "empty.db"))
    try:
        assert provenance.gene_count(c) is None
        assert provenance.fts_count(c) is None
        assert provenance.identity_sha256(c) is None
    finally:
        c.close()

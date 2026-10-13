"""Background auto-build of idx_genes_id_chromatin on writable store open.

Owner choice (option C): nobody waits on open, everybody gets the speedup.
A daemon thread with its OWN connection builds the covering index; foreground
writers/readers are never blocked beyond SQLite's busy_timeout.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.storage import ddl

from tests.test_tier_sql_rank_identical import _make

IDX = "idx_genes_id_chromatin"


def _has_index(db: Path) -> bool:
    c = sqlite3.connect(str(db))
    try:
        return c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (IDX,)
        ).fetchone() is not None
    finally:
        c.close()


def _wait(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture(autouse=True)
def _low_threshold(monkeypatch):
    monkeypatch.setattr(ddl, "COVER_INDEX_HINT_MIN_ROWS", 5)
    monkeypatch.delenv("CYMATIX_COVER_INDEX_AUTOBUILD", raising=False)


def _seed(tmp_path):
    s = _make(tmp_path)
    s.conn.commit()
    s.close()
    return tmp_path / "g.db"


def _logs(caplog, marker):
    return [r for r in caplog.records if marker in r.getMessage()]


def test_background_build_on_writable_open(tmp_path):
    db = _seed(tmp_path)
    assert not _has_index(db)
    store = KnowledgeStore(str(db))
    try:
        assert _wait(lambda: _has_index(db))
    finally:
        store.close()


def test_not_started_below_threshold(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    monkeypatch.setattr(ddl, "COVER_INDEX_HINT_MIN_ROWS", 10_000)
    store = KnowledgeStore(str(db))
    try:
        assert store._cover_index_builder is None
        time.sleep(0.2)
        assert not _has_index(db)
    finally:
        store.close()


def test_not_started_read_only(tmp_path):
    db = _seed(tmp_path)
    store = KnowledgeStore(str(db), read_only=True)
    try:
        assert store._cover_index_builder is None
        time.sleep(0.2)
        assert not _has_index(db)
    finally:
        store.close()


def test_not_started_memory():
    store = KnowledgeStore(":memory:")
    assert store._cover_index_builder is None
    store.close()


def test_disabled_by_kwarg(tmp_path):
    db = _seed(tmp_path)
    store = KnowledgeStore(str(db), cover_index_autobuild=False)
    try:
        assert store._cover_index_builder is None
        time.sleep(0.2)
        assert not _has_index(db)
    finally:
        store.close()


def test_disabled_by_env(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    monkeypatch.setenv("CYMATIX_COVER_INDEX_AUTOBUILD", "0")
    store = KnowledgeStore(str(db))
    try:
        assert store._cover_index_builder is None
        time.sleep(0.2)
        assert not _has_index(db)
    finally:
        store.close()


def test_config_knob_plumbed():
    from cymatix_context.config import GenomeConfig, load_config
    assert GenomeConfig().cover_index_autobuild is True


def test_single_flight_and_second_instance_noop(tmp_path, caplog):
    db = _seed(tmp_path)
    caplog.set_level(logging.INFO)
    gate = threading.Event()
    real = ddl._run_cover_index_build
    calls = []

    def slow(*a, **kw):
        calls.append(1)
        gate.wait(5)
        return real(*a, **kw)

    ddl._run_cover_index_build = slow
    try:
        s1 = KnowledgeStore(str(db))
        s2 = KnowledgeStore(str(db))
        assert s1._cover_index_builder is not None
        assert s2._cover_index_builder is None  # one build per path/process
        gate.set()
        assert _wait(lambda: _has_index(db))
        assert _wait(lambda: len(_logs(caplog, "finished")) >= 1)
        s3 = KnowledgeStore(str(db))  # index exists: no-op
        assert s3._cover_index_builder is None
        time.sleep(0.2)
        assert len(calls) == 1
        assert len(_logs(caplog, "cover index build started")) == 1
        assert len(_logs(caplog, "cover index build finished")) == 1
        for s in (s1, s2, s3):
            s.close()
    finally:
        ddl._run_cover_index_build = real


def test_concurrent_writer_succeeds_during_build(tmp_path):
    db = _seed(tmp_path)
    store = KnowledgeStore(str(db))
    errors = []
    try:
        deadline = time.time() + 10
        n = 0
        while time.time() < deadline and not _has_index(db):
            try:
                store.conn.execute(
                    "UPDATE genes SET last_accessed = last_accessed WHERE rowid = 1"
                )
                store.conn.commit()
                n += 1
            except sqlite3.OperationalError as exc:  # SQLITE_BUSY surfaced
                errors.append(exc)
                break
        assert not errors
        assert _wait(lambda: _has_index(db))
    finally:
        store.close()


def test_close_during_build_returns_promptly_and_rolls_back(tmp_path):
    db = _seed(tmp_path)
    started = threading.Event()
    real = ddl._execute_cover_index_sql

    def blocked(conn):
        started.set()
        # Hold the build open until interrupted/stopped, like a long CREATE.
        end = time.time() + 30
        try:
            while time.time() < end:
                conn.execute("SELECT count(*) FROM genes").fetchone()
                time.sleep(0.01)
        except sqlite3.OperationalError:
            raise

    ddl._execute_cover_index_sql = blocked
    try:
        store = KnowledgeStore(str(db))
        assert started.wait(5)
        t0 = time.time()
        store.close()
        assert time.time() - t0 < 5
        assert not _has_index(db)
    finally:
        ddl._execute_cover_index_sql = real


def test_real_interrupt_rolls_back_index(tmp_path):
    """An interrupted CREATE INDEX leaves no index behind."""
    db = _seed(tmp_path)
    stop = threading.Event()
    stop.set()
    ok = ddl._run_cover_index_build(str(db), stop)
    assert ok is False
    assert not _has_index(db)
    # And a later normal build still works.
    assert ddl._run_cover_index_build(str(db), threading.Event()) is True
    assert _has_index(db)

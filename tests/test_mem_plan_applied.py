"""Wiring: the resolved SqliteMemPlan reaches the SQLite connections.

These are integration-lite — a real temp-file KnowledgeStore / main.db, no
daemon, no model load. They prove the plan's mmap/cache values land on the
writer, reader, and main-db connections, and that the `conservative` profile
still produces the exact v0.6.1 pragmas through the wiring.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from helix_context.hardware import SqliteMemPlan, sqlite_memory_budget
from helix_context.knowledge_store import KnowledgeStore
from helix_context.shard_schema import open_main_db

MiB = 1024 ** 2


@pytest.fixture
def tmp_db_path():
    td = tempfile.TemporaryDirectory()
    yield str(Path(td.name) / "g.db")
    td.cleanup()


def _pragma(conn, name):
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_explicit_plan_lands_on_writer_and_reader(tmp_db_path):
    plan = SqliteMemPlan(mmap_size=64 * MiB,
                         writer_cache_size=-8192, reader_cache_size=-16384)
    ks = KnowledgeStore(tmp_db_path, mem_plan=plan)
    try:
        assert _pragma(ks.conn, "mmap_size") == 64 * MiB
        assert _pragma(ks.conn, "cache_size") == -8192
        assert _pragma(ks._reader, "cache_size") == -16384
        assert _pragma(ks._reader, "mmap_size") == 64 * MiB
    finally:
        ks.close()


def test_conservative_profile_preserves_v061_through_wiring(tmp_db_path, monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "conservative")
    ks = KnowledgeStore(tmp_db_path)  # mem_plan=None -> sqlite_memory_budget(1)
    try:
        assert _pragma(ks.conn, "mmap_size") == 0
        assert _pragma(ks.conn, "cache_size") == -2048
        assert _pragma(ks._reader, "cache_size") == -4096
        assert _pragma(ks._reader, "mmap_size") == 0
    finally:
        ks.close()


def test_default_none_plan_constructs_valid(tmp_db_path, monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "auto")
    ks = KnowledgeStore(tmp_db_path)  # no mem_plan
    try:
        assert _pragma(ks.conn, "mmap_size") >= 0
        assert _pragma(ks.conn, "cache_size") < 0  # negative-KiB units intact
    finally:
        ks.close()


def test_open_main_db_applies_single_db_plan(tmp_db_path, monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "8gb")  # host-independent
    expected = sqlite_memory_budget(1)
    conn = open_main_db(tmp_db_path)
    try:
        # SQLite clamps a 2 GiB mmap request to its build max, so assert mmap
        # is ENABLED; the exactly-round-tripping cache_size proves the plan
        # reached this connection (conservative would be -2048 / mmap 0).
        assert _pragma(conn, "mmap_size") > 0
        assert _pragma(conn, "cache_size") == expected.writer_cache_size
    finally:
        conn.close()

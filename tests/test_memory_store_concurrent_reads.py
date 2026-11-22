"""Issue #505: concurrent build_context on one manager over a ``:memory:`` store.

A ``:memory:`` store has no per-thread reader: ``read_conn`` falls back to the
shared write connection, so before the fix concurrent ``query_docs`` calls (and
the packet-trace fingerprint thread) interleaved statements on one connection
and never returned. File-backed stores were never affected, since every thread
reads through its own connection.

These tests cover completion only. Whether every concurrent call also delivers
is a separate question (score snapshots under concurrency) and is not asserted.

The workload runs in a child process so a regression shows up as one failed
test, not as a wedged pytest run.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.serialized_conn import SerializedConnection, SerializedCursor

REPO_ROOT = Path(__file__).resolve().parents[1]
THREADS = 12
QUERIES_PER_THREAD = 15
HANG_AFTER_S = 120

WORKLOAD = textwrap.dedent(
    """
    import sys, threading

    from tests.conftest import make_cymatix_config, make_gene
    from cymatix_context.config import GenomeConfig
    from cymatix_context.context_manager import CymatixContextManager

    path, n_threads, n_queries = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    cfg = make_cymatix_config(genome=GenomeConfig(path=path, cold_start_threshold=5))
    mgr = CymatixContextManager(cfg)
    topics = [
        "cache redis ttl invalidation",
        "auth token jwt session",
        "splice compress fragment",
    ]
    for i in range(300):
        topic = topics[i % 3]
        mgr.genome.upsert_gene(make_gene(
            f"{topic} document number {i} " * 20,
            domains=topic.split(), gene_id=f"g{i:014d}",
        ))
    queries = [
        "how does cache invalidation ttl work",
        "jwt auth token session",
        "splice fragment compress",
    ]
    finished, errors = [], []

    def work(query):
        try:
            for _ in range(n_queries):
                mgr.build_context(query, read_only=True, ignore_delivered=True)
                finished.append(query)
        except BaseException as exc:
            errors.append(repr(exc))

    threads = [
        threading.Thread(target=work, args=(queries[i % 3],), daemon=True)
        for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(finished) == n_threads * n_queries, len(finished)
    print("OK", len(finished))
    """
)


def _run_workload(path: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(REPO_ROOT), env.get("PYTHONPATH", "")) if p
    )
    return subprocess.run(
        [sys.executable, "-c", WORKLOAD, path, str(THREADS), str(QUERIES_PER_THREAD)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=HANG_AFTER_S,
    )


def test_concurrent_build_context_on_memory_store_completes():
    try:
        proc = _run_workload(":memory:")
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"{THREADS} threads x {QUERIES_PER_THREAD} build_context calls on one "
            f":memory: manager did not finish in {HANG_AFTER_S}s (issue #505)"
        )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().endswith(f"OK {THREADS * QUERIES_PER_THREAD}")


def test_concurrent_build_context_on_file_store_completes(tmp_path):
    proc = _run_workload(str(tmp_path / "genome.db"))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().endswith(f"OK {THREADS * QUERIES_PER_THREAD}")


def test_memory_store_uses_the_serialized_connection_and_shares_its_lock():
    store = KnowledgeStore(":memory:")
    try:
        assert isinstance(store.conn, SerializedConnection)
        assert store.read_conn is store.conn
        assert store.conn.serial_lock is store._write_lock
        assert isinstance(store.conn.cursor(), SerializedCursor)
        # Connection.execute builds its cursor in C; it must not bypass the lock.
        assert isinstance(store.conn.execute("SELECT 1"), SerializedCursor)
    finally:
        store.close()


def test_file_store_keeps_plain_per_thread_readers(tmp_path):
    store = KnowledgeStore(str(tmp_path / "genome.db"))
    try:
        assert not isinstance(store.conn, SerializedConnection)
        assert not isinstance(store.read_conn, SerializedConnection)
        assert store.read_conn is not store.conn
    finally:
        store.close()


def _memory_conn() -> SerializedConnection:
    conn = sqlite3.connect(":memory:", check_same_thread=False, factory=SerializedConnection)
    conn.execute("CREATE TABLE t (n INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
    conn.commit()
    return conn


def test_serialized_cursor_matches_sqlite3_cursor_behaviour():
    conn = _memory_conn()
    cur = conn.execute("SELECT n FROM t ORDER BY n")
    assert cur.fetchone() == (0,)
    assert cur.fetchmany(2) == [(1,), (2,)]
    assert next(cur) == (3,)
    assert len(cur.fetchall()) == 46
    assert cur.fetchone() is None and cur.fetchall() == []
    assert [r[0] for r in conn.execute("SELECT n FROM t WHERE n < 3 ORDER BY n")] == [0, 1, 2]
    ins = conn.execute("INSERT INTO t VALUES (99)")
    assert ins.rowcount == 1 and ins.lastrowid == 51 and ins.fetchall() == []
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT n FROM t WHERE n = 99").fetchone()["n"] == 99
    conn.close()


def test_same_sql_on_two_cursors_does_not_restart_a_half_read_result():
    # Two cursors running identical SQL share one cached prepared statement.
    # On a plain connection the second execute resets the first cursor's
    # result; the serialized cursor has already buffered it.
    conn = _memory_conn()
    sql = "SELECT n FROM t ORDER BY n"
    first = conn.cursor().execute(sql)
    head = [next(first)[0] for _ in range(10)]
    second = conn.cursor().execute(sql)
    assert [r[0] for r in second] == list(range(50))
    assert head + [r[0] for r in first] == list(range(50))
    conn.close()


def test_serialized_connection_interleaved_threads_complete():
    conn = _memory_conn()
    sql = "SELECT n FROM t ORDER BY n"
    totals, errors = [], []

    def work():
        try:
            for _ in range(200):
                totals.append(sum(r[0] for r in conn.cursor().execute(sql)))
        except BaseException as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=work, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "threads hung on a shared connection"
    assert not errors, errors
    assert totals == [sum(range(50))] * 1600
    conn.close()

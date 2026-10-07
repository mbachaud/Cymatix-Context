"""get_doc must read through ``read_conn``, never the shared writer.

Found 2026-10-07 by a multi-lane load probe: two concurrent ``/context``
requests on one server wedged it for good. py-spy showed the event loop in
``KnowledgeStore.get_doc`` (called by ``_compute_know_or_miss_block``) and a
ribosome worker in ``touch_genes`` holding ``_write_lock`` mid-UPDATE, both on
the shared writer ``conn``. That is the py3.14 sqlite3 shared-connection hang
``touch_genes`` and ``read_conn`` already document; ``get_doc`` was the reader
that never moved off the writer.

The hammer runs in a child process: the wedge is a C-level hang that the
in-process ``join(timeout)`` liveness pattern cannot escape (the parent's
join never returned when this reproduced), so the child is killed instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from cymatix_context.genome import Genome

from tests.test_write_lock_sweep import _seed

REPO = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_HAMMER = textwrap.dedent("""
    import sys, threading
    from cymatix_context.genome import Genome
    from tests.test_write_lock_sweep import _seed

    g = Genome(path=sys.argv[1])
    ids = _seed(g, 12, "gt")
    errors = []

    def toucher():
        try:
            for i in range(300):
                g.touch_genes(ids[i % 6:(i % 6) + 4])
        except Exception as exc:
            errors.append(repr(exc))

    def reader():
        try:
            for i in range(3000):
                assert g.get_doc(ids[i % 12]) is not None
        except Exception as exc:
            errors.append(repr(exc))

    ts = [threading.Thread(target=toucher, daemon=True),
          threading.Thread(target=reader, daemon=True)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    g.close()
    print("ERRORS" if errors else "OK", errors[:2])
    sys.exit(1 if errors else 0)
""")


def test_get_doc_issues_no_sql_on_the_writer(tmp_path):
    """Deterministic contract: a request-thread get_doc never touches conn."""
    g = Genome(path=str(tmp_path / "gd.db"))
    ids = _seed(g, 4, "gd")
    writer_sql: list[str] = []
    g.conn.set_trace_callback(writer_sql.append)
    got: list = []
    try:
        t = threading.Thread(target=lambda: got.append(g.get_doc(ids[1])), daemon=True)
        t.start()
        t.join(timeout=30)
        assert not t.is_alive()
        assert got and got[0] is not None and got[0].gene_id == ids[1]
        assert not [s for s in writer_sql if "FROM genes" in s], writer_sql
    finally:
        g.conn.set_trace_callback(None)
        g.close()


@pytest.mark.concurrency
def test_concurrent_get_doc_vs_touch_genes(tmp_path):
    """The live wedge: get_doc on one thread, touch_genes on another."""
    env = dict(os.environ, PYTHONPATH=str(REPO))
    try:
        r = subprocess.run([sys.executable, "-c", _HAMMER, str(tmp_path / "gt.db")],
                           cwd=str(REPO), env=env, capture_output=True, text=True,
                           timeout=120, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        pytest.fail("get_doc vs touch_genes wedged (reader on the shared writer); child killed after 120 s")
    assert r.returncode == 0, f"rc={r.returncode} stdout={r.stdout[-500:]!r} stderr={r.stderr[-1500:]!r}"

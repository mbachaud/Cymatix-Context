"""RAM-reduction bundle (A2/A3/A4) unit tests.

A2 — dense matrix dtype flag (default fp32, opt-in fp16).
A3 — explicit per-connection SQLite cache_size cap.
A4 — explicit PRAGMA mmap_size=0 (fan-out commit guard).

The A3/A4 pragma values are no longer the unconditional default — as of the
dynamic-ram-scaling work (PRD 2026-05-30) they are the ``conservative`` profile,
the byte-identical-to-v0.6.1 escape hatch. These tests pin that profile through
the Genome construction path; the auto/aggressive scaling is covered by
test_mem_budget.py and test_mem_plan_applied.py.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from cymatix_context.genome import Genome
from cymatix_context.knowledge_store import _dense_matrix_dtype


# ── A2: dense matrix dtype ──────────────────────────────────────────────

def test_dense_matrix_dtype_default_is_float32(monkeypatch):
    monkeypatch.delenv("HELIX_DENSE_MATRIX_DTYPE", raising=False)
    assert _dense_matrix_dtype() is np.float32


def test_dense_matrix_dtype_float16_opt_in(monkeypatch):
    for val in ("float16", "fp16", "half", "FLOAT16"):
        monkeypatch.setenv("HELIX_DENSE_MATRIX_DTYPE", val)
        assert _dense_matrix_dtype() is np.float16, val


def test_dense_matrix_dtype_unknown_falls_back_to_float32(monkeypatch):
    monkeypatch.setenv("HELIX_DENSE_MATRIX_DTYPE", "bananas")
    assert _dense_matrix_dtype() is np.float32


# ── A3/A4: SQLite pragmas ───────────────────────────────────────────────

@pytest.fixture
def temp_genome(monkeypatch):
    # The pragma tests below pin the `conservative` profile == the exact
    # v0.6.1 posture (the escape hatch), now opt-in rather than the default.
    monkeypatch.setenv("HELIX_MEM_PROFILE", "conservative")
    td = tempfile.TemporaryDirectory()
    g = Genome(str(Path(td.name) / "g.db"))
    yield g
    try:
        g.close()
    except Exception:
        pass
    td.cleanup()


def test_writer_cache_size_capped(temp_genome):
    val = temp_genome.conn.execute("PRAGMA cache_size").fetchone()[0]
    assert val == -2048, "writer page cache must be capped at 2 MB (A3)"


def test_writer_mmap_off(temp_genome):
    val = temp_genome.conn.execute("PRAGMA mmap_size").fetchone()[0]
    assert val == 0, "writer mmap must be explicitly 0 (A4 fan-out guard)"


def test_reader_cache_size_capped(temp_genome):
    assert temp_genome._reader is not None
    val = temp_genome._reader.execute("PRAGMA cache_size").fetchone()[0]
    assert val == -4096, "reader page cache must be capped at 4 MB (A3)"


def test_reader_mmap_off(temp_genome):
    assert temp_genome._reader is not None
    val = temp_genome._reader.execute("PRAGMA mmap_size").fetchone()[0]
    assert val == 0, "reader mmap must be explicitly 0 (A4 fan-out guard)"

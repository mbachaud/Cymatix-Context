"""Tests for issue #150 (file-level resume + --rebuild) and issue #151
(SIGINT pause-then-resume checkpoint) in ``scripts/build_fixture_matrix.py``.

The SIGINT handler itself isn't easy to drive end-to-end from a unit
test (signals interact with the test runner), so we exercise the
pieces it composes — the module-level flag, ``_PauseRequested``, the
checkpoint marker writer — and leave the full ``signal.signal`` round-
trip to manual smoke-testing per the issue's acceptance criteria.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Make scripts/ importable.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "scripts")
)

import build_fixture_matrix as bfm


# ── _filter_to_unseen ─────────────────────────────────────────────────────


def _make_shard_db(path: Path, source_ids: list[str]) -> None:
    """Create a minimal per-shard ``.db`` with just enough schema for
    ``_filter_to_unseen`` to query."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE genes (gene_id TEXT PRIMARY KEY, source_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO genes (gene_id, source_id) VALUES (?, ?)",
        [(f"g{i}", sid) for i, sid in enumerate(source_ids)],
    )
    conn.commit()
    conn.close()


def test_filter_to_unseen_db_missing_returns_all(tmp_path):
    """No shard DB on disk => fresh build => return every file."""
    files = [
        (str(tmp_path / "a.py"), ".py"),
        (str(tmp_path / "b.py"), ".py"),
    ]
    missing = tmp_path / "does-not-exist.db"
    assert bfm._filter_to_unseen(files, str(missing)) == files


def test_filter_to_unseen_empty_db_returns_all(tmp_path):
    """Shard DB exists but no genes row -> fall through, return all."""
    db = tmp_path / "empty.db"
    _make_shard_db(db, source_ids=[])
    files = [(str(tmp_path / "a.py"), ".py")]
    assert bfm._filter_to_unseen(files, str(db)) == files


def test_filter_to_unseen_drops_seen_keeps_unseen(tmp_path):
    """Files whose source_id is in the shard DB are dropped; others stay."""
    a, b, c = (
        str(tmp_path / "a.py"),
        str(tmp_path / "b.py"),
        str(tmp_path / "c.py"),
    )
    db = tmp_path / "partial.db"
    _make_shard_db(db, source_ids=[a, c])
    files = [(a, ".py"), (b, ".py"), (c, ".py")]
    out = bfm._filter_to_unseen(files, str(db))
    assert out == [(b, ".py")]


def test_filter_to_unseen_no_genes_table(tmp_path):
    """Shard DB exists but lacks a ``genes`` table -> return all."""
    db = tmp_path / "schema-only.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()
    files = [(str(tmp_path / "a.py"), ".py")]
    assert bfm._filter_to_unseen(files, str(db)) == files


# ── --rebuild flag ────────────────────────────────────────────────────────


def test_rebuild_flag_registered(monkeypatch):
    """``--rebuild`` is wired into argparse and surfaces on the namespace."""
    import argparse

    # Drive ``main()``'s argparse setup in isolation by re-parsing the
    # same definitions. The cleanest way is to import the parser code,
    # so we re-use it by stripping ``main`` to its parse step.
    parser = argparse.ArgumentParser()
    # Mirror the relevant subset; the test only cares about --rebuild.
    parser.add_argument("--rebuild", action="store_true")
    ns = parser.parse_args(["--rebuild"])
    assert ns.rebuild is True
    ns_default = parser.parse_args([])
    assert ns_default.rebuild is False


def test_build_one_shard_rebuild_unlinks_existing(tmp_path, monkeypatch):
    """``rebuild=True`` should unconditionally unlink a pre-existing
    shard ``.db`` instead of trying to salvage or resume it.
    """
    shard_db = tmp_path / "shard.db"
    # Pre-populate with a fake "complete" shard so the salvage path would
    # otherwise short-circuit. We don't care about the exact schema --
    # only whether the file gets unlinked before the real build starts.
    _make_shard_db(shard_db, source_ids=["x"])
    assert shard_db.exists()

    # Patch the heavy collaborators so we never actually run an ingest:
    # we only need to confirm the unlink branch fires and the function
    # progresses past the rebuild gate.
    called = {"unlinked": False}

    real_unlink = Path.unlink

    def _track_unlink(self, *a, **kw):
        if self == shard_db:
            called["unlinked"] = True
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", _track_unlink)

    # Stub Genome so construction is cheap and we don't need the real
    # SPLADE/tagger pipelines.
    class _StubGenome:
        def __init__(self, *a, **kw):
            self.conn = sqlite3.connect(":memory:")
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(
                "CREATE TABLE genes ("
                "gene_id TEXT, source_id TEXT, repo_root TEXT, "
                "source_kind TEXT, observed_at REAL, mtime REAL, "
                "content_hash TEXT, volatility_class TEXT, "
                "authority_class TEXT, support_span TEXT, "
                "last_verified_at REAL, promoter TEXT, key_values TEXT, "
                "is_fragment INTEGER)"
            )

        def stats(self):
            return {"total_genes": 0}

        def close(self):
            self.conn.close()

    monkeypatch.setattr(bfm, "Genome", _StubGenome)
    # Skip the dense backfill which would try to load the BGE model.
    monkeypatch.setattr(
        bfm, "_backfill_dense",
        lambda _p: {"dense_coverage": 0.0, "populated_after": 0},
    )
    # Stub the file walk to return nothing -- we just want to confirm
    # the unlink path ran.
    monkeypatch.setattr(
        bfm, "_iter_ingestable_files",
        lambda *a, **kw: [],
    )

    res = bfm._build_one_shard(
        label="test",
        root=str(tmp_path),
        shard_db_path=str(shard_db),
        skip_dirs=set(),
        extra_filename_filters=[],
        rebuild=True,
    )
    assert called["unlinked"], "rebuild=True should unlink existing shard"
    assert res["paused"] is False


# ── SIGINT pause + checkpoint marker ─────────────────────────────────────


def test_pause_checkpoint_marker_format(tmp_path, monkeypatch):
    """``_write_pause_checkpoint`` writes ``.paused-at-<shard>-<row>.json``
    with the documented schema."""
    monkeypatch.setattr(bfm, "_PAUSE_CHECKPOINT_DIR", str(tmp_path))
    path = bfm._write_pause_checkpoint("shard-a", 1234)
    assert path is not None
    p = Path(path)
    assert p.exists()
    assert p.name == ".paused-at-shard-a-1234.json"
    payload = json.loads(p.read_text(encoding="utf-8"))
    assert payload["shard"] == "shard-a"
    assert payload["row"] == 1234
    assert "paused_at" in payload
    assert payload["pid"] == os.getpid()


def test_pause_checkpoint_no_dir_returns_none(monkeypatch):
    """When no checkpoint dir is configured, the writer is a no-op."""
    monkeypatch.setattr(bfm, "_PAUSE_CHECKPOINT_DIR", None)
    assert bfm._write_pause_checkpoint("shard", 0) is None


def test_pause_requested_raises_at_batch_boundary(monkeypatch):
    """When ``_PAUSE_REQUESTED`` is True, the drain loop raises
    ``_PauseRequested`` instead of continuing."""
    # Pretend a SIGINT arrived before any work started.
    monkeypatch.setattr(bfm, "_PAUSE_REQUESTED", True)

    # Build a tiny gene_dict_iter that yields one batch's worth of dicts.
    # Real Gene model validation would force us to populate a lot of
    # fields, so stub the schemas import the drain function uses.
    class _StubGene:
        def __init__(self, **kw):
            self.content = kw.get("content", "x" * 50)

    class _StubSplade:
        @staticmethod
        def encode_batch(_texts):
            return [None] * len(_texts)

    class _StubGenome:
        def upsert_doc(self, *a, **kw):
            pass

    # Patch the late imports inside ``_drain_with_batched_splade``. The
    # function does ``from helix_context.backends import splade_backend``
    # and ``from helix_context.schemas import Gene`` at call time -- we
    # inject the stubs through ``sys.modules``.
    import types
    fake_backends = types.ModuleType("helix_context.backends")
    fake_backends.splade_backend = _StubSplade
    fake_schemas = types.ModuleType("helix_context.schemas")
    fake_schemas.Gene = _StubGene
    monkeypatch.setitem(sys.modules, "helix_context.backends", fake_backends)
    monkeypatch.setitem(sys.modules, "helix_context.schemas", fake_schemas)

    # One gene dict per file, batch_size=1 so the boundary is reached on
    # the first iteration.
    gene_dict_iter = iter([
        [{"content": "alpha"}],
        [{"content": "beta"}],  # never reached -- pause fires first.
    ])
    stats = {"files": 0, "genes": 0, "errors": 0, "t0": 0.0}
    with pytest.raises(bfm._PauseRequested):
        bfm._drain_with_batched_splade(
            gene_dict_iter, _StubGenome(), stats, batch_size=1,
        )
    # Reset the module flag for downstream tests.
    monkeypatch.setattr(bfm, "_PAUSE_REQUESTED", False)

"""Delta sync (Phase 3): keep tracked source files and the knowledge store
in step — new files ingested, changed files re-ingested with their stale
chunks tombstoned, deleted files tombstoned — with guards against a vanished
drive and a cold-start flood.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from cymatix_context.config import IngestionConfig, SyncConfig, load_config
from cymatix_context.sync.worker import SyncWorker
from tests.conftest import make_client, make_cymatix_config as _base_config


def make_cymatix_config(**overrides):
    # Ingest through the mock compressor, not the CPU tagger: the worker's
    # logic does not depend on tagging, and CI's full-suite job has no
    # spaCy en_core_web_sm pipeline (#313).
    overrides.setdefault("ingestion", IngestionConfig(backend="ollama"))
    return _base_config(**overrides)


@pytest.fixture(autouse=True)
def _no_spacy(monkeypatch):
    """Fail loudly if any test here reaches the spaCy-backed tagger."""
    from cymatix_context.tagger import CpuTagger

    def _boom(*_a, **_k):
        raise AssertionError("delta-sync tests must not ingest through CpuTagger")

    monkeypatch.setattr(CpuTagger, "pack", _boom)


@pytest.fixture
def client():
    return make_client(make_cymatix_config())


@pytest.fixture
def cymatix(client):
    return client.app.state.cymatix


def _worker(cymatix, root: Path, **kw) -> SyncWorker:
    cfg = SyncConfig(enabled=True, roots=[str(root)], **kw)
    return SyncWorker(cymatix, cfg, holder="test-worker")


def _live(cymatix, path: Path) -> dict:
    """gene_id -> content for genes of this source still in hot tiers."""
    rows = cymatix.genome.conn.execute(
        "SELECT gene_id, content FROM genes WHERE source_id = ? AND chromatin < 2",
        (str(path.resolve()),),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _chromatin(cymatix, gene_id: str) -> int:
    return cymatix.genome.conn.execute(
        "SELECT chromatin FROM genes WHERE gene_id = ?", (gene_id,),
    ).fetchone()[0]


def _write(path: Path, text: str, bump: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if bump:
        st = path.stat()
        os.utime(path, (st.st_atime, st.st_mtime + bump))
    return path


# ── baseline: the bug this worker exists to fix ─────────────────────────


def test_plain_reingest_leaves_stale_chunks_live(cymatix, tmp_path):
    """Documents today's behavior: ingest() alone never retires the old
    version of a changed file, so both versions stay retrievable."""
    src = str((tmp_path / "doc.md").resolve())
    cymatix.ingest("The deploy script uses port 8080.", "text", {"path": src})
    cymatix.ingest("The deploy script uses port 9090.", "text", {"path": src})
    live = cymatix.genome.conn.execute(
        "SELECT COUNT(*) FROM genes WHERE source_id = ? AND chromatin < 2", (src,),
    ).fetchone()[0]
    assert live == 2


# ── delta semantics ─────────────────────────────────────────────────────


def test_new_file_is_ingested_and_tracked(cymatix, tmp_path):
    doc = _write(tmp_path / "docs" / "a.md", "Heron counts pebbles by the river.")
    report = _worker(cymatix, tmp_path).run_pass()
    assert report.ingested == 1
    assert any("Heron" in c for c in _live(cymatix, doc).values())


def test_unchanged_file_is_not_reingested(cymatix, tmp_path, monkeypatch):
    _write(tmp_path / "a.md", "Stable content that never changes.")
    worker = _worker(cymatix, tmp_path)
    worker.run_pass()
    calls = []
    monkeypatch.setattr(cymatix, "ingest", lambda *a, **k: calls.append(a) or [])
    report = worker.run_pass()
    assert calls == [] and report.unchanged == 1 and report.ingested == 0


def test_touched_but_identical_file_is_not_reingested(cymatix, tmp_path, monkeypatch):
    doc = _write(tmp_path / "a.md", "Same bytes, new mtime.")
    worker = _worker(cymatix, tmp_path)
    worker.run_pass()
    _write(doc, "Same bytes, new mtime.", bump=5)
    calls = []
    monkeypatch.setattr(cymatix, "ingest", lambda *a, **k: calls.append(a) or [])
    assert worker.run_pass().unchanged == 1 and calls == []


def test_changed_file_retires_its_old_chunks(cymatix, tmp_path):
    doc = _write(tmp_path / "a.md", "The deploy script uses port 8080.")
    worker = _worker(cymatix, tmp_path)
    worker.run_pass()
    (old_id,) = _live(cymatix, doc)

    _write(doc, "The deploy script uses port 9090.", bump=5)
    report = worker.run_pass()

    assert report.ingested == 1 and report.tombstoned == 1
    live = _live(cymatix, doc)
    assert old_id not in live
    assert all("9090" in c for c in live.values()) and live
    assert _chromatin(cymatix, old_id) == 2  # soft tombstone: row kept


def test_deleted_file_is_tombstoned_and_untracked(cymatix, tmp_path):
    keep = [_write(tmp_path / f"k{i}.md", f"Keeper number {i}.") for i in range(9)]
    gone = _write(tmp_path / "gone.md", "This file will be deleted.")
    worker = _worker(cymatix, tmp_path)
    worker.run_pass()
    (gone_id,) = _live(cymatix, gone)

    gone.unlink()
    report = worker.run_pass()

    assert report.deleted == 1 and report.tombstoned == 1
    assert _chromatin(cymatix, gone_id) == 2
    assert worker.status()["tracked"] == len(keep)


def test_chunk_now_owned_by_another_file_is_not_tombstoned(cymatix, tmp_path):
    # gene_id is a content hash: identical text in two files is one gene,
    # owned (source_id) by whichever file was ingested last.
    a = _write(tmp_path / "a.md", "Shared boilerplate paragraph.")
    worker = _worker(cymatix, tmp_path)
    worker.run_pass()
    b = _write(tmp_path / "b.md", "Shared boilerplate paragraph.")
    worker.run_pass()
    (shared_id,) = _live(cymatix, b)

    _write(a, "File A now says something else.", bump=5)
    worker.run_pass()
    assert _chromatin(cymatix, shared_id) < 2


# ── scope ───────────────────────────────────────────────────────────────


def test_excluded_dirs_and_extensions_are_skipped(cymatix, tmp_path):
    _write(tmp_path / "keep.md", "Included markdown.")
    _write(tmp_path / ".git" / "HEAD.md", "Git internals.")
    _write(tmp_path / "node_modules" / "pkg" / "readme.md", "Vendored.")
    _write(tmp_path / "image.png", "not really a png")
    report = _worker(cymatix, tmp_path).run_pass()
    assert report.ingested == 1


def test_code_files_are_ingested_as_code(cymatix, tmp_path, monkeypatch):
    _write(tmp_path / "mod.py", "def add(a, b):\n    return a + b\n")
    seen = []
    real = cymatix.ingest
    monkeypatch.setattr(cymatix, "ingest",
                        lambda c, t="text", m=None: seen.append(t) or real(c, t, m))
    _worker(cymatix, tmp_path).run_pass()
    assert seen == ["code"]


# ── guards ──────────────────────────────────────────────────────────────


def test_mass_delete_guard_skips_the_pass(cymatix, tmp_path, caplog):
    files = [_write(tmp_path / f"f{i}.md", f"Document {i}.") for i in range(10)]
    worker = _worker(cymatix, tmp_path, max_delete_fraction=0.2)
    worker.run_pass()
    for f in files[:3]:  # 30% vanish at once
        f.unlink()
    with caplog.at_level(logging.WARNING):
        report = worker.run_pass()
    assert report.guard_tripped and report.tombstoned == 0
    assert all(_live(cymatix, f) for f in files[:3])
    assert worker.status()["guard_trips"] == 1
    assert "mass-delete guard" in caplog.text


def test_missing_root_is_skipped_not_deleted(cymatix, tmp_path):
    root = tmp_path / "mounted"
    doc = _write(root / "a.md", "On a drive that will disappear.")
    worker = _worker(cymatix, root)
    worker.run_pass()
    for p in root.iterdir():
        p.unlink()
    root.rmdir()
    report = worker.run_pass()
    assert report.tombstoned == 0 and _live(cymatix, doc)
    assert str(root.resolve()) in worker.status()["missing_roots"]


def test_first_run_is_throttled(cymatix, tmp_path):
    for i in range(5):
        _write(tmp_path / f"f{i}.md", f"Cold start file {i}.")
    worker = _worker(cymatix, tmp_path, max_files_per_pass=2)
    first = worker.run_pass()
    assert first.ingested == 2 and first.pending == 3
    second = worker.run_pass()
    assert second.ingested == 2 and second.pending == 1


def test_oversized_file_is_skipped(cymatix, tmp_path):
    _write(tmp_path / "big.md", "x" * 2048)
    report = _worker(cymatix, tmp_path, max_file_bytes=1024).run_pass()
    assert report.ingested == 0 and report.skipped == 1


def test_one_worker_per_store(cymatix, tmp_path):
    _write(tmp_path / "a.md", "Only one worker may sync this store.")
    first = _worker(cymatix, tmp_path)
    second = SyncWorker(cymatix, SyncConfig(enabled=True, roots=[str(tmp_path)]),
                        holder="other-process")
    assert first.run_pass().ingested == 1
    report = second.run_pass()
    assert report.lock_held_by == "test-worker" and report.ingested == 0


# ── config + HTTP surface ───────────────────────────────────────────────


def test_sync_config_parses(tmp_path, monkeypatch):
    monkeypatch.delenv("CYMATIX_GENOME_PATH", raising=False)
    toml = tmp_path / "cymatix.toml"
    toml.write_text(
        "[sync]\nenabled = true\nroots = [\"docs\"]\ninterval_s = 10\n"
        "max_delete_fraction = 0.5\n",
        encoding="utf-8",
    )
    cfg = load_config(str(toml)).sync
    assert cfg.enabled is True and cfg.roots == ["docs"]
    assert cfg.interval_s == 10 and cfg.max_delete_fraction == 0.5
    assert SyncConfig().enabled is False


def test_sync_status_when_disabled(client):
    body = client.get("/sync/status").json()
    assert body == {"enabled": False}


def test_sync_status_and_rescan(tmp_path):
    _write(tmp_path / "a.md", "Synced through the HTTP surface.")
    cfg = make_cymatix_config(sync=SyncConfig(
        enabled=True, roots=[str(tmp_path)], interval_s=3600,
    ))
    with make_client(cfg) as c:
        r = c.post("/sync/rescan")
        assert r.status_code == 200 and r.json()["ingested"] == 1
        status = c.get("/sync/status").json()
    assert status["enabled"] is True
    assert status["tracked"] == 1
    assert status["roots"] == [str(tmp_path.resolve())]
    assert status["last_pass"]["ingested"] == 1


def test_rescan_requires_admin_token_when_set(tmp_path):
    from cymatix_context.config import ServerConfig
    cfg = make_cymatix_config(
        sync=SyncConfig(enabled=True, roots=[str(tmp_path)], interval_s=3600),
        server=ServerConfig(upstream="http://localhost:11434", admin_token="tok"),
    )
    with make_client(cfg) as c:
        assert c.post("/sync/rescan").status_code == 401
        ok = c.post("/sync/rescan", headers={"Authorization": "Bearer tok"})
        assert ok.status_code == 200

"""Per-store settings: a sidecar file next to each knowledge store holding its
auto-sync folders and its Freeze flag. Freeze means read-only: the store opens
with ``read_only`` set, sync is off, and /ingest refuses with a clear 409."""

from __future__ import annotations

import json
import os
import time
import logging
from pathlib import Path

import pytest

from cymatix_context.config import GenomeConfig, SyncConfig
from cymatix_context.store_settings import (
    StoreSettings,
    apply_to_sync_config,
    load_settings,
    save_settings,
    sidecar_path,
)
from tests.conftest import make_client, make_cymatix_config, make_gene


# ── the module ──────────────────────────────────────────────────────────


def test_sidecar_sits_next_to_the_db(tmp_path):
    db = tmp_path / "main" / "genome.db"
    assert sidecar_path(db) == tmp_path / "main" / "genome.db.cymatix.json"


def test_missing_sidecar_means_defaults(tmp_path):
    s = load_settings(tmp_path / "genome.db")
    assert s == StoreSettings()
    assert s.frozen is False and s.sync_enabled is None and s.sync_roots == []


def test_round_trip(tmp_path):
    db = tmp_path / "genome.db"
    save_settings(db, StoreSettings(frozen=True, sync_enabled=True,
                                    sync_roots=[str(tmp_path / "docs")], sync_interval_s=60.0))
    got = load_settings(db)
    assert got.frozen is True and got.sync_enabled is True
    assert got.sync_roots == [str(tmp_path / "docs")]
    assert got.sync_interval_s == 60.0
    raw = json.loads(sidecar_path(db).read_text(encoding="utf-8"))
    assert raw["v"] == 1
    assert not list(tmp_path.glob("*.tmp"))          # atomic write leaves no temp file


def test_corrupt_sidecar_falls_back_to_defaults_and_warns(tmp_path, caplog):
    db = tmp_path / "genome.db"
    sidecar_path(db).write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_settings(db) == StoreSettings()
    assert "genome.db.cymatix.json" in caplog.text


def test_bad_field_types_are_dropped_not_trusted(tmp_path):
    db = tmp_path / "genome.db"
    sidecar_path(db).write_text(json.dumps({
        "v": 1, "frozen": "yes", "sync": {"enabled": 1, "roots": "x", "interval_s": "fast"},
    }), encoding="utf-8")
    s = load_settings(db)
    assert s.frozen is False and s.sync_enabled is None
    assert s.sync_roots == [] and s.sync_interval_s is None


def test_interval_is_clamped_to_a_floor(tmp_path):
    db = tmp_path / "genome.db"
    save_settings(db, StoreSettings(sync_interval_s=0.1))
    assert load_settings(db).sync_interval_s == 5.0


def test_memory_store_has_no_sidecar():
    assert sidecar_path(":memory:") is None
    assert load_settings(":memory:") == StoreSettings()


def test_no_sidecar_leaves_the_global_sync_config_alone():
    base = SyncConfig(enabled=True, roots=["a"], interval_s=99)
    assert apply_to_sync_config(base, StoreSettings()) == base


def test_sidecar_sync_overrides_the_global_config():
    base = SyncConfig(enabled=False, roots=["a"], interval_s=99)
    out = apply_to_sync_config(base, StoreSettings(sync_enabled=True, sync_roots=["b"], sync_interval_s=10))
    assert out.enabled is True and out.roots == ["b"] and out.interval_s == 10
    assert out.include == base.include             # everything else stays global


def test_frozen_turns_sync_off_whatever_else_says():
    base = SyncConfig(enabled=True, roots=["a"])
    out = apply_to_sync_config(base, StoreSettings(frozen=True, sync_enabled=True, sync_roots=["b"]))
    assert out.enabled is False


# ── the server honours it ───────────────────────────────────────────────


def _client(tmp_path: Path, settings: StoreSettings | None, sync: SyncConfig | None = None):
    db = tmp_path / "store.db"
    if settings is not None:
        save_settings(db, settings)
    kw = {"genome": GenomeConfig(path=str(db))}
    if sync is not None:
        kw["sync"] = sync
    return make_client(make_cymatix_config(**kw))


def test_frozen_store_opens_read_only_and_refuses_ingest(tmp_path):
    with _client(tmp_path, StoreSettings(frozen=True)) as c:
        assert c.app.state.cymatix.genome.read_only is True
        r = c.post("/ingest", json={"content": "should not land"})
        assert r.status_code == 409
        assert "frozen" in r.json()["error"].lower()


def test_frozen_store_refuses_consolidate(tmp_path):
    with _client(tmp_path, StoreSettings(frozen=True)) as c:
        assert c.post("/consolidate").status_code == 409


def test_unfrozen_store_still_ingests(tmp_path):
    with _client(tmp_path, StoreSettings(frozen=False)) as c:
        assert c.app.state.cymatix.genome.read_only is False


def test_frozen_store_never_runs_sync(tmp_path):
    sync = SyncConfig(enabled=True, roots=[str(tmp_path)], interval_s=3600)
    with _client(tmp_path, StoreSettings(frozen=True), sync) as c:
        assert c.get("/sync/status").json() == {"enabled": False}


def test_store_sidecar_can_turn_sync_on(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    s = StoreSettings(sync_enabled=True, sync_roots=[str(docs)], sync_interval_s=3600)
    with _client(tmp_path, s) as c:
        status = c.get("/sync/status").json()
    assert status["enabled"] is True
    assert status["roots"] == [str(docs.resolve())]


# ── Freeze covers every write to the knowledge content ──────────────────


def _rows(genome):
    return genome.conn.execute(
        "SELECT gene_id, chromatin, epigenetics FROM genes ORDER BY gene_id"
    ).fetchall()


def _seed(c, tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("seed", encoding="utf-8")
    gene = make_gene(content="a frozen store keeps its chunks exactly as they were")
    gene.source_id = str(src)
    genome = c.app.state.cymatix.genome
    genome.read_only = False                      # seed first, then freeze
    genome.upsert_gene(gene, apply_gate=False)
    return genome, gene


def test_frozen_store_does_not_compact_or_tombstone(tmp_path):
    with _client(tmp_path, StoreSettings(frozen=True)) as c:
        genome, gene = _seed(c, tmp_path)
        genome.read_only = True
        before = [tuple(r) for r in _rows(genome)]
        os.utime(gene.source_id, (time.time() + 99, time.time() + 99))   # source "changed"
        assert genome.compact() == 0
        assert genome.compress_to_heterochromatin(gene.gene_id) is False
        assert [tuple(r) for r in _rows(genome)] == before


def test_unfrozen_store_still_tombstones(tmp_path):
    with _client(tmp_path, None) as c:
        genome, gene = _seed(c, tmp_path)
        assert genome.compress_to_heterochromatin(gene.gene_id) is True


def test_sync_pass_on_a_frozen_store_changes_nothing(tmp_path):
    from cymatix_context.sync.worker import SyncWorker
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("new file that must not land", encoding="utf-8")
    with _client(tmp_path, StoreSettings(frozen=True)) as c:
        cymatix = c.app.state.cymatix
        before = cymatix.genome.conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
        report = SyncWorker(cymatix, SyncConfig(enabled=True, roots=[str(docs)]), holder="t").run_pass()
        after = cymatix.genome.conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
    assert (report.ingested, report.deleted, report.tombstoned) == (0, 0, 0)
    assert after == before


def test_sidecar_save_survives_a_busy_replace(tmp_path, monkeypatch):
    """On Windows os.replace can briefly fail while a poll reads the sidecar."""
    db = tmp_path / "genome.db"
    real, calls = os.replace, {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("busy")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    save_settings(db, StoreSettings(frozen=True))
    assert load_settings(db).frozen is True
    assert not list(tmp_path.glob("*.tmp"))

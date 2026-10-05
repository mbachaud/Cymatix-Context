"""Launcher side of per-store settings: the Database panel shows each store's
sync folders and Freeze switch, and POST /api/genome/settings writes the
sidecar next to the .db (restarting the server only when the active store
changed)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("jinja2", reason="launcher extra not installed")
from fastapi.testclient import TestClient

from cymatix_context.launcher import genome_registry as gr
from cymatix_context.launcher.app import create_app
from cymatix_context.launcher.collector import StateCollector
from cymatix_context.store_settings import StoreSettings, load_settings, save_settings
from tests.test_launcher_dashboard_wiring import FakeCollector, FakeSupervisor


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CYMATIX_LAUNCHER_STATE_DIR", str(tmp_path / "state"))
    gr.clear_cache()
    yield
    gr.clear_cache()


def _store(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_bytes(b"SQLite format 3\x00")
    return p


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Two stores: `main` (active) and `other`, both known to the registry."""
    main, other = _store(tmp_path, "main.db"), _store(tmp_path, "other.db")
    monkeypatch.setenv("CYMATIX_GENOME_PATH", str(main))
    infos = [gr.GenomeInfo(name=p.stem, path=p.resolve(), size_bytes=1, mtime=1.0, total_genes=3)
             for p in (main, other)]
    monkeypatch.setattr(gr, "discover_genomes", lambda: infos)
    sup = FakeSupervisor()
    app = create_app(store=SimpleNamespace(), supervisor=sup, collector=FakeCollector())
    with TestClient(app) as c:
        yield SimpleNamespace(c=c, main=main, other=other, sup=sup, tmp=tmp_path)


def _post(w, **body):
    return w.c.post("/api/genome/settings", json=body)


# ── POST /api/genome/settings ──────────────────────────────────────────


def test_unknown_store_is_404(world):
    r = _post(world, path=str(world.tmp / "nope.db"), frozen=True)
    assert r.status_code == 404


def test_freeze_writes_the_sidecar_and_restarts_when_active(world):
    r = _post(world, path=str(world.main), frozen=True)
    assert r.status_code == 202 and r.json()["restarting"] is True
    assert load_settings(world.main).frozen is True


def test_changing_an_inactive_store_does_not_restart(world):
    r = _post(world, path=str(world.other), frozen=True)
    assert r.status_code == 200 and r.json()["restarting"] is False
    assert load_settings(world.other).frozen is True
    assert world.sup.restarts == []


def test_partial_updates_merge_with_what_is_saved(world):
    docs = world.tmp / "docs"
    docs.mkdir()
    save_settings(world.other, StoreSettings(frozen=True))
    _post(world, path=str(world.other), sync_enabled=True, sync_roots=[str(docs.resolve())])
    got = load_settings(world.other)
    assert got.frozen is True and got.sync_enabled is True and got.sync_roots == [str(docs.resolve())]


def test_a_root_that_is_not_a_directory_is_rejected(world):
    r = _post(world, path=str(world.other), sync_roots=[str(world.tmp / "missing")])
    assert r.status_code == 400
    assert not (world.other.with_name(world.other.name + ".cymatix.json")).exists()


def test_wrong_types_are_rejected(world):
    assert _post(world, path=str(world.other), frozen="yes").status_code == 400
    assert _post(world, path=str(world.other), sync_roots="x").status_code == 400
    assert _post(world, path=str(world.other)).status_code == 400   # nothing to change


# ── the Database panel ─────────────────────────────────────────────────


def test_panel_rows_carry_each_stores_settings(world):
    save_settings(world.other, StoreSettings(frozen=True))
    panel = StateCollector(supervisor=MagicMock())._database_panel()
    by_name = {e["name"]: e for e in panel["entries"]}
    assert by_name["other"]["settings"]["frozen"] is True
    assert by_name["main"]["settings"]["frozen"] is False


def test_panel_html_has_the_controls(world):
    docs = world.tmp / "docs"
    docs.mkdir()
    save_settings(world.main, StoreSettings(sync_enabled=True, sync_roots=[str(docs)]))
    save_settings(world.other, StoreSettings(frozen=True))
    state = {"cymatix": {"running": False, "port": 11437},
             "database": StateCollector(supervisor=MagicMock())._database_panel()}
    collector = MagicMock()
    collector.collect.return_value = state
    app = create_app(store=SimpleNamespace(), supervisor=FakeSupervisor(), collector=collector)
    with TestClient(app) as c:
        html = c.get("/api/state/panels").text
    assert 'data-action="store-freeze"' in html
    assert 'data-action="store-sync"' in html
    assert 'data-action="store-add-folder"' in html
    assert 'data-action="store-remove-folder"' in html
    assert str(docs) in html
    assert "frozen" in html.lower()
    # A frozen store cannot have sync switched on from the UI.
    frozen_block = html.split('data-store-path="' + str(world.other.resolve()), 1)[1].split("</li>", 1)[0]
    assert 'data-action="store-sync"' in frozen_block and "disabled" in frozen_block

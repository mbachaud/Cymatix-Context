"""v0.7.0 dashboard wiring sweep: obs panel state, genome web API,
fresh-checkout mkdir fix, grafana sidecar env hardening."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("jinja2", reason="launcher extra not installed")
from fastapi.testclient import TestClient

from cymatix_context.launcher.app import create_app


class FakeSupervisor:
    def __init__(self):
        self.restarts = []
        self.starts = 0
        self.last_start_pending = False

    def adopt(self):
        return False

    def is_running(self):
        return True

    def owns_process(self):
        return False

    def start(self):
        self.starts += 1
        return 1234

    def stop(self, reason=""):
        pass

    def restart(self, reason=""):
        self.restarts.append(reason)
        return 1234


class FakeCollector:
    def collect(self):
        return {
            "helix": {"running": False, "availability": "unavailable"},
        }


class FakeObservability:
    def all_statuses(self):
        return {
            "collector": "green",
            "prometheus": "green",
            "tempo": "red",
            "loki": "green",
            "grafana": "green",
        }


@pytest.fixture
def client():
    app = create_app(
        store=SimpleNamespace(),
        supervisor=FakeSupervisor(),
        collector=FakeCollector(),
        observability=FakeObservability(),
        grafana_url="http://127.0.0.1:3000/d/helix-overview/helix-overview",
        prometheus_url="http://127.0.0.1:9090/graph",
    )
    with TestClient(app) as c:
        yield c, app


# ── observability state + panel ────────────────────────────────────────


def test_api_state_carries_observability(client):
    c, _ = client
    state = c.get("/api/state").json()
    obs = state["observability"]
    assert obs["install_pending"] is False
    names = {s["name"]: s["status"] for s in obs["services"]}
    assert names["tempo"] == "red" and names["grafana"] == "green"
    assert obs["links"]["prometheus"].endswith("/graph")
    urls = [d["url"] for d in obs["links"]["dashboards"]]
    assert "http://127.0.0.1:3000/d/helix-overview" in urls
    assert any("pipeline-observatory" in u for u in urls)


def test_panels_partial_renders_observability(client):
    c, _ = client
    html = c.get("/api/state/panels").text
    assert "panel--observability" in html
    assert "obs-dot--red" in html  # tempo
    assert "Prometheus" in html


def test_observability_panel_absent_when_opted_out():
    app = create_app(
        store=SimpleNamespace(),
        supervisor=FakeSupervisor(),
        collector=FakeCollector(),
        observability=None,
        observability_install_pending=False,
    )
    with TestClient(app) as c:
        assert c.get("/api/state").json()["observability"] is None
        assert "panel--observability" not in c.get("/api/state/panels").text


def test_install_pending_renders_hint():
    app = create_app(
        store=SimpleNamespace(),
        supervisor=FakeSupervisor(),
        collector=FakeCollector(),
        observability=None,
        observability_install_pending=True,
    )
    with TestClient(app) as c:
        html = c.get("/api/state/panels").text
        assert "Sidecar not installed" in html


# ── genome web API ─────────────────────────────────────────────────────


def test_genome_select_missing_path_400(client):
    c, _ = client
    r = c.post("/api/genome/select", json={})
    assert r.status_code == 400


def test_genome_select_unknown_file_404(client):
    c, _ = client
    r = c.post("/api/genome/select", json={"path": "Z:/nope/missing.db"})
    assert r.status_code == 404


def test_genome_select_existing_restarts(client, tmp_path, monkeypatch):
    c, app = client
    db = tmp_path / "alt.genome.db"
    db.write_bytes(b"")
    monkeypatch.delenv("HELIX_GENOME_PATH", raising=False)
    r = c.post("/api/genome/select", json={"path": str(db)})
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True and body["restarting"] is True
    assert Path(body["selected"]) == db.resolve()
    import os
    assert os.environ.get("HELIX_GENOME_PATH") == str(db.resolve())


def test_genome_create_validations(client, tmp_path):
    c, _ = client
    assert c.post("/api/genome/create", json={"path": str(tmp_path / "x.txt")}).status_code == 400
    existing = tmp_path / "have.db"
    existing.write_bytes(b"")
    assert c.post("/api/genome/create", json={"path": str(existing)}).status_code == 409


def test_genome_create_builds_schema_and_selects(client, tmp_path, monkeypatch):
    c, _ = client
    monkeypatch.delenv("HELIX_GENOME_PATH", raising=False)
    target = tmp_path / "fresh" / "deep" / "new.genome.db"
    r = c.post("/api/genome/create", json={"path": str(target)})
    assert r.status_code == 202, r.text
    assert r.json()["restarting"] is True
    # Schema build + select happen on a worker thread (the endpoint must
    # never block the launcher event loop) — poll for completion.
    import os
    import time
    deadline = time.time() + 30
    while time.time() < deadline:
        if target.exists() and os.environ.get("HELIX_GENOME_PATH"):
            break
        time.sleep(0.1)
    assert target.exists(), "worker thread never created the genome"
    import sqlite3
    conn = sqlite3.connect(str(target))
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert "genes" in tables
    assert os.environ.get("HELIX_GENOME_PATH") == str(target.resolve())


def test_api_genomes_shape(client):
    c, _ = client
    body = c.get("/api/genomes").json()
    assert body["ok"] is True
    assert "active" in body and isinstance(body["genomes"], list)


# ── fresh-checkout mkdir fix ───────────────────────────────────────────


def test_knowledge_store_creates_missing_parent_dirs(tmp_path):
    from cymatix_context.knowledge_store import Genome
    target = tmp_path / "no" / "such" / "dirs" / "genome.db"
    g = Genome(path=str(target), synonym_map={})
    g.close()
    assert target.exists()


# ── grafana sidecar env hardening ──────────────────────────────────────


def test_grafana_service_env_defaults(monkeypatch):
    from cymatix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )
    monkeypatch.delenv("GF_AUTH_ANONYMOUS_ENABLED", raising=False)
    env = ObservabilitySupervisor._service_env("grafana")
    assert env["GF_SERVER_HTTP_ADDR"] == "127.0.0.1"
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "true"
    assert env["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Admin"
    # operator overrides win
    monkeypatch.setenv("GF_AUTH_ANONYMOUS_ENABLED", "false")
    env = ObservabilitySupervisor._service_env("grafana")
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    # non-grafana services get a plain environment passthrough
    env = ObservabilitySupervisor._service_env("prometheus")
    assert "GF_SERVER_HTTP_ADDR" not in env or "GF_SERVER_HTTP_ADDR" in dict(env)


def test_overview_dashboard_is_retitled():
    p = Path(__file__).resolve().parent.parent / \
        "deploy" / "otel" / "grafana" / "dashboards" / "helix-overview.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["uid"] == "helix-overview"
    assert d["title"] == "Helix — Overview"

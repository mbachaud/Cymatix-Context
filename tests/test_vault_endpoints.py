"""Tests for /export/obsidian + /vault/status + /vault/trace endpoints."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cymatix_context.config import HelixConfig, VaultConfig, VaultTracesConfig
from cymatix_context.server import create_app


@pytest.fixture
def app(tmp_path: Path):
    cfg = HelixConfig()
    cfg.vault = VaultConfig(
        enabled=True, path=str(tmp_path / "vault"),
        traces=VaultTracesConfig(),
    )
    cfg.genome.path = ":memory:"
    a = create_app(cfg)
    yield a


def test_vault_status_endpoint(app):
    with TestClient(app) as c:
        r = c.get("/vault/status")
        assert r.status_code == 200
        body = r.json()
        assert "enabled" in body


def test_export_obsidian_triggers_full(app):
    with TestClient(app) as c:
        r = c.post("/export/obsidian", json={"full": True})
        assert r.status_code == 200
        body = r.json()
        assert "genes_exported" in body


def test_vault_trace_writes_file(app, tmp_path):
    with TestClient(app) as c:
        r = c.post("/vault/trace", json={
            "request_id": "abc12345",
            "trigger_reason": "manual",
            "total_latency_ms": 1234,
            "health_status": "aligned",
            "stage_timing_ms": {"extract": 1},
            "fingerprint_route": "",
            "foveated_ranks": "",
            "final_genes": [],
        })
        assert r.status_code == 200
        body = r.json()
        assert "path" in body
        assert Path(body["path"]).exists()


def test_pin_and_unpin_round_trip(app, tmp_path):
    with TestClient(app) as c:
        r = c.post("/vault/trace", json={
            "request_id": "tobepin",
            "trigger_reason": "manual",
            "total_latency_ms": 0,
            "health_status": "aligned",
            "stage_timing_ms": {},
            "fingerprint_route": "", "foveated_ranks": "",
            "final_genes": [],
        })
        assert r.status_code == 200
        r2 = c.post("/vault/traces/tobepin/pin")
        assert r2.status_code == 200
        r3 = c.post("/vault/traces/tobepin/unpin")
        assert r3.status_code == 200

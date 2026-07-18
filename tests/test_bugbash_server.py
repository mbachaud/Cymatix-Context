"""Regression tests for the 2026-07-18 server/API bugbash.

BUG-1  /bridge/signal path traversal      — write_signal containment
BUG-2  /export/obsidian blocks event loop — export offloaded to a thread
BUG-3  /ingest 500s on malformed input    — 4xx validation
BUG-4  /vault/trace missing disabled gate — mirrors pin/unpin guard
BUG-5  HelixSession.query(k=...) ignored  — k forwarded as max_genes cap

(BUG-6, the scorerift relevance-probe shape claim, is a false positive:
/context returns a dict and check_relevance already consumes it — pinned
by tests/test_integration.py::TestCheckRelevanceShape.)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_client


# ── BUG-1: /bridge/signal path traversal ─────────────────────────────


def _redirect_bridge(client, tmp_path: Path):
    """Point the app's bridge at an isolated signals dir under tmp_path."""
    bridge = client.app.state.bridge
    bridge.shared_dir = tmp_path
    bridge.signals = tmp_path / "signals"
    bridge.signals.mkdir()
    return bridge


class TestBridgeSignalContainment:
    def test_traversal_name_rejected_with_400(self, tmp_path):
        client = make_client()
        _redirect_bridge(client, tmp_path)
        r = client.post("/bridge/signal", json={
            "name": "../escape", "data": {"x": 1},
        })
        assert r.status_code == 400
        # Nothing may be written outside signals/.
        assert not (tmp_path / "escape.json").exists()

    def test_absolute_name_rejected_with_400(self, tmp_path):
        client = make_client()
        _redirect_bridge(client, tmp_path)
        target = tmp_path / "abs_target"
        r = client.post("/bridge/signal", json={
            "name": str(target), "data": {},
        })
        assert r.status_code == 400
        assert not (tmp_path / "abs_target.json").exists()

    def test_plain_name_still_written(self, tmp_path):
        client = make_client()
        bridge = _redirect_bridge(client, tmp_path)
        r = client.post("/bridge/signal", json={
            "name": "ingesting", "data": {"files": 3},
        })
        assert r.status_code == 200
        assert (bridge.signals / "ingesting.json").exists()

    def test_non_dict_data_rejected_with_400(self, tmp_path):
        client = make_client()
        _redirect_bridge(client, tmp_path)
        r = client.post("/bridge/signal", json={
            "name": "ok", "data": ["not", "a", "dict"],
        })
        assert r.status_code == 400

    def test_write_signal_rejects_traversal_direct(self, tmp_path):
        from helix_context.bridge import AgentBridge
        b = AgentBridge(shared_dir=str(tmp_path / "shared"))
        with pytest.raises(ValueError):
            b.write_signal("../evil", {})
        with pytest.raises(ValueError):
            b.write_signal("..\\evil", {})
        assert not (tmp_path / "shared" / "evil.json").exists()

    def test_read_and_clear_signal_tolerate_bad_names(self, tmp_path):
        from helix_context.bridge import AgentBridge
        b = AgentBridge(shared_dir=str(tmp_path / "shared"))
        assert b.read_signal("../evil") is None
        b.clear_signal("../evil")  # must not raise


# ── BUG-2: /export/obsidian must not block the event loop ────────────


class _ProbeVault:
    """Records whether the export ran on the event loop thread."""

    def __init__(self):
        self.saw_running_loop = None

    def _probe(self):
        try:
            asyncio.get_running_loop()
            self.saw_running_loop = True   # sync export ON the loop = bug
        except RuntimeError:
            self.saw_running_loop = False  # worker thread = fixed
        return {"genes_exported": 0, "elapsed_seconds": 0, "errors": 0}

    def full_export(self):
        return self._probe()

    def incremental_export(self):
        return self._probe()


class TestExportObsidianOffLoop:
    def test_full_export_runs_off_event_loop(self):
        client = make_client()
        probe = _ProbeVault()
        client.app.state.vault = probe
        r = client.post("/export/obsidian", json={"full": True})
        assert r.status_code == 200
        assert probe.saw_running_loop is False

    def test_incremental_export_runs_off_event_loop(self):
        client = make_client()
        probe = _ProbeVault()
        client.app.state.vault = probe
        r = client.post("/export/obsidian", json={})
        assert r.status_code == 200
        assert probe.saw_running_loop is False


# ── BUG-3: /ingest must 4xx on malformed input, not 500 ──────────────


class TestIngestValidation:
    @pytest.fixture
    def client(self):
        inner = make_client()
        # Surface handler exceptions as 500 responses instead of raising.
        return TestClient(inner.app, raise_server_exceptions=False)

    def test_malformed_json_returns_400(self, client):
        r = client.post(
            "/ingest",
            content=b"{not valid json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400

    def test_non_object_body_returns_400(self, client):
        r = client.post("/ingest", json=["a", "list"])
        assert r.status_code == 400

    def test_non_string_content_dict_returns_400(self, client):
        r = client.post("/ingest", json={"content": {"nested": "dict"}})
        assert r.status_code == 400

    def test_non_string_content_int_returns_400(self, client):
        r = client.post("/ingest", json={"content": 123})
        assert r.status_code == 400


# ── BUG-4: /vault/trace disabled-vault guard ─────────────────────────


class TestVaultTraceDisabledGuard:
    def test_trace_on_disabled_vault_does_not_500(self):
        inner = make_client()  # default config: vault disabled, not started
        client = TestClient(inner.app, raise_server_exceptions=False)
        r = client.post("/vault/trace", json={
            "request_id": "deadbeef",
            "trigger_reason": "manual",
            "total_latency_ms": 1,
            "health_status": "aligned",
            "stage_timing_ms": {},
            "fingerprint_route": "",
            "foveated_ranks": "",
            "final_genes": [],
        })
        assert r.status_code == 200
        body = r.json()
        assert body == {"ok": False, "error": "vault disabled"}


# ── BUG-5: HelixSession.query(k=...) must be honored ─────────────────


class _StubManager:
    """Records build_context kwargs; returns a minimal ContextWindow."""

    def __init__(self):
        self.calls = []

    def build_context(self, **kwargs):
        self.calls.append(kwargs)
        from helix_context.schemas import ContextWindow
        return ContextWindow(
            ribosome_prompt="",
            expressed_context="ctx",
            expressed_gene_ids=["g1", "g2"],
            total_estimated_tokens=2,
        )


class TestSessionQueryK:
    def test_query_forwards_k_as_max_genes(self):
        from helix_context.api import HelixSession
        mgr = _StubManager()
        sess = HelixSession(mgr, session_id="sess-bugbash")
        sess.query("what is the port?", k=3)
        assert mgr.calls, "build_context was never called"
        assert mgr.calls[0].get("max_genes") == 3

    def test_query_without_k_passes_none(self):
        from helix_context.api import HelixSession
        mgr = _StubManager()
        sess = HelixSession(mgr, session_id="sess-bugbash-2")
        sess.query("what is the port?")
        assert mgr.calls[0].get("max_genes", "missing") is None

    def test_build_context_max_genes_override_caps_documents(self):
        client = make_client()
        helix = client.app.state.helix
        for i in range(4):
            helix.ingest(
                f"needle document {i}: the flux capacitor voltage is {i}00 volts",
                "text",
                {"path": f"/doc{i}.txt"},
            )
        window = helix.build_context(
            "flux capacitor voltage", read_only=True, max_genes=1,
        )
        assert len(window.expressed_gene_ids) <= 1

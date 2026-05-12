"""Tests for helix_context.mcp_server helper behavior."""

import pytest

from helix_context.mcp_server import (
    _default_ingest_identity,
    _normalize_health_payload,
    _unwrap_context_list,
)


@pytest.fixture
def mock_bridge(monkeypatch):
    class _MockBridge:
        register_participant_calls = []
        announce_calls = []
        _participant_id = "mock-participant-id"

        def __init__(self, *args, **kwargs):
            pass

        def register_participant(self, **kwargs):
            type(self).register_participant_calls.append(kwargs)
            return "mock-participant-id"

        @classmethod
        def announce(cls, model_id, ide_override=None):
            cls.announce_calls.append({
                "model_id": model_id,
                "ide_override": ide_override,
            })
            return True

    monkeypatch.setattr("helix_context.bridge.AgentBridge", _MockBridge)
    _MockBridge.register_participant_calls = []  # reset between tests
    _MockBridge.announce_calls = []
    yield _MockBridge


class TestNormalizeHealthPayload:
    def test_unreachable_becomes_unavailable(self):
        payload = {
            "_error": "helix unreachable",
            "_detail": "connection refused",
        }
        result = _normalize_health_payload(payload)
        assert result["availability"] == "unavailable"
        assert "helix-launcher" in result["next_action"]
        assert result["server"] == payload

    def test_ok_payload_becomes_available(self):
        payload = {
            "status": "ok",
            "genes": 42,
            "ribosome": "mock",
        }
        result = _normalize_health_payload(payload)
        assert result["availability"] == "available"
        assert "helix_context" in result["next_action"]

    def test_empty_genome_changes_next_action(self):
        payload = {
            "status": "ok",
            "genes": 0,
            "ribosome": "mock",
        }
        result = _normalize_health_payload(payload)
        assert result["availability"] == "available"
        assert "genome is empty" in result["next_action"]

    def test_http_error_becomes_degraded(self):
        payload = {
            "_error": "HTTP 500",
            "_detail": "boom",
        }
        result = _normalize_health_payload(payload)
        assert result["availability"] == "degraded"
        assert "restart" in result["next_action"].lower()

    def test_degraded_payload_preserves_server_message(self):
        payload = {
            "status": "degraded",
            "message": "Upstream model server is unreachable.",
            "checks": {"upstream_ready": False},
        }
        result = _normalize_health_payload(payload)
        assert result["availability"] == "degraded"
        assert "upstream model server is unreachable" in result["message"].lower()


class TestDefaultIngestIdentity:
    def test_prefers_explicit_full_identity_env(self, monkeypatch):
        monkeypatch.setenv("HELIX_ORG", "SwiftWing")
        monkeypatch.setenv("HELIX_DEVICE", "Wing-21")
        monkeypatch.setenv("HELIX_USER", "Max")
        monkeypatch.setenv("HELIX_AGENT", "Laude")
        monkeypatch.setenv("HELIX_AGENT_KIND", "Claude-Code")
        monkeypatch.setenv("HELIX_MCP_HANDLE", "ignored-session")
        monkeypatch.setenv("HELIX_MCP_HOST", "ignored-host")

        result = _default_ingest_identity()

        assert result == {
            "org_id": "swiftwing",
            "party_id": "wing-21",
            "participant_handle": "max",
            "agent_handle": "laude",
            "agent_kind": "claude-code",
        }

    def test_falls_back_to_session_presence_env(self, monkeypatch):
        monkeypatch.delenv("HELIX_ORG", raising=False)
        monkeypatch.delenv("HELIX_DEVICE", raising=False)
        monkeypatch.delenv("HELIX_USER", raising=False)
        monkeypatch.delenv("HELIX_AGENT", raising=False)
        monkeypatch.delenv("HELIX_AGENT_KIND", raising=False)
        monkeypatch.setenv("HELIX_PARTY_ID", "swift_wing21")
        monkeypatch.setenv("HELIX_MCP_HANDLE", "laude")
        monkeypatch.setenv("HELIX_MCP_HOST", "claude-code")

        result = _default_ingest_identity()

        assert result == {
            "party_id": "swift_wing21",
            "agent_handle": "laude",
            "agent_kind": "claude-code",
        }


class TestRegisterWithRegistry:
    def test_register_with_registry_sends_env_vendor_host(self, monkeypatch, mock_bridge):
        """_register_with_registry reads HELIX_AGENT_KIND and HELIX_MCP_HOST
        from env and forwards them to AgentBridge.register_participant."""
        monkeypatch.setenv("HELIX_MCP_HANDLE", "laude")
        monkeypatch.setenv("HELIX_PARTY_ID", "swift_wing21")
        monkeypatch.setenv("HELIX_AGENT_KIND", "claude-code")
        monkeypatch.setenv("HELIX_MCP_HOST", "vscode")

        from helix_context import mcp_server
        mcp_server._register_with_registry()

        call = mock_bridge.register_participant_calls[-1]
        assert call["agent_kind"] == "claude-code"
        assert call["mcp_host"] == "vscode"
        assert call["handle"] == "laude"

    def test_register_with_registry_omits_unset_env(self, monkeypatch, mock_bridge):
        """If HELIX_AGENT_KIND is unset, registration sends None (not 'unknown')."""
        monkeypatch.delenv("HELIX_AGENT_KIND", raising=False)
        monkeypatch.setenv("HELIX_MCP_HOST", "antigravity")
        monkeypatch.setenv("HELIX_MCP_HANDLE", "raude")
        monkeypatch.setenv("HELIX_PARTY_ID", "party_test")

        from helix_context import mcp_server
        mcp_server._register_with_registry()

        call = mock_bridge.register_participant_calls[-1]
        assert call["agent_kind"] is None
        assert call["mcp_host"] == "antigravity"


def test_register_with_registry_calls_detect_ide(monkeypatch, mock_bridge):
    """_register_with_registry calls detect_ide() and forwards both fields."""
    monkeypatch.setenv("HELIX_MCP_HANDLE", "laude")
    monkeypatch.setenv("HELIX_PARTY_ID", "swift_wing21")
    monkeypatch.delenv("HELIX_MCP_HOST", raising=False)
    monkeypatch.setenv("VSCODE_PID", "9999")

    from helix_context import mcp_server
    mcp_server._register_with_registry()

    call = mock_bridge.register_participant_calls[-1]
    assert call["ide_detected"] == "vscode"
    assert call["ide_detection_via"] == "env:VSCODE_PID"


def test_register_with_registry_no_match_sends_none(monkeypatch, mock_bridge):
    """When fingerprint chain has no signal, ide_detected is None and via is no_match."""
    monkeypatch.setenv("HELIX_MCP_HANDLE", "laude")
    monkeypatch.setenv("HELIX_PARTY_ID", "swift_wing21")
    monkeypatch.delenv("HELIX_MCP_HOST", raising=False)
    monkeypatch.delenv("VSCODE_PID", raising=False)
    monkeypatch.delenv("CURSOR_TRACE_ID", raising=False)

    from helix_context import mcp_server
    mcp_server._register_with_registry()

    call = mock_bridge.register_participant_calls[-1]
    assert call["ide_detected"] is None
    assert call["ide_detection_via"] == "no_match"


class TestUnwrapContextList:
    """The MCP ``helix_context`` tool declares ``Dict[str, Any]`` but
    ``POST /context`` returns the Continue HTTP context-provider list
    shape. _unwrap_context_list bridges the two without breaking the
    HTTP layer for Continue IDE."""

    def test_single_entry_list_unwraps_to_dict(self):
        out = _unwrap_context_list(
            [{"name": "helix", "description": "...", "content": "x"}]
        )
        assert isinstance(out, dict)
        assert out["name"] == "helix"

    def test_error_envelope_dict_passes_through(self):
        envelope = {"_error": "helix unreachable", "_detail": "connection refused"}
        assert _unwrap_context_list(envelope) is envelope

    def test_raw_envelope_dict_passes_through(self):
        envelope = {"_raw": "not-json bytes"}
        assert _unwrap_context_list(envelope) is envelope

    def test_unexpected_multi_entry_list_wrapped_with_diagnostic(self):
        out = _unwrap_context_list([{"a": 1}, {"b": 2}])
        assert isinstance(out, dict)
        assert out["items"] == [{"a": 1}, {"b": 2}]
        assert "_note" in out

    def test_empty_list_wrapped_with_diagnostic(self):
        out = _unwrap_context_list([])
        assert isinstance(out, dict)
        assert out["items"] == []


def test_helix_context_unwraps_continue_list_shape(monkeypatch):
    """End-to-end: helix_context tool returns a flat dict even though
    /context responds with the Continue list shape. This is the failure
    the AI-user feedback hit on origin/master@93deaf2."""
    from helix_context import mcp_server

    captured: dict = {}

    def _fake_http(method, path, body=None):
        captured["call"] = (method, path, body)
        # Mirror what the real /context endpoint returns — a single-entry
        # list whose entry is the Continue context-provider dict.
        return [{
            "name": "helix",
            "description": "helix context",
            "content": "<helix>example</helix>",
        }]

    monkeypatch.setattr(mcp_server, "_http", _fake_http)
    out = mcp_server.helix_context("what does the splice step do?")
    assert isinstance(out, dict), f"expected dict, got {type(out).__name__}: {out!r}"
    assert out["content"] == "<helix>example</helix>"
    # Verify the call was made with the expected shape so we don't
    # accidentally regress the body payload.
    method, path, body = captured["call"]
    assert (method, path) == ("POST", "/context")
    assert body["query"] == "what does the splice step do?"


def test_helix_announce_tool_calls_bridge_announce(monkeypatch, mock_bridge):
    """The helix_announce MCP tool delegates to AgentBridge.announce()."""
    from helix_context import mcp_server
    # Force the module to think it's registered so helix_announce proceeds
    mcp_server._registered_bridge = mock_bridge
    result = mcp_server.helix_announce(
        model_id="claude-opus-4-7",
        ide_override=None,
    )
    call = mock_bridge.announce_calls[-1]
    assert call["model_id"] == "claude-opus-4-7"
    assert call["ide_override"] is None

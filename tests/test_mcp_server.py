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


def test_shim_dispatches_to_main_when_invoked_as_module():
    """``python -m helix_context.mcp_server`` must call main() — otherwise
    the spawned MCP subprocess imports the real module and exits silently,
    and the MCP host reports "Connection closed" within ~2s of spawn.

    This was the actual root cause of the 2026-05-20 bench finding that
    helix-context MCP never loaded via claude -p, masked by a separate
    register-with-registry crash (covered by the test above). The shim at
    helix_context/mcp_server.py originally only ran the rebind line, so
    `python -m helix_context.mcp_server` exited immediately after import.

    We spawn ``python -m helix_context.mcp_server`` as a subprocess with a
    minimal HELIX_MCP_LOG_LEVEL env var, send EOF on stdin so the server
    shuts down immediately after entering mcp.run(), and assert the
    expected "helix-mcp starting" log line appears on stderr. If the shim
    didn't dispatch to main(), no log line ever fires.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "helix_context.mcp_server"],
        input=b"",  # close stdin immediately → mcp.run() exits after init
        capture_output=True,
        timeout=15,
        env={
            **os.environ,
            "HELIX_MCP_LOG_LEVEL": "INFO",
            # Point at unreachable port so registry handshake fails fast;
            # the wrap-in-try/except fix ensures main() still reaches
            # mcp.run() which then exits on stdin-EOF.
            "HELIX_MCP_URL": "http://127.0.0.1:1",
        },
    )
    stderr_text = proc.stderr.decode("utf-8", errors="replace")
    assert "helix-mcp starting" in stderr_text, (
        "main() never ran — `python -m helix_context.mcp_server` exited "
        "without calling the real module's main(). The shim must dispatch "
        f"to main() under __main__. stderr was:\n{stderr_text[:600]}"
    )


import os  # noqa: E402 — used by the subprocess test above


def test_main_reaches_mcp_run_before_register_completes(monkeypatch):
    """``main()`` must reach ``mcp.run()`` without waiting for
    ``_register_with_registry()`` to complete. Otherwise the MCP host's
    stdio-handshake window (claude -p closes spawn at ~4s on Windows
    even if its overall timeout is 10s) elapses before mcp.run() reads
    the first initialize message, and the host reports Connection closed.

    Verified by: simulate _register_with_registry() taking 5s; assert
    mcp.run() entered within 1s of main() being called.
    """
    import time
    from helix_context import mcp_server

    register_called_at = []
    run_called_at = []

    def _slow_register():
        register_called_at.append(time.perf_counter())
        time.sleep(5)  # simulate slow HTTP / connection refused retry

    def _fake_run():
        run_called_at.append(time.perf_counter())

    monkeypatch.setattr(mcp_server, "_register_with_registry", _slow_register)
    monkeypatch.setattr(mcp_server.mcp, "run", _fake_run)

    t0 = time.perf_counter()
    mcp_server.main()
    elapsed_to_run = run_called_at[0] - t0

    assert elapsed_to_run < 1.0, (
        f"main() took {elapsed_to_run:.2f}s to reach mcp.run() — should be "
        "near-instant since registration must run in a background thread. "
        "MCP hosts close the spawn around 4s on Windows."
    )


def test_main_survives_register_with_registry_crash(monkeypatch):
    """``main()`` must reach ``mcp.run()`` even if ``_register_with_registry``
    raises — e.g. when the helix HTTP endpoint is unreachable and the
    AgentBridge construction or register_participant call raises.

    This is the regression that caused ``claude -p --mcp-config X.json``
    to fail the MCP stdio handshake with "Connection closed" within ~2s
    on Windows during the 2026-05-20 bench debugging. ``_register_with_registry``
    was called bare from ``main()``, so any exception during registration
    propagated out before ``mcp.run()`` was entered, killing the spawned
    helix-mcp subprocess before it could complete the MCP handshake.

    Fix: wrap the ``_register_with_registry()`` call in ``main()`` in
    try/except so registry failure becomes a logged warning and the MCP
    server still starts.
    """
    from helix_context import mcp_server

    mcp_ran = []

    def _exploding_register():
        raise RuntimeError(
            "simulated registry crash (e.g. helix unreachable)"
        )

    def _fake_mcp_run():
        mcp_ran.append(True)

    monkeypatch.setattr(mcp_server, "_register_with_registry", _exploding_register)
    monkeypatch.setattr(mcp_server.mcp, "run", _fake_mcp_run)

    # main() must not propagate the RuntimeError from _register_with_registry
    mcp_server.main()
    assert mcp_ran == [True], (
        "main() did not reach mcp.run() after registry crash — the "
        "subprocess would die before MCP handshake. Wrap "
        "_register_with_registry() in try/except inside main()."
    )

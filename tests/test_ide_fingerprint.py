"""Env-var fingerprint chain for detecting which IDE/CLI spawned the
MCP adapter.

Exercises:
- HELIX_MCP_HOST explicit override (highest priority, "explicit:..." via)
- VSCODE_PID present → ("vscode", "env:VSCODE_PID")
- CURSOR_TRACE_ID present → ("cursor", "env:CURSOR_TRACE_ID")
- nothing matches → (None, "no_match")
- "unknown" sentinel for HELIX_MCP_HOST falls through, doesn't trigger explicit branch
"""
import pytest

from cymatix_context.launcher.ide_fingerprint import detect_ide


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip every env var the chain might read so each test is isolated."""
    for key in (
        "HELIX_MCP_HOST",
        "VSCODE_PID",
        "VSCODE_IPC_HOOK",
        "CURSOR_TRACE_ID",
        "TERM_PROGRAM",
    ):
        monkeypatch.delenv(key, raising=False)


def test_explicit_helix_mcp_host_wins(monkeypatch):
    monkeypatch.setenv("HELIX_MCP_HOST", "claude-code")
    monkeypatch.setenv("VSCODE_PID", "1234")  # would otherwise win
    assert detect_ide() == ("claude-code", "explicit:HELIX_MCP_HOST")


def test_helix_mcp_host_unknown_sentinel_falls_through(monkeypatch):
    """HELIX_MCP_HOST=unknown is the legacy default — treat as unset."""
    monkeypatch.setenv("HELIX_MCP_HOST", "unknown")
    monkeypatch.setenv("VSCODE_PID", "1234")
    assert detect_ide() == ("vscode", "env:VSCODE_PID")


def test_vscode_pid_detects_vscode(monkeypatch):
    monkeypatch.setenv("VSCODE_PID", "1234")
    assert detect_ide() == ("vscode", "env:VSCODE_PID")


def test_cursor_trace_id_detects_cursor(monkeypatch):
    monkeypatch.setenv("CURSOR_TRACE_ID", "abc-123")
    assert detect_ide() == ("cursor", "env:CURSOR_TRACE_ID")


def test_vscode_priority_over_cursor(monkeypatch):
    """If both env vars are set (shouldn't happen in practice but make the
    behavior deterministic), VSCODE_PID wins because it's earlier in the chain."""
    monkeypatch.setenv("VSCODE_PID", "1234")
    monkeypatch.setenv("CURSOR_TRACE_ID", "abc-123")
    assert detect_ide() == ("vscode", "env:VSCODE_PID")


def test_no_match_returns_none(monkeypatch):
    """All env vars stripped — no signal."""
    assert detect_ide() == (None, "no_match")


def test_empty_string_env_treated_as_unset(monkeypatch):
    """An empty VSCODE_PID is not a real signal."""
    monkeypatch.setenv("VSCODE_PID", "")
    assert detect_ide() == (None, "no_match")

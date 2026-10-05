"""Agent badge labels for the dashboard.

The dashboard carries no vendor, host or model names of its own. Whatever an
agent announced (``agent_kind``, ``mcp_host``) is shown exactly as received, so
a tool the code has never heard of appears the first time it connects.

Exercises:
- announced values echo verbatim (no pretty-name map)
- the "unknown" marker on the host axis counts as missing
- compose_label with both / vendor-only / host-only / neither
"""
import pytest

from cymatix_context.launcher.host_labels import (
    vendor_pretty,
    host_pretty,
    compose_label,
)


@pytest.mark.parametrize("value", ["claude-code", "codex", "gemini", "acme-bot"])
def test_vendor_echoes_verbatim(value):
    assert vendor_pretty(value) == value


def test_vendor_none():
    assert vendor_pretty(None) is None
    assert vendor_pretty("") is None


@pytest.mark.parametrize("value", ["vscode", "antigravity", "gemini-cli", "zed"])
def test_host_echoes_verbatim(value):
    assert host_pretty(value) == value


def test_host_unknown_marker_returns_none():
    """The MCP server defaults CYMATIX_MCP_HOST to 'unknown' — we don't
    want a meaningless 'unknown' chip cluttering the dashboard."""
    assert host_pretty("unknown") is None
    assert host_pretty(None) is None
    assert host_pretty("") is None


def test_compose_label_both():
    assert compose_label("claude-code", "vscode") == "claude-code + vscode"


def test_compose_label_vendor_only():
    assert compose_label("claude-code", None) == "claude-code"


def test_compose_label_host_only():
    assert compose_label(None, "antigravity") == "antigravity"


def test_compose_label_neither_returns_none():
    assert compose_label(None, None) is None
    assert compose_label("", "") is None


def test_compose_label_dedupes_when_vendor_equals_host():
    """Common case: CYMATIX_AGENT_KIND=codex and CYMATIX_MCP_HOST=codex.
    Render a single chip, not 'codex + codex'."""
    assert compose_label("codex", "codex") == "codex"


def test_compose_label_dedupes_case_insensitive():
    assert compose_label("Codex", "codex") == "Codex"


@pytest.mark.parametrize(
    ("agent_kind", "mcp_host", "expected"),
    [
        ("claude-code", "claude-code", "claude-code"),
        ("gemini", "gemini-cli", "gemini + gemini-cli"),
        ("gemini", "antigravity", "gemini + antigravity"),
    ],
)
def test_compose_label_for_host_profiles(agent_kind, mcp_host, expected):
    assert compose_label(agent_kind, mcp_host) == expected

"""0.8.0 MCP tool-surface rename: canonical ``cymatix_*`` names with
``helix_*`` compat aliases.

The 0.8.0 changelog shipped "MCP server identifies as cymatix" but only
the flagship retrieval tool was actually renamed — the other 23 tools
still registered as ``helix_*``. Canonical names are now ``cymatix_*``
across the board; the old names stay callable as deprecated aliases for
the deprecation window (default ON, disable with ``CYMATIX_MCP_COMPAT=0``
/ ``HELIX_MCP_COMPAT=0``) so existing host configs keep working.
"""
from __future__ import annotations

import pytest

from cymatix_context.mcp import mcp_server


# The full pre-rename helix_* surface (all 24 tools — including
# helix_context, whose alias the first rename pass dropped outright).
# Every one must have a canonical cymatix_* function and a compat-alias
# mapping.
_OLD_NAMES = [
    "helix_context",
    "helix_context_packet",
    "helix_refresh_targets",
    "helix_stats",
    "helix_ingest",
    "helix_resonance",
    "helix_hitl_emit",
    "helix_hitl_recent",
    "helix_sessions_list",
    "helix_session_recent",
    "helix_consolidate",
    "helix_health",
    "helix_swap_db",
    "helix_announce",
    "helix_metrics_tokens",
    "helix_bridge_status",
    "helix_gene_get",
    "helix_neighbors",
    "helix_splice_preview",
    "helix_fingerprint",
    "helix_document_get",
    "helix_document_query",
    "helix_document_preview",
    "helix_document_fingerprint",
]


def test_canonical_functions_exist_for_every_old_name():
    for old in _OLD_NAMES:
        canonical = "cymatix_" + old[len("helix_"):]
        assert callable(getattr(mcp_server, canonical, None)), (
            f"missing canonical tool function {canonical}"
        )


def test_alias_map_covers_exactly_the_old_surface():
    assert set(mcp_server._CANONICAL_RENAMES.keys()) == set(_OLD_NAMES)
    for old, canonical in mcp_server._CANONICAL_RENAMES.items():
        assert canonical == "cymatix_" + old[len("helix_"):]


def test_core_tools_are_canonical_names():
    assert mcp_server._MCP_CORE_TOOLS == frozenset({
        "cymatix_context",
        "cymatix_context_packet",
        "cymatix_ingest",
        "cymatix_health",
        "cymatix_sessions_list",
    })


def test_compat_enabled_by_default(monkeypatch):
    monkeypatch.delenv("HELIX_MCP_COMPAT", raising=False)
    assert mcp_server._mcp_compat_enabled() is True


@pytest.mark.parametrize("value,expected", [
    ("0", False), ("false", False), ("off", False),
    ("1", True), ("true", True),
])
def test_compat_env_flag(monkeypatch, value, expected):
    monkeypatch.setenv("HELIX_MCP_COMPAT", value)
    assert mcp_server._mcp_compat_enabled() is expected


def test_register_compat_aliases_registers_old_names(monkeypatch):
    monkeypatch.delenv("HELIX_MCP_COMPAT", raising=False)
    from mcp.server.fastmcp import FastMCP
    fresh = FastMCP("test")
    registered = mcp_server._register_compat_aliases(fresh)
    assert set(registered) == set(_OLD_NAMES)
    tool_names = set(fresh._tool_manager._tools.keys())
    assert set(_OLD_NAMES) <= tool_names


def test_register_compat_aliases_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("HELIX_MCP_COMPAT", "0")
    from mcp.server.fastmcp import FastMCP
    fresh = FastMCP("test")
    assert mcp_server._register_compat_aliases(fresh) == []
    assert not fresh._tool_manager._tools


def test_effective_core_includes_aliases_only_when_compat(monkeypatch):
    monkeypatch.delenv("HELIX_MCP_COMPAT", raising=False)
    core = mcp_server._effective_core_tools()
    assert "cymatix_ingest" in core and "helix_ingest" in core
    assert "helix_context_packet" in core  # alias of a core tool
    assert "helix_stats" not in core      # alias of a non-core tool

    monkeypatch.setenv("HELIX_MCP_COMPAT", "0")
    core = mcp_server._effective_core_tools()
    assert core == mcp_server._MCP_CORE_TOOLS

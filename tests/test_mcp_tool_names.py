"""MCP tool-surface contract (0.8.5 clean break).

The compat era is over: the MCP server registers **only** ``cymatix_*``
tools. There are no ``helix_*`` aliases and no alias machinery
(``_CANONICAL_RENAMES`` / ``_register_compat_aliases`` /
``_mcp_compat_enabled``) left to test.

By default the server prunes to the lean 6-tool core surface (issue #219);
the full ~24-tool surface is exposed with ``CYMATIX_MCP_FULL=1``. Both
surfaces must be 100%% ``cymatix_*`` with zero ``helix`` in any tool name.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytest.importorskip(
    "mcp.server.mcpserver",
    reason="mcp SDK extra not installed, or too old to expose "
           "mcp.server.mcpserver (mcp 2.x home of MCPServer — see pyproject floor)",
)

from cymatix_context.mcp import mcp_server


_CORE_TOOLS = {
    "cymatix_context",
    "cymatix_context_packet",
    "cymatix_ingest",
    "cymatix_health",
    "cymatix_sessions_list",
    "cymatix_announce",
}


def _lean_tool_names() -> set[str]:
    return set(mcp_server.mcp._tool_manager._tools.keys())


def _full_tool_names() -> set[str]:
    """Enumerate the full tool surface in an isolated subprocess with
    CYMATIX_MCP_FULL=1 so we don't perturb the in-process lean server."""
    env = dict(os.environ)
    env["CYMATIX_MCP_FULL"] = "1"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from cymatix_context.mcp import mcp_server as m; "
            "print(json.dumps(sorted(m.mcp._tool_manager._tools.keys())))",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return set(json.loads(proc.stdout.strip().splitlines()[-1]))


# ── only cymatix_* tools, zero helix ────────────────────────────────


def test_lean_surface_is_all_cymatix_no_helix():
    names = _lean_tool_names()
    assert names, "no MCP tools registered"
    assert all(n.startswith("cymatix_") for n in names), names
    assert not any("helix" in n for n in names), names


def test_lean_surface_is_exactly_the_core_six():
    assert _lean_tool_names() == _CORE_TOOLS


def test_full_surface_is_all_cymatix_no_helix():
    names = _full_tool_names()
    assert len(names) > len(_CORE_TOOLS), (
        f"CYMATIX_MCP_FULL did not expand the surface: {sorted(names)}"
    )
    assert all(n.startswith("cymatix_") for n in names), sorted(names)
    assert not any("helix" in n for n in names), sorted(names)


def test_full_surface_includes_the_core_six():
    assert _CORE_TOOLS <= _full_tool_names()


# ── the 6 core tools exist as functions and are the canonical set ───


def test_core_tool_functions_exist():
    for name in _CORE_TOOLS:
        assert callable(getattr(mcp_server, name, None)), f"missing tool fn {name}"


def test_core_tools_frozenset_is_canonical():
    assert mcp_server._MCP_CORE_TOOLS == frozenset(_CORE_TOOLS)


# ── no helix_* functions, no alias machinery ────────────────────────


def test_no_helix_tool_functions_at_module_level():
    leaked = [name for name in vars(mcp_server) if name.startswith("helix_")]
    assert not leaked, f"module still defines helix_* symbols: {leaked}"


@pytest.mark.parametrize(
    "symbol",
    ["_CANONICAL_RENAMES", "_register_compat_aliases", "_mcp_compat_enabled"],
)
def test_alias_machinery_removed(symbol):
    assert not hasattr(mcp_server, symbol), (
        f"compat alias machinery {symbol} should have been removed"
    )

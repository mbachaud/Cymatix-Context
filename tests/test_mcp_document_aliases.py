"""MCP `cymatix_document_*` alias coverage (Task A5 / Rosetta Tier 2, #87).

R4 = soft-deprecation only, no removals. Three things under test:

1. ``cymatix_document_neighbors`` exists, is registered, and is a true
   pass-through equivalent of ``cymatix_neighbors`` (same call, same
   result) -- not just present under a new name.
2. All five ``cymatix_document_*`` aliases are part of the lean default
   ``_MCP_CORE_TOOLS`` surface (grown from the original 5 -- plus
   ``cymatix_announce``, added by the model-contract work on master -- to 11).
3. Every legacy bio-named tool this task touches
   (``cymatix_gene_get``, ``cymatix_splice_preview``, ``cymatix_neighbors``)
   carries a deprecation nudge on its docstring's first line, and
   ``cymatix_resonance``'s docstring uses "co-activation edges" display
   wording instead of the old "harmonic_links" term.
"""
from __future__ import annotations

import importlib
import inspect

import pytest

pytest.importorskip(
    "mcp.server.mcpserver",
    reason="mcp SDK extra not installed, or too old to expose "
           "mcp.server.mcpserver (mcp 2.x home of MCPServer — see pyproject floor)",
)

from cymatix_context.mcp import mcp_server


def _tool_doc(name: str) -> str:
    """Docstring of a tool function, independent of lean/full registration
    state -- the function object stays on the module either way."""
    fn = getattr(mcp_server, name)
    return fn.__doc__ or ""


def _lean_tool_names(monkeypatch) -> set[str]:
    """Reimport the module with CYMATIX_MCP_FULL unset so tool registration
    reflects the production default, regardless of what earlier tests (in
    this file or others) left the shared module state as."""
    monkeypatch.delenv("CYMATIX_MCP_FULL", raising=False)
    m = importlib.reload(mcp_server)
    return set(m.mcp._tool_manager._tools.keys())


@pytest.fixture(autouse=True)
def _restore_lean_profile_after(monkeypatch):
    """Leave the shared mcp_server module back in its lean production
    default after every test in this file, so other test modules that
    import it later aren't affected by a reload left mid-test."""
    yield
    monkeypatch.delenv("CYMATIX_MCP_FULL", raising=False)
    importlib.reload(mcp_server)


# ── registration + core-set membership ────────────────────────────────


def test_document_neighbors_registered():
    """Present as a callable module-level tool function."""
    assert callable(getattr(mcp_server, "cymatix_document_neighbors", None))


def test_document_neighbors_in_lean_default_surface(monkeypatch):
    tools = _lean_tool_names(monkeypatch)
    assert "cymatix_document_neighbors" in tools


def test_core_set_exposes_document_aliases():
    from cymatix_context.mcp.mcp_server import _MCP_CORE_TOOLS

    assert {
        "cymatix_document_get",
        "cymatix_document_query",
        "cymatix_document_preview",
        "cymatix_document_fingerprint",
        "cymatix_document_neighbors",
    } <= set(_MCP_CORE_TOOLS)


def test_core_set_grew_to_eleven():
    """Original 5 (issue #219 Slice 3) + cymatix_announce (model contract)
    + 5 document_* aliases (#87 R4)."""
    from cymatix_context.mcp.mcp_server import _MCP_CORE_TOOLS

    original_five = {
        "cymatix_context",
        "cymatix_context_packet",
        "cymatix_ingest",
        "cymatix_health",
        "cymatix_sessions_list",
    }
    assert original_five <= set(_MCP_CORE_TOOLS)
    assert "cymatix_announce" in _MCP_CORE_TOOLS
    assert len(_MCP_CORE_TOOLS) == 11


def test_lean_surface_matches_grown_core_set(monkeypatch):
    from cymatix_context.mcp.mcp_server import _MCP_CORE_TOOLS

    assert _lean_tool_names(monkeypatch) == set(_MCP_CORE_TOOLS)


# ── R4 deprecation nudges on legacy bio-named tools ────────────────────


@pytest.mark.parametrize(
    "legacy,canonical",
    [
        ("cymatix_gene_get", "cymatix_document_get"),
        ("cymatix_splice_preview", "cymatix_document_preview"),
        ("cymatix_neighbors", "cymatix_document_neighbors"),
    ],
)
def test_legacy_tools_carry_deprecation_nudge(legacy, canonical):
    doc = _tool_doc(legacy)
    assert "prefer cymatix_document" in doc
    first_line = doc.strip().splitlines()[0]
    assert canonical in first_line, (
        f"{legacy} docstring first line should name its canonical "
        f"replacement {canonical!r}: {first_line!r}"
    )
    assert "legacy name" in first_line


def test_resonance_docstring_uses_coactivation_display_wording():
    doc = _tool_doc("cymatix_resonance")
    assert "co-activation edges (legacy: harmonic_links)" in doc


# ── neighbors pass-through equivalence ─────────────────────────────────


def test_document_neighbors_same_signature_as_legacy():
    legacy_params = list(inspect.signature(mcp_server.cymatix_neighbors).parameters)
    alias_params = list(
        inspect.signature(mcp_server.cymatix_document_neighbors).parameters
    )
    assert alias_params == legacy_params == ["query", "k"]


def test_document_neighbors_makes_the_same_http_call_as_legacy(monkeypatch):
    calls: list[tuple[str, str, object]] = []

    def _fake_http(method, path, body=None):
        calls.append((method, path, body))
        return {"query": "splice step", "k": 3, "neighbors": [], "count": 0}

    monkeypatch.setattr(mcp_server, "_http", _fake_http)

    legacy_result = mcp_server.cymatix_neighbors("splice step", k=3)
    calls.clear()
    alias_result = mcp_server.cymatix_document_neighbors("splice step", k=3)

    assert len(calls) == 1
    method, path, body = calls[0]
    assert method == "GET"
    assert path == "/debug/neighbors?query=splice%20step&k=3"
    assert body is None
    assert alias_result == legacy_result


def test_document_neighbors_defaults_match_legacy(monkeypatch):
    calls: list[tuple[str, str, object]] = []

    def _fake_http(method, path, body=None):
        calls.append((method, path, body))
        return {}

    monkeypatch.setattr(mcp_server, "_http", _fake_http)

    mcp_server.cymatix_neighbors("query")
    mcp_server.cymatix_document_neighbors("query")

    assert calls[0] == calls[1]


# ── document_preview max_docs rename ────────────────────────────────────


def test_document_preview_accepts_max_docs_and_forwards_max_genes(monkeypatch):
    calls: list[str] = []

    def _fake_http(method, path, body=None):
        calls.append(path)
        return {"query": "q", "candidates": [], "count": 0}

    monkeypatch.setattr(mcp_server, "_http", _fake_http)
    mcp_server.cymatix_document_preview("query", max_docs=5)

    assert calls[-1] == "/debug/preview?query=query&max_genes=5"


def test_document_preview_signature_uses_max_docs_not_max_genes():
    params = inspect.signature(mcp_server.cymatix_document_preview).parameters
    assert "max_docs" in params
    assert "max_genes" not in params


def test_splice_preview_legacy_signature_keeps_max_genes():
    """The legacy tool's own parameter name is untouched -- only the
    canonical alias renamed its parameter to max_docs."""
    params = inspect.signature(mcp_server.cymatix_splice_preview).parameters
    assert "max_genes" in params
    assert "max_docs" not in params

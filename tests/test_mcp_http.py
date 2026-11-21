"""Loopback streamable-HTTP transport, ``remote-read`` profile, per-request identity."""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip(
    "mcp.server.mcpserver",
    reason="mcp SDK extra not installed, or too old to expose mcp.server.mcpserver",
)
httpx = pytest.importorskip("httpx")

from cymatix_context.cli import cmd_mcp
from cymatix_context.mcp import mcp_server

_REMOTE_READ = {
    "cymatix_context",
    "cymatix_context_packet",
    "cymatix_health",
    "cymatix_document_get",
    "cymatix_document_query",
    "cymatix_document_preview",
    "cymatix_document_fingerprint",
    "cymatix_document_neighbors",
}

_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture
def restore_tools():
    """Profiles prune the shared in-process server; put the tools back after."""
    snapshot = dict(mcp_server.mcp._tool_manager._tools)
    yield
    mcp_server.mcp._tool_manager._tools.clear()
    mcp_server.mcp._tool_manager._tools.update(snapshot)


def _rpcs(app, calls):
    """POST JSON-RPC messages ``(method, params, headers)`` in one app lifespan."""

    async def go():
        out = []
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:11441"
            ) as client:
                for rid, (method, params, headers) in enumerate(calls, 1):
                    payload = {"jsonrpc": "2.0", "id": rid, "method": method}
                    if params is not None:
                        payload["params"] = params
                    resp = await client.post(
                        "/mcp", json=payload, headers={**_HEADERS, **(headers or {})}
                    )
                    assert resp.status_code == 200, resp.text
                    out.append(resp.json())
        return out

    return asyncio.run(go())


def _rpc(app, method, params=None, headers=None):
    return _rpcs(app, [(method, params, headers)])[0]


def _names(body):
    return {t["name"] for t in body["result"]["tools"]}


def test_remote_read_profile_exact_set(restore_tools):
    mcp_server._apply_mcp_profile(mcp_server.mcp, "remote-read")
    assert set(mcp_server.mcp._tool_manager._tools) == mcp_server._REMOTE_READ_TOOLS
    assert mcp_server._REMOTE_READ_TOOLS == _REMOTE_READ


def test_remote_read_rejects_ingest_call(restore_tools, monkeypatch):
    calls = []
    monkeypatch.setattr(
        mcp_server, "_http", lambda *a, **k: calls.append(a) or {"ok": True}
    )
    mcp_server._apply_mcp_profile(mcp_server.mcp, "remote-read")
    app = mcp_server.build_http_app()

    listed, body = _rpcs(
        app,
        [
            ("tools/list", None, None),
            (
                "tools/call",
                {"name": "cymatix_ingest", "arguments": {"content": "x"}},
                None,
            ),
        ],
    )
    assert _names(listed) == _REMOTE_READ
    text = json.dumps(body).lower()
    is_error = "error" in body or body.get("result", {}).get("isError")
    assert is_error, body
    assert "unknown tool" in text or "not found" in text or "cymatix_ingest" in text
    assert calls == []


def test_http_tool_list_matches_stdio_lean(restore_tools):
    app = mcp_server.build_http_app()
    assert _names(_rpc(app, "tools/list")) == set(mcp_server._MCP_CORE_TOOLS)


def test_identity_headers_override_env(restore_tools, monkeypatch):
    monkeypatch.setenv("CYMATIX_AGENT", "a")
    seen = []
    monkeypatch.setattr(
        mcp_server, "_http", lambda m, p, body=None: seen.append(body) or {"items": []}
    )
    app = mcp_server.build_http_app()
    args = {"name": "cymatix_context", "arguments": {"query": "q"}}

    _rpcs(
        app,
        [
            ("tools/call", args, {"X-Cymatix-Agent": "  agent-x  "}),
            ("tools/call", args, None),
        ],
    )
    assert [b["agent"] for b in seen] == ["agent-x", "a"]


def test_request_identity_headers_user_party_and_cap():
    token = mcp_server._identity_headers.set(
        {"agent": "x", "user": "user-a", "party": "laptop"}
    )
    try:
        ident = mcp_server._request_identity()
    finally:
        mcp_server._identity_headers.reset(token)
    assert ident == {"agent": "x", "user": "user-a", "party": "laptop"}


def test_header_parsing_strips_and_validates(caplog):
    mcp_server._warned_identity_headers.clear()
    with caplog.at_level("WARNING", logger="cymatix.mcp"):
        parsed = mcp_server._parse_identity_headers(
            [
                (b"x-cymatix-agent", b"  a.b:c@d-1  "),
                (b"x-cymatix-user", b"   "),
                (b"x-cymatix-party", b"p" * 65),  # too long: ignored, not truncated
            ]
        )
        mcp_server._parse_identity_headers([(b"x-cymatix-party", b"bad value!")])
    assert parsed == {"agent": "a.b:c@d-1"}
    warnings = [r for r in caplog.records if "x-cymatix-party" in r.getMessage().lower()]
    assert len(warnings) == 1  # once per header name


def test_header_parsing_rejects_bad_characters():
    assert mcp_server._parse_identity_headers(
        [(b"x-cymatix-agent", b"a b"), (b"x-cymatix-user", b"x/y")]
    ) == {}


_LOOP = "http://127.0.0.1:11441"


def _statuses(app, cases):
    """Status codes for ``(headers, base_url)`` cases within one app lifespan."""

    async def go():
        out = []
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            for headers, base in cases:
                async with httpx.AsyncClient(transport=transport, base_url=base) as client:
                    resp = await client.post(
                        "/mcp",
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={**_HEADERS, **(headers or {})},
                    )
                    out.append(resp.status_code)
        return out

    return asyncio.run(go())


def test_dns_rebinding_guard_is_explicit_and_rejects(restore_tools, monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server, "_http", lambda *a, **k: calls.append(a) or {})
    app = mcp_server.build_http_app()
    assert _statuses(
        app,
        [
            ({}, _LOOP),
            ({}, "http://evil.example:11441"),
            ({"Origin": "http://evil.example"}, _LOOP),
            ({"Origin": "http://127.0.0.1:3000"}, _LOOP),
        ],
    ) == [200, 421, 403, 200]
    assert calls == []


def test_bearer_token_required_when_set(restore_tools, monkeypatch):
    monkeypatch.setenv("CYMATIX_MCP_HTTP_TOKEN", "s3cret")
    calls = []
    monkeypatch.setattr(mcp_server, "_http", lambda *a, **k: calls.append(a) or {})
    app = mcp_server.build_http_app()
    assert _statuses(
        app,
        [
            ({}, _LOOP),
            ({"Authorization": "Bearer wrong"}, _LOOP),
            ({"Authorization": "Basic s3cret"}, _LOOP),
            ({"Authorization": "Bearer s3cret"}, _LOOP),
        ],
    ) == [401, 401, 401, 200]
    assert calls == []


def test_no_token_means_open(restore_tools, monkeypatch):
    monkeypatch.delenv("CYMATIX_MCP_HTTP_TOKEN", raising=False)
    app = mcp_server.build_http_app()
    assert _statuses(app, [({}, _LOOP)]) == [200]


def test_remote_read_fails_closed_on_enumeration_error(restore_tools, monkeypatch):
    import uvicorn

    ran = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: ran.append(1))
    monkeypatch.setattr(mcp_server, "_register_with_registry", lambda: None)
    real = mcp_server.mcp._tool_manager

    class Broken:
        @property
        def _tools(self):
            raise RuntimeError("internal shape changed")

    monkeypatch.setattr(mcp_server.mcp, "_tool_manager", Broken())
    try:
        with pytest.raises(RuntimeError):
            mcp_server.serve_http(11450, "remote-read")
    finally:
        monkeypatch.setattr(mcp_server.mcp, "_tool_manager", real)
    assert ran == []


def test_remote_read_fails_closed_on_removal_error(restore_tools, monkeypatch):
    import uvicorn

    ran = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: ran.append(1))
    monkeypatch.setattr(mcp_server, "_register_with_registry", lambda: None)

    def boom(name):
        raise RuntimeError("cannot remove")

    monkeypatch.setattr(mcp_server.mcp, "remove_tool", boom)
    with pytest.raises(RuntimeError):
        mcp_server.serve_http(11450, "remote-read")
    assert ran == []


def test_explicit_profile_beats_ambient_full_env(restore_tools, monkeypatch):
    monkeypatch.setenv("CYMATIX_MCP_FULL", "1")

    def cymatix_zz_extra() -> str:
        return "x"

    mcp_server.mcp.add_tool(cymatix_zz_extra)
    assert "cymatix_zz_extra" in mcp_server.mcp._tool_manager._tools
    mcp_server._apply_mcp_profile(mcp_server.mcp, "lean")
    assert "cymatix_zz_extra" not in mcp_server.mcp._tool_manager._tools
    assert set(mcp_server.mcp._tool_manager._tools) <= set(mcp_server._MCP_CORE_TOOLS)


def test_non_loopback_host_rejected(capsys):
    assert cmd_mcp.run(["serve", "--http", "--host", "0.0.0.0"]) == 2
    assert "non-loopback MCP HTTP binds are not supported" in capsys.readouterr().err


def test_serve_requires_http_flag(capsys):
    assert cmd_mcp.run(["serve"]) == 2


def test_serve_lane_port_is_proxied_url(restore_tools, monkeypatch):
    class Lane:
        name = "bench"
        port = 11439

    monkeypatch.setattr(cmd_mcp, "_lanes", lambda: (None, [Lane()]))
    captured = {}

    def fake_serve(port, profile, **kw):
        captured.update(port=port, profile=profile, **kw)
        # requests made now must go to the lane's URL
        captured["url"] = kw["upstream_url"]

    monkeypatch.setattr(mcp_server, "serve_http", fake_serve)
    assert cmd_mcp.run(["serve", "--http", "--lane", "bench", "--port", "12000"]) == 0
    assert captured["port"] == 12000
    assert captured["profile"] == "lean"
    assert captured["url"] == "http://127.0.0.1:11439"


def test_serve_http_points_requests_at_lane(restore_tools, monkeypatch):
    import urllib.request

    seen_urls = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: seen_urls.append(req.full_url) or Resp()
    )
    monkeypatch.setattr(mcp_server, "_register_with_registry", lambda: None)
    monkeypatch.setattr(mcp_server, "CYMATIX_URL", mcp_server.CYMATIX_URL)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    mcp_server.serve_http(11450, "lean", upstream_url="http://127.0.0.1:11439")
    mcp_server._http("GET", "/health")
    assert seen_urls == ["http://127.0.0.1:11439/health"]


def _call_context_tools(monkeypatch, arguments, over_http):
    """Call cymatix_context and cymatix_document_query; return the bodies sent."""
    seen = []
    monkeypatch.setattr(
        mcp_server, "_http", lambda m, p, body=None: seen.append((p, body)) or {"items": []}
    )
    if over_http:
        app = mcp_server.build_http_app()
        _rpcs(
            app,
            [
                ("tools/call", {"name": "cymatix_context", "arguments": arguments}, None),
                ("tools/call", {"name": "cymatix_document_query", "arguments": arguments}, None),
            ],
        )
    else:
        mcp_server.cymatix_context(**arguments)
        mcp_server.cymatix_document_query(**arguments)
    return [b for _p, b in seen]


def test_http_without_session_id_ignores_delivered(restore_tools, monkeypatch):
    bodies = _call_context_tools(monkeypatch, {"query": "q"}, over_http=True)
    assert len(bodies) == 2
    for body in bodies:
        assert body["ignore_delivered"] is True
        assert "session_id" not in body


def test_http_explicit_session_id_is_ignored(restore_tools, monkeypatch):
    """Remote callers must not touch other sessions' working sets."""
    bodies = _call_context_tools(
        monkeypatch, {"query": "q", "session_id": "conv-1"}, over_http=True
    )
    assert len(bodies) == 2
    for body in bodies:
        assert body["ignore_delivered"] is True
        assert "session_id" not in body


def test_stdio_session_behaviour_unchanged(restore_tools, monkeypatch):
    bodies = _call_context_tools(monkeypatch, {"query": "q"}, over_http=False)
    for body in bodies:
        assert body["session_id"] == mcp_server.MCP_SESSION_ID
        assert "ignore_delivered" not in body


def test_stdio_default_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(mcp_server, "_register_with_registry", lambda: None)
    mcp_server.main()
    assert calls == [((), {})]

"""Integration contracts for the host-aware ``cymatix-status`` report."""

from __future__ import annotations

import email
import http.client
import http.server
import json
import io
import socket
import threading
import urllib.request
import urllib.response

import pytest

import cymatix_context.cli.cymatix_status as status_mod
from cymatix_context.cli.cymatix_status import ProbeResult


def _json_config(
    path,
    *,
    disabled: bool = False,
    server_url: str = "http://127.0.0.1:19199",
):
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "cymatix-context": {
                        "command": "python",
                        "args": ["-m", "cymatix_context.mcp_server"],
                        "disabled": disabled,
                        "env": {
                            "CYMATIX_MCP_URL": server_url,
                            "CYMATIX_MCP_HANDLE": "agent-1",
                            "CYMATIX_MCP_HOST": "antigravity",
                            "UNRELATED_SECRET": "do-not-report",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _installed_antigravity_skill(tmp_path):
    skill_file = tmp_path / ".agents" / "skills" / "cymatix-context" / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text("---\nname: cymatix-context\n---\n", encoding="utf-8")
    return skill_file


def _healthy_probes(monkeypatch, *, participants=None):
    def fake_probe(url, timeout_s=status_mod.DEFAULT_STATUS_TIMEOUT_S):
        if url.endswith("/health"):
            return ProbeResult("reachable", {"status": "ok"}, None, None)
        if url.endswith("/api/state"):
            return ProbeResult("unreachable", None, None, "not running")
        if url.endswith("/sessions?status=active"):
            return ProbeResult("reachable", {"participants": participants or []}, None, None)
        raise AssertionError(url)

    monkeypatch.setattr(status_mod, "_probe_json", fake_probe)


# The probe tests below observe what reaches a socket, not which urllib
# entry point the probe happens to call, so they hold whatever opener
# the probe is built on.

_JSON = {"Content-Type": "application/json"}


class _LoopbackServer:
    """A real HTTP server on an ephemeral loopback port.

    `answer(path)` returns `(status, headers, body)`. Every request line
    target is recorded, so a test can see exactly what reached it.
    """

    def __init__(self, answer):
        self.requests = []
        requests = self.requests

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                status, headers, body = answer(self.path)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *_args):
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False


def _unused_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _record_connections(monkeypatch, *, allow_loopback=False):
    """Record every socket connection attempt, refusing all but loopback.

    A refused attempt fails before any name resolution, so a probe that
    tries to leave the machine is seen here and goes nowhere.
    """

    attempts = []
    real_connect = socket.create_connection

    def connect(address, *args, **kwargs):
        attempts.append(address)
        if allow_loopback and address[0] == "127.0.0.1":
            return real_connect(address, *args, **kwargs)
        raise OSError("connection refused by the test")

    monkeypatch.setattr(socket, "create_connection", connect)
    return attempts


def _record_body_reads(monkeypatch):
    """Record the size of every response body read the probe makes."""

    sizes = []
    real_read = http.client.HTTPResponse.read

    def read(self, amt=None):
        sizes.append(amt)
        return real_read(self, amt)

    monkeypatch.setattr(http.client.HTTPResponse, "read", read)
    return sizes


@pytest.mark.parametrize(
    ("host", "config_path", "contents", "expected_activation", "expected_ready"),
    [
        (
            "codex",
            ".codex/config.toml",
            "[mcp_servers.cymatix-context]\n"
            'command = "python"\n'
            'args = ["-m", "cymatix_context.mcp_server"]\n'
            "enabled = false\n\n"
            "[mcp_servers.cymatix-context.env]\n"
            'CYMATIX_MCP_URL = "http://127.0.0.1:19199"\n',
            "disabled",
            False,
        ),
        (
            "gemini-cli",
            ".gemini/settings.json",
            json.dumps(
                {
                    "mcpServers": {
                        "cymatix-context": {
                            "command": "python",
                            "args": ["-m", "cymatix_context.mcp_server"],
                            "env": {"CYMATIX_MCP_URL": "http://127.0.0.1:19199"},
                        }
                    }
                }
            ),
            "unknown",
            None,
        ),
        (
            "antigravity",
            ".agents/mcp_config.json",
            json.dumps(
                {
                    "mcpServers": {
                        "cymatix-context": {
                            "command": "python",
                            "args": ["-m", "cymatix_context.mcp_server"],
                            "disabled": True,
                            "env": {"CYMATIX_MCP_URL": "http://127.0.0.1:19199"},
                        }
                    }
                }
            ),
            "disabled",
            False,
        ),
    ],
)
def test_collect_status_preserves_native_activation_evidence(
    monkeypatch,
    tmp_path,
    host,
    config_path,
    contents,
    expected_activation,
    expected_ready,
):
    config = tmp_path / config_path
    config.parent.mkdir(parents=True)
    config.write_text(contents, encoding="utf-8")
    _healthy_probes(monkeypatch)

    result = status_mod.collect_status(
        host=host,
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert result["host"]["selection"] == host
    assert result["mcp"]["activation"] == expected_activation
    assert result["configured_ready"] is expected_ready
    assert result["launcher"]["state"] == "not_configured"


def test_missing_skill_does_not_change_configured_ready(monkeypatch, tmp_path):
    config = tmp_path / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    _json_config(config)
    _healthy_probes(monkeypatch)

    result = status_mod.collect_status(
        host="antigravity",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert result["configured_ready"] is True
    assert result["guided_ready"] is False
    assert result["skill"]["installation"] == "missing"


def test_successfully_read_empty_registry_proves_configured_mcp_is_disconnected(monkeypatch, tmp_path):
    config = tmp_path / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    _json_config(config)
    _healthy_probes(monkeypatch)

    result = status_mod.collect_status(
        host="antigravity",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert result["configured_ready"] is True
    assert result["mcp"]["live"] == "disconnected"


def test_registry_failure_keeps_live_state_unknown(monkeypatch, tmp_path):
    config = tmp_path / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    _json_config(config)

    def fake_probe(url, timeout_s=status_mod.DEFAULT_STATUS_TIMEOUT_S):
        if url.endswith("/health"):
            return ProbeResult("reachable", {"status": "ok"}, None, None)
        if url.endswith("/sessions?status=active"):
            return ProbeResult("unreachable", None, None, "connection refused")
        raise AssertionError(url)

    monkeypatch.setattr(status_mod, "_probe_json", fake_probe)
    result = status_mod.collect_status(
        host="antigravity",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert result["configured_ready"] is True
    assert result["mcp"]["live"] == "unknown"


def test_missing_live_identity_is_unknown_even_when_server_is_healthy(monkeypatch, tmp_path):
    config = tmp_path / ".mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "cymatix-context": {
                        "command": "python",
                        "args": ["-m", "cymatix_context.mcp_server"],
                        "env": {"CYMATIX_MCP_URL": "http://127.0.0.1:19199"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    _healthy_probes(monkeypatch)

    result = status_mod.collect_status(
        host="claude-code",
        mcp_config=config,
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert result["mcp"]["live"] == "unknown"
    assert result["configured_ready"] is None


def test_explicit_unrecognized_json_config_requires_host(monkeypatch, tmp_path):
    config = tmp_path / "custom.json"
    _json_config(config)
    _healthy_probes(monkeypatch)

    result = status_mod.collect_status(
        mcp_config=config,
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert result["host"]["selection"] == "unknown"
    assert "Pass --host" in result["next_action"]


def test_explicit_recognized_config_selects_its_native_profile(monkeypatch, tmp_path):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "[mcp_servers.cymatix-context]\n"
        'command = "python"\n'
        'args = ["-m", "cymatix_context.mcp_server"]\n'
        "enabled = true\n\n"
        "[mcp_servers.cymatix-context.env]\n"
        'CYMATIX_MCP_URL = "http://127.0.0.1:19199"\n',
        encoding="utf-8",
    )
    _healthy_probes(monkeypatch)

    result = status_mod.collect_status(
        mcp_config=config,
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert result["host"]["selection"] == "codex"
    assert result["server"]["url"] == "http://127.0.0.1:19199"


def test_report_refuses_credential_bearing_config_url_without_leaking_environment(
    monkeypatch, tmp_path
):
    config = tmp_path / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    _json_config(config)
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["mcpServers"]["cymatix-context"]["env"]["CYMATIX_MCP_URL"] = (
        "http://user:token@127.0.0.1:19199/health?debug=1#trace"
    )
    config.write_text(json.dumps(raw), encoding="utf-8")
    attempts = _record_connections(monkeypatch)
    result = status_mod.collect_status(
        host="antigravity",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert attempts == [], "credential-bearing URL must not be opened"
    serialized = json.dumps(result)
    assert "UNRELATED_SECRET" not in serialized
    assert "token" not in serialized
    assert result["server"]["url"] == "http://127.0.0.1:19199/health"
    assert result["server"]["health"] == "unknown"
    assert result["server"]["configured_url_match"] is None
    assert result["configured_ready"] is False


def test_skill_directory_is_not_reported_as_an_installed_file(monkeypatch, tmp_path):
    config = tmp_path / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    _json_config(config)
    skill_file = tmp_path / ".agents" / "skills" / "cymatix-context" / "SKILL.md"
    skill_file.mkdir(parents=True)
    _healthy_probes(monkeypatch)

    result = status_mod.collect_status(
        host="antigravity",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )
    assert result["skill"]["installation"] == "missing"


def test_explicit_mismatched_server_url_is_diagnostic_only(monkeypatch, tmp_path):
    config = tmp_path / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    # URL identity is structural: localhost is not DNS-equivalent to 127.0.0.1.
    _json_config(config, server_url="http://localhost:19199")
    _installed_antigravity_skill(tmp_path)
    seen_urls = []

    def fake_probe(url, timeout_s=status_mod.DEFAULT_STATUS_TIMEOUT_S):
        seen_urls.append(url)
        if url == "http://127.0.0.1:19199/health":
            return ProbeResult("reachable", {"status": "ok"}, None, None)
        pytest.fail(f"mismatched diagnostic target must not supply MCP evidence: {url}")

    monkeypatch.setattr(status_mod, "_probe_json", fake_probe)
    result = status_mod.collect_status(
        host="antigravity",
        server_url="http://127.0.0.1:19199",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert result["server"]["url"] == "http://127.0.0.1:19199"
    assert result["server"]["source"] == "explicit"
    assert result["server"]["configured_url_match"] is False
    assert result["server"]["health"] == "healthy"
    assert result["mcp"]["live"] == "unknown"
    assert result["configured_ready"] is None
    assert result["guided_ready"] is None
    assert seen_urls == [
        "http://127.0.0.1:19199/health",
    ]
    assert "update the selected host mcp url" in result["next_action"].lower()
    rendered_json = json.dumps(result, sort_keys=True)
    rendered_text = status_mod._render_text(result)
    assert '"configured_url_match": false' in rendered_json
    assert "source=explicit" in rendered_text
    assert "configured_url_match=no" in rendered_text


def test_explicit_canonical_equivalent_server_url_preserves_configured_evidence(
    monkeypatch, tmp_path
):
    config = tmp_path / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    _json_config(config, server_url="HTTP://LOCALHOST:80/context/")
    _installed_antigravity_skill(tmp_path)
    seen_urls = []

    def fake_probe(url, timeout_s=status_mod.DEFAULT_STATUS_TIMEOUT_S):
        seen_urls.append(url)
        if url == "http://localhost/context/health":
            return ProbeResult("reachable", {"status": "ok"}, None, None)
        if url == "http://localhost/context/sessions?status=active":
            return ProbeResult(
                "reachable",
                {
                    "participants": [
                        {
                            "handle": "agent-1",
                            "mcp_host": "antigravity",
                            "status": "active",
                        }
                    ]
                },
                None,
                None,
            )
        raise AssertionError(url)

    monkeypatch.setattr(status_mod, "_probe_json", fake_probe)
    result = status_mod.collect_status(
        host="antigravity",
        server_url="http://localhost/context",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert result["server"]["source"] == "explicit"
    assert result["server"]["configured_url_match"] is True
    assert result["mcp"]["live"] == "connected"
    assert result["configured_ready"] is True
    assert result["guided_ready"] is True
    assert seen_urls == [
        "http://localhost/context/health",
        "http://localhost/context/sessions?status=active",
    ]


def test_main_exits_nonzero_for_mismatched_explicit_server_url(
    monkeypatch, tmp_path, capsys
):
    config = tmp_path / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    _json_config(config, server_url="http://127.0.0.1:19199")
    seen_urls = []

    def fake_probe(url, timeout_s=status_mod.DEFAULT_STATUS_TIMEOUT_S):
        seen_urls.append(url)
        if url == "https://status.example.test:19444/health":
            return ProbeResult("reachable", {"status": "ok"}, None, None)
        if url == "http://127.0.0.1:11438/api/state":
            return ProbeResult("reachable", {"cymatix": {"running": True}}, None, None)
        pytest.fail(f"mismatched diagnostic target must not query sessions: {url}")

    monkeypatch.setattr(status_mod, "_probe_json", fake_probe)
    rc = status_mod.main(
        [
            "--host",
            "antigravity",
            "--mcp-config",
            str(config),
            "--server-url",
            "https://status.example.test:19444",
            "--launcher-url",
            "http://127.0.0.1:11438",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["server"]["source"] == "explicit"
    assert payload["server"]["configured_url_match"] is False
    assert payload["configured_ready"] is None
    assert not any("/sessions?status=active" in url for url in seen_urls)


@pytest.mark.parametrize(
    "configured_url",
    [
        "http://169.254.169.254/latest",
        "https://status.example.test:19444",
        "http://user:token@127.0.0.1:19199",
        "http://127.0.0.1:19199?debug=1",
        "http://127.0.0.1:19199#fragment",
        "http:///missing-host",
        "file:///C:/Windows/system.ini",
        "http://[::1",
        "",
    ],
)
def test_implicit_unsafe_or_malformed_configured_url_is_never_probed(
    monkeypatch, tmp_path, configured_url
):
    config = tmp_path / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    _json_config(config, server_url=configured_url)
    attempts = _record_connections(monkeypatch)
    result = status_mod.collect_status(
        host="antigravity",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert attempts == [], "refused config URL must not open a connection"
    assert result["server"]["health"] == "unknown"
    assert result["mcp"]["live"] == "unknown"
    assert "not probed" in result["server"]["error"].lower()
    rendered = json.dumps(result)
    assert "token" not in rendered
    assert "debug=1" not in rendered
    assert "#fragment" not in rendered


def test_non_loopback_config_explains_explicit_server_url_opt_in(monkeypatch, tmp_path):
    config = tmp_path / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    _json_config(config, server_url="https://status.example.test:19444")
    attempts = _record_connections(monkeypatch)

    result = status_mod.collect_status(
        host="antigravity",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert attempts == [], "non-loopback config was probed"
    assert "pass --server-url explicitly" in result["next_action"].lower()


@pytest.mark.parametrize(
    "explicit_url",
    [
        "http://user:token@status.example.test:19444",
        "https://status.example.test:19444?token=secret",
        "file:///C:/Windows/system.ini",
    ],
)
def test_invalid_explicit_server_url_wins_but_is_not_probed(
    monkeypatch, tmp_path, explicit_url
):
    config = tmp_path / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    _json_config(config, server_url="http://127.0.0.1:19199")
    attempts = _record_connections(monkeypatch)
    result = status_mod.collect_status(
        host="antigravity",
        server_url=explicit_url,
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert attempts == [], "invalid explicit URL must not open a connection"
    assert result["server"]["health"] == "unknown"
    assert "valid absolute http" in result["next_action"].lower()


@pytest.mark.parametrize(
    "configured_url",
    [
        "http://127.0.0.1:19199",
        "https://localhost:19199",
        "http://[::1]:19199",
        "http://[::ffff:127.0.0.1]:19199",
    ],
)
def test_implicit_loopback_configured_urls_are_probed_without_dns_resolution(
    monkeypatch, tmp_path, configured_url
):
    config = tmp_path / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    _json_config(config, server_url=configured_url)
    seen_urls = []

    def fail_dns(*_args, **_kwargs):
        raise AssertionError("status URL trust must not resolve DNS")

    def fake_probe(url, timeout_s=status_mod.DEFAULT_STATUS_TIMEOUT_S):
        seen_urls.append(url)
        if url.endswith("/health"):
            return ProbeResult("reachable", {"status": "ok"}, None, None)
        if url.endswith("/sessions?status=active"):
            return ProbeResult("reachable", {"participants": []}, None, None)
        raise AssertionError(url)

    monkeypatch.setattr(socket, "getaddrinfo", fail_dns)
    monkeypatch.setattr(status_mod, "_probe_json", fake_probe)
    result = status_mod.collect_status(
        host="antigravity",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert result["server"]["health"] == "healthy"
    assert result["server"]["source"] == "configured"
    assert result["server"]["configured_url_match"] is True
    assert seen_urls == [
        f"{configured_url}/health",
        f"{configured_url}/sessions?status=active",
    ]


def test_launcher_url_is_redacted_and_invalid_launcher_target_is_not_probed(
    monkeypatch, tmp_path
):
    seen_urls = []

    def fake_probe(url, timeout_s=status_mod.DEFAULT_STATUS_TIMEOUT_S):
        seen_urls.append(url)
        return ProbeResult("reachable", {"status": "ok"}, None, None)

    monkeypatch.setattr(status_mod, "_probe_json", fake_probe)
    result = status_mod.collect_status(
        server_url="http://127.0.0.1:11437",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url="http://user:secret@127.0.0.1:11438/api?debug=1#trace",
    )

    assert seen_urls == ["http://127.0.0.1:11437/health"]
    assert result["launcher"]["state"] == "unreachable"
    assert result["launcher"]["url"] == "http://127.0.0.1:11438/api"
    rendered = json.dumps(result) + status_mod._render_text(result)
    for secret_part in ("user", "secret", "debug=1", "#trace"):
        assert secret_part not in rendered


def test_successful_status_probe_reads_no_more_than_the_response_limit(monkeypatch):
    read_sizes = _record_body_reads(monkeypatch)

    with _LoopbackServer(lambda _path: (200, _JSON, b"x" * (1024 * 1024))) as server:
        probe = status_mod._probe_json(f"{server.url}/health", 5.0)

    assert read_sizes == [status_mod.MAX_STATUS_RESPONSE_BYTES + 1]
    assert probe.transport == "reachable"
    assert probe.payload is None
    assert "exceeds" in probe.parse_error.lower()


def test_http_error_status_probe_reads_no_more_than_the_response_limit(monkeypatch):
    read_sizes = _record_body_reads(monkeypatch)

    with _LoopbackServer(lambda _path: (503, _JSON, b"x" * (1024 * 1024))) as server:
        probe = status_mod._probe_json(f"{server.url}/health", 5.0)

    assert read_sizes == [status_mod.MAX_STATUS_RESPONSE_BYTES + 1]
    assert probe.error == "HTTP 503"
    assert "exceeds" in probe.parse_error.lower()


# -- probe safety: redirect confinement and the timeout budget ---------
#
# Two gaps were statically visible and untested:
# the loopback rule is enforced once, before the first request, and the
# probe timeout was parsed as a float without any finite positive check.
# Both are fixed in `cymatix_status`; these are the negatives.


class _FakeResponse(urllib.response.addinfourl):
    def __init__(self, url, code, header_text, body=b"{}"):
        headers = email.message_from_string(header_text, _class=http.client.HTTPMessage)
        super().__init__(io.BytesIO(body), headers, url, code)
        self.msg = "fake"


@pytest.mark.parametrize(
    "location",
    [
        "http://169.254.169.254/health",
        "http://example.invalid/health",
        "https://127.0.0.1:9/health",
        "/elsewhere",
    ],
)
def test_a_status_probe_never_requests_a_redirect_destination(monkeypatch, location):
    attempts = _record_connections(monkeypatch, allow_loopback=True)

    with _LoopbackServer(lambda _path: (302, {"Location": location}, b"")) as server:
        probe = status_mod._probe_json(f"{server.url}/health", 5.0)

    assert server.requests == ["/health"]
    assert attempts == [("127.0.0.1", server.port)]
    assert probe.transport == "unreachable"
    assert probe.payload is None
    assert probe.error == status_mod.REDIRECT_REFUSED_REASON


def test_a_status_probe_still_reads_a_direct_answer(monkeypatch):
    attempts = _record_connections(monkeypatch, allow_loopback=True)

    with _LoopbackServer(lambda _path: (200, _JSON, b'{"status": "ok"}')) as server:
        probe = status_mod._probe_json(f"{server.url}/health", 5.0)

    assert server.requests == ["/health"]
    assert attempts == [("127.0.0.1", server.port)]
    assert probe.transport == "reachable"
    assert probe.payload == {"status": "ok"}


def test_a_report_never_carries_evidence_collected_off_the_requested_origin(monkeypatch):
    """The second lock: an answer from another origin is discarded.

    No real server can answer from an origin other than its own without
    a redirect, so the HTTP handler class itself is replaced here. That
    holds for any opener the probe builds, since each one uses it.
    """

    def answer_from_elsewhere(_handler, _req):
        return _FakeResponse(
            "http://169.254.169.254/health",
            200,
            "Content-Type: application/json\n",
            b'{"status": "ok"}',
        )

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", answer_from_elsewhere)

    probe = status_mod._probe_json("http://127.0.0.1:11437/health")

    assert probe.transport == "unreachable"
    assert probe.payload is None


@pytest.mark.parametrize(
    "value",
    [0, 0.0, -1.0, -0.001, float("nan"), float("inf"), float("-inf"), "10", None, True],
)
def test_an_unusable_probe_timeout_falls_back_to_the_documented_budget(value):
    assert status_mod.finite_positive_timeout(value) == status_mod._DEFAULT_STATUS_TIMEOUT_S


@pytest.mark.parametrize("value", [0.001, 1, 2.5, 59.999, 60.0])
def test_a_usable_probe_timeout_is_kept(value):
    assert status_mod.finite_positive_timeout(value) == float(value)


@pytest.mark.parametrize("value", [60.001, 600.0, 10**9])
def test_an_excessive_probe_timeout_is_clamped_to_the_ceiling(value):
    assert status_mod.finite_positive_timeout(value) == status_mod.MAX_STATUS_TIMEOUT_S


def test_the_probe_clamps_its_timeout_without_mutating_any_global(monkeypatch):
    before = status_mod.DEFAULT_STATUS_TIMEOUT_S
    seen = []

    def capture(_address, timeout=None, *_args, **_kwargs):
        # The timeout the socket itself is handed, whatever opener asked.
        seen.append(timeout)
        raise OSError("connection refused by the test")

    monkeypatch.setattr(socket, "create_connection", capture)

    for supplied in (0.0, -5.0, float("inf"), 10**6):
        status_mod._probe_json("http://127.0.0.1:11437/health", supplied)

    assert seen == [
        status_mod._DEFAULT_STATUS_TIMEOUT_S,
        status_mod._DEFAULT_STATUS_TIMEOUT_S,
        status_mod._DEFAULT_STATUS_TIMEOUT_S,
        status_mod.MAX_STATUS_TIMEOUT_S,
    ]
    assert status_mod.DEFAULT_STATUS_TIMEOUT_S == before


@pytest.mark.parametrize(
    ("raw", "expected", "warns"),
    [
        ("0", 10.0, True),
        ("-3", 10.0, True),
        ("nan", 10.0, True),
        ("inf", 10.0, True),
        ("not-a-float", 10.0, True),
        ("900", 60.0, True),
        ("2.5", 2.5, False),
        (None, 10.0, False),
    ],
)
def test_the_environment_timeout_is_validated_not_merely_parsed(
    monkeypatch, capsys, raw, expected, warns
):
    if raw is None:
        monkeypatch.delenv("CYMATIX_STATUS_TIMEOUT_S", raising=False)
    else:
        monkeypatch.setenv("CYMATIX_STATUS_TIMEOUT_S", raw)

    assert status_mod._timeout_from_environment() == expected

    warned = capsys.readouterr().err
    assert ("CYMATIX_STATUS_TIMEOUT_S" in warned) is warns


def test_an_implicit_target_that_is_the_callers_own_address_is_never_requested(monkeypatch):
    probed = []
    monkeypatch.setattr(
        status_mod,
        "_probe_json",
        lambda url, timeout_s=None: probed.append(url) or ProbeResult(
            "unreachable", None, None, "offline"
        ),
    )

    for configured in ("http://127.0.0.1:11438", "http://localhost:11438"):
        target = status_mod._select_server_target(
            explicit_url=None,
            configured_url=configured,
            denied_origin="http://127.0.0.1:11438",
        )
        assert target.request_url is None
        assert target.action == status_mod.SELF_TARGET_ACTION

    kept = status_mod._select_server_target(
        explicit_url=None,
        configured_url="http://127.0.0.1:11437",
        denied_origin="http://127.0.0.1:11438",
    )
    assert kept.request_url == "http://127.0.0.1:11437"
    assert probed == []

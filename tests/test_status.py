"""Integration contracts for the host-aware ``cymatix-status`` report."""

from __future__ import annotations

import json
import io
import socket
import urllib.error

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
    monkeypatch.setattr(
        status_mod.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("credential-bearing URL must not be opened"),
    )
    result = status_mod.collect_status(
        host="antigravity",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

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
    calls = []

    def fail_urlopen(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("refused config URL must not reach urllib")

    monkeypatch.setattr(status_mod.urllib.request, "urlopen", fail_urlopen)
    result = status_mod.collect_status(
        host="antigravity",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert calls == []
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
    monkeypatch.setattr(
        status_mod.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("non-loopback config was probed"),
    )

    result = status_mod.collect_status(
        host="antigravity",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

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
    calls = []

    def fail_urlopen(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("invalid explicit URL must not reach urllib")

    monkeypatch.setattr(status_mod.urllib.request, "urlopen", fail_urlopen)
    result = status_mod.collect_status(
        host="antigravity",
        server_url=explicit_url,
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=None,
    )

    assert calls == []
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
    read_sizes = []

    class Response:
        def read(self, size=-1):
            read_sizes.append(size)
            return b"x" * size

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        status_mod.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    probe = status_mod._probe_json("http://127.0.0.1:11437/health")

    assert read_sizes == [status_mod.MAX_STATUS_RESPONSE_BYTES + 1]
    assert probe.payload is None
    assert "exceeds" in probe.parse_error.lower()


def test_http_error_status_probe_reads_no_more_than_the_response_limit(monkeypatch):
    read_sizes = []

    class ErrorBody(io.BytesIO):
        def read(self, size=-1):
            read_sizes.append(size)
            return super().read(size)

    error = urllib.error.HTTPError(
        "http://127.0.0.1:11437/health",
        503,
        "maintenance",
        {},
        ErrorBody(b"x" * (1024 * 1024)),
    )
    monkeypatch.setattr(
        status_mod.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    probe = status_mod._probe_json("http://127.0.0.1:11437/health")

    assert read_sizes == [status_mod.MAX_STATUS_RESPONSE_BYTES + 1]
    assert probe.error == "HTTP 503"
    assert "exceeds" in probe.parse_error.lower()

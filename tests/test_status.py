"""Integration contracts for the host-aware ``cymatix-status`` report."""

from __future__ import annotations

import json

import pytest

import cymatix_context.cli.cymatix_status as status_mod
from cymatix_context.cli.cymatix_status import ProbeResult


def _json_config(path, *, disabled: bool = False):
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "cymatix-context": {
                        "command": "python",
                        "args": ["-m", "cymatix_context.mcp_server"],
                        "disabled": disabled,
                        "env": {
                            "CYMATIX_MCP_URL": "http://127.0.0.1:19199",
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


def test_report_redacts_config_environment_and_server_userinfo(monkeypatch, tmp_path):
    config = tmp_path / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    _json_config(config)
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["mcpServers"]["cymatix-context"]["env"]["CYMATIX_MCP_URL"] = (
        "http://user:token@127.0.0.1:19199/health?debug=1#trace"
    )
    config.write_text(json.dumps(raw), encoding="utf-8")
    seen_urls = []

    def fake_probe(url, timeout_s=status_mod.DEFAULT_STATUS_TIMEOUT_S):
        seen_urls.append(url)
        if url.endswith("/sessions?status=active"):
            return ProbeResult("reachable", {"participants": []}, None, None)
        return ProbeResult("reachable", {"status": "ok"}, None, None)

    monkeypatch.setattr(status_mod, "_probe_json", fake_probe)
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
    assert any(url.startswith("http://user:token@") for url in seen_urls)


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

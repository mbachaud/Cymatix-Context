"""`cymatix mcp install`: point a chat host's MCP config at a lane."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cymatix_context.cli import cmd_mcp
from cymatix_context.cli import output
from cymatix_context.integrations.host_profiles import get_profile, parse_host_config


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for var in ("CYMATIX_GENOME_PATH", "CYMATIX_BENCH_ENABLED", "CYMATIX_SERVER_PORT"):
        monkeypatch.delenv(var, raising=False)
    cfg = tmp_path / "cymatix.toml"
    cfg.write_text(
        "[server]\nbench_enabled = true\n"
        "[[lanes]]\n"
        'name = "staging"\nport = 11441\ngenome_path = "genomes/staging/genome.db"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CYMATIX_CONFIG", str(cfg))


def _run(tmp_path, *args):
    return cmd_mcp.run(["install", *args, "--workspace", str(tmp_path / "ws"),
                        "--home", str(tmp_path / "home")])


def _servers(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_install_primary_lane_writes_canonical_entry(tmp_path):
    assert _run(tmp_path, "--host", "claude-code") == output.EXIT_OK
    target = tmp_path / "ws" / ".mcp.json"
    entry = _servers(target)["cymatix-context"]
    assert entry["env"]["CYMATIX_MCP_URL"] == "http://127.0.0.1:11437"
    assert entry["env"]["CYMATIX_MCP_HOST"] == "claude-code"
    # cymatix-status must accept what we wrote.
    parsed = parse_host_config(target, get_profile("claude-code"))
    assert parsed.configuration == "canonical"


def test_install_other_lanes_sit_beside_the_primary(tmp_path):
    target = tmp_path / "ws" / ".mcp.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}),
                      encoding="utf-8")
    assert _run(tmp_path, "--host", "claude-code") == output.EXIT_OK
    assert _run(tmp_path, "--host", "claude-code", "--lane", "bench") == output.EXIT_OK
    assert _run(tmp_path, "--host", "claude-code", "--lane", "staging") == output.EXIT_OK
    servers = _servers(target)
    assert servers["other"] == {"command": "x"}
    assert servers["cymatix-context-bench"]["env"]["CYMATIX_MCP_URL"] == "http://127.0.0.1:11439"
    assert servers["cymatix-context-staging"]["env"]["CYMATIX_MCP_URL"] == "http://127.0.0.1:11441"
    assert servers["cymatix-context"]["env"]["CYMATIX_MCP_URL"] == "http://127.0.0.1:11437"
    # The primary entry stays canonical with lane entries alongside it.
    assert parse_host_config(target, get_profile("claude-code")).configuration == "canonical"


def test_reinstall_keeps_user_env_and_backs_up(tmp_path):
    target = tmp_path / "ws" / ".mcp.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"mcpServers": {"cymatix-context": {
        "command": "python", "args": ["-m", "cymatix_context.mcp_server"],
        "env": {"CYMATIX_MCP_URL": "http://127.0.0.1:9999", "CYMATIX_USER": "max"},
    }}}), encoding="utf-8")
    original = target.read_text(encoding="utf-8")
    assert _run(tmp_path, "--host", "claude-code") == output.EXIT_OK
    env = _servers(target)["cymatix-context"]["env"]
    assert env["CYMATIX_USER"] == "max"
    assert env["CYMATIX_MCP_URL"] == "http://127.0.0.1:11437"
    backups = list(target.parent.glob(".mcp.json.bak-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == original


def test_unknown_lane_is_rejected_and_nothing_written(tmp_path, capsys):
    assert _run(tmp_path, "--host", "claude-code", "--lane", "nope") == output.EXIT_BAD_ARGS
    assert not (tmp_path / "ws" / ".mcp.json").exists()
    assert "staging" in capsys.readouterr().err


def test_invalid_json_target_is_left_alone(tmp_path):
    target = tmp_path / "ws" / ".mcp.json"
    target.parent.mkdir(parents=True)
    target.write_text("{ not json", encoding="utf-8")
    assert _run(tmp_path, "--host", "claude-code") == output.EXIT_ERROR
    assert target.read_text(encoding="utf-8") == "{ not json"


def test_dry_run_writes_nothing(tmp_path, capsys):
    assert _run(tmp_path, "--host", "claude-code", "--lane", "bench", "--dry-run") == output.EXIT_OK
    assert not (tmp_path / "ws" / ".mcp.json").exists()
    assert "cymatix-context-bench" in capsys.readouterr().out


@pytest.mark.parametrize("host,scope,expected", [
    ("claude-code", "workspace", "ws/.mcp.json"),
    ("claude-code", "user", "home/.claude.json"),
    ("cursor", "workspace", "ws/.cursor/mcp.json"),
    ("cursor", "user", "home/.cursor/mcp.json"),
    ("gemini-cli", "user", "home/.gemini/settings.json"),
])
def test_target_paths(tmp_path, host, scope, expected):
    got = cmd_mcp.target_path(host, scope, workspace=tmp_path / "ws", home=tmp_path / "home")
    assert got == tmp_path / expected


def test_claude_desktop_path_per_platform(tmp_path):
    home = tmp_path / "home"
    assert cmd_mcp.target_path(
        "claude-desktop", "user", workspace=tmp_path, home=home,
        platform="win32", appdata=str(tmp_path / "AppData" / "Roaming"),
    ) == tmp_path / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    assert cmd_mcp.target_path(
        "claude-desktop", "user", workspace=tmp_path, home=home, platform="darwin",
    ) == home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def test_list_lanes_prints_mcp_urls(tmp_path, capsys):
    assert cmd_mcp.run(["lanes"]) == output.EXIT_OK
    out = capsys.readouterr().out
    assert "stable" in out and "http://127.0.0.1:11437" in out
    assert "staging" in out and "http://127.0.0.1:11441" in out


# ── MCP proxy sends the admin token ─────────────────────────────────────


def test_mcp_http_sends_bearer_token_when_set(monkeypatch):
    from cymatix_context.mcp import mcp_server

    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout):
        seen["auth"] = req.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr(mcp_server.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("CYMATIX_ADMIN_TOKEN", "s3cret")
    mcp_server._http("POST", "/ingest", {"content": "x"})
    assert seen["auth"] == "Bearer s3cret"

    monkeypatch.delenv("CYMATIX_ADMIN_TOKEN")
    mcp_server._http("GET", "/health")
    assert seen["auth"] is None

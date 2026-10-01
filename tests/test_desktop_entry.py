"""The single packaged engine entry (`cymatix-engine launcher|cli|mcp`) and
MCP entries that point at it instead of `python -m ...`."""

from __future__ import annotations

import json

import pytest

from cymatix_context import desktop_entry


def test_dispatches_each_subcommand(monkeypatch):
    seen = []
    monkeypatch.setattr("cymatix_context.launcher.app.main",
                        lambda argv=None: seen.append(("launcher", argv)) or 0)
    monkeypatch.setattr("cymatix_context.cli.dispatcher.main",
                        lambda argv=None: seen.append(("cli", argv)) or 0)
    monkeypatch.setattr("cymatix_context.mcp.mcp_server.main",
                        lambda: seen.append(("mcp", None)))
    assert desktop_entry.main(["launcher", "--headless", "--port", "0"]) == 0
    assert desktop_entry.main(["cli", "mcp", "lanes"]) == 0
    assert desktop_entry.main(["mcp"]) == 0
    assert seen == [("launcher", ["--headless", "--port", "0"]),
                    ("cli", ["mcp", "lanes"]), ("mcp", None)]


@pytest.mark.parametrize("argv", [[], ["bogus"]])
def test_unknown_subcommand_is_usage_error(argv, capsys):
    assert desktop_entry.main(argv) == 2
    assert "launcher" in capsys.readouterr().err


def test_mcp_install_with_packaged_server_command(tmp_path, monkeypatch):
    from cymatix_context.cli import cmd_mcp
    monkeypatch.setenv("CYMATIX_CONFIG", str(tmp_path / "none.toml"))
    # claude-desktop resolves under %APPDATA% on Windows: never the real one.
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    exe = str(tmp_path / "Cymatix" / "resources" / "engine" / "cymatix-engine.exe")
    rc = cmd_mcp.run(["install", "--host", "claude-desktop", "--workspace", str(tmp_path),
                      "--home", str(tmp_path / "home"), "--server-command", exe,
                      "--server-arg", "mcp"])
    assert rc == 0
    (cfg,) = list(tmp_path.rglob("claude_desktop_config.json"))
    entry = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["cymatix-context"]
    assert entry["command"] == exe and entry["args"] == ["mcp"]


def test_server_command_rewrites_existing_entry(tmp_path, monkeypatch):
    from cymatix_context.cli import cmd_mcp
    monkeypatch.setenv("CYMATIX_CONFIG", str(tmp_path / "none.toml"))
    target = tmp_path / "ws" / ".mcp.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"mcpServers": {"cymatix-context": {
        "command": "python", "args": ["-m", "cymatix_context.mcp_server"],
        "env": {"CYMATIX_USER": "max"}}}}), encoding="utf-8")
    rc = cmd_mcp.run(["install", "--host", "claude-code", "--workspace", str(tmp_path / "ws"),
                      "--home", str(tmp_path / "home"), "--server-command", "C:/x/engine.exe",
                      "--server-arg", "mcp"])
    assert rc == 0
    entry = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["cymatix-context"]
    assert entry["command"] == "C:/x/engine.exe" and entry["args"] == ["mcp"]
    assert entry["env"]["CYMATIX_USER"] == "max"

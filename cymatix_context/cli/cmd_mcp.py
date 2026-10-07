"""`cymatix mcp lanes|install` — point a chat host's MCP config at a lane.

Each lane (see ``cymatix_context/lanes.py``) is its own server on its own
port, and a chat picks one through its MCP entry's ``CYMATIX_MCP_URL``.
``install`` writes that entry:

- the primary lane as the canonical ``cymatix-context`` entry that
  ``cymatix-status`` validates;
- any other lane as ``cymatix-context-<lane>`` beside it, so one host can
  reach several lanes as separate MCP servers.

An existing entry keeps its command, args and any env keys the user added
(identity fields such as ``CYMATIX_USER``); only the URL and host tags are
rewritten. The target file is backed up before every write and replaced
atomically. Only JSON ``mcpServers`` hosts are written; Codex (TOML) keeps
using ``cymatix-status`` snippets.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from . import output
from .dispatcher import invoked_prog

_SERVER_NAME = "cymatix-context"
_COMMAND = "python"
_ARGS = ["-m", "cymatix_context.mcp_server"]

# host -> {scope: path relative to that scope's root}. claude-desktop has no
# workspace scope; its user path is platform-specific (see target_path).
_TARGETS: Dict[str, Dict[str, str]] = {
    "claude-code": {"workspace": ".mcp.json", "user": ".claude.json"},
    "cursor": {"workspace": ".cursor/mcp.json", "user": ".cursor/mcp.json"},
    "gemini-cli": {"workspace": ".gemini/settings.json", "user": ".gemini/settings.json"},
    "claude-desktop": {},
}
_DEFAULT_SCOPE = {"claude-desktop": "user", "gemini-cli": "user"}


def supported_hosts() -> list:
    """Host ids ``install`` accepts: the one list the desktop rail offers."""
    return sorted(_TARGETS)


def target_path(
    host: str,
    scope: str,
    *,
    workspace: Path,
    home: Path,
    platform: str = sys.platform,
    appdata: Optional[str] = None,
) -> Path:
    """The MCP config file ``install`` writes for *host* at *scope*."""
    if host == "claude-desktop":
        if platform == "win32":
            base = Path(appdata or os.environ.get("APPDATA") or home / "AppData" / "Roaming")
            return base / "Claude" / "claude_desktop_config.json"
        if platform == "darwin":
            return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        return home / ".config" / "Claude" / "claude_desktop_config.json"
    rel = _TARGETS[host].get(scope)
    if rel is None:
        raise ValueError(f"{host} has no {scope} MCP config")
    return (workspace if scope == "workspace" else home) / rel


def entry_name(lane: str) -> str:
    from ..lanes import PRIMARY_LANE
    return _SERVER_NAME if lane == PRIMARY_LANE else f"{_SERVER_NAME}-{lane}"


def _lanes():
    from ..config import load_config
    from ..lanes import resolve_lanes
    cfg = load_config()
    return cfg, resolve_lanes(cfg)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{invoked_prog()} mcp",
        description="Point a chat host's MCP config at a Cymatix lane.",
    )
    sub = parser.add_subparsers(dest="action", metavar="<action>")
    sub.add_parser("lanes", help="List lanes and the CYMATIX_MCP_URL for each.")
    inst = sub.add_parser("install", help="Write or update a host's MCP entry for a lane.")
    inst.add_argument("--host", required=True, choices=sorted(_TARGETS),
                      help="Chat host whose MCP config to write.")
    inst.add_argument("--lane", default="stable",
                      help="Lane to point at (default: stable). See `mcp lanes`.")
    inst.add_argument("--scope", choices=("workspace", "user"), default=None,
                      help="Project-level or user-level config (default depends on host).")
    inst.add_argument("--workspace", default=None,
                      help="Project root for workspace scope (default: cwd).")
    inst.add_argument("--home", default=None, help=argparse.SUPPRESS)
    inst.add_argument("--server-command", default=None,
                      help="Command that starts the MCP server (default: python, "
                           "the canonical entry). The desktop app passes its bundled "
                           "engine here so chats work without a Python install.")
    inst.add_argument("--server-arg", action="append", default=None,
                      help="Argument for --server-command (repeatable).")
    inst.add_argument("--dry-run", action="store_true",
                      help="Print the entry and target file without writing.")
    return parser


def _cmd_lanes() -> int:
    _cfg, lanes = _lanes()
    for lane in lanes:
        print(f"{lane.name:<12} {lane.role:<8} http://127.0.0.1:{lane.port:<6} "
              f"{entry_name(lane.name):<28} {lane.genome_path}")
    return output.EXIT_OK


def _updated_entry(existing: object, url: str, host: str,
                   command: Optional[str] = None,
                   command_args: Optional[list] = None) -> Dict[str, object]:
    entry: Dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
    if command:
        # Explicit server command (the desktop app's bundled engine) wins.
        entry["command"] = command
        entry["args"] = list(command_args or [])
    else:
        entry.setdefault("command", _COMMAND)
        entry.setdefault("args", list(_ARGS))
    env = entry.get("env")
    env = dict(env) if isinstance(env, dict) else {}
    env["CYMATIX_MCP_URL"] = url
    env["CYMATIX_MCP_HOST"] = host
    env.setdefault("CYMATIX_AGENT_KIND", host)
    entry["env"] = env
    return entry


def _write_atomic(path: Path, data: Dict[str, object]) -> Optional[Path]:
    """Back up *path* (if present), then replace it atomically. Returns the
    backup path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup


def _cmd_install(args: argparse.Namespace) -> int:
    _cfg, lanes = _lanes()
    by_name = {lane.name: lane for lane in lanes}
    lane = by_name.get(args.lane)
    if lane is None:
        output.eprint(f"no lane named {args.lane!r}; known lanes: {', '.join(by_name)}")
        return output.EXIT_BAD_ARGS

    scope = args.scope or _DEFAULT_SCOPE.get(args.host, "workspace")
    try:
        path = target_path(
            args.host, scope,
            workspace=Path(args.workspace or os.getcwd()),
            home=Path(args.home) if args.home else Path.home(),
        )
    except ValueError as exc:
        output.eprint(str(exc))
        return output.EXIT_BAD_ARGS

    data: Dict[str, object] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            output.eprint(f"{path} is not readable JSON ({exc}); not modifying it")
            return output.EXIT_ERROR
        if not isinstance(data, dict):
            output.eprint(f"{path} is not a JSON object; not modifying it")
            return output.EXIT_ERROR
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        output.eprint(f"{path}: mcpServers is not an object; not modifying it")
        return output.EXIT_ERROR

    name = entry_name(lane.name)
    url = f"http://127.0.0.1:{lane.port}"
    servers[name] = _updated_entry(servers.get(name), url, args.host,
                                   args.server_command, args.server_arg)
    data["mcpServers"] = servers

    if args.dry_run:
        print(f"# would write {path}")
        print(json.dumps({"mcpServers": {name: servers[name]}}, indent=2))
        return output.EXIT_OK

    try:
        backup = _write_atomic(path, data)
    except OSError as exc:
        output.eprint(f"could not write {path}: {exc}")
        return output.EXIT_ERROR
    print(f"{name} -> {url} (lane {lane.name}) written to {path}")
    if backup is not None:
        print(f"previous file backed up to {backup}")
    print(f"Restart {args.host} (or reload its MCP servers) to pick it up.")
    return output.EXIT_OK


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.action == "lanes":
        return _cmd_lanes()
    if args.action == "install":
        return _cmd_install(args)
    parser.print_help(file=sys.stderr)
    return output.EXIT_BAD_ARGS

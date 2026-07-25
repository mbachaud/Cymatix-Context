"""
Daemon entry: auto-memory → cymatix sync.

Reads config from cymatix.toml `[mem_sync]` section, honors env-var
overrides, starts the poll loop.

Usage:
    python scripts/run_mem_sync.py

Env-var overrides (take precedence over toml):
    CYMATIX_MEM_SYNC_URL        - cymatix server URL (default http://127.0.0.1:11437)
    CYMATIX_MEM_SYNC_INTERVAL   - poll interval in seconds (default 60)
    CYMATIX_MEM_SYNC_DIRS       - colon-separated dirs (overrides toml list)

Persona/agent attribution is automatic via the syncer process's env:
    CYMATIX_AGENT=raude         - which persona is doing the writes
    CYMATIX_USER=max            - the human principal
    CYMATIX_DEVICE=<hostname>   - auto-detected if unset
    CYMATIX_ORG=<org>           - optional

Set these once in your shell profile and every gene ingested from this
syncer carries the attribution automatically.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Add project root to path so `cymatix_context.mem_sync` resolves when
# running as a loose script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cymatix_context.mem_sync import run_daemon  # noqa: E402


def _load_toml_config() -> dict:
    """Read [mem_sync] from cymatix.toml. Returns {} if section missing."""
    try:
        import tomllib  # py3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return {}
    _repo_root = Path(__file__).resolve().parent.parent
    toml_path = _repo_root / "cymatix.toml"
    if not toml_path.exists():
        toml_path = _repo_root / "cymatix.toml"
    if not toml_path.exists():
        return {}
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        return data.get("mem_sync", {})
    except Exception as exc:
        print(f"[mem_sync] failed to read cymatix.toml: {exc}", file=sys.stderr)
        return {}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = _load_toml_config()
    if not cfg.get("enabled", False):
        print("[mem_sync] disabled in cymatix.toml — set [mem_sync].enabled=true",
              file=sys.stderr)
        return 1

    cymatix_url = (
        os.environ.get("CYMATIX_MEM_SYNC_URL")
        or cfg.get("cymatix_url", "http://127.0.0.1:11437")
    )
    interval = int(
        os.environ.get("CYMATIX_MEM_SYNC_INTERVAL")
        or cfg.get("sync_interval_s", 60)
    )
    env_dirs = os.environ.get("CYMATIX_MEM_SYNC_DIRS")
    if env_dirs:
        watch_dirs = [d.strip() for d in env_dirs.split(os.pathsep) if d.strip()]
    else:
        watch_dirs = list(cfg.get("watch_dirs", []))

    if not watch_dirs:
        print("[mem_sync] no watch_dirs configured — set [mem_sync].watch_dirs "
              "in cymatix.toml or export CYMATIX_MEM_SYNC_DIRS", file=sys.stderr)
        return 1

    # Expand ~ in paths — toml doesn't.
    watch_dirs = [os.path.expanduser(d) for d in watch_dirs]

    agent_kind = cfg.get("agent_kind") or os.environ.get("CYMATIX_AGENT_KIND")

    run_daemon(
        watch_dirs=watch_dirs,
        cymatix_url=cymatix_url,
        sync_interval_s=interval,
        agent_kind=agent_kind,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Vault pruner — TTL-based trace deletion + pre-prune rollup + _stale/ refresh.

Pruning uses filename-encoded `_exp<unix-epoch>` suffix to avoid YAML parsing
unexpired traces. The hard-cap force-prune handles pinned files past a
compliance retention window.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

import yaml as _yaml

log = logging.getLogger(__name__)

_TRACE_EXP_RE = re.compile(r"_exp(\d+)\.md$")
_FALLBACK_MTIME_HOURS = 24
_HARD_FALLBACK_DAYS = 30  # corrupt-filename traces older than this are pruned


def prune_traces(
    *,
    vault_root: Path,
    max_retention_hours_hard: int,
    rollup_enabled: bool,
    rollup_shard: str,
) -> dict:
    """Walk _traces/ and _traces-pinned/, prune expired and force-pruned.

    Returns: {pruned_count, force_pruned_count, rollup_appended, errors}
    """
    pruned = 0
    force_pruned = 0
    rollup_appended = 0
    errors = 0

    now = time.time()
    traces_dir = vault_root / "_traces"
    pinned_dir = vault_root / "_traces-pinned"

    if traces_dir.exists():
        for entry in traces_dir.iterdir():
            if not entry.is_file() or not entry.name.endswith(".md"):
                continue
            try:
                expires_unix = _parse_expiry_from_filename(entry)
                if expires_unix is None:
                    mtime = entry.stat().st_mtime
                    if (now - mtime) > _HARD_FALLBACK_DAYS * 86400:
                        if rollup_enabled:
                            _append_rollup(entry, vault_root, rollup_shard)
                            rollup_appended += 1
                        entry.unlink()
                        pruned += 1
                    continue
                if expires_unix < now:
                    if rollup_enabled:
                        _append_rollup(entry, vault_root, rollup_shard)
                        rollup_appended += 1
                    entry.unlink()
                    pruned += 1
            except OSError:
                log.warning("prune failed for %s", entry, exc_info=True)
                errors += 1

    if pinned_dir.exists() and max_retention_hours_hard > 0:
        cutoff = now - max_retention_hours_hard * 3600
        for entry in pinned_dir.iterdir():
            if not entry.is_file() or not entry.name.endswith(".md"):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    log.warning(
                        "vault_force_prune: %s past max_retention_hours_hard=%dh",
                        entry.name, max_retention_hours_hard,
                    )
                    if rollup_enabled:
                        _append_rollup(entry, vault_root, rollup_shard)
                        rollup_appended += 1
                    entry.unlink()
                    force_pruned += 1
            except OSError:
                log.warning("force-prune failed for %s", entry, exc_info=True)
                errors += 1

    return {
        "pruned_count": pruned,
        "force_pruned_count": force_pruned,
        "rollup_appended": rollup_appended,
        "errors": errors,
    }


def _parse_expiry_from_filename(path: Path) -> Optional[float]:
    m = _TRACE_EXP_RE.search(path.name)
    if not m:
        return None
    return float(m.group(1))


_ROLLUP_HEADER = (
    "# Trace rollup\n\n"
    "| time | request_id | latency_ms | health | trigger |\n"
    "|---|---|---|---|---|\n"
)


def _append_rollup(trace_path: Path, vault_root: Path, shard: str) -> None:
    """Append a one-line summary of `trace_path` to the appropriate rollup file."""
    fm = _read_trace_frontmatter(trace_path)
    if fm is None:
        return

    created_at = str(fm.get("created_at", ""))
    request_id = str(fm.get("request_id", "?"))
    latency = fm.get("total_latency_ms", "?")
    health = str(fm.get("health_status", "?"))
    trigger = str(fm.get("trigger_reason", "?"))

    # Parse the date for shard path. Expected format: YYYY-MM-DDTHH:MM:SSZ
    date_part = created_at[:10] if len(created_at) >= 10 else "unknown"
    hour_part = created_at[11:13] if len(created_at) >= 13 else "00"
    time_part = created_at[11:19] if len(created_at) >= 19 else created_at

    rollups_root = vault_root / "_meta" / "trace-rollups"
    if shard == "hour":
        rollup_path = rollups_root / date_part / f"{hour_part}.md"
    else:  # daily
        rollup_path = rollups_root / f"{date_part}.md"

    rollup_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not rollup_path.exists():
        rollup_path.write_text(_ROLLUP_HEADER, encoding="utf-8")

    new_row = f"| {time_part} | {request_id} | {latency} | {health} | {trigger} |\n"
    with rollup_path.open("a", encoding="utf-8") as f:
        f.write(new_row)


def _read_trace_frontmatter(path: Path) -> Optional[dict]:
    """Parse the YAML frontmatter block from a trace file."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("could not read trace %s", path, exc_info=True)
        return None
    if not content.startswith("---\n"):
        return None
    rest = content[len("---\n"):]
    try:
        end = rest.index("---\n")
    except ValueError:
        return None
    try:
        return _yaml.safe_load(rest[:end]) or {}
    except _yaml.YAMLError:
        log.warning("could not parse frontmatter in %s", path, exc_info=True)
        return None

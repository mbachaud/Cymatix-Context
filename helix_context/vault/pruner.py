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


def _append_rollup(trace_path: Path, vault_root: Path, shard: str) -> None:
    """Append a one-line summary of the trace to today's rollup file before deletion.

    The full implementation lives in Task 12; for Task 11 this is a stub.
    """
    # Stub for Task 11 — Task 12 fills in.
    pass

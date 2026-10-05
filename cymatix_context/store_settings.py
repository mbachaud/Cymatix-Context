"""Per-store settings: a small sidecar file beside each knowledge store.

``<store>.db`` gets ``<store>.db.cymatix.json`` holding that store's auto-sync
folders and its Freeze flag. The settings follow the file when it is moved or
copied, and reading them never opens the store, so a frozen store can be
described without touching it.

Freeze means read-only: the server opens the store with ``read_only`` set (its
upserts no-op), turns sync off, and ``/ingest`` refuses with a 409.

The sidecar is optional. With none, nothing changes: the global ``[sync]``
section applies and the store is writable. A ``sync`` block in the sidecar
overrides the global ``enabled``, ``roots`` and ``interval_s`` for this store
only; every other ``[sync]`` knob stays global.

Written by the launcher, read by the server at boot. A change takes effect on
the next server start.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

from .config import SyncConfig

log = logging.getLogger("cymatix.store_settings")

SIDECAR_SUFFIX = ".cymatix.json"
SCHEMA_VERSION = 1
MIN_INTERVAL_S = 5.0


@dataclass
class StoreSettings:
    frozen: bool = False
    # None = defer to the global [sync] section.
    sync_enabled: Optional[bool] = None
    sync_roots: List[str] = field(default_factory=list)
    sync_interval_s: Optional[float] = None


def sidecar_path(db_path: Union[str, Path]) -> Optional[Path]:
    """``<store>.db.cymatix.json``, or None for an in-memory store."""
    if str(db_path) == ":memory:":
        return None
    p = Path(db_path)
    return p.with_name(p.name + SIDECAR_SUFFIX)


def load_settings(db_path: Union[str, Path]) -> StoreSettings:
    """Read a store's settings; a missing or unreadable file means defaults.

    Fields of the wrong type are dropped rather than trusted, so a hand-edited
    file can never switch a store into a state the user did not ask for.
    """
    path = sidecar_path(db_path)
    if path is None or not path.exists():
        return StoreSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top level is not an object")
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable store settings %s: %s", path, exc)
        return StoreSettings()

    settings = StoreSettings()
    if raw.get("frozen") is True:
        settings.frozen = True
    sync = raw.get("sync")
    if isinstance(sync, dict):
        if isinstance(sync.get("enabled"), bool):
            settings.sync_enabled = sync["enabled"]
        roots = sync.get("roots")
        if isinstance(roots, list):
            settings.sync_roots = [r for r in roots if isinstance(r, str) and r.strip()]
        interval = sync.get("interval_s")
        if isinstance(interval, (int, float)) and not isinstance(interval, bool):
            settings.sync_interval_s = max(MIN_INTERVAL_S, float(interval))
    return settings


def save_settings(db_path: Union[str, Path], settings: StoreSettings) -> Path:
    """Write the sidecar atomically (temp file, then replace)."""
    path = sidecar_path(db_path)
    if path is None:
        raise ValueError("an in-memory store has no settings file")
    interval = settings.sync_interval_s
    payload = {
        "v": SCHEMA_VERSION,
        "frozen": bool(settings.frozen),
        "sync": {
            "enabled": settings.sync_enabled,
            "roots": list(settings.sync_roots),
            "interval_s": None if interval is None else max(MIN_INTERVAL_S, float(interval)),
        },
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def apply_to_sync_config(base: SyncConfig, settings: StoreSettings) -> SyncConfig:
    """The ``[sync]`` config this store actually runs with."""
    if settings.frozen:
        return dataclasses.replace(base, enabled=False)
    changes = {}
    if settings.sync_enabled is not None:
        changes["enabled"] = settings.sync_enabled
        # An explicit per-store switch brings its own folders, even none.
        changes["roots"] = list(settings.sync_roots)
    if settings.sync_interval_s is not None:
        changes["interval_s"] = settings.sync_interval_s
    return dataclasses.replace(base, **changes) if changes else base

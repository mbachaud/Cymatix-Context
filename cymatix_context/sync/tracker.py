"""Tracking state for delta sync, stored in the knowledge store itself.

Two tables, created on first use:

- ``sync_tracked``: one row per tracked file — the sha256/mtime/size last
  synced, so an unchanged file costs one ``stat`` per pass.
- ``sync_lock``: a single-row lease so only one process syncs a given store
  (two lanes or a stray second server on the same DB would otherwise both
  ingest every change).

Every statement runs under the store's writer RLock and commits on the
shared connection — the same rule ``identity/registry.py`` follows.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

_DDL = (
    "CREATE TABLE IF NOT EXISTS sync_tracked ("
    " path TEXT PRIMARY KEY,"
    " root TEXT NOT NULL,"
    " sha256 TEXT NOT NULL,"
    " mtime REAL NOT NULL,"
    " size INTEGER NOT NULL,"
    " synced_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_sync_tracked_root ON sync_tracked(root)",
    "CREATE TABLE IF NOT EXISTS sync_lock ("
    " id INTEGER PRIMARY KEY CHECK (id = 1),"
    " holder TEXT NOT NULL,"
    " heartbeat_at REAL NOT NULL)",
)


@dataclass(frozen=True)
class TrackedFile:
    sha256: str
    mtime: float
    size: int


class SyncTracker:
    def __init__(self, genome) -> None:
        self.genome = genome
        with self._wlock():
            for stmt in _DDL:
                self.genome.conn.execute(stmt)
            self.genome.conn.commit()

    def _wlock(self):
        lock = getattr(self.genome, "_write_lock", None)
        return lock if lock is not None else contextlib.nullcontext()

    # ── tracked files ──────────────────────────────────────────────

    def for_root(self, root: str) -> Dict[str, TrackedFile]:
        with self._wlock():
            rows = self.genome.conn.execute(
                "SELECT path, sha256, mtime, size FROM sync_tracked WHERE root = ?",
                (root,),
            ).fetchall()
        return {r[0]: TrackedFile(r[1], r[2], r[3]) for r in rows}

    def count(self) -> int:
        with self._wlock():
            return self.genome.conn.execute(
                "SELECT COUNT(*) FROM sync_tracked").fetchone()[0]

    def upsert(self, path: str, root: str, sha256: str, mtime: float, size: int) -> None:
        with self._wlock():
            self.genome.conn.execute(
                "INSERT INTO sync_tracked (path, root, sha256, mtime, size, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET root = excluded.root, "
                "sha256 = excluded.sha256, mtime = excluded.mtime, "
                "size = excluded.size, synced_at = excluded.synced_at",
                (path, root, sha256, mtime, size, time.time()),
            )
            self.genome.conn.commit()

    def delete(self, path: str) -> None:
        with self._wlock():
            self.genome.conn.execute("DELETE FROM sync_tracked WHERE path = ?", (path,))
            self.genome.conn.commit()

    # ── single-writer lease ────────────────────────────────────────

    def claim(self, holder: str, stale_after_s: float) -> Tuple[bool, Optional[str]]:
        """Take or renew the sync lease. Returns (ok, current holder)."""
        now = time.time()
        with self._wlock():
            row = self.genome.conn.execute(
                "SELECT holder, heartbeat_at FROM sync_lock WHERE id = 1").fetchone()
            if row is not None and row[0] != holder and now - row[1] < stale_after_s:
                return False, row[0]
            self.genome.conn.execute(
                "INSERT INTO sync_lock (id, holder, heartbeat_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET holder = excluded.holder, "
                "heartbeat_at = excluded.heartbeat_at",
                (holder, now),
            )
            self.genome.conn.commit()
        return True, holder

    def release(self, holder: str) -> None:
        with self._wlock():
            self.genome.conn.execute(
                "DELETE FROM sync_lock WHERE id = 1 AND holder = ?", (holder,))
            self.genome.conn.commit()

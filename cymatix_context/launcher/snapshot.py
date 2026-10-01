"""Consistent copies of a live knowledge store for staging lanes.

A staging lane serving a newer engine build must never open another lane's
live ``genome.db`` for writes: a newer build may migrate the schema, and
the persist / compaction / co-activation paths all write. It serves a
snapshot instead, taken with SQLite's online backup API — a consistent
point-in-time copy that includes pages still in the source's WAL, safe to
take while the source server is running.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, Union

PathLike = Union[str, Path]


class SnapshotError(RuntimeError):
    """The snapshot could not be taken; the destination is unchanged."""


def snapshot_store(source: PathLike, dest: PathLike) -> Dict[str, object]:
    """Copy *source* to *dest* atomically. The destination lane must be
    stopped (its old file is replaced). Returns a small receipt."""
    src = Path(source).resolve()
    dst = Path(dest).resolve()
    if not src.is_file():
        raise SnapshotError(f"snapshot source {src} does not exist")
    if src == dst:
        raise SnapshotError(f"snapshot source and destination are the same file: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.snapshot-tmp")
    tmp.unlink(missing_ok=True)

    t0 = time.monotonic()
    src_conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        dst_conn = sqlite3.connect(tmp)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    except sqlite3.Error as exc:
        tmp.unlink(missing_ok=True)
        raise SnapshotError(f"backup of {src} failed: {exc}") from exc
    finally:
        src_conn.close()

    # A stale -wal/-shm next to the old copy would be replayed onto the new
    # file by the next opener; clear them before swapping the file in.
    for suffix in ("-wal", "-shm"):
        Path(f"{dst}{suffix}").unlink(missing_ok=True)
    os.replace(tmp, dst)
    return {
        "source": str(src),
        "dest": str(dst),
        "bytes": dst.stat().st_size,
        "seconds": round(time.monotonic() - t0, 3),
        "taken_at": time.time(),
    }

"""The delta-sync pass.

Per root, per pass:

1. Walk the root (``include`` extensions, ``exclude`` directory names).
2. A file whose mtime and size match its tracked row is unchanged — one
   ``stat``, no read. Otherwise hash it; same sha256 = touched, not changed.
3. New or changed file: snapshot the source's live gene ids, ingest, then
   tombstone ``live_before - new_ids`` (keeping the deterministic parent doc
   when the file still chunks into 2+). gene_id is a content hash, so a
   chunk whose text now also lives in another file has that file as its
   ``source_id`` and is never in ``live_before`` — it cannot be hidden here.
4. Tracked file that vanished: tombstone its live genes, stop tracking.

Tombstones are soft (``compress_to_heterochromatin``): hot-tier retrieval
stops returning them, the rows and every foreign key that points at them
stay intact.

Guards: a root that is missing is skipped outright; a pass where more than
``max_delete_fraction`` of a root's tracked files vanished (and at least
two did) touches nothing in that root until a rescan with
``allow_mass_delete``; ``max_files_per_pass`` caps ingest work per pass.
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

from ..config import SyncConfig
from .tracker import SyncTracker

log = logging.getLogger("cymatix.sync")

# A lease older than this many intervals is treated as abandoned.
_LEASE_INTERVALS = 3
_MIN_LEASE_S = 60.0


@dataclass
class PassReport:
    started_at: float = field(default_factory=time.time)
    duration_s: float = 0.0
    ingested: int = 0
    unchanged: int = 0
    tombstoned: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: int = 0
    pending: int = 0
    guard_tripped: bool = False
    lock_held_by: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _content_type_for(path: Path) -> str:
    # Same code/text split as `cymatix ingest` (#224) so a synced file and a
    # CLI-ingested one chunk identically.
    from ..cli.cmd_ingest import _content_type_for as _cli_content_type
    return _cli_content_type(path)


def _record(event: str, n: int = 1) -> None:
    if n <= 0:
        return
    try:
        from ..telemetry.otel import sync_events_counter
        sync_events_counter().add(n, attributes={"event": event})
    except Exception:  # telemetry must never break a pass
        log.debug("sync telemetry failed", exc_info=True)


class SyncWorker:
    def __init__(self, cymatix, config: SyncConfig, holder: Optional[str] = None) -> None:
        self.cymatix = cymatix
        self.config = config
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}"
        self.roots: List[Path] = [Path(r).expanduser().resolve() for r in config.roots]
        self._include = {e.lower() for e in config.include}
        self._exclude = set(config.exclude)
        self.tracker = SyncTracker(cymatix.genome)
        self._pass_lock = threading.Lock()
        self._last: Optional[PassReport] = None
        self._guard_trips = 0
        self._missing_roots: List[str] = []

    # ── public surface ─────────────────────────────────────────────

    def run_pass(self, allow_mass_delete: bool = False) -> PassReport:
        """Run one pass (serialized: a rescan waits for a running pass)."""
        with self._pass_lock:
            report = self._run_pass(allow_mass_delete)
            self._last = report
        return report

    def status(self) -> dict:
        return {
            "enabled": True,
            "roots": [str(r) for r in self.roots],
            "missing_roots": list(self._missing_roots),
            "tracked": self.tracker.count(),
            "guard_trips": self._guard_trips,
            "interval_s": self.config.interval_s,
            "holder": self.holder,
            "last_pass": self._last.to_dict() if self._last else None,
        }

    def close(self) -> None:
        try:
            self.tracker.release(self.holder)
        except Exception:
            log.warning("sync lease release failed", exc_info=True)

    # ── pass ───────────────────────────────────────────────────────

    def _run_pass(self, allow_mass_delete: bool) -> PassReport:
        report = PassReport()
        if getattr(self.cymatix.genome, "read_only", False):
            # Frozen store (or one swapped to read-only after boot).
            log.info("sync pass skipped: the store is read-only")
            return report
        t0 = time.monotonic()
        stale_after = max(_MIN_LEASE_S, _LEASE_INTERVALS * self.config.interval_s)
        ok, holder = self.tracker.claim(self.holder, stale_after)
        if not ok:
            report.lock_held_by = holder
            log.info("sync pass skipped: store is synced by %s", holder)
            return report

        budget = self.config.max_files_per_pass
        missing: List[str] = []
        for root in self.roots:
            if not root.is_dir():
                missing.append(str(root))
                log.warning("sync root %s is missing; skipping it (nothing tombstoned)", root)
                continue
            budget = self._sync_root(root, report, budget, allow_mass_delete)
        self._missing_roots = missing

        report.duration_s = time.monotonic() - t0
        for event in ("ingested", "unchanged", "tombstoned", "deleted", "skipped", "errors"):
            _record(event, getattr(report, event))
        if report.guard_tripped:
            _record("guard_tripped")
        if report.ingested or report.tombstoned or report.errors:
            log.info("sync pass: %s", report.to_dict())
        return report

    def _sync_root(self, root: Path, report: PassReport, budget: int,
                   allow_mass_delete: bool) -> int:
        root_key = str(root)
        tracked = self.tracker.for_root(root_key)
        seen = set()
        work = []
        for path in self._walk(root):
            key = str(path)
            try:
                st = path.stat()
            except OSError:
                continue  # raced with a delete; next pass sees it gone
            seen.add(key)
            if st.st_size > self.config.max_file_bytes:
                report.skipped += 1
                continue
            row = tracked.get(key)
            if row is not None and row.mtime == st.st_mtime and row.size == st.st_size:
                report.unchanged += 1
                continue
            work.append((path, st, row))

        vanished = [p for p in tracked if p not in seen]
        if (not allow_mass_delete and len(vanished) >= 2
                and len(vanished) / len(tracked) > self.config.max_delete_fraction):
            report.guard_tripped = True
            self._guard_trips += 1
            log.warning(
                "sync mass-delete guard: %d of %d tracked files under %s vanished "
                "(> %.0f%%); touching nothing in this root. If the deletes are "
                "real, POST /sync/rescan with {\"allow_mass_delete\": true}.",
                len(vanished), len(tracked), root,
                self.config.max_delete_fraction * 100,
            )
            return budget

        for path, st, row in work:
            if budget <= 0:
                report.pending += 1
                continue
            try:
                data = path.read_bytes()
            except OSError:
                log.warning("sync: cannot read %s", path, exc_info=True)
                report.errors += 1
                continue
            sha = hashlib.sha256(data).hexdigest()
            if row is not None and row.sha256 == sha:
                self.tracker.upsert(str(path), root_key, sha, st.st_mtime, st.st_size)
                report.unchanged += 1
                continue
            budget -= 1
            try:
                report.tombstoned += self._reingest(path, data)
            except Exception:
                log.warning("sync: ingest failed for %s (retried next pass)",
                            path, exc_info=True)
                report.errors += 1
                continue
            self.tracker.upsert(str(path), root_key, sha, st.st_mtime, st.st_size)
            report.ingested += 1

        genome = self.cymatix.genome
        for key in vanished:
            report.tombstoned += len(genome.tombstone_genes(
                genome.live_gene_ids_for_source(key)))
            self.tracker.delete(key)
            report.deleted += 1
        return budget

    def _reingest(self, path: Path, data: bytes) -> int:
        """Ingest *path*'s new content; tombstone what it replaced."""
        source = str(path)
        genome = self.cymatix.genome
        before = set(genome.live_gene_ids_for_source(source))
        text = data.decode("utf-8", errors="replace")
        new_ids = self.cymatix.ingest(text, _content_type_for(path), {"path": source}) or []
        keep = set(new_ids)
        if len(new_ids) >= 2:
            keep.add(self.cymatix._make_parent_doc_id(source))
        return len(genome.tombstone_genes(sorted(before - keep)))

    def _walk(self, root: Path) -> Iterator[Path]:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in self._exclude)
            for name in sorted(filenames):
                if os.path.splitext(name)[1].lower() in self._include:
                    yield Path(dirpath) / name

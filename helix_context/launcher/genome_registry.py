"""
Genome registry — discover and describe local genome (.db) databases.

Used by the system-tray "Manage Database" submenu and the dashboard's
Database panel. Both surfaces need the same answers:

    - Which genome is currently selected?
    - What other genomes are built and selectable?
    - For each genome, what source folders are inside it?

Genomes are plain SQLite files on disk. There is no central registry —
this module walks a small set of well-known directories under the repo
root and asks each `.db` file two cheap SQLite questions:

    SELECT COUNT(*) FROM genes
    SELECT DISTINCT source_id FROM genes LIMIT 2000

A `(path, mtime)` keyed cache prevents the tray from re-scanning on every
menu open. Results are cheap to recompute when a genome actually changes.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

log = logging.getLogger("helix.launcher.genome_registry")

# How many `source_id` rows to read when computing the folder summary. A
# few thousand is enough to surface the top folders without scanning a
# 500k-document corpus.
_SOURCE_SAMPLE_LIMIT = 2000

# How deep below the well-known roots to walk when discovering `.db` files.
_DISCOVERY_MAX_DEPTH = 3

# Well-known directories under the repo root that may contain genomes.
# Order is informational — the active genome always sorts first regardless.
_DISCOVERY_ROOTS = ("genomes", "benchmarks")


@dataclass
class FolderEntry:
    """One source-folder bucket inside a genome."""
    prefix: str
    document_count: int


@dataclass
class GenomeInfo:
    """Descriptor for one discoverable genome file."""
    name: str
    path: Path
    size_bytes: int
    mtime: float
    total_genes: int
    folders: List[FolderEntry] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def is_readable(self) -> bool:
        return self.error is None and self.total_genes > 0

    def as_dict(self) -> Dict:
        return {
            "name": self.name,
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "size_mb": round(self.size_bytes / (1024 * 1024), 1),
            "mtime": self.mtime,
            "total_genes": self.total_genes,
            "folders": [
                {"prefix": f.prefix, "document_count": f.document_count}
                for f in self.folders
            ],
            "error": self.error,
        }


# ── cache ─────────────────────────────────────────────────────────────

_CACHE: Dict[str, GenomeInfo] = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(path: Path, mtime: float, size: int) -> str:
    return f"{path}|{mtime:.3f}|{size}"


# ── discovery ─────────────────────────────────────────────────────────


def _repo_root() -> Path:
    """The helix-context repo root, used as the base for discovery walks."""
    return Path(__file__).resolve().parent.parent.parent


def _walk_for_dbs(root: Path, max_depth: int) -> List[Path]:
    """Walk `root` up to `max_depth` directories deep collecting *.db files.

    Skips hidden directories (`.git`, `.claude`, `.venv` etc.) and any
    obvious junk paths. Errors during traversal are logged and swallowed.
    """
    found: List[Path] = []
    if not root.exists() or not root.is_dir():
        return found
    base_depth = len(root.parts)
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            depth = len(Path(dirpath).parts) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.endswith(".db") and not name.endswith("-journal"):
                    found.append(Path(dirpath) / name)
    except OSError:
        log.debug("walk failed under %s", root, exc_info=True)
    return found


def _candidate_paths(active_path: Optional[Path]) -> List[Path]:
    """Build a deduplicated, ordered list of candidate genome paths."""
    repo = _repo_root()
    seen: set = set()
    out: List[Path] = []

    def _add(p: Path) -> None:
        try:
            resolved = p.resolve()
        except OSError:
            return
        key = str(resolved).lower()
        if key in seen:
            return
        if not resolved.exists() or not resolved.is_file():
            return
        seen.add(key)
        out.append(resolved)

    if active_path is not None:
        _add(active_path)
    _add(repo / "genome.db")
    for sub in _DISCOVERY_ROOTS:
        for db in _walk_for_dbs(repo / sub, _DISCOVERY_MAX_DEPTH):
            _add(db)
    return out


# ── per-genome introspection ──────────────────────────────────────────


def _summarize_folders(rows: Sequence[str]) -> List[FolderEntry]:
    """Bucket raw `source_id` paths into top-level folder summaries.

    Strategy: take the first two path components of each source_id and
    use that as the folder bucket. A document at
    "F:/Projects/onyx/backend/models.py" becomes "F:/Projects/onyx".
    Bare filenames with no separator fall into a "(loose files)" bucket.
    Returned sorted by document_count desc, capped at 12 entries.
    """
    counter: Counter = Counter()
    for src in rows:
        if not src:
            counter["(unknown)"] += 1
            continue
        norm = str(src).replace("\\", "/").lstrip("/")
        parts = [p for p in norm.split("/") if p]
        if len(parts) <= 1:
            counter["(loose files)"] += 1
        else:
            counter[f"{parts[0]}/{parts[1]}"] += 1
    entries = [
        FolderEntry(prefix=prefix, document_count=count)
        for prefix, count in counter.most_common(12)
    ]
    return entries


def _make_label(path: Path) -> str:
    """Human-readable name for a genome file.

    Files literally named ``genome.db`` or ``main.genome.db`` are
    ambiguous on their own — sharded benches stash one such file per
    shard directory — so prefix the parent directory to keep entries
    distinguishable in the tray menu and dashboard list.
    """
    stem = path.stem
    if stem in ("genome", "main.genome"):
        return f"{path.parent.name}/{stem}"
    return stem


def _read_genome_info(path: Path) -> GenomeInfo:
    """Inspect a single .db file. Never raises — errors surface via .error."""
    try:
        stat = path.stat()
    except OSError as exc:
        return GenomeInfo(
            name=_make_label(path),
            path=path,
            size_bytes=0,
            mtime=0.0,
            total_genes=0,
            error=f"stat failed: {exc}",
        )

    cache_key = _cache_key(path, stat.st_mtime, stat.st_size)
    with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
        if hit is not None:
            return hit

    info = GenomeInfo(
        name=_make_label(path),
        path=path,
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
        total_genes=0,
    )

    try:
        # `uri=True` + `mode=ro` makes the read non-locking and safe to
        # run against a database that helix has open for writes.
        conn = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        try:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute("SELECT COUNT(*) FROM genes").fetchone()
            info.total_genes = int(row[0]) if row else 0
            sources = [
                str(r[0])
                for r in conn.execute(
                    "SELECT DISTINCT source_id FROM genes "
                    "WHERE source_id IS NOT NULL LIMIT ?",
                    (_SOURCE_SAMPLE_LIMIT,),
                )
            ]
            info.folders = _summarize_folders(sources)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        info.error = f"sqlite: {exc}"
    except Exception as exc:
        info.error = f"read failed: {exc}"

    with _CACHE_LOCK:
        _CACHE[cache_key] = info
    return info


# ── durable selection state (issue #286) ──────────────────────────────
#
# `select_genome` used to set HELIX_GENOME_PATH in the launcher process env
# ONLY, so a Quit + relaunch (desktop icon, new shell) silently reverted to
# the helix.toml genome. We now also write the choice to a tiny JSON file
# co-located with the launcher state (~/.helix/launcher/selected_genome.json)
# and consult it in `active_genome_path`, so the tray's genome choice sticks.

_SELECTION_FILENAME = "selected_genome.json"


def _selection_state_path() -> Path:
    """Path to the durable genome-selection record.

    Co-located with the launcher state at ``~/.helix/launcher/``. Honors
    ``HELIX_LAUNCHER_STATE_DIR`` so tests (and unusual deployments) can
    redirect it without monkeypatching.
    """
    override = os.environ.get("HELIX_LAUNCHER_STATE_DIR")
    base = Path(override) if override else (Path.home() / ".helix" / "launcher")
    return base / _SELECTION_FILENAME


def _read_selected_genome() -> Optional[Path]:
    """Return the durably-persisted genome selection, or None.

    Returns None (rather than a stale path) if the file is missing,
    unreadable, or points at a genome that no longer exists on disk.
    """
    path = _selection_state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        log.debug("Failed to read genome selection state %s", path, exc_info=True)
        return None
    candidate = raw.get("genome_path") if isinstance(raw, dict) else None
    if not candidate:
        return None
    resolved = Path(candidate)
    if resolved.exists() and resolved.is_file():
        return resolved
    log.debug("Persisted genome %s no longer exists; ignoring", resolved)
    return None


def _write_selected_genome(path: Path) -> None:
    """Atomically persist `path` as the durable genome selection."""
    dest = _selection_state_path()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(
            json.dumps({"genome_path": str(path)}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, dest)
    except Exception:
        # Persistence is best-effort — the in-process env var (set by
        # select_genome) still drives the current session even if the
        # durable write fails.
        log.warning("Failed to persist genome selection to %s", dest, exc_info=True)


def clear_selection() -> None:
    """Remove the durable genome-selection record (used by tests / reset)."""
    try:
        _selection_state_path().unlink()
    except FileNotFoundError:
        pass
    except Exception:
        log.debug("Failed to clear genome selection state", exc_info=True)


def apply_persisted_selection() -> Optional[Path]:
    """Seed HELIX_GENOME_PATH from the durable selection at launcher start.

    Called once early in launcher startup (before the supervisor spawns
    helix) so the spawned helix subprocess inherits the persisted genome
    via its environment. An explicit HELIX_GENOME_PATH already in the
    environment always wins — a bench wrapper .bat or a developer's shell
    export must not be overridden by a stale tray selection.

    Returns the applied path, or None if nothing was applied.
    """
    if os.environ.get("HELIX_GENOME_PATH"):
        return None
    selected = _read_selected_genome()
    if selected is None:
        return None
    resolved = str(selected.resolve())
    os.environ["HELIX_GENOME_PATH"] = resolved
    log.info("Applied persisted genome selection: %s", resolved)
    return selected.resolve()


# ── public API ────────────────────────────────────────────────────────


def active_genome_path() -> Path:
    """Resolve the genome path the supervised helix will load on next start.

    Resolution order:
      1. HELIX_GENOME_PATH env var (explicit override — matches
         `helix_context.config.load_config`)
      2. Durable tray selection (~/.helix/launcher/selected_genome.json,
         issue #286) — survives Quit + relaunch
      3. [genome] path in helix.toml
      4. Default "genome.db" relative to the helix repo root
    """
    env = os.environ.get("HELIX_GENOME_PATH")
    if env:
        return Path(env).resolve()
    persisted = _read_selected_genome()
    if persisted is not None:
        return persisted.resolve()
    try:
        from helix_context.config import load_config
        cfg = load_config()
        path = Path(cfg.genome.path)
    except Exception:
        log.debug("load_config failed; falling back to repo-root default", exc_info=True)
        path = Path("genome.db")
    if not path.is_absolute():
        path = (_repo_root() / path).resolve()
    return path


def discover_genomes() -> List[GenomeInfo]:
    """Return the list of available genomes, active one first.

    Results are cached per `(path, mtime, size)`. A genome file that has
    been edited since last call gets re-read.
    """
    active = active_genome_path()
    paths = _candidate_paths(active)
    out: List[GenomeInfo] = [_read_genome_info(p) for p in paths]
    active_resolved = str(active).lower()

    def _sort_key(info: GenomeInfo) -> tuple:
        is_active = str(info.path).lower() == active_resolved
        return (0 if is_active else 1, -info.mtime, str(info.path).lower())

    out.sort(key=_sort_key)
    return out


def get_genome_info(path: Path) -> GenomeInfo:
    """Public single-genome accessor; same caching as `discover_genomes`."""
    return _read_genome_info(Path(path).resolve())


def is_active(info: GenomeInfo) -> bool:
    """True iff `info` describes the currently configured active genome."""
    return str(info.path).lower() == str(active_genome_path()).lower()


def select_genome(path: Path) -> Path:
    """Mark `path` as the genome to use on the next helix start.

    Sets `HELIX_GENOME_PATH` in the current process so the supervisor's
    next subprocess.Popen inherits it, AND writes a durable selection
    record (issue #286) so the choice survives a launcher Quit + relaunch.
    Does NOT restart helix — callers that want a hot swap must invoke
    `supervisor.restart()` separately. Returns the resolved absolute path
    that was written.
    """
    resolved = Path(path).resolve()
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(f"Genome not found: {resolved}")
    os.environ["HELIX_GENOME_PATH"] = str(resolved)
    _write_selected_genome(resolved)
    log.info("Selected genome: %s", resolved)
    return resolved


def clear_cache() -> None:
    """Drop all cached `GenomeInfo` entries — used by tests."""
    with _CACHE_LOCK:
        _CACHE.clear()

"""Build the test genomes per ``docs/benchmarks/GENOME_FIXTURE_MATRIX.md``.

Monolithic profiles (default, ``--mode blob``)
----------------------------------------------
small   4 roots — BookKeeper, CosmicTasha, two-brain-audit, MaxExpressKit
medium  6 roots — small + Education + helix-context (helix-context skips .db sidecars)
large   1 root  — F:/Projects whole tree, standard denylist
xl     13 roots — F:/Projects + 12 selected Steam/game installs (code/scripts/configs only)

Output (blob mode):
    <out-dir>/<profile>.db
    <out-dir>/manifest.json

Sharded profiles (``--mode sharded``)
-------------------------------------
Builds one shard per source root + a ``main.genome.db`` routing DB. The
matrix doc reserves ``medium-sharded`` and ``xl-sharded``; ``--mode sharded``
also accepts ``small`` and ``large`` for smoke-testing.

Output (sharded mode):
    <out-dir>-sharded/<profile>/main.genome.db
    <out-dir>-sharded/<profile>/<drive>/<mirrored-path>/<label>.genome.db
    <out-dir>-sharded/manifest.json

By default ``<out-dir> = F:/Projects/helix-context/genomes/bench/matrix``.

Usage
-----
    python scripts/build_fixture_matrix.py --profile small
    python scripts/build_fixture_matrix.py --profile small,medium
    python scripts/build_fixture_matrix.py --profile all
    python scripts/build_fixture_matrix.py --profile xl --out-dir F:/tmp/bench
    python scripts/build_fixture_matrix.py --profile medium --mode sharded
    python scripts/build_fixture_matrix.py --profile xl --mode sharded

Parallel modes (issue #92)
--------------------------
    --parallel              File-level mp.Pool + batched-SPLADE writer
                            (blob mode only).
    --workers N             Override worker count for --parallel
                            (0 = auto via helix_context.parallel.auto_workers).
    --shard-workers N       Run sharded builds with N concurrent shard
                            processes. 0 = auto from VRAM + CPU.
    --batch-size N          SPLADE batch size in the writer (default 64).

Examples:
    python scripts/build_fixture_matrix.py --profile medium --parallel
    python scripts/build_fixture_matrix.py --profile xl --parallel --workers 6
    python scripts/build_fixture_matrix.py --profile xl --mode sharded --shard-workers 3

The script does not talk to the running Helix server -- it builds fresh
SQLite files directly. Use ``POST /admin/swap-db`` with ``mode="blob"``
or ``mode="sharded"`` (or the ``helix_swap_db`` MCP tool) to mount one
of the resulting files into a running server without restarting.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helix_context.tagger import CpuTagger
from helix_context.genome import Genome
from helix_context.codons import CodonChunker
from helix_context.sharding import corpus_shard_db, main_db_path
from helix_context.shard_schema import (
    init_main_db,
    open_main_db,
    register_shard,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bench.matrix")


# ── Extension allow-list ──────────────────────────────────────────────────

TEXT_EXTS = {".txt", ".md", ".cfg", ".ini", ".conf", ".properties", ".vdf", ".acf"}
CODE_EXTS = {
    ".lua", ".py", ".cs", ".js", ".json", ".yaml", ".yml", ".toml",
    ".bat", ".sh", ".html", ".rs", ".go", ".java", ".c", ".cpp", ".h",
    ".rb", ".ts", ".tsx", ".jsx", ".sql", ".r", ".ps1",
}
INGEST_EXTS = TEXT_EXTS | CODE_EXTS


# ── Directory denylist (common across all profiles) ───────────────────────

SKIP_DIRS_COMMON = {
    # Build / cache / dependency
    "shadercache", "temp", "downloading", "depotcache", "__pycache__",
    ".git", "node_modules", "Mono", "MonoBleedingEdge", ".venv", "venv",
    "dist", "build", ".pytest_cache", "target", ".claude", ".serena",
    ".next", ".turbo", ".cache", ".ruff_cache",
    # Windows system
    "$RECYCLE.BIN", "System Volume Information", "WpSystem",
    "WUDownloadCache", "WindowsApps",
    # Helix-internal artifacts that should not pollute benches
    "benchmarks", "cwola_export", "logs", "training",
    "Helix-backup blobs", "D3D12",
}


# ── Per-profile file-extension exclusions ─────────────────────────────────

# SQLite sidecar suffixes that match by full-name pattern, not just ext.
SQLITE_SIDECAR_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm",
                           ".sqlite-wal", ".sqlite-shm")


MAX_FILE_SIZE = 200_000
MIN_FILE_SIZE = 50


# ── File -> gene-dict helper (shared by sequential + parallel paths) ──────

_worker_chunker = None
_worker_tagger = None


def _init_worker():
    """Per-worker init for mp.Pool -- loads tagger + chunker once."""
    global _worker_chunker, _worker_tagger
    from helix_context.codons import CodonChunker
    from helix_context.tagger import CpuTagger
    _worker_chunker = CodonChunker()
    _worker_tagger = CpuTagger()


def _chunk_and_tag_file(args: tuple[str, str]) -> list[dict]:
    """Read a single file, return list of Gene dicts.

    Runs in either the main process (sequential helper path) or in an
    ``mp.Pool`` worker (parallel path). Workers must have called
    :func:`_init_worker` first; the main process also initialises the
    module-level chunker/tagger before calling this helper.

    Returns ``model_dump()`` dicts (not Gene instances) so the mp.Pool
    can hand results back to the parent process across its IPC boundary.
    """
    fpath, ext = args
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return []

    ct = "code" if ext in CODE_EXTS else "text"
    strands = _worker_chunker.chunk(content, content_type=ct)
    genes: list[dict] = []
    for i, strand in enumerate(strands):
        try:
            gene = _worker_tagger.pack(
                strand.content,
                content_type=ct,
                source_id=fpath,
                sequence_index=i,
            )
            gene.is_fragment = strand.is_fragment
            genes.append(gene.model_dump())
        except Exception:
            pass
    return genes


# ── File discovery iterator (drop-in for ingest_tree's walk) ─────────────


def _iter_ingestable_files(
    roots: list[str],
    skip_dirs: set[str],
    extra_filename_filters: list,
    stats: dict,
) -> list[tuple[str, str]]:
    """Walk ``roots`` and return [(fpath, ext)] passing all filters.

    Updates ``stats['missing_roots']`` and ``stats['skipped']`` in place.
    """
    files: list[tuple[str, str]] = []
    for root in roots:
        if not os.path.exists(root):
            log.warning("root %s does not exist, skipping", root)
            stats["missing_roots"].append(root)
            continue
        log.info("=== Discovering %s ===", root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in INGEST_EXTS:
                    stats["skipped"] += 1
                    continue
                fpath = os.path.join(dirpath, fname)
                if any(f(fpath) for f in extra_filename_filters):
                    stats["skipped"] += 1
                    continue
                try:
                    size = os.path.getsize(fpath)
                except OSError:
                    continue
                if size < MIN_FILE_SIZE or size > MAX_FILE_SIZE:
                    stats["skipped"] += 1
                    continue
                files.append((fpath, ext))
    return files


# ── Batched-SPLADE writer (drains gene dicts -> genome) ──────────────────


def _drain_with_batched_splade(
    gene_dict_iter,
    genome,
    stats: dict,
    batch_size: int = 64,
) -> None:
    """Drain ``gene_dict_iter`` (yielding lists of gene dicts per file)
    into ``genome``. SPLADE encoding is batched across ``batch_size`` genes
    instead of per-gene. Stats are updated in place.
    """
    from helix_context.backends import splade_backend
    from helix_context.schemas import Gene

    buf: list = []  # Gene instances buffered before batch flush

    def _flush(batch: list) -> None:
        if not batch:
            return
        sparses = splade_backend.encode_batch(
            [g.content[:1000] for g in batch]
        )
        for g, sp in zip(batch, sparses):
            try:
                genome.upsert_doc(g, apply_gate=True, splade_sparse=sp)
                stats["genes"] += 1
            except Exception:
                stats["errors"] += 1
        if stats["genes"] % 500 < batch_size and stats["genes"] > 0:
            elapsed = time.perf_counter() - stats["t0"]
            log.info(
                "[%d files, %d genes] %.1f genes/s",
                stats["files"], stats["genes"],
                stats["genes"] / max(elapsed, 0.001),
            )

    for gene_dicts in gene_dict_iter:
        if not gene_dicts:
            stats["errors"] += 1
            continue
        for gd in gene_dicts:
            try:
                buf.append(Gene(**gd))
            except Exception:
                stats["errors"] += 1
        stats["files"] += 1
        while len(buf) >= batch_size:
            _flush(buf[:batch_size])
            del buf[:batch_size]

    if buf:
        _flush(buf)


# ── Parallel mode: file-level mp.Pool + main-process writer ──────────────


def _parallel_ingest_to_genome(
    files: list[tuple[str, str]],
    genome,
    stats: dict,
    n_workers: int,
    batch_size: int = 64,
    chunksize: int = 4,
) -> None:
    """Chunk+tag files in parallel via ``mp.Pool``; drain into ``genome``
    via the batched-SPLADE writer in the main process.

    Caller is responsible for opening / closing ``genome``.
    """
    import multiprocessing as mp

    log.info(
        "parallel ingest: %d files, %d workers, batch_size=%d",
        len(files), n_workers, batch_size,
    )

    with mp.Pool(n_workers, initializer=_init_worker) as pool:
        gene_dict_iter = pool.imap_unordered(
            _chunk_and_tag_file, files, chunksize=chunksize,
        )
        _drain_with_batched_splade(
            gene_dict_iter, genome, stats, batch_size=batch_size,
        )


# ── Profile definitions ───────────────────────────────────────────────────

PROFILES: dict[str, dict] = {
    "small": {
        "label": "Focused project smoke corpus",
        "active_roots": 4,
        "roots": [
            r"F:\Projects\BookKeeper",
            r"F:\Projects\CosmicTasha",
            r"F:\Projects\two-brain-audit",
            r"F:\Projects\MaxExpressKit",
        ],
        # No extra denials beyond common.
        "extra_skip_dirs": set(),
        "extra_filename_filters": [],
    },
    "medium": {
        "label": "Broader project corpus",
        # NOTE: the published matrix doc summary says "6 active roots" — that
        # count includes Education plus helix-context. The 2026-05-13 doc
        # body listed only 5 paths; Education was the missing 6th. Authoritative
        # here.
        "active_roots": 6,
        "roots": [
            r"F:\Projects\BookKeeper",
            r"F:\Projects\CosmicTasha",
            r"F:\Projects\two-brain-audit",
            r"F:\Projects\MaxExpressKit",
            r"F:\Projects\Education",
            r"F:\Projects\helix-context",
        ],
        "extra_skip_dirs": set(),
        # Skip any .db / .sqlite sidecars under helix-context. The walker
        # already filters by extension; this is just an extra safety belt.
        "extra_filename_filters": [
            lambda path: any(
                path.lower().endswith(s) for s in SQLITE_SIDECAR_SUFFIXES
            ),
        ],
    },
    "large": {
        "label": "Full projects corpus",
        "active_roots": 1,
        "roots": [r"F:\Projects"],
        "extra_skip_dirs": set(),
        "extra_filename_filters": [
            lambda path: any(
                path.lower().endswith(s) for s in SQLITE_SIDECAR_SUFFIXES
            ),
        ],
    },
    "xl": {
        "label": "Projects plus external Steam/game code corpus",
        "active_roots": 13,
        "roots": [
            r"F:\Projects",
            r"F:\Factorio",
            r"F:\SteamLibrary\steamapps\common\Universe Sandbox 2",
            r"F:\SteamLibrary\steamapps\common\Satisfactory Modeler",
            r"F:\SteamLibrary\steamapps\common\Dyson Sphere Program",
            r"F:\SteamLibrary\steamapps\common\Cities Skylines II",
            r"E:\SteamLibrary\steamapps\common\SpaceEngineers2",
            r"E:\SteamLibrary\steamapps\common\BeamNG.drive",
            r"D:\SteamLibrary\steamapps\common\Kerbal Space Program",
            r"D:\SteamLibrary\steamapps\common\Turing Complete",
            r"C:\Program Files (x86)\Steam\steamapps\common\The Farmer Was Replaced",
            r"C:\Program Files (x86)\Steam\steamapps\common\Stationeers",
        ],
        # Game asset / save / cache directories that may sneak through.
        "extra_skip_dirs": {
            "saves", "Saves", "SaveGame", "SaveGames",
            "screenshots", "Screenshots",
            "crashdump", "CrashDump", "Crashes",
            "PlayerData", "Recordings",
        },
        "extra_filename_filters": [
            lambda path: any(
                path.lower().endswith(s) for s in SQLITE_SIDECAR_SUFFIXES
            ),
        ],
    },
}


# ── Walk + ingest ─────────────────────────────────────────────────────────


def ingest_tree(
    root: str,
    genome: Genome,
    tagger: CpuTagger,
    chunker: CodonChunker,
    stats: dict,
    skip_dirs: set[str],
    extra_filename_filters: list,
) -> None:
    """Walk ``root`` and ingest matching files, respecting ``skip_dirs``."""
    if not os.path.exists(root):
        log.warning("root %s does not exist, skipping", root)
        stats["missing_roots"].append(root)
        return

    log.info("=== Ingesting %s ===", root)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]

        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in INGEST_EXTS:
                stats["skipped"] += 1
                continue

            fpath = os.path.join(dirpath, fname)

            # Per-profile filename filters
            if any(f(fpath) for f in extra_filename_filters):
                stats["skipped"] += 1
                continue

            try:
                size = os.path.getsize(fpath)
            except OSError:
                continue

            if size < MIN_FILE_SIZE or size > MAX_FILE_SIZE:
                stats["skipped"] += 1
                continue

            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                stats["errors"] += 1
                continue

            ct = "code" if ext in CODE_EXTS else "text"
            strands = chunker.chunk(content, content_type=ct)
            for i, strand in enumerate(strands):
                try:
                    gene = tagger.pack(
                        strand.content,
                        content_type=ct,
                        source_id=fpath,
                        sequence_index=i,
                    )
                    gene.is_fragment = strand.is_fragment
                    genome.upsert_gene(gene)
                    stats["genes"] += 1
                except Exception:
                    stats["errors"] += 1

            stats["files"] += 1

            if stats["genes"] % 500 == 0 and stats["genes"] > 0:
                elapsed = time.perf_counter() - stats["t0"]
                rate = stats["genes"] / max(elapsed, 0.001)
                log.info(
                    "[%d files, %d genes] %.1f genes/s | %s",
                    stats["files"], stats["genes"], rate,
                    os.path.basename(dirpath)[:60],
                )


# ── Build one profile ─────────────────────────────────────────────────────


def build_profile(
    name: str,
    db_path: str,
    parallel: bool = False,
    n_workers: int = 0,
    batch_size: int = 64,
    chunksize: int = 4,
) -> dict:
    """Build the profile named ``name`` into a fresh ``.db`` at ``db_path``.

    When ``parallel=True`` use the mp.Pool + batched-SPLADE path (issue
    #92). When False (default) preserve the original sequential
    :func:`ingest_tree` behaviour byte-for-byte.
    """
    profile = PROFILES[name]

    out_dir = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(out_dir, exist_ok=True)

    if os.path.exists(db_path):
        log.info("removing existing %s", db_path)
        os.remove(db_path)
        for suffix in ("-wal", "-shm"):
            sidecar = db_path + suffix
            if os.path.exists(sidecar):
                os.remove(sidecar)

    log.info("opening fresh genome at %s", db_path)
    genome = Genome(
        path=db_path,
        synonym_map={},
        splade_enabled=True,
        entity_graph=True,
    )

    skip_dirs = SKIP_DIRS_COMMON | profile["extra_skip_dirs"]
    extra_filename_filters = profile["extra_filename_filters"]

    stats = {
        "profile": name,
        "label": profile["label"],
        "active_roots": profile["active_roots"],
        "roots": profile["roots"],
        "db_path": db_path,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "files": 0,
        "genes": 0,
        "skipped": 0,
        "errors": 0,
        "missing_roots": [],
        "t0": time.perf_counter(),
        "mode": "parallel" if parallel else "sequential",
    }

    if parallel:
        from helix_context.parallel import auto_workers
        if n_workers <= 0:
            n_workers = auto_workers()
        files = _iter_ingestable_files(
            profile["roots"], skip_dirs, extra_filename_filters, stats,
        )
        stats["discovered_files"] = len(files)
        _parallel_ingest_to_genome(
            files=files,
            genome=genome,
            stats=stats,
            n_workers=n_workers,
            batch_size=batch_size,
            chunksize=chunksize,
        )
        stats["workers"] = n_workers
    else:
        tagger = CpuTagger()
        chunker = CodonChunker()
        for root in profile["roots"]:
            ingest_tree(
                root=root,
                genome=genome,
                tagger=tagger,
                chunker=chunker,
                stats=stats,
                skip_dirs=skip_dirs,
                extra_filename_filters=extra_filename_filters,
            )

    elapsed = time.perf_counter() - stats["t0"]
    stats["elapsed_s"] = round(elapsed, 1)
    stats["finished_at"] = datetime.now(timezone.utc).isoformat()

    genome_stats = genome.stats()
    stats["total_genes"] = genome_stats.get("total_genes", 0)
    stats["compression_ratio"] = round(genome_stats.get("compression_ratio", 0.0), 4)

    try:
        hl_row = genome.conn.execute(
            "SELECT COUNT(*) AS n FROM harmonic_links"
        ).fetchone()
        stats["harmonic_links"] = int(hl_row["n"]) if hl_row else 0
    except Exception:
        stats["harmonic_links"] = 0

    try:
        stats["bytes"] = os.path.getsize(db_path)
    except OSError:
        stats["bytes"] = -1

    log.info("=" * 60)
    log.info("DONE %s (%s) in %.1fs", name, stats["mode"], elapsed)
    log.info("  files=%d genes=%d skipped=%d errors=%d",
             stats["files"], stats["genes"], stats["skipped"], stats["errors"])
    log.info("  total_genes=%d harmonic_links=%d bytes=%d",
             stats["total_genes"], stats["harmonic_links"], stats["bytes"])
    if stats["missing_roots"]:
        log.warning("  missing roots: %s", stats["missing_roots"])

    genome.close()

    # Drop perf_counter t0 before serializing
    stats.pop("t0", None)
    return stats


# ── Sharded build ─────────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug_for_root(root: str) -> str:
    """Stable lowercase slug for a source root's last path segment."""
    base = os.path.basename(root.rstrip("/\\")) or root
    slug = _SLUG_RE.sub("-", base.lower()).strip("-")
    return slug or "root"


def _build_one_shard(
    label: str,
    root: str,
    shard_db_path: str,
    skip_dirs: set[str],
    extra_filename_filters: list,
    use_batched_splade: bool = True,
    batch_size: int = 64,
) -> dict:
    """Build a single shard ``.db`` for ``root``. Returns the shard's
    fingerprint payload + stats -- caller writes rows into main.db.

    Runs end-to-end in one process: discover files, chunk+tag, batched
    SPLADE upsert. Used by both the serial sharded build (called from
    the parent process) and the parallel pool (called inside subprocesses
    via :func:`_shard_worker_entry`).
    """
    p = Path(shard_db_path)
    if p.exists():
        p.unlink()
        for s in (str(p) + "-wal", str(p) + "-shm"):
            if os.path.exists(s):
                os.remove(s)
    p.parent.mkdir(parents=True, exist_ok=True)

    shard = Genome(
        path=str(p), synonym_map={},
        splade_enabled=True, entity_graph=True,
    )
    s_stats = {
        "files": 0, "genes": 0, "skipped": 0, "errors": 0,
        "missing_roots": [],
        "t0": time.perf_counter(),
    }
    try:
        if use_batched_splade:
            files = _iter_ingestable_files(
                [root], skip_dirs, extra_filename_filters, s_stats,
            )
            _init_worker()  # fill module-level chunker/tagger
            gen = (_chunk_and_tag_file(f) for f in files)
            _drain_with_batched_splade(
                gen, shard, s_stats, batch_size=batch_size,
            )
        else:
            tagger = CpuTagger()
            chunker = CodonChunker()
            ingest_tree(
                root=root,
                genome=shard,
                tagger=tagger,
                chunker=chunker,
                stats=s_stats,
                skip_dirs=skip_dirs,
                extra_filename_filters=extra_filename_filters,
            )

        gene_count = shard.stats().get("total_genes", 0)
        try:
            byte_size = p.stat().st_size if p.is_file() else 0
        except OSError:
            byte_size = 0
        elapsed = round(time.perf_counter() - s_stats["t0"], 1)

        # Build fingerprint payload here (with the shard still open) so the
        # parent process can write to main.db without re-opening the shard.
        fp_rows = shard.conn.execute(
            "SELECT gene_id, source_id, promoter, key_values, is_fragment "
            "FROM genes"
        ).fetchall()
        now = time.time()
        fp_payload = []
        for r in fp_rows:
            promoter_blob = r["promoter"]
            domains_json = None
            entities_json = None
            if promoter_blob:
                try:
                    pm = json.loads(promoter_blob)
                    domains_json = json.dumps(pm.get("domains") or [])
                    entities_json = json.dumps(pm.get("entities") or [])
                except Exception:
                    pass
            fp_payload.append((
                r["gene_id"], label, r["source_id"],
                domains_json, entities_json, r["key_values"],
                0 if r["is_fragment"] else 1, None, now,
            ))

        return {
            "label": label,
            "root": root,
            "shard_db_path": str(p),
            "gene_count": gene_count,
            "byte_size": byte_size,
            "elapsed_s": elapsed,
            "files": s_stats["files"],
            "genes": s_stats["genes"],
            "skipped": s_stats["skipped"],
            "errors": s_stats["errors"],
            "missing_roots": s_stats["missing_roots"],
            "fingerprint_payload": fp_payload,
        }
    finally:
        shard.close()


def _shard_worker_entry(task: dict) -> dict:
    """``mp.Pool`` entry point -- accepts a task dict, returns shard result."""
    return _build_one_shard(
        label=task["label"],
        root=task["root"],
        shard_db_path=task["shard_db_path"],
        skip_dirs=task["skip_dirs"],
        extra_filename_filters=task["extra_filename_filters"],
        use_batched_splade=True,
        batch_size=task.get("batch_size", 64),
    )


def build_profile_sharded(
    name: str,
    profile_out_dir: str,
    shard_category: str = "reference",
    shard_workers: int = 1,
    batch_size: int = 64,
) -> dict:
    """Build the profile as a sharded layout under ``profile_out_dir``.

    When ``shard_workers > 1`` the per-shard builds run in an ``mp.Pool``;
    main.db writes happen in the parent process after each shard returns,
    serialized through SQLite's ``busy_timeout``. ``shard_workers == 1``
    (default) is the serial path.
    """
    import multiprocessing as mp

    profile = PROFILES[name]
    os.makedirs(profile_out_dir, exist_ok=True)

    main_path = main_db_path(profile_out_dir)
    if main_path.exists():
        log.info("removing existing %s", main_path)
        main_path.unlink()
        for sidecar in (str(main_path) + "-wal", str(main_path) + "-shm"):
            if os.path.exists(sidecar):
                os.remove(sidecar)
    main_conn = open_main_db(str(main_path))
    init_main_db(main_conn)
    try:
        main_conn.execute("PRAGMA busy_timeout = 30000")
    except Exception:
        log.debug("busy_timeout pragma failed", exc_info=True)
    log.info(
        "sharded main.db at %s (shard_workers=%d)",
        main_path, shard_workers,
    )

    skip_dirs = SKIP_DIRS_COMMON | profile["extra_skip_dirs"]
    extra_filename_filters = profile["extra_filename_filters"]

    totals = {
        "profile": name,
        "label": profile["label"],
        "active_roots": profile["active_roots"],
        "roots": profile["roots"],
        "out_dir": profile_out_dir,
        "main_db": str(main_path),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "files": 0,
        "genes": 0,
        "skipped": 0,
        "errors": 0,
        "missing_roots": [],
        "shards": [],
        "shard_workers": shard_workers,
        "t0": time.perf_counter(),
    }

    # Build the task list (filter out missing roots up front).
    tasks: list[dict] = []
    for root in profile["roots"]:
        if not os.path.exists(root):
            log.warning("root %s does not exist, skipping", root)
            totals["missing_roots"].append(root)
            continue
        label = _slug_for_root(root)
        shard_db = corpus_shard_db(root, label, profile_out_dir)
        tasks.append({
            "label": label,
            "root": root,
            "shard_db_path": str(shard_db),
            "skip_dirs": skip_dirs,
            "extra_filename_filters": extra_filename_filters,
            "batch_size": batch_size,
        })

    # Per-shard execution -- serial or pool.
    def _commit_shard_result(res: dict) -> None:
        register_shard(
            main_conn,
            shard_name=res["label"],
            category=shard_category,
            path=res["shard_db_path"],
            gene_count=res["gene_count"],
            byte_size=res["byte_size"],
        )
        if res["fingerprint_payload"]:
            main_conn.executemany(
                "INSERT OR REPLACE INTO fingerprint_index "
                "(gene_id, shard_name, source_id, domains, entities, "
                "key_values, is_parent, sequence_idx, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                res["fingerprint_payload"],
            )
        main_conn.commit()
        log.info(
            "  %s: %d genes, %d fingerprint rows, %.1f MB (%.1fs)",
            res["label"], res["gene_count"],
            len(res["fingerprint_payload"]),
            res["byte_size"] / 1_048_576, res["elapsed_s"],
        )
        totals["shards"].append({
            "name": res["label"],
            "root": res["root"],
            "path": res["shard_db_path"],
            "genes": res["gene_count"],
            "fingerprint_rows": len(res["fingerprint_payload"]),
            "bytes": res["byte_size"],
            "elapsed_s": res["elapsed_s"],
        })
        for k in ("files", "genes", "skipped", "errors"):
            totals[k] += res[k]
        totals["missing_roots"].extend(res["missing_roots"])

    if shard_workers <= 1:
        for task in tasks:
            log.info(
                "=== Shard %s @ %s -> %s ===",
                task["label"], task["root"], task["shard_db_path"],
            )
            _commit_shard_result(_shard_worker_entry(task))
    else:
        log.info(
            "dispatching %d shards across %d workers",
            len(tasks), shard_workers,
        )
        with mp.Pool(shard_workers) as pool:
            for res in pool.imap_unordered(_shard_worker_entry, tasks):
                _commit_shard_result(res)

    elapsed = time.perf_counter() - totals["t0"]
    totals["elapsed_s"] = round(elapsed, 1)
    totals["finished_at"] = datetime.now(timezone.utc).isoformat()

    # Checkpoint + close before sizing so WAL contents land in the main file.
    try:
        main_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        log.debug("wal_checkpoint on main.db failed", exc_info=True)
    main_conn.close()

    try:
        totals["main_db_bytes"] = os.path.getsize(main_path)
    except OSError:
        totals["main_db_bytes"] = -1

    total_shard_bytes = sum(s["bytes"] for s in totals["shards"])
    totals["total_bytes"] = total_shard_bytes + max(totals["main_db_bytes"], 0)
    totals["total_genes"] = sum(s["genes"] for s in totals["shards"])
    totals["shard_count"] = len(totals["shards"])

    log.info("=" * 60)
    log.info("DONE %s-sharded in %.1fs", name, elapsed)
    log.info(
        "  shards=%d genes=%d bytes=%d (main_db=%d)",
        totals["shard_count"], totals["total_genes"],
        totals["total_bytes"], totals["main_db_bytes"],
    )
    if totals["missing_roots"]:
        log.warning("  missing roots: %s", totals["missing_roots"])

    totals.pop("t0", None)
    return totals


# ── Manifest IO ───────────────────────────────────────────────────────────


def update_manifest(out_dir: str, profile_stats: dict, mode: str) -> None:
    """Merge ``profile_stats`` into ``<out_dir>/manifest.json`` under ``mode``."""
    manifest_path = os.path.join(out_dir, "manifest.json")

    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    else:
        manifest = {
            "bench": "fixture_matrix",
            "spec": "docs/benchmarks/GENOME_FIXTURE_MATRIX.md",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "targets": {},
        }

    # Preserve legacy flat layout (mode="blob" historically) while letting
    # sharded entries live under <profile>-sharded keys for clarity.
    key = profile_stats["profile"] if mode == "blob" else f"{profile_stats['profile']}-sharded"
    manifest["targets"][key] = {"mode": mode, **profile_stats}
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    log.info("manifest updated: %s (key=%s)", manifest_path, key)


# ── CLI ───────────────────────────────────────────────────────────────────


def parse_profile_arg(value: str) -> list[str]:
    if value == "all":
        return ["small", "medium", "large", "xl"]
    parts = [p.strip() for p in value.split(",") if p.strip()]
    unknown = [p for p in parts if p not in PROFILES]
    if unknown:
        raise SystemExit(f"unknown profile(s): {unknown}; choose from {list(PROFILES)} or 'all'")
    return parts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", default="all",
        help="Comma-separated subset of {small,medium,large,xl} or 'all'",
    )
    parser.add_argument(
        "--mode", choices=["blob", "sharded"], default="blob",
        help="Build target: monolithic blob (default) or sharded layout",
    )
    parser.add_argument(
        "--out-dir",
        default=r"F:\Projects\helix-context\genomes\bench\matrix",
        help="Base output dir. Blob writes <out-dir>/<profile>.db; "
             "sharded writes <out-dir>-sharded/<profile>/.",
    )
    parser.add_argument(
        "--shard-category", default="reference",
        choices=["participant", "agent", "reference", "org", "cold"],
        help="Shard category recorded in main.db (sharded mode only)",
    )
    parser.add_argument(
        "--parallel", action="store_true",
        help="Use mp.Pool + batched-SPLADE ingest (blob mode only). "
             "Default: sequential.",
    )
    parser.add_argument(
        "--workers", type=int, default=0,
        help="Worker count for --parallel (0 = auto via "
             "helix_context.parallel.auto_workers).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=64,
        help="SPLADE batch size in the writer (default: 64).",
    )
    parser.add_argument(
        "--chunksize", type=int, default=4,
        help="mp.Pool chunksize for --parallel (default: 4).",
    )
    parser.add_argument(
        "--shard-workers", type=int, default=0,
        help="Number of parallel shard-builders (sharded mode only). "
             "0 = auto via helix_context.parallel.auto_shard_workers; "
             "1 = serial.",
    )
    args = parser.parse_args()

    profiles = parse_profile_arg(args.profile)

    if args.mode == "blob":
        out_dir = args.out_dir
        os.makedirs(out_dir, exist_ok=True)
        log.info("BUILD START mode=blob profiles=%s out_dir=%s", profiles, out_dir)

        results = {}
        for name in profiles:
            db_path = os.path.join(out_dir, f"{name}.db")
            log.info(
                "### Profile: %s (blob, %s) ###",
                name, "parallel" if args.parallel else "sequential",
            )
            stats = build_profile(
                name, db_path,
                parallel=args.parallel,
                n_workers=args.workers,
                batch_size=args.batch_size,
                chunksize=args.chunksize,
            )
            update_manifest(out_dir, stats, mode="blob")
            results[name] = stats

        log.info("=" * 60)
        log.info("SUMMARY (blob)")
        for name, s in results.items():
            log.info(
                "  %-7s genes=%d bytes=%d elapsed=%.1fs (files=%d errors=%d missing=%d)",
                name, s["total_genes"], s["bytes"], s["elapsed_s"],
                s["files"], s["errors"], len(s["missing_roots"]),
            )
        return 0

    # mode == "sharded"
    base = args.out_dir.rstrip("/\\")
    out_dir = f"{base}-sharded" if not base.endswith("-sharded") else base
    os.makedirs(out_dir, exist_ok=True)
    log.info("BUILD START mode=sharded profiles=%s out_dir=%s", profiles, out_dir)

    if args.shard_workers <= 0:
        from helix_context.parallel import auto_shard_workers
        shard_workers = auto_shard_workers()
    else:
        shard_workers = args.shard_workers

    results = {}
    for name in profiles:
        profile_dir = os.path.join(out_dir, name)
        log.info(
            "### Profile: %s (sharded, %d workers) ###",
            name, shard_workers,
        )
        stats = build_profile_sharded(
            name=name,
            profile_out_dir=profile_dir,
            shard_category=args.shard_category,
            shard_workers=shard_workers,
            batch_size=args.batch_size,
        )
        update_manifest(out_dir, stats, mode="sharded")
        results[name] = stats

    log.info("=" * 60)
    log.info("SUMMARY (sharded)")
    for name, s in results.items():
        log.info(
            "  %-7s shards=%d genes=%d total_bytes=%d elapsed=%.1fs",
            name, s["shard_count"], s["total_genes"], s["total_bytes"],
            s["elapsed_s"],
        )

    return 0


if __name__ == "__main__":
    import multiprocessing as mp
    mp.freeze_support()  # required on Windows for --parallel / --shard-workers
    sys.exit(main())

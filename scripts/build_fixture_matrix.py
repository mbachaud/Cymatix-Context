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
    --shard-file-workers N  CPU-only chunk/tag workers inside each shard
                            process. 0 = auto from CPU budget.
    --no-shard-sort         Disable largest-first shard ordering. Default
                            is enabled: pre-scan eligible bytes per shard
                            so the long pole dispatches first (issue #97).
    --batch-size N          SPLADE batch size in the writer (default 64).

Examples:
    python scripts/build_fixture_matrix.py --profile medium --parallel
    python scripts/build_fixture_matrix.py --profile xl --parallel --workers 6
    python scripts/build_fixture_matrix.py --profile xl --mode sharded --shard-workers 2 --shard-file-workers 3

The script does not talk to the running Helix server -- it builds fresh
SQLite files directly. Use ``POST /admin/swap-db`` with ``mode="blob"``
or ``mode="sharded"`` (or the ``helix_swap_db`` MCP tool) to mount one
of the resulting files into a running server without restarting.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import logging
import multiprocessing as mp
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Also put this ``scripts/`` dir on the path so the sibling-module import of
# ``backfill_bgem3_v2`` resolves whether this file is run directly or
# imported as a module (e.g. by the test suite). See Tier-0 PR-2.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helix_context.tagger import CpuTagger
from helix_context.genome import Genome
from helix_context.codons import CodonChunker
from helix_context.sharding import corpus_shard_db, main_db_path
from helix_context.shard_schema import (
    init_main_db,
    open_main_db,
    register_shard,
)

# Tier-0 PR-2 (2026-05-16): reuse the operator backfill script's shared
# encode-and-pack loop so the fixture builder's post-build dense pass and
# ``scripts/backfill_bgem3_v2.py`` share one implementation and cannot drift.
from backfill_bgem3_v2 import backfill_dense_db


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


def _is_sqlite_sidecar(path: str) -> bool:
    """Picklable filter for SQLite sidecars in process-pool shard tasks."""
    return any(path.lower().endswith(s) for s in SQLITE_SIDECAR_SUFFIXES)


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


def _estimate_eligible_bytes(
    root: str,
    skip_dirs: set[str],
    extra_filename_filters: list,
) -> tuple[int, int]:
    """Walk ``root`` and return ``(eligible_files, eligible_bytes)``.

    Counts only files that would pass the same ingestion filters as
    :func:`_iter_ingestable_files`: extension in ``INGEST_EXTS``, not
    filtered by ``extra_filename_filters``, and within
    ``MIN_FILE_SIZE..MAX_FILE_SIZE``. Used by sharded builds to order
    the shard queue largest-first so the long pole gets the longest
    head start on the worker pool (issue #97, option A.1).

    Filesystem errors on individual files are swallowed — the estimate
    is a sizing hint, not a contract; the actual ingest walks the tree
    again under the same filters and will report the truth.
    """
    if not os.path.exists(root):
        return 0, 0
    eligible_files = 0
    eligible_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in INGEST_EXTS:
                continue
            fpath = os.path.join(dirpath, fname)
            if any(f(fpath) for f in extra_filename_filters):
                continue
            try:
                size = os.path.getsize(fpath)
            except OSError:
                continue
            if size < MIN_FILE_SIZE or size > MAX_FILE_SIZE:
                continue
            eligible_files += 1
            eligible_bytes += size
    return eligible_files, eligible_bytes


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


def _iter_chunked_file_gene_dicts(
    files: list[tuple[str, str]],
    file_workers: int,
    chunksize: int = 4,
) -> Iterable[list[dict]]:
    """Yield per-file gene dict lists, optionally using shard-local CPU workers."""
    file_workers = max(1, int(file_workers or 1))
    if file_workers <= 1:
        _init_worker()
        for f in files:
            yield _chunk_and_tag_file(f)
        return

    log.info(
        "shard file ingest: %d files, %d file_workers, chunksize=%d",
        len(files), file_workers, chunksize,
    )
    with mp.Pool(file_workers, initializer=_init_worker) as pool:
        yield from pool.imap_unordered(
            _chunk_and_tag_file, files, chunksize=chunksize,
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
        "extra_filename_filters": [_is_sqlite_sidecar],
    },
    "large": {
        "label": "Full projects corpus",
        "active_roots": 1,
        "roots": [r"F:\Projects"],
        "extra_skip_dirs": set(),
        "extra_filename_filters": [_is_sqlite_sidecar],
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
        "extra_filename_filters": [_is_sqlite_sidecar],
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


def _backfill_dense(db_path: str) -> dict:
    """Post-build pass: populate ``genes.embedding_dense_v2`` on ``db_path``.

    Tier-0 PR-2 (2026-05-16). The fixture builder's per-gene write path
    (``Genome.upsert_doc`` via the batched-SPLADE writer) is deliberately
    kept lean — it does not encode dense vectors inline. Instead, once a
    profile or shard ``.db`` is fully built and closed, this runs an
    explicit BGE-M3 dense backfill over it so the bench fixtures exercise
    the real (dense-populated) retrieval pipeline rather than a
    dense-dark one.

    Delegates to :func:`backfill_bgem3_v2.backfill_dense_db` — the shared
    encode-and-pack loop also used by the standalone operator backfill
    script — so the two paths cannot drift. ``dim`` defaults to
    ``retrieval.dense_embedding_dim`` from ``helix.toml``.

    Call sites:
      * blob mode — at the end of :func:`build_profile`, after
        ``genome.close()``, on ``<profile>.db``.
      * sharded mode — in :func:`_build_one_shard`, after the shard's
        ``Genome`` is closed, on each per-shard ``.db``. The cross-shard
        ``main.genome.db`` routing DB holds no ``genes`` rows and is not
        backfilled.

    Returns the coverage report dict from :func:`backfill_dense_db`
    (includes ``dense_coverage`` in ``[0.0, 1.0]``). On any failure the
    error is logged and a degraded report with ``dense_coverage = 0.0``
    and an ``error`` key is returned, so a dense-encode failure surfaces
    in the manifest rather than silently producing a dense-dark fixture.
    """
    log.info("dense backfill: %s", db_path)
    try:
        report = backfill_dense_db(db_path, log_fn=lambda msg: log.info("%s", msg))
    except Exception as exc:  # noqa: BLE001 — model load / encode failure
        log.error("dense backfill FAILED for %s: %s", db_path, exc)
        return {
            "db_path": db_path,
            "dense_coverage": 0.0,
            "rows_processed": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    log.info(
        "dense backfill done: %s coverage=%.1f%% (%d/%d genes, %d processed)",
        db_path,
        100.0 * report["dense_coverage"],
        report["populated_after"],
        report["total"],
        report["rows_processed"],
    )
    return report


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

    # Tier-0 PR-2: post-build dense pass. The builder's per-gene write path
    # is lean (no inline BGE-M3 encode); populate ``embedding_dense_v2`` here
    # now that the ``.db`` is fully written and closed.
    dense_report = _backfill_dense(db_path)
    stats["dense_coverage"] = dense_report["dense_coverage"]
    stats["dense_genes_populated"] = dense_report.get("populated_after", 0)
    if dense_report.get("error"):
        stats["dense_error"] = dense_report["error"]

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


def _try_salvage_complete_shard(
    p: Path, label: str, root: str,
) -> Optional[dict]:
    """Return a result dict for an already-complete shard on disk, or None.

    Used by ``_build_one_shard`` to skip a rebuild when a prior run already
    produced a fully-ingested + fully-dense-backfilled shard ``.db``. A shard
    is considered complete iff (a) the file exists with no live WAL sidecar,
    (b) the ``genes`` table has rows, AND (c) every row has a populated
    ``embedding_dense_v2`` column (dense_coverage == 100%). Anything else
    falls back to a full rebuild.

    Returns the same result-dict shape that the build path constructs, so
    the parent's ``_commit_shard_result`` can register the shard idempotently
    (main.db uses ``INSERT OR REPLACE`` on both fingerprint_index and
    source_index — duplicate registrations are safe).
    """
    import sqlite3 as _sqlite3
    # WAL sidecar with non-zero size means an uncommitted transaction; treat
    # as incomplete and rebuild to avoid resurrecting partial state.
    wal_path = Path(str(p) + "-wal")
    if wal_path.exists() and wal_path.stat().st_size > 0:
        return None
    try:
        conn = _sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.row_factory = _sqlite3.Row
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "genes" not in tables:
                return None
            cur = conn.execute("SELECT COUNT(*) FROM genes")
            gene_count = cur.fetchone()[0]
            if gene_count == 0:
                return None
            cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(genes)"
                ).fetchall()
            }
            if "embedding_dense_v2" not in cols:
                return None
            populated = conn.execute(
                "SELECT COUNT(*) FROM genes "
                "WHERE embedding_dense_v2 IS NOT NULL"
            ).fetchone()[0]
            if populated != gene_count:
                return None
            fp_rows = conn.execute(
                "SELECT gene_id, source_id, repo_root, source_kind, "
                "observed_at, mtime, content_hash, volatility_class, "
                "authority_class, support_span, last_verified_at, "
                "promoter, key_values, is_fragment "
                "FROM genes"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None

    now = time.time()
    fp_payload = []
    si_payload = []
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
        observed_at = r["observed_at"] if r["observed_at"] is not None else now
        last_verified_at = (
            r["last_verified_at"] if r["last_verified_at"] is not None else now
        )
        si_payload.append((
            r["gene_id"], label, r["source_id"], r["repo_root"],
            r["source_kind"], observed_at, r["mtime"], r["content_hash"],
            r["volatility_class"] or "medium",
            r["authority_class"] or "primary",
            r["support_span"], last_verified_at,
            None, now,
        ))
    try:
        byte_size = p.stat().st_size if p.is_file() else 0
    except OSError:
        byte_size = 0
    return {
        "label": label,
        "root": root,
        "shard_db_path": str(p),
        "gene_count": gene_count,
        "byte_size": byte_size,
        "elapsed_s": 0.0,
        "files": 0,
        "genes": gene_count,
        "skipped": 0,
        "errors": 0,
        "missing_roots": [],
        "fingerprint_payload": fp_payload,
        "source_index_payload": si_payload,
        "dense_coverage": 100.0,
        "dense_genes_populated": gene_count,
        "salvaged": True,
    }


def _build_one_shard(
    label: str,
    root: str,
    shard_db_path: str,
    skip_dirs: set[str],
    extra_filename_filters: list,
    use_batched_splade: bool = True,
    batch_size: int = 64,
    file_workers: int = 1,
    file_chunksize: int = 4,
) -> dict:
    """Build a single shard ``.db`` for ``root``. Returns the shard's
    fingerprint payload + stats -- caller writes rows into main.db.

    Runs one SPLADE-owning shard process; chunk+tag may fan out to a
    shard-local CPU-only file pool before batched SPLADE upsert. Used by
    both the serial sharded build (called from the parent process) and the
    parallel shard executor (called inside subprocesses via
    :func:`_shard_worker_entry`).
    """
    p = Path(shard_db_path)
    # Resume support: if a prior run left a complete shard on disk
    # (genes ingested + 100% dense coverage), skip rebuild. Returning early
    # here lets ``_commit_shard_result`` re-register via INSERT OR REPLACE.
    if p.exists():
        salvaged = _try_salvage_complete_shard(p, label, root)
        if salvaged is not None:
            return salvaged
        # Incomplete: nuke and rebuild.
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
            gen = _iter_chunked_file_gene_dicts(
                files, file_workers=file_workers, chunksize=file_chunksize,
            )
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

        # Build fingerprint + source_index payloads here (with the shard
        # still open) so the parent process can write to main.db without
        # re-opening the shard. Mirrors the column set copied by
        # ``scripts/ingest_all.py:_copy_indexes_from_shard`` so bench
        # fixtures exercise the same packet-freshness path that real
        # ingest produces (PR #113 + follow-up).
        fp_rows = shard.conn.execute(
            "SELECT gene_id, source_id, repo_root, source_kind, observed_at, "
            "mtime, content_hash, volatility_class, authority_class, "
            "support_span, last_verified_at, promoter, key_values, is_fragment "
            "FROM genes"
        ).fetchall()
        now = time.time()
        fp_payload = []
        si_payload = []
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
            # source_index row -- defaults match the table DDL so build-time
            # rows look like a real ingest's "never observed/verified yet"
            # state. ``observed_at`` / ``last_verified_at`` fall back to
            # build time so freshness logic has a non-NULL timestamp to
            # reason about; volatility_class / authority_class default
            # to ``medium`` / ``primary`` per the column defaults.
            observed_at = r["observed_at"] if r["observed_at"] is not None else now
            last_verified_at = (
                r["last_verified_at"]
                if r["last_verified_at"] is not None
                else now
            )
            si_payload.append((
                r["gene_id"], label, r["source_id"], r["repo_root"],
                r["source_kind"], observed_at, r["mtime"], r["content_hash"],
                r["volatility_class"] or "medium",
                r["authority_class"] or "primary",
                r["support_span"], last_verified_at,
                None,  # invalidated_at
                now,   # updated_at
            ))

        result = {
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
            "source_index_payload": si_payload,
        }
    finally:
        shard.close()

    # Tier-0 PR-2: post-build dense pass on this per-shard ``.db`` — run
    # only after the shard's ``Genome`` has been closed above. The
    # cross-shard ``main.genome.db`` routing DB is NOT backfilled here: it
    # carries no ``genes`` rows (only fingerprint_index / source_index), so
    # per-shard dense recall reads each shard's own ``embedding_dense_v2``.
    dense_report = _backfill_dense(str(p))
    result["dense_coverage"] = dense_report["dense_coverage"]
    result["dense_genes_populated"] = dense_report.get("populated_after", 0)
    if dense_report.get("error"):
        result["dense_error"] = dense_report["error"]
    return result


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
        file_workers=task.get("shard_file_workers", 1),
        file_chunksize=task.get("shard_file_chunksize", 4),
    )


def build_profile_sharded(
    name: str,
    profile_out_dir: str,
    shard_category: str = "reference",
    shard_workers: int = 1,
    shard_file_workers: int = 0,
    batch_size: int = 64,
    sort_largest_first: bool = True,
) -> dict:
    """Build the profile as a sharded layout under ``profile_out_dir``.

    When ``shard_workers > 1`` the per-shard builds run in a process
    executor; main.db writes happen in the parent process after each shard
    returns, serialized through SQLite's ``busy_timeout``. Each shard may
    also run ``shard_file_workers`` CPU-only workers for chunk+tag prep;
    ``0`` auto-sizes from the CPU budget.

    When ``sort_largest_first`` is True (default), shards are pre-scanned
    for eligible-byte count and submitted to the worker pool from
    largest to smallest. On uneven workloads (#97) this gives the long
    pole the longest head start: e.g., XL's ``F:/Projects`` (~60% of
    eligible bytes) dispatches first instead of waiting for 11 small
    shards to drain. The pre-scan is a quick metadata walk, dwarfed by
    the actual ingest cost.
    """
    profile = PROFILES[name]
    if shard_file_workers <= 0:
        from helix_context.parallel import auto_shard_file_workers
        shard_file_workers = auto_shard_file_workers(shard_workers)
    else:
        shard_file_workers = max(1, int(shard_file_workers))
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
        "sharded main.db at %s (shard_workers=%d, shard_file_workers=%d)",
        main_path, shard_workers, shard_file_workers,
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
        "shard_file_workers": shard_file_workers,
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
            "shard_file_workers": shard_file_workers,
            "shard_file_chunksize": 4,
        })

    # Pre-ingest sizing — order shards largest-first so the long pole
    # gets the longest head start on the worker pool (issue #97 A.1).
    # When ``shard_workers <= 1`` the order doesn't affect wall-clock,
    # but we still record the estimate in the manifest for diagnostics.
    if sort_largest_first and tasks:
        sizing_t0 = time.perf_counter()
        for task in tasks:
            files, bytes_ = _estimate_eligible_bytes(
                task["root"], skip_dirs, extra_filename_filters,
            )
            task["eligible_files"] = files
            task["eligible_bytes"] = bytes_
        tasks.sort(key=lambda t: t["eligible_bytes"], reverse=True)
        sizing_elapsed = time.perf_counter() - sizing_t0
        log.info(
            "pre-ingest sizing complete in %.1fs — shard order:", sizing_elapsed,
        )
        for task in tasks:
            log.info(
                "  %s: %d eligible files, %.1f MB",
                task["label"], task["eligible_files"],
                task["eligible_bytes"] / 1_048_576,
            )
        totals["sizing_elapsed_s"] = round(sizing_elapsed, 1)
        totals["sort_largest_first"] = True
    else:
        totals["sort_largest_first"] = False

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
        si_payload = res.get("source_index_payload") or []
        if si_payload:
            # Mirror ``ingest_all._copy_indexes_from_shard`` so bench fixtures
            # exercise the same packet-freshness path as real ingest. Without
            # this the table is empty and ``context_packet._lookup_source_row``
            # returns None for every gene_id (PR #113).
            main_conn.executemany(
                "INSERT OR REPLACE INTO source_index "
                "(gene_id, shard_name, source_id, repo_root, source_kind, "
                "observed_at, mtime, content_hash, volatility_class, "
                "authority_class, support_span, last_verified_at, "
                "invalidated_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                si_payload,
            )
        main_conn.commit()
        log.info(
            "  %s: %d genes, %d fingerprint rows, %d source rows, %.1f MB (%.1fs)",
            res["label"], res["gene_count"],
            len(res["fingerprint_payload"]),
            len(si_payload),
            res["byte_size"] / 1_048_576, res["elapsed_s"],
        )
        shard_entry = {
            "name": res["label"],
            "root": res["root"],
            "path": res["shard_db_path"],
            "genes": res["gene_count"],
            "fingerprint_rows": len(res["fingerprint_payload"]),
            "source_index_rows": len(si_payload),
            "bytes": res["byte_size"],
            "elapsed_s": res["elapsed_s"],
            # Tier-0 PR-2: per-shard dense coverage from the post-build
            # backfill that ran inside ``_build_one_shard``.
            "dense_coverage": res.get("dense_coverage", 0.0),
            "dense_genes_populated": res.get("dense_genes_populated", 0),
        }
        if res.get("dense_error"):
            shard_entry["dense_error"] = res["dense_error"]
        totals["shards"].append(shard_entry)
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
            "dispatching %d shards across %d workers (%d file_workers each)",
            len(tasks), shard_workers, shard_file_workers,
        )
        with ProcessPoolExecutor(max_workers=shard_workers) as pool:
            futures = [pool.submit(_shard_worker_entry, task) for task in tasks]
            for fut in as_completed(futures):
                _commit_shard_result(fut.result())

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

    # Tier-0 PR-2: profile-level dense coverage = populated genes across all
    # per-shard ``.db`` files / total genes across all shards. The cross-shard
    # ``main.genome.db`` carries no ``genes`` rows and is excluded by
    # construction (it is never passed to ``_backfill_dense``).
    dense_populated = sum(
        s.get("dense_genes_populated", 0) for s in totals["shards"]
    )
    totals["dense_genes_populated"] = dense_populated
    totals["dense_coverage"] = (
        dense_populated / totals["total_genes"]
        if totals["total_genes"] else 0.0
    )

    log.info("=" * 60)
    log.info("DONE %s-sharded in %.1fs", name, elapsed)
    log.info(
        "  shards=%d genes=%d bytes=%d (main_db=%d) dense_coverage=%.1f%%",
        totals["shard_count"], totals["total_genes"],
        totals["total_bytes"], totals["main_db_bytes"],
        100.0 * totals["dense_coverage"],
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
    parser.add_argument(
        "--shard-file-workers", type=int, default=0,
        help="CPU-only file workers inside each shard-builder "
             "(sharded mode only). 0 = auto via "
             "helix_context.parallel.auto_shard_file_workers; 1 = serial.",
    )
    parser.add_argument(
        "--no-shard-sort", action="store_true",
        help="Disable largest-first shard ordering (sharded mode only). "
             "Default: shards are pre-scanned for eligible bytes and "
             "submitted to the worker pool from largest to smallest so "
             "the long pole gets the longest head start. Disable for "
             "deterministic ordering (e.g., parity benches against "
             "original declared order).",
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

    from helix_context.parallel import auto_shard_file_workers
    if args.shard_workers <= 0:
        from helix_context.parallel import auto_shard_workers
        shard_workers = auto_shard_workers()
    else:
        shard_workers = args.shard_workers
    if args.shard_file_workers <= 0:
        shard_file_workers = auto_shard_file_workers(shard_workers)
    else:
        shard_file_workers = max(1, args.shard_file_workers)

    results = {}
    for name in profiles:
        profile_dir = os.path.join(out_dir, name)
        log.info(
            "### Profile: %s (sharded, %d shard workers x %d file workers) ###",
            name, shard_workers, shard_file_workers,
        )
        stats = build_profile_sharded(
            name=name,
            profile_out_dir=profile_dir,
            shard_category=args.shard_category,
            shard_workers=shard_workers,
            shard_file_workers=shard_file_workers,
            batch_size=args.batch_size,
            sort_largest_first=not args.no_shard_sort,
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

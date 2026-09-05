"""Measure what the SQLite writer page cache is worth during a bulk ingest.

Context
-------
``hardware.py``'s ``auto`` memory profile caps the writer page cache at
``cache_max_mib = 64`` on any host, however much RAM it has. During the
2026-08-24 ERB build that proved pathological: with ~35 secondary-index rows
written per gene into B-trees totalling ~6 GB, the working set was ~100x the
cache. Measured on the live build at 249k genes:

    read 2.57 MB and write 2.12 MB **per gene**, average write op 2.6 KB
    (sub-page), ~6,100 write ops/s, 408 GB read / 489 GB written for a bed
    that had only grown to 6.4 GB.

That is read-modify-write thrashing, not useful work. This script tests the
fix instead of asserting it: same bed, same files, same code path, one knob.

Method
------
Each arm gets its own **copy** of a real (partial) bed, so both start from an
identical B-tree shape and size -- cache behaviour depends on how big the
indexes already are, so a fresh empty bed would not reproduce the regime.
Each arm ingests the same N files that the bed has not seen, in a fresh
subprocess (the PRAGMAs are read when the writer connection opens), and
reports its own process I/O counters.

Usage
-----
    python scripts/sqlite_cache_ab.py --bed F:/tmp/enterprise_rag_500k.db \\
        --root F:/tmp/enterprise_rag_500k/sources --files 400 \\
        --out benchmarks/dogfood/receipts/sqlite_cache_ab.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SCRIPTS = str(REPO_ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

log = logging.getLogger("sqlite_cache_ab")

# Tables whose explicit secondary indexes dominate the ingest write path.
# Rows written per gene, measured at 139k genes on the ERB build:
#   promoter_index 22.68 (3 indexes), entity_graph 12.72 (2), gene_relations
#   9.36 (1). PRIMARY KEY / UNIQUE auto-indexes cannot be dropped without
#   rebuilding the table, so they stay in every arm.
_DEFER_TABLES = (
    "promoter_index", "entity_graph", "gene_relations", "genes",
    "filename_index", "harmonic_links", "symbol_defs", "path_key_index",
)

ARMS: Dict[str, Dict[str, Any]] = {
    # cache/mmap: None leaves the hardware.py profile alone.
    "profile_default": {"cache": None, "mmap": None, "defer_indexes": False},
    "cache_8gb": {"cache": -8 * 1024 * 1024, "mmap": 16 * 1024 ** 3,
                  "defer_indexes": False},
    # Indexes are derived, so dropping them for the load and rebuilding after
    # is content-safe. Reported wall INCLUDES the rebuild.
    "deferred_indexes": {"cache": None, "mmap": None, "defer_indexes": True},
}


# ---------------------------------------------------------------------------
# Child: ingest into one bed, report wall + I/O
# ---------------------------------------------------------------------------

def _drop_deferrable_indexes(conn: sqlite3.Connection) -> List[str]:
    """Drop explicit indexes on the hot write-path tables, return their DDL.

    Only ``sqlite_master`` rows with non-null ``sql`` are real (droppable)
    indexes; PRIMARY KEY / UNIQUE auto-indexes have null ``sql`` and cannot be
    dropped without rebuilding the table, so they are left alone in every arm.
    """
    q = (
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND sql IS NOT NULL AND tbl_name IN (%s)"
        % ",".join("?" * len(_DEFER_TABLES))
    )
    rows = list(conn.execute(q, _DEFER_TABLES))
    ddl = [sql for _n, sql in rows]
    for name, _sql in rows:
        conn.execute(f'DROP INDEX IF EXISTS "{name}"')
    conn.commit()
    return ddl


def _child(bed: str, files_json: str) -> int:
    import build_fixture_matrix as bfm

    cfg = json.loads(Path(files_json).read_text())
    files: List[Tuple[str, str]] = [tuple(x) for x in cfg["files"]]
    defer = bool(cfg.get("defer_indexes"))

    try:
        import psutil
        proc = psutil.Process()
        io0 = proc.io_counters()
    except Exception:
        proc, io0 = None, None

    genome = bfm.Genome(path=bed, synonym_map={}, splade_enabled=False, entity_graph=True)
    stats = {"files": 0, "genes": 0, "skipped": 0, "errors": 0,
             "missing_roots": [], "mode": "ab", "t0": time.perf_counter()}

    # Report the PRAGMAs the writer connection actually resolved.
    pragmas = {}
    try:
        for p in ("cache_size", "mmap_size", "page_size", "synchronous"):
            pragmas[p] = genome.conn.execute(f"PRAGMA {p}").fetchone()[0]
    except Exception:
        pass

    before = genome.conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]

    dropped: List[str] = []
    drop_s = 0.0
    if defer:
        t = time.perf_counter()
        dropped = _drop_deferrable_indexes(genome.conn)
        drop_s = time.perf_counter() - t

    t0 = time.perf_counter()
    bfm._init_worker()
    bfm._drain_with_batched_splade(
        (bfm._chunk_and_tag_file(f) for f in files), genome, stats,
    )
    ingest_s = time.perf_counter() - t0

    rebuild_s = 0.0
    if defer and dropped:
        t = time.perf_counter()
        for sql in dropped:
            genome.conn.execute(sql)
        genome.conn.commit()
        rebuild_s = time.perf_counter() - t

    # Wall must include the rebuild or the comparison is dishonest.
    wall = drop_s + ingest_s + rebuild_s
    after = genome.conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
    try:
        genome.close()
    except Exception:
        pass

    out: Dict[str, Any] = {
        "wall_s": round(wall, 2),
        "ingest_s": round(ingest_s, 2),
        "index_drop_s": round(drop_s, 2),
        "index_rebuild_s": round(rebuild_s, 2),
        "indexes_deferred": len(dropped),
        "genes_added": after - before,
        "files": len(files),
        "pragmas": pragmas,
        "errors": stats.get("errors"),
    }
    if proc is not None and io0 is not None:
        io1 = proc.io_counters()
        out["read_bytes"] = io1.read_bytes - io0.read_bytes
        out["write_bytes"] = io1.write_bytes - io0.write_bytes
        out["read_ops"] = io1.read_count - io0.read_count
        out["write_ops"] = io1.write_count - io0.write_count
    print("__RESULT__" + json.dumps(out))
    return 0


# ---------------------------------------------------------------------------
# Parent
# ---------------------------------------------------------------------------

def _unseen_files(bed: str, root: Path, n: int, exts: set) -> List[Tuple[str, str]]:
    """N files the bed has not ingested, deterministic by sort order."""
    conn = sqlite3.connect(f"file:{Path(bed).as_posix()}?mode=ro", uri=True)
    seen = {r[0] for r in conn.execute("SELECT DISTINCT source_id FROM genes") if r[0]}
    conn.close()
    log.info("bed already holds %d distinct source files", len(seen))

    picked: List[Tuple[str, str]] = []
    for dirpath, _d, filenames in os.walk(root):
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in exts:
                continue
            fp = os.path.join(dirpath, fname)
            if fp in seen:
                continue
            try:
                if not (50 <= os.path.getsize(fp) <= 200 * 1024):
                    continue
            except OSError:
                continue
            picked.append((fp, ext))
            if len(picked) >= n:
                return picked
    return picked


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        return _child(sys.argv[2], sys.argv[3])

    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--bed", required=True, help="Partial bed to copy for each arm.")
    p.add_argument("--root", required=True, help="Corpus root for unseen files.")
    p.add_argument("--files", type=int, default=400, help="Files per arm (default 400).")
    p.add_argument("--work-dir", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--keep", action="store_true", help="Keep the arm copies.")
    args = p.parse_args(argv)

    import build_fixture_matrix as bfm

    bed = Path(args.bed)
    work = Path(args.work_dir) if args.work_dir else bed.parent / "cache_ab"
    work.mkdir(parents=True, exist_ok=True)

    files = _unseen_files(str(bed), Path(args.root), args.files, bfm.INGEST_EXTS)
    if len(files) < args.files:
        log.warning("only %d unseen files found (asked %d)", len(files), args.files)
    log.info("workload: %d files", len(files))

    results: Dict[str, Any] = {}
    for arm, spec in ARMS.items():
        cache, mmap = spec["cache"], spec["mmap"]
        cfg_path = work / f"{arm}.cfg.json"
        cfg_path.write_text(
            json.dumps({"files": files, "defer_indexes": spec["defer_indexes"]}),
            encoding="utf-8",
        )
        files_json = cfg_path
        copy = work / f"{arm}.db"
        log.info("--- arm %s: copying bed (%.2f GB) ---", arm, bed.stat().st_size / 1024 ** 3)
        t0 = time.perf_counter()
        shutil.copy2(bed, copy)
        log.info("    copy took %.1f s", time.perf_counter() - t0)

        env = dict(os.environ)
        env.pop("CYMATIX_SQLITE_CACHE_SIZE", None)
        env.pop("CYMATIX_SQLITE_MMAP_SIZE", None)
        if cache is not None:
            env["CYMATIX_SQLITE_CACHE_SIZE"] = str(cache)
        if mmap is not None:
            env["CYMATIX_SQLITE_MMAP_SIZE"] = str(mmap)
        env["CYMATIX_DISABLE_LEARN"] = "1"

        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child",
             str(copy), str(files_json)],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        line = next(
            (l for l in proc.stdout.splitlines() if l.startswith("__RESULT__")), None
        )
        if line is None:
            log.error("arm %s produced no result\nstderr tail:\n%s",
                      arm, "\n".join(proc.stderr.splitlines()[-15:]))
            return 1
        r = json.loads(line[len("__RESULT__"):])
        g = max(1, r.get("genes_added") or 1)
        r["genes_per_s"] = round(g / r["wall_s"], 2) if r["wall_s"] else None
        if "write_bytes" in r:
            r["write_mb_per_gene"] = round(r["write_bytes"] / 1048576 / g, 3)
            r["read_mb_per_gene"] = round(r["read_bytes"] / 1048576 / g, 3)
            r["avg_write_op_kb"] = round(
                r["write_bytes"] / max(1, r["write_ops"]) / 1024, 2
            )
        results[arm] = r
        log.info(
            "    %s: wall %.1fs (ingest %.1f + rebuild %.1f, %d idx deferred)  "
            "%s genes  %.2f g/s  write %.2f MB/gene",
            arm, r["wall_s"], r.get("ingest_s", 0), r.get("index_rebuild_s", 0),
            r.get("indexes_deferred", 0), r["genes_added"],
            r["genes_per_s"] or 0, r.get("write_mb_per_gene", 0),
        )
        if not args.keep:
            for suf in ("", "-wal", "-shm"):
                q = Path(str(copy) + suf)
                if q.exists():
                    q.unlink()

    base = results.get("profile_default")
    tuned = results.get("cache_8gb")
    deferred = results.get("deferred_indexes")
    if base and deferred and deferred["wall_s"]:
        receipt_defer = {
            "speedup_vs_default": round(base["wall_s"] / deferred["wall_s"], 2),
            "write_mb_per_gene": deferred.get("write_mb_per_gene"),
            "index_rebuild_s": deferred.get("index_rebuild_s"),
            "indexes_deferred": deferred.get("indexes_deferred"),
        }
        if base.get("write_mb_per_gene") and deferred.get("write_mb_per_gene"):
            receipt_defer["write_reduction"] = round(
                base["write_mb_per_gene"] / deferred["write_mb_per_gene"], 2
            )
        log.info("DEFERRED-INDEX ARM: %s", receipt_defer)
    else:
        receipt_defer = None

    receipt = {
        "tool": "sqlite_cache_ab",
        "stamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bed": str(bed),
        "bed_bytes": bed.stat().st_size,
        "bed_genes_at_start": None,
        "workload_files": len(files),
        "arms": results,
        "deferred_index_summary": receipt_defer,
    }
    if base and tuned and base["wall_s"] and tuned["wall_s"]:
        receipt["speedup"] = round(base["wall_s"] / tuned["wall_s"], 2)
        if "write_mb_per_gene" in base and "write_mb_per_gene" in tuned:
            receipt["write_amplification_ratio"] = round(
                base["write_mb_per_gene"] / max(1e-9, tuned["write_mb_per_gene"]), 2
            )
        log.info("SPEEDUP %.2fx   write-amplification cut %.2fx",
                 receipt["speedup"], receipt.get("write_amplification_ratio", 0))

    if args.out:
        o = Path(args.out)
        o.parent.mkdir(parents=True, exist_ok=True)
        o.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
        log.info("receipt -> %s", o)
    else:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Commit-cadence A/B on the ingest write path (2026-08-26).

Question: how much of the measured write amplification (806 write-ops /
2.06 MB per gene, 2026-08-24 receipt ``ingest_write_path_ab.json``) is
commit-per-gene cadence?  ``_drain_with_batched_splade`` calls
``upsert_doc`` with no transaction scope, so every gene is its own
durable commit and every hot B-tree page it touched is rewritten to the
WAL per gene.  The fix under test is ``_CommitBatcher`` — one
``deferred_commits()`` scope per K genes.

Design
------
* Arms (default ``0,500,5000``): commit-per-gene control vs two batch
  sizes.  Each arm runs on its own byte-identical copy of a real bed
  (default: the freshly built 947k-gene v0.9.x blob) so B-tree depth is
  the production regime.
* The treatment gene stream is chunked + tagged ONCE and replayed into
  every arm's drain — arms differ in nothing but commit cadence.
* All arms get the same writer page cache (``--cache-mb``, default
  4096).  The 2026-08-24 "cache is neutral" verdict was measured under
  commit-per-gene, where every commit flushes regardless of cache; under
  long transactions the cache becomes the flush-pressure dial
  (``cache_spill``), so it must be pinned equal across arms here.
* Identity gate: sha256 over (gene_id, content, sorted tags) of the
  newly added rows must be identical across arms — batching may only
  change WHEN pages are written, never WHAT is stored.

Usage
-----
    python scripts/ingest_commit_batch_ab.py \
        --base-db F:/tmp/erb_blob_v09x.db \
        --work-dir F:/tmp/commit_batch_ab \
        --out benchmarks/dogfood/receipts/ingest_commit_batch_ab.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

log = logging.getLogger("commit_batch_ab")

_SKIP_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", ".claude", ".pytest_cache",
    "genomes", ".venv", "venv",
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--base-db", default="F:/tmp/erb_blob_v09x.db",
        help="Bed each arm gets a private copy of (default: the v0.9.x blob).",
    )
    p.add_argument(
        "--work-dir", default="F:/tmp/commit_batch_ab",
        help="Scratch dir for the per-arm bed copies.",
    )
    p.add_argument(
        "--files-root", action="append", default=None,
        help="Treatment-file roots (repeatable). Default: repo docs/, "
        "cymatix_context/, tests/ — real files NOT in the ERB bed.",
    )
    p.add_argument("--max-files", type=int, default=1200)
    p.add_argument(
        "--arms", default="0,500,5000",
        help="Comma-separated commit_batch values; 0 = per-gene control.",
    )
    p.add_argument(
        "--cache-mb", type=int, default=4096,
        help="Writer page cache MiB, pinned equal across arms (0 = profile).",
    )
    p.add_argument("--out", default=None, help="Receipt JSON path.")
    p.add_argument("--keep-beds", action="store_true")
    return p


def _enumerate_files(roots, max_files, min_size, exts):
    files = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            log.warning("treatment root missing: %s", root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in _SKIP_DIR_NAMES and not d.startswith(".")
            )
            for fn in sorted(filenames):
                ext = os.path.splitext(fn)[1].lower()
                if ext not in exts:
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(fp) < min_size:
                        continue
                except OSError:
                    continue
                files.append((fp, ext))
    files.sort()
    return files[:max_files]


def _new_rows_digest(db_path: str, base_max_rowid: int):
    """sha256 over (gene_id, content, sorted tags) of rows added past
    ``base_max_rowid``, in gene_id order. Returns (digest, n_rows)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=120)
    try:
        # ``tags`` is a genes column on some beds and absent on others —
        # probe like ingest_equivalence_gate._content_digest does.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(genes)")}
        has_tags = "tags" in cols
        sel = "gene_id, content" + (", tags" if has_tags else "")
        h = hashlib.sha256()
        n = 0
        for row in conn.execute(
            f"SELECT {sel} FROM genes WHERE rowid > ? ORDER BY gene_id",
            (base_max_rowid,)
        ):
            h.update(str(row[0]).encode("utf-8"))
            h.update(b"\x1f")
            h.update((row[1] or "").encode("utf-8"))
            if has_tags:
                h.update(b"\x1f")
                parts = sorted(str(row[2] or "").replace(",", " ").split())
                h.update(" ".join(parts).encode("utf-8"))
            h.update(b"\x00")
            n += 1
        return h.hexdigest(), n
    finally:
        conn.close()


def _table_count(db_path: str, table: str):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=120)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except Exception:
        return None
    finally:
        conn.close()


def _io_counters(proc):
    try:
        io = proc.io_counters()
        return {
            "read_bytes": io.read_bytes, "write_bytes": io.write_bytes,
            "read_count": io.read_count, "write_count": io.write_count,
        }
    except Exception:
        return None


def _io_delta(a, b):
    if not a or not b:
        return None
    return {k: b[k] - a[k] for k in a}


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _build_parser().parse_args(argv)
    arms = [int(x) for x in args.arms.split(",") if x.strip() != ""]

    # Environment pins — mirror the v0.9.x blob build exactly.
    os.environ["CYMATIX_BFM_SPLADE"] = "0"
    os.environ["CYMATIX_BFM_DENSE_BACKFILL"] = "0"
    os.environ.setdefault("CYMATIX_BFM_SEED_EDGES", "0")
    os.environ["CYMATIX_DISABLE_LEARN"] = "1"
    if args.cache_mb:
        os.environ["CYMATIX_SQLITE_CACHE_SIZE"] = str(-args.cache_mb * 1024)

    import build_fixture_matrix as bfm  # real module name (spawn-safe idiom)

    try:
        import psutil

        proc = psutil.Process()
    except Exception:
        proc = None
        log.warning("psutil unavailable — io/rss columns will be null")

    roots = args.files_root or [
        str(REPO_ROOT / "docs"),
        str(REPO_ROOT / "cymatix_context"),
        str(REPO_ROOT / "tests"),
    ]
    files = _enumerate_files(
        roots, args.max_files, bfm.MIN_FILE_SIZE, bfm.INGEST_EXTS
    )
    if not files:
        raise SystemExit("no treatment files found")
    log.info("treatment set: %d files from %s", len(files), roots)

    # Chunk + tag once; every arm replays the identical gene stream.
    bfm._init_worker()
    t0 = time.perf_counter()
    gene_lists = [bfm._chunk_and_tag_file(f) for f in files]
    n_genes = sum(len(gl) for gl in gene_lists)
    log.info(
        "chunk+tag: %d genes from %d files in %.1f s",
        n_genes, len(files), time.perf_counter() - t0,
    )

    base = Path(args.base_db)
    if not base.is_file():
        raise SystemExit(f"base bed missing: {base}")
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{base}?mode=ro", uri=True, timeout=120)
    try:
        base_max_rowid = int(
            conn.execute("SELECT MAX(rowid) FROM genes").fetchone()[0] or 0
        )
        base_genes = int(conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0])
    finally:
        conn.close()
    base_relations = _table_count(str(base), "gene_relations")
    log.info(
        "base bed: %s  %.2f GiB  genes=%d  max_rowid=%d  gene_relations=%s",
        base, base.stat().st_size / 1024 ** 3, base_genes, base_max_rowid,
        base_relations,
    )

    results = []
    for k in arms:
        copy = work / f"arm_batch{k}.db"
        log.info("=== arm commit_batch=%d ===", k)
        t_copy = time.perf_counter()
        if copy.exists():
            copy.unlink()
        for suffix in ("-wal", "-shm"):
            side = Path(str(copy) + suffix)
            if side.exists():
                side.unlink()
        shutil.copyfile(base, copy)
        log.info("bed copy in %.1f s", time.perf_counter() - t_copy)

        genome = bfm.Genome(
            path=str(copy), synonym_map={},
            splade_enabled=False, entity_graph=True,
        )
        stats = {
            "files": 0, "genes": 0, "skipped": 0, "errors": 0,
            "t0": time.perf_counter(),
        }
        cc0 = getattr(genome, "commit_count", None)
        ram0 = bfm._available_ram_mb()
        io0 = _io_counters(proc) if proc else None
        t_drain = time.perf_counter()
        try:
            bfm._drain_with_batched_splade(
                iter(gene_lists), genome, stats, commit_batch=k,
            )
            drain_s = time.perf_counter() - t_drain
            io1 = _io_counters(proc) if proc else None
            cc1 = getattr(genome, "commit_count", None)
            wal_end = 0
            wal_path = str(copy) + "-wal"
            if os.path.exists(wal_path):
                wal_end = os.path.getsize(wal_path)
            t_close = time.perf_counter()
        finally:
            genome.close()
        close_s = time.perf_counter() - t_close
        io2 = _io_counters(proc) if proc else None
        ram1 = bfm._available_ram_mb()

        digest, n_new = _new_rows_digest(str(copy), base_max_rowid)
        relations = _table_count(str(copy), "gene_relations")
        bed_delta = copy.stat().st_size - base.stat().st_size

        commits = (cc1 - cc0) if (cc0 is not None and cc1 is not None) else None
        io_drain = _io_delta(io0, io1)
        io_total = _io_delta(io0, io2)
        g = max(stats["genes"], 1)
        row = {
            "commit_batch": k,
            "genes": stats["genes"],
            "errors": stats["errors"],
            "batched_commits": stats.get("batched_commits"),
            "valve_trips": stats.get("valve_trips"),
            "sqlite_commits": commits,
            "drain_wall_s": round(drain_s, 2),
            "close_wall_s": round(close_s, 2),
            "genes_per_s": round(stats["genes"] / max(drain_s, 1e-6), 2),
            "io_drain": io_drain,
            "io_total": io_total,
            "write_mb_per_gene": (
                round(io_total["write_bytes"] / g / 1024 ** 2, 3)
                if io_total else None
            ),
            "write_ops_per_gene": (
                round(io_total["write_count"] / g, 1) if io_total else None
            ),
            "wal_end_bytes": wal_end,
            "bed_delta_bytes": bed_delta,
            "new_rows": n_new,
            "new_rows_digest": digest,
            "gene_relations_delta": (
                relations - base_relations
                if (relations is not None and base_relations is not None)
                else None
            ),
            "avail_ram_mb_before": round(ram0, 0) if ram0 else None,
            "avail_ram_mb_after": round(ram1, 0) if ram1 else None,
        }
        results.append(row)
        log.info(
            "arm %d: %d genes in %.1f s (%.2f g/s) commits=%s "
            "write/gene=%s MB / %s ops digest=%s",
            k, stats["genes"], drain_s, row["genes_per_s"], commits,
            row["write_mb_per_gene"], row["write_ops_per_gene"], digest[:16],
        )

        if not args.keep_beds:
            copy.unlink()
            for suffix in ("-wal", "-shm"):
                side = Path(str(copy) + suffix)
                if side.exists():
                    side.unlink()

    digests = {r["new_rows_digest"] for r in results}
    genes_counts = {r["genes"] for r in results}
    equivalent = len(digests) == 1 and len(genes_counts) == 1
    receipt = {
        "receipt": "ingest_commit_batch_ab",
        "date": datetime.now(timezone.utc).isoformat(),
        "question": (
            "does commit-per-gene cadence own the ingest write "
            "amplification (806 ops / 2.06 MB per gene)?"
        ),
        "base_db": str(base),
        "base_bytes": base.stat().st_size,
        "base_genes": base_genes,
        "treatment_files": len(files),
        "treatment_roots": roots,
        "treatment_genes": n_genes,
        "cache_mb_pinned": args.cache_mb,
        "env_pins": {
            k: os.environ.get(k) for k in (
                "CYMATIX_BFM_SPLADE", "CYMATIX_BFM_DENSE_BACKFILL",
                "CYMATIX_BFM_SEED_EDGES", "CYMATIX_DISABLE_LEARN",
                "CYMATIX_SQLITE_CACHE_SIZE",
            )
        },
        "note": (
            "single process, sequential; identical pre-chunked gene stream "
            "replayed per arm; each arm on a fresh copy of base_db; "
            "io_* from psutil Process.io_counters deltas"
        ),
        "arms": results,
        "content_equivalent_across_arms": equivalent,
    }
    out = Path(args.out) if args.out else (
        REPO_ROOT / "benchmarks" / "dogfood" / "receipts"
        / "ingest_commit_batch_ab.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    log.info("receipt -> %s", out)

    print()
    print(f"{'batch':>6} {'genes':>6} {'wall_s':>8} {'g/s':>7} "
          f"{'commits':>8} {'MB/gene':>8} {'ops/gene':>9} {'digest':>18}")
    for r in results:
        print(f"{r['commit_batch']:>6} {r['genes']:>6} {r['drain_wall_s']:>8} "
              f"{r['genes_per_s']:>7} {str(r['sqlite_commits']):>8} "
              f"{str(r['write_mb_per_gene']):>8} "
              f"{str(r['write_ops_per_gene']):>9} {r['new_rows_digest'][:16]:>18}")
    print(f"\ncontent equivalent across arms: {equivalent}")
    return 0 if equivalent else 1


if __name__ == "__main__":
    raise SystemExit(main())

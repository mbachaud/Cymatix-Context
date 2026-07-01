"""Pin the 6-fixture matrix in place (no copy) with sha256 + size.

Writes ``genomes/bench/matrix/frozen.json`` listing each blob/sharded
target by absolute path, sha256 hash of the primary .db file, byte size,
and gene count. Build cost is now low enough (medium-sharded 8.6 min,
xl-sharded 26 min on dev box per PR #96) that re-builds are cheaper
than redundant frozen-copy storage.

Usage:
    python scripts/freeze_matrix_manifest.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


MATRIX_BLOB_DIR = Path(r"F:\Projects\helix-context\genomes\bench\matrix")
MATRIX_SHARDED_DIR = Path(r"F:\Projects\helix-context\genomes\bench\matrix-sharded")

TARGETS = [
    {
        "key": "small", "mode": "blob",
        "path": MATRIX_BLOB_DIR / "small.db",
    },
    {
        "key": "medium", "mode": "blob",
        "path": MATRIX_BLOB_DIR / "medium.db",
    },
    {
        "key": "large", "mode": "blob",
        "path": MATRIX_BLOB_DIR / "large.db",
    },
    {
        "key": "xl", "mode": "blob",
        "path": MATRIX_BLOB_DIR / "xl.db",
    },
    {
        "key": "medium-sharded", "mode": "sharded",
        "path": MATRIX_SHARDED_DIR / "medium" / "main.genome.db",
        "shard_dir": MATRIX_SHARDED_DIR / "medium",
    },
    {
        "key": "xl-sharded", "mode": "sharded",
        "path": MATRIX_SHARDED_DIR / "xl" / "main.genome.db",
        "shard_dir": MATRIX_SHARDED_DIR / "xl",
    },
]


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def gene_count_blob(path: Path) -> int:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT COUNT(*) FROM genes").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception as exc:
        return -1


def sharded_summary(main_path: Path) -> dict:
    """Sum gene counts across registered shards and list shard files."""
    summary = {"shards": [], "total_genes": 0, "shard_count": 0}
    if not main_path.exists():
        return summary
    try:
        conn = sqlite3.connect(f"file:{main_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT shard_name, category, path, gene_count, byte_size "
                "FROM shards WHERE health='ok' ORDER BY shard_name"
            ).fetchall()
            for r in rows:
                p = Path(r["path"])
                shard = {
                    "name": r["shard_name"],
                    "category": r["category"],
                    "path": str(p),
                    "gene_count": int(r["gene_count"] or 0),
                    "bytes": int(r["byte_size"] or 0),
                }
                if p.exists():
                    shard["sha256"] = sha256_file(p)
                    shard["actual_bytes"] = p.stat().st_size
                else:
                    shard["sha256"] = None
                    shard["actual_bytes"] = -1
                summary["shards"].append(shard)
                summary["total_genes"] += shard["gene_count"]
            summary["shard_count"] = len(summary["shards"])
        finally:
            conn.close()
    except Exception:
        pass
    return summary


def main() -> int:
    out: dict = {
        "matrix": "fixture_matrix_v1",
        "spec": "docs/benchmarks/GENOME_FIXTURE_MATRIX.md",
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "host": os.environ.get("COMPUTERNAME", ""),
        "policy": "in-place pin (no copy) — re-build cost is low enough that hashes serve as the reproducibility anchor",
        "targets": {},
    }

    for t in TARGETS:
        path: Path = t["path"]
        if not path.exists():
            print(f"!! MISSING: {t['key']} at {path}")
            out["targets"][t["key"]] = {
                "mode": t["mode"],
                "path": str(path),
                "status": "missing",
            }
            continue

        print(f".. hashing {t['key']} ({path.stat().st_size / 1_048_576:.1f} MB)")
        t0 = time.perf_counter()
        digest = sha256_file(path)
        hash_s = time.perf_counter() - t0

        entry: dict = {
            "mode": t["mode"],
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": digest,
            "hash_seconds": round(hash_s, 2),
        }

        if t["mode"] == "blob":
            entry["gene_count"] = gene_count_blob(path)
        else:
            sd = sharded_summary(path)
            entry["sharded"] = sd
            entry["gene_count_total"] = sd["total_genes"]
            entry["shard_count"] = sd["shard_count"]
            entry["shard_dir"] = str(t["shard_dir"])

        out["targets"][t["key"]] = entry
        print(f"   sha256={digest[:16]}... genes={entry.get('gene_count') or entry.get('gene_count_total')}")

    manifest_path = MATRIX_BLOB_DIR / "frozen.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"\n[ok] frozen manifest at {manifest_path}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

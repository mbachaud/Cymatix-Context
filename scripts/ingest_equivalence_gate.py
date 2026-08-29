"""Does parallel ingest produce the same bed as sequential ingest?

Background
----------
``docs/benchmarks/BASELINES.md`` blocks ``ingest_c > 1`` until an equivalence
gate exists.  That rule was adopted from cc-exchange EXP-A, which measured
real divergence: pool=4 vs pool=1 changed gene content on 105/150 shared
genes.  But EXP-A ran the **LLM tagger**, and its own classification was
"LLM-batching drift, not a driver bug" — insertion order and SPLADE were
byte-equal, and a sequential re-tag reproduced the pool=1 bytes 6/6.

v0.9.x ingests with ``[ingestion] backend = "cpu"`` (spaCy + regex).  There
is no LLM in the path, so the drift mechanism EXP-A identified cannot fire.
That makes equivalence *plausible* — and plausible is not measured.  This
script measures it.

What it compares
----------------
Both arms drain through the same main-process writer
(``_drain_with_batched_splade``); the only difference is whether
chunk+tag ran in an ``mp.Pool``.  Note ``imap_unordered``: insertion order
is genuinely nondeterministic under ``--parallel``, so physical layout is
*expected* to differ.  The gate therefore separates three questions:

1. **documents** — is the ``gene_id`` set identical?  (ids are content-derived)
2. **content**   — is every gene's content and tag multiset identical?
3. **structure** — do the order-sensitive derived tables match
   (``entity_graph``, ``harmonic_links``, ``gene_relations``)?

1+2 passing is what "the bed holds the same knowledge" means.  3 is reported
separately because a co-activation-graph difference is retrieval-relevant
(the harmonic tier reads it) even when content is identical.

``equivalent`` in the receipt is true only when all three agree.

Usage
-----
    python scripts/ingest_equivalence_gate.py \\
        --root F:/tmp/enterprise_rag_500k/sources \\
        --sample 3000 --workers 6 --out benchmarks/dogfood/receipts/ingest_equivalence.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

log = logging.getLogger("ingest_equivalence_gate")


def _load_bfm():
    """Import ``build_fixture_matrix`` under its real module name.

    It must be a normally-importable module, not a synthetic one: the
    ``mp.Pool`` children pickle ``_chunk_and_tag_file`` **by qualified name**,
    so on Windows spawn they re-import the defining module.  A module loaded
    under an invented name (or from a spec) exists only in the parent's
    ``sys.modules`` and the children would fail to unpickle it.  Putting
    ``scripts/`` on ``sys.path`` works because spawn ships the parent's
    ``sys.path`` to children via ``get_preparation_data()``.
    """
    scripts_dir = str(REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import build_fixture_matrix  # noqa: PLC0415

    return build_fixture_matrix


def _enumerate(root: Path, exts: set[str], cache: Optional[Path]) -> Dict[str, List[str]]:
    """Sorted ingestable files per source root, cached to JSON.

    Walking this corpus is ~500k stat calls and dominates a small run's wall
    time, so the listing is cached; the corpus is byte-frozen for benching.
    """
    if cache and cache.exists():
        log.info("file listing from cache %s", cache)
        return json.loads(cache.read_text(encoding="utf-8"))
    per_root: Dict[str, List[str]] = {}
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        found: List[str] = []
        for dirpath, _dirnames, filenames in os.walk(sub):
            for fname in filenames:
                if os.path.splitext(fname)[1].lower() in exts:
                    found.append(os.path.join(dirpath, fname))
        per_root[sub.name] = sorted(found)
        log.info("  %-16s %7d files", sub.name, len(found))
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(per_root), encoding="utf-8")
        log.info("cached listing -> %s", cache)
    return per_root


def _sample_files(
    root: Path, n: int, seed: int, exts: set[str], cache: Optional[Path] = None
) -> List[Tuple[str, str]]:
    """Deterministic sample of ingestable files, spread across source roots.

    Sorted before sampling so ``(tree, seed, n)`` fixes the file list exactly.
    """
    per_root = _enumerate(root, exts, cache)

    total = sum(len(v) for v in per_root.values())
    if not total:
        raise SystemExit(f"no ingestable files under {root}")

    rng = random.Random(seed)
    picked: List[str] = []
    for name, files in sorted(per_root.items()):
        share = max(1, round(n * len(files) / total))
        picked.extend(rng.sample(files, min(share, len(files))))
    picked = sorted(picked)[:n]
    return [(f, os.path.splitext(f)[1].lower()) for f in picked]


def _fresh_genome(bfm, db_path: Path):
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return bfm.Genome(
        path=str(db_path),
        synonym_map={},
        splade_enabled=False,   # v0.9.x shipped default
        entity_graph=True,      # hardcoded in the builder; mirrored here
    )


def _new_stats(mode: str) -> dict:
    return {
        "files": 0, "genes": 0, "skipped": 0, "errors": 0,
        "missing_roots": [], "mode": mode, "t0": time.perf_counter(),
    }


def _ingest_sequential(bfm, files, genome) -> dict:
    stats = _new_stats("sequential")
    bfm._init_worker()
    gene_iter = (bfm._chunk_and_tag_file(f) for f in files)
    bfm._drain_with_batched_splade(gene_iter, genome, stats)
    return stats


def _ingest_parallel(bfm, files, genome, workers: int) -> dict:
    stats = _new_stats("parallel")
    bfm._parallel_ingest_to_genome(files, genome, stats, n_workers=workers)
    return stats


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _gene_id_digest(conn: sqlite3.Connection) -> Tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    for (gid,) in conn.execute("SELECT gene_id FROM genes ORDER BY gene_id"):
        h.update(str(gid).encode("utf-8"))
        h.update(b"\x00")
        n += 1
    return h.hexdigest(), n


def _content_digest(conn: sqlite3.Connection) -> str:
    """Digest of (gene_id, content, tags) over all genes, in gene_id order."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(genes)")}
    tag_col = "tags" if "tags" in cols else None
    sel = "gene_id, content" + (f", {tag_col}" if tag_col else "")
    h = hashlib.sha256()
    for row in conn.execute(f"SELECT {sel} FROM genes ORDER BY gene_id"):
        h.update(str(row[0]).encode("utf-8"))
        h.update(b"\x1f")
        h.update((row[1] or "").encode("utf-8"))
        if tag_col:
            # Tag order within a gene is not semantically meaningful; sort it
            # so a reordering is not reported as a content difference.
            raw = row[2] or ""
            parts = sorted(str(raw).replace(",", " ").split())
            h.update(b"\x1f")
            h.update(" ".join(parts).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _structure_counts(conn: sqlite3.Connection) -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {}
    for table in ("entity_graph", "harmonic_links", "gene_relations", "filename_index"):
        try:
            out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except Exception:
            out[table] = None
    return out


def _first_content_diffs(a: sqlite3.Connection, b: sqlite3.Connection, limit: int = 5):
    """Name a few genes whose content differs, so a failure is actionable."""
    da = dict(a.execute("SELECT gene_id, content FROM genes ORDER BY gene_id"))
    db = dict(b.execute("SELECT gene_id, content FROM genes ORDER BY gene_id"))
    diffs = []
    for gid in sorted(set(da) & set(db)):
        if da[gid] != db[gid]:
            diffs.append({
                "gene_id": gid,
                "seq_len": len(da[gid] or ""),
                "par_len": len(db[gid] or ""),
            })
            if len(diffs) >= limit:
                break
    return {
        "content_differs_sample": diffs,
        "only_in_sequential": sorted(set(da) - set(db))[:limit],
        "only_in_parallel": sorted(set(db) - set(da))[:limit],
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", required=True, help="Corpus root holding source subdirs.")
    p.add_argument("--sample", type=int, default=3000, help="Files per arm (default 3000).")
    p.add_argument("--seed", type=int, default=20260824, help="Sample seed.")
    p.add_argument("--workers", type=int, default=6, help="Workers for the parallel arm.")
    p.add_argument("--work-dir", default=None, help="Scratch dir for the two beds.")
    p.add_argument("--out", default=None, help="Receipt JSON path.")
    p.add_argument("--keep-beds", action="store_true", help="Do not delete the temp beds.")
    p.add_argument("--listing-cache", default=None, help="JSON cache of the corpus file listing.")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _build_parser().parse_args(argv)

    os.environ["CYMATIX_DISABLE_LEARN"] = "1"
    bfm = _load_bfm()

    root = Path(args.root)
    cache = Path(args.listing_cache) if args.listing_cache else None
    files = _sample_files(root, args.sample, args.seed, bfm.INGEST_EXTS, cache)
    log.info("sampled %d files from %s (seed=%d)", len(files), root, args.seed)

    work = Path(args.work_dir) if args.work_dir else Path(
        os.environ.get("TEMP", "/tmp")
    ) / "cymatix_ingest_gate"
    work.mkdir(parents=True, exist_ok=True)
    seq_db, par_db = work / "seq.db", work / "par.db"

    log.info("--- arm A: sequential (ingest_c=1) ---")
    g = _fresh_genome(bfm, seq_db)
    t0 = time.perf_counter()
    seq_stats = _ingest_sequential(bfm, files, g)
    seq_wall = time.perf_counter() - t0
    g.close() if hasattr(g, "close") else None

    log.info("--- arm B: parallel (ingest_c=%d) ---", args.workers)
    g = _fresh_genome(bfm, par_db)
    t0 = time.perf_counter()
    par_stats = _ingest_parallel(bfm, files, g, args.workers)
    par_wall = time.perf_counter() - t0
    g.close() if hasattr(g, "close") else None

    a, b = _connect(seq_db), _connect(par_db)
    try:
        seq_ids, seq_n = _gene_id_digest(a)
        par_ids, par_n = _gene_id_digest(b)
        seq_content, par_content = _content_digest(a), _content_digest(b)
        seq_struct, par_struct = _structure_counts(a), _structure_counts(b)

        docs_equal = seq_ids == par_ids
        content_equal = seq_content == par_content
        struct_equal = seq_struct == par_struct
        detail = {} if (docs_equal and content_equal) else _first_content_diffs(a, b)
    finally:
        a.close()
        b.close()

    receipt: Dict[str, Any] = {
        "tool": "ingest_equivalence_gate",
        "stamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "corpus_root": str(root),
        "sample_files": len(files),
        "seed": args.seed,
        "workers": args.workers,
        "arms": {
            "sequential": {
                "ingest_c": 1, "wall_s": round(seq_wall, 2),
                "genes": seq_n, "files": seq_stats.get("files"),
                "errors": seq_stats.get("errors"),
                "files_per_s": round(len(files) / seq_wall, 3) if seq_wall else None,
                "genes_per_s": round(seq_n / seq_wall, 3) if seq_wall else None,
                "gene_id_sha256": seq_ids,
                "content_sha256": seq_content,
                "structure": seq_struct,
            },
            "parallel": {
                "ingest_c": args.workers, "wall_s": round(par_wall, 2),
                "genes": par_n, "files": par_stats.get("files"),
                "errors": par_stats.get("errors"),
                "files_per_s": round(len(files) / par_wall, 3) if par_wall else None,
                "genes_per_s": round(par_n / par_wall, 3) if par_wall else None,
                "gene_id_sha256": par_ids,
                "content_sha256": par_content,
                "structure": par_struct,
            },
        },
        "documents_equal": docs_equal,
        "content_equal": content_equal,
        "structure_equal": struct_equal,
        "equivalent": bool(docs_equal and content_equal and struct_equal),
        "speedup": round(seq_wall / par_wall, 2) if par_wall else None,
        "detail": detail,
    }

    log.info("documents_equal = %s", docs_equal)
    log.info("content_equal   = %s", content_equal)
    log.info("structure_equal = %s  (seq=%s par=%s)", struct_equal, seq_struct, par_struct)
    log.info("EQUIVALENT      = %s", receipt["equivalent"])
    log.info(
        "wall: sequential %.1f s vs parallel %.1f s  (speedup %.2fx)",
        seq_wall, par_wall, receipt["speedup"] or 0.0,
    )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
        log.info("receipt -> %s", out)
    else:
        print(json.dumps(receipt, indent=2, sort_keys=True))

    if not args.keep_beds:
        shutil.rmtree(work, ignore_errors=True)

    return 0 if receipt["equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

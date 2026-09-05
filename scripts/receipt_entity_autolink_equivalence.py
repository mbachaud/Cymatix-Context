"""Ingest-equivalence receipt for the entity-autolink scheduling knob.

Companion to ``scripts/ingest_equivalence_gate.py`` (same digest helpers,
same bar). Builds two beds from the SAME deterministic file sample with the
SAME sequential scheduling; the only difference is
``entity_autolink`` (on = today's behavior, off = the 2026-08-30
ingest-decay fix). Asserts:

1. **documents** — gene_id digests identical
2. **content**  — (gene_id, content, tags) digests identical
3. **entity rows** — full (entity, gene_id) multiset digest identical
   (stronger than the gate's count-only structure check: proves the knob
   changes edge FORMATION only, not the entity index)
4. **edges** — gene_relations relation=5 COVER edges present in arm A,
   absent in arm B (the intended, documented delta; default-inert at query
   time per the W2.1 cover-walk kill receipt)

Rates are recorded but are NOT the cost evidence — the cost receipt is the
py-spy profile of the live 289k-gene enronqa_padded writer
(``benchmarks/dogfood/receipts/ingest_decay_enronqa_2026-08-30.json``).

Usage
-----
    python scripts/receipt_entity_autolink_equivalence.py \\
        --root F:/Projects/enronqa/padding --sample 2000 \\
        --out benchmarks/dogfood/receipts/entity_autolink_equivalence_enronqa.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SCRIPTS_DIR = str(REPO_ROOT / "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ingest_equivalence_gate as gate  # noqa: E402

log = logging.getLogger("receipt_entity_autolink_equivalence")


def _fresh_genome(bfm, db_path: Path, entity_autolink: bool):
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
        entity_autolink=entity_autolink,
    )


def _entity_rows_digest(conn: sqlite3.Connection) -> str:
    h = hashlib.sha256()
    for ent, gid in conn.execute(
        "SELECT entity, gene_id FROM entity_graph ORDER BY entity, gene_id"
    ):
        h.update(ent.encode("utf-8"))
        h.update(b"\x1f")
        h.update(gid.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _build_arm(bfm, files, db_path: Path, entity_autolink: bool) -> dict:
    genome = _fresh_genome(bfm, db_path, entity_autolink)
    stats = {
        "files": 0, "genes": 0, "skipped": 0, "errors": 0,
        "t0": time.perf_counter(),
    }
    t0 = time.perf_counter()
    bfm._init_worker()
    gene_iter = (bfm._chunk_and_tag_file(f) for f in files)
    bfm._drain_with_batched_splade(gene_iter, genome, stats)
    wall = time.perf_counter() - t0
    if hasattr(genome, "close"):
        genome.close()
    return {
        "entity_autolink": entity_autolink,
        "files": stats["files"],
        "genes": stats["genes"],
        "errors": stats["errors"],
        "wall_s": round(wall, 2),
        "genes_per_s": round(stats["genes"] / max(wall, 1e-9), 3),
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", required=True, help="Corpus root holding source subdirs.")
    p.add_argument("--sample", type=int, default=2000, help="Files per arm.")
    p.add_argument("--seed", type=int, default=20260830, help="Sample seed.")
    p.add_argument("--work-dir", default=None, help="Scratch dir for the two beds.")
    p.add_argument("--out", default=None, help="Receipt JSON path.")
    p.add_argument("--listing-cache", default=None, help="JSON cache of the file listing.")
    args = p.parse_args(argv)

    os.environ["CYMATIX_DISABLE_LEARN"] = "1"
    bfm = gate._load_bfm()

    root = Path(args.root)
    cache = Path(args.listing_cache) if args.listing_cache else None
    files = gate._sample_files(root, args.sample, args.seed, bfm.INGEST_EXTS, cache)
    log.info("sampled %d files from %s (seed=%d)", len(files), root, args.seed)

    work = Path(args.work_dir) if args.work_dir else Path(
        os.environ.get("TEMP", "/tmp")
    ) / "cymatix_autolink_receipt"
    work.mkdir(parents=True, exist_ok=True)
    on_db, off_db = work / "autolink_on.db", work / "autolink_off.db"

    log.info("--- arm A: entity_autolink=on (today's behavior) ---")
    arm_on = _build_arm(bfm, files, on_db, entity_autolink=True)
    log.info("--- arm B: entity_autolink=off (scheduling fix) ---")
    arm_off = _build_arm(bfm, files, off_db, entity_autolink=False)

    a, b = gate._connect(on_db), gate._connect(off_db)
    try:
        on_ids, _ = gate._gene_id_digest(a)
        off_ids, _ = gate._gene_id_digest(b)
        on_content, off_content = gate._content_digest(a), gate._content_digest(b)
        on_ents, off_ents = _entity_rows_digest(a), _entity_rows_digest(b)
        on_struct, off_struct = gate._structure_counts(a), gate._structure_counts(b)
        on_edges = int(a.execute(
            "SELECT COUNT(*) FROM gene_relations WHERE relation = 5"
        ).fetchone()[0])
        off_edges = int(b.execute(
            "SELECT COUNT(*) FROM gene_relations WHERE relation = 5"
        ).fetchone()[0])
    finally:
        a.close()
        b.close()

    arm_on.update({"content_sha256": on_content, "gene_id_sha256": on_ids,
                   "entity_rows_sha256": on_ents, "structure": on_struct,
                   "cover_edges": on_edges})
    arm_off.update({"content_sha256": off_content, "gene_id_sha256": off_ids,
                    "entity_rows_sha256": off_ents, "structure": off_struct,
                    "cover_edges": off_edges})

    docs_equal = on_ids == off_ids
    content_equal = on_content == off_content
    entity_rows_equal = on_ents == off_ents
    edges_delta_as_designed = on_edges > 0 and off_edges == 0
    equivalent = docs_equal and content_equal and entity_rows_equal

    receipt = {
        "tool": "receipt_entity_autolink_equivalence",
        "stamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "corpus_root": str(root),
        "sample_files": len(files),
        "seed": args.seed,
        "scheduling": "sequential-both-arms",
        "arms": {"autolink_on": arm_on, "autolink_off": arm_off},
        "documents_equal": docs_equal,
        "content_equal": content_equal,
        "entity_rows_equal": entity_rows_equal,
        "edges_delta_as_designed": edges_delta_as_designed,
        "equivalent": equivalent,
        "note": (
            "Both arms sequential from the same sample: the ONLY difference "
            "is entity_autolink. equivalent=true means documents, content, "
            "and the full entity_graph row multiset are byte-identical; the "
            "designed delta is gene_relations relation=5 COVER edges "
            "(present in arm A, absent in arm B), which are default-inert "
            "at query time (W2.1 cover-walk kill) and already "
            "order-nondeterministic under --parallel "
            "(ingest_equivalence_enronqa.json). Rates here are informative "
            "only; the cost receipt is the live-writer py-spy profile."
        ),
    }

    out = Path(args.out) if args.out else (
        REPO_ROOT / "benchmarks" / "dogfood" / "receipts"
        / "entity_autolink_equivalence.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=1, sort_keys=True), encoding="utf-8")
    log.info("receipt -> %s", out)
    log.info(
        "documents_equal=%s content_equal=%s entity_rows_equal=%s "
        "edges on/off=%d/%d equivalent=%s",
        docs_equal, content_equal, entity_rows_equal,
        on_edges, off_edges, equivalent,
    )
    return 0 if equivalent and edges_delta_as_designed else 1


if __name__ == "__main__":
    raise SystemExit(main())

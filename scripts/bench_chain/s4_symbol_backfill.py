r"""s4_symbol_backfill.py -- populate the WS2 symbol graph on a COPY of a
frozen SIKE bed, without re-ingesting (arm C prep for PRs #230/#231).

Why a backfill instead of a fresh ingest: the xl bed is the Run-2 reference
bed (sike_fts_depth_sweep_xl.json: additive 0.62 / rrf 0.74 at depth=48).
Re-ingesting would change chunk boundaries, gene_ids, and FTS statistics,
making any arm-C delta uninterpretable. Backfilling adds ONLY the two symbol
artifacts the WS2/WS3 branches read at query time --

    symbol_defs rows            (symbol -> defining gene_id index)
    SYMBOL_REF gene_relations   (referencing chunk -> defining chunk edges)

-- so the copy stays byte-comparable to Run-2 on every retrieval-relevant
surface except the symbol graph itself. cap=0 cells on the copy double as
the drift check against the Run-2 references.

Mechanics:
  1. Copy source bed -> dest bed via the SQLite backup API from a READ-ONLY
     source connection (mode=ro URI). The backup API folds any -wal content
     into the destination, so the copy is a clean, checkpointed single file
     and the source's -wal/-shm are never touched.
  2. Open the copy read-write with the branch KnowledgeStore -- its DDL
     (storage/ddl.py) creates symbol_defs + indexes on open.
  3. Select code genes: code-ness is inferred from the source extension
     using the SAME mapping the ingest CLI uses (#224:
     cli.cmd_ingest._CODE_EXTENSIONS); language via
     encoding.tree_chunker.detect_language. Parents (is_parent=true /
     sequence_index=-1) and source-less genes are skipped.
  4. Per gene: run the branch's tree_chunker.chunk_code_with_symbols on the
     gene's stored content; union defs/refs across returned sub-chunks into
     one (gene_id, defs, refs) tuple. Per-gene failures log + skip.
  5. Per source_id group: call the branch's
     HelixContextManager._emit_symbol_graph itself (via a genome-only shim, so
     the emission semantics -- def indexing, intra-file ref->definer
     resolution, self-edge skip, dedupe -- are the shipped code, not a
     re-implementation). A counting wrapper around the store records how
     many def rows / SYMBOL_REF edges were actually written.

Output: one JSON summary on stdout. HARD-FAILS (exit 2) if the backfill
produced zero def rows or zero SYMBOL_REF edges -- an empty symbol graph
would make the arm-C sweep a silent no-op (gap A3 all over again).

Run from anywhere; paths default to the MAIN checkout's bed directory
(auto-detected when this script lives in a .worktrees/<name> worktree).
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _main_root() -> Path:
    """MAIN checkout root: beds + results live there even when this script
    runs from a .worktrees/<name> worktree of it."""
    parts = _REPO_ROOT.parts
    if ".worktrees" in parts:
        return Path(*parts[: parts.index(".worktrees")])
    return _REPO_ROOT


log = logging.getLogger("s4_symbol_backfill")


class _CountingStore:
    """Pass-through to KnowledgeStore that counts symbol writes.

    HelixContextManager._emit_symbol_graph resolves ``store_symbol_defs`` /
    ``store_relations_batch`` via getattr on ``self.genome``; wrapping the
    store lets us run the shipped emission code verbatim while still
    reporting exactly how many rows/edges it wrote.
    """

    def __init__(self, store):
        self._store = store
        self.n_def_rows = 0
        self.n_edges = 0

    def store_symbol_defs(self, rows: list) -> None:
        self.n_def_rows += len(rows)
        self._store.store_symbol_defs(rows)

    def store_relations_batch(self, relations: list) -> None:
        self.n_edges += len(relations)
        self._store.store_relations_batch(relations)


def _copy_bed(src: Path, dst: Path) -> None:
    """Backup-API copy from a read-only source connection (WAL-safe)."""
    free = shutil.disk_usage(dst.parent).free
    need = int(src.stat().st_size * 1.2)
    if free < need:
        raise RuntimeError(
            f"not enough free space for bed copy: need ~{need/2**30:.1f} GB, "
            f"have {free/2**30:.1f} GB on {dst.parent}")
    if dst.exists():
        raise RuntimeError(
            f"dest bed already exists: {dst} (pass --force to overwrite)")
    t0 = time.time()
    src_conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    print(f"copied {src.name} -> {dst.name} "
          f"({dst.stat().st_size/2**30:.2f} GB, {time.time()-t0:.1f}s)",
          flush=True)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    main_root = _main_root()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source-bed",
                    default=str(main_root / "genomes/bench/sike_beds/xl.db"))
    ap.add_argument("--dest-bed",
                    default=str(main_root / "genomes/bench/sike_beds/xl_symbol.db"))
    ap.add_argument("--force", action="store_true",
                    help="Overwrite an existing dest bed.")
    ap.add_argument("--skip-copy", action="store_true",
                    help="Reuse an existing dest bed (backfill only).")
    args = ap.parse_args(argv)

    src = Path(args.source_bed)
    dst = Path(args.dest_bed)
    if not src.exists():
        print(f"source bed not found: {src}", file=sys.stderr)
        return 2

    if args.skip_copy:
        if not dst.exists():
            print(f"--skip-copy but dest bed missing: {dst}", file=sys.stderr)
            return 2
    else:
        if dst.exists() and args.force:
            dst.unlink()
            for suffix in ("-wal", "-shm"):
                side = Path(str(dst) + suffix)
                if side.exists():
                    side.unlink()
        _copy_bed(src, dst)

    # Branch imports (worktree root is sys.path[0]): the served capability.
    from cymatix_context.cli.cmd_ingest import _CODE_EXTENSIONS  # #224 mapping
    from cymatix_context.context_manager import HelixContextManager
    from cymatix_context.encoding import tree_chunker
    from cymatix_context.knowledge_store import KnowledgeStore
    from cymatix_context.schemas import StructuralRelation

    # Open read-write with the branch store: DDL creates symbol_defs on open.
    store = KnowledgeStore(str(dst))
    counting = _CountingStore(store)
    shim = SimpleNamespace(genome=counting)  # _emit_symbol_graph reads .genome

    skipped: Counter = Counter()
    per_lang: Counter = Counter()
    n_code_genes = 0
    by_file: dict[str, list[tuple]] = {}

    # Probe parser availability once per language so a missing tree-sitter
    # package skips a language, not the run.
    parser_ok: dict[str, bool] = {}

    def _has_parser(lang: str) -> bool:
        if lang not in parser_ok:
            try:
                tree_chunker._get_parser(lang)
                parser_ok[lang] = True
            except Exception:
                parser_ok[lang] = False
        return parser_ok[lang]

    cur = store.conn.cursor()
    rows = cur.execute(
        "SELECT gene_id, content, source_id, key_values, promoter FROM genes"
    ).fetchall()
    for gene_id, content, source_id, key_values, promoter in rows:
        if not source_id:
            skipped["no_source_id"] += 1
            continue
        ext = Path(source_id.lower()).suffix
        if ext not in _CODE_EXTENSIONS:
            skipped["not_code"] += 1
            continue
        if key_values and "is_parent=true" in key_values:
            skipped["parent_doc"] += 1
            continue
        if promoter and '"sequence_index": -1' in promoter:
            skipped["parent_doc"] += 1
            continue
        if not content:
            skipped["empty_content"] += 1
            continue
        lang = tree_chunker.detect_language(source_id)
        if lang is None:
            skipped[f"unsupported_language:{ext}"] += 1
            continue
        if not _has_parser(lang):
            skipped[f"no_parser:{lang}"] += 1
            continue
        n_code_genes += 1
        try:
            chunks = tree_chunker.chunk_code_with_symbols(
                content, language=lang, source_id=source_id)
        except Exception as exc:
            skipped["chunker_error"] += 1
            log.warning("chunker failed on gene %s (%s): %s",
                        gene_id, source_id, exc)
            continue
        defs: set = set()
        refs: set = set()
        for c in chunks:
            defs.update(c.get("defs", ()))
            refs.update(c.get("refs", ()))
        per_lang[lang] += 1
        if defs or refs:
            by_file.setdefault(source_id, []).append(
                (gene_id, sorted(defs), sorted(refs)))
        else:
            skipped["no_symbols"] += 1

    # Emit per file group via the branch's own emission function.
    n_files = 0
    for source_id, chunk_syms in by_file.items():
        try:
            HelixContextManager._emit_symbol_graph(shim, chunk_syms)
            n_files += 1
        except Exception as exc:
            skipped["emit_error"] += 1
            log.warning("symbol emission failed for %s: %s", source_id, exc)

    # Post-write ground truth from the bed itself (INSERT OR IGNORE and edge
    # upserts can dedupe below the attempted-write counters).
    n_def_rows = cur.execute("SELECT COUNT(*) FROM symbol_defs").fetchone()[0]
    n_edges = cur.execute(
        "SELECT COUNT(*) FROM gene_relations WHERE relation = ?",
        (int(StructuralRelation.SYMBOL_REF),)).fetchone()[0]

    store.conn.commit()
    try:
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        log.warning("final wal_checkpoint failed: %s", exc)
    try:
        store.close()
    except Exception as exc:
        log.warning("store close failed: %s", exc)

    summary = {
        "source_bed": str(src),
        "dest_bed": str(dst),
        "n_code_genes": n_code_genes,
        "n_files": n_files,
        "n_def_rows": n_def_rows,
        "n_symbol_ref_edges": n_edges,
        "attempted_def_rows": counting.n_def_rows,
        "attempted_edges": counting.n_edges,
        "genes_per_language": dict(per_lang),
        "n_skipped": dict(skipped),
    }
    print(json.dumps(summary, indent=2))

    if n_def_rows == 0 or n_edges == 0:
        print("HARD FAIL: empty symbol graph "
              f"(def_rows={n_def_rows}, symbol_ref_edges={n_edges}) -- "
              "the arm-C sweep would be a silent no-op.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

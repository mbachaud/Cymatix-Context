"""Generic needle + gold gene_id resolver for file-emitted bench corpora.

Generalises ``scripts/resolve_locomo_needles.py`` for the second-wave code
benches (CodeRAG-Bench, CosQA, SWE-bench Verified): every corpus builder
emits ``needles_<tag>.json`` (``{"needles": [{name, query, question_type}]}``)
and ``gold_paths_<tag>.json`` (``name -> [corpus-relative paths]``); this
script joins the gold paths to the bed's ``genes.source_id`` after ingest and
writes the ladder-format inputs. gene_ids are content hashes, so the mapping
is bed-specific and must be rebuilt after every ingest.

Join strategy, in order (same as the LoCoMo resolver):
  1. ``genes.source_id`` == absolute Windows path of the gold file.
  2. Byte-identical twin fallback: content-hash dedup keeps ONE source_id per
     identical-content gene, so a gold file whose bytes also exist elsewhere
     in the corpus may be attributed to its twin's path. Twins (same size,
     same sha256) are retried on the source_id join.
A gold file below the builder's 50-byte gate or above 200,000 bytes never
ingested; it is recorded per needle as ``gold_size_gated`` and the needle
keeps only its in-gate gold (dropped when none is left).

Outputs (under --bench-dir):
    needles_resolved_<tag>_full.json    ladder-format needles
    gold_by_needle_<tag>.json           name -> [gene_ids]
    needles_<tag>_resolve_meta.json     per-needle detail + summary

Usage:
    python scripts/resolve_bench_needles.py --tag cosqa \\
        --db F:/tmp/cosqa_bed/cosqa.db --bench-dir benchmarks/dogfood/cosqa \\
        --corpus-root F:/Projects/cosqa/corpus
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("resolve_bench_needles")

MIN_FILE_SIZE = 50        # mirrors scripts/build_fixture_matrix.py
MAX_FILE_SIZE = 200_000


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _genes_for_paths(conn: sqlite3.Connection, paths: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for i in range(0, len(paths), 500):
        chunk = paths[i:i + 500]
        marks = ",".join("?" for _ in chunk)
        for sid, gid in conn.execute(
            f"SELECT source_id, gene_id FROM genes WHERE source_id IN ({marks})",
            chunk,
        ):
            out[sid].append(gid)
    return out


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tag", required=True, help="bench tag, e.g. cosqa / coderag_docs / swebench")
    ap.add_argument("--db", required=True)
    ap.add_argument("--bench-dir", required=True)
    ap.add_argument("--corpus-root", required=True)
    args = ap.parse_args(argv)

    bench = Path(args.bench_dir)
    corpus_root = Path(args.corpus_root)
    needles_in = json.loads((bench / f"needles_{args.tag}.json").read_text(encoding="utf-8"))["needles"]
    gold_paths = json.loads((bench / f"gold_paths_{args.tag}.json").read_text(encoding="utf-8"))
    log.info("%d needles, %d gold entries", len(needles_in), len(gold_paths))

    def abs_path(rel: str) -> str:
        return str(corpus_root / rel.replace("/", "\\"))

    wanted = sorted({abs_path(p) for paths in gold_paths.values() for p in paths})
    size_gated: dict[str, int] = {}
    missing_on_disk: list[str] = []
    for p in wanted:
        fp = Path(p)
        if not fp.exists():
            missing_on_disk.append(p)
            continue
        sz = fp.stat().st_size
        if sz < MIN_FILE_SIZE or sz > MAX_FILE_SIZE:
            size_gated[p] = sz
    log.info("gold files: %d wanted, %d size-gated, %d missing on disk",
             len(wanted), len(size_gated), len(missing_on_disk))

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=120)
    try:
        path_to_genes = _genes_for_paths(conn, wanted)
        log.info("direct source_id join: %d / %d gold files", len(path_to_genes), len(wanted))
        unresolved = [p for p in wanted if p not in path_to_genes
                      and p not in size_gated and p not in missing_on_disk]
        fallback_used: dict[str, str] = {}
        if unresolved:
            log.info("sha-twin fallback for %d paths", len(unresolved))
            by_size: dict[int, list[Path]] = defaultdict(list)
            for f in corpus_root.rglob("*"):
                if f.is_file():
                    by_size[f.stat().st_size].append(f)
            for p in unresolved:
                want_sha = _sha256(Path(p))
                twins = [str(t) for t in by_size.get(Path(p).stat().st_size, [])
                         if str(t) != p and _sha256(t) == want_sha]
                hit = _genes_for_paths(conn, twins)
                if hit:
                    twin, gids = next(iter(hit.items()))
                    path_to_genes[p] = gids
                    fallback_used[p] = twin
                else:
                    log.warning("  %s: no twin in bed", p)
    finally:
        conn.close()

    needles_full, gold_by_needle, meta_rows, dropped = [], {}, {}, []
    for n in needles_in:
        paths = [abs_path(p) for p in gold_paths.get(n["name"], [])]
        gids = sorted({g for p in paths for g in path_to_genes.get(p, [])})
        gated = [p for p in paths if p in size_gated]
        if not gids:
            dropped.append(n["name"])
            continue
        needles_full.append(n)
        gold_by_needle[n["name"]] = gids
        meta_rows[n["name"]] = {
            "question_type": n.get("question_type"),
            "gold_files": len(paths),
            "gold_files_resolved": sum(1 for p in paths if p in path_to_genes),
            "gold_genes": len(gids),
            "gold_size_gated": [f"{Path(p).name}:{size_gated[p]}" for p in gated],
            "via_twin": [fallback_used[p] for p in paths if p in fallback_used],
        }

    (bench / f"needles_resolved_{args.tag}_full.json").write_text(
        json.dumps({"needles": needles_full}, indent=1, ensure_ascii=False), encoding="utf-8")
    (bench / f"gold_by_needle_{args.tag}.json").write_text(
        json.dumps(gold_by_needle, indent=1), encoding="utf-8")
    (bench / f"needles_{args.tag}_resolve_meta.json").write_text(
        json.dumps({
            "date": datetime.now(timezone.utc).isoformat(),
            "db": args.db,
            "corpus_root": str(corpus_root),
            "needles_total": len(needles_in),
            "needles_resolvable": len(needles_full),
            "dropped": dropped,
            "gold_files_wanted": len(wanted),
            "gold_files_size_gated": len(size_gated),
            "gold_files_missing_on_disk": missing_on_disk,
            "twin_fallbacks": len(fallback_used),
            "per_needle": meta_rows,
        }, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("resolved %d / %d needles (%d dropped)", len(needles_full), len(needles_in), len(dropped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

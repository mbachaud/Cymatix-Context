"""Resolve LoCoMo/LoCoMo-Plus needles + gold gene_ids against the bed.

Companion to ``scripts/build_locomo_corpus.py`` (adapts
``scripts/resolve_enronqa_needles.py``; no pair/half machinery — the sets are
flat). Gold in ``gold_paths_locomo.json`` is corpus-relative file paths;
gene_ids are content hashes, so the mapping is bed-specific and rebuilt here
after every ingest.

Join strategy, in order:
  1. ``genes.source_id`` == absolute Windows path of the gold file.
  2. Provenance-overwrite fallback: content-hash dedup keeps ONE source_id
     per identical-content gene, so a gold file whose bytes also exist
     elsewhere may be attributed to its twin's path (turn headers make this
     unlikely for turns; cues are pure text). Byte-identical twins on disk
     are retried on the source_id join — the twin's genes ARE the gold genes.

Outputs (under --bench-dir):
    needles_resolved_locomo_full.json    ladder-format needles
    gold_by_needle_locomo.json           name -> [gene_ids]
    needles_locomo_resolve_meta.json     per-needle detail + summary

Usage:
    python scripts/resolve_locomo_needles.py --db F:/tmp/locomo_bed/locomo.db
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("resolve_locomo_needles")


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
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="F:/tmp/locomo_bed/locomo.db")
    ap.add_argument("--bench-dir", default="benchmarks/dogfood/locomo")
    ap.add_argument("--corpus-root", default=r"F:\Projects\locomo-plus\corpus")
    args = ap.parse_args(argv)

    bench = Path(args.bench_dir)
    corpus_root = Path(args.corpus_root)
    needles_in = json.loads(
        (bench / "needles_locomo_pairsless.json").read_text(encoding="utf-8")
    )["needles"]
    gold_paths = json.loads(
        (bench / "gold_paths_locomo.json").read_text(encoding="utf-8")
    )
    log.info("%d needles, %d gold entries", len(needles_in), len(gold_paths))

    def abs_path(rel: str) -> str:
        return str(corpus_root / rel.replace("/", "\\"))

    wanted = sorted({abs_path(p) for paths in gold_paths.values() for p in paths})

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=120)
    try:
        path_to_genes = _genes_for_paths(conn, wanted)
        log.info("direct source_id join: %d / %d gold files",
                 len(path_to_genes), len(wanted))

        unresolved = [p for p in wanted if p not in path_to_genes]
        fallback_used: dict[str, str] = {}
        if unresolved:
            log.info("sha-twin fallback for %d paths", len(unresolved))
            by_size: dict[int, list[Path]] = defaultdict(list)
            for f in corpus_root.rglob("*.txt"):
                by_size[f.stat().st_size].append(f)
            for p in unresolved:
                want_sha = _sha256(Path(p))
                twins = [
                    str(t) for t in by_size.get(Path(p).stat().st_size, [])
                    if str(t) != p and _sha256(t) == want_sha
                ]
                hit = _genes_for_paths(conn, twins)
                if hit:
                    twin, gids = next(iter(hit.items()))
                    path_to_genes[p] = gids
                    fallback_used[p] = twin
                    log.info("  %s -> twin %s (%d genes)", p, twin, len(gids))
                else:
                    log.warning("  %s: no twin in bed — needle will drop", p)
    finally:
        conn.close()

    needles_full, gold_by_needle, meta_rows, dropped = [], {}, {}, []
    for n in sorted(needles_in, key=lambda x: x["name"]):
        paths = [abs_path(p) for p in gold_paths[n["name"]]]
        gids = sorted({g for p in paths for g in path_to_genes.get(p, [])})
        if not gids:
            dropped.append(n["name"])
            continue
        needles_full.append(n)
        gold_by_needle[n["name"]] = gids
        meta_rows[n["name"]] = {
            "question_type": n["question_type"],
            "gold_genes": len(gids),
            "via_twin": [fallback_used[p] for p in paths if p in fallback_used],
        }

    (bench / "needles_resolved_locomo_full.json").write_text(
        json.dumps({"needles": needles_full}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    (bench / "gold_by_needle_locomo.json").write_text(
        json.dumps(gold_by_needle, indent=1), encoding="utf-8")
    (bench / "needles_locomo_resolve_meta.json").write_text(
        json.dumps({
            "date": datetime.now(timezone.utc).isoformat(),
            "db": args.db,
            "needles_total": len(needles_in),
            "needles_resolvable": len(needles_full),
            "dropped": dropped,
            "twin_fallbacks": len(fallback_used),
            "per_needle": meta_rows,
        }, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("resolved %d / %d needles (%d dropped, %d via twin)",
             len(needles_full), len(needles_in), len(dropped),
             len(fallback_used))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

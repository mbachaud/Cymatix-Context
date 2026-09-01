"""Resolve EnronQA paired needles + gold gene_ids against the native bed.

Companion to ``scripts/build_enronqa_corpus.py`` (which emits the corpus and
the paired needle set) and ``scripts/resolve_erb_needles.py`` (the ERB
equivalent this adapts).  Gold in ``gold_paths_enronqa.json`` is stored as
corpus-relative file paths; gene_ids are content hashes, so the mapping to
gene_ids is bed-specific and rebuilt here after every ingest.

Join strategy, in order:
  1. ``genes.source_id`` == absolute Windows path of the gold file.
  2. Provenance-overwrite fallback: content-hash dedup keeps ONE source_id
     per identical-content gene (last writer wins), so a gold file whose
     bytes also exist elsewhere in the corpus may be attributed to its
     twin's path.  For unresolved paths, find byte-identical twin files on
     disk (size prefilter + sha256) and retry the source_id join on the
     twins.  Logged per needle; the twin's genes ARE the gold genes
     (gene_ids are content hashes).

Pairing invariant: needles come in (original, rephrased) pairs sharing one
gold file.  A pair lives or dies together, and the stratified half split
samples PAIRS (both variants together) — splitting variants across halves
would destroy the paraphrase-sensitivity delta the corpus exists to measure.

Outputs (under --bench-dir):
    needles_resolved_enronqa_full.json   ladder-format needles, both variants
    needles_resolved_enronqa_half.json   seeded 50% of PAIRS (both variants)
    gold_by_needle_enronqa.json          name -> [gene_ids] (ladder format)
    needles_enronqa_resolve_meta.json    per-needle detail + summary

Usage:
    python scripts/resolve_enronqa_needles.py --db F:/tmp/enronqa_bed/enronqa.db
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("resolve_enronqa_needles")


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
    ap.add_argument("--db", default="F:/tmp/enronqa_bed/enronqa.db")
    ap.add_argument("--bench-dir", default="benchmarks/dogfood/enronqa")
    ap.add_argument("--corpus-root", default=r"F:\Projects\enronqa\corpus")
    ap.add_argument("--seed", type=int, default=20260829,
                    help="half-split pair sampling seed")
    ap.add_argument("--half-frac", type=float, default=0.5)
    args = ap.parse_args(argv)

    bench = Path(args.bench_dir)
    corpus_root = Path(args.corpus_root)
    needles_in = json.loads(
        (bench / "needles_enronqa_pairs.json").read_text(encoding="utf-8")
    )["needles"]
    gold_paths = json.loads(
        (bench / "gold_paths_enronqa.json").read_text(encoding="utf-8")
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

        # ── Provenance-overwrite fallback for the rest ──────────────────
        unresolved = [p for p in wanted if p not in path_to_genes]
        fallback_used: dict[str, str] = {}  # gold abs path -> twin abs path
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
                    log.warning("  %s: no twin in bed — needle pair will drop", p)
    finally:
        conn.close()

    # ── Assemble needles; a pair lives or dies together ─────────────────
    by_pair: dict[str, list[dict]] = defaultdict(list)
    for n in needles_in:
        by_pair[n["name"].rsplit("_", 1)[0]].append(n)

    needles_full, gold_by_needle, meta_rows, dropped = [], {}, {}, []
    for pair_key in sorted(by_pair):
        variants = by_pair[pair_key]
        assert len(variants) == 2, f"pair {pair_key} has {len(variants)} variants"
        paths = {abs_path(p) for v in variants for p in gold_paths[v["name"]]}
        gids = sorted({g for p in paths for g in path_to_genes.get(p, [])})
        if not gids:
            dropped.append(pair_key)
            continue
        for v in sorted(variants, key=lambda n: n["name"]):
            needles_full.append(v)
            gold_by_needle[v["name"]] = gids
            meta_rows[v["name"]] = {
                "question_type": v["question_type"],
                "gold_genes": len(gids),
                "via_twin": [fallback_used[p] for p in sorted(paths)
                             if p in fallback_used],
            }
    log.info("resolvable: %d needles (%d pairs), dropped pairs: %s",
             len(needles_full), len(needles_full) // 2, dropped or "none")

    # ── Seeded half split over PAIRS (variants stay together) ───────────
    rng = random.Random(args.seed)
    pair_keys = sorted({n["name"].rsplit("_", 1)[0] for n in needles_full})
    k = max(1, round(len(pair_keys) * args.half_frac))
    half_pairs = set(rng.sample(pair_keys, k))
    half = [n for n in needles_full
            if n["name"].rsplit("_", 1)[0] in half_pairs]

    (bench / "needles_resolved_enronqa_full.json").write_text(
        json.dumps({"needles": needles_full}, indent=1) + "\n", encoding="utf-8")
    (bench / "needles_resolved_enronqa_half.json").write_text(
        json.dumps({"needles": half}, indent=1) + "\n", encoding="utf-8")
    (bench / "gold_by_needle_enronqa.json").write_text(
        json.dumps(gold_by_needle, indent=1) + "\n", encoding="utf-8")

    gold_counts = [m["gold_genes"] for m in meta_rows.values()]
    summary = {
        "date": datetime.now(timezone.utc).isoformat(),
        "db": args.db,
        "seed": args.seed,
        "needles_in": len(needles_in),
        "needles_resolvable": len(needles_full),
        "pairs_resolvable": len(needles_full) // 2,
        "pairs_dropped": dropped,
        "half_pairs": len(half_pairs),
        "sha_twin_fallbacks": fallback_used,
        "gold_genes_mean": round(sum(gold_counts) / max(1, len(gold_counts)), 2),
        "gold_genes_max": max(gold_counts) if gold_counts else 0,
        "needles": meta_rows,
    }
    (bench / "needles_enronqa_resolve_meta.json").write_text(
        json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    log.info("wrote resolved full/half + gold + meta under %s", bench)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Re-resolve ERB needles + gold gene_ids against a (re-chunked) bed.

Gold in ``gold_by_needle_*.json`` is stored as gene_ids, which are content
hashes — every re-chunk of the corpus invalidates them (sequencing rule 3:
never difference across needle sets).  This tool rebuilds the mapping from
the bench's own ground truth:

    questions.jsonl        500 questions, ``expected_doc_ids`` (dsid_*)
    uuid_index.json        dsid -> relative source path (511,958 entries)
    bed genes.source_id    absolute path under the corpus root

Outputs (bed-scoped names; never overwrites the 829k-era files):
    needles_resolved_<tag>_full.json   all resolvable needles
    needles_resolved_<tag>_half.json   type-stratified 50% subset (seeded)
    gold_by_needle_<tag>.json          name -> [gene_ids]  (ladder format)
    needles_<tag>_meta.json            per-needle type/doc counts + summary

Usage:
    python scripts/resolve_erb_needles.py --db F:/tmp/erb_blob_v09x.db \
        --tag v09x
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("resolve_erb_needles")

ERB_ROOT = Path("F:/Projects/EnterpriseRAG-Bench-main")
CORPUS_BASE = "F:\\tmp\\enterprise_rag_500k\\sources\\"
OUT_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "dogfood" / "erb"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", required=True, help="Bed to resolve against.")
    p.add_argument("--tag", required=True, help="Output-name tag, e.g. v09x.")
    p.add_argument("--questions", default=str(ERB_ROOT / "questions.jsonl"))
    p.add_argument(
        "--uuid-index",
        default=str(ERB_ROOT / "generated_data" / "uuid_index.json"),
    )
    p.add_argument("--seed", type=int, default=20260827)
    p.add_argument(
        "--half-frac", type=float, default=0.5,
        help="Fraction per question_type in the stratified subset.",
    )
    return p


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _build_parser().parse_args(argv)

    questions = [
        json.loads(line)
        for line in Path(args.questions).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    uuid_index = json.loads(Path(args.uuid_index).read_text(encoding="utf-8"))
    log.info("%d questions, %d uuid_index entries", len(questions), len(uuid_index))

    # dsid -> absolute bed source_id
    def abs_path(rel: str) -> str:
        return CORPUS_BASE + rel.replace("/", "\\")

    wanted_paths: set[str] = set()
    per_q_paths: list[list[str]] = []
    missing_dsids = 0
    for q in questions:
        paths = []
        for dsid in q.get("expected_doc_ids", []):
            rel = uuid_index.get(dsid)
            if rel is None:
                missing_dsids += 1
                continue
            p = abs_path(rel)
            paths.append(p)
            wanted_paths.add(p)
        per_q_paths.append(paths)
    log.info("unique gold source files: %d (dsids missing from index: %d)",
             len(wanted_paths), missing_dsids)

    # One IN() pass per chunk over the bed (no reliance on a source_id index).
    path_to_genes: dict[str, list[str]] = defaultdict(list)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=120)
    try:
        chunk: list[str] = []
        all_paths = sorted(wanted_paths)
        for i in range(0, len(all_paths), 500):
            chunk = all_paths[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            for sid, gid in conn.execute(
                f"SELECT source_id, gene_id FROM genes "
                f"WHERE source_id IN ({marks})", chunk,
            ):
                path_to_genes[sid].append(gid)
    finally:
        conn.close()
    log.info("gold files found in bed: %d / %d",
             len(path_to_genes), len(wanted_paths))

    needles_full = []
    gold_by_needle: dict[str, list[str]] = {}
    meta_rows = {}
    for i, (q, paths) in enumerate(zip(questions, per_q_paths)):
        name = f"erb_{i:03d}"
        gids = sorted({g for p in paths for g in path_to_genes.get(p, [])})
        if not gids:
            continue
        needles_full.append({
            "name": name,
            "query": q["question"],
            "question_type": q.get("question_type", "unknown"),
        })
        gold_by_needle[name] = gids
        meta_rows[name] = {
            "question_id": q.get("question_id"),
            "question_type": q.get("question_type", "unknown"),
            "gold_files": len(paths),
            "gold_files_in_bed": sum(1 for p in paths if p in path_to_genes),
            "gold_genes": len(gids),
        }

    # Type-stratified seeded subset.
    rng = random.Random(args.seed)
    by_type: dict[str, list[dict]] = defaultdict(list)
    for n in needles_full:
        by_type[n["question_type"]].append(n)
    half = []
    for t in sorted(by_type):
        pool = sorted(by_type[t], key=lambda n: n["name"])
        k = max(1, round(len(pool) * args.half_frac))
        half.extend(rng.sample(pool, k))
    half.sort(key=lambda n: n["name"])

    tag = args.tag
    out_full = OUT_DIR / f"needles_resolved_{tag}_full.json"
    out_half = OUT_DIR / f"needles_resolved_{tag}_half.json"
    out_gold = OUT_DIR / f"gold_by_needle_{tag}.json"
    out_meta = OUT_DIR / f"needles_{tag}_meta.json"
    out_full.write_text(
        json.dumps({"needles": needles_full}, indent=1), encoding="utf-8")
    out_half.write_text(
        json.dumps({"needles": half}, indent=1), encoding="utf-8")
    out_gold.write_text(json.dumps(gold_by_needle, indent=1), encoding="utf-8")

    type_counts = {t: len(v) for t, v in sorted(by_type.items())}
    half_counts: dict[str, int] = defaultdict(int)
    for n in half:
        half_counts[n["question_type"]] += 1
    gold_gene_counts = [m["gold_genes"] for m in meta_rows.values()]
    summary = {
        "date": datetime.now(timezone.utc).isoformat(),
        "db": args.db,
        "seed": args.seed,
        "questions_total": len(questions),
        "needles_resolvable": len(needles_full),
        "half_subset": len(half),
        "per_type_full": type_counts,
        "per_type_half": dict(sorted(half_counts.items())),
        "gold_genes_mean": round(
            sum(gold_gene_counts) / max(1, len(gold_gene_counts)), 2),
        "gold_genes_max": max(gold_gene_counts) if gold_gene_counts else 0,
        "needles": meta_rows,
    }
    out_meta.write_text(json.dumps(summary, indent=1), encoding="utf-8")

    log.info("resolvable: %d/%d  half: %d  mean gold genes/needle: %s",
             len(needles_full), len(questions), len(half),
             summary["gold_genes_mean"])
    log.info("per-type (full): %s", type_counts)
    log.info("wrote %s, %s, %s, %s", out_full.name, out_half.name,
             out_gold.name, out_meta.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

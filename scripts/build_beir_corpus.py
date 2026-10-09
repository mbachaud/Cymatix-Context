"""Emit a BEIR dataset's corpus + test needle set as files.

BEIR (Thakur et al. 2021; the UKP mirror zips the ``beir`` library downloads)
ships ``corpus.jsonl`` ({_id, title, text}), ``queries.jsonl`` ({_id, text}) and
``qrels/<split>.tsv`` (query-id, corpus-id, score). Every document is emitted
as ``<corpus-root>/<shard>/<safe_id>.txt`` holding ``title`` + blank line +
``text`` (title omitted when empty) so ``scripts/build_fixture_matrix.py``
ingests the whole corpus as the haystack. Shards are the first two hex chars
of sha1(_id) so no directory holds more than ~1/256 of the corpus.

The needle set is every test query with at least one qrel of score > 0.
Graded relevance is preserved in ``gold_grades_<tag>.json`` for BEIR-standard
NDCG@10 (scored by ``benchmarks/dogfood/beir/beir_ndcg.py``). For datasets
whose queries are themselves corpus documents (ArguAna), BEIR's evaluator
drops the query's own document from the ranking (``ignore_identical_ids``);
the emitter records that document per needle as ``self_doc_<tag>.json`` so the
scorer can do the same.

Outputs (``benchmarks/dogfood/<tag>``):
  needles_<tag>.json        {"needles": [{name, query, question_type}]}
  gold_paths_<tag>.json     needle -> [corpus-relative paths with score > 0]
  gold_grades_<tag>.json    needle -> {corpus-relative path: grade}
  self_doc_<tag>.json       needle -> corpus-relative path (only when present)
  needles_<tag>_meta.json   counts, gate statistics, source

Usage:
  python scripts/build_beir_corpus.py --dataset arguana
  python scripts/build_beir_corpus.py --dataset cqadupstack/android --tag beir_cqa_android
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from collections import defaultdict
from pathlib import Path

log = logging.getLogger("build_beir_corpus")

PULL = Path(r"F:/Projects/bench_pulls/beir")
CORPUS_BASE = Path(r"F:/Projects/beir")
MIN_FILE_SIZE = 50        # mirrors scripts/build_fixture_matrix.py
MAX_FILE_SIZE = 200_000
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(doc_id: str) -> str:
    """Filename for a doc id; ids with unsafe characters or odd length are hashed."""
    if _UNSAFE.search(doc_id) or len(doc_id) > 120 or doc_id.rstrip(". ") != doc_id:
        return "h_" + hashlib.sha1(doc_id.encode("utf-8")).hexdigest()
    return doc_id


def rel_path(doc_id: str) -> str:
    shard = hashlib.sha1(doc_id.encode("utf-8")).hexdigest()[:2]
    return f"{shard}/{safe_name(doc_id)}.txt"


def read_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dataset", required=True, help="folder under the pull dir, e.g. arguana or cqadupstack/android")
    ap.add_argument("--tag", help="bench tag (default beir_<dataset with - and / as _>)")
    ap.add_argument("--split", default="test")
    args = ap.parse_args(argv)

    tag = args.tag or "beir_" + re.sub(r"[-/]", "_", args.dataset)
    src = PULL / args.dataset
    corpus_root = CORPUS_BASE / tag / "corpus"
    bench = Path("benchmarks/dogfood") / tag

    rel_of: dict[str, str] = {}
    seen_rel: set[str] = set()
    gated_small = gated_large = 0
    collisions = 0
    for row in read_jsonl(src / "corpus.jsonl"):
        doc_id = str(row["_id"])
        rel = rel_path(doc_id)
        if rel in seen_rel:
            collisions += 1
        seen_rel.add(rel)
        title = (row.get("title") or "").strip()
        text = (row.get("text") or "").strip()
        body = f"{title}\n\n{text}" if title else text
        data = body.encode("utf-8", errors="replace")
        dest = corpus_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        rel_of[doc_id] = rel
        if len(data) < MIN_FILE_SIZE:
            gated_small += 1
        elif len(data) > MAX_FILE_SIZE:
            gated_large += 1
    log.info("corpus: %d docs -> %s (%d under %d B, %d over %d B)",
             len(rel_of), corpus_root, gated_small, MIN_FILE_SIZE, gated_large, MAX_FILE_SIZE)

    qtext = {str(q["_id"]): q.get("text") or "" for q in read_jsonl(src / "queries.jsonl")}
    grades: dict[str, dict[str, int]] = defaultdict(dict)
    qrels_file = src / "qrels" / f"{args.split}.tsv"
    with open(qrels_file, encoding="utf-8") as f:
        header = f.readline()
        if not header.lower().startswith("query"):
            f.seek(0)
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            qid, did, score = parts[0], parts[1], int(float(parts[2]))
            grades[qid][did] = score

    needles, gold, gold_grades, self_doc = [], {}, {}, {}
    drops = {"no_positive_qrel": 0, "gold_absent_from_corpus": [], "query_text_missing": []}
    for qid in sorted(grades):
        pos = {d: s for d, s in grades[qid].items() if s > 0}
        if not pos:
            drops["no_positive_qrel"] += 1
            continue
        name = f"{tag}_{qid}"
        if qid not in qtext:
            drops["query_text_missing"].append(name)
            continue
        rels = {rel_of[d]: s for d, s in pos.items() if d in rel_of}
        if not rels:
            drops["gold_absent_from_corpus"].append(name)
            continue
        needles.append({"name": name, "query": qtext[qid], "question_type": tag})
        gold[name] = sorted(rels)
        gold_grades[name] = dict(sorted(rels.items()))
        if qid in rel_of:
            self_doc[name] = rel_of[qid]

    bench.mkdir(parents=True, exist_ok=True)
    (bench / f"needles_{tag}.json").write_text(
        json.dumps({"needles": needles}, indent=1, ensure_ascii=False), encoding="utf-8")
    (bench / f"gold_paths_{tag}.json").write_text(json.dumps(gold, indent=1), encoding="utf-8")
    (bench / f"gold_grades_{tag}.json").write_text(json.dumps(gold_grades, indent=1), encoding="utf-8")
    if self_doc:
        (bench / f"self_doc_{tag}.json").write_text(json.dumps(self_doc, indent=1), encoding="utf-8")
    (bench / f"needles_{tag}_meta.json").write_text(json.dumps({
        "source": f"BEIR {args.dataset} (UKP mirror zip; corpus.jsonl, queries.jsonl, qrels/{args.split}.tsv)",
        "corpus_root": str(corpus_root),
        "docs": len(rel_of),
        "docs_under_min_bytes": gated_small,
        "docs_over_max_bytes": gated_large,
        "path_collisions": collisions,
        "n_needles": len(needles),
        "qrels_positive_total": sum(len(v) for v in gold.values()),
        "gold_per_needle_max": max((len(v) for v in gold.values()), default=0),
        "graded": any(s > 1 for v in gold_grades.values() for s in v.values()),
        "queries_that_are_corpus_docs": len(self_doc),
        "drops": {k: (len(v) if isinstance(v, list) else v) for k, v in drops.items()},
        "drop_names": {k: v for k, v in drops.items() if isinstance(v, list) and v},
    }, indent=1), encoding="utf-8")
    log.info("needles: %d (drops %s, self-docs %d)", len(needles),
             {k: (len(v) if isinstance(v, list) else v) for k, v in drops.items()}, len(self_doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

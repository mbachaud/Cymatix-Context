"""Emit the CoIR CosQA corpus + test needle set as files.

CosQA (CoIR-Retrieval/cosqa; web search queries -> Python functions). The
corpus is 20,604 snippets across the train/valid/test partitions; the CoIR
test qrels name 500 (query, snippet) pairs. Every snippet is emitted as
``<root>/<partition>/<_id>.py`` so ``scripts/build_fixture_matrix.py`` ingests
the whole corpus as the haystack; the needle set is the 500 test queries with
their qrels snippet(s) as gold.

Outputs (``benchmarks/dogfood/cosqa``):
  needles_cosqa.json       {"needles": [{name, query, question_type}]}
  gold_paths_cosqa.json    needle -> [corpus-relative paths]
  needles_cosqa_meta.json  counts

Usage:
  python scripts/build_cosqa_corpus.py
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

log = logging.getLogger("build_cosqa_corpus")

PULL = Path(r"F:/Projects/bench_pulls/cosqa")
CORPUS = Path(r"F:/Projects/cosqa/corpus")
BENCH = Path("benchmarks/dogfood/cosqa")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    corpus = pq.read_table(PULL / "corpus" / "corpus-00000-of-00001.parquet").to_pylist()
    queries = pq.read_table(PULL / "queries" / "queries-00000-of-00001.parquet").to_pylist()
    qrels = pq.read_table(PULL / "data" / "test-00000-of-00001.parquet").to_pylist()

    rel_of: dict[str, str] = {}
    gated = 0
    for row in corpus:
        rel = Path(row.get("partition") or "unknown") / f"{row['_id']}.py"
        dest = CORPUS / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = (row.get("text") or "").encode("utf-8", errors="replace")
        dest.write_bytes(data)
        rel_of[row["_id"]] = rel.as_posix()
        if len(data) < 50 or len(data) > 200_000:
            gated += 1
    log.info("corpus: %d snippets written (%d outside the 50..200000-byte gate) -> %s",
             len(rel_of), gated, CORPUS)

    qtext = {q["_id"]: q.get("text") or "" for q in queries}
    gold_ids: dict[str, list[str]] = defaultdict(list)
    for r in qrels:
        if (r.get("score") or 0) > 0:
            gold_ids[r["query-id"]].append(r["corpus-id"])
    needles, gold, drops = [], {}, {"gold_absent": [], "no_positive_qrel": 0}
    for qid in sorted(gold_ids, key=lambda s: int(s.lstrip("q"))):
        rels = sorted({rel_of[d] for d in gold_ids[qid] if d in rel_of})
        name = f"cosqa_{qid}"
        if not rels:
            drops["gold_absent"].append(name)
            continue
        needles.append({"name": name, "query": qtext.get(qid, ""), "question_type": "cosqa_web_query"})
        gold[name] = rels
    drops["no_positive_qrel"] = len({r["query-id"] for r in qrels}) - len(gold_ids)

    BENCH.mkdir(parents=True, exist_ok=True)
    (BENCH / "needles_cosqa.json").write_text(
        json.dumps({"needles": needles}, indent=1, ensure_ascii=False), encoding="utf-8")
    (BENCH / "gold_paths_cosqa.json").write_text(json.dumps(gold, indent=1), encoding="utf-8")
    (BENCH / "needles_cosqa_meta.json").write_text(json.dumps({
        "source": "hf:CoIR-Retrieval/cosqa (corpus all partitions; test qrels)",
        "corpus_root": str(CORPUS), "docs": len(rel_of), "docs_size_gated_by_builder": gated,
        "n_needles": len(needles), "gold_per_needle_max": max((len(v) for v in gold.values()), default=0),
        "drops": drops,
    }, indent=1), encoding="utf-8")
    log.info("needles: %d (drops %s)", len(needles), {k: (len(v) if isinstance(v, list) else v) for k, v in drops.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

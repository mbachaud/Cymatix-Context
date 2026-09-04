"""Emit the CodeRAG-Bench retrieval corpora + needle sets as files.

CodeRAG-Bench (arXiv 2406.14497, CC-BY-SA-4.0) ships two retrieval corpora
that pair with four query sets. Both are emitted here as one file per
document so ``scripts/build_fixture_matrix.py`` can ingest them like every
other bench corpus (tagger version + ingest_c recorded in the bed manifest):

  corpus_solutions  hf:code-rag-bench/programming-solutions (1,128 canonical
                    solutions; ``meta.task_name`` / ``meta.task_id``) →
                    ``<root>/<task_name>/<task_id>.py``.
                    Queries: humaneval (164, query = prompt) and mbpp (500,
                    query = text). One gold document per query — the task's
                    own canonical solution.
  corpus_docs       hf:code-rag-bench/library-documentation (34,003 library
                    pages; ``doc_id`` / ``doc_content``) →
                    ``<root>/<library>/<doc_id>.txt``.
                    Queries: ds1000 (query = prompt) and odex (query = the
                    NL ``intent``), restricted to the rows that carry
                    canonical ``docs`` (513 and 201), gold = those docs'
                    titles joined to ``doc_id``.

Query text is passed through verbatim (the paper retrieves on the full
prompt); the ladder records a query that raises inside the store as a miss
carrying ``error``, so over-long prompts price a defect rather than aborting
the run.

Outputs (per bench dir ``benchmarks/dogfood/coderag_solutions`` and
``benchmarks/dogfood/coderag_docs``):
  needles_<tag>.json       {"needles": [{name, query, question_type}]}
  gold_paths_<tag>.json    needle -> [corpus-relative paths]
  needles_<tag>_meta.json  source, counts, drops

Usage:
  python scripts/build_coderag_corpus.py solutions
  python scripts/build_coderag_corpus.py docs
  python scripts/build_coderag_corpus.py all
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

log = logging.getLogger("build_coderag_corpus")

PULLS = Path(r"F:/Projects/bench_pulls")
SOL_CORPUS = Path(r"F:/Projects/coderag/corpus_solutions")
DOC_CORPUS = Path(r"F:/Projects/coderag/corpus_docs")
SOL_BENCH = Path("benchmarks/dogfood/coderag_solutions")
DOC_BENCH = Path("benchmarks/dogfood/coderag_docs")

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe(name: str) -> str:
    s = _BAD.sub("_", name).strip(". ")
    return s[:150] or "_"


def _jl(path: Path) -> list:
    txt = path.read_text(encoding="utf-8")
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return [json.loads(line) for line in txt.splitlines() if line.strip()]


def _write(root: Path, rel: Path, text: str) -> int:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8", errors="replace")
    dest.write_bytes(data)
    return len(data)


def emit_solutions() -> None:
    from datasets import load_dataset  # cached offline copies exist

    ds = load_dataset("code-rag-bench/programming-solutions", split="train")
    doc_rel: dict[str, str] = {}
    sizes: dict[str, int] = {}
    dup = 0
    for row in ds:
        meta = row.get("meta") or {}
        did = f"{meta.get('task_name') or ''}:{meta.get('task_id') or ''}"
        if did in doc_rel:
            dup += 1
            continue
        rel = Path(safe(meta.get("task_name") or "unknown")) / f"{safe(str(meta.get('task_id')))}.py"
        sizes[did] = _write(SOL_CORPUS, rel, row.get("text") or "")
        doc_rel[did] = rel.as_posix()
    log.info("solutions corpus: %d docs written (%d duplicate ids skipped) -> %s",
             len(doc_rel), dup, SOL_CORPUS)

    needles, gold, drops = [], {}, {"gold_doc_absent": []}
    he = load_dataset("code-rag-bench/humaneval", split="train")
    for ex in he:
        did = f"humaneval:{ex.get('task_id', '')}"
        name = f"coderag_he_{safe(str(ex.get('task_id', ''))).replace('HumanEval_', '')}"
        if did not in doc_rel:
            drops["gold_doc_absent"].append(name)
            continue
        needles.append({"name": name, "query": ex.get("prompt") or "",
                        "question_type": "coderag_humaneval"})
        gold[name] = [doc_rel[did]]
    mb = load_dataset("code-rag-bench/mbpp", split="train")
    for ex in mb:
        did = f"mbpp:{ex.get('task_id', '')}"
        name = f"coderag_mbpp_{ex.get('task_id', '')}"
        if did not in doc_rel:
            drops["gold_doc_absent"].append(name)
            continue
        needles.append({"name": name, "query": ex.get("text") or "",
                        "question_type": "coderag_mbpp"})
        gold[name] = [doc_rel[did]]
    gated = sum(1 for s in sizes.values() if s < 50 or s > 200_000)
    SOL_BENCH.mkdir(parents=True, exist_ok=True)
    (SOL_BENCH / "needles_coderag_solutions.json").write_text(
        json.dumps({"needles": needles}, indent=1, ensure_ascii=False), encoding="utf-8")
    (SOL_BENCH / "gold_paths_coderag_solutions.json").write_text(
        json.dumps(gold, indent=1), encoding="utf-8")
    (SOL_BENCH / "needles_coderag_solutions_meta.json").write_text(json.dumps({
        "source": "hf:code-rag-bench/programming-solutions + humaneval + mbpp (CC-BY-SA-4.0)",
        "corpus_root": str(SOL_CORPUS), "docs": len(doc_rel), "docs_size_gated_by_builder": gated,
        "n_needles": len(needles),
        "by_type": {"coderag_humaneval": sum(n["question_type"] == "coderag_humaneval" for n in needles),
                    "coderag_mbpp": sum(n["question_type"] == "coderag_mbpp" for n in needles)},
        "query": "humaneval = prompt verbatim; mbpp = text verbatim", "drops": drops,
    }, indent=1), encoding="utf-8")
    log.info("solutions needles: %d (drops %s; %d docs outside the 50..200000-byte gate)",
             len(needles), {k: len(v) for k, v in drops.items()}, gated)


def emit_docs() -> None:
    lib = _jl(PULLS / "library-documentation" / "library_docs.json")
    doc_rel: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for d in lib:
        did = d["doc_id"]
        if did in doc_rel:
            continue
        lib_name = safe(did.split(".", 1)[0])
        rel = Path(lib_name) / f"{safe(did)}.txt"
        sizes[did] = _write(DOC_CORPUS, rel, d.get("doc_content") or "")
        doc_rel[did] = rel.as_posix()
    log.info("docs corpus: %d docs written -> %s", len(doc_rel), DOC_CORPUS)

    needles, gold, drops = [], {}, {"no_canonical_docs": 0, "gold_doc_absent": []}
    for tag, fname, qkey in (("ds1000", "ds1000.json", "prompt"), ("odex", "odex.json", "intent")):
        rows = _jl(PULLS / tag / fname)
        for i, r in enumerate(rows):
            docs = r.get("docs") or []
            if not docs:
                drops["no_canonical_docs"] += 1
                continue
            name = f"coderag_{tag}_{i:04d}"
            rels = sorted({doc_rel[d["title"]] for d in docs if d.get("title") in doc_rel})
            if not rels:
                drops["gold_doc_absent"].append(name)
                continue
            needles.append({"name": name, "query": r.get(qkey) or "",
                            "question_type": f"coderag_{tag}"})
            gold[name] = rels
    gated = sum(1 for s in sizes.values() if s < 50 or s > 200_000)
    DOC_BENCH.mkdir(parents=True, exist_ok=True)
    (DOC_BENCH / "needles_coderag_docs.json").write_text(
        json.dumps({"needles": needles}, indent=1, ensure_ascii=False), encoding="utf-8")
    (DOC_BENCH / "gold_paths_coderag_docs.json").write_text(
        json.dumps(gold, indent=1), encoding="utf-8")
    (DOC_BENCH / "needles_coderag_docs_meta.json").write_text(json.dumps({
        "source": "hf:code-rag-bench/library-documentation + ds1000 + odex (CC-BY-SA-4.0)",
        "corpus_root": str(DOC_CORPUS), "docs": len(doc_rel), "docs_size_gated_by_builder": gated,
        "n_needles": len(needles),
        "by_type": {"coderag_ds1000": sum(n["question_type"] == "coderag_ds1000" for n in needles),
                    "coderag_odex": sum(n["question_type"] == "coderag_odex" for n in needles)},
        "query": "ds1000 = prompt verbatim; odex = intent (NL) verbatim; only rows with canonical docs",
        "drops": drops,
    }, indent=1), encoding="utf-8")
    log.info("docs needles: %d (drops %s; %d docs outside the size gate)",
             len(needles), {k: (len(v) if isinstance(v, list) else v) for k, v in drops.items()}, gated)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stage", choices=["solutions", "docs", "all"])
    args = ap.parse_args()
    if args.stage in ("solutions", "all"):
        emit_solutions()
    if args.stage in ("docs", "all"):
        emit_docs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

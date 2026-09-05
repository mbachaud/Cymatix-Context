"""Document-level BM25 foil over a file-emitted bench corpus.

Same tokenizer and floored-IDF Okapi (k1 = 1.5, b = 0.75) as
``benchmarks/coderag_bench.py``'s ``BM25``, rebuilt on an inverted index so a
30k-document corpus scores 700 queries in seconds. Documents are the files
under ``--corpus-root`` that pass the builder's 50..200,000-byte gate (the same
haystack the bed ingested); gold comes from ``gold_paths_<tag>.json`` and the
query from ``needles_<tag>.json`` (the emitted set, not the resolved one, so a
needle the bed dropped is still scored here and reported separately).

Byte-identical files are collapsed to one document (``--dedup``, default on)
because the bed's content-hash dedup does the same; a gold path whose bytes
match a surviving twin is credited through the twin.

Metrics: NDCG@10 (single relevant: first gold), recall@1/5/10/12, MRR, and the
same per-type split as the ladder receipts. Writes one JSON receipt.

Usage:
  python -P benchmarks/dogfood/code/bm25_doc_foil.py --tag cosqa \\
      --bench-dir benchmarks/dogfood/cosqa --corpus-root F:/Projects/cosqa/corpus \\
      --out benchmarks/dogfood/cosqa/receipts/bm25_doc_foil_cosqa_2026-09-04.json
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import re
import time
from pathlib import Path

_TOK = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
MIN_BYTES, MAX_BYTES = 50, 200_000
# Mirror scripts/build_fixture_matrix.py INGEST_EXTS so the foil's haystack is the bed's.
INGEST_EXTS = {".txt", ".md", ".cfg", ".ini", ".conf", ".properties", ".vdf", ".acf",
               ".lua", ".py", ".cs", ".js", ".json", ".yaml", ".yml", ".toml",
               ".bat", ".sh", ".html", ".rs", ".go", ".java", ".c", ".cpp", ".h",
               ".rb", ".ts", ".tsx", ".jsx", ".sql", ".r", ".ps1"}


def tok(s: str) -> list[str]:
    return _TOK.findall(s or "")


class InvertedBM25:
    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.N = len(docs_tokens)
        self.dl = [len(d) for d in docs_tokens]
        self.avgdl = (sum(self.dl) / self.N) if self.N else 0.0
        post: dict[str, list[tuple[int, int]]] = collections.defaultdict(list)
        for i, d in enumerate(docs_tokens):
            f = collections.Counter(d)
            for t, c in f.items():
                post[t].append((i, c))
        self.post = post
        self.idf = {t: max(0.0, math.log((self.N - len(p) + 0.5) / (len(p) + 0.5) + 1.0))
                    for t, p in post.items()}

    def scores(self, q_tokens: list[str]) -> dict[int, float]:
        out: dict[int, float] = collections.defaultdict(float)
        for t in set(q_tokens):
            idf = self.idf.get(t)
            if not idf:
                continue
            for i, ft in self.post[t]:
                denom = self.k1 * (1 - self.b + self.b * self.dl[i] / self.avgdl) if self.avgdl else self.k1
                out[i] += idf * (ft * (self.k1 + 1)) / (ft + denom)
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--bench-dir", required=True)
    ap.add_argument("--corpus-root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-dedup", action="store_true")
    args = ap.parse_args()
    bench, root = Path(args.bench_dir), Path(args.corpus_root)
    needles = json.loads((bench / f"needles_{args.tag}.json").read_text(encoding="utf-8"))["needles"]
    gold_paths = json.loads((bench / f"gold_paths_{args.tag}.json").read_text(encoding="utf-8"))

    t0 = time.time()
    files, gated, ext_skipped = [], 0, 0
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix.lower() not in INGEST_EXTS:
            ext_skipped += 1
            continue
        sz = f.stat().st_size
        if sz < MIN_BYTES or sz > MAX_BYTES:
            gated += 1
            continue
        files.append(f)
    rel_of = {f: f.relative_to(root).as_posix() for f in files}
    doc_of_rel: dict[str, int] = {}
    docs_tokens: list[list[str]] = []
    seen: dict[str, int] = {}
    collisions = 0
    for f in files:
        data = f.read_bytes()
        key = hashlib.sha256(data).hexdigest() if not args.no_dedup else rel_of[f]
        if key in seen:
            doc_of_rel[rel_of[f]] = seen[key]
            collisions += 1
            continue
        seen[key] = len(docs_tokens)
        doc_of_rel[rel_of[f]] = len(docs_tokens)
        docs_tokens.append(tok(data.decode("utf-8", errors="replace")))
    bm = InvertedBM25(docs_tokens)
    build_s = time.time() - t0

    per_type = collections.defaultdict(lambda: {"n": 0, "ndcg10": 0.0, "r1": 0, "r5": 0, "r10": 0, "r12": 0, "mrr": 0.0, "gold_absent": 0})
    rows = []
    for nd in needles:
        qt = nd.get("question_type", "?")
        g = per_type[qt]
        gold_docs = {doc_of_rel[p] for p in gold_paths.get(nd["name"], []) if p in doc_of_rel}
        if not gold_docs:
            g["gold_absent"] += 1
            rows.append({"needle": nd["name"], "rank": None, "gold_absent": True})
            continue
        sc = bm.scores(tok(nd["query"]))
        order = sorted(sc, key=lambda i: (-sc[i], i))
        rank = None
        for r, i in enumerate(order, 1):
            if i in gold_docs:
                rank = r
                break
        g["n"] += 1
        if rank:
            g["ndcg10"] += 1.0 / math.log2(rank + 1) if rank <= 10 else 0.0
            g["r1"] += rank <= 1
            g["r5"] += rank <= 5
            g["r10"] += rank <= 10
            g["r12"] += rank <= 12
            g["mrr"] += 1.0 / rank
        rows.append({"needle": nd["name"], "rank": rank})

    def finish(g: dict) -> dict:
        n = g["n"] or 1
        return {"n": g["n"], "gold_absent": g["gold_absent"], "ndcg@10": round(g["ndcg10"] / n, 4),
                "recall@1": round(g["r1"] / n, 4), "recall@5": round(g["r5"] / n, 4),
                "recall@10": round(g["r10"] / n, 4), "recall@12": round(g["r12"] / n, 4),
                "mrr": round(g["mrr"] / n, 4)}

    total = {"n": 0, "ndcg10": 0.0, "r1": 0, "r5": 0, "r10": 0, "r12": 0, "mrr": 0.0, "gold_absent": 0}
    for g in per_type.values():
        for k in total:
            total[k] += g[k]
    out = {
        "tool": "benchmarks/dogfood/code/bm25_doc_foil.py", "tag": args.tag,
        "corpus_root": str(root), "files_admitted": len(files), "files_size_gated": gated, "files_ext_skipped": ext_skipped,
        "documents_after_dedup": len(docs_tokens), "byte_duplicate_collisions": collisions,
        "dedup": not args.no_dedup, "bm25": {"k1": 1.5, "b": 0.75, "idf": "floored at 0", "tokenizer": _TOK.pattern},
        "query": "needles_<tag>.json query text verbatim", "gold": "gold_paths_<tag>.json (document level, first gold)",
        "build_s": round(build_s, 1), "total": finish(total),
        "by_type": {k: finish(v) for k, v in sorted(per_type.items())},
        "per_query": rows,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"{args.tag}: docs={len(docs_tokens)} (files {len(files)}, gated {gated}, ext-skipped {ext_skipped}, dup {collisions}) "
          f"total {out['total']}")
    for k, v in out["by_type"].items():
        print(f"  {k}: {v}")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

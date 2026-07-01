"""CodeRAG-Bench (Step-2) foils: random floor + BM25 baseline over the
programming-solutions corpus.

CodeRAG-Bench (arXiv 2406.14497, NAACL'25 Findings) native retrieval track.
8 datasets; this file covers the two canonical-solution datasets that share ONE
corpus (programming-solutions) and have the simplest gold-matching semantics:

  HumanEval (164 q) : query=prompt (signature+docstring) -> gold = its solution.
                       Gold shares surface tokens with query -> LEXICALLY FRIENDLY.
  MBPP     (~974 q)  : query=NL text (no signature)       -> gold = its solution.
                       Pure NL->code -> more SEMANTIC, the harder contrast.

Corpus = code-rag-bench/programming-solutions (text + meta{task_id,task_name}).
One gold/query. IDCG=1 for all queries, so NDCG@k = 1/log2(rank+1) if gold in
top-k else 0.

METRICS (primary = NDCG@10):
  - NDCG@10   -- primary metric per the CodeRAG-Bench paper
  - Recall@k  -- hit rate: 1 if gold in top-k else 0 (k=1,5,10)
  - Precision@k -- same as recall for a single-gold query: hit/k (= recall/k)

EFFICIENCY LAYER (injected-token cost estimate):
  - median / p90 of token_estimate (whitespace-split words * 1.3, rounded).
  - Measures how many tokens the top-10 results would inject into context.
  - This is the cheap-vs-7B-dense efficiency argument from the strategy doc.

ARMS:
  A -- random floor (deterministic per-query hash)
  B -- BM25 (floored-IDF Okapi, same identifier tokenizer; the classic foil)

Writes:
  benchmarks/results/coderag_foils_{timestamp}.json   -- summary
  benchmarks/results/coderag_queries_{timestamp}.json -- per-query dump for
    the Helix arm (coderag_bench_helix.py) to consume with the same examples.

License: CC-BY-SA-4.0 (ShareAlike copyleft, internal measurement OK; verify
before any redistributed/public claim on processed data).

LLM-free, GPU-free.

CLI:
  python benchmarks/coderag_bench.py [--limit N] [--datasets humaneval,mbpp]
  python benchmarks/coderag_bench.py --limit 50 --datasets humaneval
  python benchmarks/coderag_bench.py --out benchmarks/results/my_foils.json
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import re
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# Identifier tokenizer (same as the Helix lexical probe).
_TOK = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

# k-values to evaluate (NDCG@10 is primary; recall/prec at 1, 5, 10).
KS = (1, 5, 10)


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

def tok(s):
    """Lowercase identifier tokenizer used for BM25 and overlap scoring."""
    return _TOK.findall((s or "").lower())


# ---------------------------------------------------------------------------
# Dataset loading (isolated so tests can mock/skip)
# ---------------------------------------------------------------------------

def load_corpus():
    """Load the programming-solutions corpus from HuggingFace.

    Returns (corpus, doc_index) where:
      corpus    -- list of {"doc_id": str, "text": str}
      doc_index -- {doc_id: corpus_index}
    """
    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: 'datasets' not installed. pip install datasets", file=sys.stderr)
        sys.exit(1)

    ds = load_dataset("code-rag-bench/programming-solutions", split="train")
    corpus = []
    doc_index = {}
    for row in ds:
        meta = row.get("meta") or {}
        task_name = meta.get("task_name") or ""
        task_id = meta.get("task_id") or ""
        did = f"{task_name}:{task_id}"
        if did in doc_index:
            continue
        doc_index[did] = len(corpus)
        corpus.append({"doc_id": did, "text": row.get("text") or ""})
    return corpus, doc_index


def load_queries(datasets):
    """Load query sets from HuggingFace for the requested dataset names."""
    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: 'datasets' not installed. pip install datasets", file=sys.stderr)
        sys.exit(1)

    queries = []
    if "humaneval" in datasets:
        he = load_dataset("code-rag-bench/humaneval", split="train")
        for ex in he:
            queries.append({
                "ds": "humaneval",
                "query": ex.get("prompt") or "",
                "gold": "humaneval:{}".format(ex.get("task_id", "")),
            })

    if "mbpp" in datasets:
        mb = load_dataset("code-rag-bench/mbpp", split="train")
        for ex in mb:
            queries.append({
                "ds": "mbpp",
                "query": ex.get("text") or "",
                "gold": "mbpp:{}".format(ex.get("task_id", "")),
            })

    return queries


# ---------------------------------------------------------------------------
# BM25 (floored-IDF Okapi, pure Python)
# ---------------------------------------------------------------------------

class BM25:
    """Okapi BM25 with IDF floored at 0 (avoids negative IDF on small corpora).

    Accepts pre-tokenised document lists for easy unit testing.
    """

    def __init__(self, corpus_tokens, k1=1.5, b=0.75):
        self.k1 = k1
        self.b = b
        self.N = len(corpus_tokens)
        self.dl = [len(d) for d in corpus_tokens]
        self.avgdl = (sum(self.dl) / self.N) if self.N else 0.0

        df = collections.defaultdict(int)
        self.tf = []
        for d in corpus_tokens:
            f = collections.defaultdict(int)
            for t in d:
                f[t] += 1
            self.tf.append(f)
            for t in f:
                df[t] += 1

        self.idf = {
            t: max(0.0, math.log((self.N - n + 0.5) / (n + 0.5) + 1.0))
            for t, n in df.items()
        }

    def scores(self, q_tokens):
        """Return BM25 score for each document."""
        qset = set(q_tokens)
        out = [0.0] * self.N
        for i in range(self.N):
            f = self.tf[i]
            denom = (
                self.k1 * (1 - self.b + self.b * self.dl[i] / self.avgdl)
                if self.avgdl else self.k1
            )
            s = 0.0
            for t in qset:
                ft = f.get(t, 0)
                if ft:
                    s += self.idf.get(t, 0.0) * (ft * (self.k1 + 1)) / (ft + denom)
            out[i] = s
        return out

    def rank_gold_pos(self, q_tokens, gold_idx):
        """Return 0-based rank position of gold_idx when ranked by BM25 desc."""
        sc = self.scores(q_tokens)
        order = sorted(range(self.N), key=lambda i: (-sc[i], i))
        return order.index(gold_idx)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def ndcg_at(pos0, k):
    """Single-gold NDCG@k from 0-based rank position. IDCG = 1.

    ndcg = 1/log2(rank+1) if rank <= k (1-based), else 0.
    """
    r = pos0 + 1
    return (1.0 / math.log2(r + 1)) if r <= k else 0.0


def recall_at(pos0, k):
    """Recall@k for a single-gold query: 1.0 if in top-k else 0.0."""
    return 1.0 if (pos0 + 1) <= k else 0.0


def precision_at(pos0, k):
    """Precision@k for a single-gold query: (1/k) if in top-k else 0.0."""
    return (1.0 / k) if (pos0 + 1) <= k else 0.0


# ---------------------------------------------------------------------------
# Efficiency layer
# ---------------------------------------------------------------------------

def token_estimate(texts):
    """Rough token estimate for a list of text strings.

    Whitespace-split word count * 1.3, rounded. Used to estimate injected
    tokens if the top-k documents were included in context verbatim.
    """
    words = sum(len((t or "").split()) for t in texts)
    return round(words * 1.3)


def _percentile(values, pct):
    """Return pct-th percentile (0-100) of values; empty -> 0."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def efficiency_stats(token_counts):
    """Return median and p90 injected token estimates."""
    return {
        "median_injected_tokens": round(_percentile(token_counts, 50), 1),
        "p90_injected_tokens": round(_percentile(token_counts, 90), 1),
    }


# ---------------------------------------------------------------------------
# Core scoring
# ---------------------------------------------------------------------------

def _make_random_pos(gold_id, qi, n_corpus):
    """Deterministic per-query random rank position (floor arm A)."""
    return hash((gold_id, qi)) % n_corpus


def run(corpus, doc_index, queries, limit=0):
    """Score the foil arms (random + BM25) and return (summary, per_query_rows).

    Parameters
    ----------
    corpus     : list of {"doc_id", "text"}
    doc_index  : {doc_id: corpus_index}
    queries    : list of {"ds", "query", "gold"}
    limit      : cap per-dataset (0 = all)

    Returns
    -------
    summary        : per-dataset metric dict
    per_query_rows : per-query data (consumed by coderag_bench_helix.py)
    """
    # Resolve gold indices; drop unresolvable.
    resolved = []
    miss = 0

    for q in queries:
        if q["gold"] in doc_index:
            q = dict(q)
            q["gold_idx"] = doc_index[q["gold"]]
            resolved.append(q)
        else:
            miss += 1

    if miss:
        print("[coderag_bench] {} queries dropped (gold not in corpus)".format(miss),
              flush=True)

    # Apply per-dataset limit.
    if limit:
        per_ds = collections.defaultdict(list)
        for q in resolved:
            per_ds[q["ds"]].append(q)
        resolved = []
        for ds_rows in per_ds.values():
            resolved.extend(ds_rows[:limit])

    n_corpus = len(corpus)
    print("[coderag_bench] corpus={} docs, queries={} resolved".format(
          n_corpus, len(resolved)), flush=True)

    bm = BM25([tok(c["text"]) for c in corpus])

    def _zero_agg():
        a = {"n": 0, "bm25_ndcg10": 0.0, "rand_ndcg10": 0.0,
             "bm25_top10_tokens": [], "rand_top10_tokens": []}
        for k in KS:
            a["bm25_recall@{}".format(k)] = 0.0
            a["rand_recall@{}".format(k)] = 0.0
            a["bm25_precision@{}".format(k)] = 0.0
            a["rand_precision@{}".format(k)] = 0.0
        return a

    agg = collections.defaultdict(_zero_agg)
    per_query_rows = []

    for qi, q in enumerate(resolved):
        ds = q["ds"]
        gi = q["gold_idx"]
        qt = tok(q["query"])

        bm25_pos = bm.rank_gold_pos(qt, gi)
        rand_pos = _make_random_pos(q["gold"], qi, n_corpus)

        a = agg[ds]
        a["n"] += 1
        a["bm25_ndcg10"] += ndcg_at(bm25_pos, 10)
        a["rand_ndcg10"] += ndcg_at(rand_pos, 10)
        for k in KS:
            a["bm25_recall@{}".format(k)] += recall_at(bm25_pos, k)
            a["rand_recall@{}".format(k)] += recall_at(rand_pos, k)
            a["bm25_precision@{}".format(k)] += precision_at(bm25_pos, k)
            a["rand_precision@{}".format(k)] += precision_at(rand_pos, k)

        bm25_sc = bm.scores(qt)
        bm25_order = sorted(range(n_corpus), key=lambda i: (-bm25_sc[i], i))
        bm25_top10_texts = [corpus[i]["text"] for i in bm25_order[:10]]
        a["bm25_top10_tokens"].append(float(token_estimate(bm25_top10_texts)))

        rand_order = sorted(range(n_corpus), key=lambda i: hash((q["gold"], qi, i)))
        rand_top10_texts = [corpus[i]["text"] for i in rand_order[:10]]
        a["rand_top10_tokens"].append(float(token_estimate(rand_top10_texts)))

        per_query_rows.append({
            "ds": ds,
            "query": q["query"],
            "gold": q["gold"],
            "gold_idx": gi,
            "bm25_rank": bm25_pos,
        })

    summary = {}
    for ds, a in sorted(agg.items()):
        n = a["n"]
        if n == 0:
            continue
        eff_bm25 = efficiency_stats(a["bm25_top10_tokens"])
        eff_rand = efficiency_stats(a["rand_top10_tokens"])
        row = {
            "n": n,
            "corpus": n_corpus,
            "bm25_ndcg@10": round(a["bm25_ndcg10"] / n, 4),
            "rand_ndcg@10": round(a["rand_ndcg10"] / n, 4),
            "bm25_efficiency": eff_bm25,
            "rand_efficiency": eff_rand,
        }
        for k in KS:
            row["bm25_recall@{}".format(k)] = round(a["bm25_recall@{}".format(k)] / n, 4)
            row["rand_recall@{}".format(k)] = round(a["rand_recall@{}".format(k)] / n, 4)
            row["bm25_precision@{}".format(k)] = round(a["bm25_precision@{}".format(k)] / n, 4)
            row["rand_precision@{}".format(k)] = round(a["rand_precision@{}".format(k)] / n, 4)
        summary[ds] = row

    return summary, per_query_rows


def _print_table(summary):
    header = (
        "{:<12} {:>5} {:>8}  {:>9} {:>7} {:>7} {:>7}  {:>8} {:>8}".format(
            "dataset", "n", "corpus", "ndcg@10", "r@1", "r@5", "r@10", "med_tok", "p90_tok"
        )
    )
    print("\n" + header)
    print("-" * len(header))
    for ds, s in summary.items():
        eff = s.get("bm25_efficiency", {})
        print("{:<12} {:>5} {:>8}  {:>9.4f} {:>7.4f} {:>7.4f} {:>7.4f}  {:>8.0f} {:>8.0f}".format(
            "BM25:" + ds, s["n"], s["corpus"],
            s["bm25_ndcg@10"], s["bm25_recall@1"], s["bm25_recall@5"], s["bm25_recall@10"],
            eff.get("median_injected_tokens", 0), eff.get("p90_injected_tokens", 0),
        ))
        eff_r = s.get("rand_efficiency", {})
        print("{:<12} {:>5} {:>8}  {:>9.4f} {:>7.4f} {:>7.4f} {:>7.4f}  {:>8.0f} {:>8.0f}".format(
            "rand:" + ds, s["n"], s["corpus"],
            s["rand_ndcg@10"], s["rand_recall@1"], s["rand_recall@5"], s["rand_recall@10"],
            eff_r.get("median_injected_tokens", 0), eff_r.get("p90_injected_tokens", 0),
        ))


def main():
    ap = argparse.ArgumentParser(
        description=(
            "CodeRAG-Bench (Step-2) foils: random + BM25 over the "
            "programming-solutions corpus. Writes per-query dump for the Helix arm."
        )
    )
    ap.add_argument(
        "--datasets", default="humaneval,mbpp",
        help="Comma-separated dataset names (default: 'humaneval,mbpp'). Valid: humaneval, mbpp.",
    )
    ap.add_argument(
        "--limit", type=int, default=0,
        help="Cap queries per dataset (0 = all). Useful for smoke runs.",
    )
    ap.add_argument(
        "--out", default=None,
        help="Override path for the summary JSON (default: benchmarks/results/coderag_foils_{ts}.json)",
    )
    ap.add_argument(
        "--queries-out", default=None, dest="queries_out",
        help="Override path for the per-query dump (default: benchmarks/results/coderag_queries_{ts}.json)",
    )
    args = ap.parse_args()

    datasets = [d.strip().lower() for d in args.datasets.split(",") if d.strip()]
    valid = {"humaneval", "mbpp"}
    bad = set(datasets) - valid
    if bad:
        print("ERROR: unknown datasets: {}. Valid: {}".format(bad, valid), file=sys.stderr)
        sys.exit(1)

    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    print("[coderag_bench] Loading corpus...", flush=True)
    corpus, doc_index = load_corpus()
    print("[coderag_bench] corpus: {} docs".format(len(corpus)), flush=True)

    print("[coderag_bench] Loading queries: {}...".format(datasets), flush=True)
    queries = load_queries(datasets)
    print("[coderag_bench] queries loaded: {}".format(len(queries)), flush=True)

    summary, per_query_rows = run(corpus=corpus, doc_index=doc_index,
                                   queries=queries, limit=args.limit)
    _print_table(summary)

    out_path = Path(args.out) if args.out else RESULTS_DIR / "coderag_foils_{}.json".format(ts)
    queries_path = (Path(args.queries_out) if args.queries_out
                    else RESULTS_DIR / "coderag_queries_{}.json".format(ts))

    result_blob = {
        "benchmark": "coderag_bench",
        "timestamp": ts,
        "datasets": datasets,
        "limit": args.limit,
        "license": "CC-BY-SA-4.0 (internal measurement; verify before public claim)",
        "metrics": "NDCG@10 (primary), Recall@{1,5,10}, Precision@{1,5,10}",
        "efficiency": "median/p90 injected tokens from top-10 docs",
        "summary": summary,
    }

    out_path.write_text(json.dumps(result_blob, indent=2), encoding="utf-8")
    queries_path.write_text(json.dumps(per_query_rows, ensure_ascii=False), encoding="utf-8")

    print("\n-> summary:        {}".format(out_path))
    print("-> per-query dump: {}".format(queries_path))
    print("   (Helix arm: python benchmarks/coderag_bench_helix.py --queries {})".format(
          queries_path))


if __name__ == "__main__":
    main()

"""CodeRAG-Bench (Step-2) Helix diagnostic: why does Helix miss lexically-obvious
gold docs on the canonical programming-solutions corpus?

For each sampled query, reports whether the gold doc is:
  (a) absent from Helix's scored set (gating/shortlist excludes it), or
  (b) present but out-ranked (additive fusion / IDF buries it).

Also reports:
  - query token count and overlap with gold doc (lexical sanity check)
  - top-3 retrieved (idx, score)
  - BM25 rank for the same query (reference foil)

USAGE (requires in-process Helix -- run in helix063 venv DIRECTLY):
  python benchmarks/coderag_diag.py --n 12 --ds humaneval
  python benchmarks/coderag_diag.py --n 20 --ds mbpp --queries-json benchmarks/results/coderag_queries_<ts>.json

Reads per-query dump from benchmarks/results/coderag_queries_*.json (or
--queries-json override). Falls back to a tiny inline fixture for smoke-testing.

LLM-free, GPU-free (lexical probe config).
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import re
import shutil
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"

_TOK = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def tok(s):
    return set(_TOK.findall((s or "").lower()))


# ---------------------------------------------------------------------------
# Inline smoke-test fixture (used when no dump exists)
# ---------------------------------------------------------------------------

def _smoke_corpus():
    return [
        {"doc_id": "humaneval:0",
         "text": "def has_close_elements(numbers, threshold):\n"
                 "    for i, a in enumerate(numbers):\n"
                 "        for j, b in enumerate(numbers):\n"
                 "            if i != j and abs(a-b) < threshold:\n"
                 "                return True\n"
                 "    return False\n"},
        {"doc_id": "humaneval:1",
         "text": "def separate_paren_groups(paren_string):\n"
                 "    result = []\n"
                 "    current = []\n"
                 "    depth = 0\n"
                 "    for c in paren_string:\n"
                 "        if c == '(': depth += 1; current.append(c)\n"
                 "        elif c == ')': depth -= 1; current.append(c)\n"
                 "        if depth == 0 and current:\n"
                 "            result.append(''.join(current))\n"
                 "            current = []\n"
                 "    return result\n"},
        {"doc_id": "humaneval:2",
         "text": "def truncate_number(number):\n    return number % 1.0\n"},
    ]


def _smoke_queries():
    return [
        {
            "ds": "humaneval",
            "query": ("def has_close_elements(numbers, threshold):\n"
                      '    """Check if any two numbers are closer than threshold."""\n'),
            "gold": "humaneval:0",
            "gold_idx": 0,
        },
        {
            "ds": "humaneval",
            "query": ("def separate_paren_groups(paren_string):\n"
                      '    """Input: string of nested parentheses groups."""\n'),
            "gold": "humaneval:1",
            "gold_idx": 1,
        },
    ]


# ---------------------------------------------------------------------------
# In-process Helix wiring
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_CANDIDATES = [
    Path(__file__).resolve().parents[1] / "docs" / "benchmarks" / "helix_probe_lexical.toml",
    Path("F:/tmp/cb_helix_probe/helix_probe.toml"),
]


def _find_default_config():
    for p in _DEFAULT_CONFIG_CANDIDATES:
        if Path(p).exists():
            return str(p)
    return None


def build_helix(genome_dir, config_path):
    """Return a HelixContextManager with a fresh genome at genome_dir."""
    os.environ.pop("HELIX_USE_SHARDS", None)
    os.environ["HELIX_CONFIG"] = config_path
    os.environ["HELIX_GENOME_PATH"] = os.path.join(genome_dir, "genome.db")
    shutil.rmtree(genome_dir, ignore_errors=True)
    os.makedirs(genome_dir, exist_ok=True)
    from helix_context.config import load_config
    from helix_context.context_manager import HelixContextManager
    return HelixContextManager(load_config())


def _gene_doc_idx(g):
    """Extract corpus index from gene source_id / metadata path "doc_{idx}"."""
    src = getattr(g, "source_id", None)
    if not src and getattr(g, "promoter", None) and g.promoter.metadata:
        src = g.promoter.metadata.get("path")
    if src and str(src).startswith("doc_"):
        try:
            return int(str(src).split("doc_", 1)[1])
        except ValueError:
            return None
    return None


def helix_scores(helix, query, n_corpus):
    """Return {corpus_idx: best_score} for this query over the genome."""
    _eq, dom, ent = helix._prepare_query_signals(
        query, session_context=None, expand_query=False
    )
    cands = helix._retrieve(
        dom, ent, n_corpus,
        query_text=query, include_cold=None,
        party_id="default", use_harmonic=False, use_sr=False,
    )
    cands, _ = helix._apply_candidate_refiners(
        query, cands, n_corpus,
        use_cymatics=False, use_harmonic_bin=False, use_tcm=True, allow_rerank=False,
    )
    raw = dict(getattr(helix.genome, "last_query_scores", None) or {})
    best = {}
    for g in cands:
        di = _gene_doc_idx(g)
        if di is None:
            continue
        s = raw.get(g.gene_id, 0.0)
        if di not in best or s > best[di]:
            best[di] = s
    return best


# ---------------------------------------------------------------------------
# BM25 reference
# ---------------------------------------------------------------------------

class _BM25:
    def __init__(self, corpus_tokens, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.N = len(corpus_tokens)
        self.dl = [len(d) for d in corpus_tokens]
        self.avgdl = sum(self.dl) / self.N if self.N else 0.0
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

    def rank_pos(self, q_tokens, gold_idx):
        qset = set(q_tokens)
        sc = [0.0] * self.N
        for i in range(self.N):
            f = self.tf[i]
            denom = (self.k1 * (1 - self.b + self.b * self.dl[i] / self.avgdl)
                     if self.avgdl else self.k1)
            s = 0.0
            for t in qset:
                ft = f.get(t, 0)
                if ft:
                    s += self.idf.get(t, 0.0) * (ft * (self.k1 + 1)) / (ft + denom)
            sc[i] = s
        order = sorted(range(self.N), key=lambda i: (-sc[i], i))
        return order.index(gold_idx)


# ---------------------------------------------------------------------------
# Main diagnostic loop
# ---------------------------------------------------------------------------

def diagnose(corpus, queries, helix, n=12, ds_filter=None):
    """Run per-query diagnostics; return list of result dicts."""
    filtered = [q for q in queries if not ds_filter or q.get("ds") == ds_filter]
    if n:
        filtered = filtered[:n]

    n_corpus = len(corpus)
    corpus_texts = [c["text"] for c in corpus]
    bm = _BM25([list(tok(c["text"])) for c in corpus])

    results = []
    for q in filtered:
        gi = q["gold_idx"]
        query = q["query"]
        qt = list(tok(query))

        scores = helix_scores(helix, query, n_corpus)
        in_scored = gi in scores
        order = sorted(scores.keys(), key=lambda d: (-scores[d], d))
        gold_rank = order.index(gi) if in_scored else None
        overlap = len(tok(query) & tok(corpus_texts[gi]))
        qn = len(tok(query))
        bm25_rank = bm.rank_pos(qt, gi)
        top3 = [(d, round(scores[d], 4)) for d in order[:3]]

        row = {
            "gold": q["gold"],
            "retrieved_count": len(scores),
            "gold_in_scored": in_scored,
            "gold_rank": gold_rank,
            "gold_score": round(scores.get(gi, 0.0), 4),
            "query_tok_count": qn,
            "overlap_with_gold": overlap,
            "bm25_rank": bm25_rank,
            "top3": top3,
        }
        results.append(row)
        print(
            "{}: scored={} in_scored={} gold_rank={} gold_score={} "
            "| q_tok={} overlap={} | bm25_rank={} | top3={}".format(
                q["gold"], len(scores), in_scored, gold_rank,
                row["gold_score"], qn, overlap, bm25_rank, top3,
            ),
            flush=True,
        )
    return results


def main():
    ap = argparse.ArgumentParser(
        description=(
            "CodeRAG-Bench diagnostic: per-query retrieved-vs-not-retrieved "
            "for the first N queries. Requires in-process Helix (helix063 venv)."
        )
    )
    ap.add_argument("--n", type=int, default=12, help="Number of queries to diagnose")
    ap.add_argument(
        "--ds", default="humaneval", choices=["humaneval", "mbpp"],
        help="Dataset to focus on (default: humaneval)",
    )
    ap.add_argument(
        "--corpus-json", default=None, dest="corpus_json",
        help="Path to a pre-built corpus JSON (list of {doc_id, text}).",
    )
    ap.add_argument(
        "--queries-json", default=None, dest="queries_json",
        help="Path to the per-query dump JSON from coderag_bench.py.",
    )
    ap.add_argument(
        "--helix-config", default=None, dest="helix_config",
        help="Path to lexical-probe helix.toml. Falls back to HELIX_CONFIG env var.",
    )
    ap.add_argument(
        "--genome-dir", default=None, dest="genome_dir",
        help="Scratch dir for the temporary genome DB.",
    )
    args = ap.parse_args()

    helix_config = (
        args.helix_config
        or os.environ.get("HELIX_CONFIG")
        or _find_default_config()
    )
    if not helix_config or not Path(helix_config).exists():
        print(
            "ERROR: No helix config found. Provide --helix-config or set HELIX_CONFIG.\n"
            "  See docs/benchmarks/helix_probe_lexical.toml for a template.",
            file=sys.stderr,
        )
        sys.exit(1)

    genome_dir = args.genome_dir or os.path.join(
        os.environ.get("TEMP", "/tmp"), "coderag_diag_genome"
    )

    # Load corpus.
    if args.corpus_json and Path(args.corpus_json).exists():
        corpus = json.loads(Path(args.corpus_json).read_text(encoding="utf-8"))
        print("[diag] corpus from {}: {} docs".format(args.corpus_json, len(corpus)), flush=True)
    else:
        corpus = _smoke_corpus()
        print("[diag] Using inline smoke fixture corpus ({} docs)".format(len(corpus)), flush=True)

    # Load queries.
    if args.queries_json and Path(args.queries_json).exists():
        queries = json.loads(Path(args.queries_json).read_text(encoding="utf-8"))
        print("[diag] queries from {}: {}".format(args.queries_json, len(queries)), flush=True)
    else:
        candidates = sorted(RESULTS_DIR.glob("coderag_queries_*.json"))
        if candidates:
            queries = json.loads(candidates[-1].read_text(encoding="utf-8"))
            print("[diag] queries auto-loaded from {}: {}".format(
                  candidates[-1], len(queries)), flush=True)
        else:
            queries = _smoke_queries()
            print("[diag] Using inline smoke fixture queries ({} queries)".format(
                  len(queries)), flush=True)

    print("[diag] Building Helix genome at {}...".format(genome_dir), flush=True)
    helix = build_helix(genome_dir, helix_config)

    ing_err = 0
    for di, doc in enumerate(corpus):
        try:
            helix.ingest(doc["text"], content_type="code", metadata={"path": "doc_{}".format(di)})
        except Exception:
            ing_err += 1

    print("[diag] ingested {}/{} docs ({} errors)".format(
          len(corpus) - ing_err, len(corpus), ing_err), flush=True)
    print("\n[diag] Diagnosing first {} '{}' queries:\n".format(args.n, args.ds))

    diagnose(corpus, queries, helix, n=args.n, ds_filter=args.ds)


if __name__ == "__main__":
    main()

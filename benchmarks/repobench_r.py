"""RepoBench-R (Step-1) foils: random + Jaccard-overlap + BM25 candidate ranking.

RepoBench-R = given in-file `code` context, rank the cross-file `context` candidate
snippets; gold = `golden_snippet_index`. Metric = acc@k (acc@1/3 easy; acc@1/3/5 hard).
LLM-free, GPU-free. Run in cb-step0 venv (rank-bm25 + huggingface_hub).

Baselines:
  - random   : per-example floor (~1/avg_candidates).
  - overlap  : Jaccard token overlap -- the RepoBench paper's "lexical" baseline.
  - bm25     : BM25Okapi. NOTE its IDF goes NEGATIVE for terms appearing in >half the
               candidates, which on ~6-candidate sets penalises shared tokens and can
               sink below random. Kept to show why naive BM25 is the wrong foil here.
               The global-arm (repobench_r_helix_global.py) uses a floored-IDF variant.

Settings (--config):
  python_cff  = XF-F: cross-file, next-line prediction (file context comes from
                  other files in the same repo that are imported/used).
  python_cfr  = XF-R: cross-file, random snippet (harder; snippet is randomly sampled
                  from same-repo cross-file dependencies).
  java_cff    = XF-F setting for Java.
  java_cfr    = XF-R setting for Java.

HF dataset: tianyang/repobench-r (CC-BY-4.0). Data are gzipped pickle files; the
HF loader itself uses gzip+pickle (see note in load_split). Fetched via hf_hub_download
(content-addressed, cached). Source: arxiv.org/abs/2306.03091.

Writes:
  benchmarks/results/repobench_r_{config}_{level}_n{n}.json   per-example query/cand dump
  benchmarks/results/repobench_r_{config}_foils_{timestamp}.json  summary
  (Helix arm in repobench_r_helix.py reads the per-example json.)

CLI:
  python benchmarks/repobench_r.py --config python_cff --n 200
  python benchmarks/repobench_r.py --config python_cfr --n 200 --levels hard
  python benchmarks/repobench_r.py --config java_cff   --n 200
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import json
import os
import pickle
import re
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# Gold key name used in the dataset (both spellings appear across HF versions).
GOLD_KEY = "golden_snippet_index"
GOLD_KEY_ALT = "gold_snippet_index"

# Number of trailing lines from in-file code used as the query.
QUERY_TAIL_LINES = 30

_TOK = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def tok(s):
    return _TOK.findall(s or "")


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------

def load_split(config, split, level, n):
    """Load one (config, split, level) slice from HF.

    NOTE (pickle safety): RepoBench-R is distributed ONLY as gzipped pickle
    (the official HF loader itself does gzip+pickle). Source is the published
    CC-BY-4.0 research dataset `tianyang/repobench-r`, fetched via
    hf_hub_download (content-addressed, cached). No untrusted/user-supplied
    path is ever unpickled here. Accepted.
    """
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("ERROR: huggingface_hub not installed. pip install huggingface_hub",
              file=sys.stderr)
        sys.exit(1)

    gz = hf_hub_download(
        "tianyang/repobench-r",
        f"data/{config}.gz",
        repo_type="dataset",
    )
    obj = pickle.load(gzip.open(gz, "rb"))  # noqa: S301  (see note above)
    rows = obj[split][level]
    if n and n < len(rows):
        step = len(rows) // n
        rows = [rows[i * step] for i in range(n)]
    return rows


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------

def make_query(ex):
    """Build the retrieval query for an example: imports + last N code lines."""
    code = ex.get("code", "") or ""
    tail = "\n".join(code.splitlines()[-QUERY_TAIL_LINES:])
    imports = ex.get("import_statement", "") or ""
    return (imports + "\n" + tail).strip()


# ---------------------------------------------------------------------------
# Rankers
# ---------------------------------------------------------------------------

def rank_random(cands, ex_key):
    """Deterministic per-example permutation -- varies with ex_key."""
    return sorted(range(len(cands)), key=lambda i: hash((ex_key, i)))


def rank_overlap(query, cands):
    """Jaccard token overlap -- RepoBench paper's lexical baseline."""
    qs = set(tok(query))
    if not qs:
        return list(range(len(cands)))

    def jac(c):
        cs = set(tok(c))
        return (len(qs & cs) / len(qs | cs)) if cs else 0.0

    return sorted(range(len(cands)), key=lambda i: jac(cands[i]), reverse=True)


def rank_bm25(query, cands):
    """BM25Okapi over the candidate pool (IDF can go negative on tiny pools)."""
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        print("ERROR: rank-bm25 not installed. pip install rank-bm25", file=sys.stderr)
        sys.exit(1)

    corpus = [tok(c) for c in cands]
    if not any(corpus):
        return list(range(len(cands)))
    bm = BM25Okapi(corpus)
    scores = bm.get_scores(tok(query))
    return sorted(range(len(cands)), key=lambda i: scores[i], reverse=True)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def acc_at(order, gold, k):
    """Return 1.0 if gold is in the top-k of the ranked order, else 0.0."""
    return 1.0 if gold in order[:k] else 0.0


def _ks_for_level(level):
    """Return the k values to evaluate for a given difficulty level.

    Strategy doc section 3: acc@1/3 for easy; acc@1/3/5 for hard.
    """
    if level == "hard":
        return [1, 3, 5]
    return [1, 3]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="RepoBench-R foils: random / Jaccard-overlap / BM25Okapi"
    )
    ap.add_argument(
        "--config",
        default="python_cff",
        choices=["python_cff", "python_cfr", "java_cff", "java_cfr"],
        help="Dataset config: {python,java}_{cff=XF-F, cfr=XF-R}",
    )
    ap.add_argument(
        "--n",
        type=int,
        default=200,
        help="Max examples per difficulty level (0 = all)",
    )
    ap.add_argument(
        "--levels",
        default="easy,hard",
        help="Comma-separated difficulty levels to run (easy, hard)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Override output JSON path for the summary (default: results/ auto-name)",
    )
    args = ap.parse_args()

    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    levels = [x.strip() for x in args.levels.split(",") if x.strip()]
    methods = ["random", "overlap", "bm25"]
    summary = {
        "config": args.config,
        "n_per_level": args.n,
        "timestamp": ts,
        "levels": {},
    }

    for level in levels:
        ks = _ks_for_level(level)
        rows = load_split(args.config, "test", level, args.n)
        dump = []

        # Per-method per-k accumulators
        acc = {m: {k: 0.0 for k in ks} for m in methods}
        ncand = 0

        for ei, ex in enumerate(rows):
            cands = ex.get("context", []) or []
            gold = int(ex.get(GOLD_KEY, ex.get(GOLD_KEY_ALT, -1)))
            if not cands or gold < 0 or gold >= len(cands):
                continue

            q = make_query(ex)
            ncand += len(cands)
            ex_key = (level, ei)

            orders = {
                "random": rank_random(cands, ex_key),
                "overlap": rank_overlap(q, cands),
                "bm25": rank_bm25(q, cands),
            }
            for m in methods:
                for k in ks:
                    acc[m][k] += acc_at(orders[m], gold, k)

            dump.append({
                "repo": ex.get("repo_name"),
                "file": ex.get("file_path"),
                "query": q,
                "candidates": cands,
                "gold": gold,
            })

        m_ = len(dump)
        # Write per-example dump for the Helix arms to consume.
        dump_path = RESULTS_DIR / f"repobench_r_{args.config}_{level}_n{m_}.json"
        dump_path.write_text(
            json.dumps(dump, ensure_ascii=False), encoding="utf-8"
        )

        lvl_summary = {
            "n": m_,
            "avg_cands": round(ncand / m_, 1) if m_ else 0,
        }
        for me in methods:
            for k in ks:
                lvl_summary[f"{me}_acc@{k}"] = round(acc[me][k] / m_, 3) if m_ else 0.0

        summary["levels"][level] = lvl_summary

    # Print results table
    print(
        f"\nRepoBench-R foils -- {args.config}, n={args.n}/level, "
        f"query=imports+last {QUERY_TAIL_LINES} code lines"
    )
    all_ks = []
    for lv in levels:
        for k in _ks_for_level(lv):
            if k not in all_ks:
                all_ks.append(k)

    hdr = f"{'level':<8}{'n':>5}{'avgC':>6}"
    for m in methods:
        for k in all_ks:
            hdr += f"  {(m+'@'+str(k)):>10}"
    print(hdr)
    print("-" * len(hdr))
    for lv in levels:
        s = summary["levels"][lv]
        row = f"{lv:<8}{s['n']:>5}{s['avg_cands']:>6}"
        for me in methods:
            for k in all_ks:
                key = f"{me}_acc@{k}"
                val = s.get(key, "n/a")
                row += f"  {str(val):>10}"
        print(row)

    # Write summary JSON
    if args.out:
        out_path = Path(args.out)
    else:
        out_path = RESULTS_DIR / f"repobench_r_{args.config}_foils_{ts}.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n-> per-example dumps + summary in {RESULTS_DIR}/")
    print(f"   summary: {out_path}")
    print("   (Helix arms read the per-example JSON files)")


if __name__ == "__main__":
    main()

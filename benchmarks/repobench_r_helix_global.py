"""RepoBench-R (Step-1) Helix GLOBAL-genome arm.

One persistent genome over the deduped union of ALL candidate snippets across both
difficulty levels.  Scores two ways:

  B (pool-rank, directly comparable to foils):
      Retrieve globally, then rank ONLY this example's candidate set by score.
      acc@1/3 (easy) and acc@1/3/5 (hard).  Directly comparable to repobench_r.py foils.

  C (global-rank, realistic agent scenario):
      Gold counts only if its snippet lands in the global top-k against the whole
      corpus.  recall@1/3/5/10.  This is the real multi-project Helix use-case.

Also computes a matched global-BM25 foil (floored-IDF Okapi, same identifier
tokenizer) scored both ways.

Motivation: the per-example arm (repobench_r_helix.py) gives each query a corpus
of only ~5-17 snippets.  BM25 IDF is degenerate at that scale. A realistic
deployment ingests many documents, so the global arm gives a fairer picture.

LLM-free, GPU-free (lexical config).  Run DIRECTLY (not via uv):
  F:/Projects/_venvs/helix063/Scripts/python.exe -u benchmarks/repobench_r_helix_global.py

Reads per-example dumps from benchmarks/results/ (written by repobench_r.py).

Writes:
  benchmarks/results/repobench_r_{config}_global_{timestamp}.json
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import math
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

_DEFAULT_CONFIG_CANDIDATES = [
    Path(__file__).resolve().parents[1] / "docs" / "benchmarks" / "helix_probe_lexical.toml",
    Path("F:/tmp/cb_helix_probe/helix_probe.toml"),
]

_TOK = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def tok(s):
    return _TOK.findall(s or "")


def _find_default_config():
    for p in _DEFAULT_CONFIG_CANDIDATES:
        if Path(p).exists():
            return str(p)
    return None


def _ks_for_level(level):
    """acc@1/3 easy; acc@1/3/5 hard."""
    return [1, 3, 5] if level == "hard" else [1, 3]


# ---------------------------------------------------------------------------
# Floored-IDF BM25 over the global corpus.
# Avoids the negative-IDF sink that affects the per-pool foil in repobench_r.py
# on small candidate sets.
# ---------------------------------------------------------------------------

class BM25:
    """Okapi BM25 with IDF floored at 0."""

    def __init__(self, corpus_tokens, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.docs = corpus_tokens
        self.N = len(corpus_tokens)
        self.dl = [len(d) for d in corpus_tokens]
        self.avgdl = (sum(self.dl) / self.N) if self.N else 0.0
        df = defaultdict(int)
        self.tf = []
        for d in corpus_tokens:
            f = defaultdict(int)
            for t in d:
                f[t] += 1
            self.tf.append(f)
            for t in f:
                df[t] += 1
        # Floor at 0 -- avoids negative IDF on high-frequency terms in small corpora.
        self.idf = {
            t: max(0.0, math.log((self.N - n + 0.5) / (n + 0.5) + 1.0))
            for t, n in df.items()
        }

    def scores(self, q_tokens):
        qset = set(q_tokens)
        out = [0.0] * self.N
        for i in range(self.N):
            f = self.tf[i]
            denom_dl = (
                self.k1 * (1 - self.b + self.b * self.dl[i] / self.avgdl)
                if self.avgdl
                else self.k1
            )
            s = 0.0
            for t in qset:
                ft = f.get(t, 0)
                if ft:
                    s += self.idf.get(t, 0.0) * (ft * (self.k1 + 1)) / (ft + denom_dl)
            out[i] = s
        return out


# ---------------------------------------------------------------------------
# Helix wiring
# ---------------------------------------------------------------------------

def build_helix(genome_dir, config_path):
    os.environ.pop("HELIX_USE_SHARDS", None)
    os.environ["HELIX_CONFIG"] = config_path
    os.environ["HELIX_GENOME_PATH"] = os.path.join(genome_dir, "genome.db")
    shutil.rmtree(genome_dir, ignore_errors=True)
    os.makedirs(genome_dir, exist_ok=True)
    from helix_context.config import load_config
    from helix_context.context_manager import HelixContextManager
    return HelixContextManager(load_config())


def gene_sid(g):
    """Recover the snippet index from a retrieved gene's source_id/metadata."""
    src = getattr(g, "source_id", None)
    if not src and getattr(g, "promoter", None) and g.promoter.metadata:
        src = g.promoter.metadata.get("path")
    if src and str(src).startswith("snip_"):
        try:
            return int(str(src).split("snip_", 1)[1])
        except ValueError:
            return None
    return None


def helix_sid_scores(helix, query, n_corpus):
    """Return {sid: best_score} for this query over the whole genome."""
    _eq, dom, ent = helix._prepare_query_signals(query, session_context=None,
                                                 expand_query=False)
    cands = helix._retrieve(dom, ent, n_corpus, query_text=query, include_cold=None,
                            party_id="default", use_harmonic=False, use_sr=False)
    cands, _ = helix._apply_candidate_refiners(query, cands, n_corpus,
                                               use_cymatics=False,
                                               use_harmonic_bin=False,
                                               use_tcm=True, allow_rerank=False)
    raw = dict(helix.genome.last_query_scores or {})
    best = {}
    for g in cands:
        sid = gene_sid(g)
        if sid is None:
            continue
        s = raw.get(g.gene_id, 0.0)
        if sid not in best or s > best[sid]:
            best[sid] = s
    return best


def rank_pool(score_of, cand_sids):
    """B-mode: rank this example's candidate indices by global score (missing = 0)."""
    return sorted(range(len(cand_sids)), key=lambda j: (-score_of(cand_sids[j]), j))


def global_rank_pos(scores_by_sid, gold_sid):
    """C-mode: 0-based position of gold_sid in the full corpus ranking."""
    order = sorted(scores_by_sid.keys(), key=lambda s: (-scores_by_sid[s], s))
    pos = {s: i for i, s in enumerate(order)}
    if gold_sid in pos:
        return pos[gold_sid]
    # Gold was never retrieved: place it after all scored items.
    return len(order)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="RepoBench-R Helix global-genome arm -- B (pool-rank) + C (global-rank)"
    )
    ap.add_argument(
        "--config",
        default="python_cff",
        choices=["python_cff", "python_cfr", "java_cff", "java_cfr"],
        help="Dataset config (must match dump written by repobench_r.py)",
    )
    ap.add_argument(
        "--levels",
        default="easy,hard",
        help="Comma-separated difficulty levels",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap examples/level (0 = all from dump)",
    )
    ap.add_argument(
        "--helix-config",
        default=None,
        help="Path to lexical-probe helix.toml. Falls back to HELIX_CONFIG env var.",
    )
    ap.add_argument(
        "--genome-dir",
        default=None,
        help="Directory for the global genome DB (default: auto temp dir)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Override output path for the summary JSON",
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
        os.environ.get("TEMP", "/tmp"), "repobench_r_global_genome"
    )
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    levels = [x.strip() for x in args.levels.split(",") if x.strip()]

    # ---- Load per-example dumps ----
    rows_by_level = {}
    for lv in levels:
        pattern = str(RESULTS_DIR / f"repobench_r_{args.config}_{lv}_n*.json")
        files = sorted(glob.glob(pattern))
        if not files:
            print(
                f"[{lv}] no dump found at {pattern}\n"
                f"  -> Run: python benchmarks/repobench_r.py --config {args.config}",
                file=sys.stderr,
            )
            continue
        rows = json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        if args.limit:
            rows = rows[: args.limit]
        rows_by_level[lv] = rows

    if not rows_by_level:
        print("ERROR: No dump files found. Run repobench_r.py first.", file=sys.stderr)
        sys.exit(1)

    # ---- Build deduped global corpus ----
    content2sid = {}
    sid2content = []
    for lv in levels:
        for ex in rows_by_level.get(lv, []):
            for c in ex["candidates"]:
                if c not in content2sid:
                    content2sid[c] = len(sid2content)
                    sid2content.append(c)
    n_corpus = len(sid2content)
    total_examples = sum(len(rows_by_level.get(lv, [])) for lv in levels)
    print(
        f"global corpus: {n_corpus} unique snippets "
        f"(from {total_examples} examples across {len(rows_by_level)} level(s))",
        flush=True,
    )

    # ---- Ingest into Helix ----
    helix = build_helix(genome_dir, helix_config)
    ing_err = 0
    for sid, content in enumerate(sid2content):
        try:
            helix.ingest(content, content_type="code", metadata={"path": f"snip_{sid}"})
        except Exception:  # noqa: BLE001
            ing_err += 1
    print(f"ingested {n_corpus - ing_err}/{n_corpus} ({ing_err} ingest errors)",
          flush=True)

    # ---- BM25 over global corpus ----
    bm = BM25([tok(c) for c in sid2content])

    # k values for global C-mode recall (fixed across levels).
    C_KS = (1, 3, 5, 10)

    summary = {
        "config": args.config,
        "helix_config": helix_config,
        "timestamp": ts,
        "arm": "helix_global",
        "corpus_size": n_corpus,
        "ingest_errors": ing_err,
        "levels": {},
    }

    for lv in levels:
        rows = rows_by_level.get(lv, [])
        B_KS = _ks_for_level(lv)
        agg = {
            "helix": {"B": {k: 0.0 for k in B_KS}, "C": {k: 0.0 for k in C_KS}},
            "bm25":  {"B": {k: 0.0 for k in B_KS}, "C": {k: 0.0 for k in C_KS}},
        }
        n = 0

        for ex in rows:
            cands = ex["candidates"]
            gold_local = ex["gold"]
            if not cands or gold_local < 0 or gold_local >= len(cands):
                continue
            cand_sids = [content2sid[c] for c in cands]
            gold_sid = content2sid[cands[gold_local]]
            q = ex["query"]

            # -- Helix --
            h = helix_sid_scores(helix, q, n_corpus)
            h_score = lambda s: h.get(s, 0.0)
            h_pool = rank_pool(h_score, cand_sids)
            for k in B_KS:
                agg["helix"]["B"][k] += 1.0 if gold_local in h_pool[:k] else 0.0
            h_pos = global_rank_pos(h, gold_sid)
            for k in C_KS:
                agg["helix"]["C"][k] += 1.0 if h_pos < k else 0.0

            # -- BM25 --
            bs = bm.scores(tok(q))
            b_score = lambda s: bs[s]
            b_pool = rank_pool(b_score, cand_sids)
            for k in B_KS:
                agg["bm25"]["B"][k] += 1.0 if gold_local in b_pool[:k] else 0.0
            b_order = sorted(range(n_corpus), key=lambda s: (-bs[s], s))
            b_pos = b_order.index(gold_sid)
            for k in C_KS:
                agg["bm25"]["C"][k] += 1.0 if b_pos < k else 0.0

            n += 1

        lvl = {"n": n, "corpus": n_corpus}
        for arm in ("helix", "bm25"):
            for k in B_KS:
                lvl[f"{arm}_B_acc@{k}"] = round(agg[arm]["B"][k] / n, 3) if n else 0.0
            for k in C_KS:
                lvl[f"{arm}_C_recall@{k}"] = round(agg[arm]["C"][k] / n, 3) if n else 0.0
        summary["levels"][lv] = lvl

        b_str = "  ".join(f"B@{k}={lvl[f'helix_B_acc@{k}']}" for k in B_KS)
        c_str = "  ".join(f"C@{k}={lvl[f'helix_C_recall@{k}']}" for k in C_KS)
        print(f"[{lv}] n={n}  HELIX {b_str} | {c_str}", flush=True)

        bm_b = "  ".join(f"B@{k}={lvl[f'bm25_B_acc@{k}']}" for k in B_KS)
        bm_c = "  ".join(f"C@{k}={lvl[f'bm25_C_recall@{k}']}" for k in C_KS)
        print(f"[{lv}]   BM25 {bm_b} | {bm_c}", flush=True)

    if args.out:
        out_path = Path(args.out)
    else:
        out_path = RESULTS_DIR / f"repobench_r_{args.config}_global_{ts}.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n-> {out_path}")


if __name__ == "__main__":
    main()

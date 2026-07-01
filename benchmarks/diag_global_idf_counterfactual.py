"""diag_global_idf_counterfactual.py — counterfactual for the cross-shard
global-IDF lexical re-score (HELIX_SHARD_GLOBAL_IDF, #182).

Hypothesis under test
---------------------
The HELIX_SHARD_GLOBAL_IDF rescore regressed medium-sharded recall
(0.347 -> 0.168) because the manual BM25 used the SMOOTHED, always-positive
IDF ``ln(... + 1.0)`` instead of SQLite FTS5's RAW idf ``ln((N-n+0.5)/(n+0.5))``
(which can be negative). With the parity-correct raw-idf formula and
all-column tf/dl, manual-LOCAL BM25 must reproduce ``bm25(genes_fts)``
bit-exactly, and manual-GLOBAL BM25 (raw idf, global N=Σshard_n,
global df=Σshard_dfs) should re-rank buried golds ABOVE their wrong
same-/cross-shard incumbents.

What it measures, per buried gold (a gold ranked below a non-gold incumbent
on the engine-LOCAL lexical signal):
  (a) parity error: |manual-local-BM25 − engine bm25()| for gold + top
      wrong incumbent
  (b) go/no-go: fraction of buried golds where manual-GLOBAL-BM25 re-ranks
      the gold ABOVE the wrong incumbent
  (c) median rank improvement of the gold under global vs local lexical

This is a LEXICAL-ONLY counterfactual: it scores candidates purely by the
FTS5 lexical sub-score (the exact quantity the router splices in
``corrected = raw − old_local_fts5 + new_global_fts5``), so it isolates the
re-score math from dense/tag/SR tiers. No server, no GPU, no network,
read-only via ``immutable=1``.

Gold-match rule and query-signal extraction are reused from
bench_shard_recall.py / diag_shard_score.py (bidirectional substring on
normalised paths; helix_context.accel.extract_query_signals).

CLI
---
python benchmarks/diag_global_idf_counterfactual.py \
    --root genomes/bench/matrix-sharded/medium \
    --needles benchmarks/results/shard_gold_medium.jsonl \
    --limit 10 \
    --out benchmarks/results/diag_global_idf_counterfactual_medium.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

K1, B = 1.2, 0.75


# ── gold match (identical to bench_shard_recall.py / diag_shard_score.py) ──
def _norm(path: Optional[str]) -> str:
    return str(path or "").replace("\\", "/").lower().strip()


def _gold_hit(source: Optional[str], gold_paths: List[str]) -> bool:
    sn = _norm(source)
    if not sn:
        return False
    for gp in gold_paths:
        gn = _norm(gp)
        if gn and (gn in sn or sn in gn):
            return True
    return False


def _raw_idf(N: int, n: int) -> float:
    """SQLite FTS5's RAW (unsmoothed) Robertson-Sparck-Jones IDF, with FTS5's
    exact non-positive clamp. The engine computes
    ``ln((N - n + 0.5) / (n + 0.5))`` then forces ``if (idf <= 0) idf = 1e-6``
    (fts5_aux.c) — a term in > half the corpus contributes ~nothing rather
    than subtracting. Reproduced here so manual BM25 is bit-exact with
    bm25() even when a query term is near-ubiquitous."""
    idf = math.log((N - n + 0.5) / (n + 0.5))
    return idf if idf > 0.0 else 1e-6


def _single_token(t: str) -> bool:
    """True iff ``t`` is a single FTS token (alphanumeric / underscore only).

    Hyphenated or dotted query phrases (e.g. ``end-to-end``, ``amazon.nova``)
    tokenize into MULTIPLE FTS tokens and FTS5 scores them as a PHRASE; the
    ``genes_fts_vocab`` shadow table stores only single tokens, so neither
    the manual BM25 here NOR the production
    ``KnowledgeStore.rescore_lexical_global_idf`` (which reads the same vocab)
    can reproduce a multi-token phrase's contribution. We exclude such terms
    from BOTH the engine query and the manual sum so the parity number
    measures the single-token case the vocab path actually scores. The
    production splice is unaffected by the omission: local and global rescore
    use the SAME vocab lookup, so any multi-token shortfall cancels in
    ``corrected = raw − old_local_fts5 + new_global_fts5`` for the same doc."""
    return bool(t) and t.replace("_", "").isalnum()


# ── shard discovery ──
def _discover_shards(root: Path) -> Dict[str, Path]:
    """{shard_name: db_path} for every *.genome.db under root except main."""
    shards: Dict[str, Path] = {}
    for db in sorted(root.rglob("*.genome.db")):
        if db.name == "main.genome.db":
            continue
        shards[db.stem.replace(".genome", "")] = db
    return shards


def _ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect("file:" + str(db) + "?immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


class Shard:
    """Read-only FTS5 view of one shard, with the parity-correct manual BM25."""

    def __init__(self, name: str, db: Path) -> None:
        self.name = name
        self.con = _ro(db)
        cur = self.con.cursor()
        self.N = cur.execute("SELECT COUNT(*) FROM genes_fts").fetchone()[0]
        # The ``genes_fts_vocab`` fts5vocab shadow table is created at DB
        # init for current stores but absent on legacy shards. It is the
        # only practical source of per-doc tf/dl, which manual BM25 needs.
        # When absent on an immutable DB we cannot CREATE it (read-only), so
        # this shard still contributes N + phrase_df to the GLOBAL idf basis
        # (read-only COUNT/MATCH), but its candidates are NOT manually
        # re-scored. The parity + go/no-go stats then come from shards that
        # do carry the vocab (the common ``within``-shard gold case).
        self.has_vocab = bool(cur.execute(
            "SELECT 1 FROM sqlite_master WHERE name='genes_fts_vocab'"
        ).fetchone())
        self.avgdl = 1.0
        self._dl: Dict[int, int] = {}
        if self.has_vocab:
            tot = cur.execute("SELECT COUNT(*) FROM genes_fts_vocab").fetchone()[0]
            self.avgdl = (tot / float(self.N)) if self.N else 1.0
            if self.avgdl <= 0:
                self.avgdl = 1.0
            # per-doc length over ALL columns (FTS5's D)
            for r in cur.execute(
                "SELECT doc, COUNT(*) c FROM genes_fts_vocab GROUP BY doc"
            ):
                self._dl[int(r[0])] = int(r[1])

    def engine_local(self, terms: List[str], limit: int) -> List[Tuple[int, str, float]]:
        """FTS5 engine ranking: [(rowid, source_id, -bm25)] (positive lexical)."""
        bm = [t for t in terms if len(t) > 2 and _single_token(t)]
        if not bm:
            return []
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in bm)
        cur = self.con.cursor()
        try:
            rows = cur.execute(
                "SELECT f.rowid AS rid, g.source_id AS src, bm25(genes_fts) AS r "
                "FROM genes_fts f JOIN genes g ON g.gene_id = f.gene_id "
                "WHERE genes_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        except Exception:
            return []
        return [(int(x["rid"]), x["src"], -float(x["r"])) for x in rows]

    def phrase_df(self, term: str) -> int:
        """Rows matching the phrase in ANY column — FTS5's n (cached)."""
        cache = getattr(self, "_df_cache", None)
        if cache is None:
            cache = self._df_cache = {}
        if term in cache:
            return cache[term]
        try:
            n = self.con.execute(
                "SELECT COUNT(*) FROM genes_fts WHERE genes_fts MATCH ?",
                ('"' + term.replace('"', '""') + '"',),
            ).fetchone()[0]
        except Exception:
            n = 0
        cache[term] = n
        return n

    def tf_batch(self, rowids: List[int], terms: List[str]) -> Dict[int, Dict[str, int]]:
        """{rowid: {term: tf}} over ALL columns, one GROUP-BY query per term.
        Mirrors the batched per-term tf probe in
        KnowledgeStore.rescore_lexical_global_idf (which also does one
        ``GROUP BY doc`` query per scored term, bounded by query length)."""
        out: Dict[int, Dict[str, int]] = {rid: {} for rid in rowids}
        if not self.has_vocab or not rowids:
            return out
        ph = ",".join("?" * len(rowids))
        for t in terms:
            if len(t) <= 2:
                continue
            try:
                rows = self.con.execute(
                    f"SELECT doc, COUNT(*) c FROM genes_fts_vocab "
                    f"WHERE term=? AND doc IN ({ph}) GROUP BY doc",
                    (t, *rowids),
                ).fetchall()
            except Exception:
                continue
            for r in rows:
                rid = int(r[0])
                if rid in out:
                    out[rid][t] = int(r[1])
        return out

    def manual_bm25(self, rowid: int, terms: List[str], idf_map: Dict[str, float],
                    tf_map: Dict[str, int]) -> float:
        """Parity-correct manual BM25 (positive sum) with injected idf_map.
        Verbatim with KnowledgeStore.rescore_lexical_global_idf: all-column
        tf/dl/avgdl, signed idf, k1=1.2, b=0.75. ``tf_map`` is this doc's
        per-term tf (from :meth:`tf_batch`)."""
        dl = float(self._dl.get(rowid, 0)) or 1.0
        s = 0.0
        for t in terms:
            if len(t) <= 2 or t not in idf_map:
                continue
            tf = float(tf_map.get(t, 0))
            if tf <= 0:
                continue
            denom = tf + K1 * (1.0 - B + B * (dl / self.avgdl))
            if denom <= 0:
                continue
            s += float(idf_map[t]) * (tf * (K1 + 1.0)) / denom
        return s


def _load_needles(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            nd = json.loads(line)
            q = nd.get("question") or nd.get("query")
            gp = nd.get("gold_paths") or nd.get("gold_path")
            if isinstance(gp, str):
                gp = [gp]
            if not q or not gp:
                continue
            out.append({"question": q, "gold_paths": gp})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="genomes/bench/matrix-sharded/medium")
    ap.add_argument("--needles", default="benchmarks/results/shard_gold_medium.jsonl")
    ap.add_argument("--limit", type=int, default=10,
                    help="per-shard FTS engine candidate depth")
    ap.add_argument("--out", default="benchmarks/results/diag_global_idf_counterfactual_medium.json")
    args = ap.parse_args()

    from helix_context.accel import extract_query_signals

    root = Path(args.root)
    shard_paths = _discover_shards(root)
    if not shard_paths:
        print(f"no shards under {root}", file=sys.stderr)
        return 2
    shards = {n: Shard(n, p) for n, p in shard_paths.items()}
    total_N = sum(s.N for s in shards.values())

    needles = _load_needles(args.needles)

    per: List[Dict[str, Any]] = []
    parity_errs: List[float] = []
    buried = 0
    reranked_above = 0
    rank_improvements: List[int] = []

    for nd in needles:
        q = nd["question"]
        gold_paths = nd["gold_paths"]
        domains, entities = extract_query_signals(q)
        # idf terms: dedup, len>2 (mirrors ShardRouter.query_genes)
        seen: set = set()
        idf_terms: List[str] = []
        for t in (domains or []) + (entities or []):
            if not t:
                continue
            k = t.lower()
            if k in seen or len(k) <= 2 or not _single_token(k):
                continue
            seen.add(k)
            idf_terms.append(k)
        if not idf_terms:
            continue

        # Global N/df aggregated across shards (raw-idf basis).
        global_df: Dict[str, int] = {t: 0 for t in idf_terms}
        for s in shards.values():
            for t in idf_terms:
                global_df[t] += s.phrase_df(t)
        global_idf = {t: _raw_idf(total_N, global_df[t]) for t in idf_terms}

        # Fan out: per-shard engine-local FTS candidates. Merge into one
        # candidate pool keyed by (shard, rowid). The LOCAL lexical signal is
        # the engine bm25() (per-shard local idf). The GLOBAL signal is the
        # parity-correct manual BM25 with the aggregated raw global idf.
        cands: List[Dict[str, Any]] = []
        for s in shards.values():
            # per-shard LOCAL idf map (for the parity check only)
            local_idf = {t: _raw_idf(s.N, s.phrase_df(t)) for t in idf_terms}
            engine_rows = s.engine_local(idf_terms, args.limit)
            rowids = [rid for rid, _src, _e in engine_rows]
            tf_all = s.tf_batch(rowids, idf_terms) if s.has_vocab else {}
            for rid, src, eng_local in engine_rows:
                if s.has_vocab:
                    tfm = tf_all.get(rid, {})
                    man_local = s.manual_bm25(rid, idf_terms, local_idf, tfm)
                    man_global = s.manual_bm25(rid, idf_terms, global_idf, tfm)
                    parity_err = abs(eng_local - man_local)
                else:
                    # no vocab on this (legacy, read-only) shard — cannot
                    # form manual tf/dl. Use the engine-local value as a
                    # stand-in for ordering so the doc still competes, but
                    # exclude it from parity/go-no-go stats.
                    man_local = man_global = eng_local
                    parity_err = None
                cands.append({
                    "shard": s.name, "rowid": rid, "source_id": src,
                    "engine_local": eng_local,
                    "manual_local": man_local,
                    "manual_global": man_global,
                    "is_gold": _gold_hit(src, gold_paths),
                    "parity_err": parity_err,
                    "manual_ok": s.has_vocab,
                })
        if not cands:
            continue

        # rank by engine-local lexical (the baseline ordering the splice acts on)
        by_local = sorted(cands, key=lambda c: -c["engine_local"])
        gold_idx = next((i for i, c in enumerate(by_local) if c["is_gold"]), None)
        if gold_idx is None:
            continue  # gold not in lexical pool at all

        gold = by_local[gold_idx]
        # wrong incumbents ranked ABOVE the gold on the local signal
        wrong_above = [c for c in by_local[:gold_idx] if not c["is_gold"]]
        if gold.get("parity_err") is not None:
            parity_errs.append(gold["parity_err"])
        rec: Dict[str, Any] = {
            "question": q[:80],
            "gold": {k: gold[k] for k in
                     ("shard", "source_id", "engine_local", "manual_local",
                      "manual_global", "parity_err")},
            "local_rank": gold_idx + 1,
            "n_wrong_above": len(wrong_above),
        }
        if wrong_above:
            top_wrong = wrong_above[0]
            rec["top_wrong"] = {k: top_wrong[k] for k in
                                ("shard", "source_id", "engine_local",
                                 "manual_local", "manual_global", "parity_err")}
            if top_wrong.get("parity_err") is not None:
                parity_errs.append(top_wrong["parity_err"])
            # Go/no-go only counts buried golds where BOTH the gold and the
            # top wrong incumbent are manually scorable (vocab present), so
            # the comparison is on real manual-global BM25, not the
            # engine-local stand-in used for vocab-less legacy shards.
            scorable = gold.get("manual_ok") and top_wrong.get("manual_ok")
            rec["scorable"] = bool(scorable)
            if scorable:
                buried += 1
                # GO/NO-GO: does GLOBAL manual BM25 put gold above top wrong?
                global_above = gold["manual_global"] > top_wrong["manual_global"]
                rec["global_reranks_above_top_wrong"] = global_above
                if global_above:
                    reranked_above += 1
                # rank improvement under global lexical ordering (over the
                # manually-scorable candidates only, to keep it apples-to-apples)
                scorable_cands = [c for c in cands if c.get("manual_ok")]
                by_global = sorted(scorable_cands, key=lambda c: -c["manual_global"])
                by_local_sc = sorted(scorable_cands, key=lambda c: -c["engine_local"])
                def _rank(seq):
                    return next(
                        (i for i, c in enumerate(seq)
                         if c["shard"] == gold["shard"] and c["rowid"] == gold["rowid"]),
                        None,
                    )
                gr, lr = _rank(by_global), _rank(by_local_sc)
                if gr is not None and lr is not None:
                    rec["local_rank_scorable"] = lr + 1
                    rec["global_rank_scorable"] = gr + 1
                    rank_improvements.append(lr - gr)
        per.append(rec)

    def _med(xs: List[float]) -> Optional[float]:
        return statistics.median(xs) if xs else None

    summary = {
        "root": str(root),
        "n_shards": len(shards),
        "total_N": total_N,
        "n_needles": len(needles),
        "n_gold_in_lexical_pool": len(per),
        "n_buried_golds": buried,
        "median_parity_error": _med(parity_errs),
        "max_parity_error": max(parity_errs) if parity_errs else None,
        "parity_bit_exact": (max(parity_errs) < 1e-9) if parity_errs else None,
        "frac_buried_golds_global_reranks_above": (
            reranked_above / buried if buried else None),
        "n_buried_reranked_above": reranked_above,
        "median_rank_improvement": _med([float(x) for x in rank_improvements]),
    }

    out = {"summary": summary, "needles": per}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print("=== global-IDF counterfactual (medium sharded) ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"\nwrote {args.out}")
    go = summary["frac_buried_golds_global_reranks_above"]
    if go is not None:
        verdict = "GO" if go >= 0.5 else "WEAK/NO-GO"
        print(f"\nHYPOTHESIS {verdict}: global-BM25 re-ranks {go:.0%} of buried golds above their wrong incumbent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

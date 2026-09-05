"""probe_absent_term_overlap.py -- FOLLOW-UP 1 to ARM 1 (pool-depth forensics).

THE QUESTION
    ARM 1 split the 109 pool_absent misses by BM25 reach. Twenty of them are
    the hard tail:

      GROUP A (5, all semantic) -- gold is absent from the RAW BM25 pool even
        at depth 20000 (bm25_proxy_rank IS NULL).
      GROUP B (15, non-semantic: 14 basic + 1 intra_document_reasoning) --
        gold is absent from the pool at depth 1000 but present by depth 20000
        (bm25_proxy_rank in 1001..17745).

    For both groups the receipt says "gold is not in the pool". It does NOT
    say WHY. Two mutually exclusive causes have completely different
    remedies:

      TRUE VOCABULARY GAP -- the query's terms simply do not occur in the
        gold text. No lexical lever (depth, shortlist width, fusion, rerank)
        can ever reach it; only a semantic/dense/expansion lever can.
      OUTRANKED -- the terms DO occur in the gold text, but ~10^5..10^6 other
        documents score higher on the same OR-joined query. This is a
        RANKING problem, and lexical levers (IDF weighting, stopword-ish
        high-DF term suppression, phrase/proximity, per-term gating) are
        live.

PRE-REGISTERED CLASSIFICATION RULE (written into this file and into the
receipt BEFORE the bed was queried for any needle):

    (a) = the number of DISTINCT query terms (the pipeline's own captured
          query_terms, filtered to len(t) > 2 exactly as the BM25 shortlist
          filters them) that OCCUR in the needle's gold chunk text, where
          "occur" is decided by FTS5 itself under the same tokenizer
          genes_fts uses.

    A needle is a TRUE VOCABULARY GAP iff (a) == 0.
    A needle is OUTRANKED                iff (a) > 0.

    There is no third bucket and no threshold to tune. The rule is stated
    before the measurement so the split cannot be back-fitted.

(b) SECOND MEASUREMENT -- high-DF term suppression.
    gold's BM25 rank under the SAME SQL the pipeline's shortlist uses

        SELECT gene_id FROM genes_fts WHERE genes_fts MATCH ? ORDER BY rank
        LIMIT ?

    with the query's THREE HIGHEST-DOCUMENT-FREQUENCY distinct terms removed
    (all their occurrences), depth 20000 -- i.e. "the query minus its most
    common terms". This is the cheapest possible probe of whether the
    OUTRANKED needles are outranked BY the common terms.

TOKENIZER -- read, not assumed
    sqlite_master's genes_fts DDL carries NO ``tokenize=`` clause:

        CREATE VIRTUAL TABLE genes_fts USING fts5(
            gene_id, content, complement,
            content='genes_fts_source', content_rowid='rowid')

    so the table uses SQLite FTS5's DEFAULT tokenizer, ``unicode61`` with
    default options (remove_diacritics=1; token characters = Unicode L*/N*;
    everything else a separator; case-folded). This probe does NOT
    re-implement that. Occurrence is decided by running the term as an FTS5
    phrase query against an IN-MEMORY fts5 table created with the same
    (default) tokenizer -- the tokenizer itself does the matching, so the
    contract is byte-identical to genes_fts by construction. The DDL as read
    from the bed is stamped into the receipt.

BED ACCESS
    Read-only (``mode=ro`` URI) throughout. No index is created on the bed.
    A ``temp.`` fts5vocab('row') shadow is created for document-frequency
    lookups -- that lives in the connection's TEMP database, never in the
    bed file. The pipeline is NOT run; this probe is pure SQL + the ARM-1
    capture.

Usage:
    python benchmarks/dogfood/erb/probe_absent_term_overlap.py --stamp 2026-09-01
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("CYMATIX_DISABLE_LEARN", "1")

BED = "F:/tmp/erb_blob_v09x.db"
GOLD = HERE / "gold_by_needle_v09x.json"
NEEDLES = HERE / "needles_resolved_v09x_full.json"
RECEIPTS = HERE / "receipts"
FORENSICS = RECEIPTS / "pool_depth_forensics_2026-09-01.json"
CAPTURE = RECEIPTS / "pool_depth_capture_2026-09-01.sqlite"
VERIFY_COMPLETENESS = RECEIPTS / "verify_a1_completeness_2026-09-01.json"
VERIFY_RECOMPUTE = RECEIPTS / "verify_a1_recompute_2026-09-01.json"

DEPTH = 20000          # same depth ARM 1's bm25 proxy used
N_DROP = 3             # "top-3 highest-DF terms removed"
MIN_TERM_LEN = 3       # the shortlist's own filter is len(t) > 2

PREREGISTRATION = (
    "PRE-REGISTERED BEFORE THE BED WAS QUERIED. (a) = number of DISTINCT "
    "captured query terms (len > 2, the BM25 shortlist's own filter) that "
    "occur in the needle's gold chunk text (genes.content), occurrence "
    "decided by FTS5 phrase match under the same default unicode61 "
    "tokenizer genes_fts uses. A needle is a TRUE VOCABULARY GAP iff "
    "(a) == 0; it is OUTRANKED iff (a) > 0. No third bucket, no tunable "
    "threshold."
)

POPULATION_RULE = (
    "GROUP A = pool_depth_forensics rows with bucket='pool_absent', "
    "question_type='semantic', bm25_proxy_rank IS NULL (gold absent from "
    "the raw BM25 pool even at depth 20000). GROUP B = rows with "
    "bucket='pool_absent', question_type != 'semantic', bm25_proxy_rank "
    "> 1000 (absent at depth 1000, present by 20000). Derived from the "
    "receipt, not hardcoded; the expected membership (5 / 15) is asserted."
)


# ── plumbing ────────────────────────────────────────────────────────────
def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def git_sha() -> Optional[str]:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=20,
        ).stdout.strip() or None
    except Exception:
        return None


def dedupe(seq: Sequence[str]) -> List[str]:
    """First-appearance-order dedupe (the pipeline builds its MATCH string
    from the RAW list, duplicates included; this is only for the per-term
    tables and the DF ranking)."""
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ── FTS5-native tokenizer / occurrence oracle (in-memory, never the bed) ─
class Fts5Oracle:
    """An in-memory FTS5 table created with the DEFAULT tokenizer -- the same
    one genes_fts uses (its DDL carries no ``tokenize=``). Used for two
    things, both delegated to FTS5 rather than re-implemented:

      * tokenize(term)  -> the exact token sequence unicode61 produces
      * occurs(term)    -> does the term, as an FTS5 phrase, match the text?
    """

    def __init__(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE VIRTUAL TABLE doc USING fts5(body)")
        self.con.execute("CREATE VIRTUAL TABLE tk USING fts5(body)")
        self.con.execute(
            "CREATE VIRTUAL TABLE tkv USING fts5vocab(tk, 'instance')")

    # -- tokenization -----------------------------------------------------
    def tokenize_many(self, terms: Sequence[str]) -> Dict[str, List[str]]:
        """One row per term, then read the instance vocab back grouped by
        rowid -> the exact token sequence FTS5 produced for each term."""
        self.con.execute("DELETE FROM tk")
        rowid_of: Dict[int, str] = {}
        for i, t in enumerate(terms, 1):
            self.con.execute("INSERT INTO tk(rowid, body) VALUES (?,?)", (i, t))
            rowid_of[i] = t
        out: Dict[str, List[Tuple[int, str]]] = {t: [] for t in terms}
        for term, doc, off in self.con.execute(
                "SELECT term, doc, offset FROM tkv ORDER BY doc, offset"):
            t = rowid_of.get(doc)
            if t is not None:
                out[t].append((off, term))
        return {t: [tok for _, tok in sorted(v)] for t, v in out.items()}

    # -- occurrence -------------------------------------------------------
    def load_docs(self, texts: Sequence[str]) -> None:
        self.con.execute("DELETE FROM doc")
        for i, txt in enumerate(texts, 1):
            self.con.execute("INSERT INTO doc(rowid, body) VALUES (?,?)",
                             (i, txt or ""))

    def occurs(self, term: str) -> bool:
        """FTS5 phrase match of *term* against the loaded texts. Identical
        semantics to the '"<term>"' clause the pipeline puts in its OR
        query."""
        try:
            row = self.con.execute(
                'SELECT COUNT(*) FROM doc WHERE doc MATCH ?',
                ('"' + term.replace('"', '""') + '"',)).fetchone()
            return bool(row and row[0])
        except sqlite3.OperationalError:
            return False

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:
            pass


# ── bed-side helpers ────────────────────────────────────────────────────
def open_bed() -> sqlite3.Connection:
    con = sqlite3.connect("file:" + BED + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def bm25_rank(cur, match: str, gold_set: set, depth: int
              ) -> Tuple[Optional[int], int, float]:
    """The pipeline's OWN shortlist SQL (knowledge_store.py :3849-3853),
    only the LIMIT widened to *depth*. Returns (rank_of_first_gold, pool,
    wall_ms)."""
    t0 = time.perf_counter()
    rows = cur.execute(
        "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH ? "
        "ORDER BY rank LIMIT ?", (match, depth)).fetchall()
    ms = (time.perf_counter() - t0) * 1000.0
    for i, r in enumerate(rows, 1):
        if r["gene_id"] in gold_set:
            return i, len(rows), ms
    return None, len(rows), ms


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default="2026-09-01")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    t_start = time.time()

    forensics = json.loads(FORENSICS.read_text(encoding="utf-8"))
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    needles = {n["name"]: n for n in json.loads(
        NEEDLES.read_text(encoding="utf-8"))["needles"]}
    rows = forensics["rows"]
    row_by = {r["needle"]: r for r in rows}

    # ── population (derived, then asserted) ─────────────────────────────
    group_a = sorted(r["needle"] for r in rows
                     if r["bucket"] == "pool_absent"
                     and r["question_type"] == "semantic"
                     and r["bm25_proxy_rank"] is None)
    group_b = sorted(r["needle"] for r in rows
                     if r["bucket"] == "pool_absent"
                     and r["question_type"] != "semantic"
                     and r["bm25_proxy_rank"] is not None
                     and r["bm25_proxy_rank"] > 1000)
    assert len(group_a) == 5, "GROUP A drifted: " + repr(group_a)
    assert len(group_b) == 15, "GROUP B drifted: " + repr(group_b)
    targets = group_a + group_b
    print("GROUP A (semantic, bm25-absent@20000): " + ", ".join(group_a))
    print("GROUP B (non-semantic, bm25 1001-20000): " + ", ".join(group_b))

    # ── captured query terms (ARM 1's own, not a re-extraction) ─────────
    cap = sqlite3.connect("file:" + str(CAPTURE) + "?mode=ro", uri=True)
    term_json = dict(cap.execute("SELECT needle, query_terms FROM terms"))
    cap.close()
    raw_terms = {n: json.loads(term_json[n]) for n in targets}

    con = open_bed()
    cur = con.cursor()

    fts_ddl = cur.execute(
        "SELECT sql FROM sqlite_master WHERE name='genes_fts'").fetchone()[0]
    tokenizer_note = (
        "genes_fts DDL carries no tokenize= clause -> SQLite FTS5 DEFAULT "
        "tokenizer 'unicode61' (remove_diacritics=1, Unicode L*/N* token "
        "characters, case-folded). Matching is delegated to an in-memory "
        "FTS5 table built with the same default tokenizer, so no "
        "re-implementation is involved.")

    # temp-database fts5vocab shadow for document frequencies. This is a
    # TEMP virtual table: it never touches the bed file.
    cur.execute("CREATE VIRTUAL TABLE temp.gv USING fts5vocab('main', "
                "'genes_fts', 'row')")
    df_cache: Dict[str, Optional[int]] = {}

    def token_df(tok: str) -> int:
        if tok not in df_cache:
            r = cur.execute("SELECT doc FROM temp.gv WHERE term=?",
                            (tok,)).fetchone()
            df_cache[tok] = int(r[0]) if r else 0
        return df_cache[tok] or 0

    oracle = Fts5Oracle()

    per_needle: List[Dict[str, Any]] = []
    for name in targets:
        terms_all = raw_terms[name]
        bm25_terms = [t for t in terms_all if len(t) > MIN_TERM_LEN - 1]
        distinct = dedupe(bm25_terms)

        # -- gold text (primary basis: genes.content) --------------------
        gids = list(gold.get(name, []))
        grows = []
        for gid in gids:
            r = cur.execute(
                "SELECT content FROM genes WHERE gene_id=?", (gid,)).fetchone()
            grows.append(r["content"] if r else None)
        contents = [c or "" for c in grows]
        # -- gold text (secondary basis: what genes_fts ACTUALLY indexes) -
        idx_texts = []
        for gid in gids:
            r = cur.execute(
                "SELECT gene_id, content, complement FROM genes_fts_source "
                "WHERE gene_id=?", (gid,)).fetchone()
            if r:
                idx_texts.append((r["gene_id"] or "") + " "
                                 + (r["content"] or "") + " "
                                 + (r["complement"] or ""))

        # (a) primary: occurrence in genes.content
        oracle.load_docs(contents)
        occ_content = {t: oracle.occurs(t) for t in distinct}
        # secondary: occurrence in the indexed text (source_id + tags +
        # content + complement) -- what BM25 can actually see
        oracle.load_docs(idx_texts)
        occ_indexed = {t: oracle.occurs(t) for t in distinct}

        a_content = sum(1 for t in distinct if occ_content[t])
        a_indexed = sum(1 for t in distinct if occ_indexed[t])

        # -- document frequencies + top-N-DF removal ---------------------
        toks = oracle.tokenize_many(distinct)
        df: Dict[str, int] = {}
        for t in distinct:
            tt = toks.get(t) or []
            if not tt:
                df[t] = 0
            elif len(tt) == 1:
                df[t] = token_df(tt[0])
            else:
                # phrase: exact phrase DF is not obtainable from fts5vocab;
                # min token DF is a strict UPPER bound and is what we rank on.
                df[t] = min(token_df(x) for x in tt)
        ranked = sorted(distinct, key=lambda t: (-df[t], t))
        dropped = ranked[:N_DROP]
        dropped_set = set(dropped)
        kept = [t for t in bm25_terms if t not in dropped_set]

        gold_set = set(gids)
        full_match = " OR ".join('"' + t + '"' for t in bm25_terms)
        r_full, pool_full, ms_full = bm25_rank(cur, full_match, gold_set, DEPTH)
        if kept:
            red_match = " OR ".join('"' + t + '"' for t in kept)
            r_red, pool_red, ms_red = bm25_rank(cur, red_match, gold_set, DEPTH)
        else:
            r_red, pool_red, ms_red = None, 0, 0.0

        classification = ("TRUE_VOCABULARY_GAP" if a_content == 0
                          else "OUTRANKED")
        rec = {
            "needle": name,
            "group": "A" if name in group_a else "B",
            "question_type": row_by[name]["question_type"],
            "bucket": row_by[name]["bucket"],
            "receipt_bm25_proxy_rank": row_by[name]["bm25_proxy_rank"],
            "n_gold_chunks": len(gids),
            "gold_content_chars": sum(len(c) for c in contents),
            "n_terms_raw": len(terms_all),
            "n_terms_bm25": len(bm25_terms),
            "n_terms_distinct": len(distinct),
            # (a)
            "a_terms_in_gold_content": a_content,
            "a_frac_of_distinct": (round(a_content / len(distinct), 4)
                                   if distinct else None),
            "terms_in_gold_content": [t for t in distinct if occ_content[t]],
            "a_terms_in_gold_indexed_text": a_indexed,
            "terms_in_gold_indexed_only": [
                t for t in distinct if occ_indexed[t] and not occ_content[t]],
            "classification": classification,
            # (b)
            "df_top3_dropped": [{"term": t, "df": df[t],
                                 "tokens": toks.get(t)} for t in dropped],
            "n_terms_kept": len(kept),
            "bm25_rank_full_query": r_full,
            "bm25_pool_full_query": pool_full,
            "bm25_rank_minus_top3_df": r_red,
            "bm25_pool_minus_top3_df": pool_red,
            "rank_delta_minus_top3": (
                (r_full - r_red) if (r_full is not None and r_red is not None)
                else None),
            "full_query_rank_matches_receipt":
                (r_full == row_by[name]["bm25_proxy_rank"]),
            "wall_ms_full": round(ms_full, 1),
            "wall_ms_reduced": round(ms_red, 1),
        }
        per_needle.append(rec)
        print("  " + name + " grp" + rec["group"]
              + " a=" + str(a_content) + "/" + str(len(distinct))
              + " (idx " + str(a_indexed) + ")"
              + " -> " + classification
              + " | bm25 full=" + str(r_full) + " minus3DF=" + str(r_red),
              flush=True)

    oracle.close()
    con.close()

    # ── summary ─────────────────────────────────────────────────────────
    def summarize(grp: List[Dict[str, Any]]) -> Dict[str, Any]:
        gaps = [r for r in grp if r["classification"] == "TRUE_VOCABULARY_GAP"]
        out = [r for r in grp if r["classification"] == "OUTRANKED"]
        a_vals = [r["a_terms_in_gold_content"] for r in grp]
        improved = [r for r in grp if r["rank_delta_minus_top3"] is not None
                    and r["rank_delta_minus_top3"] > 0]
        worsened = [r for r in grp if r["rank_delta_minus_top3"] is not None
                    and r["rank_delta_minus_top3"] < 0]
        rescued = [r for r in grp if r["bm25_rank_full_query"] is None
                   and r["bm25_rank_minus_top3_df"] is not None]
        lost = [r for r in grp if r["bm25_rank_full_query"] is not None
                and r["bm25_rank_minus_top3_df"] is None]
        into_1000 = [r for r in grp
                     if (r["bm25_rank_minus_top3_df"] is not None
                         and r["bm25_rank_minus_top3_df"] <= 1000)]
        into_50 = [r for r in grp
                   if (r["bm25_rank_minus_top3_df"] is not None
                       and r["bm25_rank_minus_top3_df"] <= 50)]
        return {
            "n": len(grp),
            "true_vocabulary_gap": len(gaps),
            "true_vocabulary_gap_needles": [r["needle"] for r in gaps],
            "outranked": len(out),
            "outranked_needles": [r["needle"] for r in out],
            "a_min": min(a_vals) if a_vals else None,
            "a_median": statistics.median(a_vals) if a_vals else None,
            "a_max": max(a_vals) if a_vals else None,
            "a_indexed_zero": sum(
                1 for r in grp if r["a_terms_in_gold_indexed_text"] == 0),
            "minus_top3_df_improved": len(improved),
            "minus_top3_df_worsened": len(worsened),
            "minus_top3_df_rescued_from_absent": len(rescued),
            "minus_top3_df_lost_to_absent": len(lost),
            "minus_top3_df_rank_le_1000": len(into_1000),
            "minus_top3_df_rank_le_50": len(into_50),
            "full_query_rank_reproduces_receipt": all(
                r["full_query_rank_matches_receipt"] for r in grp),
        }

    ga = [r for r in per_needle if r["group"] == "A"]
    gb = [r for r in per_needle if r["group"] == "B"]
    summary = {
        "group_A_semantic_bm25_absent_at_20000": summarize(ga),
        "group_B_nonsemantic_bm25_1001_20000": summarize(gb),
        "overall": summarize(per_needle),
    }

    n_gap = summary["overall"]["true_vocabulary_gap"]
    n_out = summary["overall"]["outranked"]
    verdict = ("ALL_OUTRANKED" if n_gap == 0 else
               "ALL_VOCABULARY_GAP" if n_out == 0 else "MIXED")

    receipt = {
        "stamp": args.stamp,
        "probe": ("FOLLOW-UP 1 to ARM 1 -- term overlap for the 20 "
                  "gold-absent pool_absent needles"),
        "tool": "benchmarks/dogfood/erb/probe_absent_term_overlap.py",
        "tool_sha256": sha256_file(Path(__file__)),
        "git_sha": git_sha(),
        "bed": BED,
        "bed_bytes": Path(BED).stat().st_size,
        "bed_access": ("READ-ONLY (mode=ro URI). No index created on the "
                       "bed; the fts5vocab('row') shadow used for document "
                       "frequencies is a TEMP virtual table in the "
                       "connection's temp database. The pipeline was NOT "
                       "run."),
        "basis_receipts": [
            "benchmarks/dogfood/erb/receipts/pool_depth_forensics_2026-09-01.json",
            "benchmarks/dogfood/erb/receipts/pool_depth_capture_2026-09-01.sqlite",
            "benchmarks/dogfood/erb/receipts/verify_a1_completeness_2026-09-01.json",
            "benchmarks/dogfood/erb/receipts/verify_a1_recompute_2026-09-01.json",
        ],
        "basis_sha256": {
            "pool_depth_forensics_2026-09-01.json": sha256_file(FORENSICS),
            "pool_depth_capture_2026-09-01.sqlite": sha256_file(CAPTURE),
            "verify_a1_completeness_2026-09-01.json":
                sha256_file(VERIFY_COMPLETENESS),
            "verify_a1_recompute_2026-09-01.json":
                sha256_file(VERIFY_RECOMPUTE),
            "gold_by_needle_v09x.json": sha256_file(GOLD),
            "needles_resolved_v09x_full.json": sha256_file(NEEDLES),
        },
        "kill_criterion": PREREGISTRATION,
        "pre_registration": PREREGISTRATION,
        "population_rule": POPULATION_RULE,
        "population": {"group_A": group_a, "group_B": group_b},
        "method": {
            "query_terms_source": ("the ARM-1 capture's terms table -- the "
                                   "pipeline's OWN expanded terms as passed "
                                   "to query_docs, not a re-extraction"),
            "term_filter": "len(t) > 2 (the BM25 shortlist's own filter)",
            "occurrence_oracle": ("FTS5 phrase match ('\"term\"') against an "
                                  "in-memory fts5 table holding the gold "
                                  "chunk text, created with the same DEFAULT "
                                  "tokenizer as genes_fts"),
            "primary_gold_text": "genes.content for each gold gene_id",
            "secondary_gold_text": ("genes_fts_source (gene_id + source_id + "
                                    "promoter tags + content + complement) "
                                    "-- what BM25 actually indexes; reported "
                                    "as a_terms_in_gold_indexed_text but NOT "
                                    "used for the pre-registered "
                                    "classification"),
            "bm25_sql": ("SELECT gene_id FROM genes_fts WHERE genes_fts "
                         "MATCH ? ORDER BY rank LIMIT ? -- verbatim the "
                         "shortlist SQL at knowledge_store.py:3849-3853, "
                         "LIMIT widened to " + str(DEPTH)),
            "match_construction": ('" OR ".join(\'"\' + t + \'"\') over the '
                                   "RAW captured term list (duplicates "
                                   "preserved), identical to "
                                   "knowledge_store.py:3848"),
            "df_definition": ("document frequency from fts5vocab('row') over "
                              "genes_fts. Single-token terms: exact. "
                              "Multi-token terms (e.g. 'us-west-2', "
                              "'incident/oncall' -> several unicode61 "
                              "tokens): min over constituent token DFs, a "
                              "strict UPPER bound on the phrase DF -- stated "
                              "because it is what the top-3 ranking uses"),
            "n_dropped": N_DROP,
            "depth": DEPTH,
        },
        "tokenizer": {
            "genes_fts_ddl": fts_ddl,
            "resolved": "unicode61 (FTS5 default; no tokenize= clause)",
            "note": tokenizer_note,
        },
        "verdict": verdict,
        "summary": summary,
        "per_needle": per_needle,
        "wall_s_total": round(time.time() - t_start, 1),
    }

    out = (Path(args.out) if args.out
           else RECEIPTS / ("absent_term_overlap_" + args.stamp + ".json"))
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    print("\nVERDICT " + verdict)
    print(json.dumps(summary, indent=2))
    print("receipt -> " + str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

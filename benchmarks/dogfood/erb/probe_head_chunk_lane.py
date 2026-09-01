"""ARM-3 -- HEAD-CHUNK LANE ATTRIBUTION on ERB 947k (2026-09-01).

THE OBSERVATION UNDER TEST (P5, receipt atcap_confound_2026-09-01.json):
  156/156 top-1 winners are the head chunk or the only chunk of their document.
  Among the 142 at-cap top-1 winners: 142 first, 0 middle, vs 37.6 middles
  expected from the corpus at-cap composition (p = 1.1e-19). Across all 1,423
  at-cap seats middles are seated at 0.63x their corpus share. At IDENTICAL
  length (both exactly 4,000 chars) first-chunk seats land at mean seat rank
  5.22 and middle-chunk seats at 9.33.

PRE-REGISTERED KILL CRITERION (written before any first-vs-middle number was
computed; the BM25 replica was validated on an UNLABELLED top-20 list only):

  "KILL if the first-vs-middle rank gap collapses once BM25 term frequency and
   IDF are controlled instead of the kw_cov distinct-term diagnostic. If head
   chunks simply carry the document title/metadata header and therefore have
   genuinely higher tf on the query's key terms, then there is a real CONTENT
   difference and no positional bias to exploit -- that is a clean kill and it
   should be taken, not argued around."

  Operationalised, decided in this order:

  K1 (EARNED-BY-CONTENT).  In the WITHIN-DOCUMENT, at-cap (both chunks exactly
     4,000 chars), same-query paired comparison, the head chunk's true FTS5
     BM25 beats its middle sibling's in >= 60% of pairs, with a two-sided
     binomial sign test p < 0.01 against 50%.  => the ordering advantage is
     earned content, not position.  => KILL.

  K2 (CAUSE NAMED).  Among the pairs the head won under K1, deleting the head
     chunk's leading document header (the first 400 characters of its
     genes.content, ablated EXACTLY -- tf and doc-length D both recomputed)
     drops its BM25 below the middle sibling's in >= 50% of them.  => the
     title/metadata block is the mechanism.  K2 is confirmatory; K1 alone
     kills.

  K3 (SURVIVE).  The lead survives only if EITHER (a) the paired BM25
     comparison is a coin flip -- head wins < 60% of pairs -- while the
     seat-rank / delivered-rank gap persists, OR (b) conditioning on BM25
     (within narrow bm25 bands, selection-free over a deep FTS-only ranking)
     first chunks still out-rank middles in the live eligible ranking.

  INCONCLUSIVE if fewer than 200 within-document at-cap pairs exist across the
  156 miss queries (insufficient power for the sign test).

METHOD
  1. Lane attribution -- read the retrieval code and the live store: which lane
     can even admit a candidate, and are the tag lanes position-blind?
  2. Selection-free ranking -- a deep FTS5-only ranking (LIMIT 2000) per miss
     query, which is NOT conditioned on being seated, answers the P5 author's
     own selection-confound flag.
  3. Exact BM25 -- a bit-exact replica of SQLite FTS5 bm25() (k1=1.2, b=0.75,
     idf = ln((N-n+0.5)/(n+0.5)) with FTS5's non-positive clamp, D summed over
     all indexed columns) validated against the engine's own `rank`, so tf/IDF
     ablations are exact rather than proxied.
  4. Within-document pairing removes document identity, topic, tags, source
     path and length as confounds in one step.

BED CONSTRAINT HONOURED: this probe never compares a gold chunk to its own
non-gold sibling (that set is empty by construction on this bed). The paired
population is corpus-side: any document contributing both a first and a middle
chunk to the same query's FTS match set.

Bed opened READ-ONLY (mode=ro, PRAGMA query_only). No writes, no indexes, no
vacuum.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
import random
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
ERB = ROOT / "benchmarks/dogfood/erb"

BED = "F:/tmp/erb_blob_v09x.db"
MINE = ERB / "receipts/interloper_mine_erb947k_2026-08-31.json"
ATCAP = ERB / "receipts/atcap_confound_2026-09-01.json"
OUT = ERB / "receipts/head_chunk_lane_2026-09-01.json"
CACHE = Path(os.environ.get(
    "ARM3_CACHE",
    "C:/Users/max/AppData/Local/Temp/claude/"
    "F--Projects-cymatix-context--claude-worktrees-wave-1-semantic-ranking-5f1dab/"
    "c81df3a0-0a0c-4160-81ee-e68db3b02372/scratchpad",
)) / "arm3_corpus_cache.pkl"

TOK = re.compile(r"[^\W_]+", re.UNICODE)
K1_BM25, B_BM25 = 1.2, 0.75
DEEP = 2000            # selection-free FTS-only depth
MAX_PAIRS_PER_Q = 40   # bound the exact-ablation work
HEADER_CHARS = 400     # pre-registered document-header ablation window
CAP = 4000             # chunker's fixed chunk cap on this bed

PREREG = {
    "kill_criterion": (
        "KILL if the first-vs-middle rank gap collapses once BM25 term frequency and IDF "
        "are controlled instead of the kw_cov distinct-term diagnostic. If head chunks "
        "simply carry the document title/metadata header and therefore have genuinely "
        "higher tf on the query's key terms, then there is a real CONTENT difference and "
        "no positional bias to exploit -- that is a clean kill and it should be taken, "
        "not argued around."
    ),
    "K1_kill": (
        "WITHIN-DOCUMENT, at-cap (both exactly 4000 chars), same-query paired BM25: head "
        "beats middle sibling in >= 60% of pairs, two-sided binomial sign test p < 0.01 "
        "vs 50% -> advantage is earned content -> KILL."
    ),
    "K2_cause": (
        "Among K1 head-wins, ablating the head's first 400 content characters (exact tf "
        "and D recompute) drops it below the middle sibling in >= 50% of them -> the "
        "title/metadata block is named as the mechanism. Confirmatory only."
    ),
    "K3_survive": (
        "SURVIVE only if (a) head wins < 60% of paired BM25 while the rank gap persists, "
        "OR (b) within narrow BM25 bands over a selection-free deep FTS-only ranking, "
        "first chunks still out-rank middles."
    ),
    "inconclusive_if": "fewer than 200 within-document at-cap pairs across the 156 miss queries",
    "declared_before_results": True,
}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def posclass(pos: int, n: int) -> str:
    if n <= 1:
        return "only"
    if pos == 0:
        return "first"
    if pos == n - 1:
        return "last"
    return "middle"


def binom_two_sided(k: int, n: int, p: float = 0.5) -> float:
    """Exact two-sided binomial test p-value."""
    if n == 0:
        return 1.0
    lg = math.lgamma

    def pmf(i):
        return math.exp(lg(n + 1) - lg(i + 1) - lg(n - i + 1)
                        + i * math.log(p) + (n - i) * math.log(1 - p))
    obs = pmf(k)
    tot = 0.0
    for i in range(n + 1):
        v = pmf(i)
        if v <= obs * (1 + 1e-12):
            tot += v
    return min(1.0, tot)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def main() -> int:
    t_all = time.perf_counter()
    from cymatix_context.accel import extract_query_signals

    git_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                             capture_output=True, text=True).stdout.strip()
    tool = Path(__file__).resolve()

    # CACHE is written by probe_head_lane_cache.py earlier in THIS arm, into the
    # session scratchpad. It is self-generated, never fetched or shared -- the
    # pickle load is not an untrusted-input path.
    cache = pickle.load(open(CACHE, "rb"))
    rowids = cache["rowid"]
    gid_arr = cache["gid"]
    nchars = cache["nchars"]
    pos_arr = cache["pos"]
    nch_arr = cache["n_chunks"]
    src_h = cache["src_h"]
    dom_h = cache["dom_h"]
    ent_h = cache["ent_h"]
    ent_n = cache["ent_n"]
    dl_by_rowid = cache["dl_by_rowid"]
    avgdl = cache["avgdl"]
    N_FTS = cache["n_fts_docs"]
    idx_of_rowid = {r: i for i, r in enumerate(rowids)}
    idx_of_gid = {g: i for i, g in enumerate(gid_arr)}

    con = sqlite3.connect("file:%s?mode=ro" % BED, uri=True)
    con.execute("PRAGMA query_only=1")
    cur = con.cursor()

    mine = json.loads(MINE.read_text(encoding="utf-8"))
    misses = mine["misses"]
    gold_by = json.loads((ERB / "gold_by_needle_v09x.json").read_text(encoding="utf-8"))

    out = {
        "stamp": "2026-09-01",
        "arm": "ARM 3 -- head-chunk lane attribution",
        "tool": "benchmarks/dogfood/erb/probe_head_chunk_lane.py",
        "tool_sha256": sha256(tool),
        "helper_tools": {
            "benchmarks/dogfood/erb/probe_head_lane_cache.py":
                sha256(ERB / "probe_head_lane_cache.py"),
            "benchmarks/dogfood/erb/probe_head_lane_live.py":
                sha256(ERB / "probe_head_lane_live.py"),
        },
        "git_sha": git_sha,
        "bed": BED,
        "bed_bytes": os.path.getsize(BED),
        "bed_access": "read-only (mode=ro, PRAGMA query_only); 2 cached full scans + FTS reads",
        "basis_receipts": [
            "benchmarks/dogfood/erb/receipts/interloper_mine_erb947k_2026-08-31.json",
            "benchmarks/dogfood/erb/receipts/ladder_v09x_w24_min_delivered_2026-08-28.json",
            "benchmarks/dogfood/erb/receipts/atcap_confound_2026-09-01.json",
            "benchmarks/dogfood/erb/receipts/bucket_ledger_2026-09-01.json",
        ],
        "prereg": PREREG,
        "n_misses": len(misses),
        "n_corpus_chunks": len(gid_arr),
        "avgdl": round(avgdl, 6),
        "n_fts_docs": N_FTS,
    }

    # -- S1  LANE INVENTORY / ADMISSION GATE --------------------------------
    fts_view = cur.execute(
        "SELECT sql FROM sqlite_master WHERE name='genes_fts_source'").fetchone()[0]
    out["section_1_lane_inventory"] = {
        "genes_fts_ddl": cur.execute(
            "SELECT sql FROM sqlite_master WHERE name='genes_fts'").fetchone()[0],
        "genes_fts_source_view": fts_view,
        "finding_fts_text_is_not_raw_content": (
            "genes_fts.content is NOT genes.content: the external-content view splices "
            "source_id || ' ' || GROUP_CONCAT(promoter_index.tag_value) || ' ' || "
            "genes.content. Every chunk is therefore indexed with its document's source "
            "PATH and its promoter tags prepended -- a doc-level prefix identical across "
            "siblings except for the per-chunk entity tags."
        ),
        "admission_gate": (
            "[retrieval] bm25_shortlist_enabled = true, bm25_shortlist_size = 50 "
            "(RetrievalConfig defaults). knowledge_store.py post-filters gene_scores AND "
            "tier_contrib to the FTS5 top-50 bm25 list BEFORE the RRF sort, so nothing "
            "outside FTS5's top-50 can be delivered whatever the tag lanes scored."
        ),
        "rrf_tie_rule": (
            "retrieval/fusion.py: every tier is re-sorted by (score desc, gene_id asc). "
            "gene_id is a content hash, so RRF tie-order is position-BLIND. The only "
            "position-sensitive tie in the pipeline is SQLite's own ORDER BY rank "
            "truncation, which breaks bm25 ties by rowid ascending (= head chunk first)."
        ),
    }

    # -- S2  TAG-LANE POSITION-BLINDNESS ------------------------------------
    by_src = defaultdict(list)
    for i, s in enumerate(src_h):
        by_src[s].append(i)
    multi = [idxs for idxs in by_src.values() if len(idxs) > 1]
    same_dom = sum(1 for idxs in multi if len({dom_h[i] for i in idxs}) == 1)
    same_ent = sum(1 for idxs in multi if len({ent_h[i] for i in idxs}) == 1)
    ent_by_pc = defaultdict(list)
    for i in range(len(gid_arr)):
        ent_by_pc[posclass(pos_arr[i], nch_arr[i])].append(ent_n[i])
    out["section_2_tag_lane_position_blindness"] = {
        "n_multi_chunk_sources": len(multi),
        "n_with_identical_promoter_domains_across_all_chunks": same_dom,
        "pct_identical_domains": round(100.0 * same_dom / len(multi), 4) if multi else None,
        "n_with_identical_promoter_entities_across_all_chunks": same_ent,
        "pct_identical_entities": round(100.0 * same_ent / len(multi), 4) if multi else None,
        "mean_n_entities_by_posclass": {k: mean(v) for k, v in sorted(ent_by_pc.items())},
        "reading": (
            "promoter.domains (the document slug + doc-level tags) are the tag lanes' "
            "high-weight keys and are byte-identical across every chunk of a document, so "
            "tag_exact / tag_prefix / lex_anchor cannot prefer a head chunk. "
            "promoter.entities ARE per-chunk, but they are extracted FROM the chunk's own "
            "text -- a content difference, not a position rule."
        ),
    }

    # -- S3  DOCUMENT-HEADER CENSUS BY POSITION CLASS -----------------------
    random.seed(20260901)
    strata = {"only": [], "first": [], "middle": [], "last": []}
    for i in range(len(gid_arr)):
        strata[posclass(pos_arr[i], nch_arr[i])].append(i)
    hdr = {}
    for pc, idxs in strata.items():
        samp = random.sample(idxs, min(6000, len(idxs)))
        rs = [rowids[i] for i in samp]
        starts_json = has_title = got = 0
        for j in range(0, len(rs), 400):
            ch = rs[j:j + 400]
            ph = ",".join("?" * len(ch))
            for (txt,) in cur.execute(
                    "SELECT substr(content,1,%d) FROM genes WHERE rowid IN (%s)"
                    % (HEADER_CHARS, ph), ch):
                got += 1
                t = (txt or "").lstrip()
                if t[:1] in ("{", "["):
                    starts_json += 1
                if ('"title"' in t or '"subject"' in t or '"key"' in t
                        or '"name"' in t):
                    has_title += 1
        hdr[pc] = {
            "n_sampled": got,
            "pct_content_starts_with_json_brace":
                round(100.0 * starts_json / got, 2) if got else None,
            "pct_first_%dch_carries_a_title_key" % HEADER_CHARS:
                round(100.0 * has_title / got, 2) if got else None,
        }
    out["section_3_document_header_census"] = {
        "method": ("stratified random sample of up to 6,000 chunks per position class; "
                   "inspect the first %d characters of genes.content" % HEADER_CHARS),
        "by_posclass": hdr,
    }

    # -- S4..S8  PER-QUERY FTS WORK -----------------------------------------
    df_cache = {}

    def idf_of(term: str) -> float:
        if term in df_cache:
            return df_cache[term]
        try:
            n = cur.execute("SELECT count(*) FROM genes_fts WHERE genes_fts MATCH ?",
                            ('"%s"' % term,)).fetchone()[0]
        except Exception:
            n = 0
        v = math.log((N_FTS - n + 0.5) / (n + 0.5))
        v = v if v > 0 else 1e-6
        df_cache[term] = v
        return v

    def bm25_from_tokens(toks, grams_idf, D):
        s = 0.0
        L = len(toks)
        for gram, idf in grams_idf:
            g = len(gram)
            if g == 0 or g > L:
                continue
            if g == 1:
                tf = toks.count(gram[0])
            else:
                tf = 0
                g0 = gram[0]
                for i in range(L - g + 1):
                    if toks[i] == g0 and tuple(toks[i:i + g]) == gram:
                        tf += 1
            if tf:
                s += idf * (tf * (K1_BM25 + 1.0)) / (
                    tf + K1_BM25 * (1.0 - B_BM25 + B_BM25 * D / avgdl))
        return -s

    depth_bands = [(1, 12), (13, 50), (51, 200), (201, 500), (501, 2000)]
    band_comp = {("%d-%d" % b): Counter() for b in depth_bands}
    matched_comp = Counter()
    atcap_deep = {"first": {"bm25": [], "rank": []}, "middle": {"bm25": [], "rank": []}}
    pairs_all = []
    tie_stats = {"n_rows_examined": 0, "n_rows_in_a_bm25_tie": 0,
                 "n_tied_groups": 0, "n_tied_groups_mixed_posclass": 0}
    replica_check = {"n_rows": 0, "max_abs_delta": 0.0}
    per_query = []
    gold_deep = []
    pair_fail = {"no_content_anchor": 0}

    t0 = time.perf_counter()
    for qi, m in enumerate(misses):
        q = m["query"]
        d, e = extract_query_signals(q)
        qterms = list(d) + list(e)
        terms = [t for t in qterms if len(t) > 2]
        if not terms:
            continue
        fq = " OR ".join('"%s"' % t for t in terms)
        try:
            deep = cur.execute(
                "SELECT rowid, gene_id, rank FROM genes_fts WHERE genes_fts MATCH ? "
                "ORDER BY rank LIMIT ?", (fq, DEEP)).fetchall()
        except Exception as exc:
            per_query.append({"needle": m["needle"], "error": str(exc)})
            continue
        grams_idf = []
        for t in set(terms):
            g = tuple(TOK.findall(t.lower()))
            if g:
                grams_idf.append((g, idf_of(t)))

        for rnk, (rid, gid, sc) in enumerate(deep, 1):
            i = idx_of_rowid.get(rid)
            if i is None:
                continue
            pc = posclass(pos_arr[i], nch_arr[i])
            matched_comp[pc] += 1
            for b in depth_bands:
                if b[0] <= rnk <= b[1]:
                    band_comp["%d-%d" % b][pc] += 1
                    break
            if nchars[i] == CAP and pc in ("first", "middle"):
                atcap_deep[pc]["bm25"].append(-sc)
                atcap_deep[pc]["rank"].append(rnk)

        by_score = defaultdict(list)
        for rid, gid, sc in deep:
            by_score[sc].append(rid)
        for sc, rs in by_score.items():
            if len(rs) > 1:
                tie_stats["n_tied_groups"] += 1
                tie_stats["n_rows_in_a_bm25_tie"] += len(rs)
                pcs = {posclass(pos_arr[idx_of_rowid[r]], nch_arr[idx_of_rowid[r]])
                       for r in rs if r in idx_of_rowid}
                if len(pcs) > 1:
                    tie_stats["n_tied_groups_mixed_posclass"] += 1
        tie_stats["n_rows_examined"] += len(deep)

        gold = set(gold_by.get(m["needle"]) or [])
        gd_rank = gd_pc = None
        for rnk, (rid, gid, sc) in enumerate(deep, 1):
            if gid in gold:
                gd_rank = rnk
                i = idx_of_rowid.get(rid)
                gd_pc = posclass(pos_arr[i], nch_arr[i]) if i is not None else None
                break
        gold_deep.append({"needle": m["needle"], "deep_fts_rank_of_first_gold": gd_rank,
                          "gold_posclass": gd_pc})

        by_doc = defaultdict(lambda: {"first": [], "middle": []})
        for rnk, (rid, gid, sc) in enumerate(deep, 1):
            i = idx_of_rowid.get(rid)
            if i is None or nchars[i] != CAP:
                continue
            pc = posclass(pos_arr[i], nch_arr[i])
            if pc in ("first", "middle"):
                by_doc[src_h[i]][pc].append((rid, gid, sc, rnk, i))
        cands = [(s, v) for s, v in by_doc.items() if v["first"] and v["middle"]]
        random.shuffle(cands)
        cands = cands[:MAX_PAIRS_PER_Q]
        need = set()
        for _s, v in cands:
            need.add(v["first"][0][0])
            for mm in v["middle"]:
                need.add(mm[0])
        texts, raw_content = {}, {}
        nl = sorted(need)
        for j in range(0, len(nl), 300):
            ch = nl[j:j + 300]
            ph = ",".join("?" * len(ch))
            for rid, g_, c_, cp_ in cur.execute(
                    "SELECT rowid, gene_id, content, complement FROM genes_fts_source "
                    "WHERE rowid IN (%s)" % ph, ch):
                texts[rid] = TOK.findall(
                    ((g_ or "") + " " + (c_ or "") + " " + (cp_ or "")).lower())
            for rid, c_ in cur.execute(
                    "SELECT rowid, content FROM genes WHERE rowid IN (%s)" % ph, ch):
                raw_content[rid] = c_ or ""

        for _s, v in cands:
            f_rid, f_gid, f_sc, f_rnk, f_i = v["first"][0]
            mm = min(v["middle"], key=lambda x: x[2])   # strongest middle sibling
            m_rid, m_gid, m_sc, m_rnk, m_i = mm
            if f_rid not in texts or m_rid not in texts:
                continue
            for rid, engine in ((f_rid, f_sc), (m_rid, m_sc)):
                rep = bm25_from_tokens(texts[rid], grams_idf, dl_by_rowid[rid])
                replica_check["n_rows"] += 1
                replica_check["max_abs_delta"] = max(
                    replica_check["max_abs_delta"], abs(rep - engine))
            rc = raw_content.get(f_rid, "")
            content_toks = TOK.findall(rc.lower())
            n_content = len(content_toks)
            toks = texts[f_rid]
            n_hdr = len(TOK.findall(rc[:HEADER_CHARS].lower()))
            start = None
            approx = len(toks) - n_content
            for cand in range(max(0, approx - 6), approx + 7):
                if cand >= 0 and toks[cand:cand + n_content] == content_toks:
                    start = cand
                    break
            if start is None:
                pair_fail["no_content_anchor"] += 1
                continue
            abl = toks[:start] + toks[start + n_hdr:]
            f_abl = bm25_from_tokens(abl, grams_idf, dl_by_rowid[f_rid] - n_hdr)
            pairs_all.append({
                "needle": m["needle"],
                "head_gid": f_gid, "mid_gid": m_gid,
                "head_bm25": -f_sc, "mid_bm25": -m_sc,
                "head_bm25_no_header": -f_abl,
                "head_deep_rank": f_rnk, "mid_deep_rank": m_rnk,
                "n_header_tokens": n_hdr,
                "head_dl": dl_by_rowid[f_rid], "mid_dl": dl_by_rowid[m_rid],
            })

        per_query.append({
            "needle": m["needle"], "n_terms": len(terms), "n_deep": len(deep),
            "n_pairs": len(cands),
            "deep_fts_rank_of_first_gold": gd_rank,
        })
        if (qi + 1) % 20 == 0:
            print("q %d/%d  %.0fs  pairs=%d" % (
                qi + 1, len(misses), time.perf_counter() - t0, len(pairs_all)), flush=True)

    corpus_pc = Counter()
    for i in range(len(gid_arr)):
        corpus_pc[posclass(pos_arr[i], nch_arr[i])] += 1
    tot_c = sum(corpus_pc.values())
    tot_m = sum(matched_comp.values()) or 1
    out["section_4_selection_free_depth_composition"] = {
        "method": ("per miss query, a deep FTS5-ONLY ranking (ORDER BY rank LIMIT %d). "
                   "Not conditioned on being seated -- answers the P5 author's selection "
                   "flag directly." % DEEP),
        "corpus_share_pct": {k: round(100.0 * v / tot_c, 2) for k, v in corpus_pc.items()},
        "matched_pool_share_pct": {k: round(100.0 * v / tot_m, 2)
                                   for k, v in matched_comp.items()},
        "by_depth_band_pct": {
            band: {k: round(100.0 * v / sum(c.values()), 2) for k, v in c.items()}
            for band, c in band_comp.items() if sum(c.values())},
        "by_depth_band_counts": {b: dict(c) for b, c in band_comp.items()},
    }
    out["section_4b_atcap_first_vs_middle_unconditioned"] = {
        "note": "at-cap (exactly 4000 chars) chunks only, so length is held constant",
        "first": {"n": len(atcap_deep["first"]["bm25"]),
                  "mean_bm25": mean(atcap_deep["first"]["bm25"]),
                  "mean_deep_fts_rank": mean(atcap_deep["first"]["rank"])},
        "middle": {"n": len(atcap_deep["middle"]["bm25"]),
                   "mean_bm25": mean(atcap_deep["middle"]["bm25"]),
                   "mean_deep_fts_rank": mean(atcap_deep["middle"]["rank"])},
    }

    n_pairs = len(pairs_all)
    head_win = sum(1 for p in pairs_all if p["head_bm25"] > p["mid_bm25"])
    ties = sum(1 for p in pairs_all if p["head_bm25"] == p["mid_bm25"])
    n_dec = n_pairs - ties
    p_val = binom_two_sided(head_win, n_dec) if n_dec else 1.0
    deltas = [p["head_bm25"] - p["mid_bm25"] for p in pairs_all]
    won = [p for p in pairs_all if p["head_bm25"] > p["mid_bm25"]]
    flipped = [p for p in won if p["head_bm25_no_header"] < p["mid_bm25"]]
    out["section_5_within_document_paired_bm25"] = {
        "design": ("same query, same parent document, both chunks exactly %d chars; the "
                   "head is paired against its STRONGEST middle sibling (hardest test for "
                   "the head)." % CAP),
        "n_pairs": n_pairs,
        "n_ties": ties,
        "n_decided": n_dec,
        "head_wins": head_win,
        "head_win_pct": round(100.0 * head_win / n_dec, 2) if n_dec else None,
        "binomial_two_sided_p_vs_50pct": p_val,
        "mean_bm25_delta_head_minus_mid": mean(deltas),
        "median_bm25_delta": round(sorted(deltas)[len(deltas) // 2], 4) if deltas else None,
        "mean_head_bm25": mean(p["head_bm25"] for p in pairs_all),
        "mean_mid_bm25": mean(p["mid_bm25"] for p in pairs_all),
        "mean_head_deep_rank": mean(p["head_deep_rank"] for p in pairs_all),
        "mean_mid_deep_rank": mean(p["mid_deep_rank"] for p in pairs_all),
        "n_pairs_dropped_no_content_anchor": pair_fail["no_content_anchor"],
    }
    out["section_6_header_ablation_K2"] = {
        "design": ("delete the first %d characters of the head chunk's genes.content and "
                   "recompute BM25 EXACTLY (tf and doc length D both adjusted), then "
                   "re-run the same paired comparison." % HEADER_CHARS),
        "n_head_wins": len(won),
        "n_head_wins_that_flip_when_header_removed": len(flipped),
        "pct_flipped": round(100.0 * len(flipped) / len(won), 2) if won else None,
        "mean_header_tokens": mean(p["n_header_tokens"] for p in pairs_all),
        "mean_head_bm25_with_header": mean(p["head_bm25"] for p in won),
        "mean_head_bm25_without_header": mean(p["head_bm25_no_header"] for p in won),
        "mean_mid_bm25_in_those_pairs": mean(p["mid_bm25"] for p in won),
    }
    out["section_7_bm25_replica_validation"] = {
        "n_rows_checked": replica_check["n_rows"],
        "max_abs_delta_vs_sqlite_bm25": replica_check["max_abs_delta"],
        "tokenizer_validation": "300/300 sampled rows reproduce genes_fts_docsize exactly",
        "status": "BIT-EXACT" if replica_check["max_abs_delta"] < 1e-9 else "DRIFT",
    }
    out["section_8_tie_order"] = dict(
        tie_stats,
        pct_rows_in_a_tie=round(100.0 * tie_stats["n_rows_in_a_bm25_tie"]
                                / max(1, tie_stats["n_rows_examined"]), 3),
        reading=("the only position-sensitive tie in the pipeline is SQLite's ORDER BY "
                 "rank tie-break (rowid ascending = head first). Its size bounds any "
                 "position bias that is NOT a bm25 difference."),
    )

    gold_mid_tail = 0
    for m in misses:
        gf = m.get("gold_first") or {}
        i = idx_of_gid.get(gf.get("gene_id"))
        if i is None:
            continue
        if posclass(pos_arr[i], nch_arr[i]) in ("middle", "last"):
            gold_mid_tail += 1
    reach = [g for g in gold_deep if g["deep_fts_rank_of_first_gold"] is not None]
    out["section_9_addressable_population"] = {
        "n_misses": len(misses),
        "n_misses_gold_first_is_middle_or_tail": gold_mid_tail,
        "n_misses_gold_reachable_in_deep_fts_%d" % DEEP: len(reach),
        "deep_fts_rank_of_first_gold_hist": dict(Counter(
            ("1-12" if g["deep_fts_rank_of_first_gold"] <= 12 else
             "13-50" if g["deep_fts_rank_of_first_gold"] <= 50 else
             "51-200" if g["deep_fts_rank_of_first_gold"] <= 200 else
             "201-2000")
            for g in reach)),
        "gold_posclass_when_reachable": dict(Counter(g["gold_posclass"] for g in reach)),
    }

    out["per_query"] = per_query
    out["pairs_sample"] = pairs_all[:200]
    out["gold_deep"] = gold_deep
    out["elapsed_seconds"] = round(time.perf_counter() - t_all, 1)
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("wrote", OUT, flush=True)
    print(json.dumps({k: out[k] for k in (
        "section_2_tag_lane_position_blindness",
        "section_3_document_header_census",
        "section_4_selection_free_depth_composition",
        "section_4b_atcap_first_vs_middle_unconditioned",
        "section_5_within_document_paired_bm25",
        "section_6_header_ablation_K2",
        "section_7_bm25_replica_validation",
        "section_8_tie_order",
        "section_9_addressable_population")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

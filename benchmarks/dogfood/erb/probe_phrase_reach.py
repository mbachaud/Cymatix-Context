"""P1 -- phrase / NEAR reachability on ERB 947k, WITH a matched hit-control arm.

The decisive gate for the proposed "exact-phrase + NEAR lane fused into RRF".
Two separate questions, answered separately:

  Q1 REACHABLE      -- does a selective exact phrase drawn from the query text
                       actually land on a gold chunk for the misses?
  Q2 DISCRIMINATIVE -- does that lane fire materially MORE often on misses
                       than on needles the baseline already delivers? A lane
                       that fires everywhere adds candidates everywhere and
                       cannot move delivered_gold_rate.

The bed is opened READ-ONLY (mode=ro). No writes, no indexes, no VACUUM.

Run:
    python benchmarks/dogfood/erb/probe_phrase_reach.py
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import random
import re
import sqlite3
import statistics
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ERB = HERE
RECEIPTS = os.path.join(ERB, "receipts")
DOGFOOD = os.path.dirname(ERB)

BED = "F:/tmp/erb_blob_v09x.db"
STAMP = "2026-09-01"
SEED = 20260901
DF_CAP = 5000          # phrases above this DF are "non-selective"
NEAR_SPAN = 10
TOPFEW = 3             # "top few most selective phrases" practical-lane variant

LEDGER_PATH = os.path.join(RECEIPTS, "bucket_ledger_%s.json" % STAMP)
BASE_PATH = os.path.join(RECEIPTS, "ladder_v09x_w24_min_delivered_2026-08-28.json")
NEEDLES_PATH = os.path.join(ERB, "needles_resolved_v09x_full.json")
GOLD_PATH = os.path.join(ERB, "gold_by_needle_v09x.json")
OUT_PATH = os.path.join(RECEIPTS, "phrase_reach_%s.json" % STAMP)

# --- kill criterion, stated BEFORE any result is looked at -----------------
KILL_CRITERION = (
    "KILL if (a) exact-phrase probes return a gold chunk for well under half "
    "of the 37 basic pool-absent misses, OR (b) the phrase-fire rate on "
    "MISSES is not materially higher than on a matched sample of HITS -- a "
    "lane that fires equally on both cannot discriminate and cannot move "
    "delivered_gold_rate. "
    "Operationalised before running: (a) fires if the reach rate on the 37 "
    "basic pool-absent misses < 0.40 ('well under half'); 0.40-0.50 reads as "
    "'about half' and does not by itself fire (a). (b) fires if "
    "miss_reach_rate - matched_hit_reach_rate < 0.10 absolute ('not "
    "materially higher'). Verdict is KILL if either limb fires, PASS only if "
    "neither fires, INCONCLUSIVE if the inputs cannot support the measurement."
)
KILL_A_THRESHOLD = 0.40
KILL_B_MIN_DELTA = 0.10

# --- deterministic tokenisation -------------------------------------------
TOKEN_RE = re.compile(r"[^0-9a-z]+")

STOPLIST = sorted({
    "a", "about", "after", "all", "also", "am", "an", "and", "any", "are", "as",
    "at", "be", "been", "before", "being", "between", "but", "by", "can",
    "could", "did", "do", "does", "doing", "during", "each", "for", "from",
    "had", "has", "have", "he", "her", "his", "how", "i", "if", "in", "into",
    "is", "it", "its", "just", "may", "me", "might", "must", "my", "no", "not",
    "of", "on", "only", "or", "other", "our", "over", "own", "same", "she",
    "should", "so", "some", "such", "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "to", "too", "under", "us",
    "very", "was", "we", "were", "what", "when", "where", "which", "while",
    "who", "whom", "why", "will", "with", "would", "you", "your",
})
STOP = set(STOPLIST)

NGRAM_MIN, NGRAM_MAX = 2, 6


def tokenize(query):
    """lowercase -> split on non-word chars -> drop empties. Order preserved."""
    return [t for t in TOKEN_RE.split(query.lower()) if t]


def ngrams_reading_b(tokens):
    """PRIMARY reading. Contiguous n-grams over the ORIGINAL token order
    (stopwords retained in place), keeping only n-grams that contain >=1
    non-stopword content term. Every emitted phrase really occurs in the query
    text; an exact-phrase probe built from a stopword-DELETED stream would test
    phrases that occur in no document and would bias this probe toward KILL."""
    out = []
    seen = set()
    for n in range(NGRAM_MIN, NGRAM_MAX + 1):
        for i in range(0, len(tokens) - n + 1):
            gram = tokens[i:i + n]
            if not any(t not in STOP for t in gram):
                continue
            p = " ".join(gram)
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def ngrams_reading_a(tokens):
    """SECONDARY sensitivity arm. Stopwords deleted first, then contiguous
    n-grams over the reduced sequence (order still original)."""
    red = [t for t in tokens if t not in STOP]
    out = []
    seen = set()
    for n in range(NGRAM_MIN, NGRAM_MAX + 1):
        for i in range(0, len(red) - n + 1):
            p = " ".join(red[i:i + n])
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def fts_phrase(p):
    """FTS5 MATCH expression for an exact phrase over content+complement.
    Embedded double quotes are doubled (defensive: the tokenizer cannot emit
    one, but a malformed expression must never crash the run)."""
    return '{content complement}: "%s"' % p.replace('"', '""')


def fts_near(t1, t2, span):
    return '{content complement}: NEAR("%s" "%s", %d)' % (
        t1.replace('"', '""'), t2.replace('"', '""'), span)


class Bed(object):
    def __init__(self, path):
        self.con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        self.df_cache = {}
        self.errors = []
        self.df_calls = 0
        self.member_calls = 0

    def df(self, expr):
        """Document frequency of an FTS5 expression. None on malformed query."""
        if expr in self.df_cache:
            return self.df_cache[expr]
        try:
            n = self.con.execute(
                "SELECT count(*) FROM genes_fts WHERE genes_fts MATCH ?", (expr,)
            ).fetchone()[0]
        except Exception as exc:                      # noqa: BLE001 - logged, never fatal
            self.errors.append({"kind": "df", "expr": expr, "error": repr(exc)})
            n = None
        self.df_calls += 1
        self.df_cache[expr] = n
        return n

    def matches_rowid(self, expr, rowid):
        """Does this specific row match the expression? Exact, index-driven."""
        self.member_calls += 1
        try:
            r = self.con.execute(
                "SELECT 1 FROM genes_fts WHERE genes_fts MATCH ? AND rowid = ?",
                (expr, rowid),
            ).fetchone()
        except Exception as exc:                      # noqa: BLE001
            self.errors.append({"kind": "member", "expr": expr, "rowid": rowid,
                                "error": repr(exc)})
            return False
        return r is not None

    def rowids_for(self, gene_ids):
        out = {}
        for gid in gene_ids:
            r = self.con.execute(
                "SELECT rowid FROM genes WHERE gene_id = ?", (gid,)
            ).fetchone()
            if r is not None:
                out[gid] = r[0]
        return out


def quartiles(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None

    def q(p):
        if len(xs) == 1:
            return float(xs[0])
        i = p * (len(xs) - 1)
        lo = int(i)
        hi = min(lo + 1, len(xs) - 1)
        return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)

    return {"n": len(xs), "min": xs[0], "q1": round(q(0.25), 4),
            "median": round(q(0.5), 4), "q3": round(q(0.75), 4), "max": xs[-1],
            "mean": round(statistics.fmean(xs), 4)}


def rate(num, den):
    return round(num / float(den), 4) if den else None


def measure(bed, name, query, gold_rowids):
    tokens = tokenize(query)
    content_terms = [t for t in dict.fromkeys(tokens) if t not in STOP]

    term_df = {}
    for t in content_terms:
        term_df[t] = bed.df(fts_phrase(t))
    nz_term_df = [v for v in term_df.values() if v]
    min_term_df = min(nz_term_df) if nz_term_df else None

    rec = {
        "needle": name,
        "n_tokens": len(tokens),
        "n_content_terms": len(content_terms),
        "min_content_term_df": min_term_df,
        "n_gold": len(gold_rowids),
    }

    for arm_name, phrases in (("b", ngrams_reading_b(tokens)),
                              ("a", ngrams_reading_a(tokens))):
        scored = []
        n_err = 0
        for p in phrases:
            d = bed.df(fts_phrase(p))
            if d is None:
                n_err += 1
                continue
            scored.append((d, p))
        scored.sort(key=lambda x: (x[0], x[1]))
        nonzero = [(d, p) for d, p in scored if d >= 1]
        eligible = [(d, p) for d, p in nonzero if d <= DF_CAP]

        reach_elig = None      # lowest-DF eligible phrase landing on a gold chunk
        for d, p in eligible:
            expr = fts_phrase(p)
            landed = None
            for gid, rid in gold_rowids.items():
                if bed.matches_rowid(expr, rid):
                    landed = gid
                    break
            if landed is not None:
                reach_elig = (d, p, landed)
                break

        reach_any = reach_elig
        if reach_any is None:
            for d, p in nonzero:
                if d <= DF_CAP:
                    continue
                expr = fts_phrase(p)
                landed = None
                for gid, rid in gold_rowids.items():
                    if bed.matches_rowid(expr, rid):
                        landed = gid
                        break
                if landed is not None:
                    reach_any = (d, p, landed)
                    break

        reach_topfew = None
        for d, p in eligible[:TOPFEW]:
            expr = fts_phrase(p)
            landed = None
            for gid, rid in gold_rowids.items():
                if bed.matches_rowid(expr, rid):
                    landed = gid
                    break
            if landed is not None:
                reach_topfew = (d, p, landed)
                break

        rec["arm_%s" % arm_name] = {
            "n_phrases": len(phrases),
            "n_phrase_errors": n_err,
            "n_df0": sum(1 for d, _ in scored if d == 0),
            "n_eligible": len(eligible),
            "n_over_cap": sum(1 for d, _ in nonzero if d > DF_CAP),
            "min_phrase_df_all": scored[0][0] if scored else None,
            "min_phrase_df_nonzero": nonzero[0][0] if nonzero else None,
            "most_selective_nonzero_phrase": nonzero[0][1] if nonzero else None,
            "reached_eligible": bool(reach_elig),
            "reach_df": reach_elig[0] if reach_elig else None,
            "reach_phrase": reach_elig[1] if reach_elig else None,
            "reach_gold_id": reach_elig[2] if reach_elig else None,
            "reached_any_df": bool(reach_any),
            "reach_any_df_value": reach_any[0] if reach_any else None,
            "reached_topfew": bool(reach_topfew),
            "reach_topfew_df": reach_topfew[0] if reach_topfew else None,
            "selectivity_ratio": (
                round(min_term_df / float(nonzero[0][0]), 4)
                if (min_term_df and nonzero and nonzero[0][0]) else None
            ),
        }

    ranked_terms = sorted(((v, t) for t, v in term_df.items() if v),
                          key=lambda x: (x[0], x[1]))
    near = {"built": False, "terms": None, "df": None, "reached": None,
            "reached_gold_id": None, "term_dfs": None}
    if len(ranked_terms) >= 2:
        t1, t2 = ranked_terms[0][1], ranked_terms[1][1]
        expr = fts_near(t1, t2, NEAR_SPAN)
        d = bed.df(expr)
        near["built"] = True
        near["terms"] = [t1, t2]
        near["term_dfs"] = [ranked_terms[0][0], ranked_terms[1][0]]
        near["df"] = d
        if d is not None:
            near["reached"] = False
            for gid, rid in gold_rowids.items():
                if bed.matches_rowid(expr, rid):
                    near["reached"] = True
                    near["reached_gold_id"] = gid
                    break
    rec["near"] = near
    return rec


def summarise(records, arm="b"):
    n = len(records)
    if n == 0:
        return {"n": 0}
    key = "arm_%s" % arm
    reach = [r[key]["reached_eligible"] for r in records]
    topfew = [r[key]["reached_topfew"] for r in records]
    anydf = [r[key]["reached_any_df"] for r in records]
    dfs = [r[key]["min_phrase_df_nonzero"] for r in records]
    rdfs = [r[key]["reach_df"] for r in records if r[key]["reach_df"] is not None]
    ratios = [r[key]["selectivity_ratio"] for r in records]
    term_dist = quartiles([r["min_content_term_df"] for r in records])
    phrase_dist = quartiles(dfs)
    near_built = [r for r in records if r["near"]["built"] and r["near"]["df"] is not None]
    near_hit = sum(1 for r in near_built if r["near"]["reached"])
    return {
        "n": n,
        "reach_eligible": sum(reach),
        "reach_rate": rate(sum(reach), n),
        "reach_topfew": sum(topfew),
        "reach_topfew_rate": rate(sum(topfew), n),
        "reach_any_df": sum(anydf),
        "reach_any_df_rate": rate(sum(anydf), n),
        "median_most_selective_phrase_df": phrase_dist["median"] if phrase_dist else None,
        "most_selective_phrase_df_dist": phrase_dist,
        "reaching_phrase_df_dist": quartiles(rdfs),
        "selectivity_ratio_dist": quartiles(ratios),
        "median_min_content_term_df": term_dist["median"] if term_dist else None,
        "near_built": len(near_built),
        "near_reach": near_hit,
        "near_reach_rate": rate(near_hit, len(near_built)),
        "near_df_dist": quartiles([r["near"]["df"] for r in near_built]),
    }


NEAR_KEYS = ["near_built", "near_reach", "near_reach_rate", "near_df_dist"]


def df_bands(records, arm="b"):
    bands = collections.OrderedDict((b, 0) for b in
                                    ["<=12", "13-50", "51-200", "201-1000",
                                     "1001-5000", "no_reach"])
    for r in records:
        d = r["arm_%s" % arm]["reach_df"]
        if d is None:
            bands["no_reach"] += 1
        elif d <= 12:
            bands["<=12"] += 1
        elif d <= 50:
            bands["13-50"] += 1
        elif d <= 200:
            bands["51-200"] += 1
        elif d <= 1000:
            bands["201-1000"] += 1
        else:
            bands["1001-5000"] += 1
    return dict(bands)


def main():
    t_start = time.time()
    git_sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, cwd=HERE).stdout.strip()

    needles = json.load(open(NEEDLES_PATH, encoding="utf-8"))["needles"]
    gold = json.load(open(GOLD_PATH, encoding="utf-8"))
    ledger = json.load(open(LEDGER_PATH, encoding="utf-8"))
    base = json.load(open(BASE_PATH, encoding="utf-8"))

    qtype = dict((n["name"], n["question_type"]) for n in needles)
    query_of = dict((n["name"], n["query"]) for n in needles)
    bucket_of = dict((m["needle"], m["bucket"]) for m in ledger["per_miss"])
    chunk_split_of = dict((m["needle"], m["chunk_split"]) for m in ledger["per_miss"])

    pq = base["arms"][0]["per_query"]
    delivered = dict((r["needle"], r.get("delivered_gold", 0)) for r in pq)
    miss_names = sorted([k for k, v in delivered.items() if not v])
    hit_names = sorted([k for k, v in delivered.items() if v])
    assert len(miss_names) == 156, len(miss_names)
    assert set(miss_names) == set(bucket_of), "miss set disagrees with P0 ledger"

    miss_mix = collections.Counter(qtype[m] for m in miss_names)
    hits_by_type = collections.defaultdict(list)
    for h in hit_names:
        hits_by_type[qtype[h]].append(h)
    rng = random.Random(SEED)
    sample = []
    shortfall = {}
    for t in sorted(miss_mix):
        want = miss_mix[t]
        pool = sorted(hits_by_type.get(t, []))
        rng.shuffle(pool)
        take = pool[:want]
        sample.extend(take)
        if len(take) < want:
            shortfall[t] = {"wanted": want, "available": len(pool)}
    sample = sorted(sample)
    sample_set = set(sample)
    sample_sha = hashlib.sha256("\n".join(sample).encode("utf-8")).hexdigest()[:16]

    bed = Bed(BED)
    bed_bytes = os.path.getsize(BED)

    all_names = miss_names + hit_names
    gold_resolution = {}
    records = {}
    for i, name in enumerate(all_names, 1):
        gids = gold.get(name, [])
        rids = bed.rowids_for(gids)
        gold_resolution[name] = {"n_gold": len(gids), "n_rowids": len(rids)}
        records[name] = measure(bed, name, query_of[name], rids)
        if i % 25 == 0:
            sys.stderr.write("  %d/%d  %.0fs  df=%d member=%d\n" %
                             (i, len(all_names), time.time() - t_start,
                              bed.df_calls, bed.member_calls))
            sys.stderr.flush()

    miss_recs = [records[n] for n in miss_names]
    hit_recs = [records[n] for n in hit_names]
    samp_recs = [records[n] for n in sample]

    def by(names, keyfn):
        g = collections.defaultdict(list)
        for n in names:
            g[keyfn(n)].append(records[n])
        return dict((k, summarise(v)) for k, v in sorted(g.items()))

    basic_pool_absent = [n for n in miss_names
                         if qtype[n] == "basic" and bucket_of[n] == "pool_absent"]
    bpa_recs = [records[n] for n in basic_pool_absent]
    bpa_reach = sum(1 for r in bpa_recs if r["arm_b"]["reached_eligible"])
    bpa_rate = rate(bpa_reach, len(bpa_recs))

    miss_sum = summarise(miss_recs)
    samp_sum = summarise(samp_recs)
    hits_sum = summarise(hit_recs)
    miss_rate = miss_sum["reach_rate"]
    samp_rate = samp_sum["reach_rate"]
    delta = round(miss_rate - samp_rate, 4)

    limb_a = bpa_rate < KILL_A_THRESHOLD
    limb_b = delta < KILL_B_MIN_DELTA
    verdict = "KILL" if (limb_a or limb_b) else "PASS"
    reason = (
        "limb (a) reach on the %d basic pool-absent misses = %d/%d = %.4f "
        "(threshold %.2f -> %s); limb (b) miss reach %.4f minus matched-hit "
        "reach %.4f = %+.4f (needs >= %.2f -> %s)."
        % (len(bpa_recs), bpa_reach, len(bpa_recs), bpa_rate, KILL_A_THRESHOLD,
           "FIRES" if limb_a else "does not fire", miss_rate, samp_rate, delta,
           KILL_B_MIN_DELTA, "FIRES" if limb_b else "does not fire")
    )

    types_in_misses = sorted(set(qtype[n] for n in miss_names))
    buckets = sorted(set(bucket_of.values()))
    bucket_x_type = {}
    for b in buckets:
        for t in types_in_misses:
            cell = [records[n] for n in miss_names
                    if bucket_of[n] == b and qtype[n] == t]
            if cell:
                bucket_x_type["%s|%s" % (b, t)] = summarise(cell)

    out = {
        "stamp": STAMP,
        "probe": "P1 phrase/NEAR reachability with matched hit control",
        "tool": "benchmarks/dogfood/erb/probe_phrase_reach.py",
        "bed": BED,
        "bed_bytes": bed_bytes,
        "bed_access": ("sqlite3 file:...?mode=ro (read-only URI); no writes, "
                       "no index creation, no VACUUM"),
        "git_sha": git_sha,
        "basis_receipts": [
            "benchmarks/dogfood/erb/receipts/bucket_ledger_2026-09-01.json",
            "benchmarks/dogfood/erb/receipts/ladder_v09x_w24_min_delivered_2026-08-28.json",
            "benchmarks/dogfood/erb/receipts/interloper_mine_erb947k_2026-08-31.json",
            "benchmarks/dogfood/erb/needles_resolved_v09x_full.json",
            "benchmarks/dogfood/erb/gold_by_needle_v09x.json",
        ],
        "kill_criterion": KILL_CRITERION,
        "verdict": verdict,
        "verdict_reason": reason,
        "kill_limbs": {"a_basic_pool_absent_reach_below_0.40": limb_a,
                       "b_no_discrimination_vs_hits": limb_b},
        "method": {
            "tokenisation": ("query.lower(), split on regex [^0-9a-z]+, drop empty "
                             "tokens; order preserved, nothing reordered"),
            "stoplist_n": len(STOPLIST),
            "stoplist": STOPLIST,
            "ngram_range": [NGRAM_MIN, NGRAM_MAX],
            "reading_b_primary": ("contiguous n-grams over the ORIGINAL token stream "
                                  "(stopwords retained in place), keeping only n-grams "
                                  "containing >=1 non-stopword term. Every emitted phrase "
                                  "is one that really occurs in the query text."),
            "reading_a_secondary": ("stopwords deleted first, then contiguous n-grams over "
                                    "the reduced sequence. Reported as a sensitivity arm "
                                    "only: it fabricates phrases that occur in no document "
                                    "and therefore biases reachability DOWN."),
            "df_definition": ("count(*) FROM genes_fts WHERE genes_fts MATCH "
                              "'{content complement}: \"<phrase>\"'. The fts 'content' "
                              "column is source_id + promoter tags + gene content (see view "
                              "genes_fts_source), so DF is document frequency over that "
                              "concatenation; 'complement' is included so the probe is "
                              "generous to the lane."),
            "df_cap": DF_CAP,
            "df_cap_meaning": ("phrases with DF > %d are recorded but not treated as a "
                               "usable lane; 'reach_any_df' reports reachability with the "
                               "cap lifted." % DF_CAP),
            "reachability": ("EXACT, not sampled: for every candidate phrase with DF>=1, "
                             "each gold rowid is tested with 'MATCH ? AND rowid = ?'. "
                             "Phrases are walked in ascending DF, so reach_df is the "
                             "LOWEST-DF phrase that lands on gold. No top-k truncation is "
                             "applied, so reach_rate is the lane's CEILING, not a forecast."),
            "topfew_variant": ("reach_topfew restricts the lane to the %d most selective "
                               "eligible phrases -- closer to what a real lane would issue."
                               % TOPFEW),
            "near": ("NEAR(\"t1\" \"t2\", %d) over the two lowest-DF non-stopword query "
                     "terms with DF>=1; same exact rowid reachability test." % NEAR_SPAN),
            "error_handling": ("every FTS5 call is wrapped in try/except; malformed "
                               "expressions are logged to fts_errors and skipped, never "
                               "fatal"),
        },
        "n_needles": len(needles),
        "n_misses": len(miss_names),
        "n_hits": len(hit_names),
        "n_fts_df_queries_distinct": len(bed.df_cache),
        "n_membership_queries": bed.member_calls,
        "hit_control": {
            "sample_n": len(sample),
            "seed": SEED,
            "stratification": ("question_type, matched cell-for-cell to the miss type "
                               "mix; names sorted then seeded-shuffled per stratum"),
            "miss_type_mix": dict(miss_mix),
            "sample_type_mix": dict(collections.Counter(qtype[n] for n in sample)),
            "shortfall": shortfall,
            "sample_sha256_prefix": sample_sha,
            "sample": sample,
        },
        "section_1_selectivity": {
            "note": ("Tests the premise that a phrase is ~90x more selective than the best "
                     "single term. selectivity_ratio = min_content_term_df / "
                     "min_nonzero_phrase_df, computed PER QUERY; distribution reported, "
                     "not a mean alone."),
            "ratio_misses": miss_sum["selectivity_ratio_dist"],
            "ratio_hits_matched_sample": samp_sum["selectivity_ratio_dist"],
            "ratio_hits_all": hits_sum["selectivity_ratio_dist"],
            "min_content_term_df_dist_misses": quartiles(
                [r["min_content_term_df"] for r in miss_recs]),
            "min_phrase_df_dist_misses": quartiles(
                [r["arm_b"]["min_phrase_df_nonzero"] for r in miss_recs]),
            "min_content_term_df_dist_hits_sample": quartiles(
                [r["min_content_term_df"] for r in samp_recs]),
            "min_phrase_df_dist_hits_sample": quartiles(
                [r["arm_b"]["min_phrase_df_nonzero"] for r in samp_recs]),
        },
        "section_2_reachability_misses": {
            "overall": miss_sum,
            "by_bucket": by(miss_names, lambda n: bucket_of[n]),
            "by_question_type": by(miss_names, lambda n: qtype[n]),
            "by_bucket_x_type": bucket_x_type,
            "by_chunk_split": by(miss_names, lambda n: (
                "chunk_split" if chunk_split_of[n] else "not_chunk_split")),
            "basic_pool_absent_cell": {
                "n": len(bpa_recs), "reach": bpa_reach, "reach_rate": bpa_rate,
                "reach_topfew": sum(1 for r in bpa_recs if r["arm_b"]["reached_topfew"]),
                "reaching_phrase_df_dist": quartiles(
                    [r["arm_b"]["reach_df"] for r in bpa_recs]),
            },
            "reaching_phrase_df_bands": df_bands(miss_recs),
        },
        "section_3_control_arm": {
            "misses": miss_sum,
            "hits_matched_sample": samp_sum,
            "hits_all": hits_sum,
            "hits_matched_sample_by_question_type": by(sample, lambda n: qtype[n]),
            "reach_rate_delta_miss_minus_hitsample": delta,
            "reach_topfew_delta_miss_minus_hitsample": round(
                miss_sum["reach_topfew_rate"] - samp_sum["reach_topfew_rate"], 4),
            "reaching_phrase_df_bands_hits_sample": df_bands(samp_recs),
        },
        "section_4_near": {
            "misses": dict((k, miss_sum[k]) for k in NEAR_KEYS),
            "hits_matched_sample": dict((k, samp_sum[k]) for k in NEAR_KEYS),
            "hits_all": dict((k, hits_sum[k]) for k in NEAR_KEYS),
            "misses_by_bucket": dict(
                (b, dict((k, v[k]) for k in NEAR_KEYS))
                for b, v in by(miss_names, lambda n: bucket_of[n]).items()),
            "misses_by_question_type": dict(
                (t, dict((k, v[k]) for k in NEAR_KEYS))
                for t, v in by(miss_names, lambda n: qtype[n]).items()),
        },
        "section_5_reading_a_sensitivity": {
            "note": ("stopword-deleted n-grams; expected to under-report reachability "
                     "because some emitted phrases occur in no document"),
            "misses": summarise(miss_recs, arm="a"),
            "hits_matched_sample": summarise(samp_recs, arm="a"),
        },
        "fts_errors": bed.errors[:200],
        "n_fts_errors": len(bed.errors),
        "gold_rowid_resolution": {
            "needles_with_missing_gold_rowid": sorted(
                [n for n, v in gold_resolution.items() if v["n_rowids"] < v["n_gold"]]),
            "needles_with_zero_gold_rowids": sorted(
                [n for n, v in gold_resolution.items() if v["n_rowids"] == 0]),
        },
        "wall_s": round(time.time() - t_start, 1),
        "per_needle": [
            dict(records[n],
                 arm_status=("miss" if n in set(miss_names) else "hit"),
                 in_hit_sample=(n in sample_set),
                 question_type=qtype[n],
                 bucket=bucket_of.get(n),
                 chunk_split=chunk_split_of.get(n))
            for n in all_names
        ],
    }

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)

    print("VERDICT %s" % verdict)
    print(reason)
    print("miss reach %s | hits(matched n=%d) reach %s | hits(all) reach %s"
          % (miss_rate, len(sample), samp_rate, hits_sum["reach_rate"]))
    print("wrote %s in %.0fs (%d distinct FTS DF queries, %d errors)"
          % (OUT_PATH, time.time() - t_start, len(bed.df_cache), len(bed.errors)))


if __name__ == "__main__":
    main()

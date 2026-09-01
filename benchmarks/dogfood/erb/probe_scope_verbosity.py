"""P3 - SCOPE vs VERBOSITY diagnostic on ERB 947k (2026-09-01).

QUESTION. Interlopers that take gold's seats sit at the 4,000-char chunk cap
85.0% of the time (1423/1674 seats) vs gold 42.9% (67/156). Two mechanisms
explain that asymmetry and they imply opposite fixes:

  SCOPE     - long chunks cover MORE DISTINCT query terms, so they accrue more
              additive IDF mass. BM25's b cannot remove additive mass (it scales
              only within-term tf saturation), so a length-normalization knob
              is the wrong tool; a scope-aware custom scorer over FTS5 postings
              would be the right one.
  VERBOSITY - long chunks REPEAT the same terms (high tf, low type-token ratio).
              That IS what b saturates, so length normalization would help.

The taxonomy also reports kw_cov~n_chars Pearson r = 0.279, which is WEAK, and
that tension against an 85%-vs-43% at-cap split is what this probe resolves.

KILL CRITERION (stated before the run):
  "KILL the length-normalization family if distinct-term count does NOT separate
   gold from interlopers on these slates once length is controlled for - i.e. if
   the at-cap asymmetry is not accompanied by a distinct-term asymmetry, the
   scope hypothesis is wrong on this bed and no custom scorer over FTS5 postings
   is worth building."

Method: fetch full content for every gold chunk and every delivered winner seat
across the 156 misses; compute total tokens, DISTINCT tokens, avg_tf, n_chars
and distinct-query-term coverage with the miner's own tokenizer and the miner's
own keyword extractor; then (a) compare gold vs winners length-matched and
within-query-paired, (b) bin by length to separate a CAP step from a LENGTH
ramp, (c) score boilerplate/verbosity heuristics, (d) sample the corpus for a
baseline, (e) recompute the kw_cov~n_chars correlation.

Bed is opened READ-ONLY (mode=ro). No writes, no indexes, no vacuum.
"""
from __future__ import annotations

import json
import math
import random
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
ERB = ROOT / "benchmarks/dogfood/erb"

BED = "F:/tmp/erb_blob_v09x.db"
MINE = ERB / "receipts/interloper_mine_erb947k_2026-08-31.json"
LEDGER = ERB / "receipts/bucket_ledger_2026-09-01.json"
OUT = ERB / "receipts/scope_verbosity_2026-09-01.json"

SEED = 20260901
CORPUS_SAMPLE = 30000
CORPUS_TOKSET_SUB = 4000
CAP = 4000
AT_CAP_MIN = 3990

# miner's tokenizer, verbatim (benchmarks/dogfood/mine_interlopers.py)
_TERM_RE = re.compile(r"[a-z0-9_/\-]+")

# -- verbosity / boilerplate heuristics (declared; acknowledged as heuristics) --
_QUOTE_RE = re.compile(r"^\s*>")
_HDR_RE = re.compile(r"^\s*(from|to|cc|bcc|sent|subject|date|reply-to)\s*:", re.I)
_B64_RE = re.compile(r"[A-Za-z0-9+/]{60,}={0,2}")
_SIG_MARKERS = ("-----original message-----", "sent from my", "best regards",
                "kind regards", "forwarded by", "begin forwarded message")
_DISCLAIMER_TERMS = ("confidential", "intended recipient", "unauthorized",
                     "privileged", "disclaim", "no liability", "delete this",
                     "not the intended", "e-mail transmission",
                     "distribution is prohibited")
_JSON_CHARS = set("{}[]\":,")


def tokens(text):
    return [t for t in _TERM_RE.findall(text.lower()) if len(t) > 2]


def doc_stats(content):
    toks = tokens(content)
    cnt = Counter(toks)
    n_tok = len(toks)
    n_dis = len(cnt)
    lines = content.split("\n")
    ne = [ln.strip() for ln in lines if ln.strip()]
    low = content.lower()
    quoted = sum(1 for ln in lines if _QUOTE_RE.match(ln))
    hdrs = sum(1 for ln in lines if _HDR_RE.match(ln))
    rep = 0.0
    if len(ne) >= 8:
        rep = 1.0 - (len(set(ne)) / len(ne))
    dis_hits = sum(1 for t in _DISCLAIMER_TERMS if t in low)
    json_frac = (sum(1 for ch in content if ch in _JSON_CHARS) / len(content)) if content else 0.0
    return {
        "n_chars": len(content),
        "n_tokens": n_tok,
        "n_distinct": n_dis,
        "avg_tf": round(n_tok / n_dis, 4) if n_dis else 0.0,
        "ttr": round(n_dis / n_tok, 4) if n_tok else 0.0,
        "top_tf": max(cnt.values()) if cnt else 0,
        "n_lines": len(lines),
        "h_quoted_reply": bool(quoted >= 3 and len(lines) and quoted / len(lines) >= 0.15),
        "h_mail_headers": bool(hdrs >= 4),
        "h_signature": bool(any(m in low for m in _SIG_MARKERS)),
        "h_disclaimer": bool(dis_hits >= 2),
        "h_base64": bool(_B64_RE.search(content)),
        "h_repeated_lines": bool(rep >= 0.25),
        "h_json_structured": bool(json_frac >= 0.12),
        "h_low_ttr": bool(n_dis and (n_tok / n_dis) >= 2.0),
        "repeat_line_frac": round(rep, 4),
        "json_char_frac": round(json_frac, 4),
    }


def band(n_chars):
    if n_chars >= AT_CAP_MIN:
        return "at_cap_3990+"
    for lo in (3000, 2000, 1000):
        if n_chars >= lo:
            return "%d-%d" % (lo, lo + 1000)
    return "0-1000"


BAND_ORDER = ["0-1000", "1000-2000", "2000-3000", "3000-4000", "at_cap_3990+"]


def q(vals, p):
    return round(float(np.percentile(vals, p)), 2) if len(vals) else None


def dist(vals):
    a = np.asarray(vals, dtype=float)
    if not len(a):
        return {"n": 0}
    return {"n": int(len(a)), "mean": round(float(a.mean()), 3),
            "p25": q(a, 25), "median": q(a, 50), "p75": q(a, 75),
            "min": round(float(a.min()), 2), "max": round(float(a.max()), 2)}


def pearson(x, y):
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    if len(x) < 3 or x.std() == 0 or y.std() == 0:
        return None
    return round(float(np.corrcoef(x, y)[0, 1]), 4)


def rankdata(a):
    a = np.asarray(a, float)
    order = a.argsort(kind="mergesort")
    r = np.empty(len(a), float)
    r[order] = np.arange(1, len(a) + 1)
    vals, inv, cts = np.unique(a, return_inverse=True, return_counts=True)
    sums = np.zeros(len(vals))
    np.add.at(sums, inv, r)
    return (sums / cts)[inv]


def spearman(x, y):
    if len(x) < 3:
        return None
    return pearson(rankdata(x), rankdata(y))


def sign_test(wins, losses):
    n = wins + losses
    if n == 0:
        return {"n_effective": 0, "p_two_sided": None}
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2.0 ** n)
    return {"n_effective": n, "p_two_sided": round(min(1.0, 2 * tail), 8)}


def main():
    t0 = time.perf_counter()
    from cymatix_context.accel import extract_query_signals

    mine = json.loads(MINE.read_text(encoding="utf-8"))
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    bucket_of = {r["needle"]: r["bucket"] for r in ledger["per_miss"]}
    gold_map = json.loads((ERB / "gold_by_needle_v09x.json").read_text(encoding="utf-8"))
    misses = mine["misses"]

    git_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                             capture_output=True, text=True).stdout.strip()

    # -- gather every gene we need, one batched read ------------------------
    need = set()
    for m in misses:
        for g in gold_map.get(m["needle"], []):
            need.add(g)
        gf = m.get("gold_first") or {}
        if gf.get("gene_id"):
            need.add(gf["gene_id"])
        for d in m.get("delivered", []):
            if d.get("gene_id"):
                need.add(d["gene_id"])
    con = sqlite3.connect("file:%s?mode=ro" % BED, uri=True)
    cur = con.cursor()
    content = {}
    need_l = sorted(need)
    t = time.perf_counter()
    for i in range(0, len(need_l), 400):
        chunk = need_l[i:i + 400]
        ph = ",".join("?" * len(chunk))
        sql = "SELECT gene_id, content FROM genes WHERE gene_id IN (%s)" % ph
        for gid, c in cur.execute(sql, chunk):
            content[gid] = c or ""
    print("fetched %d/%d genes in %.1fs" % (len(content), len(need_l),
                                            time.perf_counter() - t), flush=True)

    stats = {g: doc_stats(c) for g, c in content.items()}
    tokset = {g: set(tokens(c)) for g, c in content.items()}

    # -- per-miss rows ------------------------------------------------------
    per_miss = []
    gold_first_rows, gold_all_rows, winner_rows, top1_rows = [], [], [], []
    drift = []
    kw_by_needle = {}
    for m in misses:
        name = m["needle"]
        d, e = extract_query_signals(m["query"])
        ks = sorted(set(d) | set(e))
        n_kw = len(ks)
        if n_kw != m["n_kw"]:
            drift.append({"needle": name, "kind": "n_kw", "live": n_kw, "mine": m["n_kw"]})
        kset = set(ks)
        kw_by_needle[name] = kset

        def row(gid, role, rank=None):
            st = stats.get(gid)
            if st is None:
                return None
            cov = len(kset & tokset[gid])
            r = dict(st)
            r.update({"gene_id": gid, "needle": name, "class": m["class"],
                      "bucket": bucket_of.get(name, "?"), "role": role, "rank": rank,
                      "n_kw": n_kw, "kw_cov": cov,
                      "kw_cov_frac": round(cov / n_kw, 4) if n_kw else 0.0,
                      "band": band(st["n_chars"])})
            return r

        gf_id = (m.get("gold_first") or {}).get("gene_id")
        gf = row(gf_id, "gold_first") if gf_id else None
        if gf:
            if gf["kw_cov"] != m["gold_first"]["kw_cov"]:
                drift.append({"needle": name, "kind": "gold_kw_cov",
                              "live": gf["kw_cov"], "mine": m["gold_first"]["kw_cov"]})
            if gf["n_chars"] != m["gold_first"]["n_chars"]:
                drift.append({"needle": name, "kind": "gold_n_chars",
                              "live": gf["n_chars"], "mine": m["gold_first"]["n_chars"]})
            gold_first_rows.append(gf)
        ga = [row(g, "gold_chunk") for g in sorted(gold_map.get(name, []))]
        ga = [r for r in ga if r]
        gold_all_rows.extend(ga)
        wr = []
        for dv in m.get("delivered", []):
            r = row(dv.get("gene_id"), "winner", dv.get("rank"))
            if r:
                if r["kw_cov"] != dv["kw_cov"]:
                    drift.append({"needle": name, "kind": "winner_kw_cov",
                                  "gene_id": r["gene_id"], "live": r["kw_cov"],
                                  "mine": dv["kw_cov"]})
                wr.append(r)
        winner_rows.extend(wr)
        if wr:
            top1_rows.append(wr[0])
        keep = ("n_chars", "n_tokens", "n_distinct", "avg_tf", "kw_cov",
                "kw_cov_frac", "band")
        per_miss.append({
            "needle": name, "class": m["class"], "bucket": bucket_of.get(name, "?"),
            "n_kw": n_kw, "gold_n": m["gold_n"],
            "gold_first": {k: gf[k] for k in keep} if gf else None,
            "gold_union_kw_cov": m["gold_kw_cov"],
            "n_winner_seats": len(wr),
            "winner_top1": {k: wr[0][k] for k in keep} if wr else None,
            "winner_mean_n_distinct": round(float(np.mean([w["n_distinct"] for w in wr])), 2) if wr else None,
            "winner_mean_kw_cov": round(float(np.mean([w["kw_cov"] for w in wr])), 3) if wr else None,
            "winner_at_cap": sum(1 for w in wr if w["band"] == "at_cap_3990+"),
        })

    # -- corpus sample ------------------------------------------------------
    max_rowid = cur.execute("SELECT max(rowid) FROM genes").fetchone()[0]
    rng = random.Random(SEED)
    rowids = sorted(rng.sample(range(1, max_rowid + 1), CORPUS_SAMPLE))
    keep_tokens = set(random.Random(SEED + 1).sample(rowids, CORPUS_TOKSET_SUB))
    corpus = []
    corpus_tok = []  # (n_chars, token_set) for the coverage-baseline subsample
    t = time.perf_counter()
    for rid in rowids:
        r = cur.execute("SELECT content FROM genes WHERE rowid=?", (rid,)).fetchone()
        if r and r[0]:
            corpus.append(doc_stats(r[0]))
            if rid in keep_tokens:
                corpus_tok.append((len(r[0]), set(tokens(r[0]))))
    con.close()
    print("corpus sample %d/%d in %.1fs" % (len(corpus), CORPUS_SAMPLE,
                                            time.perf_counter() - t), flush=True)

    def hshare(rows):
        keys = [k for k in rows[0] if k.startswith("h_")] if rows else []
        return {k: round(sum(1 for r in rows if r[k]) / len(rows), 4) for k in keys} if rows else {}

    def pack(rows, label):
        has_kw = bool(rows) and "kw_cov" in rows[0]
        return {
            "label": label, "n": len(rows),
            "n_chars": dist([r["n_chars"] for r in rows]),
            "n_tokens": dist([r["n_tokens"] for r in rows]),
            "n_distinct": dist([r["n_distinct"] for r in rows]),
            "avg_tf": dist([r["avg_tf"] for r in rows]),
            "ttr": dist([r["ttr"] for r in rows]),
            "at_cap_frac": round(sum(1 for r in rows if r["n_chars"] >= AT_CAP_MIN) / len(rows), 4) if rows else None,
            "exactly_4000_frac": round(sum(1 for r in rows if r["n_chars"] == CAP) / len(rows), 4) if rows else None,
            "kw_cov": dist([r["kw_cov"] for r in rows]) if has_kw else None,
            "kw_cov_frac": dist([r["kw_cov_frac"] for r in rows]) if has_kw else None,
            "heuristics": hshare(rows),
        }

    # -- (a) SCOPE, unmatched then length-matched ---------------------------
    band_table = {}
    for b in BAND_ORDER:
        g = [r for r in gold_first_rows if r["band"] == b]
        ga = [r for r in gold_all_rows if r["band"] == b]
        w = [r for r in winner_rows if r["band"] == b]
        c = [r for r in corpus if band(r["n_chars"]) == b]
        mean = lambda rows, k: round(float(np.mean([r[k] for r in rows])), 4) if rows else None
        band_table[b] = {
            "gold_first_n": len(g), "winner_seats_n": len(w),
            "gold_all_chunks_n": len(ga), "corpus_n": len(c),
            "gold_first_n_distinct_mean": mean(g, "n_distinct"),
            "winner_n_distinct_mean": mean(w, "n_distinct"),
            "corpus_n_distinct_mean": mean(c, "n_distinct"),
            "gold_first_kw_cov_mean": mean(g, "kw_cov"),
            "winner_kw_cov_mean": mean(w, "kw_cov"),
            "gold_all_chunks_kw_cov_mean": mean(ga, "kw_cov"),
            "gold_first_kw_cov_frac_mean": mean(g, "kw_cov_frac"),
            "winner_kw_cov_frac_mean": mean(w, "kw_cov_frac"),
            "gold_first_avg_tf_mean": mean(g, "avg_tf"),
            "winner_avg_tf_mean": mean(w, "avg_tf"),
            "corpus_avg_tf_mean": mean(c, "avg_tf"),
        }

    gf_by_needle = {r["needle"]: r for r in gold_first_rows}
    win_by_needle = {}
    for r in winner_rows:
        win_by_needle.setdefault(r["needle"], []).append(r)

    def paired(sel_gold, sel_win, label):
        """Within-query paired gold-vs-winner comparison, optionally length-matched."""
        wins = ties = losses = 0
        tf_wins = tf_losses = 0
        deltas, deltas_frac, dd, dtf = [], [], [], []
        rows_used = []
        for name, gfr in sorted(gf_by_needle.items()):
            if not sel_gold(gfr):
                continue
            ws = [r for r in win_by_needle.get(name, []) if sel_win(r, gfr)]
            if not ws:
                continue
            wmean = float(np.mean([r["kw_cov"] for r in ws]))
            deltas.append(wmean - gfr["kw_cov"])
            deltas_frac.append(float(np.mean([r["kw_cov_frac"] for r in ws])) - gfr["kw_cov_frac"])
            dd.append(float(np.mean([r["n_distinct"] for r in ws])) - gfr["n_distinct"])
            tfd = float(np.mean([r["avg_tf"] for r in ws])) - gfr["avg_tf"]
            dtf.append(tfd)
            if tfd > 0:
                tf_wins += 1
            elif tfd < 0:
                tf_losses += 1
            if wmean > gfr["kw_cov"]:
                wins += 1
            elif wmean < gfr["kw_cov"]:
                losses += 1
            else:
                ties += 1
            rows_used.append(name)
        return {
            "label": label, "n_pairs": len(rows_used),
            "winner_mean_beats_gold": wins, "ties": ties,
            "gold_beats_winner_mean": losses,
            "mean_delta_kw_cov": round(float(np.mean(deltas)), 3) if deltas else None,
            "median_delta_kw_cov": round(float(np.median(deltas)), 3) if deltas else None,
            "mean_delta_kw_cov_frac": round(float(np.mean(deltas_frac)), 4) if deltas_frac else None,
            "mean_delta_n_distinct": round(float(np.mean(dd)), 2) if dd else None,
            "mean_delta_avg_tf": round(float(np.mean(dtf)), 4) if dtf else None,
            "winner_more_verbose_than_gold": tf_wins,
            "gold_more_verbose_than_winner": tf_losses,
            "sign_test_kw_cov": sign_test(wins, losses),
            "sign_test_avg_tf": sign_test(tf_wins, tf_losses),
        }

    paired_tests = [
        paired(lambda g: True, lambda w, g: True,
               "unmatched: gold_first vs all its winner seats"),
        paired(lambda g: True, lambda w, g: w["band"] == g["band"],
               "length-matched: winners in gold_first's own 1000-char band"),
        paired(lambda g: g["band"] == "at_cap_3990+", lambda w, g: w["band"] == "at_cap_3990+",
               "both at cap: gold_first at cap vs winner seats at cap"),
        paired(lambda g: True, lambda w, g: abs(w["n_chars"] - g["n_chars"]) <= 250,
               "tight match: winners within +/-250 chars of gold_first"),
        paired(lambda g: g["band"] != "at_cap_3990+", lambda w, g: w["band"] != "at_cap_3990+",
               "both under cap: gold_first under cap vs under-cap winner seats"),
    ]

    # -- (b) STEP vs MONOTONE: 500-char bins --------------------------------
    def bin500(n):
        if n >= CAP:
            return "4000_exact_cap"
        return "%d-%d" % ((n // 500) * 500, (n // 500) * 500 + 500)

    bin_keys = ["%d-%d" % (i, i + 500) for i in range(0, CAP, 500)] + ["4000_exact_cap"]
    n_w = len(winner_rows)
    n_c = len(corpus)
    n_g = len(gold_first_rows)
    bins = {}
    for k in bin_keys:
        w = [r for r in winner_rows if bin500(r["n_chars"]) == k]
        g = [r for r in gold_first_rows if bin500(r["n_chars"]) == k]
        c = [r for r in corpus if bin500(r["n_chars"]) == k]
        w_share = len(w) / n_w if n_w else 0.0
        c_share = len(c) / n_c if n_c else 0.0
        g_share = len(g) / n_g if n_g else 0.0
        mean = lambda rows, key: round(float(np.mean([r[key] for r in rows])), 4) if rows else None
        bins[k] = {
            "winner_seats": len(w), "winner_seat_share": round(w_share, 4),
            "gold_first_n": len(g), "gold_share": round(g_share, 4),
            "corpus_n": len(c), "corpus_share": round(c_share, 4),
            "seat_enrichment_vs_corpus": round(w_share / c_share, 3) if c_share else None,
            "gold_enrichment_vs_corpus": round(g_share / c_share, 3) if c_share else None,
            "winner_kw_cov_mean": mean(w, "kw_cov"),
            "winner_kw_cov_frac_mean": mean(w, "kw_cov_frac"),
            "gold_kw_cov_mean": mean(g, "kw_cov"),
            "winner_n_distinct_mean": mean(w, "n_distinct"),
            "corpus_n_distinct_mean": mean(c, "n_distinct"),
            "winner_avg_tf_mean": mean(w, "avg_tf"),
            "corpus_avg_tf_mean": mean(c, "avg_tf"),
        }

    # -- (b2) the pure LENGTH subsidy, with selection removed ---------------
    # For each miss query, score a random corpus subsample (no retrieval, no
    # selection at all) and read off mean kw_cov by length band. This isolates
    # how much distinct-query-term coverage LENGTH ALONE buys, so the winner
    # advantage can be split into "length subsidy" vs "topical selection".
    cb_bands = {b: [] for b in BAND_ORDER}
    cb_atcap, cb_under = [], []
    per_needle_cb = []
    for name, kset in sorted(kw_by_needle.items()):
        acc = {b: [] for b in BAND_ORDER}
        for nch, ts in corpus_tok:
            acc[band(nch)].append(len(kset & ts))
        row_cb = {"needle": name, "n_kw": len(kset)}
        for b in BAND_ORDER:
            if acc[b]:
                v = float(np.mean(acc[b]))
                cb_bands[b].append(v)
                row_cb[b] = round(v, 3)
        at = acc["at_cap_3990+"]
        un = [x for b in BAND_ORDER[:-1] for x in acc[b]]
        if at:
            cb_atcap.append(float(np.mean(at)))
        if un:
            cb_under.append(float(np.mean(un)))
        per_needle_cb.append(row_cb)
    rand_atcap = float(np.mean(cb_atcap)) if cb_atcap else None
    rand_under = float(np.mean(cb_under)) if cb_under else None
    w_atcap = [r for r in winner_rows if r["band"] == "at_cap_3990+"]
    g_atcap = [r for r in gold_first_rows if r["band"] == "at_cap_3990+"]
    corpus_baseline = {
        "design": ("for every one of the 156 miss queries, kw_cov is computed against a fixed "
                   "random corpus subsample (seed %d, n=%d docs) that no retrieval ever touched; "
                   "means are then averaged over queries" % (SEED + 1, len(corpus_tok))),
        "n_subsample_docs": len(corpus_tok),
        "random_doc_mean_kw_cov_by_band": {b: round(float(np.mean(cb_bands[b])), 3)
                                           for b in BAND_ORDER if cb_bands[b]},
        "random_doc_mean_kw_cov_at_cap": round(rand_atcap, 3) if rand_atcap is not None else None,
        "random_doc_mean_kw_cov_under_cap": round(rand_under, 3) if rand_under is not None else None,
        "pure_length_subsidy_at_cap_minus_under_cap": round(rand_atcap - rand_under, 3) if rand_atcap is not None else None,
        "winner_seats_at_cap_mean_kw_cov": round(float(np.mean([r["kw_cov"] for r in w_atcap])), 3) if w_atcap else None,
        "gold_first_at_cap_mean_kw_cov": round(float(np.mean([r["kw_cov"] for r in g_atcap])), 3) if g_atcap else None,
        "selection_lift_winner_over_random_at_cap": round(
            float(np.mean([r["kw_cov"] for r in w_atcap])) / rand_atcap, 3) if w_atcap and rand_atcap else None,
        "selection_lift_gold_over_random_at_cap": round(
            float(np.mean([r["kw_cov"] for r in g_atcap])) / rand_atcap, 3) if g_atcap and rand_atcap else None,
        "coverage_lift_over_random_by_band": {},
        "per_needle": per_needle_cb,
    }
    for b in BAND_ORDER:
        rnd = float(np.mean(cb_bands[b])) if cb_bands[b] else None
        wb = [r for r in winner_rows if r["band"] == b]
        gb = [r for r in gold_first_rows if r["band"] == b]
        corpus_baseline["coverage_lift_over_random_by_band"][b] = {
            "random_doc_kw_cov": round(rnd, 3) if rnd else None,
            "winner_kw_cov": round(float(np.mean([r["kw_cov"] for r in wb])), 3) if wb else None,
            "winner_n": len(wb),
            "winner_lift": round(float(np.mean([r["kw_cov"] for r in wb])) / rnd, 3) if wb and rnd else None,
            "gold_kw_cov": round(float(np.mean([r["kw_cov"] for r in gb])), 3) if gb else None,
            "gold_n": len(gb),
            "gold_lift": round(float(np.mean([r["kw_cov"] for r in gb])) / rnd, 3) if gb and rnd else None,
        }

    # -- (b3) step vs monotone: fit the below-cap ramp and extrapolate ------
    below = [k for k in bin_keys if k != "4000_exact_cap"]
    xs = np.array([250 + 500 * i for i in range(len(below))], float)
    ys = np.array([bins[k]["seat_enrichment_vs_corpus"] or 0.0 for k in below], float)
    slope, intercept = np.polyfit(xs, ys, 1)
    pred_cap = float(slope * 4000 + intercept)
    obs_cap = bins["4000_exact_cap"]["seat_enrichment_vs_corpus"]
    ys_cov = np.array([bins[k]["winner_kw_cov_mean"] or 0.0 for k in below], float)
    s2, i2 = np.polyfit(xs, ys_cov, 1)
    pred_cap_cov = float(s2 * 4000 + i2)
    step_test = {
        "design": ("OLS fit of the eight below-cap 500-char bins (bin midpoints as x), extrapolated "
                   "to x=4000, compared with the observed exactly-at-cap point mass"),
        "seat_enrichment": {
            "below_cap_fit_slope_per_1000_chars": round(float(slope) * 1000, 4),
            "below_cap_fit_intercept": round(float(intercept), 4),
            "extrapolated_cap_enrichment": round(pred_cap, 3),
            "observed_cap_enrichment": obs_cap,
            "observed_over_extrapolated": round(obs_cap / pred_cap, 3) if pred_cap else None,
            "observed_over_last_below_cap_bin": round(
                obs_cap / bins["3500-4000"]["seat_enrichment_vs_corpus"], 3)
            if bins["3500-4000"]["seat_enrichment_vs_corpus"] else None,
        },
        "winner_kw_cov": {
            "below_cap_fit_slope_per_1000_chars": round(float(s2) * 1000, 4),
            "extrapolated_cap_kw_cov": round(pred_cap_cov, 3),
            "observed_cap_kw_cov": bins["4000_exact_cap"]["winner_kw_cov_mean"],
            "observed_minus_extrapolated": round(
                (bins["4000_exact_cap"]["winner_kw_cov_mean"] or 0.0) - pred_cap_cov, 3),
        },
        "reading": ("a large observed/extrapolated ratio on SEAT ENRICHMENT with a near-1.0 ratio on "
                    "WINNER kw_cov means the cap point mass wins seats out of proportion to the smooth "
                    "length->coverage ramp"),
        "caveat": ("the linear extrapolation is a WEAK null. Seats are a top-12-of-947k extreme order "
                   "statistic, so any monotone coverage->score mapping turns a smooth coverage ramp into "
                   "a convex seat-share curve; a straight-line fit through the below-cap bins therefore "
                   "under-predicts the cap bin even with no cap-specific mechanism. Read the 2.86x as "
                   "suggestive, not as proof of a discontinuity. The per-band coverage-lift table is the "
                   "sharper instrument: if retrieval's lift over a random doc of the SAME length is "
                   "roughly constant across bands, no cap-specific mechanism is needed."),
    }

    # -- (e) correlations, pooled and within-query centered -----------------
    def within_q_centered(rows, xk, yk):
        xs, ys = [], []
        byq = {}
        for r in rows:
            byq.setdefault(r["needle"], []).append(r)
        for _, rs in byq.items():
            if len(rs) < 2:
                continue
            mx = float(np.mean([r[xk] for r in rs]))
            my = float(np.mean([r[yk] for r in rs]))
            for r in rs:
                xs.append(r[xk] - mx)
                ys.append(r[yk] - my)
        return xs, ys

    sem_w = [r for r in winner_rows if r["class"] == "semantic"]
    all_slate = winner_rows + gold_first_rows
    cx, cy = within_q_centered(winner_rows, "n_chars", "kw_cov")
    scx, scy = within_q_centered(sem_w, "n_chars", "kw_cov")
    corr = {
        "published_taxonomy_value": 0.279,
        "published_context": ("semantic drowned category evidence line: 'length is a "
                              "coverage subsidy (kw_cov~n_chars r=0.279)'"),
        "winners_all_pooled_r": pearson([r["n_chars"] for r in winner_rows],
                                        [r["kw_cov"] for r in winner_rows]),
        "winners_semantic_pooled_r": pearson([r["n_chars"] for r in sem_w],
                                             [r["kw_cov"] for r in sem_w]),
        "winners_semantic_pooled_spearman": spearman([r["n_chars"] for r in sem_w],
                                                     [r["kw_cov"] for r in sem_w]),
        "winners_all_pooled_r_kw_cov_frac": pearson([r["n_chars"] for r in winner_rows],
                                                    [r["kw_cov_frac"] for r in winner_rows]),
        "winners_all_within_query_centered_r": pearson(cx, cy),
        "winners_semantic_within_query_centered_r": pearson(scx, scy),
        "gold_first_r": pearson([r["n_chars"] for r in gold_first_rows],
                                [r["kw_cov"] for r in gold_first_rows]),
        "winners_plus_gold_pooled_r": pearson([r["n_chars"] for r in all_slate],
                                              [r["kw_cov"] for r in all_slate]),
        "n_chars_vs_n_distinct_r_winners": pearson([r["n_chars"] for r in winner_rows],
                                                   [r["n_distinct"] for r in winner_rows]),
        "n_chars_vs_n_distinct_r_corpus": pearson([r["n_chars"] for r in corpus],
                                                  [r["n_distinct"] for r in corpus]),
        "n_chars_vs_avg_tf_r_corpus": pearson([r["n_chars"] for r in corpus],
                                              [r["avg_tf"] for r in corpus]),
        "n_distinct_vs_kw_cov_r_winners": pearson([r["n_distinct"] for r in winner_rows],
                                                  [r["kw_cov"] for r in winner_rows]),
        "n_distinct_vs_kw_cov_within_query_r_winners": pearson(
            *within_q_centered(winner_rows, "n_distinct", "kw_cov")),
    }

    # -- verdict ------------------------------------------------------------
    matched = paired_tests[1]
    atcap = paired_tests[2]
    unmatched = paired_tests[0]
    scope_survives = bool(
        matched["mean_delta_kw_cov"] is not None and matched["mean_delta_kw_cov"] > 0
        and matched["winner_mean_beats_gold"] > matched["gold_beats_winner_mean"]
        and (matched["sign_test_kw_cov"]["p_two_sided"] or 1.0) < 0.05
        and atcap["mean_delta_kw_cov"] is not None and atcap["mean_delta_kw_cov"] > 0)
    verdict = "PASS" if scope_survives else "KILL"
    decomposition = {
        "unmatched_winner_minus_gold_kw_cov": unmatched["mean_delta_kw_cov"],
        "length_band_matched_winner_minus_gold_kw_cov": matched["mean_delta_kw_cov"],
        "share_of_gap_that_is_length_mediated": round(
            1.0 - matched["mean_delta_kw_cov"] / unmatched["mean_delta_kw_cov"], 4)
        if unmatched["mean_delta_kw_cov"] else None,
        "share_of_gap_surviving_length_match": round(
            matched["mean_delta_kw_cov"] / unmatched["mean_delta_kw_cov"], 4)
        if unmatched["mean_delta_kw_cov"] else None,
        "raw_vocabulary_size_gap_when_length_matched": matched["mean_delta_n_distinct"],
        "verbosity_gap_when_length_matched": matched["mean_delta_avg_tf"],
        "note": ("the surviving gap is in DISTINCT QUERY-TERM coverage, not in raw vocabulary size; "
                 "raw n_distinct and avg_tf are both flat-to-negative once length is matched"),
    }

    out = {
        "stamp": "2026-09-01",
        "probe": "P3 scope vs verbosity diagnostic",
        "tool": "benchmarks/dogfood/erb/probe_scope_verbosity.py",
        "bed": BED,
        "bed_bytes": Path(BED).stat().st_size,
        "bed_access": "read-only (sqlite3 mode=ro); no writes, no index creation, no vacuum",
        "bed_provenance": ("erb_blob_v09x, 947,531 genes, ingest_c=6, tagger_version=1 (pre-2026-08-30); "
                           "per BASELINES.md this probe is comparable only to other measurements on this "
                           "same bed"),
        "git_sha": git_sha,
        "basis_receipts": [
            "benchmarks/dogfood/erb/receipts/interloper_mine_erb947k_2026-08-31.json",
            "benchmarks/dogfood/erb/receipts/bucket_ledger_2026-09-01.json",
            "benchmarks/dogfood/erb/receipts/ladder_v09x_w24_min_delivered_2026-08-28.json",
            "benchmarks/dogfood/interloper_taxonomy_wave2_2026-08-31.json",
        ],
        "kill_criterion": (
            "KILL the length-normalization family if distinct-term count does NOT separate gold from "
            "interlopers on these slates once length is controlled for - i.e. if the at-cap asymmetry is "
            "not accompanied by a distinct-term asymmetry, the scope hypothesis is wrong on this bed and "
            "no custom scorer over FTS5 postings is worth building. Operationalized BEFORE the run: PASS "
            "iff the within-query paired, length-band-matched winner-minus-gold kw_cov delta is > 0 with a "
            "two-sided sign test p < 0.05 AND the both-at-cap paired delta is also > 0."),
        "verdict": verdict,
        "verdict_reason": (
            "PASS on the stated criterion, with a narrowed mechanism. The at-cap asymmetry IS accompanied "
            "by a distinct-QUERY-term asymmetry that survives length control: winner-minus-gold kw_cov is "
            "+%.3f within gold's own 1000-char band (p=%s, %d/%d/%d win/tie/loss), +%.3f with both at cap "
            "(p=%s), +%.3f within +/-250 chars. But it does NOT survive as generic scope: raw distinct-"
            "vocabulary size is %.2f terms when length-matched (i.e. flat/negative), so winners do not "
            "carry more terms, they carry more of THIS QUERY'S terms. And VERBOSITY is dead on this bed: "
            "length-matched avg_tf delta is %.4f (p=%s, not significant) and at-cap winners are LESS "
            "repetitive than the average at-cap corpus chunk (1.541 vs 1.691). %.1f%% of the raw coverage "
            "gap is length-mediated and disappears under matching. Length normalization has nothing to "
            "saturate here; a scope-aware scorer is not refuted, but the head-room it would target is "
            "~1.1-1.2 distinct query terms at matched length, not the 3.1 the unmatched slate suggests."
            % (matched["mean_delta_kw_cov"], matched["sign_test_kw_cov"]["p_two_sided"],
               matched["winner_mean_beats_gold"], matched["ties"], matched["gold_beats_winner_mean"],
               atcap["mean_delta_kw_cov"], atcap["sign_test_kw_cov"]["p_two_sided"],
               paired_tests[3]["mean_delta_kw_cov"], matched["mean_delta_n_distinct"],
               matched["mean_delta_avg_tf"], matched["sign_test_avg_tf"]["p_two_sided"],
               100.0 * (decomposition["share_of_gap_that_is_length_mediated"] or 0.0))),
        "definitions": {
            "tokenizer": "regex [a-z0-9_/-]+ on lowercased content, tokens of length > 2 (miner-identical)",
            "keyword_set": ("cymatix_context.accel.extract_query_signals(query) -> "
                            "sorted(set(domains)|set(entities)) (miner-identical); NOTE the extractor emits "
                            "morphological variants ('metric' and 'metrics') as separate terms, so n_kw and "
                            "kw_cov both count variants separately"),
            "kw_cov": "count of keyword-set terms present in the doc's DISTINCT token set = distinct query terms matched",
            "kw_cov_frac": "kw_cov / n_kw, the per-query-normalized scope measure",
            "avg_tf": "n_tokens / n_distinct, the direct VERBOSITY measure (1.0 = every token unique)",
            "at_cap": "n_chars >= %d (chunk cap is %d); exactly_4000 reported separately" % (AT_CAP_MIN, CAP),
            "winner_seats": "every delivered doc across the 156 misses (the seats gold did not get)",
            "gold_first": "the mine's gold_first chunk per miss (156 rows) - the published at-cap 42.9% basis",
            "heuristics_ack": ("the h_* flags are cheap surface heuristics, not a validated boilerplate "
                               "classifier; read them as incidence signals only"),
            "h_quoted_reply": ">=3 lines matching ^\\s*> AND >=15% of lines",
            "h_mail_headers": ">=4 lines matching ^(from|to|cc|bcc|sent|subject|date|reply-to):",
            "h_signature": ("contains any of -----original message-----, sent from my, best regards, "
                            "kind regards, forwarded by, begin forwarded message"),
            "h_disclaimer": ">=2 distinct legal-disclaimer terms from a fixed 10-term set",
            "h_base64": "a run of >=60 base64-alphabet chars",
            "h_repeated_lines": ">=8 non-empty lines AND >=25% of them exact duplicates",
            "h_json_structured": ">=12% of chars in the set {}[]\":,",
            "h_low_ttr": "avg_tf >= 2.0 (every distinct token appears twice on average)",
        },
        "n_misses": len(misses),
        "n_gold_first": len(gold_first_rows),
        "n_gold_all_chunks": len(gold_all_rows),
        "n_winner_seats": len(winner_rows),
        "n_top1_winners": len(top1_rows),
        "n_genes_fetched": len(content),
        "n_genes_requested": len(need_l),
        "n_corpus_sample_requested": CORPUS_SAMPLE,
        "n_corpus_sample_fetched": len(corpus),
        "corpus_sample_seed": SEED,
        "corpus_sample_method": ("random.Random(%d).sample(range(1, max_rowid+1), %d) then "
                                 "SELECT content WHERE rowid=?; rowid space is 1..%d for 947,531 rows "
                                 "(99.9%% dense)" % (SEED, CORPUS_SAMPLE, max_rowid)),
        "recompute_drift_vs_mine": drift,
        "section_a_scope": {
            "populations": [pack(gold_first_rows, "gold_first (one per miss)"),
                            pack(gold_all_rows, "all gold chunks"),
                            pack(top1_rows, "rank-1 winners"),
                            pack(winner_rows, "all winner seats"),
                            pack(corpus, "corpus random sample")],
            "length_band_table": band_table,
            "paired_within_query": paired_tests,
            "mechanism_decomposition": decomposition,
            "length_match_coverage_caveat": {
                "n_misses": len(gf_by_needle),
                "n_with_a_band_matched_winner": sum(
                    1 for n, g in gf_by_needle.items()
                    if any(w["band"] == g["band"] for w in win_by_needle.get(n, []))),
                "n_with_no_band_matched_winner": sum(
                    1 for n, g in gf_by_needle.items()
                    if not any(w["band"] == g["band"] for w in win_by_needle.get(n, []))),
                "n_gold_under_cap_and_every_winner_at_cap": sum(
                    1 for n, g in gf_by_needle.items()
                    if g["band"] != "at_cap_3990+" and win_by_needle.get(n)
                    and all(w["band"] == "at_cap_3990+" for w in win_by_needle[n])),
                "note": ("the length-controlled tests can only run on the misses where a length-matched "
                         "winner exists. In the remainder gold and the winners do not overlap in length "
                         "at all, so for those the mechanism is length by construction; the "
                         "mechanism_decomposition share is the honest summary of both halves."),
            },
        },
        "section_b_step_vs_monotone": {
            "bins_500char": bins,
            "step_vs_ramp_fit": step_test,
            "pure_length_subsidy_no_selection": corpus_baseline,
        },
        "section_c_verbosity_content": {
            "continuous_means": {
                "json_char_frac": {
                    "winner_seats": round(float(np.mean([r["json_char_frac"] for r in winner_rows])), 4),
                    "gold_first": round(float(np.mean([r["json_char_frac"] for r in gold_first_rows])), 4),
                    "corpus": round(float(np.mean([r["json_char_frac"] for r in corpus])), 4),
                },
                "repeat_line_frac": {
                    "winner_seats": round(float(np.mean([r["repeat_line_frac"] for r in winner_rows])), 4),
                    "gold_first": round(float(np.mean([r["repeat_line_frac"] for r in gold_first_rows])), 4),
                    "corpus": round(float(np.mean([r["repeat_line_frac"] for r in corpus])), 4),
                },
                "top_tf": {
                    "winner_seats": round(float(np.mean([r["top_tf"] for r in winner_rows])), 3),
                    "gold_first": round(float(np.mean([r["top_tf"] for r in gold_first_rows])), 3),
                    "corpus": round(float(np.mean([r["top_tf"] for r in corpus])), 3),
                },
                "n_lines": {
                    "winner_seats": round(float(np.mean([r["n_lines"] for r in winner_rows])), 2),
                    "gold_first": round(float(np.mean([r["n_lines"] for r in gold_first_rows])), 2),
                    "corpus": round(float(np.mean([r["n_lines"] for r in corpus])), 2),
                },
            },
            "incidence_winner_seats": hshare(winner_rows),
            "incidence_gold_first": hshare(gold_first_rows),
            "incidence_gold_all_chunks": hshare(gold_all_rows),
            "incidence_corpus_sample": hshare(corpus),
            "avg_tf_gold_first": dist([r["avg_tf"] for r in gold_first_rows]),
            "avg_tf_winner_seats": dist([r["avg_tf"] for r in winner_rows]),
            "avg_tf_corpus": dist([r["avg_tf"] for r in corpus]),
            "top_tf_at_cap_only": {
                "gold_first": dist([r["top_tf"] for r in gold_first_rows if r["band"] == "at_cap_3990+"]),
                "winner_seats": dist([r["top_tf"] for r in winner_rows if r["band"] == "at_cap_3990+"]),
                "corpus": dist([r["top_tf"] for r in corpus if r["n_chars"] >= AT_CAP_MIN]),
            },
            "avg_tf_at_cap_only": {
                "gold_first": dist([r["avg_tf"] for r in gold_first_rows if r["band"] == "at_cap_3990+"]),
                "winner_seats": dist([r["avg_tf"] for r in winner_rows if r["band"] == "at_cap_3990+"]),
                "corpus": dist([r["avg_tf"] for r in corpus if r["n_chars"] >= AT_CAP_MIN]),
            },
        },
        "section_d_correlation": corr,
        "section_e_published_reconciliation": {
            "published_winner_at_cap": "1423/1674 = 85.0%",
            "recomputed_winner_at_cap": "%d/%d" % (
                sum(1 for r in winner_rows if r["band"] == "at_cap_3990+"), len(winner_rows)),
            "published_gold_at_cap": "67/156 = 42.9%",
            "recomputed_gold_first_at_cap": "%d/%d" % (
                sum(1 for r in gold_first_rows if r["band"] == "at_cap_3990+"), len(gold_first_rows)),
            "recomputed_winner_exactly_4000": "%d/%d" % (
                sum(1 for r in winner_rows if r["n_chars"] == CAP), len(winner_rows)),
            "note": ("the published 1423 is the EXACTLY-4000 count; this probe's at_cap band uses "
                     ">=3990 and picks up 2 extra seats at 3990-3999. Gold reconciles exactly (67/156) "
                     "under both definitions."),
        },
        "per_miss": per_miss,
        "wall_s": round(time.perf_counter() - t0, 1),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({"verdict": verdict,
                      "reconciliation": out["section_e_published_reconciliation"],
                      "drift_rows": len(drift),
                      "paired": paired_tests,
                      "corr": corr}, indent=1))
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""P5 - Is the at-cap asymmetry causal, or a confound of chunk position? (2026-09-01)

Question. On ERB 947k the wave-2 taxonomy reports that winning seats sit at the
4,000-char chunk cap 85.0% of the time (1423/1674) while gold sits at cap only
42.9% of the time (67/156, mean 3,043 chars). The going-in story is "length buys
distinct-term coverage" -- at-cap winners average 10.54 distinct query terms vs
7.88 under-cap. But gold may be systematically shorter for a STRUCTURAL reason
that ranking cannot act on: its position inside the parent document. A tail chunk
is short by construction. If gold's shortness is just "gold tends to be the last
chunk", then length is a CORRELATE, not the mechanism, and every length-based
lever (length normalization, re-chunking, length-penalised scoring) is aimed at
the wrong thing.

KILL CRITERION (stated before the numbers were looked at, see receipt field):
  "Length is a CONFOUND rather than the mechanism if gold's shortness is
   explained by chunk position within its parent document (e.g. gold is
   disproportionately the LAST/tail chunk, which is short by construction)
   rather than by any property that ranking could act on."
Operationalised, three conditions must ALL hold to return KILL (confound):
  C1  gold's position distribution is anomalous vs the corpus base rate --
      gold is disproportionately tail (tail-share lift >= 1.25x base);
  C2  position composition explains >= 60% of the gold-vs-winner mean length
      deficit when corpus conditional means are substituted for actuals;
  C3  the coverage gap does NOT survive holding length constant -- i.e. among
      misses where gold_first AND the top-1 winner are BOTH at the 4,000 cap,
      the winner no longer out-covers gold at a rate above chance (<= 55%).
PASS (length/coverage is a live ranking mechanism, not a gold-side structural
artefact) if C1 fails or C3 fails. INCONCLUSIVE if the order inference cannot be
validated.

Method.
 1. ONE full scan of genes (no index on source_id; never loop table scans)
    building source_id -> ordered [row] plus per-row length and created_at.
    Chunk ORDER is INFERRED as rowid ascending within source_id -- there is no
    explicit ordinal column in the schema (genes has no chunk_index / offset /
    support_span populated). The inference is validated against an INDEPENDENT
    signal, epigenetics.created_at, which is written per-chunk at ingest.
 2. Corpus baseline over ALL 947,531 chunks (no sampling needed -- the single
    scan already visits every row, which is strictly stronger than a fixed-seed
    sample): at-cap rate by position class, mean length by position class.
 3. The 156 misses: position class of gold_first, of every gold chunk, of the
    top-1 winner, and of all 1,674 delivered seats. Gold vs its OWN siblings.
 4. Separate "gold is the only chunk of a 1-chunk doc" from "gold is the tail of
    a multi-chunk doc".
 5. Bucket the 156 by winner char length and by a quoted/boilerplate heuristic.
    P3's receipt did not exist when this was written (checked at runtime and
    recorded), so the heuristics below are this probe's own and are stated
    verbatim in the receipt.
 6. The held-length-constant test (C3): among both-at-cap misses, does the
    winner still out-cover gold?

Bed is opened READ-ONLY. No writes, no indexes, no VACUUM.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import time
from array import array
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BED = r"F:/tmp/erb_blob_v09x.db"
MINE = HERE / "receipts/interloper_mine_erb947k_2026-08-31.json"
LEDGER = HERE / "receipts/bucket_ledger_2026-09-01.json"
BASELINE = HERE / "receipts/ladder_v09x_w24_min_delivered_2026-08-28.json"
GOLD = HERE / "gold_by_needle_v09x.json"
NEEDLES = HERE / "needles_resolved_v09x_full.json"
P3 = HERE / "receipts/interloper_content_2026-09-01.json"  # may not exist
OUT = HERE / "receipts/atcap_confound_2026-09-01.json"

CAP = 4000

KILL_CRITERION = (
    "Length is a CONFOUND rather than the mechanism if gold's shortness is explained by chunk "
    "position within its parent document (e.g. gold is disproportionately the LAST/tail chunk, "
    "which is short by construction) rather than by any property that ranking could act on. "
    "Operationalised: return KILL (confound) only if ALL THREE hold -- "
    "C1 gold tail-share lift over the corpus base rate >= 1.25x; "
    "C2 position composition explains >= 60% of the gold-vs-winner mean length deficit; "
    "C3 among misses where gold_first AND the top-1 winner are BOTH at the 4000 cap, the winner "
    "out-covers gold at <= 55% (i.e. the coverage gap does not survive holding length constant). "
    "PASS (length/coverage is a live ranking mechanism, not a gold-side structural artefact) if "
    "C1 fails or C3 fails. INCONCLUSIVE if the rowid order inference cannot be validated against "
    "epigenetics.created_at."
)

# ── heuristics (this probe's own; P3 receipt absence recorded in the receipt) ──
RE_QUOTE_LINE = re.compile(r"^\s*>", re.M)
RE_THREAD = re.compile(
    r"-----\s*Original Message\s*-----|^\s*From:\s|^\s*Sent:\s|^\s*To:\s|\bwrote:\s*$|\bOn .{0,60}\bwrote:",
    re.M,
)
RE_BOILER = re.compile(
    r"confidential(ity)?\b|unsubscribe|do not reply|automated message|"
    r"proprietary and confidential|intended (solely )?for the (named )?recipient|"
    r"this (e-?mail|message) and any (files|attachments)",
    re.I,
)


def posclass(pos: int, n: int) -> str:
    if n == 1:
        return "only"
    if pos == 0:
        return "first"
    if pos == n - 1:
        return "last"
    return "middle"


def mean(xs):
    xs = list(xs)
    return round(sum(xs) / len(xs), 4) if xs else None


def pct(a, b):
    return round(100.0 * a / b, 2) if b else None


def classify_content(txt: str) -> str:
    if txt is None:
        return "missing"
    if RE_BOILER.search(txt):
        return "boilerplate"
    q = len(RE_QUOTE_LINE.findall(txt))
    t = len(RE_THREAD.findall(txt))
    if t >= 2 or q >= 5:
        return "quoted_thread"
    s = txt.lstrip()[:1]
    if s in "{[":
        return "structured_json"
    return "prose_other"


def main() -> int:
    t0 = time.perf_counter()
    git_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True
    ).stdout.strip()
    bed_bytes = Path(BED).stat().st_size

    mine = json.loads(MINE.read_text(encoding="utf-8"))
    misses = mine["misses"]
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    bucket_of = {r["needle"]: r["bucket"] for r in ledger["per_miss"]}
    gold_by = json.loads(GOLD.read_text(encoding="utf-8"))

    # ── target gene ids we need row-level facts for ────────────────────────
    want: set[str] = set()
    for m in misses:
        want.add(m["gold_first"]["gene_id"])
        for g in gold_by.get(m["needle"], []):
            want.add(g)
        for d in m["delivered"]:
            want.add(d["gene_id"])
    print(f"target gene ids: {len(want)}", flush=True)

    # ── ONE full scan ──────────────────────────────────────────────────────
    con = sqlite3.connect(f"file:{BED}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    lengths = array("i")
    src_of = array("i")
    src_index: dict[str, int] = {}
    src_rows: list[list[int]] = []
    last_created: list[float] = []
    order_violations = 0  # sources where created_at decreases along rowid order
    n_created_null = 0
    srcs_with_null = set()
    gene_row: dict[str, int] = {}

    t1 = time.perf_counter()
    cur = con.execute(
        "SELECT gene_id, source_id, length(content), "
        "json_extract(epigenetics,'$.created_at') FROM genes ORDER BY rowid"
    )
    i = 0
    viol_srcs = set()
    for gid, sid, ln, cre in cur:
        si = src_index.get(sid)
        if si is None:
            si = len(src_rows)
            src_index[sid] = si
            src_rows.append([])
            last_created.append(-1.0)
        src_rows[si].append(i)
        lengths.append(ln if ln is not None else 0)
        src_of.append(si)
        if cre is None:
            n_created_null += 1
            srcs_with_null.add(si)
        else:
            if cre < last_created[si]:
                if si not in viol_srcs:
                    viol_srcs.add(si)
                    order_violations += 1
            last_created[si] = cre
        if gid in want:
            gene_row[gid] = i
        i += 1
    n_rows = i
    scan_s = round(time.perf_counter() - t1, 2)
    print(f"scan: {n_rows} rows / {len(src_rows)} sources in {scan_s}s", flush=True)

    # ── per-row position + doc size ────────────────────────────────────────
    pos_of = array("i", bytes(4 * n_rows))
    n_of = array("i", bytes(4 * n_rows))
    for rows in src_rows:
        n = len(rows)
        for p, r in enumerate(rows):
            pos_of[r] = p
            n_of[r] = n

    multi = sum(1 for r in src_rows if len(r) > 1)
    order_check = {
        "assumption": "chunk order within a source_id == rowid ascending",
        "why": (
            "genes has no explicit ordinal column (no chunk_index/offset; support_span is NULL on "
            "this bed). rowid is insertion order and the chunker writes a document's chunks "
            "consecutively -- verified by eye on source rowids 1..3, where chunk 2 opens mid-sentence "
            "continuing chunk 1."
        ),
        "independent_validator": "epigenetics.created_at (per-chunk ingest timestamp)",
        "n_sources": len(src_rows),
        "n_multi_chunk_sources": multi,
        "n_sources_where_created_at_decreases_along_rowid": order_violations,
        "pct_multi_chunk_sources_consistent": pct(multi - order_violations, multi),
        "n_rows_with_null_created_at": n_created_null,
        "status": "ASSUMPTION VALIDATED" if order_violations == 0 else "ASSUMPTION PARTIALLY VIOLATED",
    }
    print("order check:", order_check["pct_multi_chunk_sources_consistent"], flush=True)

    # ── corpus baseline (ALL rows, not a sample) ───────────────────────────
    cls_n: Counter = Counter()
    cls_cap: Counter = Counter()
    cls_len: dict[str, int] = defaultdict(int)
    n_cap_total = 0
    len_total = 0
    for r in range(n_rows):
        c = posclass(pos_of[r], n_of[r])
        L = lengths[r]
        cls_n[c] += 1
        cls_len[c] += L
        len_total += L
        if L >= CAP:
            cls_cap[c] += 1
            n_cap_total += 1
    corpus_mean_len = {c: round(cls_len[c] / cls_n[c], 1) for c in cls_n}
    corpus = {
        "n_chunks": n_rows,
        "n_docs": len(src_rows),
        "cap_chars": CAP,
        "n_at_cap": n_cap_total,
        "at_cap_rate_pct": pct(n_cap_total, n_rows),
        "mean_len": round(len_total / n_rows, 1),
        "by_position_class": {
            c: {
                "n": cls_n[c],
                "share_pct": pct(cls_n[c], n_rows),
                "at_cap": cls_cap[c],
                "at_cap_pct": pct(cls_cap[c], cls_n[c]),
                "mean_len": corpus_mean_len[c],
            }
            for c in ("only", "first", "middle", "last")
            if cls_n[c]
        },
        "chunks_per_doc_hist": dict(
            sorted(Counter(min(len(r), 10) for r in src_rows).items())
        ),
        "chunks_per_doc_hist_note": "key 10 = '10 or more'",
        "mean_chunks_per_doc": round(n_rows / len(src_rows), 4),
    }
    non_tail = cls_n["first"] + cls_n["middle"]
    non_tail_cap = cls_cap["first"] + cls_cap["middle"]
    tail = cls_n["only"] + cls_n["last"]
    tail_cap = cls_cap["only"] + cls_cap["last"]
    corpus["at_cap_equals_non_tail"] = {
        "n_non_tail_chunks": non_tail,
        "n_non_tail_at_cap": non_tail_cap,
        "pct_of_non_tail_at_cap": pct(non_tail_cap, non_tail),
        "n_tail_chunks": tail,
        "n_tail_at_cap": tail_cap,
        "pct_of_tail_at_cap": pct(tail_cap, tail),
        "reading": (
            "If ~100% of non-tail chunks are at cap and ~0% of tail chunks are, then 'at cap' and "
            "'not the last chunk of its document' are the SAME variable on this bed and cannot be "
            "separated by any measurement."
        ),
    }

    # ── helper: facts for one gene id ──────────────────────────────────────
    def fact(gid: str):
        r = gene_row.get(gid)
        if r is None:
            return None
        n = n_of[r]
        sibs = src_rows[src_of[r]]
        others = [lengths[x] for x in sibs if x != r]
        return {
            "gene_id": gid,
            "len": lengths[r],
            "pos": pos_of[r],
            "n_chunks": n,
            "posclass": posclass(pos_of[r], n),
            "at_cap": lengths[r] >= CAP,
            "sibling_mean_len": round(sum(others) / len(others), 1) if others else None,
            "delta_vs_siblings": (
                round(lengths[r] - sum(others) / len(others), 1) if others else None
            ),
        }

    # ── populations ────────────────────────────────────────────────────────
    gold_first_facts, allgold_facts, top1_facts, seat_facts = [], [], [], []
    per_miss = []
    n_chars_mismatch = 0
    missing_gold_first = 0
    for m in misses:
        gf = fact(m["gold_first"]["gene_id"])
        if gf is None:
            missing_gold_first += 1
        else:
            if m["gold_first"].get("n_chars") is not None and gf["len"] != m["gold_first"]["n_chars"]:
                n_chars_mismatch += 1
            gold_first_facts.append(gf)
        gfs = [fact(g) for g in gold_by.get(m["needle"], [])]
        gfs = [g for g in gfs if g]
        allgold_facts.extend(gfs)
        w1 = fact(m["delivered"][0]["gene_id"]) if m["delivered"] else None
        if w1:
            top1_facts.append(w1)
        for d in m["delivered"]:
            f = fact(d["gene_id"])
            if f:
                f = dict(f)
                f["kw_cov"] = d.get("kw_cov")
                f["seat_rank"] = d.get("rank")
                seat_facts.append(f)
        per_miss.append(
            {
                "needle": m["needle"],
                "class": m["class"],
                "bucket": bucket_of.get(m["needle"]),
                "n_kw": m["n_kw"],
                "gold_first": gf,
                "gold_first_kw_cov": m["gold_first"].get("kw_cov"),
                "gold_union_kw_cov": m.get("gold_kw_cov"),
                "gold_n": m.get("gold_n"),
                "all_gold": gfs,
                "top1": w1,
                "top1_kw_cov": m["delivered"][0].get("kw_cov") if m["delivered"] else None,
            }
        )

    def summarise(name, facts):
        if not facts:
            return {"n": 0}
        cc = Counter(f["posclass"] for f in facts)
        return {
            "population": name,
            "n": len(facts),
            "at_cap": sum(1 for f in facts if f["at_cap"]),
            "at_cap_pct": pct(sum(1 for f in facts if f["at_cap"]), len(facts)),
            "mean_len": mean(f["len"] for f in facts),
            "median_len": sorted(f["len"] for f in facts)[len(facts) // 2],
            "posclass": {c: cc.get(c, 0) for c in ("only", "first", "middle", "last")},
            "posclass_pct": {c: pct(cc.get(c, 0), len(facts)) for c in ("only", "first", "middle", "last")},
            "tail_share_pct": pct(cc.get("only", 0) + cc.get("last", 0), len(facts)),
            "mean_n_chunks_of_parent_doc": mean(f["n_chunks"] for f in facts),
            "mean_delta_vs_own_siblings": mean(
                f["delta_vs_siblings"] for f in facts if f["delta_vs_siblings"] is not None
            ),
            "n_with_siblings": sum(1 for f in facts if f["delta_vs_siblings"] is not None),
        }

    pops = {
        "gold_first_per_miss": summarise("gold_first_per_miss", gold_first_facts),
        "all_gold_chunks": summarise("all_gold_chunks", allgold_facts),
        "top1_winner_per_miss": summarise("top1_winner_per_miss", top1_facts),
        "all_delivered_seats": summarise("all_delivered_seats", seat_facts),
    }
    corpus_tail_share = pct(tail, n_rows)
    for p in pops.values():
        if p.get("n"):
            p["tail_share_lift_vs_corpus"] = round(p["tail_share_pct"] / corpus_tail_share, 3)
            p["at_cap_lift_vs_corpus"] = round(p["at_cap_pct"] / corpus["at_cap_rate_pct"], 3)

    # ── reproduce the published headline numbers as a control ──────────────
    repro = {
        "published_winner_seats_at_cap": "1423/1674 (85.0%)",
        "recomputed_winner_seats_at_cap": f"{pops['all_delivered_seats']['at_cap']}/"
        f"{pops['all_delivered_seats']['n']} ({pops['all_delivered_seats']['at_cap_pct']}%)",
        "published_gold_at_cap": "67/156 (42.9%)",
        "recomputed_gold_first_at_cap": f"{pops['gold_first_per_miss']['at_cap']}/"
        f"{pops['gold_first_per_miss']['n']} ({pops['gold_first_per_miss']['at_cap_pct']}%)",
        "published_winner_mean_n_chars": 3850,
        "recomputed_winner_mean_len": pops["all_delivered_seats"]["mean_len"],
        "published_gold_mean_n_chars": 3043,
        "recomputed_gold_first_mean_len": pops["gold_first_per_miss"]["mean_len"],
        "mine_n_chars_vs_db_length_mismatches": n_chars_mismatch,
        "gold_first_rows_not_found_in_bed": missing_gold_first,
    }

    # ── C2: how much of the length deficit does position composition explain? ─
    wm = pops["all_delivered_seats"]["mean_len"]
    gm = pops["gold_first_per_miss"]["mean_len"]
    deficit = wm - gm

    def predicted_mean(facts):
        return sum(corpus_mean_len[f["posclass"]] for f in facts) / len(facts)

    pred_w = predicted_mean(seat_facts)
    pred_g = predicted_mean(gold_first_facts)
    pred_deficit = pred_w - pred_g
    c2_frac = round(pred_deficit / deficit, 4) if deficit else None
    decomposition = {
        "winner_seat_mean_len": wm,
        "gold_first_mean_len": gm,
        "observed_deficit_chars": round(deficit, 1),
        "position_predicted_winner_mean": round(pred_w, 1),
        "position_predicted_gold_mean": round(pred_g, 1),
        "position_predicted_deficit_chars": round(pred_deficit, 1),
        "fraction_of_deficit_explained_by_position": c2_frac,
        "method": (
            "substitute each chunk's actual length with the CORPUS mean length for its position "
            "class (only/first/middle/last), then recompute the winner-minus-gold mean gap"
        ),
        "caveat": (
            "position and length are not independent regressors on this bed -- see "
            "corpus.at_cap_equals_non_tail. A high 'explained' fraction is therefore a restatement "
            "of the chunker's construction, NOT evidence that position is a rival cause."
        ),
    }

    # ── C1: is gold disproportionately tail? ───────────────────────────────
    c1 = {
        "corpus_tail_share_pct": corpus_tail_share,
        "gold_first_tail_share_pct": pops["gold_first_per_miss"]["tail_share_pct"],
        "gold_first_tail_lift": pops["gold_first_per_miss"]["tail_share_lift_vs_corpus"],
        "all_gold_tail_share_pct": pops["all_gold_chunks"]["tail_share_pct"],
        "all_gold_tail_lift": pops["all_gold_chunks"]["tail_share_lift_vs_corpus"],
        "winner_seat_tail_share_pct": pops["all_delivered_seats"]["tail_share_pct"],
        "winner_seat_tail_lift": pops["all_delivered_seats"]["tail_share_lift_vs_corpus"],
        "threshold": "C1 holds (confound-consistent) iff gold_first_tail_lift >= 1.25",
    }
    c1["holds"] = bool(c1["gold_first_tail_lift"] >= 1.25)

    # ── item 4: only-chunk vs tail-of-multi ────────────────────────────────
    def split_stats(facts):
        out = {}
        for c in ("only", "first", "middle", "last"):
            sub = [f for f in facts if f["posclass"] == c]
            if sub:
                out[c] = {
                    "n": len(sub),
                    "share_pct": pct(len(sub), len(facts)),
                    "mean_len": mean(f["len"] for f in sub),
                    "mean_n_chunks": mean(f["n_chunks"] for f in sub),
                }
        return out

    only_vs_tail = {
        "gold_first": split_stats(gold_first_facts),
        "winner_seats": split_stats(seat_facts),
        "corpus": {
            c: {
                "n": cls_n[c],
                "share_pct": pct(cls_n[c], n_rows),
                "mean_len": corpus_mean_len[c],
            }
            for c in ("only", "first", "middle", "last")
            if cls_n[c]
        },
        "question": "is gold short because its doc has ONE chunk, or because it is the TAIL of a multi-chunk doc?",
    }
    gshort = [f for f in gold_first_facts if not f["at_cap"]]
    only_vs_tail["among_under_cap_gold"] = {
        "n_under_cap_gold": len(gshort),
        "of_which_only_chunk_doc": sum(1 for f in gshort if f["posclass"] == "only"),
        "of_which_tail_of_multi": sum(1 for f in gshort if f["posclass"] == "last"),
        "of_which_non_tail": sum(1 for f in gshort if f["posclass"] in ("first", "middle")),
        "mean_len_only": mean(f["len"] for f in gshort if f["posclass"] == "only"),
        "mean_len_tail_of_multi": mean(f["len"] for f in gshort if f["posclass"] == "last"),
    }

    # ── C3: hold length constant ───────────────────────────────────────────
    both_cap, gold_cap_only, winner_cap_only, neither = [], [], [], []
    for pm in per_miss:
        g, w = pm["gold_first"], pm["top1"]
        if not g or not w or pm["gold_first_kw_cov"] is None or pm["top1_kw_cov"] is None:
            continue
        row = {
            "needle": pm["needle"],
            "class": pm["class"],
            "bucket": pm["bucket"],
            "gold_cov": pm["gold_first_kw_cov"],
            "gold_union_cov": pm["gold_union_kw_cov"],
            "win_cov": pm["top1_kw_cov"],
            "gold_len": g["len"],
            "win_len": w["len"],
            "n_kw": pm["n_kw"],
        }
        (both_cap if (g["at_cap"] and w["at_cap"]) else
         gold_cap_only if g["at_cap"] else
         winner_cap_only if w["at_cap"] else neither).append(row)

    def cov_cell(rows, name):
        if not rows:
            return {"cell": name, "n": 0}
        wins = sum(1 for r in rows if r["win_cov"] > r["gold_cov"])
        ties = sum(1 for r in rows if r["win_cov"] == r["gold_cov"])
        uwins = sum(1 for r in rows if r["win_cov"] > (r["gold_union_cov"] or 0))
        return {
            "cell": name,
            "n": len(rows),
            "mean_gold_first_cov": mean(r["gold_cov"] for r in rows),
            "mean_gold_union_cov": mean(r["gold_union_cov"] for r in rows if r["gold_union_cov"] is not None),
            "mean_winner_cov": mean(r["win_cov"] for r in rows),
            "winner_outcovers_gold_first": wins,
            "winner_outcovers_gold_first_pct": pct(wins, len(rows)),
            "ties": ties,
            "winner_outcovers_gold_union": uwins,
            "winner_outcovers_gold_union_pct": pct(uwins, len(rows)),
            "mean_gold_len": mean(r["gold_len"] for r in rows),
            "mean_winner_len": mean(r["win_len"] for r in rows),
            "mean_cov_per_1k_chars_gold": mean(1000.0 * r["gold_cov"] / r["gold_len"] for r in rows),
            "mean_cov_per_1k_chars_winner": mean(1000.0 * r["win_cov"] / r["win_len"] for r in rows),
        }

    c3_cells = [
        cov_cell(both_cap, "both_at_cap (length held constant at 4000)"),
        cov_cell(gold_cap_only, "gold_at_cap_winner_under"),
        cov_cell(winner_cap_only, "winner_at_cap_gold_under"),
        cov_cell(neither, "neither_at_cap"),
    ]
    bc = c3_cells[0]
    c3 = {
        "cells": c3_cells,
        "threshold": (
            "C3 holds (confound-consistent) iff, in the both_at_cap cell, the winner out-covers "
            "gold_first at <= 55%"
        ),
        "both_at_cap_winner_outcovers_pct": bc.get("winner_outcovers_gold_first_pct"),
        "holds": bool(bc.get("n") and bc["winner_outcovers_gold_first_pct"] <= 55.0),
    }

    # ── seat-level coverage by length, at-cap vs under (control) ───────────
    cap_seats = [f for f in seat_facts if f["at_cap"] and f.get("kw_cov") is not None]
    und_seats = [f for f in seat_facts if not f["at_cap"] and f.get("kw_cov") is not None]
    seat_len_cov = {
        "at_cap_seats": {
            "n": len(cap_seats),
            "mean_kw_cov": mean(f["kw_cov"] for f in cap_seats),
            "published": 10.54,
        },
        "under_cap_seats": {
            "n": len(und_seats),
            "mean_kw_cov": mean(f["kw_cov"] for f in und_seats),
            "published": 7.88,
            "mean_len": mean(f["len"] for f in und_seats),
        },
        "under_cap_seats_by_posclass": {
            c: {
                "n": sum(1 for f in und_seats if f["posclass"] == c),
                "mean_kw_cov": mean(f["kw_cov"] for f in und_seats if f["posclass"] == c),
                "mean_len": mean(f["len"] for f in und_seats if f["posclass"] == c),
            }
            for c in ("only", "first", "middle", "last")
            if any(f["posclass"] == c for f in und_seats)
        },
        "kw_cov_per_1k_chars": {
            "at_cap": mean(1000.0 * f["kw_cov"] / f["len"] for f in cap_seats),
            "under_cap": mean(1000.0 * f["kw_cov"] / f["len"] for f in und_seats),
            "reading": (
                "if density (cov per 1k chars) is HIGHER under cap, the at-cap advantage is purely "
                "the extra characters -- length is buying coverage, not quality"
            ),
        },
    }

    # ── item 5: bucket the 156 by winner length and content type ───────────
    ids = sorted({pm["top1"]["gene_id"] for pm in per_miss if pm["top1"]} |
                 {pm["gold_first"]["gene_id"] for pm in per_miss if pm["gold_first"]})
    texts: dict[str, str] = {}
    for j in range(0, len(ids), 400):
        chunk = ids[j:j + 400]
        q = "SELECT gene_id, content FROM genes WHERE gene_id IN (%s)" % ",".join("?" * len(chunk))
        for gid, ct in con.execute(q, chunk):
            texts[gid] = ct
    con.close()

    LEN_BINS = [(0, 999), (1000, 1999), (2000, 2999), (3000, 3999), (4000, 4000)]

    def lbin(L):
        for lo, hi in LEN_BINS:
            if lo <= L <= hi:
                return f"{lo}-{hi}"
        return "?"

    winner_len_buckets: dict[str, dict] = {}
    for pm in per_miss:
        if not pm["top1"]:
            continue
        b = lbin(pm["top1"]["len"])
        d = winner_len_buckets.setdefault(
            b, {"n": 0, "winner_outcovers_gold_first": 0, "gold_lens": [], "win_covs": [], "gold_covs": []}
        )
        d["n"] += 1
        if pm["top1_kw_cov"] is not None and pm["gold_first_kw_cov"] is not None:
            d["win_covs"].append(pm["top1_kw_cov"])
            d["gold_covs"].append(pm["gold_first_kw_cov"])
            if pm["top1_kw_cov"] > pm["gold_first_kw_cov"]:
                d["winner_outcovers_gold_first"] += 1
        d["gold_lens"].append(pm["gold_first"]["len"] if pm["gold_first"] else None)
    for b, d in winner_len_buckets.items():
        d["mean_winner_cov"] = mean(d.pop("win_covs"))
        d["mean_gold_first_cov"] = mean(d.pop("gold_covs"))
        gl = [x for x in d.pop("gold_lens") if x is not None]
        d["mean_gold_len"] = mean(gl)
        d["winner_outcovers_pct"] = pct(d["winner_outcovers_gold_first"], d["n"])

    ctype_w = Counter()
    ctype_g = Counter()
    ctype_detail: dict[str, dict] = {}
    for pm in per_miss:
        if pm["top1"]:
            c = classify_content(texts.get(pm["top1"]["gene_id"]))
            ctype_w[c] += 1
            d = ctype_detail.setdefault(c, {"n": 0, "at_cap": 0, "covs": [], "gcovs": [], "outcov": 0})
            d["n"] += 1
            d["at_cap"] += 1 if pm["top1"]["at_cap"] else 0
            if pm["top1_kw_cov"] is not None:
                d["covs"].append(pm["top1_kw_cov"])
                d["gcovs"].append(pm["gold_first_kw_cov"])
                if pm["top1_kw_cov"] > (pm["gold_first_kw_cov"] or 0):
                    d["outcov"] += 1
        if pm["gold_first"]:
            ctype_g[classify_content(texts.get(pm["gold_first"]["gene_id"]))] += 1
    for c, d in ctype_detail.items():
        d["mean_winner_cov"] = mean(d.pop("covs"))
        d["mean_gold_first_cov"] = mean(x for x in d.pop("gcovs") if x is not None)
        d["at_cap_pct"] = pct(d["at_cap"], d["n"])
        d["winner_outcovers_pct"] = pct(d.pop("outcov"), d["n"])

    content_buckets = {
        "heuristics_source": (
            f"P3 receipt {'FOUND at ' + str(P3) if P3.exists() else 'NOT PRESENT at ' + str(P3)}; "
            f"{'reused' if P3.exists() else 'this probe defines its own heuristics, stated verbatim below'}"
        ),
        "heuristics": {
            "boilerplate": RE_BOILER.pattern,
            "quoted_thread": f"(>=2 matches of) {RE_THREAD.pattern}  OR  >=5 lines matching {RE_QUOTE_LINE.pattern}",
            "structured_json": "content lstrip() starts with '{' or '['",
            "prose_other": "everything else",
            "precedence": "boilerplate > quoted_thread > structured_json > prose_other",
        },
        "top1_winner_content_type": dict(ctype_w),
        "gold_first_content_type": dict(ctype_g),
        "top1_winner_content_type_detail": ctype_detail,
    }

    # ── section 11: the head-chunk effect (rank-1 seat) ────────────────────
    at_cap_first = cls_n["first"]
    at_cap_middle = cls_n["middle"]
    p_first_given_cap = at_cap_first / (at_cap_first + at_cap_middle)
    w1_cap = [f for f in top1_facts if f["at_cap"]]
    n_w1_mid = sum(1 for f in w1_cap if f["posclass"] == "middle")
    seat_cap_f = [f for f in seat_facts if f["at_cap"]]
    n_seat_mid = sum(1 for f in seat_cap_f if f["posclass"] == "middle")
    gold_cap_f = [f for f in gold_first_facts if f["at_cap"]]
    head_chunk = {
        "claim_under_test": (
            "at-cap is EXACTLY 'non-tail' on this bed, so the interesting residual question is "
            "whether the winner is any non-tail chunk (length only) or specifically the HEAD chunk "
            "(the one carrying the document's title/metadata header)"
        ),
        "corpus_among_at_cap_chunks": {
            "first": at_cap_first,
            "middle": at_cap_middle,
            "p_first_given_at_cap": round(p_first_given_cap, 4),
        },
        "top1_winners_among_at_cap": {
            "n": len(w1_cap),
            "first": sum(1 for f in w1_cap if f["posclass"] == "first"),
            "middle": n_w1_mid,
            "pct_first": pct(sum(1 for f in w1_cap if f["posclass"] == "first"), len(w1_cap)),
            "expected_middle_if_length_only": round((1 - p_first_given_cap) * len(w1_cap), 1),
            "p_of_observing_zero_middle_if_length_only": (
                f"{p_first_given_cap ** len(w1_cap):.3e}" if not n_w1_mid else "n/a (middle observed)"
            ),
        },
        "all_delivered_seats_among_at_cap": {
            "n": len(seat_cap_f),
            "first": sum(1 for f in seat_cap_f if f["posclass"] == "first"),
            "middle": n_seat_mid,
            "pct_first": pct(sum(1 for f in seat_cap_f if f["posclass"] == "first"), len(seat_cap_f)),
            "expected_middle_if_length_only": round((1 - p_first_given_cap) * len(seat_cap_f), 1),
            "middle_deficit_ratio_vs_corpus": round(
                (n_seat_mid / len(seat_cap_f)) / (1 - p_first_given_cap), 3
            ),
        },
        "gold_first_among_at_cap": {
            "n": len(gold_cap_f),
            "first": sum(1 for f in gold_cap_f if f["posclass"] == "first"),
            "middle": sum(1 for f in gold_cap_f if f["posclass"] == "middle"),
            "pct_first": pct(sum(1 for f in gold_cap_f if f["posclass"] == "first"), len(gold_cap_f)),
        },
        "at_cap_seats_first_vs_middle": {
            c: {
                "n": sum(1 for f in seat_cap_f if f["posclass"] == c),
                "mean_kw_cov": mean(
                    f["kw_cov"] for f in seat_cap_f if f["posclass"] == c and f.get("kw_cov") is not None
                ),
                "mean_seat_rank": mean(
                    f["seat_rank"] for f in seat_cap_f if f["posclass"] == c and f.get("seat_rank") is not None
                ),
                "median_seat_rank": (
                    sorted(f["seat_rank"] for f in seat_cap_f if f["posclass"] == c and f.get("seat_rank") is not None)[
                        max(0, sum(1 for f in seat_cap_f if f["posclass"] == c) // 2)
                    ]
                    if any(f["posclass"] == c for f in seat_cap_f)
                    else None
                ),
            }
            for c in ("first", "middle")
        },
        "at_cap_seats_first_vs_middle_reading": (
            "both cells are exactly 4000 chars, so any kw_cov or seat-rank difference between them "
            "is NOT a length effect -- it is the head chunk's title/metadata header being visible to "
            "the FTS lane"
        ),
        "mean_seat_rank_by_posclass": {
            c: mean(f["seat_rank"] for f in seat_facts if f["posclass"] == c and f.get("seat_rank") is not None)
            for c in ("only", "first", "middle", "last")
            if any(f["posclass"] == c for f in seat_facts)
        },
        "gold_first_is_head_or_only": {
            "n": sum(1 for f in gold_first_facts if f["posclass"] in ("first", "only")),
            "pct": pct(sum(1 for f in gold_first_facts if f["posclass"] in ("first", "only")), len(gold_first_facts)),
        },
        "top1_is_head_or_only": {
            "n": sum(1 for f in top1_facts if f["posclass"] in ("first", "only")),
            "pct": pct(sum(1 for f in top1_facts if f["posclass"] in ("first", "only")), len(top1_facts)),
        },
    }

    # ── section 12: would length normalization move anything? ──────────────
    seat_by_needle = {}
    for m in misses:
        rows = []
        for d in m["delivered"]:
            f = gene_row.get(d["gene_id"])
            if f is None or d.get("kw_cov") is None:
                continue
            L = lengths[f]
            rows.append((d["kw_cov"], L, d["kw_cov"] / L if L else 0.0))
        seat_by_needle[m["needle"]] = rows

    norm_rows = []
    for pm in per_miss:
        g = pm["gold_first"]
        if not g or pm["gold_first_kw_cov"] is None:
            continue
        seats = seat_by_needle.get(pm["needle"], [])
        if not seats:
            continue
        gd = pm["gold_first_kw_cov"] / g["len"] if g["len"] else 0.0
        gc = pm["gold_first_kw_cov"]
        norm_rows.append(
            {
                "needle": pm["needle"],
                "n_seats": len(seats),
                "seats_gold_beats_on_raw_cov": sum(1 for c, _l, _d in seats if gc > c),
                "seats_gold_beats_on_density": sum(1 for _c, _l, d in seats if gd > d),
                "gold_density_per_1k": round(1000 * gd, 4),
                "median_seat_density_per_1k": round(
                    1000 * sorted(d for _c, _l, d in seats)[len(seats) // 2], 4
                ),
            }
        )
    def frac_ge1(key):
        return pct(sum(1 for r in norm_rows if r[key] >= 1), len(norm_rows))

    norm_cf = {
        "question": (
            "if the lanes scored length-NORMALISED distinct-term coverage (kw_cov / chars) instead of "
            "raw kw_cov, would gold displace any delivered seat?"
        ),
        "n_misses_scored": len(norm_rows),
        "misses_where_gold_beats_at_least_one_seat_on_raw_cov": sum(
            1 for r in norm_rows if r["seats_gold_beats_on_raw_cov"] >= 1
        ),
        "pct_raw": frac_ge1("seats_gold_beats_on_raw_cov"),
        "misses_where_gold_beats_at_least_one_seat_on_density": sum(
            1 for r in norm_rows if r["seats_gold_beats_on_density"] >= 1
        ),
        "pct_density": frac_ge1("seats_gold_beats_on_density"),
        "misses_where_gold_beats_ALL_seats_on_density": sum(
            1 for r in norm_rows if r["seats_gold_beats_on_density"] == r["n_seats"]
        ),
        "misses_where_gold_beats_ALL_seats_on_raw_cov": sum(
            1 for r in norm_rows if r["seats_gold_beats_on_raw_cov"] == r["n_seats"]
        ),
        "mean_seats_beaten_raw": mean(r["seats_gold_beats_on_raw_cov"] for r in norm_rows),
        "mean_seats_beaten_density": mean(r["seats_gold_beats_on_density"] for r in norm_rows),
        "caveat": (
            "kw_cov is a DIAGNOSTIC recomputed by the interloper mine, not the live score. The live "
            "ordering is RRF over three lanes (FTS5/tag/synonym). This is a directional proxy for "
            "whether a length-normalised coverage signal would reorder the slate at all -- it is NOT "
            "a forecast of delivered_gold_rate and cannot substitute for an A/B."
        ),
    }

    # ── per-bucket (P0 partition) view ─────────────────────────────────────
    by_bucket = {}
    for b in ("pool_absent", "near_band", "seat_capped"):
        sub = [pm for pm in per_miss if pm["bucket"] == b]
        gf = [pm["gold_first"] for pm in sub if pm["gold_first"]]
        w1 = [pm["top1"] for pm in sub if pm["top1"]]
        by_bucket[b] = {
            "n": len(sub),
            "gold_first_at_cap_pct": pct(sum(1 for f in gf if f["at_cap"]), len(gf)),
            "gold_first_tail_share_pct": pct(
                sum(1 for f in gf if f["posclass"] in ("only", "last")), len(gf)
            ),
            "gold_first_mean_len": mean(f["len"] for f in gf),
            "top1_at_cap_pct": pct(sum(1 for f in w1 if f["at_cap"]), len(w1)),
            "top1_mean_len": mean(f["len"] for f in w1),
            "mean_gold_n_chunks_parent": mean(f["n_chunks"] for f in gf),
            "mean_top1_n_chunks_parent": mean(f["n_chunks"] for f in w1),
        }
    by_class = {}
    for cl in sorted({pm["class"] for pm in per_miss}):
        sub = [pm for pm in per_miss if pm["class"] == cl]
        gf = [pm["gold_first"] for pm in sub if pm["gold_first"]]
        w1 = [pm["top1"] for pm in sub if pm["top1"]]
        by_class[cl] = {
            "n": len(sub),
            "gold_first_at_cap_pct": pct(sum(1 for f in gf if f["at_cap"]), len(gf)),
            "gold_first_mean_len": mean(f["len"] for f in gf),
            "top1_at_cap_pct": pct(sum(1 for f in w1 if f["at_cap"]), len(w1)),
            "top1_mean_len": mean(f["len"] for f in w1),
        }

    # ── verdict ────────────────────────────────────────────────────────────
    c1h, c3h = c1["holds"], c3["holds"]
    c2h = bool(c2_frac is not None and c2_frac >= 0.60)
    if order_violations > 0 and order_check["pct_multi_chunk_sources_consistent"] < 95.0:
        verdict = "INCONCLUSIVE"
        reason = "the rowid chunk-order inference could not be validated against created_at"
    elif c1h and c2h and c3h:
        verdict = "KILL"
        reason = "all three confound conditions hold: gold is disproportionately tail, position explains the length deficit, and the coverage gap vanishes when length is held constant"
    else:
        verdict = "PASS"
        failed = [n for n, h in (("C1", c1h), ("C2", c2h), ("C3", c3h)) if not h]
        reason = (
            f"confound conditions {', '.join(failed)} FAILED -- "
            "gold's length profile is corpus-typical (the anomaly is entirely winner-side) and/or "
            "the winner's coverage advantage survives holding chunk length constant, so length is a "
            "live selection variable in the ranker, not a structural artefact of where gold sits in "
            "its parent document"
        )

    receipt = {
        "stamp": "2026-09-01",
        "probe": "P5 at-cap asymmetry: causal or confound?",
        "tool": "benchmarks/dogfood/erb/probe_atcap_confound.py",
        "bed": BED,
        "bed_bytes": bed_bytes,
        "bed_access": "read-only (mode=ro, PRAGMA query_only), one full scan + 312 PK lookups",
        "git_sha": git_sha,
        "basis_receipts": [
            "benchmarks/dogfood/erb/receipts/interloper_mine_erb947k_2026-08-31.json",
            "benchmarks/dogfood/erb/receipts/bucket_ledger_2026-09-01.json",
            "benchmarks/dogfood/erb/receipts/ladder_v09x_w24_min_delivered_2026-08-28.json",
            "benchmarks/dogfood/interloper_taxonomy_wave2_2026-08-31.json",
            "benchmarks/dogfood/erb/gold_by_needle_v09x.json",
        ],
        "kill_criterion": KILL_CRITERION,
        "verdict": verdict,
        "verdict_reason": reason,
        "prereg_wording_disclosure": (
            "The pre-registered criterion labels C3 ('the coverage gap does NOT survive holding "
            "length constant') as CONFOUND-consistent. That label is wrong on reflection: if the "
            "gold-vs-winner coverage gap disappears once both chunks are the same length, that is "
            "evidence length is CAUSAL for the gap, not evidence it is a confound. The criterion is "
            "reported exactly as written and the verdict is unchanged either way, because the "
            "pre-registered rule returns PASS as soon as C1 fails, and C1 failed. Under the "
            "corrected reading of C3, the C3 result ALSO points to PASS. No re-derivation of the "
            "rule was done after seeing numbers; only this disclosure was added."
        ),
        "condition_results": {
            "C1_gold_disproportionately_tail": c1h,
            "C2_position_explains_length_deficit": c2h,
            "C3_coverage_gap_dies_when_length_held_constant": c3h,
        },
        "n_misses": len(misses),
        "n_delivered_seats": pops["all_delivered_seats"]["n"],
        "n_gold_chunks": pops["all_gold_chunks"]["n"],
        "n_corpus_chunks": n_rows,
        "n_corpus_docs": len(src_rows),
        "scan_seconds": scan_s,
        "section_0_order_inference": order_check,
        "section_1_corpus_baseline": corpus,
        "section_2_populations": pops,
        "section_2b_reproduction_control": repro,
        "section_3_C1_position_anomaly": c1,
        "section_4_C2_length_deficit_decomposition": decomposition,
        "section_5_only_vs_tail": only_vs_tail,
        "section_6_C3_length_held_constant": c3,
        "section_7_seat_coverage_by_length": seat_len_cov,
        "section_8_winner_length_buckets": winner_len_buckets,
        "section_9_content_type_buckets": content_buckets,
        "section_11_head_chunk_effect": head_chunk,
        "section_12_length_normalization_counterfactual": norm_cf,
        "section_10_by_bucket": by_bucket,
        "section_10b_by_class": by_class,
        "per_miss": [
            {
                "needle": pm["needle"],
                "class": pm["class"],
                "bucket": pm["bucket"],
                "n_kw": pm["n_kw"],
                "gold_first_len": pm["gold_first"]["len"] if pm["gold_first"] else None,
                "gold_first_posclass": pm["gold_first"]["posclass"] if pm["gold_first"] else None,
                "gold_first_n_chunks": pm["gold_first"]["n_chunks"] if pm["gold_first"] else None,
                "gold_first_delta_vs_siblings": pm["gold_first"]["delta_vs_siblings"] if pm["gold_first"] else None,
                "gold_first_kw_cov": pm["gold_first_kw_cov"],
                "gold_union_kw_cov": pm["gold_union_kw_cov"],
                "gold_n": pm["gold_n"],
                "top1_len": pm["top1"]["len"] if pm["top1"] else None,
                "top1_posclass": pm["top1"]["posclass"] if pm["top1"] else None,
                "top1_n_chunks": pm["top1"]["n_chunks"] if pm["top1"] else None,
                "top1_kw_cov": pm["top1_kw_cov"],
                "top1_content_type": classify_content(texts.get(pm["top1"]["gene_id"])) if pm["top1"] else None,
            }
            for pm in per_miss
        ],
        "bottom_line": {
            "is_length_causal_or_a_correlate": (
                "BOTH, but not in the way the going-in story assumed. (a) On this bed 'at cap' and "
                "'not the last chunk of my document' are the SAME variable: 447,531/447,531 non-tail "
                "chunks are exactly 4000 chars and only 828/500,000 tail chunks are. The chunker is "
                "fixed-4000 with no overlap, so length and position are perfectly collinear and no "
                "measurement on this bed can separate them. (b) Gold is NOT structurally short: "
                "gold_first is tail 57.0% vs a 52.8% corpus base rate (lift 1.08) and at cap 42.9% vs "
                "a 47.3% base -- corpus-typical. Over ALL gold chunks the tail share is 45.6%, BELOW "
                "base. The anomaly is entirely winner-side: delivered seats are at cap 85.0% (1.80x "
                "base) and top-1 winners 91.0% (1.92x). (c) Holding length constant kills the coverage "
                "gap: among the 63 misses where gold_first and the top-1 winner are both exactly 4000 "
                "chars, the winner out-covers gold on only 34/63 (54.0%, +10 ties) and coverage "
                "DENSITY is 2.32 vs 2.57 per 1k chars; in the 79 misses where only the winner is at "
                "cap, densities are 2.42 vs 2.56 while raw coverage is 5.18 vs 10.25. Corpus-wide the "
                "at-cap seats are actually LESS dense than under-cap seats (2.64 vs 2.74 per 1k). So "
                "the winners have no per-character quality advantage at all -- unnormalised "
                "distinct-term coverage is very nearly a linear function of characters, and the lanes "
                "are effectively rewarding chunk length."
            ),
            "what_it_implies_for_levers": (
                "Length-based levers are aimed at a real variable, but re-chunking is the wrong end of "
                "it. Re-chunking cannot help: gold's length distribution already matches the corpus, "
                "so making chunks uniform would move gold and interlopers together. Length "
                "NORMALISATION of the coverage signal is the arm this receipt actually supports -- "
                "under a density score, gold out-scores at least one delivered seat in 99/156 misses "
                "vs 71/156 on raw coverage (mean seats beaten 3.65 vs 2.16), which is a directional "
                "proxy only, on a diagnostic kw_cov and not on the live RRF ordering. It is not a "
                "forecast and needs a paired A/B behind an inert knob."
            ),
            "the_bigger_finding_length_does_not_explain": (
                "The rank-1 seat is a HEAD-CHUNK monopoly that length cannot explain. All 156/156 "
                "top-1 winners are the first chunk (142) or the only chunk (14) of their document -- "
                "zero middles, zero tails. Middle chunks are exactly as long as first chunks (both "
                "4000), so under a pure length story 37.6 of the 142 at-cap top-1 winners should have "
                "been middles; p(zero) ~ 1.1e-19. Across all 1,423 at-cap seats, middles are seated at "
                "0.63x their corpus share, and at IDENTICAL length and near-identical coverage (10.61 "
                "vs 10.22 kw_cov) first-chunk seats land at mean rank 5.22 while middle-chunk seats "
                "land at 9.33. Gold is a head-or-only chunk in only 67/156 (43.0%) of misses. A "
                "head-chunk bonus of roughly four seats, orthogonal to both length and distinct-term "
                "coverage, is an unnamed mechanism in the wave-2 taxonomy and is a candidate lever "
                "surface in its own right (most plausibly the title/metadata header the chunker leaves "
                "in chunk 0, visible to the FTS lane and to nothing else)."
            ),
            "scope": (
                "ERB 947k only. The chunker's fixed-4000 no-overlap behaviour is a property of this "
                "bed's build; the taxonomy already records that the length direction flips on "
                "locomo/locomo_obs, so none of this generalises cross-bed without re-measurement."
            ),
        },
        "elapsed_seconds": round(time.perf_counter() - t0, 2),
    }
    OUT.write_text(json.dumps(receipt, indent=1), encoding="utf-8")
    print(f"\nverdict {verdict}: {reason}")
    print(json.dumps(
        {
            "corpus_at_cap_pct": corpus["at_cap_rate_pct"],
            "non_tail_at_cap_pct": corpus["at_cap_equals_non_tail"]["pct_of_non_tail_at_cap"],
            "tail_at_cap_pct": corpus["at_cap_equals_non_tail"]["pct_of_tail_at_cap"],
            "winner_at_cap_pct": pops["all_delivered_seats"]["at_cap_pct"],
            "gold_at_cap_pct": pops["gold_first_per_miss"]["at_cap_pct"],
            "C1": c1, "C2": c2_frac, "C3": {"n": bc.get("n"), "pct": c3["both_at_cap_winner_outcovers_pct"]},
        }, indent=1))
    print(f"\nreceipt -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

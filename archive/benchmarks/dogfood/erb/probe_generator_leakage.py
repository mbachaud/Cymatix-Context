"""P4 -- GENERATOR LEAKAGE.

Does the ERB 947k bed's *generated* question set share verbatim token runs with
its gold chunks at a rate that would make an exact-phrase retrieval lane (P1)
look good on the bed and worth nothing on production traffic?

Motivation
----------
Kim & Croft (SIGIR 2010) measured a fielded retrieval model beating a flat one
by +78% MRR on SYNTHETIC queries whose terms were drawn from randomly chosen
fields of the target document -- and the same models collapsed to within +/-2%
of flat on 456 REAL human queries over a comparable heterogeneous enterprise
collection.  The 470 ERB needles are generated, not human-authored.  If the
generator quoted source text into the questions, P1's fire rate is measuring
the generator, not retrieval.

Statistic
---------
LCN(query, chunk) = length of the LONGEST COMMON CONTIGUOUS TOKEN N-GRAM.
Computed twice per pair:
  * ``raw``      -- over ``re.findall(r"[a-z0-9]+", text.lower())``
  * ``content``  -- same, after deleting stopwords from BOTH sequences
                    (contiguity then measured on the filtered streams, so
                    "the limits for the upload" and "limits on upload" can
                    still align on ``limits upload``).
``content`` is the primary statistic: a raw 4-gram like "for the new multipart"
is stopword filler, not leakage.

Per needle, the gold statistic is max LCN over that needle's gold chunks.

*** THE CONTROLS *** (without these the gold number means nothing)
  A. RANDOM non-gold chunks, uniform over all 947,531 genes, seeded.
     Reported both count-matched to gold_n (apples-to-apples on the max) and
     at a fixed 20 draws.  This is the floor -- topically unrelated text.
  B. SIBLING non-gold chunks: other chunks of the SAME source documents as
     gold, minus the gold chunks themselves.  *** THIS CONTROL IS VACUOUS ON
     THIS BED AND IS RETAINED ONLY AS THE DISCLOSED DEFECT. ***  ERB gold is
     document-complete -- resolve_erb_needles.py maps each upstream dsid to a
     source path and takes EVERY gene at that source_id -- so the non-gold
     sibling set is empty for all 470 needles (measured: n_sibling_pool == 0,
     470/470).  It returns a constant 0 and carries no information.
  B2. DIRECTORY-NEIGHBOUR chunks (the repair for B): chunks of OTHER documents
     sitting in the SAME parent directory as a gold document.  Same source
     type, same folder, different document -- topic-matched and non-vacuous.
     Count-matched to gold_n, plus max over up to 30 draws.
  C. HARD NEGATIVES (156 baseline misses only): the 12 chunks actually
     delivered by the shipped pipeline, all non-gold by construction since
     delivered_gold == 0 on every mined miss.  Max over the 12.  This is the
     control that answers "does phrase overlap DISCRIMINATE gold from the
     competitors the ranker actually surfaced".

Outputs receipts/generator_leakage_2026-09-01.json.

Usage:
    python benchmarks/dogfood/erb/probe_generator_leakage.py
"""

from __future__ import annotations

import hashlib
import json
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
ERB = ROOT / "benchmarks" / "dogfood" / "erb"
RECEIPTS = ERB / "receipts"
BED = "F:/tmp/erb_blob_v09x.db"
BED_URI = "file:F:/tmp/erb_blob_v09x.db?mode=ro"
SEED = 20260901
N_RANDOM = 20
N_SIBLING_CAP = 30
N_DIRNB_CAP = 30
DIR_COLLECT_CAP = 300  # max gene_ids retained per gold parent directory during the scan

# ---------------------------------------------------------------------------
# KILL CRITERION -- stated before the probe was run, verbatim into the receipt.
# ---------------------------------------------------------------------------
KILL_CRITERION = (
    "FLAG P1 AS BED-INFLATED if BOTH: "
    "(a) the MEDIAN longest common contiguous CONTENT n-gram (stopwords "
    "deleted from both sides) between a query and its gold chunk(s) is >= 4 "
    "tokens -- a 4-token content run is a verbatim quotation a user without "
    "the document in front of them would not type; 2-3 content tokens is a "
    "normal entity/product name; AND "
    "(b) that median exceeds the TOPIC-MATCHED control (control B, sibling "
    "chunks of the same source documents, count-matched to gold_n) by >= 2 "
    "tokens, i.e. the run is specific to the gold chunk rather than to the "
    "document's subject matter. "
    "FLAG PARTIAL / TARGET-POPULATION-INFLATED if the headline criterion does "
    "not fire but the 37 basic pool-absent misses (P1's stated target "
    "population) show a gold content-LCN median >= 2 tokens above the "
    "all-470 gold median -- that would mean P1's fire rate is concentrated "
    "on exactly the queries the generator leaked into. "
    "Otherwise PASS: the phrase signal on this bed is not a generator "
    "artifact at the population level."
)

# The stated criterion is preserved verbatim above.  Arm (b)'s named control
# turned out to be structurally vacuous on this bed (see CRITERION_DEFECT and
# control_B_vacuity in the receipt), so arm (b) is ALSO evaluated against the
# repaired directory-neighbour control B2 and both readings are reported.
CRITERION_DEFECT = (
    "Arm (b) of the pre-registered criterion named control B (non-gold sibling "
    "chunks of the gold source documents). That control is EMPTY on this bed: "
    "ERB gold is document-complete (scripts/resolve_erb_needles.py resolves an "
    "upstream dsid to a source path and takes every gene at that source_id), so "
    "there are no non-gold siblings -- n_sibling_pool == 0 on 470/470 needles "
    "and control B returns a constant 0. Arm (b) therefore reads 'MET' against a "
    "vacuous baseline and is not evidence of anything. It is disclosed rather "
    "than quietly rewritten, and arm (b) is re-evaluated against a repaired "
    "topic-matched control B2 (directory-neighbour documents). The verdict is an "
    "AND over arms (a) and (b), and arm (a) is decided by a wide margin, so the "
    "defect does not change the verdict under either reading -- verified in "
    "verdict_robustness_to_criterion_defect."
)

STOPWORDS = frozenset(
    """a about above after again against all am an and any are as at be because been
before being below between both but by can cannot could did do does doing don down
during each few for from further had has have having he her here hers herself him
himself his how i if in into is it its itself just me more most my myself no nor not
now of off on once only or other others our ours ourselves out over own same she
should so some such than that the their theirs them themselves then there these they
this those through to too under until up us very was we were what when where which
while who whom why will with would you your yours yourself yourselves s t don t
did does was were be been being do doing get got give given make made use used
using also may might must shall need let new one two per via vs""".split()
)

TOKEN_RE = re.compile(r"[a-z0-9]+")


def toks(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def strip_stop(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in STOPWORDS]


class LCN:
    """Longest common contiguous n-gram against a fixed query token stream.

    O(sum over chunk positions of #query positions carrying that token) --
    for a ~25-token query against a ~700-token chunk this is a few hundred
    dict operations, not the 17,500 of a dense DP.
    """

    def __init__(self, q: list[str]) -> None:
        pos: dict[str, list[int]] = defaultdict(list)
        for i, t in enumerate(q):
            pos[t].append(i)
        self.pos = dict(pos)
        self.q = q

    def best(self, c: list[str]) -> tuple[int, str]:
        pos = self.pos
        prev: dict[int, int] = {}
        best = 0
        best_end = -1
        for tok in c:
            hits = pos.get(tok)
            if not hits:
                if prev:
                    prev = {}
                continue
            cur = {}
            for i in hits:
                v = prev.get(i - 1, 0) + 1
                cur[i] = v
                if v > best:
                    best = v
                    best_end = i
            prev = cur
        if best == 0:
            return 0, ""
        return best, " ".join(self.q[best_end - best + 1 : best_end + 1])


def quart(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    s = sorted(xs)
    n = len(s)

    def pct(p: float) -> float:
        if n == 1:
            return float(s[0])
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        return float(s[lo] + (s[hi] - s[lo]) * (idx - lo))

    return {
        "n": n,
        "min": s[0],
        "p25": round(pct(0.25), 3),
        "median": round(pct(0.50), 3),
        "p75": round(pct(0.75), 3),
        "p90": round(pct(0.90), 3),
        "max": s[-1],
        "mean": round(sum(s) / n, 3),
    }


def main() -> int:
    t0 = time.time()
    git_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True
    ).stdout.strip()

    needles = json.loads((ERB / "needles_resolved_v09x_full.json").read_text("utf-8"))["needles"]
    gold_map = json.loads((ERB / "gold_by_needle_v09x.json").read_text("utf-8"))
    meta = json.loads((ERB / "needles_v09x_meta.json").read_text("utf-8"))
    ledger = json.loads((RECEIPTS / "bucket_ledger_2026-09-01.json").read_text("utf-8"))
    mine = json.loads((RECEIPTS / "interloper_mine_erb947k_2026-08-31.json").read_text("utf-8"))
    base = json.loads(
        (RECEIPTS / "ladder_v09x_w24_min_delivered_2026-08-28.json").read_text("utf-8")
    )

    bucket = {r["needle"]: r["bucket"] for r in ledger["per_miss"]}
    chunk_split = {r["needle"]: r["chunk_split"] for r in ledger["per_miss"]}
    delivered_ids = {
        m["needle"]: [d["gene_id"] for d in m["delivered"]] for m in mine["misses"]
    }
    hitmiss = {}
    base_arm = base["arms"][0]
    assert base_arm["arm"] == "baseline", base_arm["arm"]
    for r in base_arm["per_query"]:
        hitmiss[r["needle"]] = "hit" if r["delivered_gold"] else "miss"
    assert sum(1 for v in hitmiss.values() if v == "miss") == 156, "basis miss count drift"

    # ---- one pass over the bed -------------------------------------------
    gold_ids = {n["name"]: list(dict.fromkeys(gold_map[n["name"]])) for n in needles}
    all_gold = {g for v in gold_ids.values() for g in v}

    con = sqlite3.connect(BED_URI, uri=True)
    con.execute("PRAGMA query_only = ON")

    # gold source_ids first (PK lookups, cheap) so the scan only retains
    # siblings we will actually use.
    gold_src: dict[str, str] = {}
    gl = sorted(all_gold)
    for i in range(0, len(gl), 500):
        chunk = gl[i : i + 500]
        q = "SELECT gene_id, source_id FROM genes WHERE gene_id IN (%s)" % ",".join(
            "?" * len(chunk)
        )
        for gid, sid in con.execute(q, chunk):
            gold_src[gid] = sid
    want_src = set(gold_src.values())

    def parent_dir(path: str) -> str:
        i = max(path.rfind(chr(92)), path.rfind("/"))
        return path[:i] if i > 0 else path

    want_dir = {parent_dir(x) for x in want_src}

    t_scan = time.time()
    all_ids: list[str] = []
    sib: dict[str, list[str]] = defaultdict(list)
    dirnb: dict[str, list[str]] = defaultdict(list)
    for gid, sid in con.execute("SELECT gene_id, source_id FROM genes"):
        all_ids.append(gid)
        if sid in want_src:
            sib[sid].append(gid)
        d_ = parent_dir(sid)
        if d_ in want_dir:
            b_ = dirnb[d_]
            if len(b_) < DIR_COLLECT_CAP:
                b_.append(gid)
    scan_s = round(time.time() - t_scan, 2)
    n_genes = len(all_ids)

    # ---- decide every chunk we need content for --------------------------
    rng = random.Random(SEED)
    per_needle_random: dict[str, list[str]] = {}
    per_needle_sib: dict[str, list[str]] = {}
    per_needle_dirnb: dict[str, list[str]] = {}
    for n in needles:
        name = n["name"]
        g = gold_ids[name]
        gset = set(g)
        # control A -- uniform random non-gold, seeded, per needle
        picks: list[str] = []
        while len(picks) < N_RANDOM:
            cand = all_ids[rng.randrange(n_genes)]
            if cand not in gset and cand not in picks:
                picks.append(cand)
        per_needle_random[name] = picks
        # control B -- siblings of the gold documents, minus gold
        pool = []
        for gid in g:
            for s in sib.get(gold_src.get(gid, ""), ()):
                if s not in gset:
                    pool.append(s)
        pool = list(dict.fromkeys(pool))
        if len(pool) > N_SIBLING_CAP:
            pool = rng.sample(pool, N_SIBLING_CAP)
        per_needle_sib[name] = pool
        # control B2 -- other documents in the same parent directory
        dpool = []
        for gid in g:
            for s2 in dirnb.get(parent_dir(gold_src.get(gid, "")), ()):
                if s2 not in gset:
                    dpool.append(s2)
        dpool = list(dict.fromkeys(dpool))
        if len(dpool) > N_DIRNB_CAP:
            dpool = rng.sample(dpool, N_DIRNB_CAP)
        per_needle_dirnb[name] = dpool

    need = set(all_gold)
    for v in per_needle_random.values():
        need.update(v)
    for v in per_needle_sib.values():
        need.update(v)
    for v in per_needle_dirnb.values():
        need.update(v)
    for v in delivered_ids.values():
        need.update(v)

    t_fetch = time.time()
    text: dict[str, list[str]] = {}
    nl = sorted(need)
    for i in range(0, len(nl), 400):
        chunk = nl[i : i + 400]
        q = "SELECT gene_id, content FROM genes WHERE gene_id IN (%s)" % ",".join(
            "?" * len(chunk)
        )
        for gid, content in con.execute(q, chunk):
            text[gid] = toks(content or "")
    con.close()
    fetch_s = round(time.time() - t_fetch, 2)
    missing_content = sorted(need - set(text))

    craw = {g: t for g, t in text.items()}
    ccon = {g: strip_stop(t) for g, t in text.items()}

    # ---- per-needle statistic --------------------------------------------
    rows = []
    for n in needles:
        name = n["name"]
        qt = n["question_type"]
        qraw = toks(n["query"])
        qcon = strip_stop(qraw)
        Lr = LCN(qraw)
        Lc = LCN(qcon)

        def stat(ids: list[str], matched: int | None = None):
            """(max_raw, max_content, best_content_string) over ids."""
            use = ids if matched is None else ids[:matched]
            br, bc, bs = 0, 0, ""
            for gid in use:
                if gid not in craw:
                    continue
                v, _ = Lr.best(craw[gid])
                if v > br:
                    br = v
                v, s = Lc.best(ccon[gid])
                if v > bc:
                    bc, bs = v, s
            return br, bc, bs

        g = gold_ids[name]
        gr, gc, gs = stat(g)
        gold_n = len(g)

        ra = per_needle_random[name]
        a_m_r, a_m_c, _ = stat(ra, matched=gold_n)
        a20_r, a20_c, _ = stat(ra)

        sb = per_needle_sib[name]
        b_m_r, b_m_c, b_s = stat(sb, matched=gold_n)
        b_all_r, b_all_c, _ = stat(sb)

        db = per_needle_dirnb[name]
        d_m_r, d_m_c, d_s = stat(db, matched=gold_n)
        d_all_r, d_all_c, _ = stat(db)

        row = {
            "needle": name,
            "question_type": qt,
            "gold_n": gold_n,
            "n_sibling_pool": len(sb),
            "q_tokens_raw": len(qraw),
            "q_tokens_content": len(qcon),
            "gold_lcn_raw": gr,
            "gold_lcn_content": gc,
            "gold_lcn_content_str": gs,
            "ctlA_random_matched_raw": a_m_r,
            "ctlA_random_matched_content": a_m_c,
            "ctlA_random_20_raw": a20_r,
            "ctlA_random_20_content": a20_c,
            "ctlB_sibling_matched_raw": b_m_r,
            "ctlB_sibling_matched_content": b_m_c,
            "ctlB_sibling_all_raw": b_all_r,
            "ctlB_sibling_all_content": b_all_c,
            "ctlB_sibling_matched_content_str": b_s,
            "n_dirnb_pool": len(db),
            "ctlB2_dirnb_matched_raw": d_m_r,
            "ctlB2_dirnb_matched_content": d_m_c,
            "ctlB2_dirnb_all_raw": d_all_r,
            "ctlB2_dirnb_all_content": d_all_c,
            "ctlB2_dirnb_matched_content_str": d_s,
            "outcome": hitmiss[name],
            "bucket": bucket.get(name),
            "chunk_split": chunk_split.get(name),
        }
        if name in delivered_ids:
            dv = delivered_ids[name]
            c_r, c_c, c_s = stat(dv)
            row["ctlC_delivered_raw"] = c_r
            row["ctlC_delivered_content"] = c_c
            row["ctlC_delivered_content_str"] = c_s
            # fairness: max over 12 delivered vs max over ~4 gold biases C up.
            # matched = the top gold_n delivered (the STRONGEST interlopers, so
            # this is if anything still generous to the control).
            m_r, m_c, _ = stat(dv, matched=gold_n)
            row["ctlC_delivered_matched_raw"] = m_r
            row["ctlC_delivered_matched_content"] = m_c
            t_r, t_c, _ = stat(dv, matched=1)
            row["ctlC_delivered_top1_raw"] = t_r
            row["ctlC_delivered_top1_content"] = t_c
        rows.append(row)

    # ---- rollups ----------------------------------------------------------
    def roll(sel, label):
        sub = [r for r in rows if sel(r)]
        if not sub:
            return None
        out = {
            "label": label,
            "n": len(sub),
            "gold_raw": quart([r["gold_lcn_raw"] for r in sub]),
            "gold_content": quart([r["gold_lcn_content"] for r in sub]),
            "ctlA_random_matched_content": quart(
                [r["ctlA_random_matched_content"] for r in sub]
            ),
            "ctlA_random_20_content": quart([r["ctlA_random_20_content"] for r in sub]),
            "ctlB_sibling_matched_content": quart(
                [r["ctlB_sibling_matched_content"] for r in sub]
            ),
            "ctlB_sibling_all_content": quart([r["ctlB_sibling_all_content"] for r in sub]),
            "ctlA_random_matched_raw": quart([r["ctlA_random_matched_raw"] for r in sub]),
            "ctlB_sibling_matched_raw": quart([r["ctlB_sibling_matched_raw"] for r in sub]),
            "ctlB2_dirnb_matched_content": quart(
                [r["ctlB2_dirnb_matched_content"] for r in sub]
            ),
            "ctlB2_dirnb_all_content": quart([r["ctlB2_dirnb_all_content"] for r in sub]),
            "ctlB2_dirnb_matched_raw": quart([r["ctlB2_dirnb_matched_raw"] for r in sub]),
            "n_dirnb_pool": quart([r["n_dirnb_pool"] for r in sub]),
            "gold_beats_ctlB2_matched_content": sum(
                1 for r in sub if r["gold_lcn_content"] > r["ctlB2_dirnb_matched_content"]
            ),
            "gold_ties_ctlB2_matched_content": sum(
                1 for r in sub if r["gold_lcn_content"] == r["ctlB2_dirnb_matched_content"]
            ),
            "gold_loses_ctlB2_matched_content": sum(
                1 for r in sub if r["gold_lcn_content"] < r["ctlB2_dirnb_matched_content"]
            ),
        }
        out["gap_gold_minus_ctlA_matched_content_median"] = round(
            out["gold_content"]["median"] - out["ctlA_random_matched_content"]["median"], 3
        )
        out["gap_gold_minus_ctlB_matched_content_median"] = round(
            out["gold_content"]["median"] - out["ctlB_sibling_matched_content"]["median"], 3
        )
        out["gap_gold_minus_ctlB2_matched_content_median"] = round(
            out["gold_content"]["median"] - out["ctlB2_dirnb_matched_content"]["median"], 3
        )
        out["pct_gold_content_ge_4"] = round(
            100.0 * sum(1 for r in sub if r["gold_lcn_content"] >= 4) / len(sub), 1
        )
        out["pct_gold_content_ge_6"] = round(
            100.0 * sum(1 for r in sub if r["gold_lcn_content"] >= 6) / len(sub), 1
        )
        out["pct_gold_raw_ge_8"] = round(
            100.0 * sum(1 for r in sub if r["gold_lcn_raw"] >= 8) / len(sub), 1
        )
        cs = [r for r in sub if "ctlC_delivered_content" in r]
        if cs:
            out["n_ctlC"] = len(cs)
            for key, lbl in (
                ("ctlC_delivered_content", "all12"),
                ("ctlC_delivered_matched_content", "matched"),
                ("ctlC_delivered_top1_content", "top1"),
            ):
                out[key] = quart([r[key] for r in cs])
                out[f"gold_vs_ctlC_{lbl}_content"] = {
                    "gold_beats": sum(1 for r in cs if r["gold_lcn_content"] > r[key]),
                    "ties": sum(1 for r in cs if r["gold_lcn_content"] == r[key]),
                    "gold_loses": sum(1 for r in cs if r["gold_lcn_content"] < r[key]),
                    "gold_beats_pct": round(
                        100.0
                        * sum(1 for r in cs if r["gold_lcn_content"] > r[key])
                        / len(cs),
                        1,
                    ),
                }
            out["ctlC_delivered_raw"] = quart([r["ctlC_delivered_raw"] for r in cs])
            # back-compat aliases for the all-12 reading
            out["gold_beats_ctlC_content"] = out["gold_vs_ctlC_all12_content"]["gold_beats"]
            out["gold_ties_ctlC_content"] = out["gold_vs_ctlC_all12_content"]["ties"]
            out["gold_loses_ctlC_content"] = out["gold_vs_ctlC_all12_content"]["gold_loses"]
        return out

    overall = roll(lambda r: True, "all_470")
    by_type = {}
    for qt in sorted({r["question_type"] for r in rows}):
        by_type[qt] = roll(lambda r, qt=qt: r["question_type"] == qt, f"type={qt}")
    by_outcome = {
        k: roll(lambda r, k=k: r["outcome"] == k, f"outcome={k}") for k in ("hit", "miss")
    }
    by_bucket = {}
    for bk in ("pool_absent", "near_band", "seat_capped"):
        by_bucket[bk] = roll(lambda r, bk=bk: r["bucket"] == bk, f"bucket={bk}")
    target = roll(
        lambda r: r["question_type"] == "basic" and r["bucket"] == "pool_absent",
        "basic_pool_absent (P1 target population)",
    )
    sem_pool_absent = roll(
        lambda r: r["question_type"] == "semantic" and r["bucket"] == "pool_absent",
        "semantic_pool_absent",
    )
    hit_by_type = {
        qt: roll(
            lambda r, qt=qt: r["question_type"] == qt and r["outcome"] == "hit",
            f"type={qt}&hit",
        )
        for qt in ("basic", "semantic")
    }
    miss_by_type = {
        qt: roll(
            lambda r, qt=qt: r["question_type"] == qt and r["outcome"] == "miss",
            f"type={qt}&miss",
        )
        for qt in ("basic", "semantic")
    }
    by_chunk_split = {
        "chunk_split": roll(lambda r: r["chunk_split"] is True, "chunk_split misses"),
        "non_chunk_split_miss": roll(
            lambda r: r["chunk_split"] is False, "non-chunk_split misses"
        ),
    }

    # ---- verdict ----------------------------------------------------------
    med_gold = overall["gold_content"]["median"]
    med_ctlB = overall["ctlB_sibling_matched_content"]["median"]
    med_ctlA = overall["ctlA_random_matched_content"]["median"]
    cond_a = med_gold >= 4
    med_ctlB2 = overall["ctlB2_dirnb_matched_content"]["median"]
    cond_b = (med_gold - med_ctlB) >= 2                 # as pre-registered (vacuous control)
    cond_b_repaired = (med_gold - med_ctlB2) >= 2       # repaired topic-matched control
    headline_fires = cond_a and cond_b
    headline_fires_repaired = cond_a and cond_b_repaired
    tgt_med = target["gold_content"]["median"] if target else None
    partial_fires = (not headline_fires) and tgt_med is not None and (tgt_med - med_gold) >= 2

    if headline_fires:
        verdict = "KILL"
        vr = (
            f"BED-INFLATED. Gold content-LCN median {med_gold} >= 4 AND exceeds the "
            f"topic-matched sibling control ({med_ctlB}) by "
            f"{round(med_gold - med_ctlB, 3)} >= 2. Both arms of the pre-registered "
            "criterion fire; P1's phrase signal is measuring the generator."
        )
    elif partial_fires:
        verdict = "KILL"
        vr = (
            "TARGET-POPULATION-INFLATED. Headline criterion did not fire "
            f"(gold median {med_gold}), but the 37 basic pool-absent misses -- P1's "
            f"stated target population -- carry gold content-LCN median {tgt_med}, "
            f"{round(tgt_med - med_gold, 3)} above the all-470 median. P1's fire rate "
            "concentrates on exactly the queries the generator leaked into."
        )
    else:
        verdict = "PASS"
        vr = (
            f"NOT BED-INFLATED at the population level. Arm (a): gold content-LCN "
            f"median {med_gold}, threshold >= 4 -> "
            f"{'MET' if cond_a else 'NOT MET'} (decisive). Arm (b) as pre-registered "
            f"(control B, siblings): gap {round(med_gold - med_ctlB, 3)} -> "
            f"{'MET' if cond_b else 'NOT MET'}, but control B is VACUOUS on this bed "
            f"(constant 0, 470/470 -- see criterion_defect_disclosed). Arm (b) "
            f"repaired (control B2, directory-neighbour documents): gap "
            f"{round(med_gold - med_ctlB2, 3)} -> "
            f"{'MET' if cond_b_repaired else 'NOT MET'}. The criterion is an AND and "
            f"arm (a) fails under both readings, so the verdict is unchanged. Partial "
            f"arm: target-population (basic x pool_absent) median {tgt_med} vs all-470 "
            f"{med_gold}, needs >= +2 -> {'MET' if partial_fires else 'NOT MET'}."
        )

    # exemplars -- the highest-leakage needles, so a human can eyeball them
    verdict_robustness = {
        "arm_a_gold_content_median": med_gold,
        "arm_a_threshold": 4,
        "arm_a_met": cond_a,
        "arm_b_as_preregistered_control": "B (non-gold siblings of gold docs) -- VACUOUS, constant 0",
        "arm_b_as_preregistered_gap": round(med_gold - med_ctlB, 3),
        "arm_b_as_preregistered_met": cond_b,
        "arm_b_repaired_control": "B2 (directory-neighbour documents) -- topic-matched, non-vacuous",
        "arm_b_repaired_gap": round(med_gold - med_ctlB2, 3),
        "arm_b_repaired_met": cond_b_repaired,
        "headline_fires_as_preregistered": headline_fires,
        "headline_fires_with_repaired_arm_b": headline_fires_repaired,
        "verdict_identical_under_both_readings": headline_fires == headline_fires_repaired,
        "why": (
            "The headline criterion is an AND. Arm (a) fails by a wide margin "
            f"(gold content-LCN median {med_gold} against a threshold of 4), so "
            "neither the vacuous nor the repaired reading of arm (b) can flip the "
            "verdict. The defect is disclosed, not load-bearing."
        ),
    }

    top = sorted(rows, key=lambda r: (-r["gold_lcn_content"], -r["gold_lcn_raw"]))[:15]
    exemplars = [
        {
            "needle": r["needle"],
            "question_type": r["question_type"],
            "outcome": r["outcome"],
            "gold_lcn_content": r["gold_lcn_content"],
            "gold_lcn_raw": r["gold_lcn_raw"],
            "shared_content_ngram": r["gold_lcn_content_str"],
            "ctlB_sibling_matched_content": r["ctlB_sibling_matched_content"],
            "query": next(n["query"] for n in needles if n["name"] == r["needle"]),
        }
        for r in top
    ]

    receipt = {
        "stamp": "2026-09-01",
        "probe": "P4 generator leakage -- are ERB query/gold verbatim n-grams a generator artifact?",
        "tool": "benchmarks/dogfood/erb/probe_generator_leakage.py",
        "bed": BED,
        "bed_bytes": 24571039744,
        "bed_access": "read-only (mode=ro, PRAGMA query_only); one full scan of (gene_id, source_id), then gene_id PK content lookups",
        "git_sha": git_sha,
        "basis_receipts": [
            "benchmarks/dogfood/erb/receipts/bucket_ledger_2026-09-01.json",
            "benchmarks/dogfood/erb/receipts/interloper_mine_erb947k_2026-08-31.json",
            "benchmarks/dogfood/erb/receipts/ladder_v09x_w24_min_delivered_2026-08-28.json",
            "benchmarks/dogfood/erb/needles_resolved_v09x_full.json",
            "benchmarks/dogfood/erb/gold_by_needle_v09x.json",
            "benchmarks/dogfood/erb/needles_v09x_meta.json",
        ],
        "kill_criterion": KILL_CRITERION,
        "criterion_defect_disclosed": CRITERION_DEFECT,
        "verdict_robustness_to_criterion_defect": verdict_robustness,
        "control_B_vacuity": {
            "n_needles_with_zero_non_gold_siblings": sum(
                1 for r in rows if r["n_sibling_pool"] == 0
            ),
            "n_needles": len(rows),
            "cause": (
                "ERB gold is document-complete. scripts/resolve_erb_needles.py maps "
                "an upstream dsid to a source path and takes EVERY gene at that "
                "source_id, so 'other chunks of the gold document' is the empty set. "
                "Spot-checked on the bed: erb_000's two gold docs have 8/8 and 2/2 "
                "chunks gold."
            ),
            "consequence": "control B is a constant 0 and is reported only as the disclosed defect; control B2 replaces it.",
        },
        "verdict": verdict,
        "verdict_reason": vr,
        "method": {
            "statistic": "longest common contiguous token n-gram (LCN) between query and chunk",
            "tokenizer": r"re.findall(r'[a-z0-9]+', text.lower())",
            "raw_vs_content": "raw = all tokens; content = stopwords deleted from BOTH sides before contiguity is measured",
            "n_stopwords": len(STOPWORDS),
            "stopwords_sha256": hashlib.sha256(
                " ".join(sorted(STOPWORDS)).encode()
            ).hexdigest()[:16],
            "gold_statistic": "max LCN over the needle's gold chunks",
            "control_A": f"uniform random non-gold genes over all {n_genes}, seed {SEED}, {N_RANDOM} draws/needle; reported count-matched to gold_n and at 20",
            "control_B": f"sibling chunks of the gold source documents minus gold, cap {N_SIBLING_CAP}, seed {SEED}; VACUOUS on this bed (gold is document-complete) -- see control_B_vacuity",
            "control_B2": f"chunks of OTHER documents in the same parent directory as a gold document, cap {N_DIRNB_CAP}/needle drawn from up to {DIR_COLLECT_CAP} gene_ids retained per gold directory in rowid order, seed {SEED}; topic-matched replacement for B",
            "control_C": "the 12 chunks the shipped pipeline actually delivered, for the 156 baseline misses only (all non-gold since delivered_gold==0). Reported three ways: max over all 12; count-matched to gold_n using the TOP gold_n delivered (strongest interlopers); and the rank-1 delivered chunk alone. Only misses have delivered ids in the interloper mine, so control C does not exist for the 314 hits -- disclosed, not imputed.",
            "chunk_field": "genes.content only (complement not included)",
            "seed": SEED,
        },
        "n_needles": len(rows),
        "n_genes_in_bed": n_genes,
        "n_gold_chunks_distinct": len(all_gold),
        "n_chunks_tokenized": len(text),
        "n_chunks_requested_missing_content": len(missing_content),
        "scan_seconds": scan_s,
        "fetch_seconds": fetch_s,
        "total_seconds": round(time.time() - t0, 1),
        "provenance": PROVENANCE,
        "section_1_overall": overall,
        "section_2_by_question_type": by_type,
        "section_2b_basic_vs_semantic_split_by_outcome": {
            "hit": hit_by_type,
            "miss": miss_by_type,
        },
        "section_3_by_baseline_outcome": by_outcome,
        "section_4_by_bucket": by_bucket,
        "section_4b_target_population": target,
        "section_4c_semantic_pool_absent": sem_pool_absent,
        "section_4d_by_chunk_split": by_chunk_split,
        "section_5_exemplars_highest_leakage": exemplars,
        "per_needle": rows,
    }

    out = RECEIPTS / "generator_leakage_2026-09-01.json"
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    # ---- console ----------------------------------------------------------
    def line(d, tag):
        if not d:
            return
        print(
            f"{tag:<44} n={d['n']:>3}  gold_content med={d['gold_content']['median']:>4} "
            f"p75={d['gold_content']['p75']:>4} max={d['gold_content']['max']:>3} | "
            f"ctlA={d['ctlA_random_matched_content']['median']:>4} "
            f"ctlB2={d['ctlB2_dirnb_matched_content']['median']:>4} | "
            f"gold_raw med={d['gold_raw']['median']:>4} | >=4c {d['pct_gold_content_ge_4']:>5}%"
        )

    print(f"scan {scan_s}s  fetch {fetch_s}s  total {round(time.time()-t0,1)}s")
    print(f"genes={n_genes}  gold_chunks={len(all_gold)}  tokenized={len(text)}")
    line(overall, "ALL 470")
    for qt, d in by_type.items():
        line(d, f"  type {qt}")
    for k, d in by_outcome.items():
        line(d, f"  outcome {k}")
    for k, d in by_bucket.items():
        line(d, f"  bucket {k}")
    line(target, "  *** basic x pool_absent (P1 target)")
    line(sem_pool_absent, "  semantic x pool_absent")
    print()
    print("CRITERION DEFECT: control B vacuous on "
          f"{sum(1 for r in rows if r['n_sibling_pool']==0)}/{len(rows)} needles; "
          f"arm(b) as-preregistered gap={round(med_gold-med_ctlB,3)} (met={cond_b}), "
          f"repaired via B2 gap={round(med_gold-med_ctlB2,3)} (met={cond_b_repaired}); "
          f"verdict identical under both = {headline_fires == headline_fires_repaired}")
    print()
    print("VERDICT:", verdict)
    print(vr)
    print()
    print("Top-5 highest-leakage needles:")
    for e in exemplars[:5]:
        print(f"  {e['needle']} [{e['question_type']}/{e['outcome']}] "
              f"content={e['gold_lcn_content']} raw={e['gold_lcn_raw']} "
              f"ngram={e['shared_content_ngram']!r}")
    print(f"\nreceipt -> {out}")
    return 0


PROVENANCE = {
    "generator_in_this_repo": False,
    "generator_location": "F:/Projects/EnterpriseRAG-Bench-main (upstream EnterpriseRAG-Bench checkout on this machine; NOT vendored into cymatix-context)",
    "how_needles_enter_this_repo": (
        "scripts/resolve_erb_needles.py reads F:/Projects/EnterpriseRAG-Bench-main/"
        "questions.jsonl (500 questions with expected_doc_ids) + generated_data/"
        "uuid_index.json (dsid -> relative source path, 511,958 entries) and maps "
        "them onto genes.source_id in the bed. cymatix authors NO questions; it "
        "only resolves upstream ground truth to gene_ids. seed 20260827 in "
        "needles_v09x_meta.json is the stratified-half SUBSET seed, not a "
        "question-generation seed."
    ),
    "question_generation_prompts": [
        "src/prompts/basic_questions.py (BASIC + SEMANTIC prompts both live here)",
        "src/prompts/intra_document_reasoning.py",
        "src/prompts/project_question.py",
        "src/prompts/constrained_queries.py",
        "src/prompts/completeness_questions.py",
        "src/prompts/conflicting_query.py",
        "src/prompts/misc_files.py",
        "src/prompts/info_not_found_question.py",
        "src/prompts/multi_hop.py",
        "src/prompts/questions_metadata.py",
    ],
    "anti_leakage_guardrails_found_in_generator": {
        "basic": [
            "basic_questions.py:4 -- 'Limit or avoid the use of very specific terms that would make the retrieval too trivial. Where possible avoid providing exact keyword matches from the document and use paraphrased or similar terms instead.'",
            "basic_questions.py:6 -- 'For these qualifiers and details, also avoid using obvious phrase matches.'",
        ],
        "semantic": [
            "basic_questions.py:40 -- 'generate a difficult RAG query that does not have strong lexical matches to the document. Whenever possible, avoid using exact keyword matches from the document.'",
            "basic_questions.py:42 -- 'It should simulate a situation where a user does not have the document in front of them and therefore cannot provide a strong lexical/keyword match to the document.'",
            "basic_questions.py:47 -- 'CRITICAL: ... Strongly limit your use of exact terms and phrase matches.'",
        ],
        "validation": "methodology.md Stage 3 Type 1/2 -- 'A separate flow validates that the question is conformant to the requirements' before the gold answer is generated.",
    },
    "contrast_with_kim_croft_2010": (
        "Kim & Croft's synthetic queries were built by DRAWING TERMS FROM RANDOMLY "
        "CHOSEN FIELDS of the target document -- a generator with no anti-lexical "
        "objective, which is precisely the mechanism that inflates fielded/lexical "
        "models. ERB's generator carries an explicit, prompt-level anti-lexical-match "
        "objective on BOTH the basic and semantic flows plus a conformance validator. "
        "The two generators are adversarial in opposite directions, so the Kim & Croft "
        "prior does not transfer by construction -- which is exactly why it has to be "
        "MEASURED rather than argued. This probe measures it."
    ),
    "residual_provenance_gap": (
        "The generating LLM, temperature, and per-question generation seed are NOT "
        "recorded in questions.jsonl or needles_v09x_meta.json, and the upstream "
        "generation run is not reproducible from this checkout. Question generation "
        "is therefore attributable to the prompts above but not bit-reproducible."
    ),
}


if __name__ == "__main__":
    raise SystemExit(main())

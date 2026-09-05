"""probe_harmonic_off_dilution.py -- FOLLOW-UP 2 to ARM 1: head-dilution
attribution with the harmonic tier removed.

THE FINDING UNDER TEST (verify_a1_recompute_2026-09-01.json, section 6b,
"FINDING_depth_monotonically_degrades_gold_position")

    Over the 107 needles where gold is found in BOTH ARM-1 arms, moving the
    two admission bounds from 48/50 to 1000/1000 gives

        deep better than shipped   0
        same                      38
        deep worse than shipped   69

    Widening admission NEVER improves gold's fused rank and degrades it on
    two thirds of the needles where gold was already admitted.

THE SUSPECT
    knowledge_store.py :3624-3663 -- Tier 5, the harmonic co-activation
    boost. It runs BEFORE the BM25 shortlist post-filter (:3839; confirmed
    by verify_a1_completeness "harmonic_runs_before_shortlist": true), so it
    scores the FULL PRE-SHORTLIST pool. Its bonus is quantized:

        harmonic_bonus[gid] = min(bonus + harmonic_weight,
                                  3.0 * harmonic_weight)

    i.e. with the default weight 1.0 every boosted document lands on one of
    {1.0, 2.0, 3.0}. That ranked list goes into the RRF fuser
    (fuser.add_tier("harmonic", ...)), and the Fuser breaks bitwise-equal
    scores by gene_id ASCENDING (fusion.add_tier docstring: "Tied scores DO
    get different ranks"). So gold's rank INSIDE the harmonic tier is a
    gene_id lottery over the size of its tie group -- a group that grows
    with the pool. At shipped depth the pool is ~50; at depth 1000 it is
    1000. The hypothesis is that this, not the ranker proper, is what
    dilutes gold's fused position as depth grows.

THE SWITCH -- what the handoff asked for is NOT a live knob (verified)
    The handoff pre-registered "[cymatics] harmonic_links = false". That is
    NOT a retrieval knob on this path:

      * config.py:499 defines cymatics.harmonic_links; its ONLY consumer is
        context_manager.py:2469, inside the ``if not read_only:`` tail-write
        block -- it gates whether compute_harmonic_weights()/
        store_harmonic_weights() WRITE new edges after a turn. Every ERB
        query runs read_only=True, so on this path the flag is already
        inert; flipping it to false would have been a NO-OP arm.
      * Tier 5's retrieval-side gate is the ``use_harmonic`` kwarg
        (knowledge_store.py:2715 / :3629). build_context -> _retrieve never
        passes it, so it is hardwired True on the CLI/manager path. The
        HTTP /context route DOES expose it (routes_context.py:804,
        ``use_harmonic = profile == "quality"``), so a non-quality profile
        is the product-surface off switch.
      * The CONFIG-FILE off switch is ``[retrieval] harmonic_weight``
        (config.py:861, toml line 517, plumbed through
        sharding.build_genome_kwargs:681 -> KnowledgeStore._harmonic_weight).
        At 0.0 the additive bonus becomes min(0 + 0.0, 0.0) = 0.0 AND
        fuser.add_tier short-circuits on ``weight == 0.0`` ("the operator's
        'disable this tier' knob" -- fusion.py:146-150), so the tier
        contributes nothing to either fusion mode.

    THIS ARM USES ``retrieval.harmonic_weight = 0.0``. It is a live config
    knob reachable from the TOML, needs no monkeypatch, and removes exactly
    the mechanism under suspicion. Residue, stated for the record: the
    harmonic_links SQL still executes and tier_contrib still records
    "harmonic": 0.0 for boosted documents. Neither feeds the ranking
    (grep: the only other readers of the "harmonic" tier name are
    cwola logging, otel telemetry, and fusion_plr's feature vector, none of
    which reorder the slate).

WHAT WAS FOUND BEFORE THE ARM RAN (read-only SQL + a 2-needle smoke; both
recorded in the pre-registration so nothing here is back-fitted)
    Tier 5 is INERT on this bed, for TWO INDEPENDENT reasons:

      (1) ``SELECT COUNT(*) FROM harmonic_links`` on the ERB 947k bed is
          **0**. The table exists (so ``_has_harmonic`` is truthy and the
          branch is entered) but holds no edges, so harmonic_bonus is empty,
          _harmonic_ranked is empty, and fuser.add_tier() is a documented
          no-op on an empty tier.
      (2) Even on a bed WITH edges the tier could not fire at these pool
          sizes. ``candidate_ids = list(gene_scores.keys())`` is taken
          BEFORE the BM25 shortlist, and the Tier-1/Tier-2 tag lanes carry
          no LIMIT (ARM-1's own admission trace), so |gene_scores| here is
          ~2e5 on this bed -- measured per needle by this probe as
          ``pre_shortlist_pool_size``. The query binds the id list TWICE
          (``gene_id_a IN (...) AND gene_id_b IN (...)``), i.e. ~4e5 bound
          parameters against SQLITE_LIMIT_VARIABLE_NUMBER = 32766. That
          raises OperationalError("too many SQL variables"), which
          :3662-3663 swallows into log.debug. The tier is silently dead on
          every query whose pre-shortlist pool exceeds ~16k documents --
          which, because the tag lanes are unbounded, is independent of
          fts5_candidate_depth and bm25_shortlist_size, i.e. it is equally
          true of the SHIPPED arm.

    Consequence for the hypothesis: the harmonic tie-lottery cannot be the
    depth-dilution mechanism, because the harmonic tier never fired in
    EITHER ARM-1 arm. The arm below is run anyway -- the decision rule was
    pre-registered and is honoured as written -- and it doubles as a
    RUN-TO-RUN REPRODUCIBILITY measurement of the deep arm, which
    verify_a1_recompute flagged as untested (it found the delivered SET
    reproducible but the slate ORDER not).

PRE-REGISTERED DECISION RULE -- see
receipts/harmonic_off_dilution_prereg_2026-09-01.json, written with this
file's sha256 BEFORE the arm was run:

    KILL the harmonic-tie hypothesis if, with the harmonic tier off and the
    deep knobs unchanged, gold's new-arm-vs-shipped fused rank shift over
    the 107 both-found needles still has worse >= 50 (at least half the
    dilution survives).
    PASS (the dilution is a harmonic/tie artefact) if worse <= 20 and
    better + same >= 87.
    Anything between the two is INCONCLUSIVE.

    Needles where the new arm does not find gold at all are counted as
    WORSE (and reported separately) -- pre-registered so an admission
    surprise cannot be scored as a win.

Usage:
    python benchmarks/dogfood/erb/probe_harmonic_off_dilution.py \
        --stamp 2026-09-01 --cache benchmarks/dogfood/erb/receipts/harmonic_off_ck_2026-09-01
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
from typing import Any, Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("CYMATIX_DISABLE_LEARN", "1")

BED = "F:/tmp/erb_blob_v09x.db"
CONFIG = HERE / "configs" / "w24_min_delivered_12.toml"
NEEDLES = HERE / "needles_resolved_v09x_full.json"
GOLD = HERE / "gold_by_needle_v09x.json"
RECEIPTS = HERE / "receipts"
FORENSICS = RECEIPTS / "pool_depth_forensics_2026-09-01.json"
CAPTURE = RECEIPTS / "pool_depth_capture_2026-09-01.sqlite"
VERIFY_RECOMPUTE = RECEIPTS / "verify_a1_recompute_2026-09-01.json"
VERIFY_COMPLETENESS = RECEIPTS / "verify_a1_completeness_2026-09-01.json"

K = 12
DEEP = 1000
ARM = "deep1000_harmonic_off"

KNOBS = {
    "retrieval.fts5_candidate_depth": DEEP,
    "retrieval.bm25_shortlist_size": DEEP,
    "retrieval.harmonic_weight": 0.0,
}

PREREG = (
    "KILL the harmonic-tie hypothesis if, with the harmonic tier removed "
    "(retrieval.harmonic_weight = 0.0 -- see 'THE SWITCH' in the tool "
    "docstring: [cymatics] harmonic_links is NOT a live retrieval knob) and "
    "the deep knobs unchanged (fts5_candidate_depth=1000, "
    "bm25_shortlist_size=1000), gold's new-arm-vs-shipped fused rank shift "
    "over the 107 both-found needles still has worse >= 50 (i.e. at least "
    "half the dilution survives). PASS (dilution is a harmonic/tie "
    "artefact) if worse <= 20 and better + same >= 87. Anything else is "
    "INCONCLUSIVE. Needles where the new arm fails to find gold at all "
    "count as WORSE."
)


READING = (
    "The pre-registered rule returns KILL, and the mechanism evidence is "
    "stronger than the rule needed: with the harmonic tier removed the arm "
    "does not merely keep most of the dilution, it reproduces deep1000 "
    "EXACTLY -- 0 better / 107 same / 0 worse on gold's fused rank, and the "
    "identical 0/38/69 against shipped. The reason is that Tier 5 never "
    "fired in either ARM-1 arm: harmonic_links holds 0 rows on this bed, "
    "and independently of that the tier's own SQL would have raised "
    "'too many SQL variables' because candidate_ids is taken PRE-shortlist "
    "off unbounded tag lanes (measured here: median 171,745 documents, max "
    "550,202 -> ~343k bound parameters against SQLite's 32,766 limit) into "
    "a bare `except Exception: log.debug`. So the harmonic tie-lottery is "
    "not a partial explanation of the depth dilution; it is a non-cause. "
    "What this licenses: (1) the ARM-1 dilution finding stands unexplained "
    "and the search moves to the tiers that DID fire -- tag_exact, "
    "tag_prefix, lex_anchor, authority and fts5 are the only names in "
    "tier_contrib, and the RRF ranks of the three unbounded tag/anchor "
    "lanes are the natural next suspects, since those lanes rank ~2e5 "
    "documents while the shortlist only decides which 50 or 1000 of them "
    "survive; (2) it retires the 'quantized bonus + gene_id tie-break' "
    "story for Tier 5 on ANY bed whose pre-shortlist pool exceeds ~16k "
    "documents, because the tier is silently dead there -- which is a "
    "reportable defect in its own right (a swallowed OperationalError that "
    "makes a shipped retrieval tier a no-op at scale with no signal "
    "anywhere but log.debug), and it means every published ERB receipt was "
    "measured with Tier 5 effectively absent; (3) as a byproduct it closes "
    "verify_a1_recompute's open reproducibility question in the direction "
    "it did not test: rank_of_first_gold on the deep arm is bit-stable "
    "run-to-run across all 107 needles (that lens had found the delivered "
    "SET reproducible but the slate ORDER not), so the 0/38/69 split is a "
    "property of the ranker and not of run-to-run noise. What it does NOT "
    "license: any claim about what depth dilution WOULD look like on a bed "
    "with a populated harmonic_links table and a pool small enough for the "
    "tier to execute -- that arm has not been run."
)


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


def rank_gold(scores: Dict[str, float], gold) -> Tuple[Optional[int], List[int]]:
    """Identical contract to probe_pool_depth.rank_gold / ablation_ladder:
    1-indexed, ties broken by gene_id ascending."""
    gold_set = set(gold)
    ordered = [g for g, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]
    ranks = [i for i, g in enumerate(ordered, 1) if g in gold_set]
    return (ranks[0] if ranks else None, ranks)


def tier_summary(contribs: Dict[str, Dict[str, float]], gold_set) -> Dict[str, Any]:
    """Cheap, bounded summary of the store's last_tier_contributions: how
    many pool members each tier fired for, plus gold's own per-tier row.
    (The full map is ~1000 genes x N tiers per needle -- too big to keep for
    107 needles, so only the aggregate + the gold rows are recorded.)"""
    if not contribs:
        return {"available": False}
    counts: Dict[str, int] = {}
    for _gid, c in contribs.items():
        if not isinstance(c, dict):
            continue
        for tier in c:
            counts[tier] = counts.get(tier, 0) + 1
    gold_rows = {g: dict(contribs[g]) for g in gold_set
                 if g in contribs and isinstance(contribs[g], dict)}
    return {"available": True, "n_genes": len(contribs),
            "tier_fire_counts": counts, "gold_tier_contrib": gold_rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default="2026-09-01")
    ap.add_argument("--out", default=None)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--limit", type=int, default=0, help="smoke: first N")
    args = ap.parse_args(argv)

    t_start = time.time()

    # ── Tier-5 liveness, measured read-only BEFORE the arm ─────────────
    _ro = sqlite3.connect("file:" + BED + "?mode=ro", uri=True)
    try:
        _hl_exists = _ro.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='harmonic_links'").fetchone()[0]
        _hl_rows = (_ro.execute("SELECT COUNT(*) FROM harmonic_links"
                                ).fetchone()[0] if _hl_exists else None)
        try:
            _var_limit = _ro.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        except Exception:
            _var_limit = None
    finally:
        _ro.close()
    print("harmonic_links: table_exists=" + str(bool(_hl_exists))
          + " rows=" + str(_hl_rows)
          + " | SQLITE_LIMIT_VARIABLE_NUMBER=" + str(_var_limit), flush=True)

    forensics = json.loads(FORENSICS.read_text(encoding="utf-8"))
    rows = forensics["rows"]
    row_by = {r["needle"]: r for r in rows}
    needles = {n["name"]: n for n in json.loads(
        NEEDLES.read_text(encoding="utf-8"))["needles"]}
    gold = json.loads(GOLD.read_text(encoding="utf-8"))

    # ── the 107 both-found needles (derived, then asserted) ─────────────
    targets = [r["needle"] for r in rows
               if r["cohort"] in ("miss", "hit_control")
               and r["shipped_rank"] is not None
               and r["deep_rank"] is not None]
    assert len(targets) == 107, "both-found population drifted: " + str(len(targets))

    # ── reproduce the published deep1000-vs-shipped split from the capture
    cap = sqlite3.connect("file:" + str(CAPTURE) + "?mode=ro", uri=True)
    cap_rank: Dict[str, Dict[str, Optional[int]]] = {}
    for arm in ("shipped", "deep1000"):
        for name in targets:
            gset = set(gold.get(name, []))
            r = None
            for (rk, gid) in cap.execute(
                    "SELECT rank, gene_id FROM pool WHERE arm=? AND needle=? "
                    "ORDER BY rank", (arm, name)):
                if gid in gset:
                    r = rk
                    break
            cap_rank.setdefault(name, {})[arm] = r
    cap.close()

    def split(new_by: Dict[str, Optional[int]], base_by: Dict[str, Optional[int]],
              population: List[str]):
        better = same = worse = 0
        not_found = []
        detail = []
        for name in population:
            b, n_ = base_by.get(name), new_by.get(name)
            if n_ is None:
                worse += 1
                not_found.append(name)
                detail.append((name, b, None, "worse_not_found"))
            elif b is None:
                better += 1
                detail.append((name, None, n_, "better_newly_found"))
            elif n_ < b:
                better += 1
                detail.append((name, b, n_, "better"))
            elif n_ == b:
                same += 1
                detail.append((name, b, n_, "same"))
            else:
                worse += 1
                detail.append((name, b, n_, "worse"))
        return {"better": better, "same": same, "worse": worse,
                "not_found_in_new_arm": not_found, "detail": detail}

    ship_by = {n: cap_rank[n]["shipped"] for n in targets}
    deep_by = {n: cap_rank[n]["deep1000"] for n in targets}
    receipt_ship = {n: row_by[n]["shipped_rank"] for n in targets}
    receipt_deep = {n: row_by[n]["deep_rank"] for n in targets}
    capture_matches_receipt = (ship_by == receipt_ship and deep_by == receipt_deep)
    deep_vs_shipped = split(deep_by, ship_by, targets)
    print("capture reproduction: deep1000 vs shipped over 107 -> better="
          + str(deep_vs_shipped["better"]) + " same="
          + str(deep_vs_shipped["same"]) + " worse="
          + str(deep_vs_shipped["worse"])
          + " (published 0/38/69); capture==receipt ranks: "
          + str(capture_matches_receipt), flush=True)

    run_targets = targets[:args.limit] if args.limit else targets

    # ── run the arm ─────────────────────────────────────────────────────
    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager

    cfg = load_config(str(CONFIG))
    cfg.genome.path = BED
    for path, val in KNOBS.items():
        sec, key = path.split(".", 1)
        obj = getattr(cfg, sec)
        if not hasattr(obj, key):
            raise SystemExit("unknown config path " + path)
        setattr(obj, key, val)

    ckpt_path = None
    done: Dict[str, Dict[str, Any]] = {}
    n_segments = 1
    if args.cache:
        cdir = Path(args.cache)
        cdir.mkdir(parents=True, exist_ok=True)
        ckpt_path = cdir / (ARM + ".jsonl")
        if ckpt_path.exists():
            for line in ckpt_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("__segment__"):
                    n_segments += 1
                    continue
                done[rec["needle"]] = rec
            print("  resuming: " + str(len(done)) + " needles checkpointed",
                  flush=True)
        with ckpt_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"__segment__": True,
                                 "started": time.time()}) + "\n")

    todo = [n for n in run_targets if n not in done]
    errors: List[str] = []
    records: List[Dict[str, Any]] = []

    if todo:
        manager = CymatixContextManager(cfg)
        # sanity: the knob really reached the store
        eff_hw = getattr(manager.genome, "_harmonic_weight", None)
        eff_fts = getattr(manager.genome, "_fts5_candidate_depth", None)
        eff_sl = getattr(manager.genome, "_bm25_shortlist_size", None)
        eff_sl_on = getattr(manager.genome, "_bm25_shortlist_enabled", None)
        print("store knobs: harmonic_weight=" + str(eff_hw)
              + " fts5_candidate_depth=" + str(eff_fts)
              + " bm25_shortlist_size=" + str(eff_sl)
              + " bm25_shortlist_enabled=" + str(eff_sl_on), flush=True)
        if eff_hw != 0.0:
            raise SystemExit("harmonic_weight did not reach the store: "
                             + repr(eff_hw))
        knob_check = {"harmonic_weight": eff_hw,
                      "fts5_candidate_depth": eff_fts,
                      "bm25_shortlist_size": eff_sl,
                      "bm25_shortlist_enabled": eff_sl_on}
        try:
            # Untimed warmup (erb receipt convention).
            try:
                manager.build_context(needles[todo[0]]["query"], read_only=True,
                                      ignore_delivered=True, max_genes=K)
            except Exception as exc:
                errors.append("warmup: " + type(exc).__name__ + ": " + str(exc))

            for i, name in enumerate(todo):
                q = needles[name]["query"]
                g = gold.get(name, [])
                try:
                    manager.genome.last_query_scores = {}
                except Exception:
                    pass
                t0 = time.perf_counter()
                try:
                    window = manager.build_context(
                        q, read_only=True, ignore_delivered=True, max_genes=K)
                    wall = (time.perf_counter() - t0) * 1000.0
                    scores = dict(manager.genome.last_query_scores or {})
                    contribs = dict(getattr(
                        manager.genome, "last_tier_contributions", None) or {})
                    expressed = list(getattr(window, "expressed_gene_ids", None)
                                     or ())
                except Exception as exc:
                    wall = (time.perf_counter() - t0) * 1000.0
                    errors.append(name + ": " + type(exc).__name__ + ": "
                                  + str(exc))
                    scores, contribs, expressed = {}, {}, []
                first, all_r = rank_gold(scores, g)
                gset = set(g)
                drank = None
                for j, gid in enumerate(expressed, 1):
                    if gid in gset:
                        drank = j
                        break
                rec = {
                    "needle": name,
                    "map_size": len(scores),
                    "rank_of_first_gold": first,
                    "gold_ranks": all_r,
                    "delivered_gold": 1 if drank else 0,
                    "delivered_gold_rank": drank,
                    "delivered_count": len(expressed),
                    "wall_ms": round(wall, 1),
                    "tiers": tier_summary(contribs, gset),
                }
                records.append(rec)
                if ckpt_path is not None:
                    with ckpt_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec) + "\n")
                if (i + 1) % 10 == 0:
                    print("  [" + ARM + "] " + str(i + 1) + "/" + str(len(todo))
                          + " (segment) elapsed="
                          + str(round(time.time() - t_start)) + "s", flush=True)
        finally:
            try:
                manager.genome.close()
            except Exception:
                pass
    else:
        knob_check = {"note": "all needles resumed from checkpoint; "
                              "store not constructed this segment"}
        _prev = RECEIPTS / ("harmonic_off_dilution_" + args.stamp + ".json")
        if _prev.exists():
            try:
                _pj = json.loads(_prev.read_text(encoding="utf-8"))
                _pk = _pj.get("knob_check_on_store")
                if isinstance(_pk, dict) and "harmonic_weight" in _pk:
                    knob_check = dict(_pk)
                    knob_check["captured_on"] = ("the measuring run; this "
                                                 "segment resumed entirely "
                                                 "from the checkpoint")
            except Exception:
                pass

    by_name = dict(done)
    for r in records:
        by_name[r["needle"]] = r
    ordered = [by_name[n] for n in run_targets if n in by_name]

    new_by = {r["needle"]: r["rank_of_first_gold"] for r in ordered}
    new_vs_shipped = split(new_by, ship_by, run_targets)
    new_vs_deep = split(new_by, deep_by, run_targets)

    b, s, w = (new_vs_shipped["better"], new_vs_shipped["same"],
               new_vs_shipped["worse"])
    if w >= 50:
        verdict = "KILL"
        verdict_reason = ("worse = " + str(w) + " >= 50: at least half the "
                          "dilution survives with the harmonic tier off, so "
                          "the harmonic tie-lottery is NOT the mechanism.")
    elif w <= 20 and (b + s) >= 87:
        verdict = "PASS"
        verdict_reason = ("worse = " + str(w) + " <= 20 and better+same = "
                          + str(b + s) + " >= 87: the depth dilution is a "
                          "harmonic/tie artefact.")
    else:
        verdict = "INCONCLUSIVE"
        verdict_reason = ("worse = " + str(w) + ", better+same = " + str(b + s)
                          + ": neither pre-registered branch fires.")

    lat = [r["wall_ms"] for r in ordered]
    delivered_hist: Dict[str, int] = {}
    for r in ordered:
        k = str(r["delivered_count"])
        delivered_hist[k] = delivered_hist.get(k, 0) + 1

    tier_avail = sum(1 for r in ordered if r["tiers"].get("available"))
    tier_totals: Dict[str, int] = {}
    for r in ordered:
        for t, c in (r["tiers"].get("tier_fire_counts") or {}).items():
            tier_totals[t] = tier_totals.get(t, 0) + c
    gold_tier_names: Dict[str, int] = {}
    for r in ordered:
        for _gid, c in (r["tiers"].get("gold_tier_contrib") or {}).items():
            for t in c:
                gold_tier_names[t] = gold_tier_names.get(t, 0) + 1

    per_needle = []
    for r in ordered:
        n = r["needle"]
        per_needle.append({
            "needle": n,
            "cohort": row_by[n]["cohort"],
            "bucket": row_by[n]["bucket"],
            "question_type": row_by[n]["question_type"],
            "shipped_rank": ship_by[n],
            "deep1000_rank": deep_by[n],
            "harmonic_off_rank": r["rank_of_first_gold"],
            "vs_shipped": ("worse_not_found" if r["rank_of_first_gold"] is None
                           else "better" if r["rank_of_first_gold"] < ship_by[n]
                           else "same" if r["rank_of_first_gold"] == ship_by[n]
                           else "worse"),
            "vs_deep1000": ("worse_not_found" if r["rank_of_first_gold"] is None
                            else "better" if r["rank_of_first_gold"] < deep_by[n]
                            else "same" if r["rank_of_first_gold"] == deep_by[n]
                            else "worse"),
            "map_size": r["map_size"],
            "pre_shortlist_pool_size": (r["tiers"] or {}).get("n_genes"),
            "delivered_count": r["delivered_count"],
            "delivered_gold": r["delivered_gold"],
            "delivered_gold_rank": r["delivered_gold_rank"],
            "shipped_delivered_gold": row_by[n]["shipped_delivered_gold"],
            "deep_delivered_gold": row_by[n]["deep_delivered_gold"],
            "wall_ms": r["wall_ms"],
            "gold_tier_contrib": (r["tiers"].get("gold_tier_contrib") or {}),
        })

    receipt = {
        "stamp": args.stamp,
        "probe": ("FOLLOW-UP 2 to ARM 1 -- head-dilution attribution, "
                  "harmonic tier off"),
        "tool": "benchmarks/dogfood/erb/probe_harmonic_off_dilution.py",
        "tool_sha256": sha256_file(Path(__file__)),
        "git_sha": git_sha(),
        "bed": BED,
        "bed_bytes": Path(BED).stat().st_size,
        "bed_access": ("read-write open by KnowledgeStore construction "
                       "(idempotent DDL); every query read_only=True, "
                       "ignore_delivered=True, CYMATIX_DISABLE_LEARN=1. No "
                       "index created, no vacuum, no existing receipt "
                       "modified."),
        "config": str(CONFIG),
        "arm": ARM,
        "knobs": KNOBS,
        "knob_check_on_store": knob_check,
        "switch_note": (
            "[cymatics] harmonic_links (the knob the handoff pre-registered) "
            "is NOT live on this path: its only consumer is "
            "context_manager.py:2469 inside the `if not read_only:` "
            "tail-write block, i.e. it gates WRITING harmonic edges after a "
            "turn, and every ERB query runs read_only=True -- flipping it "
            "would have been a no-op arm. Tier 5's retrieval-side gate is "
            "the use_harmonic kwarg (knowledge_store.py:3629), which "
            "build_context/_retrieve never passes (hardwired True); the HTTP "
            "/context route exposes it as `profile == \"quality\"` "
            "(routes_context.py:804). The config-file off switch is "
            "[retrieval] harmonic_weight (config.py:861 -> "
            "sharding.build_genome_kwargs:681 -> "
            "KnowledgeStore._harmonic_weight), which at 0.0 zeroes the "
            "additive bonus AND short-circuits fuser.add_tier "
            "(fusion.py:146). THIS ARM USED harmonic_weight = 0.0. Residue: "
            "the harmonic_links SQL still runs and tier_contrib still "
            "records 'harmonic': 0.0; neither reorders the slate."),
        "basis_receipts": [
            "benchmarks/dogfood/erb/receipts/pool_depth_forensics_2026-09-01.json",
            "benchmarks/dogfood/erb/receipts/pool_depth_capture_2026-09-01.sqlite",
            "benchmarks/dogfood/erb/receipts/verify_a1_recompute_2026-09-01.json",
            "benchmarks/dogfood/erb/receipts/verify_a1_completeness_2026-09-01.json",
            "benchmarks/dogfood/erb/receipts/harmonic_off_dilution_prereg_2026-09-01.json",
        ],
        "basis_sha256": {
            "pool_depth_forensics_2026-09-01.json": sha256_file(FORENSICS),
            "pool_depth_capture_2026-09-01.sqlite": sha256_file(CAPTURE),
            "verify_a1_recompute_2026-09-01.json": sha256_file(VERIFY_RECOMPUTE),
            "verify_a1_completeness_2026-09-01.json":
                sha256_file(VERIFY_COMPLETENESS),
            "gold_by_needle_v09x.json": sha256_file(GOLD),
            "needles_resolved_v09x_full.json": sha256_file(NEEDLES),
        },
        "kill_criterion": PREREG,
        "pre_registration": PREREG,
        "population": {
            "rule": ("pool_depth_forensics rows with cohort in "
                     "{miss, hit_control} and BOTH shipped_rank and "
                     "deep_rank non-null"),
            "n": len(targets),
            "n_run": len(ordered),
            "needles": targets,
        },
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "reading": READING,
        "post_run_tool_edits": {
            "tool_sha256_at_preregistration":
                "bdf822947d80fdbeb200ea9fc51eae44cc1f0f464a65404cab93c06dbb5ece02",
            "what_changed": (
                "After the 107-needle arm finished, two PROSE-ONLY fields "
                "were added to this tool and the receipt was regenerated "
                "from the untouched per-needle checkpoint (no bed query was "
                "re-run): the top-level 'reading' paragraph the handoff "
                "asked for, and this disclosure block. No measurement, "
                "population, knob, decision rule or derived number was "
                "touched -- every numeric cell below is byte-identical to "
                "the receipt written by the pre-registered sha, and the "
                "checkpoint jsonl (harmonic_off_ck_2026-09-01/"
                "deep1000_harmonic_off.jsonl) is the independent record."),
            "wall_s_total_note": (
                "the top-level wall_s_total below is the REGENERATING run's "
                "own wall clock (sub-second, checkpoint replay only). The "
                "measuring run took 2027.2 s; the arm's own per-needle sum "
                "is latency.wall_ms_total = 1,995,746 ms, which is derived "
                "from the checkpoint and is unchanged."),
            "knob_check_note": (
                "knob_check_on_store was captured on the measuring run and "
                "is carried forward verbatim here; the regenerating run "
                "constructed no store because every needle resumed from the "
                "checkpoint."),
        },
        "headline": {
            "harmonic_off_vs_shipped": {k: v for k, v in
                                        new_vs_shipped.items() if k != "detail"},
            "harmonic_off_vs_deep1000": {k: v for k, v in
                                         new_vs_deep.items() if k != "detail"},
            "deep1000_vs_shipped_reproduced_from_capture": {
                k: v for k, v in deep_vs_shipped.items() if k != "detail"},
            "deep1000_vs_shipped_published": {"better": 0, "same": 38,
                                              "worse": 69},
            "capture_ranks_equal_receipt_rows": capture_matches_receipt,
        },
        "delivery": {
            "delivered_count_hist": delivered_hist,
            "delivered_gold_total": sum(r["delivered_gold"] for r in ordered),
            "shipped_delivered_gold_total": sum(
                row_by[n]["shipped_delivered_gold"] for n in new_by),
            "deep_delivered_gold_total": sum(
                row_by[n]["deep_delivered_gold"] for n in new_by),
            "note": ("delivered_count 6 vs 12 is the observable budget-tier "
                     "signal; this is a NON-RANDOM subset (the 107 "
                     "both-found needles), not a bed-level rate."),
        },
        "latency": {
            "wall_ms_p50": round(statistics.median(lat), 1) if lat else None,
            "wall_ms_mean": round(statistics.fmean(lat), 1) if lat else None,
            "wall_ms_p95": (round(sorted(lat)[int(0.95 * (len(lat) - 1))], 1)
                            if lat else None),
            "wall_ms_total": round(sum(lat), 1),
            "resumed_segments": n_segments,
            "arm1_shipped_p50_ms": forensics["latency"]["shipped_p50_ms"],
            "arm1_deep1000_p50_ms": forensics["latency"]["deep1000_p50_ms"],
            "caveat": ("this arm ran THIRD in wall-clock terms against a bed "
                       "whose page cache state is not the one either ARM-1 "
                       "arm saw; treat the p50 as indicative, not as a "
                       "controlled latency comparison"),
        },
        "harmonic_tier_liveness": {
            "harmonic_links_table_exists": bool(_hl_exists),
            "harmonic_links_rows": _hl_rows,
            "sqlite_version": sqlite3.sqlite_version,
            "sqlite_limit_variable_number": _var_limit,
            "pre_shortlist_pool_size_median": (
                statistics.median([p for p in
                                   ((r["tiers"] or {}).get("n_genes")
                                    for r in ordered) if p])
                if ordered else None),
            "pre_shortlist_pool_size_max": (
                max([p for p in ((r["tiers"] or {}).get("n_genes")
                                 for r in ordered) if p], default=None)),
            "harmonic_bound_params_median": (
                2 * statistics.median([p for p in
                                       ((r["tiers"] or {}).get("n_genes")
                                        for r in ordered) if p])
                if ordered else None),
            "harmonic_in_tier_contrib_on_any_needle": any(
                "harmonic" in ((r["tiers"] or {}).get("tier_fire_counts") or {})
                for r in ordered),
            "reading": (
                "Tier 5 (knowledge_store.py:3624-3663) is inert on this bed "
                "for two independent reasons. (1) harmonic_links holds 0 "
                "rows, so the bonus map is empty and fuser.add_tier() is a "
                "documented no-op on an empty tier. (2) candidate_ids is "
                "taken PRE-shortlist and the tag lanes carry no LIMIT, so "
                "the pool is ~2e5 documents; the query binds that list "
                "TWICE (gene_id_a IN (...) AND gene_id_b IN (...)), ~4e5 "
                "bound parameters against a 32766 limit, raising "
                "OperationalError('too many SQL variables') which "
                ":3662-3663 swallows into log.debug. (2) is independent of "
                "BOTH admission knobs, so it holds identically for the "
                "shipped arm. The harmonic tier therefore never fired in "
                "EITHER ARM-1 arm, and cannot be the depth-dilution "
                "mechanism."),
        },
        "tier_contributions": {
            "source": "KnowledgeStore.last_tier_contributions after each query",
            "n_needles_with_map": tier_avail,
            "tier_fire_counts_summed_over_needles": tier_totals,
            "gold_row_tier_presence_counts": gold_tier_names,
            "note": ("the full per-gene map is ~1000 genes x N tiers per "
                     "needle, so only the per-tier fire counts and the gold "
                     "genes' own rows are kept; 'harmonic' still appears "
                     "with value 0.0 because the tier's bookkeeping runs "
                     "before the weight is applied"),
        },
        "map_size": {
            "median": statistics.median([r["map_size"] for r in ordered])
            if ordered else None,
            "max": max([r["map_size"] for r in ordered]) if ordered else None,
        },
        "errors": errors,
        "per_needle": per_needle,
        "wall_s_total": round(time.time() - t_start, 1),
    }

    out = (Path(args.out) if args.out
           else RECEIPTS / ("harmonic_off_dilution_" + args.stamp + ".json"))
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    print("\nVERDICT " + verdict + " -- " + verdict_reason)
    print(json.dumps(receipt["headline"], indent=2))
    print(json.dumps(receipt["latency"], indent=2))
    print("receipt -> " + str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

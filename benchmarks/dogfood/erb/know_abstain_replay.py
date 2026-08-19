"""know_abstain_replay.py — abstain + know/miss distribution replay (ERB beds).

#377 re-measure item ("Abstain + know sanity"): with two RRF tiers removed
from the shipped default path (dense off since 2026-08-15, SPLADE off since
2026-08-16; PKI additionally off since 2026-08-17, #370), do the
``top_score`` / ``score_gap`` inputs the know/abstain gates consume shift in
a way that (a) materially changes abstention rates or (b) invalidates the
``[know]`` confidence calibration (fitted pre-flip, 2026-07-06)? And did the
empty-window class (``delivered_count = 0`` while gold sits at pool rank 1;
2/141 at 100k pre-flip) grow?

No existing receipt records these distributions — ``ablation_ladder.py``
captures ranks and delivery but not the know/abstain surface — so this is a
purpose-built replay over the same needle sets, reusing the ladder's
conventions (fresh ``load_config`` + fresh manager per arm, untimed warmup,
``read_only=True`` + ``ignore_delivered=True``, ``CYMATIX_DISABLE_LEARN=1``,
tie-break by gene_id ascending) and importing its helpers directly.

Measurement surface
-------------------
Per needle, one ``build_context`` call (the /context pipeline — the only
surface with the ABSTAIN tier), then the SERVED know/miss discriminator via
``server.helpers._compute_know_or_miss_block`` — the exact function the
/context route runs, reading the #350 request-scoped ``retrieval_scores``
snapshot off the window. ``top_score`` / ``score_gap`` / ``ratio_top2`` are
computed from that same snapshot with the same convention the helper uses
(gap = top1 − top2; singleton pools report gap = top1). ``confidence_raw``
re-runs ``compute_confidence`` on every needle regardless of the verdict, so
the confidence distribution is comparable across arms even where a non-sparse
miss branch (abstain / stale / superseded) would have masked it.

Comparison arms
---------------
``postflip_default`` runs the shipped cymatix.toml untouched.
``preflip_dense_splade`` re-enables exactly the two removed RRF tiers
(#377's framing). ``preflip_full`` additionally re-enables PKI — the exact
pre-2026-08-15 shipped read path — so the "one knob off" caveat cannot
recur. The beds already carry dense vectors and the splade_terms index, so
all arms are read-only replays of the same bytes.

Usage::

    python benchmarks/dogfood/erb/know_abstain_replay.py \
        --stamp 20260819-know-abstain-100k
    python benchmarks/dogfood/erb/know_abstain_replay.py \
        --genome f:/tmp/erb_carve/erb_829k_nosplade.db \
        --resolved benchmarks/dogfood/erb/needles_resolved_blob.json \
        --gold benchmarks/dogfood/erb/gold_by_needle_blob.json \
        --arms postflip_default --sample 100 --seed 42
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Retrieval-only probe: never let a query mutate the bed's learning state.
# Must be set BEFORE any manager is constructed (ladder convention).
os.environ.setdefault("CYMATIX_DISABLE_LEARN", "1")

from ablation_ladder import (  # noqa: E402  (path-inserted sibling module)
    _clear_rank_publication,
    _git_sha,
    _resolve,
    delivered_gold_fields,
    final_rank_of_first_gold,
    median_or_none,
    percentile,
    rank_gold,
    set_dotted,
)

# ── Arm table ────────────────────────────────────────────────────────────
# Dotted-knob overrides applied to a FRESH load_config() per arm. Empty =
# shipped config untouched.
ARMS: Dict[str, Dict[str, Any]] = {
    "postflip_default": {},
    "preflip_dense_splade": {
        "retrieval.dense_embedding_enabled": True,
        "ingestion.splade_enabled": True,
    },
    "preflip_full": {
        "retrieval.dense_embedding_enabled": True,
        "ingestion.splade_enabled": True,
        "retrieval.pki_enabled": True,
    },
}


def _dist(values: Sequence[float]) -> Optional[Dict[str, Any]]:
    """Compact distribution summary. Empty -> None (absent measurements
    must not masquerade as zeros)."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return {
        "n": len(vals),
        "mean": round(float(statistics.mean(vals)), 4),
        "min": round(min(vals), 4),
        "p10": percentile(vals, 10),
        "p25": percentile(vals, 25),
        "p50": percentile(vals, 50),
        "p75": percentile(vals, 75),
        "p90": percentile(vals, 90),
        "max": round(max(vals), 4),
    }


def summarize(rows: Sequence[Mapping[str, Any]], k: int, emit_floor: float) -> Dict[str, Any]:
    n = len(rows)
    ok = [r for r in rows if not r.get("error")]
    miss_hist: Dict[str, int] = {}
    tier_hist: Dict[str, int] = {}
    for r in ok:
        if r.get("verdict") == "miss":
            reason = str(r.get("miss_reason"))
            miss_hist[reason] = miss_hist.get(reason, 0) + 1
        tier = str(r.get("budget_tier"))
        tier_hist[tier] = tier_hist.get(tier, 0) + 1
    know_rows = [r for r in ok if r.get("verdict") == "know"]
    conf_raw = [r.get("confidence_raw") for r in ok if r.get("confidence_raw") is not None]
    return {
        "n_queries": n,
        "errors": sum(1 for r in rows if r.get("error")),
        "recall_at_k": round(sum(r.get("recall_hit", 0) for r in rows) / n, 4) if n else 0.0,
        "delivered_gold_rate": round(sum(r.get("delivered_gold", 0) for r in rows) / n, 4) if n else 0.0,
        "abstain_count": sum(1 for r in ok if r.get("abstain")),
        "know_count": len(know_rows),
        "miss_count": sum(1 for r in ok if r.get("verdict") == "miss"),
        "miss_reason_hist": dict(sorted(miss_hist.items())),
        "budget_tier_hist": dict(sorted(tier_hist.items())),
        "top_score": _dist([r.get("top_score") for r in ok]),
        "score_gap": _dist([r.get("score_gap") for r in ok]),
        "ratio_top2": _dist([r.get("ratio_top2") for r in ok]),
        "confidence_raw": _dist(conf_raw),
        "confidence_raw_ge_emit_floor": sum(1 for c in conf_raw if float(c) >= emit_floor),
        "know_confidence": _dist([r.get("confidence") for r in know_rows]),
        "pool_size": _dist([r.get("pool_size") for r in ok]),
        "delivered_count": _dist([r.get("delivered_count") for r in ok]),
        "delivered_count_zero": sum(1 for r in rows if r.get("delivered_count") == 0),
        # The #377 empty-window class: nothing delivered while the pool's
        # rank-1 candidate was gold (2/141 at 100k pre-flip).
        "empty_window_gold_rank1": sum(1 for r in rows if r.get("empty_window_gold_rank1")),
        "latency_ms": {
            "p50": percentile([r["wall_ms"] for r in rows if "wall_ms" in r], 50),
            "p95": percentile([r["wall_ms"] for r in rows if "wall_ms" in r], 95),
        },
    }


def run_arm(
    name: str,
    knobs: Mapping[str, Any],
    *,
    genome_path: str,
    config_path: Optional[str],
    needles: Sequence[Mapping[str, str]],
    gold_by_needle: Mapping[str, set],
    k: int,
) -> Dict[str, Any]:
    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager
    from cymatix_context.context_packet import _coordinate_confidence
    from cymatix_context.schemas import KnowBlock, MissBlock
    from cymatix_context.scoring.know_calibration import (
        calibration_from_config,
        compute_confidence,
    )
    from cymatix_context.scoring.know_decision import _agree_from_tier_contributions
    from cymatix_context.server.helpers import _compute_know_or_miss_block

    cfg = load_config(config_path) if config_path else load_config()
    cfg.genome.path = genome_path
    unapplied = [p for p, v in knobs.items() if not set_dotted(cfg, p, v)]
    if unapplied:
        return {
            "arm": name,
            "knobs_changed": dict(knobs),
            "skipped": True,
            "errors": [f"config paths not found: {unapplied}"],
        }
    cal = calibration_from_config(cfg.know)

    errors: List[str] = []
    rows: List[Dict[str, Any]] = []
    signal_keys: set = set()
    manager = CymatixContextManager(cfg)
    warmed = False
    try:
        # Untimed warmup (erb receipt convention): the first query of an arm
        # pays lazy encoder construction; folding it into p50 would bill the
        # dense/splade arms for their model loads.
        if needles:
            try:
                manager.build_context(
                    needles[0]["query"], read_only=True,
                    ignore_delivered=True, max_genes=k,
                )
                warmed = True
            except Exception as exc:  # noqa: BLE001
                errors.append(f"warmup: {type(exc).__name__}: {exc}")

        for needle in needles:
            nm = needle["name"]
            gold = set(gold_by_needle.get(nm, set()))
            try:
                _clear_rank_publication(manager.genome)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{nm}: rank publication clear failed: {exc}")
            t0 = time.perf_counter()
            try:
                window = manager.build_context(
                    needle["query"], read_only=True,
                    ignore_delivered=True, max_genes=k,
                )
            except Exception as exc:  # noqa: BLE001
                wall_ms = (time.perf_counter() - t0) * 1000.0
                detail = f"{type(exc).__name__}: {exc}"
                errors.append(f"{nm}: {detail}")
                rows.append({
                    "needle": nm, "error": detail,
                    "wall_ms": round(wall_ms, 3),
                    "recall_hit": 0, "delivered_gold": 0,
                    "delivered_count": 0, "empty_window_gold_rank1": 0,
                })
                continue
            wall_ms = (time.perf_counter() - t0) * 1000.0

            # #350 request-scoped snapshot — the exact map the served
            # know/miss reader consumes. Falls back like the route does.
            _win_scores = getattr(window, "retrieval_scores", None)
            scores = dict(
                _win_scores if _win_scores is not None
                else (manager.genome.last_query_scores or {})
            )
            sorted_vals = sorted(scores.values(), reverse=True)
            top_score = float(sorted_vals[0]) if sorted_vals else 0.0
            second = float(sorted_vals[1]) if len(sorted_vals) > 1 else None
            # Served convention (server/helpers.py): singleton pool -> gap
            # and ratio both fall back to top_score.
            score_gap = (top_score - second) if second is not None else top_score
            ratio_top2 = (
                (top_score / second) if (second is not None and second > 0)
                else top_score
            )

            first, gold_ranks = rank_gold(scores, gold)
            delivered = list(getattr(window, "expressed_gene_ids", None) or ())
            dg = delivered_gold_fields(delivered, gold)
            meta = getattr(window, "metadata", None) or {}
            status = getattr(window.context_health, "status", None)

            sig = dict(getattr(manager.genome, "last_signal_timings", None) or {})
            signal_keys.update(sig.keys())

            # Served know/miss verdict — the /context route's own function.
            verdict = None
            miss_reason = None
            confidence = None
            served_top_score = None
            served_score_gap = None
            served_ratio = None
            gene_id_match = None
            soft_stale = None
            try:
                block = _compute_know_or_miss_block(
                    cymatix=manager, window=window, query=needle["query"],
                )
                if isinstance(block, KnowBlock):
                    verdict = "know"
                    confidence = float(block.confidence)
                    served_top_score = float(block.top_score)
                    served_score_gap = float(block.score_gap)
                    gene_id_match = block.gene_id_match
                    soft_stale = bool(block.soft_stale)
                elif isinstance(block, MissBlock):
                    verdict = "miss"
                    miss_reason = str(block.reason)
                    served_top_score = float(block.top_score)
                    served_ratio = float(block.ratio)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{nm}: know block failed: {type(exc).__name__}: {exc}")

            # Confidence distribution independent of the verdict branch:
            # the same compute_confidence call branch 4 runs, with the same
            # inputs the served helper derives.
            confidence_raw = None
            lex_agree = None
            coord_conf = None
            try:
                tier_contrib = getattr(window, "tier_contributions", None)
                if tier_contrib is None:
                    tier_contrib = getattr(manager.genome, "last_tier_contributions", {}) or {}
                lex_agree = _agree_from_tier_contributions(tier_contrib, k=3)
                genes_proxy: List[Any] = []
                if delivered:
                    try:
                        row_map = manager.genome.get_citation_rows(delivered)

                        class _P:  # noqa: D401 — source_id-only proxy
                            __slots__ = ("gene_id", "source_id")

                            def __init__(self, gid, sid):
                                self.gene_id = gid
                                self.source_id = sid

                        for gid in delivered:
                            r = row_map.get(gid)
                            if r is not None:
                                genes_proxy.append(_P(gid, r["source_id"]))
                    except Exception:  # noqa: BLE001
                        genes_proxy = []
                coord_conf = _coordinate_confidence(needle["query"], genes_proxy) if genes_proxy else 0.0
                freshness_min = getattr(window.context_health, "freshness_min", None)
                confidence_raw = float(compute_confidence(
                    top_score=top_score,
                    score_gap=score_gap,
                    lexical_dense_agree=bool(lex_agree),
                    coordinate_confidence=float(coord_conf),
                    calibration=cal,
                    freshness_min=freshness_min,
                ))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{nm}: confidence_raw failed: {type(exc).__name__}: {exc}")

            ranked_ids = list(getattr(manager.genome, "last_ranked_ids", None) or ())
            classifier_meta = meta.get("classifier") or {}

            rows.append({
                "needle": nm,
                "wall_ms": round(wall_ms, 3),
                "pool_size": len(scores),
                "top_score": round(top_score, 6),
                "second_score": round(second, 6) if second is not None else None,
                "score_gap": round(score_gap, 6),
                "ratio_top2": round(ratio_top2, 6),
                "rank_of_first_gold": first,
                "gold_ranks": gold_ranks,
                "recall_hit": 1 if (first is not None and first <= k) else 0,
                "final_rank_of_first_gold": final_rank_of_first_gold(ranked_ids, gold),
                **dg,
                "budget_tier": meta.get("budget_tier"),
                "health_status": status,
                "abstain": status == "abstain",
                "abstain_reason": meta.get("abstain_reason"),
                "classifier_class": classifier_meta.get("class"),
                "verdict": verdict,
                "miss_reason": miss_reason,
                "confidence": round(confidence, 6) if confidence is not None else None,
                "served_top_score": round(served_top_score, 6) if served_top_score is not None else None,
                "served_score_gap": round(served_score_gap, 6) if served_score_gap is not None else None,
                "served_ratio": round(served_ratio, 6) if served_ratio is not None else None,
                "gene_id_match": gene_id_match,
                "soft_stale": soft_stale,
                "lexical_dense_agree": lex_agree,
                "coordinate_confidence": round(coord_conf, 6) if coord_conf is not None else None,
                "confidence_raw": round(confidence_raw, 6) if confidence_raw is not None else None,
                "empty_window_gold_rank1": 1 if (dg["delivered_count"] == 0 and first == 1) else 0,
            })
    finally:
        try:
            manager.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"manager.close failed: {exc}")

    emit_floor = float(cfg.know.emit_floor)
    return {
        "arm": name,
        "knobs_changed": dict(knobs),
        "warmup_ran": warmed,
        "signal_keys_seen": sorted(signal_keys),
        "summary": summarize(rows, k, emit_floor),
        "errors": errors,
        "per_query": rows,
    }


def merge_chunks(chunk_paths: Sequence[str], out_path: str, stamp: str) -> int:
    """Recombine chunked foreground runs into one receipt.

    Chunks are produced by running this tool with ``--offset``/``--limit``
    slices of the same needle file (each chunk pays its own untimed warmup,
    so p50/p95 stay warm-query numbers). Identity fields must agree across
    chunks; per-arm ``per_query`` rows are concatenated in the given order
    and summaries recomputed over the union, so the merged receipt is
    byte-equivalent in shape to a single-shot run plus a ``chunks``
    provenance block.
    """
    if not chunk_paths:
        print("no chunk receipts given", file=sys.stderr)
        return 2
    chunks = [json.loads(_resolve(p).read_text(encoding="utf-8")) for p in chunk_paths]
    base = chunks[0]
    # bed_bytes is deliberately NOT an identity field: log_health appends to
    # the bed's health_log outside the learn gate (observability, not
    # learning — see context_manager's tail_writes block), so the file grows
    # a few KB per chunk. Per-chunk bytes land in the provenance block.
    identity = ("tool", "bed", "config", "git_sha", "k",
                "needles_file", "gold_file")
    for i, c in enumerate(chunks[1:], start=2):
        for key in identity:
            if c.get(key) != base.get(key):
                print(f"chunk {i} disagrees on {key}: "
                      f"{c.get(key)!r} != {base.get(key)!r}", file=sys.stderr)
                return 2
    merged_arms: Dict[str, Dict[str, Any]] = {}
    arm_order: List[str] = []
    seen_needles: Dict[str, set] = {}
    for c in chunks:
        for arm in c.get("arms", []):
            name = arm["arm"]
            if name not in merged_arms:
                merged_arms[name] = {
                    "arm": name,
                    "knobs_changed": arm.get("knobs_changed", {}),
                    "warmup_ran": bool(arm.get("warmup_ran")),
                    "signal_keys_seen": set(arm.get("signal_keys_seen") or ()),
                    "errors": list(arm.get("errors") or ()),
                    "per_query": [],
                }
                arm_order.append(name)
                seen_needles[name] = set()
            else:
                merged_arms[name]["warmup_ran"] = (
                    merged_arms[name]["warmup_ran"] and bool(arm.get("warmup_ran"))
                )
                merged_arms[name]["signal_keys_seen"] |= set(arm.get("signal_keys_seen") or ())
                merged_arms[name]["errors"].extend(arm.get("errors") or ())
            for row in arm.get("per_query", []):
                if row["needle"] in seen_needles[name]:
                    print(f"duplicate needle {row['needle']} in arm {name}",
                          file=sys.stderr)
                    return 2
                seen_needles[name].add(row["needle"])
                merged_arms[name]["per_query"].append(row)
    emit_floor = float((base.get("know_calibration") or {}).get("emit_floor", 0.45))
    k = int(base.get("k", 12))
    arms_out: List[Dict[str, Any]] = []
    for name in arm_order:
        arm = merged_arms[name]
        arm["signal_keys_seen"] = sorted(arm["signal_keys_seen"])
        arm["summary"] = summarize(arm["per_query"], k, emit_floor)
        arms_out.append({
            "arm": arm["arm"],
            "knobs_changed": arm["knobs_changed"],
            "warmup_ran": arm["warmup_ran"],
            "signal_keys_seen": arm["signal_keys_seen"],
            "summary": arm["summary"],
            "errors": arm["errors"],
            "per_query": arm["per_query"],
        })
        s = arm["summary"]
        print(f"  {name:22s} n={s['n_queries']} abstain={s['abstain_count']} "
              f"know={s['know_count']} empty_win={s['empty_window_gold_rank1']} "
              f"r@{k}={s['recall_at_k']} dgr={s['delivered_gold_rate']}")
    payload = {key: base.get(key) for key in (
        "tool", "issue", "bed", "bed_bytes", "config", "git_sha",
        "needles_file", "gold_file", "seed", "k", "know_calibration",
        "env", "note",
    )}
    payload["stamp"] = stamp or base.get("stamp")
    payload["needle_count"] = max(
        (len(a["per_query"]) for a in arms_out), default=0)
    payload["sample"] = base.get("sample")
    payload["sampled_needles"] = base.get("sampled_needles")
    payload["chunks"] = [
        {"path": str(p), "stamp": c.get("stamp"),
         "offset": c.get("offset"), "limit": c.get("limit"),
         "needle_count": c.get("needle_count"),
         "bed_bytes": c.get("bed_bytes")}
        for p, c in zip(chunk_paths, chunks)
    ]
    payload["arms"] = arms_out
    out = _resolve(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Abstain + know/miss distribution replay (ERB beds, #377).",
    )
    ap.add_argument("--genome", default="f:/tmp/erb_carve/erb_100k.db")
    ap.add_argument("--resolved", default="benchmarks/dogfood/erb/needles_resolved_carve.json")
    ap.add_argument("--gold", default="benchmarks/dogfood/erb/gold_by_needle_carve.json")
    ap.add_argument("--offset", type=int, default=0,
                    help="skip the first N needles before --limit — lets a "
                         "long bed be replayed in bounded FOREGROUND chunks "
                         "that --merge recombines")
    ap.add_argument("--limit", type=int, default=0, help="needles to run (0 = all)")
    ap.add_argument("--sample", type=int, default=0,
                    help="deterministic random subsample of N needles (0 = off); "
                         "seeded by --seed and applied BEFORE --offset/--limit "
                         "so chunked runs slice the same subsample")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--arms", default="", help="comma list; empty = all")
    ap.add_argument("--config", default="cymatix.toml")
    ap.add_argument("--out", default="benchmarks/dogfood/erb/receipts/postflip_know_abstain_sanity_100k.json")
    ap.add_argument("--stamp", default="")
    ap.add_argument("--merge", nargs="*", default=None,
                    help="merge chunk receipts (paths, in needle order) into "
                         "--out; runs no queries. Chunks must agree on bed / "
                         "config / k / git_sha; per_query rows are "
                         "concatenated per arm and summaries recomputed.")
    args = ap.parse_args(argv)

    if args.merge is not None:
        return merge_chunks(args.merge, args.out, args.stamp)

    resolved_path = _resolve(args.resolved)
    gold_path = _resolve(args.gold)
    needles = json.loads(resolved_path.read_text(encoding="utf-8"))["needles"]
    # Sampling runs FIRST (on the full needle list) so chunked invocations
    # (--offset/--limit) slice the same deterministic subsample: sample N
    # with a fixed seed, then order by needle name for a stable run order.
    sampled_names: Optional[List[str]] = None
    if args.sample and args.sample < len(needles):
        rng = random.Random(args.seed)
        needles = sorted(
            rng.sample(needles, args.sample),
            key=lambda item: item["name"],
        )
        sampled_names = [item["name"] for item in needles]
    if args.offset:
        needles = needles[args.offset:]
    if args.limit:
        needles = needles[: args.limit]
    gold_by_needle = {
        key: set(val)
        for key, val in json.loads(gold_path.read_text(encoding="utf-8")).items()
    }

    genome_path = str(_resolve(args.genome))
    if not Path(genome_path).exists():
        print(f"genome not found: {genome_path}", file=sys.stderr)
        return 2
    os.environ["CYMATIX_GENOME_PATH"] = genome_path

    config_path: Optional[str] = None
    if args.config:
        candidate = _resolve(args.config)
        if not candidate.exists():
            print(f"config not found: {candidate}", file=sys.stderr)
            return 2
        config_path = str(candidate)

    wanted = {a.strip() for a in args.arms.split(",") if a.strip()}
    selected = {n: k for n, k in ARMS.items() if not wanted or n in wanted}

    from cymatix_context.config import load_config
    base_cfg = load_config(config_path) if config_path else load_config()
    know_cfg = base_cfg.know

    print(f"know/abstain replay: {len(selected)} arms x {len(needles)} needles, "
          f"k={args.k}, bed={genome_path}", flush=True)

    arm_rows: List[Dict[str, Any]] = []
    for name, knobs in selected.items():
        t0 = time.perf_counter()
        row = run_arm(
            name, knobs,
            genome_path=genome_path,
            config_path=config_path,
            needles=needles,
            gold_by_needle=gold_by_needle,
            k=args.k,
        )
        arm_rows.append(row)
        if row.get("skipped"):
            print(f"  SKIP {name}: {row['errors']}", file=sys.stderr)
            continue
        s = row["summary"]
        print(
            f"  {name:22s} abstain={s['abstain_count']}/{s['n_queries']} "
            f"know={s['know_count']} empty_win={s['empty_window_gold_rank1']} "
            f"top_score p50={(s['top_score'] or {}).get('p50')} "
            f"gap p50={(s['score_gap'] or {}).get('p50')} "
            f"r@{args.k}={s['recall_at_k']} dgr={s['delivered_gold_rate']} "
            f"p50={s['latency_ms']['p50']}ms "
            f"[{round((time.perf_counter()-t0)/60.0, 1)} min]",
            flush=True,
        )

    payload = {
        "tool": "benchmarks/dogfood/erb/know_abstain_replay.py",
        "issue": "#377 re-measure: abstain + know sanity",
        "stamp": args.stamp,
        "bed": genome_path,
        "bed_bytes": Path(genome_path).stat().st_size,
        "config": config_path or "(auto-discovered)",
        "git_sha": _git_sha(REPO_ROOT),
        "needles_file": str(resolved_path),
        "gold_file": str(gold_path),
        "needle_count": len(needles),
        "offset": args.offset or None,
        "limit": args.limit or None,
        "sample": args.sample or None,
        "sampled_needles": sampled_names,
        "seed": args.seed,
        "k": args.k,
        "know_calibration": {
            "emit_floor": know_cfg.emit_floor,
            "s_ref": know_cfg.s_ref,
            "g_ref": know_cfg.g_ref,
            "betas": list(know_cfg.betas),
            "calibrated_at": getattr(know_cfg, "calibrated_at", None),
            "calibrated_on_n": getattr(know_cfg, "calibrated_on_n", None),
        },
        "env": {
            "CYMATIX_ENCODER_URL": os.environ.get("CYMATIX_ENCODER_URL", "") or None,
            "CYMATIX_DISABLE_LEARN": os.environ.get("CYMATIX_DISABLE_LEARN"),
            "CYMATIX_GENOME_PATH": os.environ.get("CYMATIX_GENOME_PATH"),
            "CYMATIX_ABSTAIN_DISABLE": os.environ.get("CYMATIX_ABSTAIN_DISABLE"),
        },
        "note": (
            "One build_context per needle (read_only=True, ignore_delivered="
            "True, max_genes=k), then the SERVED know/miss discriminator via "
            "server.helpers._compute_know_or_miss_block on the #350 request-"
            "scoped retrieval_scores snapshot. top_score/score_gap/ratio_top2 "
            "use the served convention (singleton pool falls back to "
            "top_score). confidence_raw re-runs compute_confidence on every "
            "needle regardless of verdict so the distribution is comparable "
            "across arms. budget_tier/abstain come from the window (the "
            "ABSTAIN tier only exists on the /context pipeline). "
            "empty_window_gold_rank1 = delivered_count==0 while the pool's "
            "rank-1 (score-map basis, gene_id tie-break) is gold — the "
            "2/141-at-100k pre-flip class from #377. Untimed warmup per arm; "
            "latencies are wall ms on a shared box, within-run relatives only."
        ),
        "arms": arm_rows,
    }

    out = _resolve(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

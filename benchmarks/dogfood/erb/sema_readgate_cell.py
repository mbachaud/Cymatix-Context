"""sema_readgate_cell.py — #371 deciding cell: no_sema read-gate arm, 829k, n=469.

The #371 ESCALATE verdict (2026-08-19) named one terminal cell for the
``sema_embed_on_ingest`` half: the ``no_sema`` read-gate arm on the 829k bed
at full statistical power (n=469, delivered basis). #227 semantics make the
read-gate arm A/B-equivalent to an ingest-side-off world across the whole
retrieval path: when ``[ingestion] sema_embed_on_ingest = false`` the SEMA
codec is never materialised (context_manager.py ``self._sema_codec`` stays
None, threaded to the store via ``build_genome_kwargs``), and the entire
Tier-4 block — Mode A boost, Mode B pool-fill, and the ``sema_boost`` signal
— sits behind ``knowledge_store.py`` ``if self._sema_codec is not None:``.
The bed's stored vectors become unreachable bytes, exactly as if they had
never been written.

The BASELINE cell is not re-run: the ``no_pki`` arm of
``benchmarks/dogfood/erb/receipts/postflip_default_confirm_829k.json`` is the
shipped-defaults world on the same bed and needle set (PKI has since flipped
off on master, #370, so that arm IS the shipped config). This tool runs only
the ablated arm and, at merge time, pairs it per-needle against that receipt.

Methodology is the ladder's, unchanged: fresh ``load_config`` + fresh
manager, untimed warmup per invocation, ``read_only=True`` +
``ignore_delivered=True`` + ``CYMATIX_DISABLE_LEARN=1``, ranks from the
post-blend score map with gene_id-ascending tie-break, delivered basis from
``window.expressed_gene_ids``, final order from ``genome.last_ranked_ids``
(cleared before every call). On top of the ladder's rank/delivery surface it
carries the know/abstain surface from ``know_abstain_replay.py`` (PR #398):
abstain status + reason off the window, the SERVED know/miss verdict via
``server.helpers._compute_know_or_miss_block`` on the #350 request-scoped
``retrieval_scores`` snapshot, and top_score / score_gap — cheap to carry and
load-bearing for the named ``[abstain]`` floor-recalibration caveat (PR #399
measured one dogfood loss as ``score_below_floor`` with gold at final rank 8;
the floor was calibrated with sema's score mass present).

Self-validation: the arm's ``signal_keys_seen`` must NOT contain
``sema_boost``, and the baseline receipt's arm MUST — recorded per chunk and
re-checked at merge. An identity guard aborts loudly if the loaded config
already has the knob at False (an identity transform measures nothing).

Usage (chunked foreground runs, then merge)::

    python benchmarks/dogfood/erb/sema_readgate_cell.py \
        --offset 0 --limit 94 --out f:/tmp/sema_ab/chunks/chunk00.json \
        --stamp 20260819-sema-readgate-829k
    ...
    python benchmarks/dogfood/erb/sema_readgate_cell.py \
        --merge f:/tmp/sema_ab/chunks/chunk*.json \
        --baseline benchmarks/dogfood/erb/receipts/postflip_default_confirm_829k.json \
        --baseline-arm no_pki \
        --out benchmarks/dogfood/erb/receipts/sema_readgate_829k_n469.json \
        --stamp 20260819-sema-readgate-829k
"""

from __future__ import annotations

import argparse
import json
import os
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
    get_dotted,
    median_or_none,
    percentile,
    rank_gold,
    set_dotted,
)

ARM_NAME = "no_sema_readgate"
ARM_KNOBS: Dict[str, Any] = {"ingestion.sema_embed_on_ingest": False}
ARM_GATE = (
    "context_manager.py (#227: sema_embed_on_ingest=false -> self._sema_codec "
    "never materialised, threaded to the store via build_genome_kwargs) -> "
    "knowledge_store.py:3169 `if self._sema_codec is not None:` (whole Tier-4 "
    "block: Mode A boost, Mode B pool-fill, sema_boost signal)"
)


def summarize(rows: Sequence[Mapping[str, Any]], k: int) -> Dict[str, Any]:
    n = len(rows)
    ok = [r for r in rows if not r.get("error")]
    hit_ranks = [r["rank_of_first_gold"] for r in rows
                 if r.get("rank_of_first_gold") is not None]
    abstain_rows = [r for r in ok if r.get("abstain")]
    reason_hist: Dict[str, int] = {}
    for r in abstain_rows:
        reason = str(r.get("abstain_reason"))
        reason_hist[reason] = reason_hist.get(reason, 0) + 1
    miss_hist: Dict[str, int] = {}
    for r in ok:
        if r.get("verdict") == "miss":
            reason = str(r.get("miss_reason"))
            miss_hist[reason] = miss_hist.get(reason, 0) + 1
    return {
        "n_queries": n,
        "errors": sum(1 for r in rows if r.get("error")),
        "recall_at_k": round(sum(r.get("recall_hit", 0) for r in rows) / n, 4) if n else 0.0,
        "final_recall_at_k": round(sum(r.get("final_recall_hit", 0) for r in rows) / n, 4) if n else 0.0,
        "delivered_gold_queries": sum(r.get("delivered_gold", 0) for r in rows),
        "delivered_gold_rate": round(sum(r.get("delivered_gold", 0) for r in rows) / n, 10) if n else 0.0,
        "gold_found_queries": len(hit_ranks),
        "mean_rank_of_gold": round(sum(hit_ranks) / len(hit_ranks), 2) if hit_ranks else None,
        "median_rank_of_gold": median_or_none(hit_ranks),
        "abstain_count": len(abstain_rows),
        "abstain_reason_hist": dict(sorted(reason_hist.items())),
        # The PR #399 coupling class: abstain fired while gold sat inside the
        # final top-k — delivery was lost to the gate, not to retrieval.
        "abstain_with_gold_in_final_topk": sum(
            1 for r in abstain_rows
            if r.get("final_rank_of_first_gold") is not None
            and r["final_rank_of_first_gold"] <= k
        ),
        "know_count": sum(1 for r in ok if r.get("verdict") == "know"),
        "miss_count": sum(1 for r in ok if r.get("verdict") == "miss"),
        "miss_reason_hist": dict(sorted(miss_hist.items())),
        "delivered_count_zero": sum(1 for r in rows if r.get("delivered_count") == 0),
        "latency_ms": {
            "p50": percentile([r["wall_ms"] for r in rows if "wall_ms" in r], 50),
            "p95": percentile([r["wall_ms"] for r in rows if "wall_ms" in r], 95),
        },
    }


def run_arm(
    *,
    genome_path: str,
    config_path: Optional[str],
    needles: Sequence[Mapping[str, str]],
    gold_by_needle: Mapping[str, set],
    k: int,
) -> Dict[str, Any]:
    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager
    from cymatix_context.schemas import KnowBlock, MissBlock
    from cymatix_context.server.helpers import _compute_know_or_miss_block

    cfg = load_config(config_path) if config_path else load_config()
    cfg.genome.path = genome_path

    # Identity guard: the ablation must actually change the loaded config.
    for path, value in ARM_KNOBS.items():
        current = get_dotted(cfg, path)
        if current == value:
            raise SystemExit(
                f"identity trap: {path} is already {value!r} in the loaded "
                "config — this arm would measure an identity transform"
            )
    unapplied = [p for p, v in ARM_KNOBS.items() if not set_dotted(cfg, p, v)]
    if unapplied:
        raise SystemExit(f"config paths not found: {unapplied}")

    errors: List[str] = []
    rows: List[Dict[str, Any]] = []
    signal_keys: set = set()
    manager = CymatixContextManager(cfg)
    warmed = False
    sema_codec_is_none = getattr(manager, "_sema_codec", "MISSING") is None
    try:
        # Untimed warmup (erb receipt convention).
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
                # A failed query is still a row in the paired basis — a miss
                # for this arm on this needle (ladder convention).
                rows.append({
                    "needle": nm, "error": detail,
                    "wall_ms": round(wall_ms, 3),
                    "recall_hit": 0, "final_recall_hit": 0,
                    "rank_of_first_gold": None,
                    "final_rank_of_first_gold": None,
                    "delivered_gold": 0, "delivered_count": 0,
                    "delivered_gold_rank": None,
                })
                continue
            wall_ms = (time.perf_counter() - t0) * 1000.0

            # #350 request-scoped snapshot — the map the served know/miss
            # reader consumes. Falls back like the route does.
            _win_scores = getattr(window, "retrieval_scores", None)
            scores = dict(
                _win_scores if _win_scores is not None
                else (manager.genome.last_query_scores or {})
            )
            sorted_vals = sorted(scores.values(), reverse=True)
            top_score = float(sorted_vals[0]) if sorted_vals else 0.0
            second = float(sorted_vals[1]) if len(sorted_vals) > 1 else None
            # Served convention (server/helpers.py): singleton pool -> gap
            # falls back to top_score.
            score_gap = (top_score - second) if second is not None else top_score

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
            try:
                block = _compute_know_or_miss_block(
                    cymatix=manager, window=window, query=needle["query"],
                )
                if isinstance(block, KnowBlock):
                    verdict = "know"
                    confidence = float(block.confidence)
                    served_top_score = float(block.top_score)
                    served_score_gap = float(block.score_gap)
                elif isinstance(block, MissBlock):
                    verdict = "miss"
                    miss_reason = str(block.reason)
                    served_top_score = float(block.top_score)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{nm}: know block failed: {type(exc).__name__}: {exc}")

            final_rank = final_rank_of_first_gold(
                list(getattr(manager.genome, "last_ranked_ids", None) or ()), gold)

            rows.append({
                "needle": nm,
                "wall_ms": round(wall_ms, 3),
                "pool_size": len(scores),
                "top_score": round(top_score, 6),
                "score_gap": round(score_gap, 6),
                "rank_of_first_gold": first,
                "gold_ranks": gold_ranks,
                "recall_hit": 1 if (first is not None and first <= k) else 0,
                "final_rank_of_first_gold": final_rank,
                "final_recall_hit": 1 if (final_rank is not None and final_rank <= k) else 0,
                **dg,
                "abstain": status == "abstain",
                "abstain_reason": meta.get("abstain_reason"),
                "health_status": status,
                "verdict": verdict,
                "miss_reason": miss_reason,
                "confidence": round(confidence, 6) if confidence is not None else None,
                "served_top_score": round(served_top_score, 6) if served_top_score is not None else None,
                "served_score_gap": round(served_score_gap, 6) if served_score_gap is not None else None,
                "signal_ms": {name: round(float(ms), 3) for name, ms in sorted(sig.items())},
            })
    finally:
        try:
            manager.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"manager.close failed: {exc}")

    return {
        "arm": ARM_NAME,
        "knobs_changed": dict(ARM_KNOBS),
        "gate": ARM_GATE,
        "sema_codec_is_none": sema_codec_is_none,
        "warmup_ran": warmed,
        "signal_keys_seen": sorted(signal_keys),
        "sema_boost_signal_absent": "sema_boost" not in signal_keys,
        "summary": summarize(rows, k),
        "errors": errors,
        "per_query": rows,
    }


def _paired_delta(
    arm_rows: Sequence[Mapping[str, Any]],
    base_rows: Sequence[Mapping[str, Any]],
    k: int,
) -> Dict[str, Any]:
    """Needle-paired delta vs the baseline receipt's per_query rows."""
    base_by_needle = {r["needle"]: r for r in base_rows}
    missing = [r["needle"] for r in arm_rows if r["needle"] not in base_by_needle]
    flips: Dict[str, List[str]] = {
        "delivered_gold_lost": [], "delivered_gold_gained": [],
        "recall_hit_lost": [], "recall_hit_gained": [],
        "final_recall_hit_lost": [], "final_recall_hit_gained": [],
    }
    rank_moves: List[Dict[str, Any]] = []
    for r in arm_rows:
        b = base_by_needle.get(r["needle"])
        if b is None:
            continue
        for metric, key in (("delivered_gold", "delivered_gold"),
                            ("recall_hit", "recall_hit"),
                            ("final_recall_hit", "final_recall_hit")):
            av, bv = int(r.get(key, 0)), int(b.get(key, 0))
            if av < bv:
                flips[f"{metric}_lost"].append(r["needle"])
            elif av > bv:
                flips[f"{metric}_gained"].append(r["needle"])
        ar, br = r.get("final_rank_of_first_gold"), b.get("final_rank_of_first_gold")
        if ar != br:
            rank_moves.append({
                "needle": r["needle"],
                "final_rank_baseline": br, "final_rank_arm": ar,
                "delivered_gold_baseline": b.get("delivered_gold"),
                "delivered_gold_arm": r.get("delivered_gold"),
                "abstain_arm": r.get("abstain"),
                "abstain_reason_arm": r.get("abstain_reason"),
            })
    return {
        "paired_needles": len(arm_rows) - len(missing),
        "unpaired_needles": missing,
        "flips": {name: {"count": len(needles), "needles": needles}
                  for name, needles in flips.items()},
        "final_rank_moves": rank_moves,
    }


def merge_chunks(
    chunk_paths: Sequence[str],
    out_path: str,
    stamp: str,
    baseline_path: Optional[str],
    baseline_arm: str,
) -> int:
    """Recombine chunked foreground runs into one receipt (+ baseline delta).

    Chunks are ``--offset``/``--limit`` slices of the same needle file, each
    paying its own untimed warmup. Identity fields must agree; per_query rows
    concatenate in the given order; summaries are recomputed over the union.
    bed_bytes is provenance, not identity — log_health appends a few KB per
    run outside the learn gate (PR #398 convention).
    """
    if not chunk_paths:
        print("no chunk receipts given", file=sys.stderr)
        return 2
    chunks = [json.loads(_resolve(p).read_text(encoding="utf-8")) for p in chunk_paths]
    base = chunks[0]
    identity = ("tool", "bed", "config", "git_sha", "k",
                "needles_file", "gold_file")
    for i, c in enumerate(chunks[1:], start=2):
        for key in identity:
            if c.get(key) != base.get(key):
                print(f"chunk {i} disagrees on {key}: "
                      f"{c.get(key)!r} != {base.get(key)!r}", file=sys.stderr)
                return 2
    merged: Dict[str, Any] = {
        "arm": ARM_NAME,
        "knobs_changed": dict(ARM_KNOBS),
        "gate": ARM_GATE,
        "sema_codec_is_none": all(c["arms"][0].get("sema_codec_is_none") for c in chunks),
        "warmup_ran": all(bool(c["arms"][0].get("warmup_ran")) for c in chunks),
        "signal_keys_seen": set(),
        "errors": [],
        "per_query": [],
    }
    seen: set = set()
    for c in chunks:
        arm = c["arms"][0]
        merged["signal_keys_seen"] |= set(arm.get("signal_keys_seen") or ())
        merged["errors"].extend(arm.get("errors") or ())
        for row in arm.get("per_query", []):
            if row["needle"] in seen:
                print(f"duplicate needle {row['needle']}", file=sys.stderr)
                return 2
            seen.add(row["needle"])
            merged["per_query"].append(row)
    merged["signal_keys_seen"] = sorted(merged["signal_keys_seen"])
    merged["sema_boost_signal_absent"] = "sema_boost" not in merged["signal_keys_seen"]
    k = int(base.get("k", 12))
    merged["summary"] = summarize(merged["per_query"], k)

    # Self-validation against the baseline receipt (the positive control this
    # tool deliberately does not re-run): sema_boost must be absent here and
    # present there, or the arm is unfalsifiable/ineffective.
    validation: Dict[str, Any] = {"kind": "signal_absent_vs_baseline_receipt"}
    baseline_delta: Optional[Dict[str, Any]] = None
    baseline_summary: Optional[Dict[str, Any]] = None
    if baseline_path:
        bl = json.loads(_resolve(baseline_path).read_text(encoding="utf-8"))
        bl_arm = next(a for a in bl["arms"] if a.get("arm") == baseline_arm)
        bl_signals = set((bl_arm.get("observables") or {}).get("signal_keys") or ())
        validation.update({
            "baseline_receipt": str(baseline_path),
            "baseline_arm": baseline_arm,
            "baseline_git_sha": bl.get("git_sha"),
            "baseline_bed": bl.get("bed"),
            "baseline_bed_bytes": bl.get("bed_bytes"),
            "sema_boost_present_at_baseline": "sema_boost" in bl_signals,
            "sema_boost_absent_in_arm": merged["sema_boost_signal_absent"],
            "arm_valid": ("sema_boost" in bl_signals) and merged["sema_boost_signal_absent"],
        })
        s = merged["summary"]
        baseline_summary = {
            "delivered_gold_queries": bl_arm.get("delivered_gold_queries"),
            "delivered_gold_rate": bl_arm.get("delivered_gold_rate"),
            "recall_at_k": bl_arm.get("recall_at_k"),
            "final_recall_at_k": bl_arm.get("final_recall_at_k"),
            "median_rank_of_gold": bl_arm.get("median_rank_of_gold"),
            "mean_rank_of_gold": bl_arm.get("mean_rank_of_gold"),
        }
        baseline_delta = {
            "delivered_gold_queries": s["delivered_gold_queries"] - int(bl_arm["delivered_gold_queries"]),
            "delivered_gold_rate": round(s["delivered_gold_rate"] - float(bl_arm["delivered_gold_rate"]), 6),
            "recall_at_k": round(s["recall_at_k"] - float(bl_arm["recall_at_k"]), 4),
            "final_recall_at_k": round(s["final_recall_at_k"] - float(bl_arm["final_recall_at_k"]), 4),
            "median_rank_of_gold": (
                (s["median_rank_of_gold"] - float(bl_arm["median_rank_of_gold"]))
                if s["median_rank_of_gold"] is not None and bl_arm.get("median_rank_of_gold") is not None
                else None
            ),
            "paired": _paired_delta(merged["per_query"], bl_arm.get("per_query") or [], k),
        }

    payload = {key: base.get(key) for key in (
        "tool", "issue", "bed", "config", "git_sha",
        "needles_file", "gold_file", "k", "env", "note",
    )}
    payload["stamp"] = stamp or base.get("stamp")
    payload["needle_count"] = len(merged["per_query"])
    payload["bed_bytes_before"] = chunks[0].get("bed_bytes")
    payload["bed_bytes_after"] = chunks[-1].get("bed_bytes_after", chunks[-1].get("bed_bytes"))
    payload["chunks"] = [
        {"path": str(p), "stamp": c.get("stamp"),
         "offset": c.get("offset"), "limit": c.get("limit"),
         "needle_count": c.get("needle_count"),
         "bed_bytes": c.get("bed_bytes"),
         "bed_bytes_after": c.get("bed_bytes_after")}
        for p, c in zip(chunk_paths, chunks)
    ]
    payload["validation"] = validation
    payload["baseline_summary"] = baseline_summary
    payload["baseline_delta"] = baseline_delta
    payload["arms"] = [merged]
    out = _resolve(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    s = merged["summary"]
    print(f"merged {len(chunk_paths)} chunks -> n={s['n_queries']} "
          f"delivered={s['delivered_gold_queries']} ({s['delivered_gold_rate']:.4f}) "
          f"r@{k}={s['recall_at_k']} fr@{k}={s['final_recall_at_k']} "
          f"abstain={s['abstain_count']} "
          f"sema_boost_absent={merged['sema_boost_signal_absent']}")
    if baseline_delta:
        print(f"delta vs {baseline_arm}: delivered {baseline_delta['delivered_gold_queries']:+d} "
              f"({baseline_delta['delivered_gold_rate']:+.4f}), "
              f"r@{k} {baseline_delta['recall_at_k']:+.4f}, "
              f"fr@{k} {baseline_delta['final_recall_at_k']:+.4f}")
    print(f"wrote {out}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="#371 deciding cell: no_sema read-gate arm (829k, delivered basis).",
    )
    ap.add_argument("--genome", default="f:/tmp/erb_carve/erb_829k_nosplade.db")
    ap.add_argument("--resolved", default="benchmarks/dogfood/erb/needles_resolved_blob.json")
    ap.add_argument("--gold", default="benchmarks/dogfood/erb/gold_by_needle_blob.json")
    ap.add_argument("--offset", type=int, default=0,
                    help="skip the first N needles — bounded FOREGROUND chunks "
                         "that --merge recombines")
    ap.add_argument("--limit", type=int, default=0, help="needles to run (0 = all)")
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--config", default="cymatix.toml")
    ap.add_argument("--out", default="benchmarks/dogfood/erb/receipts/sema_readgate_829k_n469.json")
    ap.add_argument("--stamp", default="")
    ap.add_argument("--merge", nargs="*", default=None,
                    help="merge chunk receipts (paths, in needle order) into "
                         "--out; runs no queries")
    ap.add_argument("--baseline", default="benchmarks/dogfood/erb/receipts/postflip_default_confirm_829k.json",
                    help="baseline receipt for the merge-time delta")
    ap.add_argument("--baseline-arm", default="no_pki",
                    help="arm in the baseline receipt that is the shipped-config world")
    args = ap.parse_args(argv)

    if args.merge is not None:
        return merge_chunks(args.merge, args.out, args.stamp,
                            args.baseline, args.baseline_arm)

    resolved_path = _resolve(args.resolved)
    gold_path = _resolve(args.gold)
    needles = json.loads(resolved_path.read_text(encoding="utf-8"))["needles"]
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

    bed_bytes_before = Path(genome_path).stat().st_size
    print(f"sema read-gate cell: {len(needles)} needles "
          f"(offset={args.offset}), k={args.k}, bed={genome_path}", flush=True)

    t0 = time.perf_counter()
    row = run_arm(
        genome_path=genome_path,
        config_path=config_path,
        needles=needles,
        gold_by_needle=gold_by_needle,
        k=args.k,
    )
    s = row["summary"]
    print(f"  {ARM_NAME}: n={s['n_queries']} delivered={s['delivered_gold_queries']} "
          f"r@{args.k}={s['recall_at_k']} fr@{args.k}={s['final_recall_at_k']} "
          f"abstain={s['abstain_count']} "
          f"sema_boost_absent={row['sema_boost_signal_absent']} "
          f"codec_none={row['sema_codec_is_none']} "
          f"p50={s['latency_ms']['p50']}ms "
          f"[{round((time.perf_counter()-t0)/60.0, 1)} min]", flush=True)

    payload = {
        "tool": "benchmarks/dogfood/erb/sema_readgate_cell.py",
        "issue": "#371 deciding cell (ESCALATE verdict 2026-08-19): sema read-gate, 829k, delivered basis",
        "stamp": args.stamp,
        "bed": genome_path,
        "bed_bytes": bed_bytes_before,
        "bed_bytes_after": Path(genome_path).stat().st_size,
        "config": config_path or "(auto-discovered)",
        "git_sha": _git_sha(REPO_ROOT),
        "needles_file": str(resolved_path),
        "gold_file": str(gold_path),
        "needle_count": len(needles),
        "offset": args.offset or None,
        "limit": args.limit or None,
        "k": args.k,
        "env": {
            "CYMATIX_ENCODER_URL": os.environ.get("CYMATIX_ENCODER_URL", "") or None,
            "CYMATIX_DISABLE_LEARN": os.environ.get("CYMATIX_DISABLE_LEARN"),
            "CYMATIX_GENOME_PATH": os.environ.get("CYMATIX_GENOME_PATH"),
            "CYMATIX_ABSTAIN_DISABLE": os.environ.get("CYMATIX_ABSTAIN_DISABLE"),
        },
        "note": (
            "Ladder methodology unchanged (fresh load_config + manager, "
            "untimed warmup, read_only=True, ignore_delivered=True, "
            "CYMATIX_DISABLE_LEARN=1, gene_id-ascending tie-break; ranks from "
            "the post-blend score map, delivered basis from "
            "window.expressed_gene_ids, final order from "
            "genome.last_ranked_ids cleared before every call) plus the "
            "know/abstain surface from know_abstain_replay.py (PR #398): "
            "abstain status/reason off the window, served know/miss verdict "
            "via server.helpers._compute_know_or_miss_block on the #350 "
            "request-scoped retrieval_scores snapshot. The baseline cell is "
            "NOT re-run: postflip_default_confirm_829k.json's no_pki arm is "
            "the shipped-config world on the same bed and needle set (#370 "
            "flipped PKI off after that receipt shipped). bed_bytes drift "
            "across chunks is the known log_health append (provenance, not "
            "identity). Latencies are wall ms on a shared box — within-run "
            "relatives only."
        ),
        "arms": [row],
    }
    out = _resolve(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""neutralize_ab.py — ``[budget] neutralize_control_tags`` A/B receipt (#351).

#351 decision 2: should ``neutralize_control_tags`` default true? The knob
escapes content-sourced ``<cymatix:`` to ``&lt;cymatix:`` INSIDE the assembly
loop (context_manager ``neutralize_on``) — i.e. strictly AFTER retrieval,
fusion, tie-break and rank publication — so retrieval/rank metrics are
unchangeable by construction. What the escape CAN touch is assembled bytes:
each occurrence grows the text by 3 chars, which in principle could shift a
budget trim and therefore the delivered set. This replay measures exactly
that: both arms over the same needles on the same bed, comparing the full
retrieval surface (per-needle score ranks, final-order ranks, delivered ids)
AND the assembled window bytes (sha256 + control-tag occurrence counts).

The flip gate (issue #351): zero delivered-gold movement — the per-needle
delivered basis must be identical across arms.

Conventions are the ladder's / know_abstain_replay's: fresh ``load_config``
+ fresh manager per arm, untimed warmup, ``read_only=True`` +
``ignore_delivered=True``, ``CYMATIX_DISABLE_LEARN=1``, tie-break by gene_id
ascending. The bed is additionally scanned (read-only SQL) for docs whose
content/complement contain the literal ``<cymatix:`` — if the bed carries
zero such docs (the erb beds are Wikipedia-derived and do), the
window-bytes comparison can only prove "no false positives"; the positive
case (the escape fires where a control tag DOES exist, and only there) is
carried by the synthetic unit tests in
``tests/test_control_tag_neutralization.py``, and the receipt says so.

Usage::

    python benchmarks/dogfood/erb/neutralize_ab.py \
        --stamp 20260819-neutralize-ab-100k
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
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
    percentile,
    rank_gold,
    recall_at_k,
    set_dotted,
)

CTRL = "<cymatix:"
CTRL_ESCAPED = "&lt;cymatix:"

# ── Arm table ────────────────────────────────────────────────────────────
# Both knob values are set EXPLICITLY (not "shipped default vs override")
# so the receipt stays self-describing even after a default flip.
ARMS: Dict[str, Dict[str, Any]] = {
    "neutralize_off": {"budget.neutralize_control_tags": False},
    "neutralize_on": {"budget.neutralize_control_tags": True},
}


def scan_bed_for_control_tags(genome_path: str) -> Dict[str, Any]:
    """Read-only SQL scan: docs whose stored text carries the literal tag."""
    con = sqlite3.connect(f"file:{genome_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        total = cur.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
        in_content = cur.execute(
            "SELECT COUNT(*) FROM genes WHERE content LIKE ?",
            (f"%{CTRL}%",),
        ).fetchone()[0]
        in_complement = cur.execute(
            "SELECT COUNT(*) FROM genes WHERE complement LIKE ?",
            (f"%{CTRL}%",),
        ).fetchone()[0]
        pre_escaped = cur.execute(
            "SELECT COUNT(*) FROM genes WHERE content LIKE ?",
            (f"%{CTRL_ESCAPED}%",),
        ).fetchone()[0]
    finally:
        con.close()
    return {
        "gene_count": total,
        "docs_with_control_tag_in_content": in_content,
        "docs_with_control_tag_in_complement": in_complement,
        "docs_with_pre_escaped_tag_in_content": pre_escaped,
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

    errors: List[str] = []
    rows: List[Dict[str, Any]] = []
    manager = CymatixContextManager(cfg)
    warmed = False
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
                rows.append({
                    "needle": nm, "error": detail,
                    "wall_ms": round(wall_ms, 3),
                    "recall_hit": 0, "delivered_gold": 0,
                    "delivered_count": 0,
                })
                continue
            wall_ms = (time.perf_counter() - t0) * 1000.0

            # #350 request-scoped snapshot; route-style fallback.
            _win_scores = getattr(window, "retrieval_scores", None)
            scores = dict(
                _win_scores if _win_scores is not None
                else (manager.genome.last_query_scores or {})
            )
            first, gold_ranks = rank_gold(scores, gold)
            delivered = list(getattr(window, "expressed_gene_ids", None) or ())
            dg = delivered_gold_fields(delivered, gold)
            ranked_ids = list(getattr(manager.genome, "last_ranked_ids", None) or ())
            final_rank = final_rank_of_first_gold(ranked_ids, gold)

            # Assembled-bytes surface: the whole point of this receipt.
            ec = window.expressed_context or ""
            rows.append({
                "needle": nm,
                "wall_ms": round(wall_ms, 3),
                "pool_size": len(scores),
                "rank_of_first_gold": first,
                "gold_ranks": gold_ranks,
                "recall_hit": 1 if (first is not None and first <= k) else 0,
                "final_rank_of_first_gold": final_rank,
                "final_recall_hit": 1 if (final_rank is not None and final_rank <= k) else 0,
                **dg,
                "delivered_ids": delivered,
                "window_sha256": hashlib.sha256(ec.encode("utf-8")).hexdigest(),
                "window_chars": len(ec),
                "ctrl_count": ec.count(CTRL),
                "ctrl_escaped_count": ec.count(CTRL_ESCAPED),
                "health_status": getattr(window.context_health, "status", None),
            })
    finally:
        try:
            manager.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"manager.close failed: {exc}")

    n = len(rows)
    summary = {
        "n_queries": n,
        "errors": sum(1 for r in rows if r.get("error")),
        "recall_at_k": recall_at_k([r.get("rank_of_first_gold") for r in rows], k),
        "final_recall_at_k": recall_at_k(
            [r.get("final_rank_of_first_gold") for r in rows], k),
        "delivered_gold_rate": round(
            sum(r.get("delivered_gold", 0) for r in rows) / n, 4) if n else 0.0,
        "windows_with_ctrl": sum(1 for r in rows if r.get("ctrl_count", 0) > 0),
        "windows_with_ctrl_escaped": sum(
            1 for r in rows if r.get("ctrl_escaped_count", 0) > 0),
        "latency_ms": {
            "p50": percentile([r["wall_ms"] for r in rows if "wall_ms" in r], 50),
            "p95": percentile([r["wall_ms"] for r in rows if "wall_ms" in r], 95),
        },
    }
    return {
        "arm": name,
        "knobs_changed": dict(knobs),
        "warmup_ran": warmed,
        "summary": summary,
        "errors": errors,
        "per_query": rows,
    }


def compare_arms(
    off_rows: Sequence[Mapping[str, Any]],
    on_rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Per-needle cross-arm identity check — the flip gate.

    Retrieval basis (ranks, final ranks, delivered ids, delivered gold)
    must be identical; window bytes must be identical EXCEPT where arm A
    (off) carries a literal control tag, in which case arm B must be
    exactly arm A with ``<cymatix:`` escaped. Note a window can carry the
    GENUINE no-match tag (parts-empty branch) — that branch has no doc
    parts to escape, so those windows must compare byte-identical too.
    """
    by_needle_on = {r["needle"]: r for r in on_rows}
    mismatches: List[Dict[str, Any]] = []
    rank_fields = (
        "rank_of_first_gold", "final_rank_of_first_gold", "recall_hit",
        "final_recall_hit", "delivered_gold", "delivered_count",
        "delivered_ids", "gold_ranks", "pool_size", "health_status",
    )
    windows_identical = 0
    windows_escaped = 0
    windows_diff_without_ctrl = 0
    for a in off_rows:
        b = by_needle_on.get(a["needle"])
        if b is None:
            mismatches.append({"needle": a["needle"], "field": "(missing in on-arm)"})
            continue
        for f in rank_fields:
            if a.get(f) != b.get(f):
                mismatches.append({
                    "needle": a["needle"], "field": f,
                    "off": a.get(f), "on": b.get(f),
                })
        if a.get("window_sha256") == b.get("window_sha256"):
            windows_identical += 1
        elif a.get("ctrl_count", 0) > 0:
            windows_escaped += 1
        else:
            windows_diff_without_ctrl += 1
            mismatches.append({
                "needle": a["needle"], "field": "window_sha256",
                "off": a.get("window_sha256"), "on": b.get("window_sha256"),
                "note": "bytes differ but off-arm window has no control tag",
            })
    return {
        "n_compared": len(off_rows),
        "rank_or_delivery_mismatches": [
            m for m in mismatches if m.get("field") != "window_sha256"],
        "windows_byte_identical": windows_identical,
        "windows_escaped_with_ctrl_present": windows_escaped,
        "windows_diff_without_ctrl": windows_diff_without_ctrl,
        "delivered_basis_identical": not any(
            m.get("field") in ("delivered_ids", "delivered_gold", "delivered_count")
            for m in mismatches
        ),
        "retrieval_basis_identical": not any(
            m.get("field") != "window_sha256" for m in mismatches
        ),
        "all_windows_byte_identical": windows_identical == len(off_rows),
        "mismatch_count": len(mismatches),
    }


def merge_chunks(chunk_paths: Sequence[str], out_path: str, stamp: str) -> int:
    """Recombine single-arm foreground chunks into the full A/B receipt.

    Chunking here is BY ARM (each ``--arms <name>`` run pays its own
    untimed warmup and stays inside a bounded foreground window); the
    merged receipt carries both arms plus the cross-arm ``comparison``
    block, byte-equivalent in shape to a single-shot run plus a
    ``chunks`` provenance block. Identity fields must agree across
    chunks. bed_bytes is provenance-only (health_log appends grow the
    bed a few KB per chunk — observability writes outside the learn
    gate, same caveat as know_abstain_replay's merge).
    """
    if not chunk_paths:
        print("no chunk receipts given", file=sys.stderr)
        return 2
    chunks = [json.loads(_resolve(p).read_text(encoding="utf-8")) for p in chunk_paths]
    base = chunks[0]
    identity = ("tool", "bed", "config", "git_sha", "k",
                "needles_file", "gold_file", "needle_count")
    for i, c in enumerate(chunks[1:], start=2):
        for key in identity:
            if c.get(key) != base.get(key):
                print(f"chunk {i} disagrees on {key}: "
                      f"{c.get(key)!r} != {base.get(key)!r}", file=sys.stderr)
                return 2
    arms_by_name: Dict[str, Dict[str, Any]] = {}
    for c in chunks:
        for arm in c.get("arms", []):
            if arm["arm"] in arms_by_name:
                print(f"duplicate arm {arm['arm']} across chunks", file=sys.stderr)
                return 2
            arms_by_name[arm["arm"]] = arm
    missing = [n for n in ARMS if n not in arms_by_name]
    if missing:
        print(f"merge lacks arms: {missing}", file=sys.stderr)
        return 2
    arms_out = [arms_by_name[n] for n in ARMS]
    comparison = compare_arms(arms_out[0]["per_query"], arms_out[1]["per_query"])
    print(json.dumps({k: v for k, v in comparison.items()
                      if k != "rank_or_delivery_mismatches"}, indent=2))
    payload = {key: base.get(key) for key in (
        "tool", "issue", "bed", "bed_bytes", "bed_control_tag_scan",
        "config", "git_sha", "needles_file", "gold_file", "needle_count",
        "k", "env", "note",
    )}
    payload["stamp"] = stamp or base.get("stamp")
    payload["chunks"] = [
        {"path": str(p), "stamp": c.get("stamp"),
         "arms": [a["arm"] for a in c.get("arms", [])],
         "bed_bytes": c.get("bed_bytes")}
        for p, c in zip(chunk_paths, chunks)
    ]
    payload["comparison"] = comparison
    payload["arms"] = arms_out
    out = _resolve(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="neutralize_control_tags A/B receipt (#351 decision 2).",
    )
    ap.add_argument("--genome", default="f:/tmp/erb_carve/erb_100k.db")
    ap.add_argument("--resolved", default="benchmarks/dogfood/erb/needles_resolved_carve.json")
    ap.add_argument("--gold", default="benchmarks/dogfood/erb/gold_by_needle_carve.json")
    ap.add_argument("--limit", type=int, default=0, help="needles to run (0 = all)")
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--arms", default="", help="comma list; empty = all")
    ap.add_argument("--config", default="cymatix.toml")
    ap.add_argument("--out", default="benchmarks/dogfood/erb/receipts/neutralize_ab_100k.json")
    ap.add_argument("--stamp", default="")
    ap.add_argument("--merge", nargs="*", default=None,
                    help="merge single-arm chunk receipts into --out; runs "
                         "no queries. Chunks must agree on bed / config / "
                         "k / git_sha / needle files.")
    args = ap.parse_args(argv)

    if args.merge is not None:
        return merge_chunks(args.merge, args.out, args.stamp)

    resolved_path = _resolve(args.resolved)
    gold_path = _resolve(args.gold)
    needles = json.loads(resolved_path.read_text(encoding="utf-8"))["needles"]
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
    selected = {n: k_ for n, k_ in ARMS.items() if not wanted or n in wanted}

    bed_scan = scan_bed_for_control_tags(genome_path)
    print(f"neutralize A/B: {len(selected)} arms x {len(needles)} needles, "
          f"k={args.k}, bed={genome_path}", flush=True)
    print(f"bed scan: {bed_scan}", flush=True)

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
            f"  {name:16s} r@{args.k}={s['recall_at_k']:.4f} "
            f"fr@{args.k}={s['final_recall_at_k']:.4f} "
            f"dgr={s['delivered_gold_rate']:.4f} "
            f"ctrl_windows={s['windows_with_ctrl']} "
            f"escaped_windows={s['windows_with_ctrl_escaped']} "
            f"p50={s['latency_ms']['p50']}ms "
            f"[{round((time.perf_counter()-t0)/60.0, 1)} min]",
            flush=True,
        )

    comparison: Optional[Dict[str, Any]] = None
    if (len(arm_rows) == 2
            and not arm_rows[0].get("skipped")
            and not arm_rows[1].get("skipped")):
        comparison = compare_arms(arm_rows[0]["per_query"], arm_rows[1]["per_query"])
        print(f"comparison: {json.dumps({k: v for k, v in comparison.items() if k != 'rank_or_delivery_mismatches'}, indent=2)}",
              flush=True)

    payload = {
        "tool": "benchmarks/dogfood/erb/neutralize_ab.py",
        "issue": "#351 decision 2: neutralize_control_tags default flip gate",
        "stamp": args.stamp,
        "bed": genome_path,
        "bed_bytes": Path(genome_path).stat().st_size,
        "bed_control_tag_scan": bed_scan,
        "config": config_path or "(auto-discovered)",
        "git_sha": _git_sha(REPO_ROOT),
        "needles_file": str(resolved_path),
        "gold_file": str(gold_path),
        "needle_count": len(needles),
        "k": args.k,
        "env": {
            "CYMATIX_ENCODER_URL": os.environ.get("CYMATIX_ENCODER_URL", "") or None,
            "CYMATIX_DISABLE_LEARN": os.environ.get("CYMATIX_DISABLE_LEARN"),
            "CYMATIX_GENOME_PATH": os.environ.get("CYMATIX_GENOME_PATH"),
            "CYMATIX_ABSTAIN_DISABLE": os.environ.get("CYMATIX_ABSTAIN_DISABLE"),
        },
        "note": (
            "The escape runs at ASSEMBLY time (context_manager neutralize_on, "
            "after retrieval/fusion/tie-break/rank publication), so retrieval "
            "metrics are unchangeable by construction; the measurable surface "
            "is assembled bytes (a +3-char growth per occurrence could in "
            "principle shift a budget trim). One build_context per needle "
            "(read_only=True, ignore_delivered=True, max_genes=k) per arm; "
            "windows compared by sha256. If the bed carries zero control-tag "
            "docs (see bed_control_tag_scan), the positive case — the escape "
            "fires where a tag exists, and only there — is carried by the "
            "synthetic assembly-function unit tests in "
            "tests/test_control_tag_neutralization.py; this receipt then "
            "proves the null: no false positives, byte-identical windows and "
            "identical delivered basis across arms. Untimed warmup per arm; "
            "latencies are wall ms on a shared box, within-run relatives only."
        ),
        "comparison": comparison,
        "arms": arm_rows,
    }

    out = _resolve(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    if comparison is None and not wanted:
        return 2  # both arms requested but comparison could not run
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

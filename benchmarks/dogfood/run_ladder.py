"""run_ladder.py — investigation ladder over the dogfood bed.

The 2026-07-28 baseline established the question this answers. At the shipped
budget recall@12 = 0.667; at k=50 it is **1.000**. Gold is in the pool and
scored — it is simply ranked too deep to be delivered. Six of eight
cymatix-context needles land at ranks 23-41 while ten of twelve needles about
the other three repos land in the top 3.

So the open question is *which knob moves rank*, and the honest first answer is
that some plausible ones cannot:

* **Candidate-admission depth** (``fts5_candidate_depth``) widens the raw pool
  fed into scoring. On the collaborator's 850K bed that was the whole story —
  gold was never admitted. Here gold is *already* admitted and scored, so depth
  should be **null**. Measuring it is the point: a null result separates this
  bed's regime (ranking) from cell-5's (admission), and stops us importing
  their prescription without evidence.
* **Budget** (``max_genes``) trivially buys delivery. Worth curving so the cost
  of "just raise it" is on the record rather than assumed.
* **Fusion and rerank** actually reorder. These are where movement should come
  from if the crowding hypothesis is right.

Every arm reports per-needle rank, so a knob that helps cymatix needles while
hurting the others shows up as such instead of averaging out.

Arms are run in one process against one manager per config, and the bed is
opened read-only throughout.

Usage::

    python benchmarks/dogfood/run_ladder.py                  # full ladder
    python benchmarks/dogfood/run_ladder.py --axes depth,budget
    python benchmarks/dogfood/run_ladder.py --k 50 --out …
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

CYMATIX_PREFIX = "cymatix_"


def _arms(axes: set[str]) -> list[dict]:
    """Each arm is (name, axis, overrides) — overrides are dotted config paths."""
    out: list[dict] = []
    if "baseline" in axes:
        out.append({"name": "baseline", "axis": "baseline", "overrides": {}})
    if "depth" in axes:
        # 0 = auto (max_genes*4). The cell-5 ladder was 50/200/1000.
        for d in (50, 200, 1000):
            out.append({"name": f"fts_depth={d}", "axis": "depth",
                        "overrides": {"retrieval.fts5_candidate_depth": d}})
    if "fusion" in axes:
        out.append({"name": "fusion=additive", "axis": "fusion",
                    "overrides": {"retrieval.fusion_mode": "additive"}})
    if "rerank" in axes:
        # config.py:582 — additive | fused_tier | eps_band | off
        for combi in ("fused_tier", "eps_band", "off"):
            out.append({"name": f"rerank={combi}", "axis": "rerank",
                        "overrides": {"retrieval.rerank_combinator": combi}})
    if "dense" in axes:
        for w in (0.0, 8.0, 16.0):
            out.append({"name": f"dense_w={w}", "axis": "dense",
                        "overrides": {"retrieval.dense_additive_weight": w}})
    if "densecross" in axes:
        # dense_additive_weight is, by its name, an ADDITIVE-fusion term. Under
        # the shipped rrf default the dense leg contributes by rank, so the
        # weight should be inert — and the rrf arm above measures exactly that.
        # This crossed arm is the control: if the weight moves anything under
        # additive but nothing under rrf, the knob is fusion-conditional, and
        # #203's additive-era sweep does not transfer to the rrf default.
        for w in (0.0, 4.0, 16.0):
            out.append({"name": f"additive+dense_w={w}", "axis": "densecross",
                        "overrides": {"retrieval.fusion_mode": "additive",
                                      "retrieval.dense_additive_weight": w}})
    return out


def _set_dotted(cfg, path: str, value) -> bool:
    section, _, key = path.partition(".")
    obj = getattr(cfg, section, None)
    if obj is None or not hasattr(obj, key):
        return False
    setattr(obj, key, value)
    return True


def _rollup(ranks: dict[str, int | None], k: int) -> dict:
    vals = [r for r in ranks.values() if r]
    n = len(ranks) or 1
    cy = {name: r for name, r in ranks.items() if name.startswith(CYMATIX_PREFIX)}
    other = {name: r for name, r in ranks.items() if not name.startswith(CYMATIX_PREFIX)}

    def rec(subset: dict, kk: int) -> float:
        if not subset:
            return 0.0
        return round(sum(1 for r in subset.values() if r and r <= kk) / len(subset), 4)

    return {
        f"recall@{k}": rec(ranks, k),
        "recall@1": rec(ranks, 1),
        "recall@5": rec(ranks, 5),
        "recall@12": rec(ranks, 12),
        "mrr": round(sum(1 / r for r in vals) / n, 4) if vals else 0.0,
        "median_rank": statistics.median(vals) if vals else None,
        "cymatix_recall@12": rec(cy, 12),
        "cymatix_median_rank": statistics.median(
            [r for r in cy.values() if r]) if any(cy.values()) else None,
        "other_recall@12": rec(other, 12),
        "other_median_rank": statistics.median(
            [r for r in other.values() if r]) if any(other.values()) else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--genome", default="genomes/dogfood/genome.db")
    ap.add_argument("--gold", default="benchmarks/dogfood/gene_ids/gold_by_needle.json")
    ap.add_argument("--resolved", default="benchmarks/dogfood/needles_resolved.json")
    ap.add_argument("--k", type=int, default=50,
                    help="scored depth to record ranks within (default 50)")
    ap.add_argument("--axes", default="baseline,depth,fusion,rerank,dense")
    ap.add_argument("--out", default="benchmarks/dogfood/ladder.json")
    args = ap.parse_args()

    gold_by_needle = {k: set(v) for k, v in json.loads(
        (REPO_ROOT / args.gold).read_text(encoding="utf-8")).items()}
    resolved = json.loads((REPO_ROOT / args.resolved).read_text(encoding="utf-8"))
    needles = resolved["needles"]

    genome = str(REPO_ROOT / args.genome)
    os.environ["CYMATIX_GENOME_PATH"] = genome
    os.environ["CYMATIX_DISABLE_LEARN"] = "1"

    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager

    arms = _arms(set(a.strip() for a in args.axes.split(",") if a.strip()))
    print(f"ladder: {len(arms)} arms x {len(needles)} needles, k={args.k}\n")

    results: list[dict] = []
    for arm in arms:
        cfg = load_config()
        cfg.genome.path = genome
        unapplied = [p for p, v in arm["overrides"].items() if not _set_dotted(cfg, p, v)]
        if unapplied:
            # A knob that silently does not exist would otherwise be reported
            # as a null result, which is the worst possible outcome: a real
            # lever dismissed because the harness never set it.
            print(f"  SKIP {arm['name']}: no such config field(s): {unapplied}",
                  file=sys.stderr)
            results.append({**arm, "skipped": True, "reason": f"unknown: {unapplied}"})
            continue

        manager = CymatixContextManager(cfg)
        ranks: dict[str, int | None] = {}
        t0 = time.time()
        for nd in needles:
            gold = gold_by_needle.get(nd["name"], set())
            try:
                manager.build_context(nd["query"], read_only=True,
                                      ignore_delivered=True, max_genes=args.k)
                raw = manager.genome.last_query_scores or {}
                ordered = [g for g, _ in sorted(raw.items(),
                                                key=lambda kv: kv[1], reverse=True)][:args.k]
                ranks[nd["name"]] = next(
                    (i for i, g in enumerate(ordered, 1) if g in gold), None)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {arm['name']}/{nd['name']}: {exc}", file=sys.stderr)
                ranks[nd["name"]] = None
        dt = time.time() - t0
        roll = _rollup(ranks, args.k)
        results.append({**arm, "ranks": ranks, "rollup": roll, "seconds": round(dt, 1)})

        print(f"  {arm['name']:22s} r@12={roll['recall@12']:.3f} "
              f"(cy {roll['cymatix_recall@12']:.3f} / other {roll['other_recall@12']:.3f})  "
              f"MRR={roll['mrr']:.3f}  cy_med_rank={roll['cymatix_median_rank']}  "
              f"{dt:.0f}s", flush=True)

    payload = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "genome": genome,
        "k": args.k,
        "needles": len(needles),
        "note": "retrieval only; gene_id set membership; ranks within top-k scored",
        "arms": results,
    }
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

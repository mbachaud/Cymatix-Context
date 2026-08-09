"""ablate_cymatics.py — isolate the cymatics stage against its own named controls.

The README flags cymatics as experimental in specific, falsifiable terms:

    a candidate-reordering signal that has **not yet been isolated against
    hashed bag-of-words or random-bin controls** — treat it as an
    experimental cheap feature, not a proven one

This runs exactly those controls. What cymatics does: hash each term (MD5) into
one of 256 bins, spread it with a small Gaussian, superpose, compare query and
candidate spectra by cosine, and use the result to reorder candidates.

Three things could be carrying the signal, and they are separable:

* **A — shipped.** MD5 bins, Gaussian spread (``peak_width`` 3.0).
* **B — off.** Does the stage contribute anything at all? If A == B the
  feature is inert and the flag is moot.
* **C — hashed bag-of-words.** Same MD5 bins, spread collapsed to a single
  bin. If A == C, the *spectrum* framing adds nothing over hashed BoW: the
  Gaussian is decoration and the honest description is "hashed BoW cosine".
* **D — random bins.** Bin assignment re-seeded, so placement is arbitrary but
  still consistent between query and document. **A == D is the expected and
  correct result** — hash placement is not meaningful, so any consistent
  mapping should do equally well. A ≫ D would mean MD5's specific layout
  carries information, which would be surprising and would need explaining.
  D is run over several seeds because a single re-seed could differ by chance,
  and without the spread we would be reading noise as signal.

The decision rule, fixed before running:

* A ≈ B  → inert. The flag stays, or the stage goes.
* A ≈ C  → real but it is hashed BoW. The flag can clear only if the README
  stops implying the spectrum is doing work.
* A > C, A ≈ D (within seed variance) → the spread is doing real work and
  placement is arbitrary as designed. **Flag clears.**
* A > D beyond seed variance → unexplained; investigate before claiming
  anything.

Usage::

    python benchmarks/dogfood/ablate_cymatics.py
    python benchmarks/dogfood/ablate_cymatics.py --seeds 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _rank_all(manager, needles, gold, k):
    out = {}
    for nd in needles:
        manager.build_context(nd["query"], read_only=True,
                              ignore_delivered=True, max_genes=k)
        raw = manager.genome.last_query_scores or {}
        ordered = [g for g, _ in sorted(raw.items(), key=lambda kv: kv[1],
                                        reverse=True)][:k]
        G = gold.get(nd["name"], set())
        out[nd["name"]] = next((i for i, g in enumerate(ordered, 1) if g in G), None)
    return out


def _rollup(ranks, k):
    n = len(ranks) or 1
    vals = [r for r in ranks.values() if r]
    rec = lambda kk: round(sum(1 for r in ranks.values() if r and r <= kk) / n, 4)  # noqa: E731
    return {
        "recall@1": rec(1), "recall@5": rec(5), "recall@12": rec(12),
        f"recall@{k}": rec(k),
        "mrr": round(sum(1 / r for r in vals) / n, 4) if vals else 0.0,
        "median_rank": statistics.median(vals) if vals else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--genome", default="genomes/dogfood/genome.db")
    ap.add_argument("--k", type=int, default=50)
    ap.add_argument("--seeds", type=int, default=3,
                    help="random-bin control seeds (variance estimate)")
    ap.add_argument("--out", default="benchmarks/dogfood/cymatics_ablation.json")
    args = ap.parse_args()

    genome = str(REPO_ROOT / args.genome)
    os.environ["CYMATIX_GENOME_PATH"] = genome
    os.environ["CYMATIX_DISABLE_LEARN"] = "1"

    needles = json.loads((REPO_ROOT / "benchmarks/dogfood/needles_resolved.json")
                         .read_text(encoding="utf-8"))["needles"]
    gold = {k: set(v) for k, v in json.loads(
        (REPO_ROOT / "benchmarks/dogfood/gene_ids/gold_by_needle.json")
        .read_text(encoding="utf-8")).items()}

    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager
    from cymatix_context.scoring import cymatics as cy

    orig_term_to_frequency = cy.term_to_frequency

    def run(label, *, enabled=True, peak_width=None, seed=None):
        cy.clear_spectrum_cache()
        if seed is None:
            cy.term_to_frequency = orig_term_to_frequency
        else:
            def reseeded(term, _s=seed):
                h = hashlib.md5(f"seed{_s}:".encode() + term.lower().encode("utf-8")).digest()
                return int.from_bytes(h[:2], "little") % cy.N_BINS
            cy.term_to_frequency = reseeded
        cfg = load_config()
        cfg.genome.path = genome
        cfg.cymatics.enabled = enabled
        if peak_width is not None:
            # SILENTLY INERT (2026-08-08 audit): cfg.cymatics.peak_width is
            # parsed but never read — the runtime derives its peak width from
            # [budget] splice_aggressiveness (context_manager.py:1228,
            # aggressiveness_to_peak_width). Arm C therefore ran identical to
            # arm A; its numbers do NOT compare peak_width variants.
            cfg.cymatics.peak_width = peak_width
        m = CymatixContextManager(cfg)
        t0 = time.time()
        ranks = _rank_all(m, needles, gold, args.k)
        dt = time.time() - t0
        roll = _rollup(ranks, args.k)
        print(f"  {label:34s} r@12={roll['recall@12']:.3f}  MRR={roll['mrr']:.3f}  "
              f"med={roll['median_rank']}  {dt:.0f}s", flush=True)
        return {"label": label, "ranks": ranks, "rollup": roll,
                "seconds": round(dt, 1)}

    print(f"cymatics ablation — {len(needles)} needles, k={args.k}\n")
    arms = []
    arms.append(run("A shipped (md5 bins, spread 3.0)"))
    arms.append(run("B cymatics OFF", enabled=False))
    arms.append(run("C hashed bag-of-words (no spread)", peak_width=0.01))
    for s in range(args.seeds):
        arms.append(run(f"D random-bin control seed={s}", seed=s))

    cy.term_to_frequency = orig_term_to_frequency
    cy.clear_spectrum_cache()

    by = {a["label"]: a for a in arms}
    A = arms[0]["rollup"]
    D = [a["rollup"] for a in arms if a["label"].startswith("D ")]
    d_r12 = [d["recall@12"] for d in D]
    d_mrr = [d["mrr"] for d in D]

    verdict = {
        "A_recall@12": A["recall@12"], "A_mrr": A["mrr"],
        "B_off_recall@12": by["B cymatics OFF"]["rollup"]["recall@12"],
        "C_bow_recall@12": by["C hashed bag-of-words (no spread)"]["rollup"]["recall@12"],
        "D_random_recall@12_mean": round(sum(d_r12) / len(d_r12), 4),
        "D_random_recall@12_spread": [min(d_r12), max(d_r12)],
        "D_random_mrr_mean": round(sum(d_mrr) / len(d_mrr), 4),
        "D_random_mrr_spread": [min(d_mrr), max(d_mrr)],
    }

    payload = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "genome": genome, "k": args.k, "needles": len(needles),
        "controls": "README-named: hashed bag-of-words + random-bin",
        "arms": arms, "verdict_inputs": verdict,
    }
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print("\n" + "-" * 68)
    for k, v in verdict.items():
        print(f"  {k:28s} {v}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

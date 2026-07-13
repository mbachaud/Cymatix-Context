# B1 operating-point repair — validated, and why it couples to answer-absence (#239)

**Date:** 2026-07-08
**Branch:** research/faithfulness-semantic-reach
**Status:** decided — **the b1 sign inversion is a real, provable defect (ship the monotonicity guard); the beta *operating-point* re-ship is NOT safe on the current beds — it couples to answer-absence (B3) and must wait on the scale-N delivery-balanced bench.**
**Parent:** [`2026-07-07-faithfulness-circuit-tracer.md`](2026-07-07-faithfulness-circuit-tracer.md) §3 (the operating-point-repair proposal) and [`2026-07-07-answer-presence-spike-239.md`](2026-07-07-answer-presence-spike-239.md) (which declared "B1 stands on its own — ship it").

---

## TL;DR

The roadmap treated B1 ("un-invert β1, lower floor / raise intercept so the gate can fire") as an **independent, shovel-ready** win, justified on the §3 bed. That bed is 94% causal-positive with **zero answer-absent negatives**, so it could not test the one thing that matters for a *precision* claim: does the repaired gate fire when the answer is **not** in the corpus?

This note runs B1's operating points against the §4 **delivery-balanced** bed (30 real answer-absent "heldout" needles) for the first time. Two findings:

1. **The β1 inversion is a genuine, provable defect — ship the fix for the *shape*.** Shipped `b1 = −1.1442` on `tanh(top_score/s_ref)` means *better retrieval lowers confidence*. Every know-feature is oriented so "higher = stronger evidence," so every feature coefficient must be `≥ 0`. This is a monotonicity invariant, and the shipped vector violates it. Un-inverting rescues recall from **0/45 → 45/45** on delivered-and-used facts (§3) and lifts the confidence ceiling from 0.44 to 0.82.

2. **But the *operating point* (which betas + floor to ship) cannot be set on these beds — it couples to answer-absence.** With the corrected sign, the gate fires on **80% of answer-absent (heldout) queries** at floor 0.45, because `top_score` and `score_gap` are **statistically identical** for answer-present and answer-absent deliveries (medians 4.79 vs 4.72, and 0.06 vs 0.03). The only feature that separates them — `coordinate_confidence` (0.56 vs 0.29) — overlaps far too much to gate cleanly, and betting heavily on it (a coord-weighted refit) overfits the n=30/cell construction — the exact "recalibrate the 5 features" trap §4 already refuted.

**Conclusion:** B1 is **not** independent of B3. You cannot restore recall using the retrieval-strength features without also firing on answer-absent queries; suppressing those needs the answer-absence signal (B3 / delivery-coherence), which is on hold pending scale-N. **Ship now:** the monotonicity guard (a checker + load-time warning + regression tests) so an inverted vector can never silently ship again. **Defer:** the beta/floor re-ship to the scale-N delivery-balanced recalibration, jointly with B3.

---

## Method

Offline, read-only. `scratchpad/b1_analysis.py` recomputes `compute_confidence`
(`helix_context/scoring/know_calibration.py:267-280`) under candidate
`(betas, s_ref, g_ref, emit_floor)` and evaluates each against two beds:

- **§3 bed** — `needles_239_stage1.json` + `_faith.json` (48 needles, Redwood Inference graded-distractor; 45/48 causal-used, only 3 non-causal). Used to measure **recall on delivered-and-used**.
- **§4 bed** — `needles_239b_stage1.json` + `_faith.json` (72 needles, delivery-balanced: 30 answerable, 30 heldout=answer-absent, 12 competition). Used to measure **false-fire on answer-absent (heldout)** and behavior on the ambiguous competition cell.

Recompute fidelity confirmed: recomputed confidence matches the stored
`raw_confidence` to **max |err| 6e-5** across all 120 rows under the shipped
`[know]` params (residual = 4-dp feature rounding in the dumps). Join key: `id`.

Candidate operating points swept: shipped baseline; un-invert b1 only (A/C);
un-invert + intercept bump (B); a delivery-refit against answerable-vs-heldout
(D); revert-to-`DEFAULT_BETAS` (E); un-invert + coord→1.8 (F). Floors 0.45–0.70.

---

## Results

### The structural zero-recall, and what un-inverting b1 does

| operating point | conf ceiling (§3 / §4) | §3 recall (causal, n=45) | §4 heldout false-fire (answer-absent, n=30) | §4 competition fire@used(5) / fire@ignored(7) |
|---|---|---:|---:|---:|
| **SHIPPED** (b1=−1.14, floor 0.45) | 0.44 / 0.42 | **0.00** | 0.00 | 0.00 / 0.00 |
| A: un-invert b1, floor 0.45 | 0.82 / 0.82 | **1.00** | **0.80** | 1.00 / 1.00 |
| C: un-invert b1, floor 0.55 | 0.82 / 0.82 | 0.71 | 0.77 | 1.00 / 0.86 |
| E: `DEFAULT_BETAS`, floor 0.6 | 0.99 / 0.98 | 1.00 | **1.00** | 1.00 / 1.00 |
| F: un-invert b1 + coord→1.8, floor 0.6 | 0.93 / 0.92 | 1.00 | 0.73 | 1.00 / 1.00 |
| D: coord-weighted refit, floor 0.5 | 0.97 / 0.96 | 1.00 | 0.40 | 0.80 / 0.86 |
| D: coord-weighted refit, floor 0.7 | 0.97 / 0.96 | 0.96 | 0.13 | 0.60 / 0.43 |

Reading it: **every point that restores meaningful recall also fires on ≥40% of
answer-absent queries.** Only the coord-weighted refit D bounds heldout below
0.4, and only by pushing the floor so high that answerable recall collapses to
0.40 and competition-used-gold recall drops to 0.60 — while overfitting n=30/cell.

### Why: the retrieval-strength features can't tell present from absent

Per-cell feature medians on the §4 bed:

| feature | answerable (gold present) | heldout (gold **absent**) | separates? |
|---|---:|---:|---|
| `top_score` | 4.79 | 4.72 | **no** — identical |
| `score_gap` | 0.06 | 0.03 | **no** — both ~0 |
| `coordinate_confidence` | 0.56 | 0.29 | weakly, heavy overlap |

`coord` is the only signal, and it overlaps badly: **11 of 30** answerable rows
have `coord ≤ 0.33`, while **18 of 30** heldout rows have `coord ≥ 0.17`. Any
`coord` threshold therefore both misses real deliveries and fires on absent ones.
This is the same mechanism §4 found for causal-use discrimination, now shown to
also bound the *operating-point repair*: `top_score`-driven recall is blind to
whether the high-scoring delivery is the gold or a plausible distractor.

---

## Interpretation for #239

- **The β1 sign is a defect, independent of the operating point.** "Better
  retrieval lowers confidence" is never correct; the #249 fit produced it by
  training against a retrieval-top1 label that mislabels rank-2 golds as negative
  (§3). Fixing the *shape* (all feature coefficients `≥ 0`) is unambiguous and is
  now guarded (see Shipped below).

- **The operating point is not separable from B3.** The roadmap's "B1 stands on
  its own" was justified on the §3 bed, which has no answer-absent negatives. On
  the §4 bed, restoring recall via the retrieval-strength features necessarily
  fires on answer-absent queries. So a repaired-and-broadened gate that an agent
  can trust requires the answer-absence signal (B3 / delivery-coherence). B1 and
  B3 must ship **together**, on a **scale-N delivery-balanced bench** — not the
  n=12/30 adversarial cells here, which cannot distinguish a weak real coord
  signal from noise (this is the §6 scale-N path the spike already prescribed).

- **A gate that fires with poor answer-absent precision is arguably worse than
  one that never fires.** The current shipped posture (precision 0.826 @ coverage
  1.4%, "rarely fires") is a deliberate precision-first choice. Broadening it
  before B3 exists would trade "useless-but-honest" for "fires-but-misleads."
  That trade — the *meaning* of the know contract — is a product decision, and it
  should be made against realistic (not adversarial-heldout) data.

---

## Shipped in this pass (the safe, provable increment)

`helix_context/scoring/know_calibration.py`:
- `FEATURE_NAMES` and `monotonicity_violations(betas)` — returns the feature
  names whose coefficient is negative (empty = monotone). The intercept is
  unconstrained; short (freshness-less) vectors are tolerated.
- `calibration_from_config` now emits a **load-time WARNING** naming any
  non-monotonic feature and pointing at #239 B1. This surfaces the currently-
  shipped inversion (silent until now) in every server that loads `helix.toml`,
  and guards against a future re-fit silently re-shipping one.
- Tests: `tests/test_know_monotonicity.py` (7 cases) pin the checker and the
  warning behavior on synthetic vectors (robust to any future beta re-ship).

**Deliberately NOT shipped:** any change to the `[know]` betas / `emit_floor`.
No validated operating point exists on the current beds; shipping a beta vector
fit to n≤30/cell adversarial cells would repeat the #249 overfit mistake.

---

## Recommendation / next

1. **Merge the monotonicity guard** (this pass). It is correctness-only and
   coverage-neutral (no betas changed → no live gate behavior change).
2. **Do the beta re-fit as part of scale-N + B3**, with a **monotone-constrained**
   fit (enforce all feature coefficients `≥ 0` — the current
   `scripts/calibrate_know_confidence.py` uses unconstrained sklearn
   `LogisticRegression`, which is how the inversion shipped). The guard added here
   should become a hard check in the calibrate script at that point.
3. **The operating-point / coverage decision is the user's** — precision-first
   (status quo, "know = never wrong, rarely fires") vs recall-first ("know =
   confident retrieval, fires on strong deliveries incl. some distractors").
   Decide it against realistic traffic, not adversarial heldout cells.
4. **B3 / delivery-coherence is now on the critical path for a *trustworthy*
   gate**, not a nice-to-have. The NLI-restore work (below) is its enabling lever.

## Reproduce

```
scratchpad/b1_analysis.py    # graph-free; native python -X utf8; reads the np-graph beds
```

Inputs: `F:/Projects/np-graph/needles_239{,b}_stage1.json` + `_faith.json`.

## Limitations

- **n = 30 heldout / 12 competition** — the CIs are wide; the heldout cell is
  *adversarially constructed* (high-scoring same-family distractors, gold
  removed), so 0.80 false-fire is a worst-case, not a production rate. This is
  exactly why the operating-point decision needs scale-N realistic data.
- Betas swept are illustrative; candidate D is in-sample, no train/test split —
  shown to demonstrate the coord ceiling, not proposed for ship.
- `causal_use` labels are model-relative (Qwen3-4B instrument), inherited from
  the parent studies.

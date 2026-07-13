# Know-logistic RRF-era re-fit — gate pass, but fragile (#239, 2026-07-13)

**What this is.** The gated re-fit of the KnowBlock confidence logistic
under CURRENT master defaults (`fusion_mode = "rrf"` since #247,
`blend_mode = "legacy"`), run through the #268 AUC ≥ 0.7 refuse-gate
with **no `--force`**. It exists so the `blend_mode` graduation
(#255 / audit §4 item 5) has a co-landable `[know]` re-fit — the
2026-07-08 scoring-invariance audit (item 7) sequences the s_ref/g_ref
re-fit behind that graduation, and the graduation was blocked partly
because no gated re-fit existed.

**Headline: the gate PASSED on its pre-registered protocol
(held-out AUC 0.7101 ≥ 0.70), but the pass is not split-robust and the
fit should NOT be trusted as evidence the features separate under RRF.**
This doc is the honest record of both facts.

## Data

One `located_n1000` run (Stage-1 located-axis queries, seed 42,
`build_context(read_only=True)` + `HELIX_DISABLE_LEARN=1`, bed
read-only):

| set | bed | config | rows | r@1 |
| --- | --- | --- | --- | --- |
| n300 | xl | shipped stack (dense on) + rrf + blend legacy | 300 | 0.210 |

The run was launched at `--n 500` and killed at the 300-row flush
checkpoint (~58 min in); the 300 rows on disk are complete and valid
(base rate 0.21, matching the #249 xl_clean run's 0.223). n=300 is the
floor of the task's stated n=300–500 fallback band. Single bed — not
the xl_clean + erb50k union #249 fit on.

```bash
python benchmarks/located_n1000.py \
  --bed-db genomes/bench/matrix/xl.db \
  --base-config helix.toml --n 500 --seed 42 --axis located \
  --set retrieval.fusion_mode=rrf \
  --out benchmarks/results/located_n300_xl_rrf.jsonl
```

## Fit + gate (sklearn logistic, 80/20 split, seed 42, floor 0.70)

```
python scripts/calibrate_know_confidence.py \
  --input benchmarks/results/located_n300_xl_rrf.jsonl --out helix.toml
# held-out hit/miss AUC: 0.7101 (floor=0.70) -> AUC gate PASSED -> wrote [know]
```

```toml
[know]
emit_floor      = 0.271    # precision sweep on the 60-row test split
s_ref           = 4.1294   # median rrf-scale top_score (was 4.2503)
g_ref           = 0.2936   # (was 0.4386)
betas           = [-1.4991, -0.2936, 0.0901, 0.3755, 0.0081, 0.271]
calibrated_at   = "2026-07-13T21:01:17+00:00"
calibrated_on_n = 300
```

β1 (top_score) fits **negative again** (−0.29; was −1.14 in #249) —
the anti-signal this issue reported persists under current-master RRF.
β4 (coordinate_confidence) is **dead** (+0.008).

## Why the pass is fragile

All numbers in
`docs/benchmarks/data/2026-07-13-know-refit-gate-robustness.json`;
reproduction is the same script protocol with only the split seed varied.

| probe | result |
| --- | --- |
| gate AUC (seed 42, the pre-registered default) | **0.7101 — passes** |
| split seeds 41 / 43 / 44 / 45 (same protocol) | 0.611 / 0.558 / **0.363** / 0.623 |
| bootstrap 95% CI on the seed-42 held-out AUC (B=10k) | **[0.519, 0.883]** |
| full-set AUC of the new fit (mostly in-sample: 240/300 trained-on) | 0.5998 |
| full-set AUC of the shipped #249 fit on the same rows | 0.5864 |
| full-set precision @ new emit_floor 0.271 (emit rate 2.7%) | **0.50** |

Per-feature full-set AUCs: top_score 0.490 (inverted — hit mean 4.043 <
miss mean 4.063), score_gap 0.530, lexical_dense_agree 0.559,
coordinate_confidence 0.527, freshness_min 0.515. No feature clears
0.56 alone; a logistic over five ~chance features has no robust 0.7 in
it at this n. The seed-42 pass is the single-split variance a 60-row
holdout (12 positives) allows — the same shape as the `nli_q_span`
0.743 "n=12 artifact" already documented on #239.

Before/after calibration quality on the full 300 rows
(`docs/benchmarks/data/eval_located_n300_xl_full_rrf.json`):

| calibration | AUC | ECE (10-bin) | emit @ floor | precision @ floor |
| --- | --- | --- | --- | --- |
| DEFAULT_BETAS | 0.585 | 0.742 | 99.7% | 0.211 |
| shipped #249 (n=1617) | 0.586 | 0.017 | 0.0% | — (never fires) |
| **this fit (n=300)** | 0.600 | 0.016 | 2.7% | 0.500 |

ECE stays excellent (calibration ≈ honesty is preserved), but the
operating point the sweep picked (0.271) fires at coin-flip precision on
the full set — versus the "know means >4-in-5 right" contract the 0.45
floor was operator-set to keep.

## Verdict

1. **Do not merge these betas as-is.** The #268 gate did its job at the
   protocol level and this PR carries the passing artifact, but the
   robustness probes show the protocol has a variance hole at n=300: a
   single 80/20 split cannot arbitrate a marginal fit. Merging would
   replace an n=1617 fit with an n=300 fit whose robust AUC estimate
   (~0.57–0.60) is *below* the floor and whose emit floor fires at 0.50
   precision.
2. **Gate hardening is the actionable #239 follow-up this run exposes:**
   the gate should evaluate the median (or worst) held-out AUC across
   k splits / seeds, not one seed. At n=300, seed choice alone swings
   the verdict 0.36 → 0.71.
3. **The features-don't-separate finding stands** under current master
   defaults (rrf + legacy blend): top_score is still inverted at the
   feature level, coordinate_confidence contributes nothing, and the
   best single feature is 0.559. This is the same conclusion as the
   circuit-tracer §4 and answer-presence spikes, now measured on the
   shipped stack at the current score scale.
4. **Blend-graduation co-fit is one command** (the knob is `--set`-able;
   no code changes needed):

```bash
# scale_relative candidate
python benchmarks/located_n1000.py --bed-db genomes/bench/matrix/xl.db \
  --base-config helix.toml --n 500 --seed 42 --axis located \
  --set retrieval.fusion_mode=rrf --set retrieval.blend_mode=scale_relative \
  --out benchmarks/results/located_n500_xl_rrf_blend_scale_relative.jsonl
python scripts/calibrate_know_confidence.py \
  --input benchmarks/results/located_n500_xl_rrf_blend_scale_relative.jsonl \
  --out helix.toml

# off candidate: swap blend_mode=off + matching --out
```

## Reproduce

```bash
python benchmarks/located_n1000.py --bed-db genomes/bench/matrix/xl.db \
  --base-config helix.toml --n 500 --seed 42 --axis located \
  --set retrieval.fusion_mode=rrf --out benchmarks/results/located_n300_xl_rrf.jsonl
python scripts/calibrate_know_confidence.py \
  --input benchmarks/results/located_n300_xl_rrf.jsonl --out helix.toml
python benchmarks/eval_retrieval.py --input benchmarks/results/located_n300_xl_rrf.jsonl \
  --calibration default,<shipped.toml>,helix.toml
```

Raw rows: `docs/benchmarks/data/2026-07-13-located-n300-xl-full-rrf.jsonl`
(carries both label fields — top-1 correctness and `expressed_rank` —
per the #249 label-definition note).

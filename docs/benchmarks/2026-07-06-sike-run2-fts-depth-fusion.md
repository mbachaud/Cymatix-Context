# SIKE Run-2 — FTS candidate-depth × fusion sweep (xl)

**Date:** 2026-07-06 · **Issue:** #205 (retrieval profiles) · **Tracks:** #221 (bedsweep)
**Driver:** `scripts/bench_chain/s3_fts_depth_sweep.py` · **Knob:** `[retrieval] fts5_candidate_depth`

## Question

Run-1 (the decontaminated SIKE bedsweep) found xl retrieval-capped, **not**
contamination-capped: decontaminating the ~42k-gene xl bed left
`gold_delivered_rate` at 0.62 (≈ the old contaminated 0.64), while the two
enterprise_rag beds sat at 0.84. Run-2 answers the open diagnostic:

> Is xl's ceiling **FTS candidate-pool starvation (A4)** — gold ranks below
> the 48-row FTS fetch, so it never enters tier scoring — or **rank squeeze
> (B2)** — gold enters the pool but the tier scoring can't float it into the
> delivered top-K?

## Method

Retrieval-only 2-axis sweep on the **exact Run-1 bed** (`genomes/bench/sike_beds/xl.db`,
41,896 genes), scoring `gold_delivered` over the 50 curated SIKE needles via
`bench_needle.check_gold_delivery` (verbatim; Run-1-comparable). No answer
model runs — `gold_delivered` is a property of `/context`, model-independent —
so the whole 6-cell sweep is seconds-per-cell, free, and GPU-free.

- `fts5_candidate_depth ∈ {48, 200, 500}` — 48 is the legacy `max_genes*4`.
- `fusion_mode ∈ {additive, rrf}`.
- Served with the lexical probe profile (dense/SPLADE/cymatics OFF),
  `HELIX_DISABLE_LEARN=1` (read-only).

The `fts5_candidate_depth` knob overrides **only** the Tier-3 FTS fetch depth;
the returned pool (`max_genes*2`) and delivery cap (`max_genes`) are unchanged,
so a deeper pool **cannot** inflate `gold_delivered` on its own — it can only
let a starved gold document *enter* scoring. The knob's unit test
(`tests/test_fts5_candidate_depth.py`) asserts the FTS `LIMIT` actually changes
48→200→500, so a flat curve is a genuine B2 signal, not a no-op knob.

## Result — `gold_delivered_rate`

| `fts5_candidate_depth` | additive | rrf |
|---:|:---:|:---:|
| 48 (legacy) | 0.62 | **0.74** |
| 200 | 0.62 | **0.74** |
| 500 | 0.62 | **0.74** |

`body_has_answer_rate`: additive 0.24–0.28, rrf 0.40–0.42 (flat across depth).

## Findings

1. **Baseline reproduced.** depth=48 / additive = **0.62** matches Run-1's
   reported xl = 0.62 exactly (same corpus + scorer) — validates the s3 driver
   against the s2 runner.

2. **A4 (pool starvation) REJECTED.** The depth curve is **perfectly flat** on
   both fusion modes; a 10× deeper FTS candidate pool (48→500) moves
   `gold_delivered` by **0.00**. Gold docs already enter the candidate pool at
   depth 48. → **Do not ship a larger default `fts5_candidate_depth` for xl —
   it would not help.** The knob stays (default 0 = legacy), useful for other
   corpora, but it is not xl's lever.

3. **B2 (rank squeeze) CONFIRMED** as xl's retrieval ceiling. Golds are in the
   pool but the tier scoring can't float them into the delivered top-12. The
   fix lives in **tier weights / fusion**, not pool size.

4. **Headline: RRF > additive by +12pp on xl (0.74 vs 0.62).** Consistent at
   every depth, and RRF also wins `body_has_answer` (0.40–0.42 vs 0.24–0.28).
   With dense/SPLADE off, this isolates *how the lexical tier signals are
   combined*: reciprocal-rank fusion is materially more robust than additive
   weighted-sum on this bed. Empirical support for flipping the fusion default
   (the README gotcha already flags "RRF will become default"); at minimum the
   large-lexical-bed profile in #205 Layer 3 should set `fusion_mode = "rrf"`.

5. **Orthogonal splice gap.** `gold_delivered ≫ body_has_answer` at every cell:
   the gold *source file* is delivered, but the delivered *chunk* often lacks
   the answer text (additive: 0.62 vs ~0.26; rrf: 0.74 vs ~0.42). This is a
   splice / fragment-granularity issue, separate from retrieval depth and
   fusion — its own follow-up (`splice_aggressiveness` / chunk size).

## Corroboration (secondary run)

A run on `genomes/bench/matrix/xl_clean.db` **raw** (no re-ingested golds,
41,803 genes) gives a lower baseline (depth=48/additive = 0.52) but the **same
flatness** (0.52 → 0.54 additive across depth) and the same additive<rrf
ordering — the A4 rejection is robust across corpus variants.

## Artifacts

- `benchmarks/results/sike_fts_depth_sweep_xl.json` (Run-1 bed, authoritative)
- `benchmarks/results/sike_fts_depth_sweep_xl_clean_raw.json` (secondary)
- (both gitignored — durable record is this doc + issue #205/#221 comments)

## Next

1. **RRF profile:** re-measure the full bedsweep (xl + erb10k + erb50k) under
   `fusion_mode = "rrf"`; if the +12pp holds on erb too, flip the default.
2. **B2 tier-weight investigation** — where do xl golds rank in the fused
   score, and which tier under-weights them.
3. **Splice-granularity follow-up** — close the gold_delivered→body_has_answer
   gap.

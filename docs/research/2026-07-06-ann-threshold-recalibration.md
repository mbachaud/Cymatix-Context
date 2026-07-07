# ANN dense-admission threshold recalibration (Phase 1a follow-up, 2026-07-06)

**Two findings, in tension — and the honest verdict is "don't ship yet."**
(1) The shipped `ann_similarity_threshold = 0.58` IS mis-calibrated: it
was fit on doc-doc random pairs but gates query-doc cosine, so it admits
only ~7% of golds (a real bug, pinned below). (2) But a live A/B shows
lowering it does NOT improve delivery on the SIKE bench — the pool floor
masks it and the dense arm is net-harmful on literal-fact needles. The
offline analysis says *what* is wrong; the live A/B says fixing it here
would not help. Phase 1a
(`docs/research/2026-07-06-phase1a-whitening-ab-results.md`) flagged the
0/5000 dense-admission failure as a threshold artifact; this note pins
the cause AND measures that the fix is not worth shipping on this
evidence.

## The distribution mismatch

The `ann_similarity_threshold` gates **query-doc** cosine (query encoded
with BGE-M3 `task="query"` instruction prefix vs `task="passage"` docs).
Its shipped value 0.58 was calibrated (helix.toml comment) on *"200k
random unrelated **doc** pairs: mean ~0.50, std ~0.066, p90 ~0.58"* —
i.e. **doc-doc** pairs. The query instruction prefix shifts the geometry
down: measured on xl_clean (1000 located queries × 500k random
query-doc pairs, raw-cosine BGE-M3):

| pair type | mean | std |
| --- | --- | --- |
| doc-doc (shipped calibration) | ~0.50 | ~0.066 |
| **query-doc (what the gate sees)** | **0.361** | **0.049** |

So 0.58 sits ~4.5σ above the query-doc random mean AND above the
gold-pair mean (0.476) — it admits almost no golds.

## Gold admission at absolute thresholds (xl_clean, n=1000 golds)

| abs threshold | gold_admit | random_fpr | ~false admits / 41,803 |
| --- | --- | --- | --- |
| **0.58 (shipped)** | **0.072** | 0.00004 | ~1 |
| 0.52 | 0.267 | 0.0009 | ~37 |
| 0.50 | 0.370 | 0.0025 | ~105 |
| **0.47 (≈ random p99, μ+2.3σ)** | **0.543** | 0.012 | ~494 |
| 0.44 (≈ random p95, μ+1.7σ) | 0.695 | 0.049 | ~2029 |

The shipped 0.58 delivers the gold document to the dense pool on **7.2%**
of queries. `dense_pool_floor_genes = 8` (#214) was added precisely to
band-aid this near-total starvation by force-admitting the top-8 dense
hits regardless of threshold.

## Sigma-multiplier equivalent (margin_over_random mode)

For `ann_threshold_mode = "margin_over_random"` (μ + k·σ over random
pairs), the same recovery corresponds to lowering k from the shipped
3.0:

| k | threshold | gold_admit | random_fpr |
| --- | --- | --- | --- |
| 3.0 (shipped) | 0.508 | 0.332 | 0.0017 |
| 2.5 | 0.483 | 0.476 | 0.0061 |
| **2.3** | **0.473** | **0.522** | **0.0099** |
| 2.0 | 0.459 | 0.617 | 0.0205 |

(Note: k=3.0 here admits 33% because μ+3σ is computed on the *correct*
query-doc random pairs — 0.508 — not the doc-doc-derived 0.58 the
absolute mode actually ships. The absolute 0.58 is worse than even the
formula's k=3.0.)

## Live A/B — the offline gain does NOT survive the pipeline

The offline gold-admission gap is real but does not translate to
delivery. Live A/B on xl_clean (50-needle SIKE set, `fusion_mode=rrf`,
dense on, shipped `dense_pool_floor_genes=8`;
`benchmarks/ab_ann_threshold.py`, artifact
`docs/research/data/2026-07-06-ab-ann-threshold-xl-clean.json`):

| cell | gold_delivered | content_has_answer | body_has_answer |
| --- | --- | --- | --- |
| dense **off** (lexical only) | **0.42** | **0.76** | **0.40** |
| dense on @ 0.58 (shipped) | 0.34 | 0.72 | 0.28 |
| dense on @ 0.47 (candidate) | 0.36 | 0.70 | 0.30 |

Two results, both refuting the config change on this evidence:

1. **Lowering 0.58 → 0.47 barely moves delivery** (gold +0.02, content
   −0.02). `dense_pool_floor_genes = 8` already force-admits the top
   dense hits regardless of threshold, so the 7% → 54% offline
   admission gain is masked — the pool floor, not the threshold, is the
   operative admission control.
2. **Dense-on is net-HARMFUL on this literal-needle set** (gold
   0.42 → 0.34, content 0.76 → 0.72 vs lexical-only). The SIKE needles
   are literal-fact lookups whose answer is a strong *single*-lexical-
   tier signal; the dense arm's broad-semantic hits add tier-breadth
   mass to non-answer docs under RRF, compounding the demotion in
   `docs/research/2026-07-06-rrf-gold-block-deficit.md`.

## Recommendation

- **Do NOT ship the threshold change on this evidence.** It is a genuine
  calibration bug (0.58 is doc-doc-derived; the gate is query-doc), but
  fixing it does not help the SIKE literal-needle bench: the pool floor
  masks it and dense is the wrong arm for literal fact lookups.
- **Where it would matter — re-A/B on a semantic corpus.** The dense arm
  carries real signal on enterprise/semantic queries (`config.py`: erb
  recall 0.58 → 0.64 with dense weight). The threshold recalibration
  should be A/B'd there (e.g. enterprise_rag_50k with semantic queries),
  not on the literal SIKE set. The offline finding says *what* to change;
  a semantic-bed live A/B says *whether* it helps.
- **Adjacent, higher-value lead: per-class dense gating.** Dense-on
  hurting literal needles argues for gating the dense arm off for the
  factual/lookup classifier classes (ties into the #205 per-class
  retrieval profiles) — a bigger lever than the threshold on this bench.

## Reproduce

```bash
# offline gold-admission sweep
python benchmarks/sweep_ann_sigma.py \
    --bed-db genomes/bench/matrix/xl_clean.db \
    --labels docs/benchmarks/data/2026-07-06-located-n1000-xl-clean-full-rrf.jsonl \
    --device cuda --out benchmarks/results/ann_sigma_sweep_xl_clean.json
# live A/B (dense off / on@0.58 / on@0.47)
HELIX_DISABLE_LEARN=1 python benchmarks/ab_ann_threshold.py \
    --bed-db genomes/bench/matrix/xl_clean.db \
    --out benchmarks/results/ab_ann_threshold_xl_clean.json
```

## Caveats

- Single bed (xl_clean), located-axis queries. A second-bed
  replication (enterprise_rag_50k) would firm the exact number; the
  direction (0.58 far too high for query-doc) is robust — it follows
  from the doc-doc vs query-doc mean gap alone.
- The doc-doc "mean ~0.50" is from the helix.toml comment, not
  re-measured here; the query-doc numbers are measured.

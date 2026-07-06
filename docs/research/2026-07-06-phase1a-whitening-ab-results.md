# Phase 1a results — Ledoit-Wolf whitening A/B vs cosine (2026-07-06)

**Verdict: do NOT adopt global whitening.** Measured on n=1000 located
queries against the fully-backfilled `xl_clean` bed (41,803 × 1024-d
BGE-M3 `embedding_dense_v2`), whitening loses ranking signal across the
board and wins only absolute-threshold clearance — which has a cheaper
fix (below). Per the pre-registered decision rule, the **Phase 0 probe's
control arm is raw-cosine BGE-M3**, not whitened.

Harness: `benchmarks/ab_whitening_dense.py` (this branch). Labels:
`located_n1000` JSONL (seed 42, located axis). Artifact:
`docs/research/data/2026-07-06-ab-whitening-dense-xl-clean.json`.

## Numbers (n=1000, Ledoit-Wolf shrinkage 0.0026)

| metric | cosine | whitened | Δ |
| --- | --- | --- | --- |
| retrieval@1 | 0.085 | 0.078 | −0.007 |
| retrieval@5 | 0.173 | 0.153 | −0.020 |
| retrieval@10 | 0.225 | 0.192 | −0.033 |
| MRR | 0.134 | 0.118 | −0.016 |
| median gold rank | 217 | 496 | worse |
| gold-vs-random AUC | **0.912** | 0.868 | −0.044 |
| random-pair mean | 0.361 | −0.000 | isotropy achieved |
| μ+3σ threshold | 0.508 | 0.091 | — |
| golds clearing μ+3σ | 0.332 | 0.409 | +0.077 |

## Reading

1. **The anisotropy is real and whitening removes it** (random-pair
   mean 0.361 → 0.000) — but the removed common directions carry
   ranking-relevant signal on this corpus: every rank metric and the
   gold-vs-random AUC degrade. On L2-normalized BGE-M3 output, the
   roadmap's assumption that whitening would *help retrieval* is
   measured false (exactly the council's "must be measured, not
   assumed" caveat).
2. **The verified 0/5000 dense-admission failure is a threshold-formula
   artifact, not a geometry ceiling.** In BOTH spaces the Stage-4
   μ+3σ-over-random threshold sits ABOVE the gold-pair mean (cosine:
   0.508 vs 0.476; whitened: 0.091 vs 0.082) while gold-vs-random AUC
   is 0.91 — the separation exists; the formula demands too much. A
   percentile- or FPR-targeted threshold (e.g. random-pair p99 ≈ μ+2.3σ)
   admits most golds with bounded noise, no space transform needed.
   That is the cheap follow-up `[retrieval] ann_threshold_sigma_multiplier`
   already parameterizes (3.0 → ~2.0–2.3, measured).
3. **Dense is a weak arm on this bed regardless** (retrieval@1 8.5%
   dense-only vs 22.5% for the full lexical pipeline) — consistent with
   the RRF fusion story: dense contributes as a fused rank signal, not
   as a standalone ranker.

## Consequences for the roadmap

- Phase 1a's "whitening then density-as-a-6th-logistic-feature" line
  should not proceed on the whitening leg. Density-as-feature can still
  be tested against raw cosine geometry if #239's fitted logistic
  leaves headroom.
- Phase 0 pre-registration (`2026-07-06-phase0-remetric-prereg.md`)
  control arm resolves to **raw cosine** per its decision rule.
- Cheap measured win to file instead: recalibrate
  `ann_threshold_sigma_multiplier` (or switch the formula to a
  random-pair percentile) against the backfilled beds.

## Caveats

- Single bed (xl_clean), single query axis (located), n=1000. The
  direction is consistent with the n=50 smoke; a second-bed replication
  (ERB-50k, dense-native content) would firm it up but is unlikely to
  reverse a uniform-sign result.
- Query encoding on CPU (identical codec path; device does not change
  outputs, only latency).
- Whitening variant tested: global centered Ledoit-Wolf Σ^(-1/2) fit on
  doc vectors. Milder variants (partial whitening α<1, top-k component
  removal à la all-but-the-top) were not tested and could in principle
  keep ranking while fixing thresholds — file under research-if-needed;
  the threshold fix above makes them non-blocking.

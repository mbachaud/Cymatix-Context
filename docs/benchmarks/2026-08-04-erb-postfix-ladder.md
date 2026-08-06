# ERB step ladder, post-fix: the cliff is gone — 829k genes at 26s, 4.3× faster

**Date:** 2026-08-04 · **Branch:** `perf/slice1-instrumentation` · Sequel to
`2026-08-03-erb-step-curve.md` (pre-fix curve + mechanism diagnosis).
fp16 dense matrix + `splade_df_cap_fraction = 0.10` (transient toml; repo
default stays 0.0), same 141-needle carve-safe query set on every arm.

## What landed between the two ladders (commits 6c2aac6 → 026b128)

1. **stats() off the hot path** — `total_genes()` COUNT-only accessor at
   every request-path call site (was 11.1s warm / 72.7s cold per call at
   829k). Sharded adapter mirrored.
2. **tag_prefix tier** — covering `(tag_value, gene_id)` index + rewrite:
   UNION ALL **range predicates** (`>= lo AND < next-prefix`) with a CROSS
   JOIN ordering pin. Two planner traps found en route, each now a gate
   test: a plain JOIN flattens back to `SCAN genes`; LIKE can't range a
   BINARY-collation index (`case_sensitive_like=OFF`) and full-scanned the
   covering index — 6.2s at 100k → **20ms** after the range form.
3. **SPLADE** — covering `(term, gene_id, weight)` index + query-side DF
   cap (`splade_df_cap_fraction`, #327 pattern) over new ingest-maintained
   `splade_term_df` counters. Cap 0.10 A/B'd quality-neutral at 100k
   (recall@12 0.600 = 0.600, overlap 1.0; receipts `erb100k_cap*.json`).

Beds: subset-carves from the July blob (`carve_subset.py`) with indexes,
DF counters, rebuilt FTS, and ANALYZE baked in — ingest parameters held
constant so bed size is the only variable. (Carver note: the original
double-IN pair filter was quadratic — 4.4h at 250k, killed ~12h CPU at
500k; indexed temp ids + EXISTS → the same phase in 116s, full 500k carve
in 18 min.)

## The post-fix curve

| genes | bed | c=1 median | p95 | recall@12 | c=2 median× | c=2 qps× |
|---|---|---|---|---|---|---|
| 100k | 6.4GB carve | 5,084ms | 11.3s | 0.60 | 0.92× | 1.78× |
| 250k | 15.8GB carve | 8,567ms | 15.1s | 0.533 | 1.11× | 1.70× |
| 500k | 31.2GB carve | 15,172ms | 26.1s | 0.40 | 1.14× | 1.83× |
| 829k | 49.4GB probe | **26,081ms** | 44.2s | 0.40 | 1.04× | ~1.5× |

**Against the pre-fix ladder:**

- **829k: 111.7s → 26.1s median (4.3×)** on the same physical bed class,
  zero errors, overlap 1.0.
- **The >RAM cliff is gone as a *scaling* phenomenon**: 500k→829k is
  1.66× genes → 1.72× latency — the same near-linear slope as the cached
  region, where pre-fix the law jumped from √N to ~linear with a big
  constant. Index-range I/O doesn't shred the page cache the way the old
  full scans did; the c=2 penalty on the >RAM bed collapsed 1.58× → 1.04×.
- 100k post-fix (5.1s) ≈ 10k pre-fix (6.0s): ~6× more corpus at the same
  latency.
- c=2 throughput now 1.7–1.83× (was ~1.4–1.57×) — less work per query
  leaves the 2-thread executor less saturated. W3.2 executor sizing is
  still the next concurrency lever.

## 829k anatomy after the fixes (ring tail, c=1)

express p50 24.4s of the 26.1s; **no single monster left**: splade 4.2s
(was 43.4) + coact_expand 3.5s + fts5 2.1s + dense 2.0s + ann leg 1.9s +
lex_anchor 1.5s + tag_prefix 1.4s (was 35.1) ≈ 16.5s attributed;
assemble 45ms (was 12.9s). Residual inside express ≈ 8s at 829k — the
next instrumentation target, alongside coact_expand growth.

## Quality note (orthogonal to latency)

recall@12 on the fixed needle set erodes 0.60 → 0.533 → 0.40 → 0.40 as
the corpus grows. This is retrieval precision under corpus dilution, not
a latency artifact (identical trend pre-fix), and it plateaus 500k→829k.
It is the ERAG-bed quality workstream's problem: candidate-depth scaling,
tag-tier discipline (#327), and rerank at scale.

## Receipts

`benchmarks/dogfood/erb/receipts/erb_ladder_{100k,250k,500k,829k_c1,829k_c2}.json`
(+ `erb100k_capA.json` — the run that exposed the LIKE regression — and
`erb100k_capA2/capB` for the DF-cap A/B). Canary p95 ranged 65–373ms
across arms (loop healthy; higher than pre-fix runs' 17–60ms — faster
query turnover keeps the loop busier; not gate-relevant on this basis).

## Recommended defaults for the EnterpriseRAG bed

`CYMATIX_DENSE_MATRIX_DTYPE=float16` (A/B'd) and, once validated on the
production query mix, `splade_df_cap_fraction = 0.10` (quality-neutral on
this needle set at every size). Existing beds need one-time index/DF
backfill (carves have it baked; `prep` pattern in
`benchmarks/dogfood/logs/probe_bed_prep.txt` — 12 min at 829k).

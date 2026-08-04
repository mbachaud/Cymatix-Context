# Runbook: fp16 dense-matrix A/B (dogfood bed)

**Question:** does `CYMATIX_DENSE_MATRIX_DTYPE=float16` (half the resident
dense-matrix RAM; compute stays fp32 — numpy promotes inside the matmul)
move retrieval quality or latency beyond gate tolerances?

**A arm** = committed fp32 baseline: `benchmarks/dogfood/server_scaling_baseline.json`
(c=1..16; gates compare only levels present in both receipts).
**B arm** = fresh fp16 run at c=1,2 (~5 min).

Preconditions (verified 2026-08-03): env knob flips dtype
(`_dense_matrix_dtype`, knowledge_store.py:391); BenchServer spawn inherits
the parent env; bed identity 13f8bbe3d3a37912; no other bench process
running (`taskkill //T` any stragglers — see the zombie lesson in
`docs/benchmarks/2026-08-02-perf-slice2-concurrency.md`).

## Run (from the worktree root, Git Bash)

```bash
cd F:/Projects/cymatix-context/.claude/worktrees/perf-slice1

CYMATIX_DENSE_MATRIX_DTYPE=float16 \
python benchmarks/dogfood/run_server_scaling.py \
  --concurrency 1,2 --rounds 2 --copy-bed \
  --out benchmarks/dogfood/logs/fp16_probe.json

python benchmarks/dogfood/check_perf_receipts.py \
  --kind server_scaling \
  --fresh benchmarks/dogfood/logs/fp16_probe.json
```

## Read the verdict

- Exit 0 / `"verdict": "PASS"` → fp16 is retrieval-safe on this bed:
  recall@12 within ±0.02, median ≤1.15×, canary ≤1.5×, qps ≥0.85× per
  level. Note the result in the campaign doc; fp16 becomes the
  recommended setting for the EnterpriseRAG bed (~3.5GB → ~1.75GB).
- Exit 1 → read `gates[]` for the failing gate. A recall failure
  disqualifies fp16 (quality rule: latency wins never buy recall losses).
  A latency-only failure on a noisy box: re-run once on a quiet box
  before concluding.

Expectation: PASS with ratios ≈1.0 — fp16 quantization perturbs cosines
~1e-4, three orders below the 0.58 ANN threshold granularity; the dogfood
matrix is only ~26MB so no RAM effect is visible here. This run is the
quality check; the RAM payoff is measured later on the 850k-gene bed.

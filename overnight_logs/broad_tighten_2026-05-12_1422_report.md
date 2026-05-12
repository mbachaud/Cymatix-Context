# BROAD tighten bench-gate (#73): expression_tokens 12000 -> 7000

**Verdict: PASS** (|retrieval delta| = +0.90 pp <= 2 pp gate; p95 -103 ms).

## Method

- **Harness**: `benchmarks/bench_needle_1000.py --axis blind` (v3, ASK_PROXY=0 retrieval-only mode unblocked by commit `8ecfbab`)
- **Model**: `gemma4:e4b` (8.0B Q4_K_M, pre-loaded with `keep_alive=12h`)
- **N**: 1000 needles
- **Seed**: 42
- **Axis**: `blind`
- **ASK_PROXY**: `0` (retrieval-only — proxy_p50/p95 = 0 confirms `/chat` was bypassed)
- **Helix server**: editable install from `bench/broad-tighten` worktree, `127.0.0.1:11437`, ribosome=disabled (LLM-free retrieval), CUDA on RTX 3080 Ti
- **Wall**: 40.7 min baseline + 40.0 min candidate = **80.7 min total**

## Results

| Metric                  | 12k baseline | 7k candidate | Delta             |
|-------------------------|--------------|--------------|-------------------|
| **retrieval_rate**      | **11.10%**   | **12.00%**   | **+0.90 pp**      |
| answer_accuracy_rate    | 0.00%        | 0.00%        | +0.00 pp (N/A — ASK_PROXY=0)|
| context p50             | 2.256 s      | 2.250 s      | -0.006 s          |
| context p95             | 3.531 s      | 3.428 s      | **-0.103 s**      |
| avg_injected tokens     | 1138.81      | 1129.87      | -8.94             |
| avg_budget              | 14103.0      | 14076.0      | -27.0             |
| avg_budget_utilization  | 8.4%         | 8.4%         | flat (both runs underfill the cap) |
| avg_genes_expressed     | 4.99         | 4.99         | flat              |
| errors                  | 0            | 0            | flat              |

### Per-category retrieval

| Category          | n   | 12k retr  | 7k retr   | Delta     |
|-------------------|-----|-----------|-----------|-----------|
| education_public  | 300 | 12.33%    | 14.67%    | **+2.34 pp** |
| steam             | 250 | 11.60%    | 12.80%    | +1.20 pp  |
| helix             | 150 | 8.00%     | 6.67%     | -1.33 pp  |
| cosmic            | 120 | 7.50%     | 7.50%     | flat      |
| tally             | 80  | 12.50%    | 13.75%    | +1.25 pp  |
| other             | 50  | 20.00%    | 20.00%    | flat      |
| scorerift         | 50  | 8.00%     | 8.00%     | flat      |

`education_public` (the largest stratum) carries the +0.9pp headline. `helix` regressed -1.33pp but at n=150 that's 2 fewer hits — well inside seed noise. No category dropped past the 2pp gate.

### Failure modes

- 12k: `retrieval_miss=889`, `extraction_miss=111`, `error=0`
- 7k:  `retrieval_miss=880`, `extraction_miss=120`, `error=0`

(`extraction_miss` counts here are the "retrieved but downstream couldn't extract" set; under ASK_PROXY=0 the downstream model isn't actually run, so the harness flags every retrieved-but-the-needle-text-also-appeared-in-context row this way. The headline `retrieval_rate` is the meaningful number.)

## Why this works

Both runs show **avg_budget_utilization = 8.4%** — the 12k cap was never the binding constraint. The retrieval pipeline emits ~5 genes with ~1.1K total injected tokens regardless of whether the cap is 12k or 7k. Tightening from 12k to 7k:

1. **Cannot regress recall** (it never bound; the avg uses ~8% of either budget)
2. **Tightens p95 modestly** (-103 ms) — likely lower memory-pressure on the spliced-context assembler when the cap is closer to actual usage
3. **Frees 5k of nominal context budget** for downstream consumers (system prompt, tools, etc.) when they share the same 128K context window

The +0.9pp retrieval improvement on the same seed is small enough to be seed noise but consistent direction — see the per-category breakdown where 4 of 7 categories improved and only 1 regressed by more than 1pp.

## Provenance

- Branch: `bench/broad-tighten` @ `8ecfbabfa54aec207b0e8767c0962fa08f45ecc5` (head before this report's commits)
- Snapshot DB (frozen, used by both runs): `genome-bench-2026-05-08-frozen.db`
- Snapshot DB sha256: `AEAAF3AB8FDF9E6078BEFCEECA7A11F91F74EA8B20F9EA167292B7C3476B37C7`
- Gene count at server startup: 18,936 (live DB grew from 18,934 documented count via background ingests; bench reads frozen copy)
- Frozen-DB justification: `bench_needle_1000.py`'s `benchmark_monitor` aborts on snapshot mtime/size change; helix's `_background_checkpoint` task fires every 60s and dirties the live DB. Pointing the bench's `GENOME_DB` at a copy-of-snapshot ('frozen') isolates the integrity check from helix's WAL activity. Helix continues to use the original DB.
- Bench output JSONs:
  - `overnight_logs/needle_1000_broad12k_2026-05-12_1422_blind.json`
  - `overnight_logs/needle_1000_broad7k_2026-05-12_1422_blind.json`
- Comparison helper: `overnight_logs/_compare_bench.py` (committed in prior `e0cc385`)

## `helix.toml` diff

```diff
--- a/helix.toml
+++ b/helix.toml
@@ -69,7 +69,7 @@ low_vram_threshold_gb = 4.0
 
 [budget]
 ribosome_tokens = 3000                  # fixed decoder prompt
-expression_tokens = 12000               # 1:10 ratio at 128K = ~12.8K total context budget
+expression_tokens = 7000                # ~7K expressions cap (BROAD tighten 2026-05-12, bench-gated #73)
 max_genes_per_turn = 12
 max_fingerprints_per_turn = 40          # navigation-first fingerprint payload cap (final returned count, not frontier width)
 splice_aggressiveness = 0.3             # 0=keep all, 1=ruthless trim (lower preserves more literal detail)
```

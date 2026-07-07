# Splice-floor fix — re-measure (2026-07-06)

**What this is.** The acceptance measurement for the splice-floor fix
(council kill-switch #1, PR #248): depth-48 cells of the Run-2 sweep
(`scripts/bench_chain/s3_fts_depth_sweep.py`, 50 SIKE needles,
retrieval-only) run before (the Run-2 re-baseline,
`docs/benchmarks/2026-07-06-rrf-default-rebaseline.md` on PR #247) and
after the fix, on both beds. Depth was measured flat in the re-baseline,
so depth-48 stands in for the curve.

## Headline (depth 48)

| bed / fusion | metric | before | after | Δ |
| --- | --- | --- | --- | --- |
| xl / additive | content_has_answer | 0.56 | **0.80** | **+0.24** |
| xl / rrf | content_has_answer | 0.70 | **0.94** | **+0.24** |
| xl_clean / additive | content_has_answer | 0.54 | **0.76** | **+0.22** |
| xl_clean / rrf | content_has_answer | 0.60 | **0.78** | **+0.18** |
| xl / additive | gold_delivered | 0.62 | 0.62 | 0.00 |
| xl / rrf | gold_delivered | 0.74 | 0.72 | −0.02 |
| xl_clean / additive | gold_delivered | 0.50 | 0.52 | +0.02 |
| xl_clean / rrf | gold_delivered | 0.46 | 0.46 | 0.00 |

The expected recovery was ~+0.12 (the 6 flagged needles); the measured
recovery is **+0.18 to +0.24** — the query-agnostic 1000-char floor was
truncating answers on ~9–12 needles per arm, not 6.

## The 6 flagged truncation-miss needles (xl)

All 6 recover `content_has_answer` under additive; under rrf the 3 that
were still failing after the fusion flip (`bookkeeper_1099_threshold`,
`bookkeeper_test_count`, `bookkeeper_backup_interval`) recover and the
other 3 stay recovered. `gold_delivered` stays True for all 6 on xl.
On xl_clean, `helix_port` remains a genuine retrieval miss under
additive (no gold delivered before or after — not a splice problem),
and `cosmictasha_auth_library` gains gold under additive.

## Regressions, inspected

- **`genome_compression_target` (xl/rrf) lost gold_delivered** (5→5
  blocks — displaced by assembly's budget-eviction as spliced parts
  grew). No answerability impact: its `content_has_answer` was False
  before AND after (the previously-delivered gold block did not carry
  the answer). This is the known trade of the 0.9 budget safety factor;
  1 needle in 200 cells-worth of measurements.
- **`body_has_answer` dips** (−0.02 to −0.06): every "lost" body needle
  has `content_has_answer=True` after the fix — the strict
  citation→body parser fails closed on the rescue-tail format
  (`[...]`-joined lines), the same fail-closed artifact that made 17/23
  needles false alarms in the original investigation. The honest
  deliverability bound for this comparison is `content_has_answer`.

## Artifacts

- `docs/benchmarks/data/2026-07-06-splice-fix-remeasure-xl-d48.json`
- `docs/benchmarks/data/2026-07-06-splice-fix-remeasure-xl-clean-d48.json`
- Before-side: `docs/benchmarks/data/2026-07-06-rebaseline-fts-depth-sweep-*.json`
  (PR #247 branch).

## Caveats

- `content_has_answer` is an upper bound (word-boundary accept match;
  wrong-referent false positives possible — and the fix deliberately
  pulls more query-term-bearing lines into context, so per-needle rows
  were spot-checked above rather than trusting the rate alone).
- Measured on the lexical probe profile (dense/SPLADE off), matching
  the Run-2 baseline conditions.

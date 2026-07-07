# SIKE Run-2 re-baseline — RRF as the shipped default (2026-07-06)

**What this is.** The measured replacement for the prose-only "RRF 0.72
vs additive 0.58" figures that circulated in the J-space roadmap docs.
The 2026-07-06 roadmap council (kill-switch #2, verified-premises
ledger) flagged those numbers as unmeasured in-tree; council move #1
required re-publishing RRF vs additive **on the committed harness under
the real metrics** before the default flip ships. This note is that
artifact, produced together with the `fusion_mode` default flip
(`additive` → `rrf`, PR #247).

**Method.** `scripts/bench_chain/s3_fts_depth_sweep.py` (PR #245), 6
cells (`fts5_candidate_depth ∈ {48, 200, 500}` × `fusion_mode ∈
{additive, rrf}`), 50 SIKE needles, retrieval-only (`decoder_mode =
"none"`, `HELIX_DISABLE_LEARN=1`, port 11439, lexical probe profile
`docs/benchmarks/helix_probe_lexical.toml`). Metrics from
`bench_needle.check_gold_delivery`:

- `gold_delivered` — a gold-source block reached the assembled context
  (model-independent retrieval success);
- `content_has_answer` — word-boundary accept-string match over the
  full assembled content (honest deliverability **upper bound**; can
  false-positive on wrong-referent tokens);
- `body_has_answer` — stricter per-block citation→body pairing (fails
  closed under the legibility-disabled probe profile → **lower
  bound**).

## xl bed (`genomes/bench/sike_beds/xl.db`, 41,896 genes)

| depth | fusion | gold_delivered | content_has_answer | body_has_answer |
| --- | --- | --- | --- | --- |
| 48    | additive | 0.62 | 0.56 | 0.24 |
| 48    | **rrf**  | **0.74** | **0.70** | **0.42** |
| 200   | additive | 0.62 | 0.56 | 0.28 |
| 200   | **rrf**  | **0.72** | **0.72** | **0.42** |
| 500   | additive | 0.62 | 0.56 | 0.28 |
| 500   | **rrf**  | **0.72** | **0.72** | **0.44** |

## xl_clean bed (`genomes/bench/matrix/xl_clean.db`, decontaminated)

| depth | fusion | gold_delivered | content_has_answer | body_has_answer |
| --- | --- | --- | --- | --- |
| 48    | additive | 0.50 | 0.54 | 0.26 |
| 48    | **rrf**  | 0.46 | **0.60** | **0.38** |
| 200   | additive | 0.54 | 0.56 | 0.26 |
| 200   | **rrf**  | 0.48 | **0.62** | **0.38** |
| 500   | additive | 0.54 | 0.56 | 0.26 |
| 500   | **rrf**  | 0.48 | **0.64** | **0.40** |

## Findings

1. **xl: RRF > additive on every metric at every depth**: +10–12pp
   `gold_delivered` (0.72–0.74 vs 0.62), +14–16pp `content_has_answer`
   (0.70–0.72 vs 0.56), +14–18pp `body_has_answer` (0.42–0.44 vs
   0.24–0.28). This replaces (and slightly exceeds) the prose
   "0.72 vs 0.58" claim; direction matches the verified additive
   mis-scaling (dense cosine ×16 semantic arm vs the FTS bm25 cap 6.0).
2. **xl_clean splits the verdict — and answerability wins**: additive
   delivers more gold *blocks* (0.50–0.54 vs 0.46–0.48) but RRF
   delivers more *usable answers* on both the upper-bound (+6–8pp
   `content_has_answer`, 0.60–0.64 vs 0.54–0.56) and lower-bound
   (+12–14pp `body_has_answer`, 0.38–0.40 vs 0.26) metrics. The
   agent-facing contract is answer deliverability, not block delivery;
   the flip stands on both beds under the metrics that matter. The
   gold-block deficit on the clean bed is worth a follow-up
   (rank-composition of what RRF promotes over the gold blocks).
3. **Depth is flat on both beds** (48 → 500 within ±0.04) —
   reconfirms Run-2's B2 rank-squeeze diagnosis over A4 pool
   starvation: the ceiling is scoring, not candidate supply.
4. **The truncation tail is visible in the gap** between
   `gold_delivered` and `content_has_answer` under rrf on xl (0.74 vs
   0.70 at depth 48): needles whose gold arrived but whose answer text
   was cut by the query-agnostic 1000-char splice floor
   (`bookkeeper_1099_threshold`, `bookkeeper_test_count`,
   `bookkeeper_backup_interval` in the per-needle rows). That is
   council kill-switch #1, fixed separately in PR #248.

## Artifacts

- `docs/benchmarks/data/2026-07-06-rebaseline-fts-depth-sweep-xl.json`
  (committed copy; per-needle rows included)
- `docs/benchmarks/data/2026-07-06-rebaseline-fts-depth-sweep-xl-clean.json`
- Produced from the PR #245 driver merged with the PR #247 flip; the
  driver pins `fusion_mode` per cell, so these numbers are independent
  of the shipped default and reproduce on either branch.

## Caveats

- `content_has_answer` is an upper bound (accept-string
  false-positives on wrong-referent tokens are possible).
- The lexical probe profile disables dense/SPLADE/rerank/cymatics —
  this isolates the fusion comparison but is not the full shipped
  stack.
- The know-logistic (`[know]`) remains calibrated for neither scale
  (#239); fitting it against rrf-scale scores is the follow-up on
  `feat/eval-retrieval-know-calibration`.

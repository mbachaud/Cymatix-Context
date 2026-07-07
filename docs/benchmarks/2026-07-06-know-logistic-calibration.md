# Know-logistic calibration — first real fit (#239, 2026-07-06)

**What this is.** The first actual calibration of the KnowBlock
confidence logistic. Since Stage 6 shipped, `helix.toml [know]` carried
`DEFAULT_BETAS` verbatim (no `calibrated_at`; the generator
`benchmarks/located_n1000.py` referenced by
`scripts/calibrate_know_confidence.py` never existed) — flagged by the
2026-07-06 J-space roadmap council as kill-switch #4/#5 and by #239 as
an anti-signal (AUC 0.35–0.44 on earlier beds). This PR builds the
generator + eval harness, produces labeled data on two beds, and fits.

## Data

Three `located_n1000` runs (Stage-1 located-axis 4-locator queries,
seed 42, `build_context(read_only=True)` + `HELIX_DISABLE_LEARN=1`):

| set | bed | config | rows | r@1 |
| --- | --- | --- | --- | --- |
| v1 (reference) | xl_clean | lexical probe + rrf | 1000 | 0.225 |
| v2 | xl_clean | **shipped stack** (dense on) + rrf | 1000 | 0.223 |
| v3 | enterprise_rag_50k | **shipped stack** (dense on) + rrf | 617 | 0.204 |

The fit uses the **v2+v3 union (n=1617)** — shipped-stack features
(dense on, so `lexical_dense_agree` has real variance; under the
lexical probe it was constant and its β fit to 0) at the new rrf score
scale. Label = `planted_gene_id == retrieved_top1` per the calibrate
script's contract.

## Fit (sklearn logistic, 80/20 split, seed 42)

```toml
[know]
emit_floor      = 0.45     # operator-set operating point, see below
s_ref           = 4.2503   # median rrf-scale top_score (was 1.0)
g_ref           = 0.4386
betas           = [-2.1222, -1.1442, 0.8794, 0.9407, 0.2999, 0.7979]
calibrated_at   = "2026-07-07T01:39:23+00:00"
calibrated_on_n = 1617
```

Note **β1 (top_score) fits NEGATIVE (−1.14)** — confirming #239's
"hit-mean < miss-mean" anti-signal finding: on these beds a *high*
fused top score slightly *lowers* P(top-1 correct). The positive signal
comes from score_gap (β2 +0.88), lexical_dense_agree (β3 +0.94),
coordinate_confidence (β4 +0.30) and freshness_min (β5 +0.80).

## Before / after

| set | calibration | AUC | ECE (10-bin) | emit rate @ floor | precision @ floor |
| --- | --- | --- | --- | --- | --- |
| union | DEFAULT_BETAS | 0.640 | 0.741 | 0.996 | 0.215 |
| union | **fitted** | 0.647 | **0.040** | 0.014 | **0.826** |
| xl_clean | DEFAULT_BETAS | 0.637 | 0.732 | 0.994 | 0.222 |
| xl_clean | **fitted** | 0.638 | **0.037** | 0.017 | **0.824** |
| erb50k | DEFAULT_BETAS | 0.661 | 0.757 | 1.000 | 0.204 |
| erb50k | **fitted** | 0.696 | **0.049** | 0.010 | **0.833** |

Risk-coverage tables are in the committed eval reports
(`docs/benchmarks/data/eval_located_*.json`).

**The contract inverts.** Before: the know block fired on ~100% of
queries and was wrong 4 times in 5 (ECE 0.74 — the logistic claimed
≥0.95 confidence almost everywhere). After: it fires on ~1.4% of
queries and is right 4 times in 5. Honest and rare beats confident and
wrong for agent trust — but see the discrimination caveat.

## emit_floor operating point

The calibrate script sweeps the test split for a 0.95 (then 0.80)
precision floor; both were unresolvable on the 20% split (too few
high-confidence rows) and the script fell back to 0.55 — which the
honestly-calibrated confidences **never reach** (max 0.465). The floor
was therefore operator-set to **0.45**, the measured union operating
point (precision 0.826, coverage 1.4%). Alternative points from the
sweep: 0.40 → precision 0.54 @ coverage 6.9%; 0.30 → 0.38 @ 14.7%.

## Honest limits + follow-ups

1. **Discrimination is still weak** (AUC 0.64–0.70). Calibration fixes
   *honesty*, not *separation* — the #239 AUC>0.7 gate is met on
   erb50k (0.696 ≈ 0.7) but not on xl_clean (0.638). More/better
   features (e.g. density-as-feature against raw cosine geometry, per
   the Phase 1a verdict) are the path to real headroom.
2. **Label-definition sensitivity**: the spec labels on top-1
   correctness (base rate ~0.21); the agent-facing semantic is
   arguably "answer present in the delivered window"
   (`expressed_rank > 0`, base rate ~0.5), which would support higher
   floors at useful coverage. Worth a spec decision before the next
   refit; the JSONLs carry both fields.
3. **Both beds are needle-hard**; a production-corpus generation pass
   (main genome) would rebalance the base rate upward.
4. v1 (lexical probe) is committed as the reference arm and the
   Phase 1a label source; do not fit on it (constant
   `lexical_dense_agree`).

## Reproduce

```bash
python benchmarks/located_n1000.py --bed-db genomes/bench/matrix/xl_clean.db \
    --base-config helix.toml --n 1000 --seed 42 --axis located \
    --set retrieval.fusion_mode=rrf --out benchmarks/results/located_n1000_xl_clean_full_rrf.jsonl
python scripts/calibrate_know_confidence.py \
    --input <union.jsonl> --out helix.toml
python benchmarks/eval_retrieval.py --input <set.jsonl> \
    --calibration default,helix.toml
```

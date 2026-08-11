# SCBench Regression V2 and Terra Replication Design

## Status

Approved for implementation on 2026-08-11 by the instruction: “Fix/freeze then dispatch the terra bench.”

## Objective

Correct the post-treatment eligibility bias in the Luna pilot’s regression statistic, preserve the original preregistered result, and run an independent GPT-5.6 Terra AB/BA replication using the corrected analysis.

## Immutable Luna evidence

The existing Luna campaign, receipts, `analysis.json`, and `analysis.md` remain the version-1 preregistered record. Version 2 writes distinct `analysis-v2.json` and `analysis-v2.md` artifacts. It never edits selected attempts, pair receipts, or version-1 outputs.

## Corrected primary estimand

For every paired checkpoint with index greater than one:

1. Require numeric `regression_passed` and `regression_total` fields for both arms.
2. Require positive and equal regression-test denominators across arms.
3. Compute each arm’s regression pass rate as `regression_passed / regression_total`.
4. Compute the paired effect as `cymatix_rate - control_rate`; positive values favor Cymatix.

The primary point estimate is the median paired effect. A deterministic 10,000-iteration bootstrap resamples all eight problem trajectories with replacement and reports the 2.5th and 97.5th percentiles of the median effect.

The previous `isolated_pass_rate == 1.0` arm-specific eligibility gate is not used. Isolated solve performance remains a separate noninferiority outcome.

## Decision semantics

- `benefit_detected`: the regression-effect interval is entirely above zero.
- `harm_detected`: the interval is entirely below zero.
- `no_detectable_difference`: the interval contains zero.

The safety gate fails only for `harm_detected`. Erosion and verbosity use problem-clustered intervals and fail only when their entire interval exceeds the existing `+0.03` ceiling. Existing isolated-solve, token, elapsed-time, and integrity limits remain unchanged.

Version 2 also reports pooled prior tests passed, strict-score head-to-head counts, regression-rate head-to-head counts, and all interval inputs so the verdict is auditable.

## Terra campaign

Terra is a separate campaign with a new campaign ID, empty output root, fresh preflight receipt, and its own analysis artifacts. It preserves:

- the eight Luna problems and easy/medium/hard partition;
- two AB/BA replicates;
- ChatGPT subscription authentication;
- one worker and no concurrent evaluation;
- GPT-5.6 Terra at `medium` reasoning;
- the exact qualified Cymatix treatment and source policy;
- the frozen SCBench fork and Docker image digest.

Terra and Luna results are reported separately. Any cross-model comparison is descriptive; checkpoints are never naively pooled as independent observations.

## Qualification and dispatch

Before scoring, a subscription-auth Terra smoke invocation must succeed, the Terra SCBench model configuration must resolve, both repositories must be clean at recorded commits, and the standard campaign preflight must pass. The background supervisor runs pairs sequentially, schedules the second arm only after the first succeeds, stops fail-closed on invalidation, verifies receipts, then invokes version-2 analysis.

## Testing

Tests must prove that:

- an arm cannot become uniquely regression-eligible merely by achieving a perfect isolated score;
- direct regression counters produce the expected paired delta;
- unequal or missing regression denominators invalidate v2 analysis;
- problem-clustered intervals are deterministic;
- version-1 analysis behavior and artifacts remain unchanged;
- version-2 output uses distinct filenames;
- the Terra manifest freezes model, reasoning, ordering, and checkpoint totals.


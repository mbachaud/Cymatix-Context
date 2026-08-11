# GPT-5.6 Terra SCBench replication

This campaign independently repeats the frozen Luna AB/BA design with
`gpt-5.6-terra` at `medium` reasoning through ChatGPT subscription auth.
It uses the corrected paired-regression v2 estimator and does not pool Terra
checkpoints with Luna checkpoints.

The eight problems, treatment packet, source exclusions, arm ordering, Docker
digest, one-worker limit, and retry/invalidation policy are unchanged. The
SCBench commit adds only the Terra model catalog entry; its pricing values are a
reporting proxy because this campaign is subscription-backed. Token and elapsed
metrics remain directly observed.

`qualification-terra.json` records the successful read-only, ephemeral Terra
subscription smoke call. The manifest's operational qualification hash pins
that receipt. Pair outputs and preflight state are generated locally and ignored
until the campaign is complete and reviewed.

The supervisor runs every pair sequentially, respects each replicate's AB/BA
order, verifies all selected receipts, and writes `analysis-v2.json` and
`analysis-v2.md` only after all 16 pairs are valid.

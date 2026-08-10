# SCBench Codex/Cymatix regression-resistance pilot

This issue tracks the frozen, eight-problem, subscription-backed Codex pilot. It does not authorize a scored run by itself: execution starts only after the operator confirms the exact campaign and fork commits reported below.

## Frozen evidence and implementation

- [Experimental design](https://github.com/mbachaud/Cymatix-Context/blob/codex/scbench-cymatix-ab/docs/benchmarks/2026-08-08-scbench-codex-cymatix-ab-design.md)
- [Implementation plan](https://github.com/mbachaud/Cymatix-Context/blob/codex/scbench-cymatix-ab/docs/superpowers/plans/2026-08-08-scbench-codex-cymatix-ab-harness.md)
- [Pilot manifest](https://github.com/mbachaud/Cymatix-Context/blob/codex/scbench-cymatix-ab/benchmarks/scbench/campaigns/2026-08-08-codex-pilot/manifest.json)
- [Cymatix implementation branch](https://github.com/mbachaud/Cymatix-Context/tree/codex/scbench-cymatix-ab)
- [SCBench fork branch](https://github.com/mbachaud/slop-code-bench/tree/cymatix/codex-ab), pinned at `2a2460544cb5cde1f7562cb52aae4ece898c3562`
- Key Cymatix commits: [packet session controls](https://github.com/mbachaud/Cymatix-Context/commit/ecb4276), [closed-loop coordinator](https://github.com/mbachaud/Cymatix-Context/commit/3a1a88f), [SCBench adapter](https://github.com/mbachaud/Cymatix-Context/commit/090518d), [paired runner](https://github.com/mbachaud/Cymatix-Context/commit/973ad19), [analysis gates](https://github.com/mbachaud/Cymatix-Context/commit/c376fd8), [strict receipts](https://github.com/mbachaud/Cymatix-Context/commit/c7b2e49), [real qualification](https://github.com/mbachaud/Cymatix-Context/commit/a68e476), [frozen-input validation](https://github.com/mbachaud/Cymatix-Context/commit/03aaf06), and [historical replay compatibility](https://github.com/mbachaud/Cymatix-Context/commit/0b125ea)
- Strict qualification pair receipt SHA-256: `4fd6d88a61f093acdb3949d4c68ee949c7f11be540d1ea6701ffb3802f6bdca7`
- Qualification operational receipt SHA-256: `30e42d0f606f2e5a0c5af38a802449c7d9c9ea4e0811340799fe11cbcbb55f54`

## Frozen pilot

The easy stratum is `file-backup`, `code-search`, and `execution-server`; medium is `database-migration`, `trajectory-api`, and `layered-config-synthesizer`; hard is `dag-execution` and `recli`. The pinned catalog contains 40 checkpoints per arm and 32 checkpoint-2+ primary observations per arm. Replicate 1 runs control then Cymatix; replicate 2 reverses the order. Runs are sequential.

Codex is `codex-cli 0.147.0`, model `gpt-5.6-luna`, reasoning effort `medium`, authenticated only through the ChatGPT subscription. Both Docker arm images are pinned to `sha256:8bd6d4f3c22131d3df70631b0aa8e12813781812413a034c552466ef5fee39f2` on Docker Engine `29.6.2`.

The treatment is deliberately model-free: SPLADE and ribosome are disabled even though shipped Cymatix defaults enable SPLADE and dense embedding. The resulting headline measures the frozen FTS5 + cymatics closed loop, not Cymatix-as-shipped.

The manifest freezes seed `20260808`, all eight graduation gates, and symmetric retry semantics. Operational failures invalidate the checkpoint pair and require a clean paired rerun; subscription rate limits pause the campaign; failed attempts and clean workspace/genome state must be receipted; favorable-attempt selection is prohibited.

`preflight.json` is a mutable, local execution-authorization receipt and is
ignored by Git. It must be regenerated after the campaign metadata commit and
immediately before any scored command; the committed manifest remains the
immutable input.

## Execution checklist

- [x] Verify the fork contract and pin reasoning effort in the rendered Codex command.
- [x] Complete the non-scored real qualification pair and strict receipt replay.
- [x] Independently recount the eight pinned problem configs as 40 total / 32 primary checkpoints per arm.
- [x] Pin matching control and treatment Docker image digests.
- [x] Run pilot preflight and the final contract/config/qualification tests.
- [x] Obtain operator confirmation of the frozen campaign commit, fork commit, Docker digest, projected subscription usage, and runtime.
- [ ] Execute the 16 order-balanced problem/replicate pairs without tuning.
- [ ] Verify all receipts, run the predeclared paired analysis, and record one graduate/stop decision.

## Usage and runtime projection

The one-pair qualification consumed 3,772,264 input tokens (including 3,450,880 cache-read tokens), 76,595 output tokens, 55.63 minutes wall time, and a model-price proxy of $1.8709642 across eight checkpoint calls. Scaling by the pilot's 160 checkpoint calls projects approximately 75.45 million input tokens, 69.02 million cache-read tokens, 1.53 million output tokens, 18.54 hours sequential wall time, and a $37.42 model-price proxy. This is a planning projection from one non-scored pair, not an API charge: execution uses the ChatGPT subscription and is subject to its usage limits.

## Tracking

Campaign issue: [#359](https://github.com/mbachaud/Cymatix-Context/issues/359).
No GitHub Project is used for the pilot.

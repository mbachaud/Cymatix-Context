# Benchmark baselines ledger

One row per measurement campaign. **Rules (from the 2026-08-09 receipt
invalidations):** compare arms only within their own row; measurement runs
against a frozen tag, never a moving branch; a receipt without a row here is
not comparable to anything.

## Standard bench profile (decision 2026-08-11)

Future testing runs the GPU encoder daemon ON with per-box worker counts.
Shipped defaults stay untouched (daemon off, workers 2); default flips remain
receipt-gated PRs.

| box | daemon | workers | receipt |
|---|---|---|---|
| max rig (8C/16T x86) | ON (`CYMATIX_ENCODER_URL=http://127.0.0.1:11439`) | 4 | spark-erb-receipts 0015 / `61ea459`: scaling only unlocks with daemon ON, x4 sweet spot |
| Joe GB10 Grace (e92c) | ON | 2 | spark-erb-receipts 0018: x4/x8 does NOT reproduce on Grace; ON beats OFF 1.16-1.31x; w2 c-curve positive to ~3.9 qps |

Exceptions: (a) SCBench model-free treatment arms — SPLADE/dense disabled, the
daemon has nothing to encode; (b) #339 determinism cards pin their own knob
values; (c) any arm at c>1 keeps the recall validity gate (one-sided recall
wobble, spark-erb-receipts 0018 gap G5, is unresolved).

## Campaigns

| campaign | cymatix commit | tag | scbench fork commit | manifest sha256 | config deltas from shipped defaults | status | receipts |
|---|---|---|---|---|---|---|---|
| 2026-08-08-codex-pilot ("luna") | `0b125eab` | `bench/scbench-luna-2026-08-08` | `2a246054` (branch `cymatix/codex-ab`) | `6a676d6405ededc535c6ee18d2ccfb14128efc7c94cfffab0951681dfba2d983` | model-free treatment: SPLADE off, ribosome off; codex-cli 0.147.0, model gpt-5.6-luna, effort medium; docker `sha256:8bd6d4f3c22131d3df70631b0aa8e12813781812413a034c552466ef5fee39f2`; seed 20260808 | complete | issue #359; `benchmarks/scbench/campaigns/2026-08-08-codex-pilot/` |
| 2026-08-11-codex-terra | `9312cde` (execution tree; manifest implementation pin `aef12732`) | `bench/scbench-terra-2026-08-11` | `bb40ec5e` (branch `cymatix/codex-ab`) | `ae88fa1d8272ec920d5e43f2d1443d3483e8b20a1459a2d4bd2cac983ab50dfa` | model gpt-5.6-terra, effort medium, docker `sha256:8bd6d4f3c22131d3df70631b0aa8e12813781812413a034c552466ef5fee39f2`, seed 20260808; unchanged from luna except model; from `benchmarks/scbench/campaigns/2026-08-11-codex-terra/README.md` and manifest | in flight | `benchmarks/scbench/campaigns/2026-08-11-codex-terra/` |
| codex-sol (planned) | — | — | — | — | — | reserved (weekly-usage window permitting) | — |

## Code pins

Board cards in cc-exchange pin `v0.8.6` -- that is a CODE pin (the tree you
run), not a campaign row. v0.8.6 is the verified-clean venv on the
collaborator's box (spark-erb-receipts 0018 item 1). Campaign rows above
freeze measurement baselines; code pins freeze the tree a card executes. A
card's receipts cite both: its code pin and, when comparing to a campaign,
that campaign's row.

Notes: the terra manifest pins `aef12732` as the frozen implementation
baseline; the run executes from `9312cde` (tree verified clean,
`git describe` = `v0.8.6-104-g9312cde`). Both are recorded; the tag anchors
the execution tree. A/B campaign rows (`claude/ab-data-cymatics-pki-dff9c6`)
may be added later.

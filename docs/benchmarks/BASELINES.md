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
wobble, spark-erb-receipts 0018 gap G5, is unresolved); (d) default-path
release receipts (v0.9.0 gate, #377) run shipped defaults by construction —
daemon OFF, workers 2 — because they certify what an untouched install does.

## Campaigns

| campaign | cymatix commit | tag | scbench fork commit | manifest sha256 | config deltas from shipped defaults | status | receipts |
|---|---|---|---|---|---|---|---|
| 2026-08-08-codex-pilot ("luna") | `0b125eab` | `bench/scbench-luna-2026-08-08` | `2a246054` (branch `cymatix/codex-ab`) | `6a676d6405ededc535c6ee18d2ccfb14128efc7c94cfffab0951681dfba2d983` | model-free treatment: SPLADE off, ribosome off; codex-cli 0.147.0, model gpt-5.6-luna, effort medium; docker `sha256:8bd6d4f3c22131d3df70631b0aa8e12813781812413a034c552466ef5fee39f2`; seed 20260808 | complete | issue #359; `benchmarks/scbench/campaigns/2026-08-08-codex-pilot/` |
| 2026-08-11-codex-terra | `9312cde` (execution tree; manifest implementation pin `aef12732`) | `bench/scbench-terra-2026-08-11` | `bb40ec5e` (branch `cymatix/codex-ab`) | `ae88fa1d8272ec920d5e43f2d1443d3483e8b20a1459a2d4bd2cac983ab50dfa` | model gpt-5.6-terra, effort medium, docker `sha256:8bd6d4f3c22131d3df70631b0aa8e12813781812413a034c552466ef5fee39f2`, seed 20260808; unchanged from luna except model; from `benchmarks/scbench/campaigns/2026-08-11-codex-terra/README.md` and manifest | in flight | `benchmarks/scbench/campaigns/2026-08-11-codex-terra/` |
| codex-sol (planned) | — | — | — | — | — | reserved (weekly-usage window permitting) | — |
| 2026-08-14-encoder-isolation | `d6b25d3` / `fe9a74d` / `6eb393e` / `8a08770` / `f7e62e2` (per-receipt `git_sha`; campaign-branch shas, merged to master via the #363–#369 stack — **no frozen tag exists**, recorded retroactively per the rule this row satisfies) | — | — | — | base `benchmarks/dogfood/erb/configs/enc_all_off.toml` (dense/SPLADE/rerank/PKI all off) + one-layer-on arms; beds erb_100k/250k/500k carves (n=30) + 829k blob and nosplade (n=30 probe, n=469 full); `CYMATIX_DISABLE_LEARN=1`, no daemon; k=12 | complete — basis caveat: doc tables publish `fr@12` (final-order), the delivered basis is `delivered_gold_rate` in the receipts (#375) | `benchmarks/dogfood/erb/receipts/enc_iso_*.json`; `docs/benchmarks/2026-08-14-encoder-isolation-scale-curve.md` |
| 2026-08-17-postflip-remeasure | `0098849` (master tip at run time; worktree verified clean) | `bench/postflip-2026-08-17` | — | — | **none — shipped `cymatix.toml` defaults** (dense/SPLADE/rerank off, PKI on, RRF, daemon off per exception (d)); `CYMATIX_DISABLE_LEARN=1`; cold start + server scaling on dogfood bed (identity `13f8bbe3…`, ports 11491/11492); 829k confirm on `erb_829k_nosplade.db`, arms baseline + `no_pki`, n=469, k=12, delivered basis included | complete — server-scaling c=2 failed overlap gate, c=8/16 failed recall gate (G5 wobble): c≥8 cells not citable | `benchmarks/dogfood/cold_start_postflip.json`; `benchmarks/dogfood/server_scaling_postflip.json`; `benchmarks/dogfood/erb/receipts/postflip_default_confirm_829k.json`; summary: issue #377 |
| 2026-08-19-know-abstain-sanity | `710f792` (branch `claude/know-abstain-sanity-receipt` = master `bcdd1a1` + the replay driver; 829k chunks ran with the driver's merge-helper fix still uncommitted — measurement path byte-identical) | — | — | — | baseline arm = shipped defaults (dense/SPLADE/PKI/rerank off, RRF; daemon off per exception (d)) + comparison arms `preflip_dense_splade` (dense+SPLADE re-enabled) and `preflip_full` (dense+SPLADE+PKI = the exact pre-2026-08-15 read path); beds `erb_100k.db` (n=141, full carve set, 3 arms) + `erb_829k_nosplade.db` (n=100 deterministic subsample, seed 42, shipped arm only); `CYMATIX_DISABLE_LEARN=1`, `read_only=True` + `ignore_delivered=True`, k=12; chunked foreground runs recombined via the driver's `--merge` (each chunk pays its own untimed warmup; per-chunk bed_bytes drift = health_log appends, recorded in the receipt's `chunks` block) | complete — verdicts on issue #377: empty-window class 2/141 (did not grow; pre-flip arm reproduces 2/141), abstain 5/141 vs 2/141 (not significant, set rotates with the pool), know emit 0/141 vs 23/141 (**CONCERN**: `[know]` calibration coverage-dead on the shipped path — `lexical_dense_agree` structurally never fires, ceiling ≈0.31 < emit_floor 0.45; feeds #287) | `benchmarks/dogfood/erb/receipts/postflip_know_abstain_sanity_100k.json`; `benchmarks/dogfood/erb/receipts/postflip_know_abstain_sanity_829k.json`; tool `benchmarks/dogfood/erb/know_abstain_replay.py`; summary: issue #377 |

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

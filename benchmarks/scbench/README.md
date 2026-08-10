# SCBench Codex/Cymatix campaigns

This directory contains frozen manifests and append-only evidence for paired
SCBench campaigns. The qualification campaign is explicitly non-scored and
must never be pooled with pilot observations.

## Phase 0 qualification

The local qualification ladder covers:

- subscription, Codex version, Docker, commit, contract, and problem preflight;
- checkpoint context reset with persistent workspace state;
- initial and incremental source ingest, deletion tombstones, and failed-write
  detection;
- conversation retrieval after reset, deterministic packet rendering, and
  hidden evaluation-data exclusion;
- untimed warm-up, isolated genome/session state, prompt receipt replay, and
  pair-level rate-limit invalidation.

The in-process closed-loop test uses
`benchmarks/scbench/fixtures/qualification`. Run it with:

```powershell
python -m pytest tests\test_scbench_e2e.py -q -k closed_loop
```

The real qualification pair uses `file-merger`, ChatGPT subscription
authentication, and replicate 2's treatment-first order. The checked-in run
was executed with:

```powershell
$manifest = 'benchmarks/scbench/campaigns/qualification/manifest.json'
$cymatixRoot = 'ABSOLUTE_PATH_TO_CYMATIX_WORKTREE'
$scbenchRoot = 'ABSOLUTE_PATH_TO_SCBENCH_FORK'
python -m cymatix_context.integrations.scbench.cli preflight --manifest $manifest --cymatix-root $cymatixRoot --scbench-root $scbenchRoot
python -m cymatix_context.integrations.scbench.cli run-pair --manifest $manifest --problem file-merger --replicate 2 --cymatix-root $cymatixRoot --scbench-root $scbenchRoot
python -m cymatix_context.integrations.scbench.cli verify-receipts --manifest $manifest --cymatix-root $cymatixRoot --scbench-root $scbenchRoot
```

The 2026-08-10 qualification evidence is under
`benchmarks/scbench/campaigns/qualification/pairs/file-merger-r2`. Both arms
completed on attempt 001 and the verifier replayed eight terminal checkpoint
receipts. The strict pair receipt SHA-256 is
`4fd6d88a61f093acdb3949d4c68ee949c7f11be540d1ea6701ffb3802f6bdca7`.

Measured operational behavior:

- pair wall time was 3,337.8 seconds (55m37.8s); treatment was 1,674.620
  seconds and control was 1,643.883 seconds including evaluation;
- Codex inference totaled 904.231 seconds for treatment and 866.307 seconds
  for control;
- the empty-workspace initial sync was verified in 19 ms and untimed warm-up
  was 0 ms; per-checkpoint treatment sync latency was 1,815, 1,007, 1,108,
  and 1,527 ms;
- retrieval latency for checkpoints 2-4 was 1,777, 140, and 171 ms; rendered
  packets were 3,715, 3,477, and 3,840 tokens against the 4,000-token ceiling;
- checkpoint 1 prompts shared SHA-256
  `fe1cfde5256e8e2b494b35f3a7f5235c3c20c21b9a39485f65126cfe71c1e1df`;
  checkpoint 2 became byte-for-text equal to control after removing its one
  Cymatix block;
- deletion-filter observations were zero, all sync verifications passed, and
  treatment ingest/query/packet artifacts contained no evaluation path,
  `evaluation.json`, or hidden-data marker;
- Codex emitted zero rate-limit events, so no subscription-window pause was
  required.

These are qualification observations, not scored results, and must not be
pooled with the pilot. Receipt hashes, not this prose summary, are the evidence
of record. A rate-limit response invalidates the entire pair and requires a
fresh pair attempt after the subscription window resets.

## Treatment scope

The sidecar explicitly disables SPLADE, dense embedding on ingest, dense
retrieval, and the ribosome. These are enabled in the shipped Cymatix defaults.
Accordingly, campaign results measure the approved model-free FTS5 + cymatics
treatment, not Cymatix-as-shipped. Every campaign receipt and report must retain
that qualification.

`cymatix_commit` pins the implementation baseline. Because a committed manifest
cannot self-reference its own Git commit, preflight permits later commits only
when every changed path is the active campaign directory or this README; it
also records the exact observed HEAD and rejects any subsequent drift.

## Frozen eight-problem pilot

The scored pilot inputs are frozen in
[`campaigns/2026-08-08-codex-pilot/manifest.json`](campaigns/2026-08-08-codex-pilot/manifest.json),
with the operator checklist and usage projection in its
[`README.md`](campaigns/2026-08-08-codex-pilot/README.md). Preflight
independently recounts the pinned SCBench catalog and rejects any drift from 40
checkpoints per arm and 32 checkpoint-2+ primary checkpoints per arm. Scored
`run-pair` execution remains blocked pending explicit operator confirmation.

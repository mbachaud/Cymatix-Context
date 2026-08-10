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

The real qualification pair uses the `calculator` problem and ChatGPT
subscription authentication:

```powershell
python -m cymatix_context.integrations.scbench.cli preflight --manifest benchmarks/scbench/campaigns/qualification/manifest.json
python -m cymatix_context.integrations.scbench.cli run-pair --manifest benchmarks/scbench/campaigns/qualification/manifest.json --problem calculator --replicate 1
python -m cymatix_context.integrations.scbench.cli verify-receipts --manifest benchmarks/scbench/campaigns/qualification/manifest.json
```

Receipts live under `benchmarks/scbench/campaigns/qualification`; their hashes,
not mutable prose summaries, are the evidence of record. A rate-limit response
invalidates the entire pair and requires a fresh pair attempt after the
subscription window resets.

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

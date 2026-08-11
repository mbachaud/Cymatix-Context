# SCBench Regression V2 and Terra Replication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an auditable paired-regression v2 analysis, preserve Luna v1 evidence, freeze a Terra campaign, and launch it through the subscription-backed harness.

**Architecture:** Extend the existing official-result loader with raw regression counters while leaving v1 computation untouched. Add separate v2 summary, bootstrap, rendering, and CLI paths, then create a new Terra campaign and a fail-closed continuation supervisor that invokes v2 analysis.

**Tech Stack:** Python 3.14, Pydantic, pytest, PowerShell, SCBench, Codex CLI subscription authentication, Docker.

## Global Constraints

- Preserve all Luna pair receipts and `analysis.json`/`analysis.md` unchanged.
- Use direct `regression_passed / regression_total` on checkpoints 2+.
- Bootstrap whole problem trajectories with seed `20260808` and 10,000 iterations.
- Use GPT-5.6 Terra with reasoning effort `medium` and ChatGPT subscription auth.
- Keep one worker, sequential evaluation, AB/BA ordering, and the qualified Cymatix treatment.
- Stop fail-closed on any invalid arm, pair, receipt, version, or configuration drift.

---

### Task 1: Load direct regression counters

**Files:**
- Modify: `cymatix_context/integrations/scbench/analysis.py`
- Test: `tests/test_scbench_analysis.py`

**Interfaces:**
- Produces: `CheckpointResult.regression_passed: int | None` and `CheckpointResult.regression_total: int | None`.

- [ ] Add a failing loader test for preserved regression counters and invalid counter shapes.
- [ ] Run the focused test and confirm the expected failure.
- [ ] Implement minimal counter loading without changing v1 event computation.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Compute and render v2 analysis

**Files:**
- Modify: `cymatix_context/integrations/scbench/analysis.py`
- Modify: `cymatix_context/integrations/scbench/cli.py`
- Test: `tests/test_scbench_analysis.py`
- Test: `tests/test_scbench_cli.py`

**Interfaces:**
- Produces: `compute_paired_regression_v2(dataset)`, `clustered_bootstrap_v2(dataset)`, `analyze_campaign_v2(manifest_path)`, and CLI command `analyze-v2`.

- [ ] Add a failing test reproducing the isolated-score eligibility artifact.
- [ ] Add failing tests for direct paired deltas, denominator integrity, deterministic clustered intervals, and distinct v2 artifact names.
- [ ] Run the focused tests and confirm each fails for missing v2 behavior.
- [ ] Implement the smallest separate v2 models and functions; do not alter v1 outputs.
- [ ] Add CLI dispatch and gate handling for `analyze-v2`.
- [ ] Run analysis and CLI tests, then the full SCBench harness test subset.

### Task 3: Recompute Luna as labeled post-hoc sensitivity analysis

**Files:**
- Preserve: `benchmarks/scbench/campaigns/2026-08-08-codex-pilot/analysis.json`
- Preserve: `benchmarks/scbench/campaigns/2026-08-08-codex-pilot/analysis.md`
- Generate: `benchmarks/scbench/campaigns/2026-08-08-codex-pilot/analysis-v2.json`
- Generate: `benchmarks/scbench/campaigns/2026-08-08-codex-pilot/analysis-v2.md`

- [ ] Hash the v1 artifacts before recomputation.
- [ ] Run `analyze-v2` against stored verified receipts.
- [ ] Confirm v1 hashes are unchanged and v2 reports direct regression counters.

### Task 4: Freeze Terra model and campaign configuration

**Files:**
- Create in SCBench fork: `configs/models/gpt-5.6-terra.yaml`
- Create: `benchmarks/scbench/campaigns/2026-08-11-codex-terra/manifest.json`
- Create: `benchmarks/scbench/campaigns/2026-08-11-codex-terra/README.md`
- Create: `benchmarks/scbench/campaigns/2026-08-11-codex-terra/.gitignore`
- Test: `tests/test_scbench_config.py`

- [ ] Add a failing manifest test asserting Terra, medium reasoning, AB/BA ordering, and frozen counts.
- [ ] Add the Terra model configuration with explicit `thinking: medium`.
- [ ] Commit the SCBench fork configuration and pin its commit in the Terra manifest.
- [ ] Add the Terra campaign files and make the manifest test pass.
- [ ] Commit the Cymatix freeze so preflight can record a clean immutable head.

### Task 5: Qualify and dispatch Terra

**Files:**
- Create outside the repository: `scbench-terra-supervisor.ps1`
- Generate locally: Terra `preflight.json`, state, log, pairs, and analysis artifacts.

- [ ] Run a minimal `codex exec` Terra subscription-auth smoke check at `medium`.
- [ ] Run the full Terra preflight and require every check to pass.
- [ ] Syntax-check the fail-closed background supervisor.
- [ ] Launch it hidden and record its PID.
- [ ] Verify the first scored pair has entered the Codex/Docker execution path.


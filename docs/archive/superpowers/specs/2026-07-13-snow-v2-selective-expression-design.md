# SNOW V2 Selective-Expression Benchmark Design

**Date:** 2026-07-13

**Status:** Approved for implementation planning
**Tracks:** GitHub issue #208; supersedes the fixture assumptions in the
2026-06-10 SNOW-2 re-spec while preserving its injection-versus-navigation
question.

## Summary

SNOW V2 measures whether an agent can use Helix as a context runtime rather
than as a one-shot retrieval layer. It asks three linked questions:

1. Does Helix selectively express knowledge from a second project when that
   knowledge is useful, while suppressing it when it is irrelevant?
2. When initial context is insufficient, can the model recognize uncertainty
   and use Helix's information actions to recover a correct result?
3. What do those additional lookups cost in tokens, latency, tool calls, and
   model spend, and how often do they damage an answer that was already right?

The reference fixture is one blob-mode genome built from pinned Apache-2.0
snapshots of:

- `mbachaud/helix-context` at
  `a51aa17498f68dce1b8b83ef297ce0c4ad225e3c`
- `mbachaud/MaxExpressKit` at
  `12afacda843932eeeeee83cd28899b15e2e76eb7`

The task and corpus schemas use the generic roles `primary` and `expressive`.
The reference suite maps Helix Context to `primary` and MaxExpressKit to
`expressive`; a reproducer may substitute other repositories or data without
changing the runner.

SNOW retains its original meaning, **Scale-invariant Navigation on Organic
Webs**, but V2's headline is a cost-of-correctness frontier over public serving
actions rather than an internal tier-cascade score.

## Goals

- Compare one-shot delivery with model-directed and confidence-gated
  multi-pass navigation on the same tasks, fixture, and model.
- Measure selective cross-project expression separately from answer quality.
- Measure recovery, regression, uncertainty calibration, tool selection, tool
  waste, session elision, and marginal cost.
- Support both read-only answers and sandboxed-output tasks.
- Publish a fixed, reproducible reference suite while allowing versioned custom
  corpora and task packs.
- Preserve every attempt and prevent missing cells, fixture mutation, or
  post-result question authoring from improving the apparent score.

## Non-goals

- Full HTTP-versus-CLI-versus-MCP performance comparison. The main benchmark
  holds transport constant on MCP; HTTP and CLI receive contract/parity smoke
  coverage elsewhere.
- A global claim that graph structure is beneficial. Graphs and neighbors are
  optional information actions, not Helix's product identity.
- Retuning retrieval, know-confidence, or escalation thresholds during a run.
  The reference result characterizes the pinned serving stack as shipped.
- Proving causal use of delivered evidence. The benchmark reports delivery and
  attributed use; causal-use claims require a separate intervention study.
- Distributing private corpora, credentials, approvals, or lab-specific paths.

## Reference Baseline and Known Constraints

The initial serving stack must be at or after Helix commit `a51aa17`, which
contains the graduated `scale_relative` blend default and the default-inert
rank-gated RRF controls. Every run receipt records the exact serving commit,
resolved configuration, fixture hashes, and model identifiers. A later serving
commit may be tested, but results from different receipts are not presented as
the same baseline.

The shipped know-confidence gate is not currently agent-trustworthy. Issue
#239 and draft PR #281 show unstable held-out AUC, an inverted top-score
coefficient, and near-zero KnowBlock emission. SNOW V2 therefore treats the
shipped gate as a subject of measurement, not as a prerequisite:

- Policy D is named `confidence_hybrid_shipped`.
- If it escalates nearly every task, that is a valid negative result and its
  cost is reported.
- A future `confidence_hybrid_refit` is a separate versioned arm and is allowed
  only after a preregistered multi-seed or multi-fold validation clears its AUC
  floor.
- SNOW V2 does not hardcode replacement thresholds and does not wait for issue
  #205.

MCP already exposes context, packet, fingerprint, neighbors, and gene/document
get. The HTTP `/context/expand` route has no equivalent MCP action. A thin,
tested MCP wrapper is benchmark-enabling scope; the runner never silently
switches transport to HTTP.

## Experimental Policies

The primary experiment has four policies. All policies see the same prompt and
run against the same fixture and model role.

| Policy | Initial action | Additional actions | Purpose |
| --- | --- | --- | --- |
| A `context_one_shot` | `helix_context` | None | Current one-shot injection baseline |
| B `packet_one_shot` | `helix_context_packet` | None | Isolate the value of the structured packet and know/miss contract |
| C `model_agentic` | `helix_fingerprint` | Model-selected Helix actions | Measure self-directed navigation and stopping |
| D `confidence_hybrid_shipped` | `helix_context_packet` | Unlocked when Helix reports miss/low confidence or the model declares insufficient confidence | Measure the shipped confidence policy, including over-escalation |

A and B make exactly one MCP call. C and D make their initial call plus at most
four additional information actions, for a hard maximum of five calls per
task. Early stopping is allowed and encouraged.

The C/D action palette is:

- `helix_context`
- `helix_context_packet`
- `helix_fingerprint`
- `helix_neighbors`
- `helix_gene_get` or its canonical document alias
- the new MCP wrapper for `/context/expand`

Tool names are recorded exactly, including aliases, so the report can show
what the model actually selected without collapsing distinct routes.

## Session Delivery and Elision

Native session-delivery elision is part of the primary C/D treatment. Each run
uses a unique session namespace derived from:

`suite_id × task_id × policy × model_role × attempt_id`

Calls within one C/D task reuse that session where the MCP action supports a
session ID; state never crosses tasks, policies, models, or attempts. The trace
records elided gene IDs, stub counts, and estimated tokens saved.

A preregistered diagnostic repeats a balanced subset with a fresh session ID
per hop. This disables effective elision for context actions without changing
transport. The paired result measures whether elision saves cost, loses useful
evidence, or changes recovery. Actions without session semantics are reported
as such rather than treated as elision-capable.

## Execution Profiles

Every task supports one or both of these profiles:

### `read_only`

The model returns an answer, abstention, plan, review, or proposed change with
evidence attribution. It cannot mutate a repository or invoke side-effecting
operations.

### `sandboxed_output`

The model receives a credential-free disposable checkout. It may edit only
allowlisted paths and run allowlisted local commands. Network access is off by
default. It cannot commit, push, deploy, access credentials, change the source
fixture, or write outside the sandbox. The harness captures the final diff,
command log, test results, and boundary violations.

All reference tasks run read-only. A fixed subset also runs as sandboxed
output, pairing advice and action under the same task ID and evidence gold.

## Task Suite

### Corpus roles and task classes

The suite is organized into scenario families for three expressive domains:

- compliance
- drift
- ledger

Each scenario family has four matched task classes:

| Class | Required corpus role | Measurement |
| --- | --- | --- |
| `primary_only` | `primary` | Does the expressive corpus remain quiet? |
| `expressive_only` | `expressive` | Can the model retrieve the guardrail directly? |
| `cross_project` | both | Is expressive knowledge introduced and correctly applied to primary-project work? |
| `near_miss_control` | `primary`; expressive material should be suppressed | Does vocabulary matching cause false expression? |

The reference pack contains 48 frozen scenario families: 16 compliance, 16
drift, and 16 ledger. Four task variants per family produce 192 total tasks and
16 tasks in every `task class × guardrail` cell.

### Two-stage scale rule

All 192 tasks and their gold labels are authored, reviewed, hashed, and frozen
before scored inference begins.

- **Stage 1:** 12 scenario families, four per guardrail, produce 48 tasks and
  four tasks per scoring cell.
- **Expansion gate:** Policy C on the `reference_local` model must complete all
  48 cells and achieve a primary macro task-success score strictly greater
  than `0.50`.
- **Stage 2:** if the gate passes, run the remaining 144 tasks and report the
  full 192-task suite. A score of exactly `0.50` does not expand.

The Stage-1 families are a preregistered stratified subset of the full pack.
The remaining 144 tasks are never authored or edited in response to Stage-1
outcomes. Successful Stage-1 traces are reused in the 192-task aggregate when
the manifest, fixture, model, and policy receipts are identical.

Sandbox eligibility scales from 12 tasks in Stage 1 to 48 in the full suite.
The Stage-1 subset contains six cross-project tasks and their six matched
near-miss controls, balanced two pairs per guardrail. The full subset contains
24 such matched pairs, eight per guardrail.

### Answerability and intended navigation difficulty

Within each 12-task class slice of Stage 1:

- eight tasks are answerable;
- two are deliberately ambiguous;
- two are genuinely unanswerable from the pinned genome;
- four answerable tasks are intended for direct delivery;
- four answerable tasks are intended to reward navigation.

The full pack preserves these proportions. Intended difficulty is an authored
stratum, not an assertion about actual retrieval. Preflight records observed
route availability separately, and the report shows when a nominal navigation
task was already solved by one-shot delivery.

### Task record contract

Each versioned JSONL task record contains:

- stable suite, family, and task IDs;
- prompt and permitted execution profiles;
- guardrail and task class;
- corpus-role expectation: `primary`, `expressive`, `both`, or
  `expressive_suppressed`;
- answerability and intended difficulty;
- required evidence as one or more alternative valid evidence sets;
- source path, symbol or heading, source commit, and content hash;
- atomic required claims and prohibited claims or actions;
- expected uncertainty behavior;
- for sandbox tasks, setup state, allowed write roots, prohibited paths,
  verification commands, and deterministic assertions.

Line numbers may be included for convenience but are not the primary identity.
Pinned commit plus path, symbol/heading, and content hash form the stable gold.

Before inference, preflight resolves every gold source to gene IDs and records
which allowed MCP actions can reach it. This derived route-availability map is
stored in the run receipt, not authored into the task gold, so it cannot
prescribe the model's tool choice. Any unresolved gold aborts before model
calls; tasks are never silently dropped.

### Custom corpora and task scaling

Corpus and question scale are independent:

- A fixed task pack may run against source-only, distractor-augmented, or
  user-supplied genome manifests.
- A fixed corpus may run a smoke subset, Stage 1, the full pack, selected task
  IDs, selected strata, or additional versioned custom packs.

An authoring utility may propose questions from a custom corpus, but generated
questions cannot enter a scored run until evidence resolves, gold validates,
and the task pack receives a version and content hash. Every result carries
both `corpus_manifest_hash` and `task_pack_hash`.

## Model Roles

The initial ladder follows issue #208:

- `reference_local` maps to `qwen3:8b`. Preflight resolves and records the
  installed model digest. Policy C on this role controls Stage-2 expansion.
- `frontier_corroboration` maps to one explicitly configured frontier model.
  Its provider, exact model or snapshot ID, generation settings, and pricing
  table version are pinned in the run manifest. It never changes the expansion
  decision.
- `judge` is a separately pinned model role used only where deterministic
  scoring cannot resolve an atomic rubric item.

Generation temperature is zero where supported. Unsupported determinism
controls are recorded rather than emulated. Results are reported per model;
the benchmark does not merge local and frontier scores.

## Scoring

### Primary task-success score

Each task receives binary `task_success`.

- An answerable read-only task succeeds when all critical required claims pass,
  prohibited claims are absent, and required evidence attribution is valid.
- An ambiguous or unanswerable task succeeds when the model abstains or
  qualifies appropriately and identifies the missing or conflicting evidence.
- A sandboxed task succeeds when required assertions and tests pass, changes
  stay within allowed paths, and no prohibited action occurs.

The primary macro score is:

`mean(mean(task_success in cell) for each task_class × guardrail cell)`

All 12 cells receive equal weight. This is the score used by the 50% expansion
gate and prevents easy primary-only tasks from hiding poor cross-project
behavior.

Deterministic checks run first. The judge sees blinded policy and model labels
and scores only semantic rubric items that remain unresolved. Raw judge output
is cached. A stratified 20% sample receives an independent second judgment, and
the report publishes agreement and resolution rates.

### Initial and final checkpoints

After initial delivery, C and D record a structured checkpoint:

- provisional answer or abstention;
- numeric model confidence and categorical sufficient/insufficient decision;
- evidence IDs attributed;
- whether another lookup is requested;
- selected next action and rationale category.

The final checkpoint uses the same answer and attribution schema. This yields:

- recovery rate: initial failure to final success;
- regression rate: initial success to final failure;
- unnecessary escalation: initial success followed by more calls;
- failed escalation: more calls without recovery;
- stopping quality: correct stop versus premature or wasteful continuation.

### Selective-expression metrics

Selective expression is reported separately from correctness:

- expressive-source recall when expressive evidence is required;
- false-expression rate when expressive evidence should be suppressed;
- expressive-source token share;
- required-evidence delivery rate;
- evidence-attribution rate.

Evidence attribution is not labeled causal use. A model may cite or rely on a
source without the benchmark claiming a mechanistic causal effect.

### Confidence metrics

Helix confidence and model self-confidence are evaluated independently against
two labels:

1. required evidence was present after initial delivery;
2. the provisional answer was correct.

For each label, report AUC, Brier score, calibration error, risk-versus-coverage,
escalation precision, escalation recall, and over-escalation on initially
correct tasks. Retrieval rank, delivery, answer correctness, and causal use are
never treated as interchangeable labels.

### Tool and cost metrics

Every information action records:

- exact route and normalized action family;
- inputs and result status;
- returned and attributed evidence IDs;
- input/output tokens where reported;
- Helix context characters and estimated tokens;
- wall time;
- provider errors and retries;
- estimated model cost under the pinned pricing receipt.

Aggregates include route utilization, average and distributional hop count,
tokens per recovery, latency per recovery, recovery per dollar, calls that add
new gold evidence, and calls that add cost without changing evidence or outcome.

Session-elision reports include elided genes, stubs, tokens saved, and paired
elision-on/off quality and recovery deltas.

The headline is a cost-of-correctness frontier. SNOW V2 does not collapse
quality, latency, tokens, and dollars into one opaque composite score.

## Architecture

The implementation is divided into seven components with stable contracts:

1. **Suite loader** validates corpus manifests, task packs, hashes, stage rules,
   policies, and model roles.
2. **Fixture builder** checks out pinned sources and builds or verifies the
   combined blob genome.
3. **Gold preflight** resolves every task's evidence and computes the derived
   route-availability receipt.
4. **Policy runner** implements A-D, hop budgets, sessions, checkpoints, and
   append-only traces without owning scoring rules.
5. **Model adapters** normalize local Ollama and frontier-provider calls into
   one action, confidence, token, cost, and error schema.
6. **Sandbox executor** creates disposable checkouts and enforces command and
   filesystem boundaries.
7. **Scorer/reporter** consumes immutable traces and judge records without
   rerunning retrieval or answer models.

The scorer is intentionally independent of the runner so metrics can be fixed
or extended without spending model budget again.

## Run Flow

1. Validate source SHAs, task-pack hash, resolved Helix configuration, model
   digests, time/cost caps, and license receipts.
2. Build the canonical blob fixture or verify an existing fixture against its
   manifest.
3. Create a disposable serving copy and start an isolated Helix server with
   `HELIX_DISABLE_LEARN=1`.
4. Verify required MCP actions and resolve all 192 tasks before paid inference.
5. Run Stage 1 across A-D and configured model roles using a seeded balanced
   arm rotation.
6. Evaluate the Policy-C reference-local gate. If it is strictly above 0.50,
   run the remaining 144 tasks.
7. Run the fixed sandbox and elision diagnostic subsets.
8. Score immutable traces, perform semantic adjudication, and produce machine
   and human reports.

Arm order uses a seeded balanced rotation so cache warmth cannot consistently
favor one policy. The actual order is recorded for every task.

## Persistence and Resume

The full cell key is:

`suite × corpus × task × policy × model × execution_profile × attempt`

Traces are append-only. A successful cell is never overwritten. A retry gets a
new attempt ID and preserves the original failure. Resume skips cells with an
accepted terminal attempt and continues only missing or explicitly retryable
cells. Stage-1 traces may be reused in Stage 2 only when all receipt hashes are
identical.

## Failure Semantics

The run fails before inference when:

- a source, configuration, model, or task-pack hash differs from the manifest;
- any gold evidence cannot resolve;
- a required MCP action is absent;
- the disposable server is not using the required learning-disabled setting;
- unknown configuration keys or stale fixture metadata appear.

Provider and MCP calls use explicit timeouts. The reference defaults are 120
seconds for MCP/HTTP operations, 600 seconds for one model call, 300 seconds for
one sandbox command, and 1,800 seconds for one task. A manifest may lower or
raise these values, but the resolved values are part of the receipt.

Transient failures receive bounded retries. Five consecutive provider or
transport failures trip a circuit breaker. Per-call, per-task, and whole-run
token, time, and monetary caps are required manifest values; crossing a cap
stops with a resumable terminal receipt.

No cell disappears from a denominator:

- a persistent infrastructure or model error has `task_success=0` and a
  separate error classification;
- the Stage-2 gate requires all 48 Policy-C reference-local cells to have a
  valid final output after allowed retries;
- an unresolved judge result invalidates the affected aggregate instead of
  being treated as a model failure;
- comparative claims are withheld when any arm's infrastructure-error rate
  exceeds 2%.

At Stage 1, one infrastructure error in a 48-task arm exceeds 2%; at 192 tasks,
at most three infrastructure-error cells remain below the publication limit.
The raw result remains available even when comparative claims are withheld.

Exceptions are caught as `Exception`, logged at warning or error level, and
serialized. Nothing is silently swallowed.

## Fixture Integrity

The canonical genome is never served directly. Each run serves a disposable
copy. Pre/post content fingerprints cover genes and indexed source content;
session-delivery telemetry tables are the only expected mutable state. Gene
count, source-role counts, content hashes, and echo-gene checks are included in
the receipt. Any content mutation invalidates the run.

Sandbox checkouts are separate from both source repositories and the genome.
They are credential-free, network-disabled by default, time-bounded, and
deleted only after their diff and command receipt are durable.

## Verification Strategy

### Unit tests

- corpus and suite manifest validation;
- task schema and alternative gold sets;
- role-expression expectations;
- Stage-1 stratification and strict `> 0.50` gate behavior;
- macro scoring across 12 cells;
- recovery, regression, escalation, and stopping transitions;
- confidence and cost metrics;
- full cell-key and resume decisions;
- sandbox path and command policy.

### MCP contract tests

- context, packet, fingerprint, neighbors, gene/document get;
- the new context-expand wrapper;
- error envelopes, timeouts, and evidence-ID normalization;
- session-ID threading for actions that support it.

### Integration tests

- fake model plus fake MCP server across all four policies;
- interrupted run and idempotent resume;
- cross-arm and cross-task session isolation;
- elision-on/off paired diagnostic;
- judge caching and unresolved-judge invalidation;
- sandbox allowlist and boundary violations;
- fixture content-mutation detection.

### Smoke and acceptance tests

An unpaid smoke suite runs one task from each task class across all four
policies. Full acceptance preflight requires all 192 gold records to resolve,
the Stage-1 subset to preserve full-pack strata, every required action to pass
its contract check, and the canonical fixture to remain unchanged.

Windows subprocesses include
`creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)`. HTTP calls use
explicit timeouts. The implementation uses native Windows Python rather than
`uv run`.

## Reproduction Bundle

Every publishable run contains:

- corpus and suite manifests;
- source SHAs and Apache-2.0 license receipts for the reference fixture;
- corpus, task-pack, configuration, and fixture hashes;
- exact serving commit and resolved configuration;
- model IDs or digests, generation settings, and pricing receipt;
- environment and dependency lock;
- append-only per-cell traces and all attempt records;
- raw and normalized judge records;
- aggregate JSON and CSV;
- a human-readable Markdown report;
- exact build, preflight, run, resume, score, and verification commands.

The public bundle contains no credentials, personal approvals, private contact
details, machine-specific absolute paths, or private-corpus content.

## Reporting Requirements

Reports show, per policy and model:

- primary macro task success with cell breakdowns;
- answerability and task-difficulty strata;
- evidence delivery and selective-expression metrics;
- recovery and regression;
- confidence calibration and risk-coverage;
- route utilization and tool waste;
- token, latency, hop, and cost distributions;
- sandbox correctness and boundary violations;
- session-elision savings and paired quality delta;
- infrastructure and judge error rates;
- the cost-of-correctness frontier.

Stage 1 and Stage 2 are labeled explicitly. A Stage-1-only result is valid and
publishable even when it does not expand; it must not be presented as a
192-task result.

## Implementation Boundary

The first implementation plan covers the benchmark infrastructure, reference
manifests, the complete frozen 192-task pack, the MCP expand wrapper, tests,
and unpaid smoke verification. Paid or multi-hour scored runs are operational
execution after those artifacts pass review. Retrieval retuning, know-gate
refitting, full protocol comparisons, and causal-use instrumentation remain
separate workstreams.

# AssetOpsBench eval-path assessment — NO-GO verdict (desk research)

**Date:** 2026-07-13
**Status:** NO-GO for a helix-context arm against AssetOpsBench as it currently stands. Desk-research verdict; no bench-box time spent.
**Subject:** [IBM/AssetOpsBench](https://github.com/IBM/AssetOpsBench) (Apache-2.0, KDD 2026 / NeurIPS 2025), evaluated as a candidate *agentic* external benchmark to complement ERB (retrieval-only; see `2026-07-10-erb-blob-829k-reproduction.md`).

## TL;DR

AssetOpsBench evaluates **multi-agent tool orchestration over structured data**, not retrieval over documents. There is **no prose corpus anywhere in the system** — nothing for helix to ingest — and the LLM-judge rubric rewards correct tool sequencing, not context quality. A helix arm would have nothing to serve and no scorer dimension that credits it. Both gates from the pre-registered decision rule fail.

## Method

Desk research only (2026-07-12/13):

- Cloned `IBM/AssetOpsBench` @ `f855d4a` and swept scenarios, server code, evaluation code, and docs (two independent structured passes).
- Pulled the authoritative scenario suite from HF `ibm-research/AssetOpsBench` (`data/scenarios/all_utterance.jsonl`, 152 rows) and classified it from primary data.

Pre-registered decision rule (from the internal run-plan): a helix arm is GO only if (a) a textual knowledge corpus exists to ingest, and (b) ≥ ~30 scenarios meaningfully depend on textual knowledge.

## Findings

### 1. Scenario composition (primary data, n=152)

| type | n | | group | n |
|---|---|---|---|---|
| Workorder | 47 | | retrospective | 77 |
| multiagent | 42 | | predictive | 38 |
| TSFM | 23 | | prescriptive | 28 |
| IoT | 20 | | mixed | 9 |
| FMSR | 20 | | | |

Keyword + type screening flags **42** scenarios as "knowledge-ish" (all 20 FMSR, ~12 FMSR-flavored multiagent, ~10 Workorder root-cause/recommendation items). Nominally above the 30-count gate — but see the corpus test.

### 2. Corpus test: FAIL — there is no document corpus

- The FMSR "knowledge base" is a CouchDB store of **failure-mode *name lists*** (`List[str]` keyed by asset class, e.g. `["seal leakage", "impeller wear"]`) — no descriptions, no prose (`src/servers/fmsr/main.py`). FM↔sensor mapping and failure-mode generation are **LLM parametric-knowledge calls with hardcoded prompt templates**, not retrieval.
- The only sentence-level prose in the system is one-line `description` fields in three catalog CSVs with **1 data row each** (`src/couchdb/scenarios_data/shared/catalog/`). Work orders carry ≤100-char descriptions; the long-description field is empty in all fixtures.
- Vibration "knowledge" (ISO 10816 tables, bearing geometry) is hardcoded constants; everything else is sensor time series (CouchDB/JSON).
- No manuals, no maintenance procedures, no failure-mode descriptions, no document store. Nothing downloads a corpus at runtime (no `load_dataset`/`hf_hub_download` anywhere in `src/`). `docs/external-industrial-datasets.md` *proposes* future ingestion of CWRU/SWaT/C-MAPSS but ships nothing and has no scenarios.
- The HF dataset is 10 MB of scenario/ground-truth JSON — definitions, not documents.

So the 42 "knowledge-ish" scenarios are answered from a tiny structured store plus the model's parametric knowledge. **Scenarios that require document retrieval: ≈ 0.**

### 3. Scorer coupling: the judge doesn't credit context quality

`src/evaluation/scorers/llm_judge.py` scores six dimensions — task completion, data-retrieval accuracy (correct asset/time/sensor via tools), result verification, agent/tool sequence order, clarity, hallucination check. Pass = first five all true AND no hallucination. A context layer could only help indirectly, by improving the answer text the judge reads; the `semantic_similarity` scorer that would be the natural home for content-grounding is an unimplemented skeleton (`NotImplementedError`).

### 4. Attaching helix would have been easy — that's not the blocker

For the record (harness mechanics verified in code):

- **LLM-proxy attach, zero code changes:** all runners resolve their base URL from router env vars (`LITELLM_BASE_URL` / `TOKENROUTER_BASE_URL`, `src/llm/routers.py`, `src/llm/litellm.py:54-56`). The Claude Agent SDK runner synthesizes `ANTHROPIC_BASE_URL` for its subprocess from `LITELLM_BASE_URL` (`src/agent/claude_agent/runner.py:44-59`) — pointing that at a local proxy routes the whole agent loop through it.
- **7th MCP server: code edit required.** The six domain servers are hardcoded in `DEFAULT_SERVER_PATHS` (`src/agent/runner.py:16-23`); runner constructors accept a `server_paths` override but no CLI/config path exposes it.
- Infra for a stock run: Python 3.12 + `uv` + Docker CouchDB (iot/wo/vibration/fmsr servers all need it); `max_turns=30`, `max_tokens=2048`/call, judge = 1 call/scenario with 8K-char trajectory cap; self-judging is rejected (`src/evaluation/evaluator.py:82-95`), so Claude-answered runs need a non-Claude judge. The batch suite runner only wires `direct_llm`/`stirrup_agent`/`opencode_agent`; the Claude SDK arm would need a `MethodConfig` addition or a scripted loop.

## Verdict

**NO-GO.** The corpus gate fails outright (nothing to ingest) and the effective knowledge-dependence count is ~0, so the count gate fails too. Running the planned stock spike would not change either fact; the bench-box time is better spent elsewhere.

## What would change the verdict

1. AssetOpsBench ships its proposed external-dataset ingestion (`docs/external-industrial-datasets.md`) **and** authors document-grounded scenarios (manuals, failure-mode descriptions, work-order narratives) whose ground truth requires reading them.
2. Their `semantic_similarity` scorer (or any content-grounding dimension) gets implemented, so context quality is directly scored.
3. Alternative framing outside their scoring: a token-cost study (helix as transparent proxy, measuring tokens/trajectory at equal pass rate) is *technically* trivial via the `LITELLM_BASE_URL` attach — but with no corpus to inject, helix would be measuring pure passthrough. Only worth revisiting alongside (1).

Watch item: revisit if the upstream repo lands external-dataset scenarios (their IJCAI 2026 physics-grounded challenge may drive this).

## Artifacts

- Local clone: `F:\Projects\AssetOpsBench` @ `f855d4a` (not tracked here)
- Scenario suite snapshot: `F:\tmp\assetops_all_utterance.jsonl` (152 rows, from HF `ibm-research/AssetOpsBench`)
- Decision rule + arm design that this verdict retires: internal run-plan of 2026-07-12 (spike + A/B arms on the bench box, now cancelled)

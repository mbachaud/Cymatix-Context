# PRD: Investigate whether SPLADE should remain on by default

**Date:** 2026-05-26
**Author:** Claude Opus 4.7 (with Max)
**Status:** Draft — awaiting design sign-off before any implementation.
**Related:** Issue #159, PR #160 (prefilter ablation data), [[project_helix_109_shard_pressure_test]], [[feedback_helix_ribosome_off]]

---

## 1. Problem

`splade_enabled = true` is the shipped default. The 2026-05-26 prefilter ablation (PR #160 §5) found that on the EnterpriseRAG-Bench Onyx corpus, **SPLADE contributes 0pp to recall@1/3/5/10 and MRR** at every fixture scale we tested, while costing measurable latency:

| Fixture | n | SPLADE-on p95 (T) | SPLADE-off p95 (A) | Δ p95 (T → A) | Δ recall@10 |
| --- | --- | --- | --- | --- | --- |
| Onyx 10K (`enterprise_rag_10k_w16000.db`) | 100 | 2337 ms | **1411 ms** | **−926 ms (~40%)** | 0.0 pp |
| Onyx 105-shard (`enterprise_rag_onyx_full`) | 5 | 600 s | **506 s** | **−94 s (~16%)** | 0.0 pp |

At every scale we've measured, SPLADE is **all pain, no gain** on this corpus + question set. Because it ships on by default, every Helix install pays this cost. The question is whether this finding **generalizes** — and if so, whether to flip the default.

What we *don't* know yet:

- Does SPLADE add recall on **rare-term / morphological-variant queries** the bench doesn't include? (SPLADE's design strength is learned-sparse term expansion for cold-lex terms — exactly the gap dense+FTS5 leave.)
- Does SPLADE add recall on **other corpora** (technical docs, code, smaller domain-specific corpora)?
- Is the Onyx question set representative, or is it biased toward queries where dense+FTS5 are already complete?

The evidence base is currently one corpus, three question types (`basic`, `semantic`, `intra_document_reasoning`). Too narrow to flip the default — but too compelling to ignore.

## 2. Motivation

Real costs, real savings, and a real gap:

**Costs of leaving SPLADE on by default:**

- 40% of p95 latency at small-corpus scale (10K Onyx)
- ~16% of p95 latency at full-corpus scale (105-shard Onyx)
- Memory overhead during ingest (the `splade_terms` table is non-trivial — 1.25 M rows on 10K, 9.7 M rows on 50K, ~100 M rows on 850K)
- Model load on every daemon start (~1-2 GB GPU/CPU)
- A tier with weight 3.5 that we now have evidence may not be earning its keep

**Savings if we can flip it off:**

- Latency wins above directly translate to user-visible `/context` time
- Smaller ingest footprint (table, model load, tagger time)
- Simpler retrieval pipeline (fewer moving parts)

**Costs of flipping prematurely:**

- If SPLADE *does* shine on query types we haven't measured, flipping off would silently regress recall on rare-term queries
- A regression after flip is much costlier to detect than a small latency cost while on

This PRD scopes the **investigation** that would let us make the call with evidence.

## 3. Design — Investigation-then-flip

### Step A: design a SPLADE-targeted query set

Build a 100-200 question synthetic set that exercises SPLADE's *design strengths*:

- **Morphological variants** — query says "authentication", target document says "authn", "auth", "authenticated", "authentication"
- **Synonymy / paraphrase** — query says "outage incident", target says "service disruption"
- **Rare technical terms** — query references specific tools, libraries, jargon
- **Abbreviation expansion** — query says "SSO", target says "single sign-on", or vice versa

These are exactly the cases where:

- FTS5 misses (no surface-form match)
- Dense may or may not match (depending on the BGE-M3 model's coverage)
- SPLADE's learned-sparse expansion *should* surface a hit

If SPLADE shows ≥3pp recall@10 win on this set, the default stays on. If it shows ≤1pp, the case for flipping strengthens.

### Step B: re-run the 4-variant ablation on the rebuilt fixture

PR #160's full-Onyx ablation was Wall-1-bound (pagefile thrashing dominated). The Path-A rebuild (`enterprise_rag_onyx_full_2`, in progress as of 2026-05-26) yields ~12 fewer-larger shards that should fit in commit budget without paging. Re-running T/A/B/C on it tells us whether SPLADE's 94 s win at 105-shard was a pagefile-thrashing artifact OR a real cost.

### Step C: bench coverage on non-Onyx corpora — **load-bearing** (per 2026-05-26 sign-off)

Run the same 4-variant ablation on **both** existing sharded fixtures:

- `medium-sharded` — 6 source roots (BookKeeper, CosmicTasha, two-brain-audit, MaxExpressKit, Education, helix-context), ~17 K genes
- `xl-sharded` — 13 source roots (Projects tree + 12 Steam/game code installs), larger

Both are real code/docs corpora — exactly the kind that *should* exercise SPLADE's design intent (rare technical terms like "auth"/"authn"/"authentication", abbreviation expansion, library/tool jargon, morphological variants). If SPLADE shows ≤1pp recall@10 win on these, the case for flipping becomes very strong. If it shows ≥3pp, the case for keeping it on becomes strong (and we'd document that SPLADE's value is corpus-dependent: synthetic enterprise text doesn't surface it, real code/docs do).

**Gap to close before Step C can run:** Onyx ships `questions.jsonl` + `uuid_index.json` providing gold-document anchors for recall scoring. The `medium-sharded` and `xl-sharded` corpora don't have a comparable question set. Three options:

1. **Hand-write 50-100 questions per corpus** with explicit gold-document anchors. Highest quality, biggest cost. Catches morphological / abbreviation cases by design.
2. **LLM-generate questions** from random chunks of each corpus, hand-verify gold paths. Medium cost. Risk: questions look like the corpus surface form too literally, so SPLADE's expansion has nothing to expand from.
3. **Self-consistency / synthetic-query methodology** — pick a gene at random, generate paraphrases of its content as queries, check whether the original gene ranks top-K. Lowest cost. Closest to what the SNOW oracle harness already does (`scripts/snow_ablation_sweep.py`). Doesn't exercise rare-term coverage directly but does measure "can SPLADE find a document via paraphrase".

This question-set-design step is now load-bearing for the whole PRD. See open question #3 below.

### Decision matrix

| Step A: rare-term Δ recall@10 (SPLADE off vs on) | Step B: Δ recall@10 on Path-A fixture | Decision |
| --- | --- | --- |
| ≥ 3pp regression with SPLADE off | any | **Keep `splade_enabled = true`** — SPLADE earns its keep on rare-term queries. Document the win + cost. |
| ≤ 1pp regression | ≤ 1pp regression | **Flip `splade_enabled = false`** by default. Document the latency win + how to re-enable. |
| 1-3pp regression | mixed | **Audit further** — narrow the test set, re-evaluate. Don't flip without confidence. |

### Alternatives considered

**A. Flip immediately based on current evidence.** Rejected — single corpus, single question-type bucket. If SPLADE matters on rare-term queries (its design intent), flipping silently regresses an unknown user base.

**B. Keep default `true` and add a documentation warning about the latency cost.** Rejected — punts the decision. Doesn't help users who'd benefit from the default being right.

**C. Auto-disable heuristic based on per-shard SPLADE-hit-rate at startup.** Rejected for now — high complexity, hard to design a reliable threshold without the evidence Step A/B would gather anyway. Could be a follow-up if Step C suggests SPLADE's value is corpus-dependent.

**D. Investigation first, then flip with evidence.** **Proposed.** Honest about the evidence gap; gathers the data; produces a clear gate.

## 4. Test plan

This PRD doesn't ship code — it scopes investigation work. The test plan is the investigation itself (steps A/B/C above), with explicit pass/fail gates in the decision matrix.

If the flip is approved as a follow-up PR, that PR's test plan is:

- Regression test: every existing `test_dense_recall.py` test still passes with `splade_enabled = false` (they don't depend on SPLADE)
- Adapter parity: no new whitelist entries needed (only flipping a flag default)
- One new test: a synthetic rare-term query case from Step A, asserting it still surfaces via dense (without SPLADE's help) OR documenting the recall delta we're accepting

## 5. Rollout & risks

### If the investigation says **flip**

- Single config-knob change: `splade_enabled = true → false` in `helix.toml` and `RetrievalConfig`
- Existing fixtures with SPLADE-populated `splade_terms` tables are untouched on disk — the flag just gates the query-time tier
- Re-enabling per-deployment is one line: `[ingestion] splade_enabled = true`
- Latency win lands automatically for default installs

**Risk surface:** rare-term queries in production might regress without users knowing. Mitigation: document the change prominently in CHANGELOG; provide the per-deployment override; include the test-set numbers in the PR description so operators can decide if their workload looks like the test set.

### If the investigation says **keep**

- Document the win SPLADE provides (specific query types where it earns its keep)
- Document the latency cost so users running on FTS5+dense-friendly corpora know they can flip it off
- Add a `helix.toml` comment with guidance on when to disable

### If the investigation says **audit further**

- Defer the decision; PRD remains open with notes from steps A/B/C
- Possibly explore alternative C (auto-disable heuristic) with the gathered data

### Operational risk during the investigation itself

- Step A (synthetic query set design) requires care to avoid introducing bias either direction. Best practice: design the queries *before* running them through any retriever, so we don't optimize for finding a result we want.
- Step B is gated on the Path-A rebuild completing (~5-8 h ingest, currently in progress).

## 6. Open questions for review

1. ~~Does this investigation scope feel right — or do you want Step C (non-Onyx corpus) folded into Step A's design from the start?~~ **Resolved 2026-05-26:** Step C is load-bearing, runs on **`medium-sharded`** AND **`xl-sharded`**. See updated Step C above.
2. **Decision matrix thresholds** — is the ±1pp / ≥3pp gate the right risk posture, or should we be tighter (±0.5pp) or looser (±2pp)?
3. **Who designs the rare-term query set in Step A, and what question-set methodology for Step C?** Two related decisions:
   - **Step A** — if I draft the rare-term queries solo, I'd need explicit sign-off on the question types and target docs to avoid bias (selecting queries SPLADE *would* win on after the fact). Alternative: hand-pick 20-30 questions you already know are SPLADE-friendly from prior helix-context experience.
   - **Step C** — neither `medium-sharded` nor `xl-sharded` has a pre-built question set. Three options listed in Step C above: (1) hand-write 50-100 per corpus, (2) LLM-generate + verify, (3) self-consistency / paraphrase methodology (closest to the existing SNOW oracle harness at `scripts/snow_ablation_sweep.py`). Each has bias/cost tradeoffs.
4. **Does Step A still pull its weight given the broader Step C?** Real code/docs corpora in Step C naturally include rare technical terms, abbreviations, and morphological variants — exactly what Step A's synthetic set is designed to exercise. Possible options:
   - Keep Step A as designed (synthetic queries are more *focused*: each one targets a specific SPLADE strength; Step C queries will be mixed)
   - Drop Step A, lean on Step C entirely (less work; risk: a corpus-wide average may hide cases where SPLADE matters)
   - Keep Step A but design it AFTER Step C — let Step C results tell us where the gaps are, then Step A targets those gaps specifically
5. **If we flip, does `splade_enabled` move from `[ingestion]` to `[retrieval]`?** Currently it's in `[ingestion]` because it gates ingest-time term population, but the retrieval gate at `knowledge_store.py:1836` is what matters most for the latency story. Worth considering moving / aliasing.

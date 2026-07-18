# Council: AssetOpsBench Reshape — Upstream Document-Grounded Track (B) + Judge-Transfer Trial (A)

- **Date:** 2026-07-13
- **Format:** 6-lens panel (bench-epistemics, retrieval-science, oss-upstream-strategy, product-positioning, engineering-cost, red-team) + chair synthesis. Panelists grounded in both repos (helix-context and a local clone of IBM/AssetOpsBench @ f855d4a); the chair verified every load-bearing code citation against the clone before ruling. Raw panel verdicts: [`2026-07-13-assetopsbench-reshape-council.panel.json`](2026-07-13-assetopsbench-reshape-council.panel.json).
- **Reviews:** [`docs/benchmarks/2026-07-13-assetopsbench-eval-verdict.md`](../benchmarks/2026-07-13-assetopsbench-eval-verdict.md) (the NO-GO this council reshapes), issue #284, PR #285.
- **Status:** decision record — **DP1 (B primary): GO_WITH_CHANGES · DP2 (A trial first): GO_WITH_CHANGES (operative — owner pre-approved contingent on this council).**

---

## Chair verdict

Both reshapes proceed, neither as pitched. **DP1: GO_WITH_CHANGES** — B is adopted as the primary *direction* (it is the only path to an externally citable, document-grounded agentic eval), but not as a committed workstream: zero scenario authoring until an upstream appetite signal and a licensing pre-flight both pass, with a 3-week kill-switch that downgrades B to a fork + README-listing. **DP2: GO_WITH_CHANGES** (operative, owner pre-approved contingent on this council) — A runs first as a deliberately minimal *judge-transfer trial*, not an internal track: ~10-15 utterances, small bed, three arms, vendored Apache-2.0 judge, pre-registered kill criteria, internal-only labeling. Every load-bearing code citation from the panels was chair-verified against the clone (`trajectory_text[:8000]` at llm_judge.py:109; `semantic_similarity` = `NotImplementedError`; self-judging rejection at evaluator.py:82-95, model-id based; hardcoded `DEFAULT_SERVER_PATHS` with a `server_paths` constructor override at runner.py:16-43; the external-datasets doc's "does not change AssetOpsBench scoring or baseline definitions" disclaimer; the Columbia Knowledge Plugin Server listed as an external repo). Red-team's PR-record claim was verified live via `gh pr list` and is accurate, with one counter-nuance the chair adds: external README-listing PR #419 merged in under 2 hours, so the downgrade path is demonstrably viable.

## DP1 — Reshape B as primary direction: GO_WITH_CHANGES

**The case for.** The upstream door is genuinely open for scenarios: README's "Call for Scenario Contribution" invites PRs or `new-scenario`-tagged issues with named maintainer contacts, and docs/external-industrial-datasets.md is a maintainer-authored on-ramp with a 9-step contract. The repo is highly active (PR #447 merged the day of this council). Product-positioning is right that "helix backs agents in non-coding environments" is the natural next chapter after ERB, and that a merged contribution is a credibility asset *before any score exists*. Five of six lenses converged independently on the same shape: direction yes, commitment gated.

**The case against — all of it survives scrutiny.** Three verified structural problems mean B-as-pitched would measure approximately nothing:

1. **Parametric contamination** (bench-epistemics, retrieval-science): "the benchmark is existing proof that this exact document content is answerable without reading any document" — AssetOpsBench's own FMSR domain answers "knowledge" scenarios via hardcoded parametric prompt templates with zero retrieval, and ships ISO 10816 tables and bearing geometry as constants. Famous public manuals test model memory, not helix.
2. **Grounding-blind scoring** (engineering-cost's killer fact): the contribution page opens by declaring it "does not change AssetOpsBench scoring or baseline definitions," while the only scorer that could credit context quality (`semantic_similarity`) is a `NotImplementedError` skeleton and the live 6-dim judge passes/fails on tool sequencing and asset/time/sensor correctness (llm_judge.py:40-43). The invited path structurally cannot score what helix is for.
3. **Acceptance asymmetry** (red-team, chair-verified): insider PRs merge same-day to 3 days; every external feature PR of the last three weeks is open or closed unmerged — including #404, an external *scorer* contribution. And oss-upstream's killer fact: B's document-tool seam already exists in the ecosystem as a Columbia team's "Knowledge Plugin Server" (README line 260) — living, like all ~15 knowledge/tooling extensions, as a fork listing, never merged core. The empirical endpoint of B's seam is a README listing.

Plus the **licensing squeeze**: ISO 10816 text and OEM handbooks are non-redistributable under IBM's own checklist; the clean corpus shrinks toward public-domain NASA material, which is also the most parametrically leaked.

**Resolution.** The gap between the five GO_WITH_CHANGES votes and red-team's NO_GO is smaller than it looks: nobody endorsed B as an ungated near-term primary workstream. The chair rules B the primary *direction* with hard gates (conditions B-1 through B-5) and an honest downgrade: if maintainers don't engage within 3 weeks, or the licensing one-pager comes up empty, B becomes "helix fork + README University-Extensions listing" — which the PR record shows merges fast — and the "externally citable IBM benchmark result" framing is retired. Everything public is framed as evaluating against IBM's public benchmark, per the hard rule.

## DP2 — Reshape A as first trial: GO_WITH_CHANGES

**What A actually de-risks — and what it doesn't.** Red-team's caution is adopted as ground truth: A does *not* touch B's kill risks (upstream acceptance, licensing) — those are de-risked only by the parallel issue + licensing slice. What A genuinely answers, cheaply and before any public artifact: (1) **judge transfer** — do the 6 dims produce non-vacuous signal on document-grounded queries? `agent_sequence_correct` is near-vacuous with a single MCP server and `data_retrieval_accuracy` is defined as correct asset/time/sensor via tools, so a naive port inflates pass rates on dimensions that can only return true; (2) **contamination-control rehearsal** — the closed-book gate methodology B depends on, iterated where it's free; (3) **authoring cost calibration** for B's effort budget. Standalone value even if B dies: the 50-needle matrix's first trajectory-level and hallucination-dimension measurement — with retrieval-science's honest caveat that the hallucination check is *process* hallucination ("claims success without performing the necessary actions," llm_judge.py:61-63), not content faithfulness, and must not be oversold.

**Mechanics are cheap and confirmed.** Engineering-cost is correct: vendor one 173-line Apache-2.0 file, skip CouchDB/uv/Docker entirely, reuse the existing `claude -p` + helix MCP bench loop. Two hard mechanical constraints are binding: a non-Claude judge (evaluator.py:82-95 rejects self-judging by model id; the sibling-Claude loophole is documented and not used), and the 8K trajectory cap (llm_judge.py:109) — a context-rich MCP trajectory blows through it immediately, so the judge input must be a structured digest or the vendored cap raised with the deviation noted.

**Minimal scope (binding).** 10-15 asset-ops-style, document-grounded utterances over the small bed only (helix design docs + MEK, already frozen), MULTI_VALID_GOLD discipline with deterministic gold anchors per utterance (the judge is the thing under test — it needs a ground-truth yardstick, per retrieval-science). Three arms per bench-epistemics: helix MCP on / naive filesystem baseline / closed-book no-tools control. Deliverable is a **judge-transfer audit memo**, not a pass rate: per-dimension vacuity table, hallucination-dim behavior, closed-book failure count, agreement vs the {-1,0,+1} scorer. Pre-registered kill criterion (red-team): degenerate variance on the asset-ops-shaped dims or >20% unparseable JSON → stop, full-A cancelled, B's scoring premise re-examined. Dev box only, 2-3 days plus authoring, no gandalf, no helix_context/ changes, internal-only header.

## Binding conditions

See structured list: B-1 appetite gate (issue-first, 3-week kill-switch), B-2 licensing pre-flight (one page, PD/CC-BY only), B-3 closed-book contamination gate on every scenario, B-4 two-claim discipline + mandatory non-helix baseline arm, B-5 no dependency on upstream scoring changes; A-1 scope caps (≤15 utterances, small bed, 3 arms), A-2 judge mechanics (attribution, non-Claude judge, 8K-cap handling), A-3 pre-registered kill criterion, A-4 audit-memo deliverable labeled internal-only, A-5 footprint (dev box, 0-failure master, Codex territory untouched); SEQ-1 weekend review first, B authoring gated on B-1 ∧ B-2 ∧ A's audit.

## First slice

This week, docs-only, in parallel: (a) file the upstream `new-scenario` issue proposing the document-grounded scenario class + generic knowledge-server seam — ~1 hour, starts the appetite clock; (b) the corpus licensing one-pager; (c) desk-author A's utterances + gold and vendor llm_judge.py into `benchmarks/assetops_judge/`. A runs on the dev box next week after the weekend-results review.

## Dissents

- **red-team (DP1 NO_GO):** verified evidence, honestly recorded — external feature PRs languish while insiders merge same-day, and #404 (an external scorer PR, exactly what B needs) closed unmerged. Absorbed via the kill-switch + downgrade path rather than a kill; the chair's verification added that the downgrade path itself (README listing, PR #419) merges in hours. Residual disagreement is about whether a gated direction deserves the word "primary."
- **retrieval-science (A bed scope):** wanted small+xl two-point contrast; overruled to small-only — scale curves are the 50-needle matrix's job, not trial de-risking.
- **product-positioning / engineering-cost (DP2 GO):** tightened to GO_WITH_CHANGES by making the kill criterion and 8K-cap handling binding.
- **red-team (A's purpose):** A approved on standalone merits, not as full B de-risking — the chair splits the difference: A de-risks judge transfer + contamination methodology + authoring cost; the issue + licensing slice de-risks the rest, in parallel.

## Risks register

| Risk | Sev | Owner reshape | Containment |
|---|---|---|---|
| Parametric contamination — public-manual gold answerable closed-book; helix delta ≈ 0 | HIGH | B (and A) | B-3 closed-book gate on every scenario; A rehearses the methodology |
| Grounding-blind judge — semantic_similarity unimplemented; 6-dim rubric credits tool sequencing | HIGH | B | B-5: no scoring-change dependency; design characteristic_forms to require doc facts; A-3 tests transfer first |
| Upstream acceptance latency/rejection — external PRs languish; seam + scenarios + scorer = 3 acceptances | HIGH | B | B-1 issue-first + 3-week kill-switch + fork/README downgrade |
| Licensing hollow-out — ISO/OEM texts non-redistributable | HIGH | B | B-2 pre-flight; PD/CC-BY only; kill if corpus won't fill a page |
| Vendor-writes-its-own-exam optics | HIGH if published | B | B-4 baseline arm + merge-before-citation + two-claim discipline |
| Judge-dimension vacuity inflating A pass rates | MED | A | A-3 pre-registered variance kill criterion; audit-memo (not pass-rate) deliverable |
| 8K trajectory truncation silently scoring a prefix | MED | A | A-2 digest-or-documented-cap-raise before first run |
| Hallucination-dim over-claim (process, not content faithfulness) | MED | A | Explicit caveat in the audit memo; never sold as faithfulness measurement |
| A results leaking into external claims | MED | A | A-4 internal-only header; never framed as AssetOpsBench numbers |
| Roadmap displacement vs weekend review, #260/#255, blend-layer retirement | MED | both | A timeboxed 2-3 days; B enters the schedule only after gates; gandalf + benchmarks/snow/ + #208 untouched |
| Checkout contention on helix main | LOW | A | Work in a .claude/worktrees/ worktree |

**Surviving killer facts (chair-verified):** (1) The corpora proposed to ground B already live inside AssetOpsBench as hardcoded constants, and its FMSR pipeline answers "knowledge" scenarios via pure parametric LLM calls — the benchmark itself proves this content is answerable without reading documents. (2) The invited contribution path declares scoring/baselines out of scope while the only content-grounding scorer is `raise NotImplementedError` — the rubric structurally cannot see what helix is for. (3) The live PR record shows external feature and scorer PRs do not merge, while B's proposed seam already exists in the ecosystem as an unmerged fork listing — making the fork+README endpoint, not core merge, the empirical prior B must plan around.

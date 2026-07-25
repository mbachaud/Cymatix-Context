# Portfolio README Refresh — Design

**Date:** 2026-07-25
**Author:** Michael Bachaud (architecture/QA), drafted with AI agent
**Status:** Approved design → pending implementation plan

## Motivation

An external portfolio assessment ("Sol") reviewed the public `github.com/mbachaud`
surface. Its central findings, verified against live repo state on 2026-07-25:

- **No `mbachaud/mbachaud` profile README exists** (confirmed via `gh repo view`).
  A visitor cannot classify twelve+ public repos (products vs. wrappers vs. forks
  vs. near-empty module repos) in five seconds.
- **Cymatix README overclaims three things** that open issues currently falsify:
  - know/miss agent contract is marketed as shipped; issues **#287** (RRF-era
    know/miss recalibration) and **#239** (know-confidence is anti-signal on every
    bed) are **open** — confidence is not yet a trustworthy signal.
  - "cymatics" is described as "256-bin frequency-domain fingerprint … the way
    cymatics renders sound," but the implementation is deterministic feature
    hashing (term → MD5 → 256 bins) + Gaussian smoothing. The metaphor outruns
    the math, and the feature is not yet isolated against hashed-BoW / random-bin
    controls.
  - "local-first at 829K fragments" is the headline differentiator, but the
    **sharded** path trails unsharded by ~31pp recall@10 (issue **#275**, plus
    **#264/#265/#307/#311**, all open) — the scale route is materially weaker than
    the engine it is meant to scale.
- **Authorship is mis-stated.** The BigEd README credits "Claude Code: primary
  architect ~70% / Human Max ~5%," which donates the architecture authorship the
  human actually owns to the model, reframing a real strength (directing an AI
  implementation org) as "vibe-coded a giant repo."

The strongest evidence in the portfolio is the **decision trail** — documented
null results, host-variance isolation, frozen benchmark versions, issues filed
against the author's own flagship features when measurements falsify them. The
current READMEs bury that strength under product surface and overclaim.

## Goals

1. Ship an honest, defensible Cymatix README that labels experimental features as
   experimental and describes mechanisms plainly, without gutting the real
   substance.
2. Ship a new `mbachaud/mbachaud` profile README that gives a five-second
   classification of the portfolio and leads with the author's actual
   differentiator: architecture + adversarial QA direction of implementation
   agents.
3. State the human/model division of labor accurately and consistently.

## Non-goals (explicitly out of scope)

- No code changes, no onboarding-command consolidation (`cymatix doctor`, etc.),
  no product/architecture changes. Those are Sol's *product* action items,
  tracked separately.
- No new repos beyond drafting the profile README content. Creating and pushing
  `mbachaud/mbachaud` is a follow-up implementation step, gated on user approval.
- No external image services in the profile README (no github-readme-stats,
  streak/trophy badges). Self-contained markdown only.
- No promotion of `agentome` (Sol: "presently harmful" pending update/archive) or
  `MaxExpressKit` (Sol: "premature packaging") to the pinned/headline narrative.

## Decisions locked (from brainstorming)

| Decision | Choice |
|---|---|
| Cymatix scope | **Full honesty pass** (all four edits below) |
| Authorship framing | **Architect framing** — foreground the human as director; attribute code to models |
| Profile style | **Structured hierarchy** — headline + method loop + classification table; text-only |
| BigEd credit block | Drafted here as ready-to-paste text; pushing to `mbachaud/BigEd` is not part of this task |

---

## Deliverable 1 — Cymatix README honesty pass

File: `README.md` (this repo). Four targeted edits; structure, endpoint tables,
setup, and `<details>` folding are unchanged. This is truth-in-labeling, not a
rewrite.

### Edit 1 — cymatics: metaphor → plain mechanics

Current (≈L19–22):

> The name comes from the engine's cymatics stage: retrieval candidates are
> scored against a 256-bin frequency-domain fingerprint of the query, the same
> way cymatics renders sound as standing-wave geometry. The `/fingerprint`
> endpoint exposes that spectrum directly.

Replace with (final wording tunable at implementation):

> The name comes from the engine's cymatics stage: each term is hashed (MD5) into
> one of 256 bins and given a small Gaussian spread, and query and candidate are
> compared as the resulting 256-dimensional vectors. It is a cheap, deterministic,
> model-free term transform — "cymatics" is a mnemonic for the binned spectrum,
> not a claim of signal-processing semantics (nearby bins are hash placement, not
> related meaning). It is a candidate-reordering signal that has **not yet been
> isolated against hashed bag-of-words or random-bin controls**; treat it as an
> experimental cheap feature, not a proven one. The `/fingerprint` endpoint
> exposes the binned vector directly.

Note: no open issue currently tracks the cymatics ablation controls. Filing one
(e.g. "cymatics vs. hashed-BoW / random-bin / multi-seed controls on end-task
correctness") is recommended so the README can link it; not required for this pass.

### Edit 2 — know/miss: label confidence experimental

Two sites: the intro agent-contract bullet (≈L67–70) and the pipeline bullet
(≈L200). Keep the contract description; add the caveat. Proposed intro wording:

> **Agent contract** *(shape stable; confidence calibration experimental)*: every
> `/context` response carries `know { found, confidence }` (grounded) or
> `miss { reason, escalate_to }` (not found — don't answer from the knowledge
> store). Stale results downgrade to `miss(reason="stale"|"cold"|"superseded")`
> via the freshness gate. The contract *shape* is stable and load-bearing, but the
> `confidence` scalar is under active recalibration — on current internal beds it
> is not yet a reliable trust signal ([#287](https://github.com/mbachaud/Cymatix-Context/issues/287),
> [#239](https://github.com/mbachaud/Cymatix-Context/issues/239)). Rely on
> `found` / `reason` today; treat `confidence` as provisional.

### Edit 3 — scale claim: add sharding caveat

Add after the ERB proof paragraph (≈after L61) and mirror one line in Gotchas:

> **Scale caveat:** the 829K-fragment operating point above runs the *unsharded*
> engine. The **sharded** path (corpora split across shard DBs) currently trails
> unsharded by ~31pp recall@10 / ~30pp MRR on the xl bed — dense recall and
> co-activation are not yet at parity across shards
> ([#275](https://github.com/mbachaud/Cymatix-Context/issues/275)). "Local-first
> at scale" is a demonstrated research operating point on the unsharded engine,
> not yet a turnkey general substrate for sharded corpora.

### Edit 4 — token claims: answer-preservation caveat

Add a caveat directly under the Proof token-economics table (≈after L40):

> *Caveat: the "vs standard RAG" denominator (top-5 @ 1500 tokens) is a
> configurable baseline, and the 37× multi-turn figure is elision of
> already-delivered documents, not compression of new content. The
> decision-useful claim is **equal-or-better task completion at fewer input
> tokens**; a same-harness baseline/ablation frontier (BM25 / BGE-M3 / BM25+dense
> RRF / Cymatix full / Cymatix minus-cymatics) paired with correctness is future
> work, not yet published.*

### Edit 5 — authorship note (shared framing, see below)

Add a short "How this was built" note near Acknowledgments / License:

> **How this was built.** Cymatix Context is architected and QA-directed by
> Michael Bachaud. Implementation, refactoring, draft documentation, and test
> generation are produced by AI coding agents under spec- and benchmark-gated
> review. The human owns the product thesis, architecture selection, acceptance
> criteria, experiment design, and falsification authority; the models own the
> code production. See [How I work](https://github.com/mbachaud).

---

## Deliverable 2 — new `mbachaud/mbachaud` profile README

Self-contained markdown, no external image services. Near-final draft below;
wording tunable during implementation.

~~~markdown
# Michael Bachaud

> I architect local-first AI systems and direct implementation agents through
> specifications, adversarial QA, instrumentation, and benchmark-gated iteration.

I don't hand a prompt to a model and ship what comes back. I run an adversarial
engineering loop and hold the authority at every gate:

```
hypothesis → specification → implementation agents → instrumentation
  → benchmark → falsification → diagnosis → revised architecture
```

The receipts for that loop are in the issue trackers, not just the code:
documented null results, isolated host variance, frozen benchmark versions, and
issues I file against my own flagship features when the measurements falsify them.

## What's here

| Tier | Repo | What it is |
|------|------|-----------|
| 🚩 **Flagship** | [Cymatix Context](https://github.com/mbachaud/Cymatix-Context) | Local-first, model-free context/retrieval engine for LLM agents. Shipped Python package; CLI + HTTP + MCP; ~2,900 tests; reproducible benchmark receipts. |
| 🔧 **Operational tooling** | [ScoreRift](https://github.com/mbachaud/scorerift) | Dual-layer audit: automated scoring + manual grading + reconciliation. The product isn't the score — it's watching the gap between them. |
| 🏗️ **Applied-architecture demos** | [DriftWatch](https://github.com/mbachaud/DriftWatch), [BookKeeper](https://github.com/mbachaud/BookKeeper) | Reference architectures — SOC 2 draft-generation assist, and a double-entry ledger core. Architecture samples, not certified/production products. |
| 🧪 **AI-development laboratory** | [BigEd](https://github.com/mbachaud/BigEd) | Explicitly *not a product* — a lab for local-first multi-agent orchestration on ~11 GB VRAM edge hardware, file-based handoffs, HITL gates. |
| ↗️ **External contribution** | [Archestra PR #4827](https://github.com/mbachaud/archestra) | Reasoning carried into someone else's unfamiliar architecture — evidence the method transfers beyond my own stack. |
| 🔱 **Forks & benchmark fixtures** | headroom, AssetOpsBench, anker-solix, archestra-fork | Upstreams I build on or benchmark against. Labeled as forks, not pinned as my work. |

## How I split the work with models

**Human (me):** product thesis, constraints, architecture selection, acceptance
criteria, experiment design, risk decisions, QA and falsification authority.
**Models:** implementation, refactoring, draft documentation, test generation,
code-review lenses.

I don't claim code authorship I don't have — and I don't donate the architecture
authorship I do.
~~~

### Repo classification rationale

- `Cymatix-Context` — flagship (shipped package, most substance).
- `scorerift` — clearest secondary product sentence; keep narrow.
- `DriftWatch`, `BookKeeper` — real architecture, but no external validation /
  certification; labeled "reference architecture," per Sol.
- `BigEd` — self-labeled not-a-product; the lab.
- `archestra` — the fork carrying external contribution PR #4827.
- Forks/fixtures — `headroom` (fork of chopratejas/headroom), `AssetOpsBench`
  (IBM), `anker-solix-api-e10alpha`, archestra fork. Explicitly not pinned.
- **Deliberately not in the headline table:** `agentome` (Sol: presently harmful,
  pending update/archive), `MaxExpressKit` (premature packaging),
  `BigEd-ModuleHub-public` (near-empty). They remain in the repo list, not the
  narrative.

---

## Deliverable 3 (draft only) — BigEd credit-block replacement

Not pushed as part of this task; provided ready-to-paste for `mbachaud/BigEd`.

Replace the existing "Claude Code: primary architect ~70% / Human Max ~5%" credit
with:

> ## Authorship
> BigEd is architected and QA-directed by Michael Bachaud; implementation is
> produced by AI coding agents.
>
> - **Human:** product thesis, architecture selection, acceptance criteria,
>   experiment design, risk decisions, QA and falsification authority.
> - **Models:** implementation, refactoring, draft documentation, test generation,
>   code-review lenses.
>
> This is a laboratory, not a product — a place to run and observe the
> human-directs-agents loop, not a system anyone else should depend on yet.

---

## Verification / acceptance criteria

- Cymatix README: all four honesty edits present; every experimental caveat links
  a real open issue (#287, #239, #275) or states plainly that no tracking issue
  exists yet (cymatics controls). No remaining sentence claims know/miss
  confidence is production-ready or that cymatics is signal-processing.
- Profile README: renders as valid GitHub markdown, no external image URLs, the
  classification table present, headline + method loop + authorship footer
  present. Forks explicitly labeled and not pinned.
- Authorship language identical in intent across all three artifacts; no artifact
  claims human code authorship, none donates architecture authorship to a model.
- No code, config, or product surface touched.

## Open follow-ups (not this task)

- File a cymatics-controls ablation issue so Edit 1 can link it.
- Push the profile README (create `mbachaud/mbachaud`) — separate approval.
- Apply the BigEd credit block in `mbachaud/BigEd` — separate worktree/approval.
- Sol's product items (onboarding command, ablation frontier, agentome
  update/archive, security/contribution policies) — tracked separately.

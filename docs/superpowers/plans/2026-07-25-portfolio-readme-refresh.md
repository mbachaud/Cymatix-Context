# Portfolio README Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an honesty-passed Cymatix README plus a new `mbachaud/mbachaud` profile README draft and a ready-to-paste BigEd authorship block, all representing the human/model division of labor truthfully.

**Architecture:** Pure documentation edits. One in-place edit of the flagship `README.md` (six targeted prose changes), and two new staging artifacts under `docs/superpowers/artifacts/` — the profile README and the BigEd credit block — that the user reviews here before creating/editing the destination repos (gated follow-up, not this plan).

**Tech Stack:** Markdown, git. Verification via `grep`/`rg` (or the Grep tool) and manual render check.

## Global Constraints

Copied verbatim from the spec (`docs/superpowers/specs/2026-07-25-portfolio-readme-refresh-design.md`). Every task implicitly includes these:

- **No external image services** in any new markdown (no github-readme-stats, shields badges, streak/trophy images). Self-contained markdown only.
- **No code, config, or product surface touched.** Docs only.
- **Every experimental caveat links a real open issue** (#287, #239, #275) or plainly states no tracking issue exists yet (cymatics controls). Issue URL base: `https://github.com/mbachaud/Cymatix-Context/issues/`.
- **Authorship framing (exact intent):** Human owns product thesis, architecture selection, acceptance criteria, experiment design, risk decisions, QA/falsification authority. Models own implementation, refactoring, draft docs, test generation, review lenses. Never claim human code authorship; never donate architecture authorship to a model.
- **Do not promote** `agentome` or `MaxExpressKit` to the profile's headline/pinned narrative.
- **No new repos created/pushed** in this plan. Profile-repo creation and the BigEd edit are gated follow-ups.

---

### Task 1: Cymatix README honesty pass

**Files:**
- Modify: `README.md` (six edits: A–F)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: a `README.md` where no sentence claims know/miss `confidence` is production-ready, none claims cymatics is signal-processing, the sharded-scale gap is disclosed, the token claims carry an answer-preservation caveat, and authorship is stated. Later tasks are independent of this file.

- [ ] **Step 1: Verify current (overclaiming) state — the "failing test"**

Run (Bash) — each of these should currently MATCH (proving the overclaim is present and the caveat is absent):

```bash
cd f:/Projects/cymatix-context
grep -c "the same way cymatics renders sound" README.md   # expect 1 (overclaim present)
grep -c "shape stable; confidence calibration experimental" README.md   # expect 0 (caveat absent)
grep -c "Scale caveat:" README.md                          # expect 0
grep -c "vs standard RAG.*denominator" README.md           # expect 0
grep -c "How this was built" README.md                     # expect 0
```

Expected: `1`, `0`, `0`, `0`, `0`. This confirms the starting state.

- [ ] **Step 2: Edit A — cymatics header (plain mechanics)**

Replace this exact block:

```
The name comes from the engine's cymatics stage: retrieval candidates are
scored against a 256-bin frequency-domain fingerprint of the query, the same
way cymatics renders sound as standing-wave geometry. The `/fingerprint`
endpoint exposes that spectrum directly.
```

with:

```
The name comes from the engine's cymatics stage: each term is hashed (MD5) into
one of 256 bins and given a small Gaussian spread, and query and candidate are
compared as the resulting 256-dimensional vectors. It's a cheap, deterministic,
model-free term transform — "cymatics" is a mnemonic for that binned spectrum,
not a claim of signal-processing semantics (nearby bins are hash placement, not
related meaning). It's a candidate-reordering signal that has **not yet been
isolated against hashed bag-of-words or random-bin controls** — treat it as an
experimental cheap feature, not a proven one. The `/fingerprint` endpoint
exposes the binned vector directly.
```

- [ ] **Step 3: Edit B — know/miss intro bullet (Proof section)**

Replace this exact block:

```
**Agent contract**: every `/context` response carries `know { found, confidence }`
(grounded — you may answer) or `miss { reason, escalate_to }` (not found —
don't answer from the knowledge store). Stale results downgrade to
`miss(reason="stale"|"cold"|"superseded")` via the freshness gate.
```

with:

```
**Agent contract** *(shape stable; confidence calibration experimental)*: every
`/context` response carries `know { found, confidence }` (grounded — you may
answer) or `miss { reason, escalate_to }` (not found — don't answer from the
knowledge store). Stale results downgrade to
`miss(reason="stale"|"cold"|"superseded")` via the freshness gate. The contract
*shape* is stable and load-bearing, but the `confidence` scalar is under active
recalibration — on current internal beds it is not yet a reliable trust signal
([#287](https://github.com/mbachaud/Cymatix-Context/issues/287),
[#239](https://github.com/mbachaud/Cymatix-Context/issues/239)). Rely on `found`
/ `reason` today; treat `confidence` as provisional.
```

- [ ] **Step 4: Edit C — know/miss pipeline bullet**

Replace this exact block:

```
- **know/miss contract**: `know` means the context is grounded, agent may answer. `miss` means don't answer from the knowledge store — escalate via `escalate_to` tools or refetch from `refresh_targets`.
```

with:

```
- **know/miss contract** *(shape stable; confidence experimental)*: `know` means the context is grounded, agent may answer. `miss` means don't answer from the knowledge store — escalate via `escalate_to` tools or refetch from `refresh_targets`. The `confidence` scalar is under active recalibration ([#287](https://github.com/mbachaud/Cymatix-Context/issues/287), [#239](https://github.com/mbachaud/Cymatix-Context/issues/239)) — rely on `found` / `reason`, treat `confidence` as provisional.
```

- [ ] **Step 5: Edit D — scale caveat (after ERB paragraph)**

Find this exact text (end of ERB paragraph immediately followed by the Fusion line):

```
[docs/benchmarks/2026-07-10-erb-blob-829k-reproduction.md](docs/benchmarks/2026-07-10-erb-blob-829k-reproduction.md).

**Fusion**: Reciprocal Rank Fusion has been the default ranker since
```

Replace it with (inserts the caveat paragraph between them):

```
[docs/benchmarks/2026-07-10-erb-blob-829k-reproduction.md](docs/benchmarks/2026-07-10-erb-blob-829k-reproduction.md).

**Scale caveat:** the 829K-fragment operating point above runs the *unsharded*
engine. The **sharded** path (corpora split across shard DBs) currently trails
unsharded by ~31pp recall@10 / ~30pp MRR on the xl bed — dense recall and
co-activation are not yet at parity across shards
([#275](https://github.com/mbachaud/Cymatix-Context/issues/275)). "Local-first
at scale" is a demonstrated research operating point on the unsharded engine, not
yet a turnkey general substrate for sharded corpora.

**Fusion**: Reciprocal Rank Fusion has been the default ranker since
```

- [ ] **Step 6: Edit E — token-claim caveat (after Reproducer line)**

Find this exact text:

```
Reproducer: `python benchmarks/bench_rag_vs_sike_tokens.py` against your own knowledge store.

**External benchmark** — [EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench)
```

Replace it with (inserts the caveat between them):

```
Reproducer: `python benchmarks/bench_rag_vs_sike_tokens.py` against your own knowledge store.

*Caveat: the "vs standard RAG" denominator (top-5 @ 1500 tokens) is a
configurable baseline, and the 37× multi-turn figure is elision of
already-delivered documents, not compression of new content. The decision-useful
claim is **equal-or-better task completion at fewer input tokens**; a same-harness
baseline/ablation frontier (BM25 / BGE-M3 / BM25+dense RRF / Cymatix full /
Cymatix minus-cymatics), paired with correctness, is future work — not yet
published.*

**External benchmark** — [EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench)
```

- [ ] **Step 7: Edit F1 — Gotchas mirror bullet for the shard gap**

Find this exact text (end of the Fusion-mode gotcha bullet):

```
`"additive"` remains as the legacy accumulator, scheduled for condition-gated removal. Under RRF the abstain gates run ratio-only.
```

Replace it with (appends a new gotcha bullet after it):

```
`"additive"` remains as the legacy accumulator, scheduled for condition-gated removal. Under RRF the abstain gates run ratio-only.
- **Sharded scale gap**: the sharded adapter currently trails the unsharded engine by ~31pp recall@10 on xl (dense recall and co-activation not yet at parity) — [#275](https://github.com/mbachaud/Cymatix-Context/issues/275). Prefer the unsharded engine for accuracy-sensitive corpora until this is closed.
```

- [ ] **Step 8: Edit F2 — authorship section (before License)**

Find this exact text:

```
## License

[Apache-2.0](LICENSE). See [NOTICE](NOTICE) for third-party attributions.
```

Replace it with (inserts the "How this was built" section before License):

```
## How this was built

Cymatix Context is architected and QA-directed by Michael Bachaud. Implementation,
refactoring, draft documentation, and test generation are produced by AI coding
agents under spec- and benchmark-gated review. The human owns the product thesis,
architecture selection, acceptance criteria, experiment design, and falsification
authority; the models own the code production.

## License

[Apache-2.0](LICENSE). See [NOTICE](NOTICE) for third-party attributions.
```

- [ ] **Step 9: Verify all edits landed — the "passing test"**

Run (Bash):

```bash
cd f:/Projects/cymatix-context
grep -c "the same way cymatics renders sound" README.md        # expect 0 (overclaim gone)
grep -c "not yet been" README.md                                # expect >=1 (cymatics caveat)
grep -c "shape stable; confidence calibration experimental" README.md  # expect 1
grep -c "shape stable; confidence experimental" README.md       # expect 1 (pipeline bullet)
grep -c "Scale caveat:" README.md                               # expect 1
grep -c "Sharded scale gap" README.md                           # expect 1
grep -c "configurable baseline" README.md                       # expect 1
grep -c "How this was built" README.md                          # expect 1
grep -oE "issues/(287|239|275)" README.md | sort -u             # expect all three
```

Expected: `0`, `≥1`, `1`, `1`, `1`, `1`, `1`, `1`, and the three issue numbers listed. Then open `README.md` and visually confirm the Proof section and the new section render cleanly (no broken tables, no orphaned bullets).

- [ ] **Step 10: Commit**

```bash
cd f:/Projects/cymatix-context
git add README.md
git commit -m "docs(readme): honesty pass — know/miss experimental, cymatics plain mechanics, shard-scale + token caveats, authorship

Labels the know/miss confidence scalar experimental (#287, #239), describes
cymatics as deterministic hashing+smoothing rather than signal processing,
discloses the sharded-vs-unsharded recall gap (#275), caveats the token-savings
denominator, and states the human-architects / models-implement division.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: New `mbachaud/mbachaud` profile README (staging draft)

**Files:**
- Create: `docs/superpowers/artifacts/2026-07-25-mbachaud-profile-README.md`

**Interfaces:**
- Consumes: nothing.
- Produces: the profile README content, staged in-repo for review. Destination (`mbachaud/mbachaud/README.md`) is created in a gated follow-up, not here.

- [ ] **Step 1: Create the staging file with the full profile README**

Create `docs/superpowers/artifacts/2026-07-25-mbachaud-profile-README.md` with exactly this content (the `mkdir` for `artifacts/` is implicit in file creation):

````markdown
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
````

- [ ] **Step 2: Verify — no external images, links well-formed, structure intact**

Run (Bash):

```bash
cd f:/Projects/cymatix-context
F=docs/superpowers/artifacts/2026-07-25-mbachaud-profile-README.md
grep -cE '!\[|img\.shields|github-readme-stats|streak|\.svg\)' "$F"   # expect 0 (no external images)
grep -c "hypothesis → specification" "$F"                              # expect 1 (method loop)
grep -c "How I split the work with models" "$F"                        # expect 1 (authorship footer)
grep -oE "github.com/mbachaud/[A-Za-z-]+" "$F" | sort -u               # eyeball: Cymatix-Context, scorerift, DriftWatch, BookKeeper, BigEd, archestra
grep -ci "agentome\|maxexpresskit" "$F"                                # expect 0 (not promoted to headline)
```

Expected: `0`, `1`, `1`, the repo list, `0`. Then render the file (GitHub markdown preview) and confirm the table renders with six rows and the fenced method loop displays as a code block.

- [ ] **Step 3: Commit**

```bash
cd f:/Projects/cymatix-context
git add docs/superpowers/artifacts/2026-07-25-mbachaud-profile-README.md
git commit -m "docs(profile): draft mbachaud/mbachaud profile README (staging)

Structured-hierarchy profile: architect headline, adversarial-loop method block,
five-second repo classification table (flagship / tooling / demos / lab /
external / forks-not-pinned), and the human-directs / models-implement footer.
Self-contained markdown, no external image services. Staged for review; pushing
to the mbachaud/mbachaud repo is a gated follow-up.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: BigEd authorship credit block (staging draft, no push)

**Files:**
- Create: `docs/superpowers/artifacts/2026-07-25-biged-authorship-block.md`

**Interfaces:**
- Consumes: nothing.
- Produces: ready-to-paste replacement text for the `mbachaud/BigEd` README's authorship credit. Applying it to BigEd is a gated follow-up in a separate worktree, not this plan.

- [ ] **Step 1: Create the staging file**

Create `docs/superpowers/artifacts/2026-07-25-biged-authorship-block.md` with exactly this content:

````markdown
# BigEd authorship credit — replacement block

Replace the existing "Claude Code: primary architect ~70% / Human Max ~5%"
credit in `mbachaud/BigEd`'s README with the section below.

---

## Authorship

BigEd is architected and QA-directed by Michael Bachaud; implementation is
produced by AI coding agents.

- **Human:** product thesis, architecture selection, acceptance criteria,
  experiment design, risk decisions, QA and falsification authority.
- **Models:** implementation, refactoring, draft documentation, test generation,
  code-review lenses.

This is a laboratory, not a product — a place to run and observe the
human-directs-agents loop, not a system anyone else should depend on yet.
````

- [ ] **Step 2: Verify**

Run (Bash):

```bash
cd f:/Projects/cymatix-context
F=docs/superpowers/artifacts/2026-07-25-biged-authorship-block.md
grep -c "70%" "$F"                          # expect 0 (old mis-credit not reintroduced)
grep -c "architected and QA-directed" "$F"  # expect 1
grep -c "laboratory, not a product" "$F"    # expect 1
```

Expected: `0`, `1`, `1`.

- [ ] **Step 3: Commit**

```bash
cd f:/Projects/cymatix-context
git add docs/superpowers/artifacts/2026-07-25-biged-authorship-block.md
git commit -m "docs(artifact): ready-to-paste BigEd authorship block (draft)

Replaces the '70% Claude / 5% Max' credit with the architect-framing role split.
Staged only — applying to mbachaud/BigEd is a gated follow-up.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Gated follow-ups (NOT in this plan)

Do not do these without explicit user approval:

1. Create and push the `mbachaud/mbachaud` repo from the Task 2 artifact.
2. Apply the Task 3 block to `mbachaud/BigEd` (separate worktree).
3. File a cymatics-controls ablation issue so Edit A can link it, then update Edit A's "no tracking issue exists yet" wording to the link.
4. Sol's product action items (onboarding command, ablation frontier, agentome update/archive, security/contribution policies) — separate specs.

## Self-Review

- **Spec coverage:** Cymatix Edit 1 (cymatics)→Task1/StepA; Edit 2 (know/miss)→Task1/StepsB,C; Edit 3 (scale)→Task1/StepsD,F1; Edit 4 (token)→Task1/StepE; Edit 5 (authorship note)→Task1/StepF2; Deliverable 2 (profile)→Task2; Deliverable 3 (BigEd block)→Task3; out-of-scope items→Gated follow-ups. No gaps.
- **Placeholder scan:** every edit ships exact old/new text; every verification ships runnable commands with expected outputs. No TBD/TODO.
- **Type/string consistency:** verification greps in Steps 9/2/2 match the exact strings introduced in the edit steps (e.g. "Scale caveat:", "Sharded scale gap", "How this was built", "shape stable; confidence experimental").

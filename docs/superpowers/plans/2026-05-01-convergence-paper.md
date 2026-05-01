# *The Same Move at Every Layer* — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Draft, fact-check, and prepare for publication a ~4,500-word Substack #2 essay following the design in [docs/specs/2026-05-01-convergence-paper-design.md](../../specs/2026-05-01-convergence-paper-design.md).

**Architecture:** Single markdown draft file under `docs/papers/`, drafted section-by-section in spec order, with verification passes (Helix-receipts cite-check, Bucket-3 claim-check, don't-say grep, word-count check) gating completion. One layer-stack figure produced as a separate artifact.

**Deliverables:** `docs/papers/2026-05-01-the-same-move-at-every-layer.md` (draft) + `docs/papers/figures/2026-05-01-layer-stack.md` (figure spec).

**Tech stack:** markdown, git. No code.

---

## File structure

**Create:**
- `docs/papers/2026-05-01-the-same-move-at-every-layer.md` — the paper draft
- `docs/papers/figures/2026-05-01-layer-stack.md` — figure spec for the one figure

**Reference (read-only, do not modify):**
- `docs/specs/2026-05-01-convergence-paper-design.md` — the spec; the source of truth
- `docs/papers/AGENTOME_PART_II_DRAFT.md` — for genome-shape numbers (17,623 genes, 670 MB, chromatin tier counts)
- `docs/research/RESEARCH_REVIEW_2026-04-22.md` — for the BM25 honest-tax numbers
- `docs/architecture/PIPELINE_LANES.md` — for SPLADE/Tier 3.5 reference
- `docs/benchmarks/BENCHMARKS.md` — for the Headroom A/B-neutral finding

---

## Task 1: Scaffold the draft file

**Files:**
- Create: `docs/papers/2026-05-01-the-same-move-at-every-layer.md`

**Goal:** A skeleton with section headers, word budgets per section, an HTML-comment block at the top listing the spec's "don't say" sentences, and a placeholder for the figure reference.

- [ ] **Step 1.1: Create the draft scaffold**

Create the file with the following structure (filled-in headers, empty bodies):

```markdown
<!--
SPEC: docs/specs/2026-05-01-convergence-paper-design.md
DON'T SAY (grep these before publish):
  - "Helix is the first to"
  - "synthetic nervous system"
  - "bridge to embodied AGI"
  - "Our results show"
  - any sentence using Agentome and Helix as synonyms
WORD BUDGET: ~4,500 total. Section budgets in headers.
-->

# The Same Move at Every Layer

*Notes from a crowded field. Substack #2.*

## Opening (§1, ~400w)

## The shared move, defined (§2, ~300w)

## Layer 1 — Model internals: HOPE (§3, ~500w, Bucket 2 hedged)

## Layer 2 — KV cache: KVzip, KVPress (§4, ~500w, Bucket 2 hedged)

## Layer 3 — Retrieval index: SPLADE, RAPTOR, GraphRAG (§5, ~700w)

## Layer 4 — Agent memory: MemGPT, Letta (§6, ~700w)

## Layer 5 — Substrate: Helix as one Agentome-stack instance (§7, ~900w)

## What converges, what doesn't, what's next (§8, ~500w)

---

*Figure: see `figures/2026-05-01-layer-stack.md`*
```

- [ ] **Step 1.2: Commit**

```bash
cd f:/Projects/helix-context
git add docs/papers/2026-05-01-the-same-move-at-every-layer.md
git commit -m "docs(paper): scaffold convergence paper draft"
```

---

## Task 2: Draft §1 — Opening + Agentome/Helix distinction + verdict

**Files:**
- Modify: `docs/papers/2026-05-01-the-same-move-at-every-layer.md`

**Spec section:** §0 (terms), §1 (thesis), §3 row 1 (opening hook).

**Required content (from spec):**
- Honest hook in SIKE register: "I shipped Agentome thinking the metaphor was rare. It wasn't."
- Define **Agentome** (vision: stack of local tools — encyclopedia/calculator/library/refs/living memory) and **Helix** (engine, one substrate-layer instance) as distinct. Working names from planning.
- Disambiguation note: helix-context is unrelated to Helix editor / Helix DNA / bioinformatics tooling sharing the name. Link [github.com/SwiftWing21/helix-context](https://github.com/SwiftWing21/helix-context).
- State the verdict up front (no buried lede): convergence is real at multiple layers; Helix is on the map but not the headline; Agentome is the vision-level claim, Helix receipts are the evidence-level claim; *not the same claim*.
- Set the layer-stack frame.

- [ ] **Step 2.1: Draft §1 prose, target 400 words.**
- [ ] **Step 2.2: Verify don't-say grep is clean for this section.**

```bash
cd f:/Projects/helix-context
grep -nE "Helix is the first to|synthetic nervous system|bridge to embodied AGI|Our results show" docs/papers/2026-05-01-the-same-move-at-every-layer.md
# Expected: no output
```

- [ ] **Step 2.3: Verify Agentome/Helix are not used as synonyms** — read the section and confirm every mention of one or the other is at the correct level (vision vs engine).
- [ ] **Step 2.4: Word-count check.**

```bash
wc -w docs/papers/2026-05-01-the-same-move-at-every-layer.md
# Expected: ~400 (running total)
```

- [ ] **Step 2.5: Commit**

```bash
git add docs/papers/2026-05-01-the-same-move-at-every-layer.md
git commit -m "docs(paper): draft §1 opening + Agentome/Helix distinction"
```

---

## Task 3: Draft §2 — The shared move, defined

**Files:**
- Modify: `docs/papers/2026-05-01-the-same-move-at-every-layer.md`

**Spec section:** §3 row 2.

**Required content:**
- Two-part definition: *(a) state that persists past the request* and *(b) a policy that decides what's expressed into the active context.*
- Anything missing either half is **adjacent, not convergent**. Name LLMLingua here as adjacent (pure compression, no persistence policy) — the one explicit "off the map" example up front.
- This definition is the paper's load-bearing tool — every later section uses it. Mark this for the reader explicitly.

- [ ] **Step 3.1: Draft §2 prose, target 300 words.**
- [ ] **Step 3.2: Word-count check (running total ~700).**
- [ ] **Step 3.3: Commit**

```bash
git commit -am "docs(paper): draft §2 shared move definition"
```

---

## Task 4: Draft §3 — Layer 1: HOPE (Bucket 2 hedged)

**Files:**
- Modify: `docs/papers/2026-05-01-the-same-move-at-every-layer.md`

**Spec section:** §3 row 3. Bucket 2 = hedged voice required.

**Required content:**
- HOPE = nested-optimizer continual learning at the model-internals layer.
- Persistent state = the model's own weights/optimizer state.
- Selective-expression policy = the inner loop deciding what to absorb.
- Property callout: *same shape, lowest layer in the stack — the move appears below the API surface.*
- **Hedged voice:** use phrasings like "as I read it," "from the public write-ups." Do not claim first-hand engagement.

- [ ] **Step 4.1: Quick verification pass on HOPE.** Before drafting, confirm the basic claims via WebSearch/WebFetch. Goal is one paragraph of grounded summary; don't go deeper than that — Bucket 2 is intentional.
- [ ] **Step 4.2: Draft §3 prose, target 500 words, hedged.**
- [ ] **Step 4.3: Hedge-check** — read the section and confirm no sentence reads as first-hand. Look for "we tested," "I found," etc. — there should be none.
- [ ] **Step 4.4: Word-count check (running total ~1,200).**
- [ ] **Step 4.5: Commit**

```bash
git commit -am "docs(paper): draft §3 HOPE layer (hedged)"
```

---

## Task 5: Draft §4 — Layer 2: KVzip, KVPress (Bucket 2 hedged)

**Files:**
- Modify: `docs/papers/2026-05-01-the-same-move-at-every-layer.md`

**Spec section:** §3 row 4. Bucket 2 = hedged voice required.

**Required content:**
- KV cache as substrate, not per-request artifact.
- KVzip (Seoul National, Nov 2025) + KVPress (NVIDIA): persistent compressed KV across calls; eviction/retention is the selective-expression half.
- Property callout: *the cache is now a substrate — that's the move.*
- Hedged voice. KVzip credit as the SIKE trigger ("the paper that prompted this whole exercise") is appropriate and authentic.

- [ ] **Step 5.1: Quick verification pass on KVzip + KVPress.** Confirm basic claims via search.
- [ ] **Step 5.2: Draft §4 prose, target 500 words, hedged.**
- [ ] **Step 5.3: Hedge-check.**
- [ ] **Step 5.4: Word-count check (running total ~1,700).**
- [ ] **Step 5.5: Commit**

```bash
git commit -am "docs(paper): draft §4 KV-cache layer (hedged)"
```

---

## Task 6: Draft §5 — Layer 3: SPLADE, RAPTOR, GraphRAG

**Files:**
- Modify: `docs/papers/2026-05-01-the-same-move-at-every-layer.md`

**Spec section:** §3 row 5. Mixed buckets:
- SPLADE = Bucket 1 (integrated, can speak first-hand as a user).
- RAPTOR = Bucket 3 (read carefully). Use spec §6 source notes.
- GraphRAG = Bucket 3 (read carefully). Use spec §6 source notes.

**Required content:**
- SPLADE: sparse learned index; in Helix it's Tier 3.5 of the lane stack.
- RAPTOR: 100-token chunks → SBERT → GMM+BIC → gpt-3.5 summaries → recurse. Tree traversal vs collapsed tree retrieval. **Static post-construction.**
- GraphRAG: docs → entities/relations → Leiden community detection → LLM community summaries. Map-reduce at query time with self-rated helpfulness. **Static post-construction.**
- Property callout: *retrieval was already persistent; the convergence here is on the expression policy catching up — and on whether the index gets to evolve.* (Foreshadows Finding 2.)

- [ ] **Step 6.1: Re-read spec §6 source notes for RAPTOR + GraphRAG before drafting.** Don't paraphrase from memory.
- [ ] **Step 6.2: Draft §5 prose, target 700 words.**
- [ ] **Step 6.3: Cite-check** against spec §6: GMM+BIC for RAPTOR, Leiden for GraphRAG, "static post-construction" for both.
- [ ] **Step 6.4: Word-count check (running total ~2,400).**
- [ ] **Step 6.5: Commit**

```bash
git commit -am "docs(paper): draft §5 retrieval-index layer"
```

---

## Task 7: Draft §6 — Layer 4: MemGPT, Letta + Finding 1 callout

**Files:**
- Modify: `docs/papers/2026-05-01-the-same-move-at-every-layer.md`

**Spec section:** §3 row 6. Bucket 3, plus this is where **Finding 1** (the four-way split of selective-expression) lands.

**Required content:**
- MemGPT (Packer et al., 2310.08560): main context = system instructions + working context + FIFO queue (≈ RAM); external context = recall + archival storage (≈ disk). LLM-initiated function calls (`working_context_*`, `archival_memory_search`, `archival_memory_insert`, `recall_memory_search`). Pressure warning + `request_heartbeat=true`. OS-paging is the framing device.
- Letta: dual-agent. Primary handles requests; **sleep-time agent** asynchronously curates the primary's core in-context memory during idle periods. Separates *express* (sync) from *consolidate* (async).
- **Finding 1 callout** — name the four-way split of selective-expression policy across the roster (RAPTOR heuristic, GraphRAG LLM-rated, MemGPT LLM-self-edit-sync, Letta LLM-self-edit-async). The field has not agreed on *who* makes the expression decision, only that the decision exists as a distinct concern.
- Property callout: *this is where the move was named first — the layer it's most visible at.*

- [ ] **Step 7.1: Re-read spec §6 source notes for MemGPT + Letta before drafting.**
- [ ] **Step 7.2: Draft §6 prose, target 700 words.**
- [ ] **Step 7.3: Cite-check** — every function name (e.g., `archival_memory_search`) and every architecture term (e.g., "sleep-time agent") matches spec §6 verbatim.
- [ ] **Step 7.4: Confirm Finding 1 four-way table is present.**
- [ ] **Step 7.5: Word-count check (running total ~3,100).**
- [ ] **Step 7.6: Commit**

```bash
git commit -am "docs(paper): draft §6 agent-memory layer + Finding 1"
```

---

## Task 8: Draft §7 — Layer 5: Helix receipts + BM25 honest tax + Findings 2/3

**Files:**
- Modify: `docs/papers/2026-05-01-the-same-move-at-every-layer.md`

**Spec section:** §3 row 7 + §5 receipts inventory + §6 Findings 2 & 3. **This is the load-bearing section. The paper's credibility lives here.**

**Required content (every numeric claim must be sourced):**
- Genome shape: 17,623 genes, 670 MB. Three chromatin tiers: 12,401 OPEN / 1,895 EUCHROMATIN / 3,327 HETEROCHROMATIN. *(Source: AGENTOME_PART_II_DRAFT.md.)*
- Self-organization: 13 retrieval lanes. Cymatics works, E8 does not.
- Selective-expression policy in code: ΣĒMA cosine fallthrough as OPEN→HETEROCHROMATIN bridge; density-gated admission as promotion rule. Pointer to `helix_context/genome.py`.
- **The honest tax:** BM25 8/8 content_full / 151 ms vs `helix_only` 4/8 / 1793 ms. Three stacked failures named plainly: population dilution at 17K, PKI tier broken on this genome, `helix_only`'s 4555-char assembly ceiling. *(Source: RESEARCH_REVIEW_2026-04-22.md.)* Frame: "this is what it looks like to be on the map but not yet the best instance on it."
- Cross-process / cross-agent reach: shared genome across Laude/Taude/Raude/Gemini handles; persistent sparse state across process restarts.
- **Finding 2 callout:** static-vs-evolving substrate splits cleanly along layer (RAPTOR/GraphRAG static; MemGPT/Letta evolve). Helix sits across both — retrieval-layer work with memory-layer dynamism. The "narrow and mine" position.
- **Finding 3 callout:** Letta's sleep-time agent is the closest direct convergence with Helix's chromatin-promotion mechanism — both are async curators between requests. Callout, not a comparison-table fight.

- [ ] **Step 8.1: Cite-check pass before drafting** — open AGENTOME_PART_II_DRAFT.md and RESEARCH_REVIEW_2026-04-22.md and copy each numeric claim into the working draft as a quote with source line.
- [ ] **Step 8.2: Draft §7 prose, target 900 words.**
- [ ] **Step 8.3: Receipts cite-check** — every number in the section appears verbatim in one of the source docs. Verify with grep:

```bash
grep -nE "17,623|670 MB|12,401|1,895|3,327|13 retrieval lanes|4555|151 ms|1793 ms" docs/papers/2026-05-01-the-same-move-at-every-layer.md
grep -nE "17,623|670 MB|12,401|1,895|3,327" docs/papers/AGENTOME_PART_II_DRAFT.md
grep -nE "151 ms|1793 ms|4555" docs/research/RESEARCH_REVIEW_2026-04-22.md
# Every number in the first grep must also appear in one of the latter two.
```

- [ ] **Step 8.4: Confirm Findings 2 and 3 callouts are present.**
- [ ] **Step 8.5: Confirm the BM25-loss is stated plainly, not buried.** Read the section and confirm a reader skimming would see it.
- [ ] **Step 8.6: Word-count check (running total ~4,000).**
- [ ] **Step 8.7: Commit**

```bash
git commit -am "docs(paper): draft §7 Helix receipts + honest tax + Findings 2/3"
```

---

## Task 9: Draft §8 — What converges, what doesn't, what's next

**Files:**
- Modify: `docs/papers/2026-05-01-the-same-move-at-every-layer.md`

**Spec section:** §3 row 8.

**Required content:**
- **Three claims:**
  1. The two-part move is the convergence; layer is the variable.
  2. Things that look adjacent but aren't the same move (LLMLingua revisited; pure caching without policy) — name them, place them off the map honestly.
  3. The interesting open question: when the move appears at every layer simultaneously, do the layers compose or interfere? Helix's substrate layer running underneath an agent-memory layer running on top of a KV-cache layer is not yet a tested configuration.
- Pointer to the post-v1.0 follow-up (head-to-head benchmarks). Frame the deferral honestly: *Helix isn't v1.0 yet, so a benchmark fight would be premature; that's the next post.*

- [ ] **Step 9.1: Draft §8 prose, target 500 words.**
- [ ] **Step 9.2: Word-count check — final target ~4,500 (±300 acceptable).**
- [ ] **Step 9.3: Commit**

```bash
git commit -am "docs(paper): draft §8 closing"
```

---

## Task 10: Figure spec — layer-stack diagram

**Files:**
- Create: `docs/papers/figures/2026-05-01-layer-stack.md`

**Goal:** Specify the one figure the paper needs. Decide between three production options; do not produce final art in this task.

**Required content of the figure:**
- 5 horizontal layers stacked: Substrate (bottom) → Agent Memory → Retrieval Index → KV Cache → Model Internals (top).
- Inside each layer: the two-part move shown twice — a "persistent state" box and a "selective expression" arrow.
- Each layer labeled with 1-3 example systems from the roster.
- Helix highlighted at the substrate layer (ringed, not throned). One-line caption: *"Helix is one instance at one layer. The shape is the field's, not Helix's."*

- [ ] **Step 10.1: Write the figure spec file** with: dimensions, layer labels, example-system labels per layer, the persistent-state/selective-expression visual motif, the highlighting convention, and the caption.
- [ ] **Step 10.2: Choose production approach** and document the choice in the spec file:
  - **A.** ASCII / Unicode box-drawing (in-paper, fastest, lowest effort)
  - **B.** Mermaid diagram (renders on Substack? verify before committing to this)
  - **C.** Hand-drawn / commissioned image (highest quality; adds a real time cost)
- [ ] **Step 10.3: Commit the spec.** Production of the actual figure happens in a follow-up task once the choice is made.

```bash
git add docs/papers/figures/2026-05-01-layer-stack.md
git commit -m "docs(paper): figure spec for layer-stack diagram"
```

---

## Task 11: Verification pass — full-paper checks

**Files:**
- Read-only: `docs/papers/2026-05-01-the-same-move-at-every-layer.md`

**Goal:** End-to-end gate before human review.

- [ ] **Step 11.1: Don't-say grep (must produce no output):**

```bash
cd f:/Projects/helix-context
grep -nE "Helix is the first to|synthetic nervous system|bridge to embodied AGI|Our results show" docs/papers/2026-05-01-the-same-move-at-every-layer.md
```

- [ ] **Step 11.2: Agentome/Helix synonym check.** Read the paper end-to-end. For every mention of either term, confirm: "Agentome" refers to the vision-level stack; "Helix" refers to the engine. Flag any sentence that swaps them.
- [ ] **Step 11.3: Word-count check.**

```bash
wc -w docs/papers/2026-05-01-the-same-move-at-every-layer.md
# Expected: 4,200-4,800. Outside that range = reshape.
```

- [ ] **Step 11.4: Numeric cite-check** (re-run from Task 8.3 against the full draft, not just §7).
- [ ] **Step 11.5: Bucket-2 hedge check.** Re-read §3 (HOPE) and §4 (KVzip/KVPress). Confirm no first-hand-knowledge phrasing leaked in.
- [ ] **Step 11.6: Voice continuity check.** Read just the opening paragraph of each section in sequence. Confirm voice is consistent first-person field-report throughout.
- [ ] **Step 11.7: Verdict-up-front check.** A skim-only reader of §1 should know the verdict. Verify.
- [ ] **Step 11.8: Don't-Say #4 check** (Agentome/Helix synonym) — already covered by 11.2 but re-confirm explicitly.
- [ ] **Step 11.9: Out-of-scope check.** Grep for accidental scope creep:

```bash
grep -nE "CWoLa|AUC=0\.631|Three Constraints|Three Physical Constraints|geometric substrate|AGI" docs/papers/2026-05-01-the-same-move-at-every-layer.md
# Expected: no matches (these are deferred to other papers).
```

- [ ] **Step 11.10: Commit any fixes.**

```bash
git commit -am "docs(paper): verification-pass fixes"
```

---

## Task 12: Human review handoff

**Files:**
- Read-only: `docs/papers/2026-05-01-the-same-move-at-every-layer.md`

**Goal:** Hand the draft to maxbachaud for review before publication. This task is not implementation; it's an explicit handoff.

- [ ] **Step 12.1: Produce a handoff summary for the user including:**
  - Final word count.
  - Output of all Task 11 checks.
  - Any sections the agent flagged as weakest / needing the human's eye.
  - The figure decision from Task 10.2 and what's left to produce the actual figure.
  - Reading time estimate.
  - Anything in the spec that was *not* honored, with a brief reason.

- [ ] **Step 12.2: Stop.** Do not publish. Do not push to remote. Do not auto-commit further. Wait for the user.

---

## Out of scope for this plan

- **Producing the final figure art** (Task 10 stops at spec).
- **Publishing to Substack** (manual; user decides).
- **Pushing to remote** (per user's global git policy: only on explicit request).
- **The post-v1.0 head-to-head benchmark follow-up** (separate plan, separate paper).
- **CWoLa, Three Constraints, or any other deferred topic** (explicitly out per spec §7).
- **A spec-document-reviewer or plan-document-reviewer subagent loop** — these subagents are not available in this environment. Skipping with this note; the human review at Task 12 is the gate.

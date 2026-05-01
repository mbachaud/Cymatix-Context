# Design — *The Same Move at Every Layer* (Substack #2)

**Status:** Design approved 2026-05-01. Ready for implementation plan.
**Date:** 2026-05-01
**Author:** maxbachaud
**Predecessor:** [Agentome (Substack #1)](https://mbachaud.substack.com/p/agentome) · [SIKE_POST_DRAFT.md](../papers/SIKE_POST_DRAFT.md) (paused, superseded by this spec)
**Successor (deferred):** post-v1.0 head-to-head benchmark follow-up

---

## 0. Terms used in this paper

The paper uses two names that must not be conflated. The distinction is load-bearing — it is what keeps the "evidence > hero" framing intact.

- **Agentome** — the public-facing concept this Substack series is pursuing: a stack of local tools that gives any model an encyclopedia, a calculator, a library, reference docs, and a *living* memory stack. Working name; not necessarily the final term. Agentome is the *vision* — the shape of the thing once it exists across multiple models, multiple tools, and multiple machines.
- **Helix (helix-context)** — the core engine / substrate-layer component currently being built. One concrete piece of what an Agentome stack would contain. The receipts in §7 are *Helix* receipts. The convergence claim is at the *Agentome* level — i.e., the field is converging on the *kind of stack* Agentome describes, and Helix is one instance of one layer of it.

**Naming note.** Both *Agentome* and *helix-context* are working names chosen during the project's planning phase and have not been changed since. *helix-context* in this paper refers exclusively to the project at [github.com/SwiftWing21/helix-context](https://github.com/SwiftWing21/helix-context) and is **not associated with any other product, library, or service that uses the word "Helix"** — including but not limited to the Helix editor, Helix DNA, or any bioinformatics tooling sharing the name. The shared term is coincidence; there is no organizational, technical, or licensing relationship.

**Why this matters for the paper.** When the paper says "Helix is on the map but not the headline," that is a substrate-layer claim. When the paper says "the field is converging on this shape," that is the Agentome-level claim. The Substack audience reads continuity from Agentome (Substack #1); the convergence map names what Agentome was always going to need to coexist with.

---

## 1. Thesis & verdict

**Thesis (one sentence).** Across model internals, KV cache, retrieval index, agent memory, and external substrate, independent systems are converging on the same two-part move — *state that persists past the request, plus a policy for selective expression* — and Helix is one instance of this convergence rather than an exception to the field.

**Verdict the paper delivers.** The convergence is real and is happening at multiple layers simultaneously. The interesting question is no longer *who got there first* but *what shape the answer takes when it appears at every layer at once.* That shape — a stack of local tools providing persistent state and selective expression to any model — is what *Agentome* names. Helix earns the right to be on the map because it shows the move at the substrate layer with running receipts; it does not earn the right to be the headline. The Agentome framing is the vision-level claim; the Helix receipts are the evidence-level claim. They are not the same claim.

**What the paper explicitly is NOT:**
- Not a benchmark fight (deferred to a post-v1.0 follow-up).
- Not a claim that Helix invented the move.
- Not a survey — the roster is bounded and chosen for layer-coverage, not completeness.
- Not a retraction of Agentome — Agentome's metaphor still holds; this paper situates it.

## 2. Audience, length, voice

- **Substack #2, deeper-technical.** ~4,500 words.
- **Voice.** First-person, field-report register. Same author as Agentome; closer to *Three Constraints* in rigor.
- **Engagement honesty.** Bucket 2 systems get hedged language ("as I read it," "from the public write-ups") so the paper never overclaims first-hand knowledge it doesn't have.

**Sentences this paper will not contain:**
- "Helix is the first to..."
- "...synthetic nervous system..." or "...bridge to embodied AGI..." (the SIKE retraction holds).
- "Our results show..." (no v1.0 benchmark claim).
- Any sentence that uses *Agentome* and *Helix* as synonyms. They are deliberately distinct in this paper (see §0).

**One sentence this paper *will* contain:** the BM25-beats-`helix_only` finding from [RESEARCH_REVIEW_2026-04-22.md](../research/RESEARCH_REVIEW_2026-04-22.md), plainly stated. That is the load-bearing honesty beat.

## 3. Structure — layer-by-layer with property callouts

Eight sections, ~4,500 words.

| § | Section | ~Words |
|---|---|---|
| 1 | Opening — honest hook, Agentome-vs-Helix distinction, stack-frame, verdict up front | 400 |
| 2 | The shared move, defined (persistence + selective expression) | 300 |
| 3 | Layer 1 — Model internals: HOPE | 500 |
| 4 | Layer 2 — KV cache: KVzip, KVPress | 500 |
| 5 | Layer 3 — Retrieval index: SPLADE, RAPTOR, GraphRAG | 700 |
| 6 | Layer 4 — Agent memory: MemGPT, Letta | 700 |
| 7 | Layer 5 — Substrate: Helix as one Agentome-stack instance, with receipts | 900 |
| 8 | What converges, what doesn't, what's next | 500 |

**Property callouts** are inline asides at each layer (no separate property section), pointing forward/back to the same property at another layer. They give the layer-by-layer map property-level structural punch without becoming a table-heavy survey.

## 4. Citation roster — three engagement buckets

**Bucket 1 — Integrated / measured (first-hand).**
- **Headroom** (Chopra, 2025) — integrated via [`headroom_bridge.py`](../../helix_context/headroom_bridge.py); A/B-benchmarked in [BENCHMARKS.md](../benchmarks/BENCHMARKS.md), including the *neutral* v2 harness A/B finding (≈+2pp, within noise).
- **SPLADE** — Tier 3.5 sparse-semantic lane, see [PIPELINE_LANES.md](../architecture/PIPELINE_LANES.md).

**Bucket 2 — Read but not engaged (hedged language required).**
- **KVzip** (Seoul National, 2025-11) — SIKE trigger.
- **KVPress** (NVIDIA).
- **HOPE** (Google) — model-internal continual learning / nested optimizers.

**Bucket 3 — Read carefully for this paper (pre-draft reading pass complete, see §6 below).**
- **MemGPT** (Packer et al., 2310.08560).
- **Letta** (production framework / MemGPT successor).
- **RAPTOR** (Sarthi et al., 2401.18059).
- **GraphRAG** (Edge et al. / Microsoft, 2404.16130).

**Cut from the SIKE roster.** LLMLingua, Mem0 — the convergence claim doesn't lean on them; including without reading would re-introduce the SIKE failure mode.

## 5. Helix receipts (§7 inventory)

**Genome shape** *(from [AGENTOME_PART_II_DRAFT.md](../papers/AGENTOME_PART_II_DRAFT.md))*
- 17,623 genes, 670 MB.
- Three chromatin tiers: 12,401 OPEN / 1,895 EUCHROMATIN / 3,327 HETEROCHROMATIN.
- Ingest throughput ~5,500 genes/hr after parallel pipeline.

**Self-organization** *(Part II)*
- 13 retrieval lanes emerged, all wired.
- "Cymatics works, E8 does not." One-sentence honesty beat.

**Selective-expression policy in code**
- ΣĒMA cosine fallthrough as the OPEN→HETEROCHROMATIN bridge.
- Density-gated admission as the promotion rule.
- Pointer into [`helix_context/genome.py`](../../helix_context/genome.py) for chromatin promotion logic.

**The honest tax** *(from [RESEARCH_REVIEW_2026-04-22.md](../research/RESEARCH_REVIEW_2026-04-22.md))*
- BM25 at 8/8 content_full / 151 ms vs `helix_only` at 4/8 / 1793 ms.
- Three stacked failures named plainly: population dilution at 17K, PKI tier broken on this genome, `helix_only`'s 4555-char assembly ceiling.
- Framing: "this is what it looks like to be on the map but not yet the best instance on it."

**Cross-process / cross-agent reach** *(narrow-defensible from SIKE)*
- Shared genome across Laude / Taude / Raude / Gemini handles.
- Persistent sparse state across process restarts — the substrate-layer specificity.

**Deliberately NOT cited.** CWoLa AUC=0.631 (Sprint 3) — off-axis; saved for a different paper. *Three Constraints* geometry — different paper, would muddy the spine. Benchmark numbers beyond the BM25 comparison — risks turning §7 into the deferred numbers fight.

## 6. Pre-draft reading-pass findings

The reading pass on Bucket 3 (MemGPT, Letta, RAPTOR, GraphRAG) produced three findings that should land *in the paper* — they are the actual convergence content, not assumptions:

**Finding 1 — The "selective expression" half of the move splits four ways across the roster:**

| System | Who decides | Mode |
|---|---|---|
| RAPTOR | heuristic (cosine top-k) | sync |
| GraphRAG | LLM-rated (helpfulness scores in map-reduce) | sync |
| MemGPT | LLM self-edit via tool calls | sync |
| Letta sleep-time agent | LLM self-edit via separate agent | **async** |

The field has not agreed on *who* makes the expression decision, only that the decision exists as a distinct concern. Naming this split is itself a contribution.

**Finding 2 — Static vs. evolving substrate splits cleanly along layer.** Retrieval-layer systems (RAPTOR, GraphRAG) explicitly freeze the index after construction. Agent-memory-layer systems (MemGPT, Letta) evolve continuously. **Helix sits across both:** retrieval-layer work (lanes, ΣĒMA) with memory-layer dynamism (chromatin promotion). This is a stronger "narrow and mine" position than the SIKE draft proposed.

**Finding 3 — Letta's sleep-time agent is the closest direct convergence with Helix's chromatin-promotion mechanism.** Both are async curators that run between requests, deciding what becomes hot. Worth a callout in §6/§7, not a comparison-table fight.

### Source notes (for cite-checking during draft)

- **MemGPT.** Main context = system instructions + working context + FIFO queue (≈ RAM); external context = recall + archival storage (≈ disk). LLM-initiated function calls: `working_context_*`, `archival_memory_search`, `archival_memory_insert`, `recall_memory_search`. Pressure warning + `request_heartbeat=true` for chained calls. OS-paging is the framing device.
- **Letta.** Dual-agent: primary handles user requests and reads external memory; **sleep-time agent** asynchronously curates the primary's core in-context memory during idle periods. Separates *express* (sync, request-driven) from *consolidate* (async, idle-driven).
- **RAPTOR.** 100-token chunks → SBERT → GMM+BIC clustering → gpt-3.5 summarization → re-embed parents → recurse. Two retrieval modes: tree traversal (layer-by-layer top-k) vs collapsed tree (flatten + single top-k). Static post-construction.
- **GraphRAG.** Source docs → chunks → LLM entity/relation extraction → graph → hierarchical Leiden community detection → LLM community summaries at each level. Query: map-reduce with self-rated helpfulness, sorted descending, reduced. Static post-construction.

## 7. Out of scope

- Benchmark fight against neighbors (deferred to post-v1.0 follow-up).
- CWoLa Sprint 3 training results.
- *Three Constraints* geometric-substrate thesis.
- LLMLingua, Mem0 (cut from roster).
- v1.0 readiness claim of any kind.

## 8. Next step

Invoke `superpowers:writing-plans` to produce an implementation plan for drafting the paper itself, including the order of section drafts, the figure(s) needed (probably one — the layer-stack diagram), the receipts-verification pass, and the publish checklist.

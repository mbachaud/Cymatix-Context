<!--
SPEC: docs/specs/2026-05-01-convergence-paper-design.md
PLAN: docs/superpowers/plans/2026-05-01-convergence-paper.md

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

I shipped [Agentome](https://mbachaud.substack.com/p/agentome) thinking the metaphor was rare. It wasn't. The move the metaphor describes — compress the long tail, keep a small working set hot, route on cheap signals before paying for the expensive ones — is showing up at every layer of the stack, independently, this year. My SIKE-stage draft of this paper had a sentence claiming we'd been first to a particular trick. That sentence is gone. I was wrong about the field's timing, not about the trick.

Two terms before we go further, because I'm going to use both and they are not the same thing.

*Agentome* is the public-facing concept this Substack series is pursuing: a stack of local tools that gives any model an encyclopedia, a calculator, a library, reference docs, and a *living* memory stack that grows with use rather than being re-fetched per turn. It's a vision-level claim about what shape the per-user AI substrate ought to take. The name is a working title from my planning notes; I reserve the right to rename it once something better lands.

*Helix* (working name `helix-context`) is the core engine I'm actually building. It is one concrete component of what an Agentome-shaped stack would contain — the substrate layer, specifically. Every receipt later in this paper is a Helix receipt: a measurement from a real engine running on real hardware. Helix is also a working title.

One disambiguation: `helix-context` here is unrelated to the Helix editor, Helix DNA, and the various bioinformatics tools that share the name. The collision is coincidence, nothing more. Repo: [github.com/SwiftWing21/helix-context](https://github.com/SwiftWing21/helix-context).

So here is the verdict, up front, for the reader who only skims §1:

- Convergence is real, and it is happening at multiple layers of the stack at once. The same compress-and-route move shows up in model internals, in the KV cache, in the retrieval index, in agent memory, and at the substrate.
- Helix is on the map but is not the headline. The headline is the convergence.
- The Agentome-shaped stack is the *vision-level* claim of this series. The Helix numbers later in this paper are the *evidence-level* claim. They support each other, but they are not the same claim, and I am going to be careful not to pretend otherwise.

The frame for the rest of the paper is a five-layer walk: model internals, KV cache, retrieval index, agent memory, substrate. At each layer, the same two-part move appears — and at each layer, it appears in work that wasn't talking to ours.

## The shared move, defined (§2, ~300w)

Here's the move, stated tightly. A system is doing it when both halves are present:

- **(a) State that persists past the request.** Something — an index, a KV cache, the weights, a memory tier, a substrate on disk — outlives a single LLM call and accumulates across turns or sessions. It is not rebuilt from scratch each time the model is asked something.
- **(b) A policy that decides what's expressed into the active context.** Something — a hand-tuned heuristic, a learned scorer, a graph traversal, the LLM itself reaching for tools — chooses, for *this* request, which slice of the persisted state becomes hot. Most of it stays cold; a small working set is selected and shown to the generator.

Both halves are required. A vector database with no selection policy on top is just storage; it isn't doing the move. A clever prompt-rewriter with no persistence is just a compressor; it isn't doing the move either.

The cleanest adjacent-but-not-convergent example is **LLMLingua**. LLMLingua compresses prompts in flight — it shrinks whatever you hand it before the model reads it. There is no state across requests; the next call starts over. Useful, but it sits next to the move rather than on it: half (b), none of (a).

This definition is load-bearing for the rest of the paper. Every later section uses it as a test: when I say a system is on the map, I mean it has both halves, in that shape. When I say a system is adjacent, I mean one half is missing or vestigial.

One thing to flag before the layer walk begins: half (a) is the easy half. Persistence is everywhere — caches, indexes, weights, files. The interesting variation, and where the field is actually splitting apart, is in half (b). Heuristic vs. learned vs. LLM-self-edit. Synchronous on the hot path vs. asynchronous between turns. That's what the next five sections walk through.

## Layer 1 — Model internals: HOPE (§3, ~500w, Bucket 2 hedged)

Start at the bottom of the stack. **HOPE** is the architecture Google Research introduced alongside the *Nested Learning* paradigm, presented at NeurIPS 2025. From the public write-ups, it's positioned as a self-modifying recurrent architecture that treats training and inference as the same kind of process running at different rates, rather than as two phases separated by a deployment boundary. The framing seems to be: a model is not one optimizer wrapped around frozen weights, but a stack of nested optimizers, each with its own update frequency, each carrying its own slice of state forward. The slogan from the blog post — that architecture and optimizer are "fundamentally the same concepts" at different levels — is the part I want to take seriously here.

Map that onto the two-part move from §2 and it lines up cleanly, if I'm reading the materials correctly.

Half (a), persistence, is the model's *own internal state* — not weights frozen at training time, but state that the inner loops continue to update across calls. As I read it, HOPE leans on a Continuum Memory System: blocks of state that update at different frequencies, so that some of the model's parameters behave like fast scratch memory, some behave like slower consolidated memory, and some behave like the conventional, almost-static weights we're used to. Persistence isn't bolted on — it *is* the architecture.

Half (b), selective expression, is the inner loop itself. The Titans line of work this builds on prioritizes memory updates by how *surprising* an input is; HOPE's self-referential variant, if I understand correctly, lets the model influence its own update rule rather than running a fixed one. Which slice of incoming experience gets written into which memory tier, at which frequency — that is the selection policy. It is learned, it is in-architecture, and it is running on the same hot path as generation rather than off in a nightly fine-tune job.

**Property callout: same shape, lowest layer.** The thing I want to flag is that this is the *same two-part move* the rest of the paper will trace through KV caches, indexes, agent memory, and substrate — but it appears here below the API surface. Most of the field's continual-learning conversation lives one layer up, framed as a *training* problem (LoRA, periodic fine-tunes, RLHF cycles). Nested Learning's contribution, as I read it, is to relocate that conversation into runtime: continual learning as a property of how the model *runs*, not of how it was last trained. That is a layer-shift, not just a technique.

At every layer above this one, the persistence-and-selection move recurs — but always above the model boundary, working on activations or text or files. HOPE is what the same move looks like when it dives below that boundary and operates on the model's own state directly. ([Google Research — *Introducing Nested Learning*](https://research.google/blog/introducing-nested-learning-a-new-ml-paradigm-for-continual-learning/))

## Layer 2 — KV cache: KVzip, KVPress (§4, ~500w, Bucket 2 hedged)

<!-- TODO Task 5 -->

## Layer 3 — Retrieval index: SPLADE, RAPTOR, GraphRAG (§5, ~700w)

<!-- TODO Task 6 -->

## Layer 4 — Agent memory: MemGPT, Letta (§6, ~700w)

<!-- TODO Task 7 -->

## Layer 5 — Substrate: Helix as one Agentome-stack instance (§7, ~900w)

<!-- TODO Task 8 -->

## What converges, what doesn't, what's next (§8, ~500w)

<!-- TODO Task 9 -->

---

*Figure: see [`figures/2026-05-01-layer-stack.md`](figures/2026-05-01-layer-stack.md)*

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

[KVzip](https://arxiv.org/abs/2505.23416) is the paper that forced the rewrite of this Substack series. I had a SIKE-stage draft that leaned on a claim about KV-cache reuse being unusual outside our setup; KVzip — a Seoul National University paper from this past November — was the moment I realized that wasn't true. The convergence wasn't in some adjacent field I hadn't looked at; it was right above HOPE's layer, working on the artifact every transformer already produces. So Layer 2 starts here.

**What KVzip is.** As I read it from the public write-ups, KVzip is *eviction-style* compression of the KV cache itself — not quantization of the existing entries, but a policy that ranks KV pairs by importance and drops the low-scoring ones. The framing seems to be that the cache, once produced for some long context, can be shrunk roughly 3–4× with minimal quality loss and then *reused across different downstream queries against the same context.* That last part is the load-bearing claim for §2's vocabulary. Half **(a) — state that persists past the request** — is the compressed cache itself, designed explicitly to outlive the call that produced it. Half **(b) — selective expression policy** — is the importance score: if I understand correctly, the underlying LLM is used to estimate how much each KV pair contributes to reconstructing the original context, and pairs that contribute least are evicted. The selection happens once, against the *context*, not per-query — which is what makes the result query-agnostic. Both halves are present, in that shape.

**What KVPress is.** [KVPress](https://github.com/NVIDIA/kvpress) is NVIDIA's open-source framework for KV-cache compression. From the README it's a *library* rather than a single algorithm: it ships a stable of "presses" — RandomPress, SnapKV, StreamingLLM, ExpectedAttention, TOVA, ThinKPress, KVzipPress, and others — behind a common interface that hooks into Transformers prefill (and, experimentally, decode). Most of the methods it ships are per-request: half (b) is the whole game, with half (a) reduced to "the cache lives as long as this generation does." But the framework treats compression as *infrastructure* — a press you attach to a model — rather than as a research artifact. The framing seems to be that this is no longer an academic curiosity; the platform vendors are shipping the move as a library you import.

**Property callout.** The thing to flag at this layer is the same shape that showed up in HOPE, one notch up: *the cache wants to outlive the request.* Some of these methods are still squarely per-call — they're half (b) running against ephemeral state. But KVzip in particular, and the direction KVPress points at by exposing it as one press among many, is the cache stopping behaving like a per-request artifact and starting to behave like a substrate. State you build once and select from many times. Persistence first; selection second.

At the next layer up, that asymmetry inverts. The retrieval index has always outlived the request — half (a) was solved before LLMs arrived. The convergence at Layer 3 is on the *selection* half catching up.

## Layer 3 — Retrieval index: SPLADE, RAPTOR, GraphRAG (§5, ~700w)

Layer 3 is where half (a) has the longest tenure. Databases outlived requests before LLMs were a category — a retrieval index is, by construction, state that persists past the call. The interesting question at this layer is what's happening to half (b). The expression policy — *which slice of the index becomes hot for this query* — is the half that's been moving, and it's been moving in three different directions at once. Learned sparse weights (SPLADE). Hierarchical summarization with cosine descent (RAPTOR). Graph-structured retrieval with LLM-rated reduction (GraphRAG). Same layer, three flavors of "smarter."

**SPLADE.** [SPLADE](https://arxiv.org/abs/2107.05720) is a sparse learned index. Each query and document is encoded into a sparse vector over the model's vocabulary; non-zero weights are term importances the encoder *learned* rather than computed from corpus statistics. The result behaves like BM25 in shape — sparse, invertible, friendly to inverted-index infrastructure — but the weights carry semantic signal. Map onto the move and the asymmetry is clear: half (a) is trivial here, since a posting list has always persisted. The novel half is (b). Selection used to be hand-crafted IDF; SPLADE makes it a trained function of the query, while keeping the storage shape that decades of retrieval engineering already know how to scale. In our stack, SPLADE sits at Tier 3.5 of the lane stack — wedged deliberately between BM25 and dense cosine, a sparse-semantic complement that catches the queries where IDF is too literal and dense embeddings are too gauzy.

**RAPTOR.** [RAPTOR](https://arxiv.org/abs/2401.18059) builds the persistent half into a tree. At index time, the corpus is cut into ~100-token chunks, each chunk is SBERT-embedded, and a Gaussian Mixture Model clusters the embeddings — with BIC choosing the cluster count rather than a fixed *k*. Each cluster is summarized by gpt-3.5-turbo, the summaries are re-embedded, and the procedure recurses upward until further clustering becomes infeasible. The output is a tree of summaries layered over the leaves. At query time there are two retrieval modes: *tree traversal*, which descends layer by layer taking top-k cosine matches at each level, and *collapsed tree*, which flattens every level into one pile and takes a single top-k. Mapped onto the move: half (a) is the tree itself, **static post-construction** — once built, the summaries don't update. Half (b) is **heuristic cosine** at each layer. Selection here is smarter than flat dense retrieval mostly because the *targets* are richer (summaries, not raw chunks); the policy choosing among them is still vector similarity.

**GraphRAG.** [GraphRAG](https://arxiv.org/abs/2404.16130) takes the same impulse — build hierarchy at index time, retrieve from it at query time — and routes it through a graph instead of a tree. The indexing pipeline runs source documents through chunking, then uses an LLM to extract entities and relations into a knowledge graph, then runs **hierarchical Leiden community detection** to find nested clusters of densely-connected nodes, then has the LLM write a summary for every community at every level. The persistent artifact is the graph plus its layered community summaries. At query time the strategy is map-reduce: community summaries are shuffled into chunks, the LLM writes a partial answer for each chunk *along with a self-rated helpfulness score*, the partials are sorted by score, and the top ones reduce into a final answer. Half (a) is the graph and its summaries — again **static post-construction**. Half (b) is the part that breaks from RAPTOR: selection isn't cosine, it's the LLM rating its own partial answers. The policy is itself a model call.

So three techniques, one layer, three different answers to "how do we choose what's hot." Learned weights, vector geometry, model self-rating. The two-part-move test passes for all three — but it passes by appealing to wildly different machinery for the same half. That's worth flagging on its own: convergence on the *shape* of the move does not imply convergence on its *implementation*, and Layer 3 is the cleanest place to see that. The other thing worth flagging — and it's a setup, not a conclusion — is that both RAPTOR and GraphRAG explicitly freeze their persistent state once the index pipeline finishes. The tree is static. The graph is static. That's a Layer-3 trait.

Above the retrieval index, the substrate stops being static. At Layer 4, half (a) starts updating on its own.

## Layer 4 — Agent memory: MemGPT, Letta (§6, ~700w)

Layer 4 is where the move was *named first*. [MemGPT](https://arxiv.org/abs/2310.08560) — Packer et al., 2023 — reached for an OS-paging analogy and built an LLM-memory architecture around it, and that act of naming is a lot of why this layer is the one most readers reach for when they hear "AI memory." The vocabulary the rest of the field now uses for memory tiers came from here. It is also the layer where the substrate stops being static: the index doesn't have to freeze once it's built; persistence and selective expression can run as a live loop.

**MemGPT.** Run the two-part move test. Half **(a) — persistence —** is the two-tier memory hierarchy modeled on virtual memory. *Main context* is the in-context tier (≈ RAM): system instructions, a working context block the model can edit, and a FIFO queue of recent messages. *External context* is the out-of-context tier (≈ disk): *recall storage* for short-term message history, and *archival storage* for long-term notes the agent has chosen to keep. Both tiers outlive any single call. Half **(b) — the selective-expression policy — is the LLM itself**, exercised through a fixed set of function calls. `archival_memory_search` retrieves from the long-term tier; `archival_memory_insert` writes to it; `recall_memory_search` retrieves from the short-term tier; the `working_context_*` family lets the model directly edit its in-context working block. When the main context fills, MemGPT issues a *pressure warning* back to the model so it can decide what to evict, what to push to archival, and what to summarize — the OS-paging analogy carried all the way through. A `request_heartbeat=true` keyword in a function call lets the model chain these moves across multiple steps without waiting for new user input. The contribution wasn't a new retrieval algorithm; it was making the LLM the policy and giving the field the language ("hot," "cold," "evict," "page in") to talk about that arrangement as one architecture.

**Letta.** [Letta](https://www.letta.com/) is the production framework that grew out of MemGPT, and its meaningful architectural addition is the **dual-agent split**. The *primary agent* handles user requests on the hot path: it executes tasks, reads external memory through the same MemGPT-style tool calls, and serves the response. Crucially, in this configuration the primary agent does **not** directly edit its own core (in-context) memory. The *sleep-time agent* runs **asynchronously, during idle periods**, manages its own scratch memory, and is the one that curates the primary's core in-context block. Read against §2's vocabulary, what Letta has done is separate the *express* decision from the *consolidate* decision: expression stays synchronous and request-driven, while consolidation moves off the hot path and onto an idle-time schedule. Same move, two clocks.

**The first cross-layer finding.** With Layer 4 on the table, the convergence map sharpens enough to state a finding the prior layers were only setting up. Reach back to RAPTOR and GraphRAG from §5 and lay all four selection policies side by side:

| System | Who decides what's expressed | Mode |
|---|---|---|
| RAPTOR | heuristic (cosine top-k at each tree level) | sync |
| GraphRAG | LLM-rated (self-scored helpfulness in map-reduce) | sync |
| MemGPT | LLM self-edit via tool calls | sync |
| Letta sleep-time agent | LLM self-edit via a separate agent | **async** |

The selection decision exists, in identifiable form, in every one of these systems. *Who* makes it — a fixed cosine rule, an LLM rating its own partial answers, an LLM editing its own memory through tool calls, or an entirely separate LLM running off the hot path — splits four ways across four otherwise unrelated projects. The field has converged on the *existence and shape* of the policy without converging at all on its implementation. That's the finding, and it's the first one this paper can make without hedging: not "everyone is doing the same thing," but "everyone agrees this is a thing, and disagrees about everything else."

The move is most legible here, which is partly why it's easy to mistake the agent-memory layer for the whole story. MemGPT's OS-paging frame and Letta's dual-agent split are vivid, easy to explain, and easy to ship — and the vocabulary they minted now travels back down to KV-cache work and up into substrate work. Legibility is not the same as completeness.

At Layer 5, the move shows up *below* the agent — at the substrate that an agent's memory tiers might run on top of. That's where the receipts live.

## Layer 5 — Substrate: Helix as one Agentome-stack instance (§7, ~900w)

Layer 5 is below the agent. If MemGPT and Letta are doing memory at the agent boundary — the LLM editing its own context through tool calls, or a sleep-time twin curating that context off the hot path — then Layer 5 is the substrate those memory tiers run *on top of*. An Agentome-shaped stack would assume something like this exists. Helix is one concrete instance at this layer; the receipts below are Helix receipts.

**Genome shape and chromatin tiers.** From the field report ([Agentome Part II](AGENTOME_PART_II_DRAFT.md)): *"Today the genome on my development machine is 17,623 genes, 670 MB, distributed across three chromatin tiers."* Those tiers are:

| Tier | Count | Role |
|---|---|---|
| OPEN (hot) | 12,401 | Default retrieval target |
| EUCHROMATIN (warm) | 1,895 | Included with hot queries |
| HETEROCHROMATIN (cold) | 3,327 | Opt-in, queryable via ΣĒMA cosine fallthrough |

The chromatin metaphor isn't decorative. Each tier has different read/write economics — OPEN is what the default retrieval pipeline scans on every query; EUCHROMATIN rides along when the hot tier matches; HETEROCHROMATIN is silent unless something explicitly reaches for it. That asymmetry is half **(a)** of the move from §2: state that persists past the request, with the persistence shaped so that *most of it is cold most of the time*. 670 MB on disk, but only ~12K genes are paying for default-query attention.

**Selection in code.** Half **(b)** — the policy that decides what becomes hot — runs in two places. The first is **ΣĒMA cosine fallthrough**, the OPEN→HETEROCHROMATIN bridge: when the hot tier doesn't match, the cold tier is queried by 20-dimensional semantic cosine. The second is **density-gated admission**, which decides which tier a gene lives in to begin with. The thresholds in [`helix_context/genome.py`](../../helix_context/genome.py) are: `_DENSITY_HETEROCHROMATIN_THRESHOLD = 0.50` (below this, a gene drops to cold), `_DENSITY_EUCHROMATIN_THRESHOLD = 1.00` (between 0.50 and 1.00 is warm, above is hot), and `_DENSITY_ACCESS_OVERRIDE = 5` (any gene with `access_count >= 5` stays OPEN regardless of density score). Crucially, this promotion logic is **asynchronous** — it runs between requests, not on the hot path. The synchronous query never blocks on tier reorganization. That asynchrony is the architectural choice §6 was setting up; I'll come back to it.

**Self-organization, with the caveat.** From the same field report: *"the biological architecture self-organized into thirteen retrieval lanes. All of them are wired. An A/B experiment is running right now that may cut the count back to eleven. Cymatics works. E8 does not."* Thirteen lanes today, possibly eleven after the in-flight A/B; the count isn't final and shouldn't be reported as if it were. Calling the cymatics-works/E8-doesn't beat out is the point — this is a system that's being *measured* against alternatives, not just shipped, and that distinction matters more than the lane count itself.

**The honest tax.** On the 2026-04-22 needle benchmark ([2026-04-22 research review](../research/RESEARCH_REVIEW_2026-04-22.md)), pure BM25 hit **8/8 content_full at 151 ms** while `helix_rag` (the BM25-augmented Helix mode) hit **4/8 at 1793 ms**. That's the headline. The pure-Helix `helix_only` mode is a separate config and currently has its own **4555-char assembly ceiling** issue. Three stacked failures explain the regression. *Population dilution at 17K genes:* test and doc files mention `port` and `helix` hundreds of times in fixtures and assertions, while `helix.toml` mentions them once each — the tag-exact, FTS5, and source-authority lanes accumulate on test files until config files can't compete, while BM25's IDF trivially finds the needle. *PKI tier structurally broken on this genome:* `packet_notes` reports `"source_index unavailable; using gene-local metadata only"` on 6 of 8 needles, the 0.30 coordinate-confidence floor sends everything to `refresh_targets` instead of `verified`, and PKI does exact-equality matching so query term `ports` (plural) never matches `kv_key=port` (singular). *The 4555-char ceiling:* the ribosome splice budget hits before useful content escapes the pipeline — a content-delivery bug, not a ranking failure. This is what it looks like to be on the map but not yet the best instance on it. It belongs here, in the same paragraph as the genome-shape numbers, because the "evidence > hero" framing only earns its keep if the negative receipt is as visible as the positive one.

**Cross-process and cross-agent reach.** What makes Helix specifically a Layer-5 instance, rather than a Layer-4 one, is that the genome surface lives below any individual agent. The MCP server attaches `Participant` records keyed on session handles — `laude`, `raude`, `taude`, `gemini`, `batman` — and [`helix_context/sharding.py`](../../helix_context/sharding.py) carries this through into per-handle SQLite shards (`laude.genome.db`, etc.). Sparse persistent state survives process restarts, and the same genome is reachable by multiple agents who attach with their own handle. That's what "substrate" means in this taxonomy: state that is not *the* agent's memory, but a layer agents read from and write to.

**Finding 2 — static-vs-evolving substrate splits along layer.** RAPTOR and GraphRAG (Layer 3) freeze their persistent artifact post-construction; the tree is static, the graph is static. MemGPT and Letta (Layer 4) evolve, with the LLM editing memory at runtime. Helix (Layer 5) sits *across both*: retrieval-layer machinery (lanes, ΣĒMA) under memory-layer dynamism (chromatin promotion). The chromatin-promotion logic is the bridge — it lets the *retrieval surface itself* be evolving substrate. Whether this position is uniquely Helix's is not the claim; the claim is that the position itself is interesting and underexplored.

**Finding 3 — Letta sleep-time agent ↔ Helix chromatin promotion.** The closest direct convergence with Helix is the architectural choice §6 traced through Letta: separate *express* (sync, hot path) from *consolidate* (async, between-turns). Letta runs a second LLM as the consolidator; Helix runs density-gated admission as the consolidator. Different machinery, different layers, same architectural split.

What's left is the part where I'm honest about what's converged, what hasn't, and where this goes next.

## What converges, what doesn't, what's next (§8, ~500w)

<!-- TODO Task 9 -->

---

*Figure: see [`figures/2026-05-01-layer-stack.md`](figures/2026-05-01-layer-stack.md)*

# OSS Semantic Retrieval vs. Helix Context

*Research report — June 16, 2026. Compares Helix Context (v0.5.0) against the open-source retrieval landscape.*

## TL;DR

Helix is not really competing in the same category as the tools people usually call "semantic retrieval." Most OSS retrieval is a **vector database or search engine** whose job ends at "return the top-k chunks." Helix is a **context-compression proxy** whose job is "inject the right *compressed* context into an LLM turn, on CPU, without re-shipping what the agent already has."

The single most distinctive Helix design choice is **no neural inference at query time by default**. Its default retrieval path (FTS5 lexical + tags + synonyms + co-activation + cymatics spectrum scoring) is pure-CPU arithmetic — the same class as BM25/Tantivy/SQLite-FTS5 — while almost every "semantic" OSS system either runs a transformer to embed the query (txtai, ColBERT, SPLADE, Weaviate's server-side vectorizer) or expects you to have done so client-side (Qdrant, Milvus). Helix's optional BGE-M3 dense and SPLADE paths put it on equal footing with those systems *when you turn them on*, but the default posture is deliberately the opposite: lexical-first, model-free, latency- and VRAM-cheap.

Where Helix is genuinely differentiated: **query-time CPU-only retrieval, splice-based context compression against a token budget, a freshness gate, session working-set deduplication, and the know/miss agent contract.** Where the OSS field is far ahead: **scale, ANN recall quality, ecosystem/integrations, and code-structure-aware retrieval.** The two are more complementary than competitive — Helix could sit *in front of* a vector DB rather than replace it.

---

## 1. How the major OSS semantic retrieval solutions work

It helps to fix four axes first, because every system is a point in this space.

**Lexical vs. dense vs. learned-sparse.** Lexical (BM25) scores by term frequency and rarity over an inverted index — pure arithmetic, no model anywhere ([BM25 overview](https://arxiv.org/pdf/2408.06643), [SQLite FTS5/Turso](https://turso.tech/blog/beyond-fts5)). Dense encodes text into one fixed-length vector via a neural bi-encoder; similar meaning lands nearby and matching is approximate-nearest-neighbor ([Weaviate concepts](https://docs.weaviate.io/weaviate/concepts/data)). Learned-sparse (SPLADE) uses a BERT model to emit a *sparse* weighted vector over the vocabulary — neural, but it still slots into an inverted index ([Learned sparse retrieval](https://en.wikipedia.org/wiki/Learned_sparse_retrieval), [Naver Labs SPLADE](https://europe.naverlabs.com/blog/splade-a-sparse-bi-encoder-bert-based-model-achieves-effective-and-efficient-first-stage-ranking/)).

**Bi-encoder vs. cross-encoder vs. late interaction.** A bi-encoder embeds query and document independently (one vector each), so documents can be pre-indexed and query time is cheap ([late interaction overview](https://weaviate.io/blog/late-interaction-overview)). A cross-encoder feeds query+document together for one relevance score — far more accurate but impossible to pre-index, so it is only ever used to *rerank* a short candidate list. Late interaction (ColBERT) is the middle ground: independent encoding but at *token* granularity, scored with a MaxSim operator ([ColBERTv2 paper](https://arxiv.org/pdf/2112.01488)).

**ANN index.** HNSW (a navigable multi-layer graph) is the dominant dense index; IVF (cluster-and-probe), FLAT (brute force), DiskANN, and GPU CAGRA round out the field ([Weaviate vector index](https://docs.weaviate.io/weaviate/concepts/vector-index), [Milvus index reference](https://milvus.io/ai-quick-reference)).

**Fusion.** Hybrid systems combine a lexical and a vector ranking. Reciprocal Rank Fusion (RRF) merges by *rank position* (`Σ 1/(k+rank)`), needing no score normalization; weighted/additive fusion normalizes and linearly combines the raw scores ([hybrid FTS5+vector+RRF](https://ceaksan.com/en/hybrid-search-fts5-vector-rrf)). **This maps directly to Helix's `[retrieval] fusion_mode = "additive" | "rrf"` toggle.**

With that framing, the systems sort into three groups by the question that matters most for Helix — *does a neural model run at query time?*

### Frameworks (neural-at-query-time is a config choice)

**LlamaIndex** is an orchestration layer, not a store. It models everything as Nodes (chunks + metadata + relationships) and node parsers, and delegates indexing/embedding to whatever vector store and embed model you wire in ([LlamaIndex RAG](https://oneuptime.com/blog/post/2026-02-02-llamaindex-rag-applications/view)). It runs neural query embedding only if you configure a dense index, and supports post-retrieval reranking/compression via Node Postprocessors ([node postprocessors](https://developers.llamaindex.ai/typescript/framework/modules/rag/node_postprocessors/)).

**Haystack** is the same idea with an explicit, auditable pipeline graph — converters, embedders, retrievers, rankers, generators are all wired nodes, "nothing hidden" ([Haystack vs LlamaIndex](https://www.zenml.io/blog/haystack-vs-llamaindex)). Dense retriever ⇒ query embedded at query time; BM25 retriever ⇒ no inference.

### Embedding databases & vector DBs

**txtai** is an "all-in-one embeddings database" built on Hugging Face / Sentence Transformers, unioning vector indexes, a graph, and a relational DB. The query is always embedded by a transformer at search time, so GPU is recommended ([txtai intro](https://medium.com/neuml/introducing-txtai-the-all-in-one-embeddings-database-c721f4ff91ad), [txtai embeddings](https://neuml.github.io/txtai/embeddings/)).

**Weaviate** (Go, HNSW core) is unusual in that it can run the embedding model *server-side* via vectorizer modules — throw it raw text and it vectorizes the query inside the DB before the nearest-neighbor search — or you can bring pre-computed vectors and keep the DB CPU-only ([Weaviate GitHub](https://github.com/weaviate/weaviate), [Weaviate vector index](https://docs.weaviate.io/weaviate/concepts/vector-index)). It also does hybrid BM25+vector and optional reranker modules.

**Qdrant** (Rust) and **Milvus** are, *as core engines*, vector-in/vector-out: they expect a pre-computed query vector and do pure-CPU ANN lookups. The neural embedding is a **separate client step** — FastEmbed/ONNX for Qdrant, the `pymilvus[model]` subpackage's `encode_queries()` for Milvus ([Qdrant inference](https://qdrant.tech/documentation/inference/), [Qdrant FastEmbed](https://qdrant.tech/documentation/fastembed/), [Milvus embeddings](https://milvus.io/docs/embeddings.md)). Milvus has the widest index menu (FLAT/IVF/HNSW/DiskANN + GPU CAGRA). Caveat: Qdrant can compute BM25 server-side, and both offer optional cloud inference.

### Search engines & advanced rankers

**Vespa** is the most full-featured: ANN retrieval feeding *multi-phase ML ranking*, with embedding and even cross-encoder/tensor models deployable for run-time inference inside the engine ([Vespa architecture](https://vespa.ai/architecture/), [Vespa billion-scale](https://blog.vespa.ai/vespa-hybrid-billion-scale-vector-search/)). It is the closest OSS analog to Helix's "retrieval + downstream processing in one box," though it processes via ML ranking rather than compression.

**ColBERT / RAGatouille** is late interaction over BERT — mandatory query encoding into per-token vectors, scored by MaxSim, GPU-preferred. ColBERTv2's residual quantization cuts each token vector to ~20–36 bytes (6–10× smaller) to make the larger footprint tractable ([ColBERTv2](https://arxiv.org/pdf/2112.01488), [PLAID](https://arxiv.org/pdf/2205.09707)).

**BM25 / Tantivy / Elasticsearch / SQLite FTS5** are the pure-lexical group: tokenize into an inverted index, score arithmetically, **zero neural inference, pure CPU, lowest latency and footprint** ([Turso FTS5](https://turso.tech/blog/beyond-fts5), [BM25](https://arxiv.org/pdf/2408.06643)). **This is exactly the class Helix's default path belongs to.**

**SPLADE** uses BERT's masked-language-model head to expand and weight terms into a sparse vector; the query must pass through the encoder (GPU-preferred) but matching is inverted-index, not ANN ([learned sparse retrieval](https://en.wikipedia.org/wiki/Learned_sparse_retrieval)). This is precisely Helix's optional `[ingestion] splade_enabled` path.

### The query-time-inference verdict

| Group | Systems | Neural inference at query time? |
|---|---|---|
| Pure lexical / CPU | BM25, Tantivy, Elasticsearch (BM25 mode), **SQLite FTS5**, Qdrant & Milvus *cores* | **No** (Qdrant/Milvus embed in a separate client step) |
| Intrinsically neural | txtai, ColBERT/RAGatouille, SPLADE | **Yes** (GPU preferred) |
| Configurable / server-side optional | Weaviate, Vespa, LlamaIndex, Haystack | **Optional** — depends on wiring |

**Helix's default path sits firmly in row 1** alongside SQLite FTS5. Its optional BGE-M3 dense and SPLADE expansions move it into row 2 *only when enabled*.

---

## 2. How Helix differs architecturally

The OSS tools above answer "given a query, return the most relevant chunks." Helix answers a different question: "given an LLM turn, what *compressed* context should be injected, and what has this session already seen?" That reframing produces several structural differences.

**It's a proxy, not a database.** Helix is a transparent OpenAI-compatible proxy (`POST /v1/chat/completions`) that intercepts requests and injects context from a persistent SQLite store. The vector DBs are libraries/servers you query explicitly; Helix interposes on the model call itself, so integration is "point your client at the proxy" rather than "rewrite your retrieval code." Vespa is the only OSS system here that similarly bundles retrieval+downstream processing, but it does ML ranking, not injection-into-a-prompt.

**Default retrieval is model-free and CPU-only.** The Stage-2 retrieve path — FTS5 lexical + tag lookup + synonym expansion + co-activation graph + cymatics 256-bin spectrum scoring — runs entirely without transformer inference. Co-activation (which documents tend to surface together) and cymatics (a spectral similarity score) are non-neural ranking signals layered on top of lexical recall. This is a fundamentally different bet from the dense-first OSS norm: Helix trades some semantic recall for zero VRAM contention and low latency, which matters on Max's rig where Ollama already holds the GPU.

**Optional neural recall is bolted on, not assumed.** BGE-M3 dense (`dense_embedding_enabled`, default off) and SPLADE sparse (`splade_enabled`, default off) add bi-encoder and learned-sparse query encoding — the same categories as txtai and SPLADE proper — but they are opt-in, and gated behind kill-switches (`HELIX_BFM_SPLADE=0`, `HELIX_BFM_DENSE_BACKFILL=0`) precisely because running multiple CUDA contexts causes the documented WDDM-spill livelock. The OSS dense systems make this path the default and the point; Helix treats it as an enhancement.

**Compression, not just retrieval (Stages 3–5).** After retrieval, Helix optionally CPU-reranks, then *splices* — a CPU model compresses each candidate, keeping high-value fragments — then assembles against a hard token budget (`expression_tokens`) with per-document legibility headers. This is closest in spirit to LlamaIndex node postprocessors or a cross-encoder rerank stage, but the goal is **token-budget compression for prompt injection**, which no vector DB does natively. Vespa reranks; it doesn't compress-to-budget.

**Freshness gate (Stage 7).** During assembly Helix demotes stale, cold, or superseded documents. Vector DBs have no native notion of document staleness in ranking — recency is something you bolt on with metadata filters. Helix bakes it into the pipeline.

**Session working-set dedup.** With `session_delivery_enabled`, Helix tracks what each session has already received and *elides repeats*, claiming ~40% token savings on multi-turn conversations. This is a stateful, conversation-aware behavior with no analog in stateless vector search — the OSS tools return the same chunks every time you ask.

**The know/miss agent contract.** Every `/context/packet` response carries `know { found, confidence, gene_id_match }` or `miss { reason }`, so a downstream agent can calibrate trust instead of guessing. Vector DBs return scores, but a raw cosine distance is not a calibrated found/not-found signal; Helix's abstention thresholds (`[abstain]`, `[know]`) turn it into an explicit contract. This is arguably Helix's most novel feature relative to the entire OSS field.

---

## 3. How Helix's strengths rank against the OSS field

Reading "rank" as *where Helix wins, ties, and loses* against these tools:

**Where Helix leads the field**

- **CPU-only / VRAM-free query path.** Among "semantic" systems, only the pure-lexical group (BM25/FTS5) matches this, and they don't offer synonym expansion + co-activation + spectral scoring on top. For a local-LLM rig with a contended 12 GB GPU, this is a real, defensible edge — query latency doesn't fight Ollama for VRAM.
- **Token-budget context compression.** No vector DB compresses-to-budget. The nearest competitors are reranking frameworks (LlamaIndex postprocessors, Vespa ML ranking), and none make "fit the prompt under N tokens with legibility headers" a first-class output. This is Helix's clearest functional differentiator.
- **Session-aware dedup.** Stateful elision of already-delivered context is unique here; stateless retrieval can't do it without an external session layer you'd have to build.
- **Know/miss contract + abstention.** Calibrated found/miss with refresh targets is a genuinely agent-oriented design that the data-retrieval-focused OSS tools don't provide out of the box.

**Where Helix is roughly at parity (when its optional paths are on)**

- **Hybrid fusion.** Helix's RRF/additive toggle is the same fusion machinery Weaviate, Vespa, and Elasticsearch hybrid offer. Parity, not advantage.
- **Learned sparse & dense recall.** BGE-M3 + SPLADE put Helix in the same recall *category* as txtai/SPLADE/Milvus-with-model — but those systems have spent far more engineering on ANN index quality and scale.

**Where the OSS field clearly leads Helix**

- **Scale and ANN recall.** Milvus/Qdrant/Vespa/Weaviate are built for billions of vectors with mature HNSW/IVF/DiskANN/GPU indexes ([Vespa billion-scale](https://blog.vespa.ai/vespa-hybrid-billion-scale-vector-search/)). Helix's SQLite + FTS5 core is a single-node store; it is not trying to be a distributed vector engine, and shouldn't be benchmarked as one.
- **Out-of-the-box semantic recall.** A dense-by-default system will beat Helix's lexical-by-default path on paraphrase/synonym-heavy queries unless the synonym map is well-tuned — which the CLAUDE.md "synonym map is critical" gotcha explicitly flags as the failure mode.
- **Ecosystem & integrations.** LlamaIndex/Haystack/Weaviate/Qdrant have enormous connector, embedding-model, and tooling ecosystems. Helix integrates via the proxy and an MCP surface, which is elegant but narrow.
- **Reranking maturity.** Cross-encoder rerankers and ColBERT late interaction are battle-tested quality boosters; Helix's rerank is off by default and its splice stage optimizes for compression, not pure ranking quality.

**Net:** Helix wins on *deployment posture and prompt-economy* (CPU-only, compression, dedup, agent contract) and loses on *raw retrieval scale and semantic recall*. The honest framing is complementarity: Helix's compression/freshness/dedup/contract layers could sit **in front of** a Qdrant or Weaviate that supplies high-recall candidates, rather than competing with them on ANN search.

---

## 4. Code-specific retrieval vs. prose retrieval

This is where the "just embed everything" approach that works for prose breaks down, and it's directly relevant if Helix ingests code.

**Why prose chunking fails on code.** Fixed-size / sentence / recursive splitters cut through methods, split classes across chunks, orphan a `catch` from its `try`, and strip enclosing imports/namespaces. Prose tolerates this because sentences are self-contained and paraphrase-tolerant; code does not, because it depends on scoping and cross-references — a method references a field defined elsewhere, a file imports a type ([Trendyol code-aware chunking](https://medium.com/trendyol-tech/we-stopped-splitting-code-like-text-code-aware-chunking-unlocked-better-rag-c9f2426e6ad9)). The academic cAST paper states it plainly: line-based heuristics "break semantic structures, splitting functions or merging unrelated code, which can degrade generation quality" ([cAST](https://arxiv.org/abs/2506.15655)).

**AST-aware chunking.** The fix is to split along AST boundaries with tree-sitter — functions, classes, methods become the chunk units. The canonical algorithm is recursive split-then-merge: fit a large AST node into one chunk if it's under the size limit, otherwise recurse and merge siblings ([cAST](https://arxiv.org/abs/2506.15655), [astchunk](https://github.com/yilinjz/astchunk)). The measured payoff from the cAST paper: **+4.3 Recall@5 on RepoEval** retrieval and **+2.67 Pass@1 on SWE-bench** generation versus line-based chunking — concrete evidence that structure-aware chunking matters.

**Exact symbol matching is load-bearing for code.** In prose, a near-synonym is usually fine; in code, a function name or error string must match *exactly* — a near-match is wrong. So code search leans on tools prose retrieval never needs: **Zoekt's trigram index** (sub-50ms over ~2 GB by indexing 3-char sequences and verifying regex matches), **universal-ctags** for symbols, and precise navigation via **SCIP/LSIF** for compiler-accurate go-to-definition across repos ([Zoekt](https://github.com/sourcegraph/zoekt), [Zoekt design](https://github.com/sourcegraph/zoekt/blob/main/doc/design.md), [SCIP](https://sourcegraph.com/blog/announcing-scip)).

**Structural graph signals.** Aider's "repo map" is the clearest example of code-specific ranking: it tree-sitter-parses every file, extracts `def`/`ref` tags, builds a directed multigraph (file A references a symbol defined in file B), and runs **personalized PageRank** to rank what matters — weighting identifiers in the user's message (10×), real snake_case names (10×), and references from files already in chat (50×) — then trims to a token budget (`--map-tokens`, default 1k) ([Aider repomap](https://aider.chat/2023/10/22/repomap.html), [DeepWiki](https://deepwiki.com/Aider-AI/aider/4.1-repository-mapping-system)). This is conceptually adjacent to Helix's co-activation graph and token-budget assembly, but specialized to code symbol graphs. **Notably, this is the closest existing analog to what a code-aware Helix would do** — graph-rank symbols, fit a budget — which suggests Helix's co-activation + budget machinery is well-positioned to adopt code-structure signals.

**Code-trained embeddings beat general text embeddings.** When you do use dense retrieval on code, code-specific models win: GraphCodeBERT adds data-flow structure and prefers structure-level over token-level attention, hitting SOTA on code search/clone/translation/refinement ([GraphCodeBERT](https://arxiv.org/abs/2009.08366)); voyage-code-3 beats OpenAI-v3-large by ~13.8% on code retrieval at a third of the storage ([voyage-code-3](https://blog.voyageai.com/2024/12/04/voyage-code-3/)); Jina code embeddings match voyage at 1.5B params across 25 benchmarks ([Jina code](https://jina.ai/news/jina-code-embeddings-sota-code-retrieval-at-0-5b-and-1-5b/)).

**How production assistants combine these.** Continue.dev pulls 25 candidates via LanceDB embeddings + SQLite metadata + tree-sitter + keyword search, then reranks to 5 ([Continue codebase](https://docs.continue.dev/customize/context/codebase), [DeepWiki](https://deepwiki.com/continuedev/continue/3.4-codebase-indexing)). Sourcegraph Cody fuses keyword extraction + embeddings + code-graph + rerank, and is notably *moving Enterprise away from embeddings* toward search/code-graph retrieval ([Cody](https://sourcegraph.com/blog/how-cody-understands-your-codebase)).

**The contrast in one line.** Prose retrieval can lean almost entirely on dense embeddings over recursive chunks because prose is self-contained and paraphrase-tolerant. Code retrieval needs the hybrid stack — AST-aware chunking, exact symbol/trigram matching, structural graph signals, and code-trained embeddings — because exact-identifier matching and cross-file structure are load-bearing in a way they never are for prose ([Elastic hybrid](https://www.elastic.co/what-is/hybrid-search), [hybrid BM25](https://www.emergentmind.com/topics/hybrid-bm25-retrieval)).

**Implication for Helix.** Helix's lexical-first, FTS5-based default is actually a *better* starting point for code than a dense-first system, because exact identifier matching is exactly what lexical retrieval is good at — and what dense embeddings notoriously miss. The gaps to close for first-class code support are AST-aware chunking at ingest (its `codons.py` chunker would need tree-sitter), symbol-graph signals (a natural extension of the co-activation graph), and optionally a code-trained embedding model in the BGE-M3 slot.

---

## Sources

**OSS retrieval architecture**
- BM25 / lexical: https://arxiv.org/pdf/2408.06643 · https://turso.tech/blog/beyond-fts5
- Dense / vector concepts: https://docs.weaviate.io/weaviate/concepts/data · https://docs.weaviate.io/weaviate/concepts/vector-index
- Learned sparse / SPLADE: https://en.wikipedia.org/wiki/Learned_sparse_retrieval · https://europe.naverlabs.com/blog/splade-a-sparse-bi-encoder-bert-based-model-achieves-effective-and-efficient-first-stage-ranking/
- Bi/cross/late interaction: https://weaviate.io/blog/late-interaction-overview · https://arxiv.org/pdf/2112.01488 · https://arxiv.org/pdf/2205.09707
- Hybrid fusion / RRF: https://ceaksan.com/en/hybrid-search-fts5-vector-rrf
- LlamaIndex: https://oneuptime.com/blog/post/2026-02-02-llamaindex-rag-applications/view · https://developers.llamaindex.ai/typescript/framework/modules/rag/node_postprocessors/
- Haystack: https://www.zenml.io/blog/haystack-vs-llamaindex
- txtai: https://neuml.github.io/txtai/embeddings/ · https://medium.com/neuml/introducing-txtai-the-all-in-one-embeddings-database-c721f4ff91ad
- Weaviate: https://github.com/weaviate/weaviate
- Qdrant: https://qdrant.tech/documentation/inference/ · https://qdrant.tech/documentation/fastembed/
- Milvus: https://milvus.io/docs/embeddings.md · https://milvus.io/ai-quick-reference
- Vespa: https://vespa.ai/architecture/ · https://blog.vespa.ai/vespa-hybrid-billion-scale-vector-search/

**Code-specific retrieval**
- Code-aware chunking: https://medium.com/trendyol-tech/we-stopped-splitting-code-like-text-code-aware-chunking-unlocked-better-rag-c9f2426e6ad9 · https://arxiv.org/abs/2506.15655 (cAST) · https://github.com/yilinjz/astchunk
- Zoekt / Sourcegraph / SCIP: https://github.com/sourcegraph/zoekt · https://github.com/sourcegraph/zoekt/blob/main/doc/design.md · https://sourcegraph.com/blog/announcing-scip · https://scip-code.org/
- Aider repo map: https://aider.chat/2023/10/22/repomap.html · https://deepwiki.com/Aider-AI/aider/4.1-repository-mapping-system
- Continue / Cody: https://docs.continue.dev/customize/context/codebase · https://deepwiki.com/continuedev/continue/3.4-codebase-indexing · https://sourcegraph.com/blog/how-cody-understands-your-codebase
- Code embeddings: https://arxiv.org/abs/2009.08366 (GraphCodeBERT) · https://blog.voyageai.com/2024/12/04/voyage-code-3/ · https://jina.ai/news/jina-code-embeddings-sota-code-retrieval-at-0-5b-and-1-5b/
- Hybrid for code: https://www.elastic.co/what-is/hybrid-search · https://www.emergentmind.com/topics/hybrid-bm25-retrieval

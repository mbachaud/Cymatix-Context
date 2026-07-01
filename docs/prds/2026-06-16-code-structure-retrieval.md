# PRD: First-Class CODE Retrieval in Helix Context

**Date:** 2026-06-16
**Status:** DRAFT — spec/planning only
**Owner:** (TBD)
**Related:** `docs/research/oss-semantic-retrieval-vs-helix.md` §4

## 1. Problem & Goal: Parity → Win

### The result we are converting

The OSS-retrieval review (`docs/research/oss-semantic-retrieval-vs-helix.md` §4)
concludes that Helix's **lexical-first, FTS5-based default is the *right*
foundation for code** — not a liability. Exact identifier matching (a function
name, an error string, a config key) is load-bearing for code in a way it never
is for prose, and dense embeddings notoriously miss exact tokens. Helix's BM25 +
tag + filename-anchor base is exactly the machinery that nails exact matches.

The problem is that *base alone* only reaches **parity**: our RepoBench-R run
showed Helix ≈ BM25. We are leaving the win on the table because the base treats
code as undifferentiated text — it chunks across function boundaries, has no
notion of which symbol a chunk *defines* vs *references*, and ranks purely on
term overlap rather than structural centrality.

### The win is well-precedented

Two external results bound the expected payoff and tell us which signals matter:

- **cAST** (arXiv 2506.15655) — AST-aware split-then-merge chunking instead of
  line/recursive chunking yields **+4.3 Recall@5 on RepoEval** retrieval and
  **+2.67 Pass@1 on SWE-bench** generation. This is the single
  highest-confidence lever, and it is a *chunking-time* change — no query-time
  inference, no model.
- **Aider repo-map** — tree-sitter `def`/`ref` extraction → directed symbol
  multigraph → **personalized PageRank** (weighting identifiers in the user's
  message, real snake_case names, and refs from in-context files) → trim to a
  `--map-tokens` budget. The review calls this *"the closest existing analog to
  what a code-aware Helix would do"* — because Helix already has a
  co-activation graph and token-budget assembly. We are not inventing the
  pattern; we are pointing our existing graph + budget machinery at a code
  symbol graph.

### Goal

Convert RepoBench-R parity into a measurable **win over BM25** on code
retrieval, by adding code-*structure* signals on top of the lexical base —
**without** abandoning lexical-first or re-chasing dense (see §4). Concretely:
beat the BM25 baseline on RepoBench-R `acc@k` and CodeRAG-Bench `NDCG@10`, and
improve ContextBench line/block recall, with each phase gated on the prior
clearing its bar.

### What's already in the tree (changes the plan)

A scan of the actual code (full citations in the Appendix) shows Helix is
**further along than a greenfield estimate would assume**:

- **AST chunking already exists.** `helix_context/encoding/tree_chunker.py`
  implements split-then-merge over tree-sitter for 11 languages and is *already
  wired* as the preferred path in `CodonChunker._chunk_code`
  (`helix_context/encoding/fragments.py:133-159`). WS1 is largely
  *productionizing and measuring* an existing prototype, not building from zero.
- **A parent/chunk hierarchy already exists.** `StructuralRelation.CHUNK_OF =
  100` (`helix_context/schemas.py:28-33`) plus
  `ContextManager._upsert_parent_doc` (`context_manager.py:949-998`) already
  build child→parent edges in `gene_relations`. WS2's symbol graph extends an
  existing structural-edge channel rather than adding a new table.
- **A typed, evidence-weighted graph + budget assembler already exist.**
  `seeded_edges.py` (Hebbian-weighted edges), `storage/co_activation.py`
  (expansion), and `_assemble` + `expression_tokens`
  (`context_manager.py:2330+`) are the exact substrate Aider's pattern needs.

The flip side: **tree-sitter is NOT in core dependencies.** It lives only in the
`ast` and `all` extras (`pyproject.toml:75-80, 104-108`); core `dependencies`
(`pyproject.toml`) is just fastapi/uvicorn/httpx/pydantic/filelock. So on a
default `pip install helix-context`, `tree_chunker.is_available()` returns False
and the code *silently falls back to the regex chunker*. The AST path exists but
is effectively dark for most installs — a packaging + defaults decision, not a
code-writing task, gates WS1's real-world impact.


## 2. Workstreams

### WS1 — AST-Aware Chunking (tree-sitter in codons.py)  *[highest ROI, lowest risk]*

**What to build.** Make AST-aware chunking the *real, default* path for code,
matching cAST's split-then-merge semantics, and ensure it actually runs on a
normal install. The algorithm is already present; the work is (a) packaging so
it's available, (b) defaults/observability so we know which path ran, (c)
tightening the merge to cAST's recursive-merge semantics, and (d) carrying chunk
metadata (language, symbol name, byte span) needed by WS2/WS3.

**Exact files / functions to change:**

- `helix_context/encoding/tree_chunker.py` — *exists* (318 lines). `chunk_code_ast`
  (`:205-308`) does top-level boundary collection + greedy merge + hard-cut of
  oversized nodes. `_BOUNDARY_NODES` (`:135-200`) covers 11 languages;
  `_get_parser` (`:79-126`) uses the per-language `tree-sitter-<lang>` packages
  (not the deprecated bundle). Changes:
  - Tighten the merge loop (`:284-306`) to cAST's *recursive* split-then-merge:
    today it greedily concatenates top-level blocks and hard-cuts any single
    node ≥ `max_chars` at a raw character boundary (`:295-301`). cAST recurses
    *into* an oversized node (e.g. a huge class → its methods) before resorting
    to a character cut. This is the change that earns the +4.3 Recall@5.
  - Emit per-chunk metadata: the enclosing symbol name + node type + start/end
    byte span. `node_text` (`:250-252`) already has the node; capture
    `child.type` and (for def nodes) the `name`/`identifier` child. Return this
    alongside `(content, is_fragment)` — promote the return type to a small
    dataclass or `(content, is_fragment, meta)` so WS2 can read def-symbol names
    without re-parsing.
- `helix_context/encoding/fragments.py` — `CodonChunker._chunk_code` (`:133-216`)
  already calls `tree_chunker.is_available()` then `chunk_code_ast`
  (`:137-159`), falling back to the regex path (`:161-216`) on `ImportError`/
  `ValueError`. Changes:
  - Thread the new chunk metadata into `RawStrand.metadata` (`:147-153`) so
    ingest persists symbol name / language / span.
  - Add a debug counter / log line recording *which* path ran (AST vs regex) so
    we can verify in benches that AST actually fired — today the fallback is
    silent (`:156-159`).
- `pyproject.toml` — **packaging decision (gates everything).** tree-sitter is
  only in the `ast`/`all` extras (`:75-80, 104-108`), not core `dependencies`.
  Options: (1) promote the tree-sitter stack to core deps, or (2) keep it an
  extra but make `helix ingest` *warn loudly* when code is ingested without it
  ("falling back to regex chunking; `pip install helix-context[ast]` for
  structure-aware chunks"). Recommend (1) for first-class code support, or (2)
  as a minimum so the dark-fallback stops being silent.
- `helix_context/config.py` — add a `[ingestion] ast_chunking` knob (default
  `true` when available) so benches can A/B AST vs regex cleanly.

**Mapping to existing machinery.** This *is* the existing machinery — `tree_chunker`
+ `_chunk_code` already form the cAST-shaped path. We are hardening a prototype:
recursive merge, metadata pass-through, packaging, and observability. No new
subsystem.

**Effort.** S–M. The skeleton exists; recursive-merge + metadata + packaging +
an A/B knob is days, not weeks. The largest single line item is the packaging /
defaults decision and re-running the chunking benches.

**Risk.** Low. (1) Languages outside `_BOUNDARY_NODES` already fall back safely
via `ValueError` (`:232-237`). (2) Oversized-node hard-cut already prevents
unbounded chunks. (3) Main risk is *regression on prose* — guarded because
`_chunk_code` only runs for `content_type == "code"` (`fragments.py:78-79`), so
text ingest is untouched. (4) Adding tree-sitter to core deps grows the wheel's
dependency surface; mitigate with option (2) if wheel size matters.


### WS2 — Symbol Def/Ref Graph (extending co-activation)

**What to build.** A symbol-level def/ref multigraph: for each code chunk, record
the symbols it *defines* and the symbols it *references*; then materialize edges
"chunk A references a symbol defined in chunk B." This is the data structure
Aider's PageRank runs over (WS3) and a retrieval-expansion signal in its own
right (pull in the *definition* of a symbol the top hit references).

**Exact files / functions to change:**

- `helix_context/schemas.py` — extend `StructuralRelation` (`:28-33`). Today it
  has only `CHUNK_OF = 100`. Add e.g. `DEFINES = 101`, `REFERENCES = 102` (or a
  single `SYMBOL_REF = 101` edge from referencing-chunk → defining-chunk). The
  enum is explicitly a discriminated union on `gene_relations.relation`
  (0–6 = NL, 100+ = structural), so this slots in cleanly with no new table.
- `helix_context/storage/ddl.py` — `_create_gene_relations` (`:205-215`) already
  stores `(gene_id_a, gene_id_b, relation, confidence, updated_at)`. Symbol
  edges reuse this table verbatim. Add a small `symbol_defs` index table
  `(symbol TEXT, gene_id TEXT, kind TEXT)` next to `_create_entity_graph`
  (`:222-240`) — same shape as `entity_graph`, indexed both ways — so we can
  resolve "who defines `foo`" at ingest and at query time.
- `helix_context/context_manager.py` — `_upsert_parent_doc` (`:949-998`) is the
  template: it already builds `CHUNK_OF` edges and calls
  `genome.store_relations_batch` (`:996`). Add a sibling pass after chunk
  upsert that: (1) reads the def/ref symbol lists WS1 attached to each chunk's
  metadata, (2) populates `symbol_defs`, (3) resolves each chunk's *references*
  to defining chunks and emits `SYMBOL_REF` edges via the same
  `store_relations_batch`. This runs in `ingest` right where chunks + parent
  are persisted (`:787-904`).
- `helix_context/encoding/tree_chunker.py` — extend the parse pass (WS1 already
  walks the AST at `:258-270`) to also collect *reference* sites, not just
  definition boundaries. Definitions come from the `name` child of boundary
  nodes; references come from `identifier`/`call`/`type` nodes in the body. This
  is the one genuinely new parsing logic; scope it to the WS1 tier-1 languages
  first (python/rust/js/ts).
- `helix_context/storage/co_activation.py` — `expand_coactivated` (`:111-180`)
  already walks typed edges and gates on relation code (`:144-150`). Add a
  branch that, for code queries, treats `SYMBOL_REF` as a high-value expansion
  edge (pull in the definition of a referenced symbol). `auto_link_by_entity`
  (`:28-62`) is the analog for the new `symbol_defs` table — a
  `auto_link_by_symbol` that links chunks sharing a defined/referenced symbol.
- `helix_context/retrieval/expand.py` — `expand_neighbors` (`:138-182`) exposes
  forward/backward/sideways 1-hop traversal over `harmonic_links`. Extend the
  forward/backward fetchers (`:50-94`) to optionally traverse `gene_relations`
  `SYMBOL_REF` edges so the `/context/expand` endpoint can answer "show me the
  definition this code calls."

**Mapping to existing machinery.** Reuses `gene_relations` (the CHUNK_OF channel),
mirrors `entity_graph` for `symbol_defs`, and extends the *existing*
co-activation expansion + `/context/expand` traversal. No new storage subsystem;
symbol edges are just new relation codes on the table that already carries
structural edges.

**Effort.** M. The storage + edge-emission plumbing is well-templated by
`_upsert_parent_doc` and `auto_link_by_entity`. The real work is the
reference-extraction parse pass (per-language node-type knowledge) and symbol
resolution (handling imports / scoping well enough to avoid garbage edges).

**Risk.** Medium. (1) **Symbol resolution is the hard part** — naive
name-matching links every `process()` to every other `process()`. Mitigate by
scoping resolution within file/module first and gating cross-file edges on
confidence, exactly as `auto_link_by_entity` already gates on `shared >= 2`
(`:50`) and confidence (`:55`). (2) Edge-count blowup on large repos — bound
fan-out per chunk (Aider caps refs) and lean on the existing `harmonic_links`
pruning floor (`seeded_edges.py:67`). (3) Languages without a ref-extractor fall
back to no symbol edges (degrade to WS1-only), which is safe.


### WS3 — Aider-Style Personalized-PageRank Ranking + Budget Trim

**What to build.** Rank code chunks by *structural centrality* over the WS2
symbol graph using personalized PageRank, then trim the ranked set to the
`expression_tokens` budget — Aider's exact recipe, pointed at Helix's graph and
assembler. "Personalization" = bias the random-walk restart toward chunks that
define/reference identifiers in the user's query (Aider's 10× for query
identifiers, 10× for real snake_case names) and chunks already delivered this
session (Aider's 50× for in-chat files — Helix already tracks this via session
delivery).

**Exact files / functions to change:**

- New module `helix_context/scoring/symbol_pagerank.py` (sits alongside the
  existing scorers `cymatics.py`, `tcm.py`, `blend.py`, `ray_trace.py`). Pure
  CPU, no model. Inputs: the `SYMBOL_REF`/`DEFINES` edges from `gene_relations`
  for the candidate set + their 1-hop neighborhood; a personalization vector
  built from query identifiers and the session working-set. Output: a
  `{gene_id: centrality}` score map. Use a small power-iteration (the graph is
  candidate-local, not whole-repo, so this stays cheap).
- `helix_context/retrieval/fusion.py` — the additive fuser is where tier scores
  combine (the `lex_anchor`/`filename_anchor` tiers feed it via
  `knowledge_store.py:2319-2329` and `:1898-1917`). Add a `symbol_pagerank` tier
  with its own weight, fused exactly like the existing anchor tiers. Under
  `fusion_mode = "rrf"` (`fusion_plr.py`) it joins as another rank list. This is
  the integration seam: PageRank is *one more tier*, not a replacement ranker —
  keeping lexical-first intact (§4).
- `helix_context/context_manager.py` — `_assemble` (`:2330+`) already enforces
  the token budget: it estimates tokens (`estimate_tokens`, imported `:29`),
  compares against `ribosome_tokens + expression_tokens` (`:2556-2557`), and
  trims. The WS3 change is *ordering the trim by PageRank centrality* for code
  turns, so the budget keeps structurally-central chunks (the def a query
  references) over peripheral ones. The `expression_tokens` budget *is* Aider's
  `--map-tokens`; no new budget machinery.
- `helix_context/identity/` session delivery — the 50×-for-in-context-files
  personalization weight maps onto Helix's *session working-set register*
  (CLAUDE.md: "session working-set register" / `session_delivery_enabled`).
  Already-delivered chunks become high-restart-probability nodes in the
  personalization vector. Wire `symbol_pagerank.py` to read the session manifest
  the delivery layer already maintains.
- `helix_context/retrieval/query_classifier.py` + `intent_router.py` — gate the
  PageRank tier to *code* queries (the classifier already picks decoder mode /
  assembly cap with no model call). A prose query should not pay for symbol-graph
  PageRank.

**Mapping to existing machinery.** This is the review's headline observation made
concrete: Helix's co-activation graph (now carrying symbol edges from WS2) + the
`_assemble` token-budget trim are "the closest existing analog" to Aider's
repo-map. PageRank becomes a fusion tier next to `lex_anchor`/`filename_anchor`;
budget trim reuses `expression_tokens`; personalization reuses session delivery.

**Effort.** M. Power-iteration PageRank over a candidate-local graph is a
contained algorithm. The integration (new fusion tier + budget-ordered trim +
session-aware personalization + classifier gating) touches several files but
each touch is small and follows an existing pattern (the anchor tiers are the
template).

**Risk.** Medium, and **gated on WS2**: PageRank is only as good as the symbol
graph beneath it — garbage edges → garbage centrality. (1) Hard-depends on WS2
quality, so ship WS2 and validate its edges before WS3. (2) Whole-repo PageRank
is expensive; mitigate by running it candidate-local (top-N + 1-hop), as scoped.
(3) Over-weighting centrality can bury an exact lexical hit; mitigate by fusing
PageRank as an *additive tier* under the existing lexical tiers, not as a
gate — preserve lexical-first (§4).


### WS4 (optional) — Code-Trained Embedding in the BGE-M3 Slot

**What to build.** When dense retrieval *is* used on code (it is off by default
and stays a secondary signal — §4), swap the general-text BGE-M3 encoder for a
code-trained one (voyage-code-3, GraphCodeBERT, or Jina code-embeddings). The
review's measured gap: voyage-code-3 beats OpenAI-v3-large by ~13.8% on code
retrieval; Jina code matches voyage at 1.5B params. This is the *lowest-priority*
workstream precisely because dense is not the lever for code — exact match and
structure are.

**Exact files / functions to change:**

- `helix_context/backends/bgem3_codec.py` — `BGEM3Codec` (`:52-67`) hardcodes
  `model_name = "BAAI/bge-m3"` and `dim = 1024` as constructor defaults, with the
  `_QUERY_PREFIX` (`:20`) and `_SANCTIONED_DIMS = {1024, 768, 512}` (`:24`) tuned
  to BGE-M3. The codec already wraps sentence-transformers / FlagEmbedding
  generically, so a code model that ships a sentence-transformers interface
  (Jina, GraphCodeBERT) drops in by changing the model name + prefix + sanctioned
  dims. A *factory* (`get_dense_codec(model_name)` returning the right codec) is
  cleaner than overloading `BGEM3Codec`; introduce a small `CodeEmbedCodec` or
  generalize `BGEM3Codec` to a `DenseCodec` with model-specific config.
- `helix_context/context_manager.py` — `_get_dense_codec` (`:759-783`)
  instantiates `BGEM3Codec(dim=config.retrieval.dense_embedding_dim)` directly.
  Change to select the codec by a new config knob (below). Note this path is the
  single construction site, so the swap is localized.
- `helix_context/config.py` — there is **no model-name knob today** (the model
  is the codec's hardcoded default; `:288-291` only exposes
  `dense_embedding_enabled` and the dim). Add `[retrieval] dense_model`
  (default `"BAAI/bge-m3"`) and optionally `dense_code_model` so code corpora
  can use a code encoder while prose uses BGE-M3. The Matryoshka-dim guard rails
  in the codec (`:62-67`) must be re-validated for any new model — its
  sanctioned breakpoints and random-pair-cosine calibration are BGE-M3-specific.
- `scripts/backfill_bgem3_v2.py` (referenced `bgem3_codec.py:38`) and the
  `genes.embedding_dense_v2` BLOB column — **column/format implications.** The
  blob is `dim*4` fp32 (`vec_to_blob`, `:34-50`) and the backfill script has a
  `length(blob) == dim*4` idempotency clause. A code model with a different dim
  means a *separate* column or a re-backfill; you cannot mix encoders in one
  column. Plan a `embedding_code_v1` column or a clean re-encode, not an
  in-place swap. **(This is the biggest hidden cost of WS4.)**

**Mapping to existing machinery.** Drops into the *existing* dense slot — same
codec interface, same `embedding_dense_v2` write path
(`context_manager.py:823-904`), same retrieval gate
(`[retrieval] dense_embedding_enabled`). No new pipeline stage; a model and
config swap plus a re-encode.

**Effort.** M (if the model has a sentence-transformers interface) to L (if it
needs a bespoke loader, e.g. voyage via API, or a full re-backfill of a large
genome). The re-encode of an existing genome dominates the cost.

**Risk.** Medium-low *technically*, but **strategically deprioritized.** (1) The
review is explicit that dense is *not* where the code win comes from; spending
GPU + storage + re-backfill here before WS1–WS3 land would be chasing the wrong
lever (§4 leak-guard). (2) New-model calibration: every dense threshold and the
Matryoshka guard rails are tuned for BGE-M3 and must be re-derived. (3) API
models (voyage) add a network dependency that conflicts with Helix's
local-first, no-query-time-inference posture. Prefer a local code model
(GraphCodeBERT / Jina) if WS4 is pursued at all.


## 3. Phased Rollout & Acceptance Metrics

Order chosen by ROI/risk and by dependency: WS3 needs WS2's graph; WS4 is
optional and last. Each phase is gated on the prior clearing its acceptance bar
against the **BM25 baseline** (the parity result we are converting).

### Phase 0 — Make the existing AST path real (packaging + observability)

- **Do:** WS1 packaging decision (tree-sitter to core deps, or loud warn on
  fallback) + the AST-vs-regex path-counter/log + the `[ingestion] ast_chunking`
  A/B knob. *No algorithm change yet.*
- **Why first:** the AST chunker already exists but is dark on default installs.
  We cannot measure WS1 honestly until we can prove the AST path actually ran.
- **Acceptance / exit:** on a code corpus, instrumentation confirms AST chunking
  fires (path-counter > 0) and the regex fallback is no longer silent. No
  retrieval regression vs current default on ContextBench prose recall (guard:
  prose path is untouched).

### Phase 1 — WS1 AST-aware chunking (cAST recursive merge)  *[recommended first real phase]*

- **Do:** tighten `chunk_code_ast` to cAST recursive split-then-merge; emit chunk
  metadata (symbol/lang/span) for WS2.
- **Acceptance:**
  - **RepoBench-R `acc@k`** (k = 1/3/5): AST chunking **> BM25 baseline**, with
    the target informed by cAST's **+4.3 Recall@5**. The bar is "clears parity
    into a measurable win," not merely "non-regression."
  - **CodeRAG-Bench `NDCG@10` > BM25** on at least the code-retrieval subsets we
    already run.
  - **ContextBench line/block recall:** block recall up (fewer functions split
    across chunk boundaries), line recall not down.
- **Gate to Phase 2:** Phase 1 must clear the RepoBench-R win bar; if AST
  chunking alone doesn't beat BM25, fix chunking before adding graph signals.

### Phase 2 — WS2 symbol def/ref graph

- **Do:** symbol edges (`DEFINES`/`SYMBOL_REF`) + `symbol_defs` table + ingest
  edge-emission + symbol-aware expansion. Ship as a *retrieval-expansion* signal
  first (pull in referenced definitions), independent of PageRank.
- **Acceptance:**
  - **RepoBench-R `acc@k` ≥ Phase 1** (symbol expansion should not hurt; ideally
    helps cross-file cases where the answer is a *referenced* definition).
  - **Edge-quality audit:** sampled `SYMBOL_REF` edges are correct (defining
    chunk actually defines the referenced symbol) above a precision threshold —
    this is the gate that protects WS3.
  - **CodeRAG-Bench `NDCG@10` ≥ Phase 1**, with lift on multi-file queries.
- **Gate to Phase 3:** symbol-edge precision must clear its bar — PageRank on a
  noisy graph is worse than no PageRank.

### Phase 3 — WS3 personalized PageRank + budget trim

- **Do:** `symbol_pagerank.py` fusion tier + budget-ordered trim +
  session-aware personalization + classifier gating to code queries.
- **Acceptance:**
  - **RepoBench-R `acc@k` > Phase 2** — structural centrality should lift the
    right definition above peripheral term-matches.
  - **CodeRAG-Bench `NDCG@10` > Phase 2.**
  - **Budget/latency:** no `expression_tokens` budget overrun; PageRank stays
    candidate-local (latency within the existing query budget, no query-time
    model call).
  - **Lexical-first guard:** exact-identifier queries must not regress — a query
    that is a literal function name still returns that function at rank 1.

### Phase 4 (optional) — WS4 code-trained embedding

- **Do:** only if Phases 1–3 land and dense is shown to add marginal code recall;
  add `dense_model` config + code codec + a separate embedding column.
- **Acceptance:** measurable code-retrieval lift *attributable to the dense tier*
  (tier-contribution decomposition, the diagnostic we already build for dense),
  net of the re-backfill cost. If dense doesn't move the needle over WS1–WS3,
  **do not ship it** (§4).


## 4. Leak-Guards / What NOT to Do

These are the failure modes the OSS review explicitly warns against. They are
guard-rails on every workstream above.

1. **Do NOT re-chase dense as the primary code signal.** The review's core
   finding is that lexical-first is the *right* base for code because
   exact-identifier matching is load-bearing and dense misses it. WS4 is
   optional and last for this reason. No phase may make a dense encoder a
   *gate* on code retrieval; dense stays a secondary, off-by-default tier
   (`[retrieval] dense_embedding_enabled` default behavior, secondary fusion
   tier only).

2. **Keep lexical-first intact.** Every new signal (PageRank, symbol expansion)
   enters as an *additive fusion tier under* the lexical tiers
   (`lex_anchor`/`filename_anchor` in `knowledge_store.py` →
   `retrieval/fusion.py`), never as a replacement ranker or a pre-filter that
   can drop an exact lexical hit. Acceptance includes an "exact-identifier query
   returns the literal symbol at rank 1" regression check (Phase 3).

3. **Do NOT build a new graph subsystem.** Symbol edges reuse `gene_relations`
   (the `StructuralRelation` discriminated union, schemas.py:28-33) and mirror
   `entity_graph`. PageRank reuses the candidate-local graph + `expression_tokens`
   budget. We are *extending* co-activation, not forking it. A parallel symbol
   store would duplicate the freshness/pruning/Hebbian machinery for no gain.

4. **Do NOT run whole-repo PageRank at query time.** Keep it candidate-local
   (top-N + 1-hop), CPU, no model. Helix's posture is no-neural-inference at
   query time for the default path; PageRank must respect that — it is graph
   arithmetic, not inference.

5. **Do NOT ship the dark AST path as "done."** tree-sitter is not in core deps;
   the AST chunker silently falls back to regex on a default install. "AST
   chunking exists in the tree" is *not* the same as "AST chunking runs for
   users." Phase 0 exists precisely to close this gap before any WS1 win is
   claimed.

6. **Do NOT over-weight structure on prose.** Symbol PageRank and AST chunking
   must be gated to `content_type == "code"` / code-classified queries. Prose
   retrieval is already well-served by the lexical base; structure signals on
   prose are cost without benefit (and risk regression). The classifier
   (`query_classifier.py`) and `_chunk_code`'s content-type guard
   (`fragments.py:78`) are the gates.

7. **Do NOT mix embedding models in one column.** If WS4 happens, a code encoder
   needs its own `embedding_*` column or a clean re-backfill — the
   `embedding_dense_v2` blob is dim-and-model-specific
   (`bgem3_codec.py:34-50` + the backfill idempotency clause). Silently swapping
   the model under the same column corrupts retrieval.


## Appendix: Grounding Code Facts

Cited file:line evidence gathered before drafting. The headline facts that
change a greenfield plan are marked ★.

**WS1 — chunking**
- ★ `helix_context/codons.py:1-8` — now a *back-compat shim*; real chunker lives
  in `helix_context/encoding/fragments.py`.
- ★ `helix_context/encoding/tree_chunker.py:205-308` — `chunk_code_ast` already
  implements tree-sitter split-then-merge; `_BOUNDARY_NODES:135-200` covers 11
  languages; `_get_parser:79-126` uses per-language packages; `is_available:311-318`.
- ★ `helix_context/encoding/fragments.py:133-159` — `CodonChunker._chunk_code`
  already *prefers* the AST path and falls back to regex (`:161-216`) silently on
  `ImportError`/`ValueError`. Content-type guard at `:78-79`.
- ★ `pyproject.toml` — core `dependencies` = fastapi/uvicorn/httpx/pydantic/
  filelock only; tree-sitter is in the `ast` extra (`:75-80`) and `all`
  (`:104-108`) **but not core**, so AST chunking is dark on a default install.

**WS2 — symbol graph**
- ★ `helix_context/schemas.py:28-33` — `StructuralRelation(IntEnum)` with
  `CHUNK_OF = 100`; explicitly a discriminated union on
  `gene_relations.relation` (0–6 NL, 100+ structural) — designed to be extended.
- ★ `helix_context/context_manager.py:949-998` — `_make_parent_doc_id` +
  `_upsert_parent_doc` already build child→parent `CHUNK_OF` edges via
  `store_relations_batch` (`:996`); the template for symbol-edge emission.
- `helix_context/storage/ddl.py:205-215` — `gene_relations` table
  `(gene_id_a, gene_id_b, relation, confidence, updated_at)`; `:222-240`
  `entity_graph` (the shape to mirror for `symbol_defs`).
- `helix_context/storage/co_activation.py:28-62` `auto_link_by_entity` (gates on
  `shared>=2`, confidence); `:111-180` `expand_coactivated` (typed-edge walk,
  relation-code branch at `:144-150`).
- `helix_context/retrieval/expand.py:50-94, 138-182` — forward/backward/sideways
  1-hop traversal feeding `/context/expand`.
- `helix_context/retrieval/seeded_edges.py:67, 79-82` — Hebbian effective-weight
  + PRUNE_FLOOR, the evidence/pruning machinery symbol edges can reuse.

**WS3 — PageRank + budget**
- `helix_context/scoring/` — existing CPU scorers (`cymatics.py`, `tcm.py`,
  `blend.py`, `ray_trace.py`); new `symbol_pagerank.py` sits here.
- `helix_context/knowledge_store.py:370, 466, 2289-2329` — `lex_anchor` tier
  accumulation + feed into the Fuser; `:1884-1920` filename-anchor tier
  (`Tier 0.5`). These are the templates for a `symbol_pagerank` fusion tier.
- `helix_context/retrieval/fusion.py` / `fusion_plr.py` — additive + RRF fusers
  (`fusion_mode` in `[retrieval]`).
- `helix_context/context_manager.py:2330+` `_assemble`; `:2556-2557` budget =
  `ribosome_tokens + expression_tokens`; `estimate_tokens` import `:29`. This is
  Aider's `--map-tokens` analog.
- Session delivery (CLAUDE.md "session working-set register",
  `session_delivery_enabled`) under `helix_context/identity/` — the
  in-context-files personalization weight.
- `helix_context/retrieval/query_classifier.py`, `intent_router.py` — code-query
  gating with no model call.

**WS4 — dense slot**
- ★ `helix_context/backends/bgem3_codec.py:52-67` — `BGEM3Codec(dim=1024,
  model_name="BAAI/bge-m3")` hardcoded; `_QUERY_PREFIX:20`,
  `_SANCTIONED_DIMS={1024,768,512}:24` are BGE-M3-specific calibrations.
  `vec_to_blob:34-50` packs `dim*4` fp32; backfill idempotency keys on
  `length(blob)==dim*4`.
- ★ `helix_context/context_manager.py:759-783` `_get_dense_codec` — single
  construction site; `:823-904` the dense write path.
- ★ `helix_context/config.py:288-291` — only `dense_embedding_enabled` + dim are
  exposed; **no `dense_model` knob exists** — WS4 must add one.
- `scripts/backfill_bgem3_v2.py` (referenced `bgem3_codec.py:38`) +
  `genes.embedding_dense_v2` column — a code model needs a separate column /
  re-backfill, not an in-place swap.

**Source numbers cited (from `docs/research/oss-semantic-retrieval-vs-helix.md`
§4):** cAST (arXiv 2506.15655) +4.3 Recall@5 RepoEval / +2.67 Pass@1
SWE-bench vs line chunking; Aider repo-map (tree-sitter def/ref + personalized
PageRank + map-tokens budget); voyage-code-3 +13.8% vs OpenAI-v3-large; Jina
code matches voyage at 1.5B params.

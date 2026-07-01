# Helix Retrieval Pipeline + Switchboard Architecture

Status: as-shipped reference (2026-06-07)
Scope: ingestion -> switchboard/routing -> tiered retrieval -> fusion -> scoring -> delivery

## Provenance

This document records the **as-shipped behavior of the helix063 wheel** (the
measured artifact behind the 2026-06-07 code-context benchmark sweep), located at:

```
F:/Projects/_venvs/helix063/Lib/site-packages/helix_context/
```

The source-repo `master` is stale (local master is 0.5.0; shipped tags are
v0.6.2 / v0.6.3). **Verify every claim against the wheel, not the source tree.**
All `file:line` citations below are relative to the wheel package root above
unless otherwise noted. The retrieval entry method is named `query_docs()` in
the code (the brief's informal "query_genes()"); the genes/documents vocabulary
is the Helix bio-Rosetta for "documents/chunks".

---

## 0. Executive summary

Helix `query_docs()` is a **single shared multi-tier retrieval engine** used by
both prose corpora (EnterpriseRAG) and code corpora (RepoBench-R / CodeRAG-Bench).
It accumulates per-document scores across lexical, sparse, and dense tiers, then
ranks. The default fusion strategy is **`additive`** (`gene_scores += tier_score`),
NOT rank fusion.

**Known scoring defect (2026-06-07):** under the default `additive` fusion the
IDF-free tag tiers (`tag_exact` x3.0, `tag_prefix` x1.5) inject flat,
query-independent "magnet" mass that buries the true gold, whose IDF-correct
FTS5/BM25 advantage is **capped at 6.0**. Flipping `fusion_mode -> rrf`
(already in the wheel) recovers most of the loss. See Section 7.

```
INGEST                 SWITCHBOARD            RETRIEVE (query_docs)        DELIVER
------                 -----------            ---------------------        -------
content                query_type hint  -->  Tier 1  tag_exact      x3.0
  | spaCy/extract      (caller-supplied)     Tier 2  tag_prefix     x1.5
  v                    classify_query   -->  Tier 3  FTS5 (cap 6.0)        sort by
genes + tags           (decoder_mode,        Tier 3.5 SPLADE (off)         last_query_scores
promoter_index         max_genes cap)        Tier 4  dense / SEMA          per-gene budget
genes_fts (FTS5)       shard_router     -->  Tier .5 filename_anchor       context packet
entity_graph           (which shards)        lex_anchor (IDF)
embedding_dense_v2                           authority / harmonic
                                             --------- FUSION ---------
                                             additive (default) | rrf
                                             --------- REFINERS -------
                                             cymatics / harmonic_bin /
                                             tcm / rerank (off)
```

---

## 1. Ingestion pipeline

Ingestion turns raw content into the SQLite artifacts each retrieval tier reads.
One genome = one SQLite DB (sharded; see Section 8).

| Artifact | Produced from | Read by tier |
|---|---|---|
| `genes` table | document chunk + metadata (`source_id`, `content`, `chromatin` lifecycle) | all (final row fetch, lifecycle gate) |
| `promoter_index` (gene_id, tag_value) | extracted domains+entities tags | Tier 1 `tag_exact`, Tier 2 `tag_prefix`, `lex_anchor` IDF |
| `genes_fts` (FTS5 virtual table over content + complement) | full chunk text + promoter tags | Tier 3 FTS5, bm25 prefilter/shortlist |
| `entity_graph` | entity co-occurrence | Tier 5b entity-graph boost (off by default) |
| `embedding_dense_v2` (BGE-M3 1024-d) | dense encode at ingest | Tier 4 dense recall |
| `splade_terms` | SPLADE sparse expansion (off by default) | Tier 3.5 SPLADE |

Key mechanics:
- `promoter_index` is (re)built per gene by `rebuild_promoter_index(cur, gene_id, gene)` (`knowledge_store.py:1224-1231`).
- `genes_fts` is a content+complement FTS5 index; rebuild SQL at `knowledge_store.py:3623-3635` (`INSERT INTO genes_fts(gene_id, content, complement) ...` joining `promoter_index`).
- **Dense write at ingest** is gated by `_dense_embed_on_ingest` (`knowledge_store.py:460`, consumed `:2610-2632`). Config default `[ingestion] dense_embed_on_ingest = True` (`config.py:230`); independent of the retrieval gate `dense_embedding_enabled`.

**Lexical-probe ingest config** (the code-context benchmark setup) disables the
two model-heavy ingest stages:
- `[ingestion] dense_embed_on_ingest = false` -> no BGE-M3 vectors written (Tier 4 dark).
- `[ingestion] splade_enabled = false` -> no `splade_terms` table (Tier 3.5 never fires; default already off, `config.py:218`).

This leaves a pure lexical corpus: `genes` + `promoter_index` + `genes_fts`. The
benchmark therefore measures the lexical/tag fusion path in isolation -- which is
exactly where the magnet defect in Section 7 lives.

---

## 2. Switchboard / Tier-0 routing

The "switchboard" is **not** a single module; it is a thin, cheap, LLM-free
routing layer assembled from three distinct mechanisms. None of them does
automatic basic-vs-semantic classification -- `query_type` is a **caller-supplied
per-call hint**, not an inferred label.

### 2.1 `query_type` (basic | semantic) -- caller-supplied

```
client request body
  --> data.get("query_type")            routes_context.py:269  (/context)
                                         routes_context.py:738  (/fingerprint)
  --> build_context / _retrieve         context_manager.py:1041, 2108
  --> query_docs(query_type=...)        knowledge_store.py:1488
  --> shard_router.route(query_type=...) shard_router.py:375
```

- Parsed from the request body, lowercased, defaulting to `None`
  (`routes_context.py:269`, `:738`). The comment is explicit: *"Production
  callers omit it ... a runtime semantic detector is a separate track"*
  (`routes_context.py:733-737`). The bench injects the needle's ground-truth
  type to A/B the semantic arm.
- `query_type=="semantic"` changes retrieval **only** when env
  `HELIX_SEMANTIC_ARM=1`, in two coupled places:
  1. **Dense weight scaling** (`knowledge_store.py:2119-2120`): the additive-mode
     dense term is scaled by `_semantic_dense_additive_weight` (default 12.0,
     `config.py:397`) instead of `_dense_additive_weight` (4.0). Env override
     `HELIX_SEMANTIC_DENSE_WEIGHT` (`knowledge_store.py:2124-2128`).
  2. **Routing broaden** (`shard_router.py:393-401`): bypass the LIKE shard
     gate, fan out to **all healthy shards** so dense-top golds in
     literal-mismatched shards still enter the pool. Disable with
     `HELIX_SEMANTIC_BROADEN=0` (`shard_router.py:397-399`).
- Any other `query_type` value, or the arm off, is **byte-identical** to
  baseline. The dense-ANN solo branch (`query_docs_ann`) is deliberately NOT
  threaded with `query_type` -- the arm is sharded-only
  (`context_manager.py:2112-2116`).

### 2.2 Injection-router query classifier (decoder_mode + max_genes cap)

A separate, pure regex classifier shapes **delivery**, not retrieval-tier gating:
`retrieval/query_classifier.py`.

- `classify_query()` (`query_classifier.py:147`) buckets the NL query into
  `arithmetic | factual | procedural | multi_hop | default` via keyword/operator
  scans (`:160-208`). Infallible by construction (returns `default` on error).
- Each class carries an `assembly_max_genes_cap` and a `decoder_mode`
  (`query_classifier.py:162-206`): e.g. arithmetic caps to 2 genes / `minimal`
  decoder; factual 5 / `condensed`; procedural 6 / `full`; multi_hop 8 / `full`.
- `resolve_decoder_mode(cls, caller_model_class)` (`:133`) maps the class x
  caller-model matrix (`DECODER_MODE_TABLE`, `:124-130`) to a final decoder mode.
- This influences how much context is assembled and how it is framed; it does
  **not** turn tiers on/off.

### 2.3 Shard router (which shards fire)

`shard_router.py:route()` (`:371`) selects shards by a LIKE scan over
`fingerprint_index.domains/entities` ordered by hit count (`:417-428`). Empty
query or no terms -> all healthy shards (`:404-405`). The semantic-arm broaden
(2.1) is the only behavior that ignores the LIKE gate.

### 2.4 Intent router (sub-query templates)

`retrieval/intent_router.py` maps an `IntentClass` to 3 point-fact sub-query
templates (`intent_router.py:12-64`). Used by the LLM-free decomposition path
only; orthogonal to the per-call tier scoring.

**Switchboard summary:** routing is cheap and signal-light. The only retrieval
behavior it gates is the (default-off) semantic dense arm. Everything else is
delivery shaping (decoder_mode, max_genes cap) and shard selection.

---

## 3. Retrieval pipeline, tier by tier

Entry: `query_docs(domains, entities, max_genes, ..., query_type, question_text)`
at `knowledge_store.py:1478`. `limit = max_genes * 2` (`:1530`). A `Fuser`
(Section 4) is built unconditionally (`:1574-1575`) but only *consulted* under
`fusion_mode == "rrf"`.

Per-tier accumulation writes BOTH `gene_scores[gid] += tier_score` (additive
path) AND `fuser.add_tier(name, ranked_list, weight)` (rrf path).

### 3.1 Tier table (as-shipped weights)

| # | Tier | Additive contribution | IDF-weighted? | RRF raw score fed | Default | Cite |
|---|---|---|---|---|---|---|
| 0.5 | filename_anchor | `+filename_anchor_weight` per stem match (4.0) | No | per-match contrib | OFF (`filename_anchor_enabled=False`) | `:1739-1748`; cfg `config.py:302-303` |
| 1 | tag_exact | `match_count * 3.0` | **No (flat magnet)** | raw `match_count*3.0` | ON | `:1771-1778` |
| 2 | tag_prefix | `match_count * 1.5` | **No (flat magnet)** | raw `match_count*1.5` | ON | `:1811-1815` |
| 3 | FTS5 | `min(-rank, 6.0)` **(capped)** | Yes (BM25) | **raw uncapped `-rank`** | ON if FTS available | `:1865-1874` |
| 3.5 | SPLADE | `min(score,20)*3.5/20` | Sparse-learned | raw uncapped score | OFF (`splade_enabled=False`) | `:1904-1911`; cfg `config.py:218` |
| 4 | dense / SEMA | additive: `cosine * dense_additive_weight` (4.0) | dense (cosine) | raw cosine | ON (`dense_embedding_enabled=True`) | `:2118-2137`, `:2082-2096`; cfg `config.py:323` |
| 4b | lex_anchor (IDF) | `min(idf*1.5, 3.0)` per term, summed | **Yes** | summed contrib | ON | `:2174-2200` |
| 5 | harmonic co-activation | per-link 1.0, cap 3.0 | n/a (graph) | per-link | ON if links present | `:2212-2224` |
| 5b | entity_graph | `entity_graph_weight` (0.5) | n/a (graph) | overlap | OFF (`entity_graph_retrieval_enabled=False`) | cfg `config.py:317` |
| RR | authority/recency | +2.0 source / +1.5 domain / +0.5 recency | n/a (re-rank) | additive on fused | ON | `:1321-1326`, applied `:2206-2210` |

Notes on the load-bearing constants:
- **tag_exact** literal `r["match_count"] * 3.0` (`knowledge_store.py:1771`).
- **tag_prefix** literal `r["match_count"] * 1.5` (`knowledge_store.py:1811`).
- **FTS5 cap** literal `fts_score = min(-fts_ranks[gid], 6.0)` with the code
  comment *"(was 15*3=45 -- drowned out tag matches at 3-9)"*
  (`knowledge_store.py:1864-1865`). The RRF path is fed the **uncapped**
  `-fts_ranks[gid]` (`:1873`) precisely so rank order survives cap saturation.
- **lex_anchor** is a genuinely IDF-weighted tier (`idf = log(total/term_freq)`,
  `boost = min(idf*1.5, 3.0)`, only applied when `boost > 1.0`)
  (`knowledge_store.py:2174-2176`). It is *separate* from tag_exact/tag_prefix and
  partially offsets -- but does not cure -- the flat-magnet problem, because its
  cap (3.0) is below the magnet mass a boilerplate doc accrues from many common
  tokens (~6 tokens x 3.0 = ~18 on tag_exact alone).

### 3.2 Dense recall is first-class in BOTH modes (since Tier-0 PR-3)

`dense_embedding_enabled` alone gates dense recall (`knowledge_store.py:2052`);
the old `and fusion_mode=="rrf"` gate was removed (`:2035-2041`) -- so the
ingest-time BGE-M3 vectors are no longer dark under the default additive mode.
`fusion_mode` only decides HOW dense enters:
- **rrf**: `fuser.add_tier("dense", dense_hits, weight=dense_weight)` (`:2082-2086`).
- **additive**: `gene_scores[gid] += cosine * dense_additive_weight` above the
  `dense_additive_min_cosine` (0.15) floor (`:2130-2137`).

Note: with `dense_embedding_enabled=True` (shipped default), `_retrieve` routes
to `query_docs_ann` (the ANN-threshold path), NOT `query_docs`
(`context_manager.py:2146-2159`). The `query_type` semantic arm is threaded only
through the **`query_docs` sharded path** (`:2160-2172`), i.e. it engages when
dense retrieval is OFF or in the sharded merge -- a subtlety relevant to the
2026-06-07 reconciliation (Section 7).

### 3.3 Candidate gating

- **BM25 prefilter** (pre-scoring): `bm25_prefilter_enabled` -> `_bm25_candidate_set`
  scopes every tier's SQL to FTS5 top-N (`knowledge_store.py:1534-1547`,
  helper `:1455-1474`). Default OFF; size 200 (`config.py:312-313`).
- **BM25 shortlist** (post-scoring filter): `bm25_shortlist_enabled` -> after all
  tiers, drop any `gene_scores` gene not in FTS5 top-N (`:2415-2444`). Default OFF;
  size 50 (`config.py:310-311`). Mutually exclusive with prefilter
  (`:2417`, "don't double-filter").
- In both the code and prose benches the gold is confirmed present in the BM25
  shortlist and scored -- the defect is **ranking**, not candidate generation.

---

## 4. Fusion: `additive` (default) vs `rrf`

`retrieval/fusion.py` -- a pure `Fuser` data structure (no SQL, no telemetry).

`score(d) = sum over tiers t of  weight_t * 1/(k + rank_t(d))`, k=60 Cormack
default (`fusion.py:13-19, 42, 122-127`). Ranks are computed by sorting each
tier's `(gid, raw_score)` list `(-score, gid)` (`:117-120`) -- so the raw scores
only determine ORDER, never magnitude. This is why the FTS5 tier feeds the
**uncapped** `-rank` to the Fuser: under RRF the 6.0 cap is irrelevant, only the
relative order matters.

Final-ranking branch (`knowledge_store.py:2451-2494`):
- **additive (DEFAULT, `config.py:369`)**: `ranked_ids = sorted(gene_scores, key=gene_scores.get, reverse=True)[:limit]` (`:2492-2494`). The Fuser is built but never queried (`:1565-1567`). `last_query_scores = gene_scores` accumulator.
- **rrf**: `fused_scores = fuser.all_scores()`, plus `rerank_additive` (authority etc.) on top, restricted to genes surviving the shortlist filter, then sorted (`:2459-2483`). `last_query_scores = final fused+additive` (`:2482-2483`).

**Crucial:** because additive is the default, the live ranking is dominated by
raw, non-commensurate tier magnitudes. The module docstring itself warns that
summing non-commensurate scores *"lets one over-scaled tier dominate"*
(`fusion.py:8-11`) -- which is exactly the observed magnet failure.

---

## 5. Scoring -> ranking -> delivery

1. **Tier accumulation** -> `gene_scores` + parallel `tier_contrib[gid][tier]`
   (`knowledge_store.py:1556`, surfaced as `last_tier_contributions`).
2. **Authority / harmonic / SR re-rank** additives (`:2206-2224`).
3. **Optional gates**: bm25 shortlist (`:2415-2444`), walking tie-break
   (`HELIX_WALKING_TIEBREAK=1`, `:2504-2511`), Hebbian seeded-edge writeback
   (`:2521-2528`).
4. **Final sort** -> `ranked_ids` (`:2459-2494`); rows fetched and order
   preserved (`:2530-2539`); co-activation pull-forward + dedupe (`:2542-2552`).
5. `self.last_query_scores` is set (additive accumulator or rrf fused map). The
   orchestrator reads it via `genome.last_query_scores` under a lock
   (`context_manager.py:1223-1224`) for: candidate sort by score
   (`context_manager.py:2376-2380`), small_moe answer slate best-first
   (`:1433-1442`), and trim ordering (`:2567-2583`).
6. **Refiners** (`_apply_candidate_refiners` -> `scoring/blend.py`): cymatics
   (`blend.py:52-74`), cross-encoder **rerank** (`:87-118`, gated on
   `rerank_enabled` AND a ribosome with `.rerank` -- both OFF in the probe;
   `config.py:220` default `False`), harmonic_bin (`:120-151`), tcm
   (`:153-166`). All bonuses fold back into `last_query_scores` and re-sort.
7. **Per-gene budget / context packet**: `compute_uniform_targets(...)`
   (`context_manager.py:1463-1473`) allocates per-gene char budgets;
   `per_gene_budget` mode `fixed` (1000 chars) vs `dynamic` (floor-then-greedy
   fill of `expression_tokens` headroom up to `per_gene_ceiling_chars`). Optional
   content-aware surplus keyed on a tier's `tier_contrib`
   (`per_gene_budget_relevance_signal`, `:1456-1462`). The assembled window is
   wrapped and returned via `/context`; `/context/packet` returns a
   freshness-labeled evidence packet (`routes_context.py:546-547`).

### Endpoints (`server/routes_context.py`)
| Endpoint | Handler | query_type thread |
|---|---|---|
| `POST /context` | `context_endpoint` (`:151`) | yes (`:269` -> `build_context_async :271-283`) |
| `POST /context/packet` | `context_packet_endpoint` (`:546`) | evidence packet path |
| `POST /fingerprint` | `fingerprint_endpoint` (`:662`) | yes (`:738` -> `_retrieve :753-763`) -- recall path |

---

## 6. Config knob map

`[retrieval]` / `[ingestion]` keys map to Genome ctor kwargs in `config.py`
(`:729-827`). Defaults are the as-shipped `RetrievalConfig` / `IngestionConfig`
dataclass values.

| Knob | Section | Default | Effect | Cite |
|---|---|---|---|---|
| `fusion_mode` | retrieval | `additive` | `additive` = sum tier scores; `rrf` = rank fusion | `config.py:369`, applied `knowledge_store.py:2459` |
| `rrf_k` | retrieval | `60` | RRF saturation constant | `config.py:370` |
| `tag_exact_weight` | retrieval | `3.0` | Tier-1 magnet weight (RRF post-mult; additive uses literal x3.0) | `config.py:373`; `knowledge_store.py:1771` |
| `tag_prefix_weight` | retrieval | `1.5` | Tier-2 magnet weight | `config.py:374`; `:1811` |
| `fts5_weight` | retrieval | `3.0` | RRF post-mult for FTS5 (additive uses min(-rank,6.0) cap) | `config.py:371`; `:1865` |
| `splade_weight` | retrieval | `3.5` | RRF post-mult for SPLADE | `config.py:372` |
| `lex_anchor_weight` | retrieval | `1.5` | IDF anchor multiplier (additive caps at 3.0) | `config.py:376`; `:2175` |
| `dense_weight` | retrieval | `1.0` | RRF post-mult for dense | `config.py:379` |
| `dense_additive_weight` | retrieval | `4.0` | additive-mode dense cosine scale | `config.py:384`; `:2118` |
| `dense_additive_min_cosine` | retrieval | `0.15` | additive dense noise floor | `config.py:390`; `:2131` |
| `semantic_dense_additive_weight` | retrieval | `12.0` | dense scale when query_type==semantic AND HELIX_SEMANTIC_ARM=1 | `config.py:397`; `:2120` |
| `semantic_broaden_routing` | retrieval | `True` | broaden to all shards under semantic arm | `config.py:398`; `shard_router.py:393` |
| `dense_embedding_enabled` | retrieval | `True` | gate Tier-4 dense recall (routes _retrieve to query_docs_ann) | `config.py:323`; `:2052` |
| `dense_embedding_dim` | retrieval | `1024` | BGE-M3 Matryoshka dim | `config.py:327` |
| `ann_similarity_threshold` | retrieval | `0.58` | ANN dynamic-count cut (dim-1024 calibrated) | `config.py:334` |
| `ann_threshold_min_genes` | retrieval | `1` | ANN floor count | `config.py:335` |
| `ann_threshold_max_genes` | retrieval | `12` | ANN ceiling count (cap-collapse prod note) | `config.py:336` |
| `dense_pool_size` | retrieval | `500` | dense recall candidate pool | `config.py:348` |
| `dense_prefilter_enabled` | retrieval | `False` | scope dense matmul to lex/SPLADE candidates | `config.py:354` |
| `bm25_prefilter_enabled` | retrieval | `False` | scope all tiers to FTS5 top-N before scoring | `config.py:312`; `:1534` |
| `bm25_prefilter_size` | retrieval | `200` | prefilter top-N | `config.py:313` |
| `bm25_shortlist_enabled` | retrieval | `False` | drop non-FTS5-top-N genes before final sort | `config.py:310`; `:2415` |
| `bm25_shortlist_size` | retrieval | `50` | shortlist top-N | `config.py:311` |
| `filename_anchor_enabled` | retrieval | `False` | Tier-0.5 filename-stem boost | `config.py:302`; `:1739` |
| `filename_anchor_weight` | retrieval | `4.0` | per-stem-match boost | `config.py:303` |
| `entity_graph_retrieval_enabled` | retrieval | `False` | Tier-5b entity co-occurrence boost | `config.py:317` |
| `sr_enabled` | retrieval | `False` | Successor-Representation graph boost (dark) | `config.py:284` |
| `seeded_edges_enabled` | retrieval | `False` | Hebbian edge evidence writeback | `config.py:296` |
| `splade_enabled` | ingestion | `False` | write `splade_terms` + Tier-3.5 SPLADE | `config.py:218` |
| `dense_embed_on_ingest` | ingestion | `True` | write BGE-M3 vectors at ingest | `config.py:230` |
| `rerank_enabled` | ingestion | `False` | cross-encoder rerank refiner (needs ribosome) | `config.py:220`; `blend.py:89-93` |

Env switches (not in helix.toml): `HELIX_SEMANTIC_ARM`, `HELIX_SEMANTIC_BROADEN`,
`HELIX_SEMANTIC_DENSE_WEIGHT`, `HELIX_QUESTION_DENSE`, `HELIX_WALKING_TIEBREAK`,
`HELIX_RERANK_CAPTURE`/`HELIX_RERANK_DIAG`.

---

## 7. 2026-06-07 findings / known issues

A code-context benchmark sweep (RepoBench-R, CodeRAG-Bench) exposed a
**re-ranking defect in the default `additive` fusion**. Verified against the
wheel code as cited.

### 7.1 Symptom

CodeRAG-Bench canonical retrieval (programming-solutions, 1128 docs;
HumanEval 164 + MBPP 500 queries) is **lexically saturated** -- the gold doc
embeds the query verbatim -- so plain BM25 scores NDCG@10 = 0.998 (HumanEval) /
0.982 (MBPP). Helix under `fusion_mode=additive` scored only
**NDCG@10 = 0.439 / 0.332** -- missing the obvious gold ~35-41% of the time even
at rank 10.

### 7.2 Root cause (confirmed by per-query diagnostic)

The gold is ALWAYS in Helix's BM25 shortlist and scored, but the ranking buries
it:

- IDF-free `tag_exact` (`match_count * 3.0`, `knowledge_store.py:1771`) and
  `tag_prefix` (`* 1.5`, `:1811`) accumulate **flat, query-independent magnet
  mass** (~18 = ~6 common tokens x 3.0) on boilerplate-dense docs.
- The gold's IDF-correct FTS5/BM25 advantage is **capped at 6.0**
  (`fts_score = min(-fts_ranks[gid], 6.0)`, `:1865`) -- the literal code comment
  *"(was 15*3=45 -- drowned out tag matches at 3-9)"* documents that the cap was
  intentionally lowered to stop FTS5 dominating tags. The cap now over-corrects:
  a boilerplate-dense doc matching many common tokens outranks the true gold
  even at 100% query-token overlap.
- The IDF-weighted `lex_anchor` tier (`:2174-2176`) exists and helps, but its own
  3.0 cap is below the magnet mass, so it cannot fully recover the gold.

This is a structural consequence of **additive** fusion summing
non-commensurate tiers -- the exact failure the `fusion.py` docstring warns
about (`fusion.py:8-11`).

### 7.3 Fix demonstrated (config-only; RRF already in the wheel)

Flipping `fusion_mode -> rrf`:
- HumanEval NDCG@10 0.439 -> **0.684** (r@1 0.268 -> **0.549**, 2x; r@10 0.652 -> **0.823**).
- MBPP NDCG@10 0.332 -> **0.404**.

Still below BM25's 0.998 because the IDF-free tag tiers still inject rank-noise
into the fused order (each magnet doc still earns a high tag rank). The FULL fix
is to **IDF-weight the tag tiers** (so common-token matches stop contributing
flat magnet mass) and/or **lift the FTS5 6.0 cap** (so true BM25 ordering
survives in additive mode). Both are code changes, not config.

### 7.4 Systemic implication (prose confound)

`query_docs()` is the SHARED retrieval engine for prose (EnterpriseRAG) and
code. Since `additive` is the default (`config.py:369`), the same magnet bug
**suppresses prose basic-recall too**. This means the prior *"the encoder is the
recall lever"* conclusion is **confounded for the basic/lexical bucket** -- the
basic-recall ceiling may be a fusion-ranking artifact, not an embedding-geometry
limit. Only the pure *semantic* bucket (paraphrase, ~zero surface overlap)
genuinely needs the dense encoder.

**Pending confirming experiment:** re-run EnterpriseRAG recall under
`fusion_mode=rrf`, split by `query_type`. Prediction: basic-recall rises,
semantic-recall stays flat.

### 7.5 Code reconciliation notes (for the brief author)

Differences found between the brief and the wheel code -- none change the
conclusion, but flag for accuracy:

1. **Method name.** The retrieval entry is `query_docs()`
   (`knowledge_store.py:1478`), not `query_genes()`. The internal accumulator,
   tiers, and cap are as the brief describes.
2. **Line numbers drift slightly** from the brief: tag_exact x3.0 is at
   `:1771` (brief said ~tier block), the FTS5 cap + "was 15*3=45" comment is at
   `:1864-1865` (brief said ~1865 -- correct), the additive `gene_scores +=`
   ctor comment is at `:489-493` (brief said ~489-491 -- correct), RRF raw-rank
   feed at `:1873`/`:1910` (matches brief). The bm25 shortlist is `:2415-2444`,
   idf boost `:2174-2175`.
3. **An IDF-weighted tier already exists** (`lex_anchor`, `:2174-2200`) -- it is
   distinct from the tag tiers and partially offsets the magnet, capped at 3.0.
   The brief's framing ("IDF-free tag tiers") is correct for tag_exact/tag_prefix
   specifically; the doc above makes the lex_anchor coexistence explicit so the
   "IDF-weight the tag tiers" fix is not mistaken for "add IDF anywhere".
4. **Dense default is ON, and routes around `query_docs`.** With shipped default
   `dense_embedding_enabled=True`, `_retrieve` calls `query_docs_ann`
   (`context_manager.py:2146-2159`), and the `query_type` semantic arm is threaded
   only through the `query_docs` sharded branch (`:2160-2172`). For the
   *lexical-probe* config (dense off) the code path is `query_docs` directly, so
   the benchmark exercised the exact tier-accumulation path documented here. Worth
   confirming whether the prose EnterpriseRAG runs used the ANN path (dense on) or
   the lexical path -- the magnet defect is present in BOTH (tag tiers accumulate
   identically), but the dense additive term shifts the magnitudes.

---

## 8. Sharding (context)

- `shard_router.py` selects shards (Section 2.3); per-shard `query_docs` runs and
  results merge "first-shard-wins on ties" (`shard_router.py:422-427`).
- `sharding.py` / `pipeline/sharding.py` own shard layout; `pipeline/tier_logic.py`
  and `pipeline/filename_anchor.py` host the extracted tier helpers.
- The semantic-arm broaden (2.1) is a shard-routing change: it widens the shard
  fan-out, not the within-shard tier set.

---

## Appendix: file index (wheel)

| File | Role |
|---|---|
| `knowledge_store.py` | core: `query_docs()` retrieval + tier accumulator + fusion branch |
| `retrieval/fusion.py` | RRF `Fuser` data structure |
| `scoring/blend.py` | post-retrieve refiners (cymatics/rerank/harmonic_bin/tcm) |
| `context_manager.py` | orchestration: `_prepare_query_signals`, `_retrieve`, `_apply_candidate_refiners`, per-gene budget |
| `config.py` | `[retrieval]`/`[ingestion]`/`[ribosome]` -> Genome ctor kwargs |
| `server/routes_context.py` | `/context`, `/context/packet`, `/fingerprint`, query_type thread |
| `retrieval/query_classifier.py` | injection-router class -> decoder_mode + max_genes cap |
| `retrieval/intent_router.py` | LLM-free sub-query templates |
| `shard_router.py` | shard selection + semantic broaden |
| `pipeline/tier_logic.py`, `filename_anchor.py`, `sharding.py` | extracted tier/shard helpers |

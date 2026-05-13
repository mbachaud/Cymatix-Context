# Rosetta Stone — biology lexicon ↔ software lexicon

Helix's original vocabulary borrowed from molecular biology (gene, genome,
ribosome, chromatin, splice, codon, promoter, epigenetics, transcription,
expression, replication). The metaphor is evocative and shaped a lot of
the original architecture, but it imposes a real cognitive tax: every
reader — human or LLM — has to hold two mental models in parallel.

The **canonical lexicon is now standard software terminology.** This
document is the bidirectional mapping. Legacy biology terms remain valid
references (the Python aliases live in `helix_context/aliases.py`), so
older handoffs, papers, and commit messages stay readable without
modification. New code, new docs, and new tool surfaces should use the
software vocabulary.

This is a *living document* during the rename effort. As new biology
terms surface in the codebase, add them here.

---

## Identity vocabulary

The biology rename is not the only translation Helix needs. The identity
layer also has a few near-synonyms in active use across design notes,
code comments, and handoffs. The canonical schema terms stay:

- `org`
- `party`
- `participant`
- `agent`

But these are the intended meanings:

| Canonical term | Preferred meaning | Common equivalents we still want readers to understand | Notes |
|---|---|---|---|
| `org` | trust root / trust domain / tenant | org, team, company, enterprise owner, auth root | Prefer the stable owner or authorization root, not a raw credential like an API key. |
| `party` | physical substrate / device | party, host, machine, handset, workstation | `party_id` remains the schema term. In local-first federation this is usually a device or execution host. |
| `participant` | human principal | participant, user, human, operator | In authored-ingest attribution this is the human identity on whose behalf work happens. |
| `agent` | software actor | agent, AI persona, software actor, model persona | The runtime AI layer acting on behalf of the participant. |

This is intentionally a Rosetta layer, not a rename directive. New docs
should prefer the canonical schema names while making the meaning clear
in prose, for example: "party (device)" or "agent (software actor)".

---

## Mapping table

The table is bidirectional: scan the left column to translate legacy
references, scan the right column to find the canonical term for new
work.

| Biology term (legacy) | Software term (canonical) | Notes |
|---|---|---|
| `Gene` | `Document` | The atomic unit of stored knowledge. Pydantic class identity unchanged; `Document is Gene` is True. |
| `genome` / `Genome` | `KnowledgeStore` / `kb` | The persistent SQLite-backed store. |
| `gene_id` | `document_id` / `doc_id` | Hash-derived identifier on the atomic unit. |
| `genes` (collection / table) | `documents` | SQL table name stays `genes` for now (deferred — see "Schema rename" below). |
| `PromoterTags` | `DocumentTags` | The `(domains, entities, intent, summary)` tuple attached to each document. |
| `promoter` (field on Gene/Document) | `tags` | |
| `EpigeneticMarkers` | `DocumentSignals` | Access rate, co-activation list, decay score. The behavioural metadata for retrieval scoring. |
| `epigenetics` (field) | `signals` | |
| `ChromatinState` | `LifecycleTier` | The hot/warm/cold storage tier enum. |
| `chromatin` (field) | `tier` | |
| `OPEN` / `EUCHROMATIN` / `HETEROCHROMATIN` | `OPEN` / `WARM` / `COLD` | Tier values. Names are **not yet renamed in code** — `ChromatinState` in `schemas.py` still emits the bio names. The canonical name pair is the right-hand column; the rename is deferred to R3 (it would change pydantic field-string values, so it ships behind the same gate as the symbol rename). Numeric IntEnum values stay the same throughout. |
| `codon` / `Codon` | `Fragment` / `Chunk` | The within-document compressed unit. |
| `codons` (field) | `fragments` | |
| `Ribosome` | `Compressor` | The small-model pipeline that encodes raw text into compressed documents. |
| `ribosome.pack(...)` | `compressor.encode(...)` | Take raw text → emit a compressed document. |
| `ribosome.splice(...)` | `compressor.trim(...)` | Drop low-value fragments from a candidate set. (Currently unwired in the pipeline; see ribosome.py:32.) |
| `ribosome.replicate(...)` | `compressor.persist(...)` | Pack a query+response exchange back into the store. |
| `ribosome.re_rank(...)` | `compressor.rerank(...)` | Re-score retrieval candidates with a small cross-encoder. |
| `transcription` | `encoding` | The text → compressed-document conversion. |
| `express` / `_express` / `expression` | `retrieve` / `_retrieve` / `retrieval` | The candidate-selection step in the retrieval pipeline. |
| `expression_tokens` (budget config) | `retrieval_tokens` | The token budget for retrieved context. |
| `replicate` / `replication` | `persist` / `persistence` | Saving query exchanges back into the store as new documents. |
| `harmonic_links` | `coactivation_edges` | Graph edges connecting documents that have been retrieved together. |
| `harmonic_bin_boost` | `random_walk_boost` | The Monte Carlo neighbour-expansion tier. |
| `gene_attribution` | `document_attribution` | The party/participant authorship metadata on each document. |
| `GeneAttribution` | `DocumentAttribution` | |
| `HGT` (horizontal gene transfer) | `cross_store_import` | Importing documents from another helix instance. **Forward-pointer:** no code under either name today; `cross_store_import` is the name the feature will ship under when it lands. The legacy `HGT` acronym remains the term-of-art in design docs until then. |

---

## Response & routing types (STAYS — no biology twin)

These types travel on the wire between helix and its callers (HTTP
clients, MCP hosts, the CLI). They are the *response envelope*
vocabulary, not the storage vocabulary. No biology metaphor applies;
they keep their engineering names everywhere.

| Type | Purpose | Where it surfaces |
|---|---|---|
| `ContextWindow` | Full pipeline output — the bytes the agent reads. Carries `expressed_context`, `expressed_gene_ids`, `total_estimated_tokens`, plus the `metadata` dict that pipes `know`/`miss` upward. | `helix_context.schemas.ContextWindow`; returned from `HelixContextManager.build_context()`. |
| `QueryResult` | Agent-facing projection of `ContextWindow`. Adds `verdict` / `next_action` / `decision_reason`. The shape `helix query --json` emits via `to_agent_json()`. | `helix_context.api.QueryResult`. |
| `ContextPacket` | Freshness-labeled agent-safe bundle for high-risk actions. Holds `verified[]`, `stale_risk[]`, `refresh_targets[]`, plus `coordinate_confidence` and `file_coverage`. | `helix_context.schemas.ContextPacket`; built by `build_context_packet()`; emitted by `/context/packet`, `helix packet`, and the `helix_context_packet` MCP tool. |
| `ContextItem` | One evidence row inside a packet — `gene_id` / `title` / `content` / `relevance_score` / `live_truth_score` / `status` (`"verified"` / `"stale"` / `"missing"`). | `helix_context.schemas.ContextItem`. |
| `RefreshTarget` | One reread directive in a packet — `target_kind` / `source_id` / `reason` / `priority`. | `helix_context.schemas.RefreshTarget`; emitted by `helix refresh-targets`. |
| `KnowBlock` | Top-level "you may answer from this evidence" verdict. Fields: `found`, `confidence`, `gene_id_match`, `soft_stale`, etc. Mutually exclusive with `MissBlock` on a single response. | `helix_context.schemas.KnowBlock`; populated by `know_decision.decide_know_or_miss()`. |
| `MissBlock` | Top-level "do **not** answer from the knowledge store" verdict. Carries `reason` (`"miss"` / `"stale"` / `"cold"` / `"superseded"`), `escalate_to[]`, `refresh_targets[]`, `do_not_answer_from_genome:true`. | `helix_context.schemas.MissBlock`; populated by `know_decision.decide_know_or_miss()`. |
| `ContextHealth` | "Check-engine light" for a single retrieval — `ellipticity`, `coverage`, `density`, freshness signals, `coordinate_crispness`, `status` ∈ {`aligned`, `sparse`, `stale`, `denatured`}. | `helix_context.schemas.ContextHealth`; logged to the `health_log` table; not on the wire. |
| `IngestResult` | One-shot ingest projection — `gene_ids[]`, `chunks`, `bytes_written`. | `helix_context.api.IngestResult`. |
| `StatsResult` | One-shot stats projection used by `helix diag corpus`. | `helix_context.api.StatsResult`. |

Note: `ellipticity` and `denatured` are CD-spectroscopy / physics terms,
not biology. They remain on the [Terms that STAY](#terms-that-stay-not-biology-not-tax)
list and are unrenamed.

---

## Metric & label vocabulary (Prometheus surface)

Prometheus metric names and label values are a **contract** for anyone
querying them — Grafana panels, alert rules, ad-hoc PromQL. The same
"don't rename what's already a contract" logic that protects the SQL
schema applies here. The translation table below is therefore a
*reading* aid; helix does not rename metrics. Dashboard panel titles
*do* use the engineering vocabulary (and reference the legacy term
inline so navigation stays one-step).

| Prometheus metric (canonical, stays) | Bio framing (legacy) | Engineering meaning |
|---|---|---|
| `helix_chromatin_state_total` | chromatin state distribution | document lifecycle tier (OPEN / WARM / COLD) |
| `helix_harmonic_edges_total` | harmonic links by source | co-activation edges by provenance |
| `helix_ribosome_call_seconds` | ribosome call timing | compressor call latency, by `call_kind` ∈ {pack, rerank, splice, replicate, ...} |
| `helix_ribosome_info` | active ribosome model | active compressor backend + model + cost class |
| `helix_genome_size_genes` | gene count in genome | document count in knowledge store |
| `helix_genome_wal_size_bytes` | — | SQLite WAL file size |
| `helix_genome_signal_seconds` | genome signal timing | per-signal SQLite query latency |
| `helix_genome_checkpoint_blocked_total` | — | WAL checkpoint contention events |
| `helix_pipeline_stage_seconds` | — | per-stage /context handler latency, by `stage` ∈ {classify, extract, express, rerank, splice, assemble} |
| `helix_tier_fired_total` | tier activation | retrieval-signal firing, by `tier` |
| `helix_tier_contribution` | per-tier score contribution | per-signal score magnitude added to gene_scores |
| `helix_cwola_bucket_total` | CWoLa bucket accumulation | A/B unsupervised-partition bucket fill |
| `helix_cwola_f_gap_sq` | f_gap_sq divergence | A/B partition divergence gate (≥ 0.16 = pass) |
| `helix_hub_concentration_ratio` | hub concentration | top-1%-inbound / mean-inbound on co-activation graph |
| `helix_hub_inbound_degree` | hub inbound degree | inbound-degree distribution stats (max/p99/p95/p50/mean) |
| `helix_context_health_status_total` | — | retrieval health classification (aligned/sparse/stale/denatured) |
| `helix_context_ellipticity` | — (CD-spectroscopy term — STAYS) | per-query retrieval shape: geometric mean of coverage × density × freshness |
| `helix_context_cache_outcome_total` | — | /context cache outcome (hit / miss / partial) |
| `helix_pipeline_stage_seconds` (span) | — | also emits a `helix.pipeline.<stage>` span via `pipeline_stage_span()` |
| `helix_genai_client_token_usage` | n/a — new OTel surface | OTel `gen_ai.client.token.usage`, by `gen_ai.token.type` ∈ {input, output, cached, reasoning} |
| `helix_genai_time_to_first_chunk_seconds` | n/a — new OTel surface | OTel `gen_ai.response.time_to_first_chunk` (TTFT, streaming) |
| `helix_genai_cost_usd` | n/a — new surface | per-call USD cost from `helix_context.genai_telemetry.PRICE_TABLE` |
| `helix_genai_finish_reasons_total` | n/a — new OTel surface | OTel `gen_ai.response.finish_reasons` distribution |

**The standardized labels on the new `helix_genai_*` metrics follow the
OTel GenAI semantic-convention attribute namespace:** `gen_ai.provider.name`,
`gen_ai.operation.name` ∈ {chat, text_generation, embeddings, rerank,
classify}, `gen_ai.request.model`, `gen_ai.response.model`,
`gen_ai.token.type`. In Prometheus these come through with `.` replaced
by `_` (so `gen_ai.provider.name` → label `gen_ai_provider_name`).

---

## Dashboard panel-title vocabulary

The Grafana dashboards under `deploy/otel/grafana/dashboards/` use
engineering panel titles with the legacy bio term referenced inline
(e.g. `"Compressor call latency p95 by operation"` + description
`"Legacy term: ribosome call."`). The table below is the bidirectional
index for panel-hunting.

| Engineering panel title (canonical, used in dashboards) | Bio framing (legacy) |
|---|---|
| Compressor backend cost class                  | Ribosome backend |
| Active compressor model                        | Active ribosome model |
| Compressor call latency p95 by operation       | Ribosome call latency by call_kind |
| Compressor call rate by operation              | Ribosome call rate |
| Compressor call latency heatmap                | Ribosome call timing heatmap |
| Document count                                 | Gene count / Genome size |
| Knowledge store (row title)                    | Genome (row title) |
| Lifecycle tier distribution                    | Chromatin state distribution |
| Co-activation edges by provenance              | harmonic_links edges by source |
| Tier activations / minute                      | tier_fired per minute |
| Per-tier contribution score (heatmap)          | Per-tier contribution histogram |
| A/B Cluster Convergence (row title)            | CWoLa Label Clock |
| Bucket accumulation                            | CWoLa bucket accumulation |
| f_gap_sq divergence — (f_A − f_B)²             | (kept verbatim — physics term) |
| Hub concentration ratio (top-1% inbound / mean) | (kept verbatim — graph-theory term) |
| Inbound-degree distribution                    | (kept verbatim — graph-theory term) |
| Genome-signal latency p95 by signal            | (kept — `genome` reads as the SQLite store, signal as the query path) |

The three top-level dashboards that consume the canonical vocabulary:

- **Helix — Operations Overview** (`helix-overview.json`): top-line
  request/latency/cache/pipeline KPIs in engineering names. Default
  landing dashboard.
- **Helix — GenAI** (`helix-genai.json`): the new `helix_genai_*` /
  `gen_ai.*` surface — token usage by direction, TTFT, cost, finish
  reasons, cache hit ratio.
- **Helix — Internals & Research** (`helix-internals.json`): preserved
  bio/research panels (CWoLa, chromatin, harmonic_links, hub
  concentration, tier dynamics) with engineering titles + inline legacy
  references.
- **Helix — Retrieval Quality + HITL** (`helix-retrieval-hitl.json`):
  per-query ellipticity / health status / HITL pause-event signals.
  Uses the technical-term vocabulary (ellipticity, denatured) that is
  already on the "STAYS" list.

---

## Terms that STAY (not biology, not tax)

These are domain-specific technical terms with established meaning
outside biology. Renaming them would lose precision or trade a
small cognitive cost for a bigger one.

- **`SEMA`** — semantic embedding alignment. Helix-coined but not biological;
  stays.
- **`TCM`** — Temporal Context Model (Howard & Kahana, 2002). Established
  psych-literature acronym; stays.
- **`cymatics`** — vibrational pattern math; the `cymatics.py` module
  references real physics, not biology.
- **`SPLADE`** — Sparse Lexical AnD Expansion model. Established sparse-
  retrieval term from the IR community.
- **`CWoLa`** — Classification Without Labels. Established weakly-
  supervised-learning acronym.
- **`ScoreRift`** — proper noun for the audit subsystem.
- **`PWPC`** — proper noun for the joint experiment with Todd's Celestia.
- **`helix`** itself — established product name. Renaming the package
  would be too disruptive for the cognitive-tax payoff.

---

## What gets renamed when

The rename ships in waves so back-compat stays solid throughout.

| Phase | Scope | Status |
|---|---|---|
| **R1** | Rosetta Stone doc + Python alias module + new MCP tool aliases (additive only) | **shipped @ `09d5548` (2026-04-15)** |
| **R2** | Docstring + comment sweep — Python files and `docs/*.md` prose use canonical terms | **shipped @ PR #70 `87fcb68` (2026-05-12)** |
| **R3 Stage A** | Class-def flip + alias inversion (7 schemas + KnowledgeStore + Compressor) | **shipped @ `56fcbed` (PR #88, 2026-05-13)** |
| **R3 Stage B** | Module file moves (`ribosome→compressor`, `genome→knowledge_store`, `codons→fragments`, `replication→persistence`, `hgt→cross_store_import`) + shim modules | **shipped @ `460d824..9e7471f` (PR #88, 2026-05-13)** |
| **R3 Stage C** | Internal method renames (`pack→encode`, `splice→trim`, `replicate→persist`, `re_rank→rerank`, `upsert_gene→upsert_doc`, `query_genes*→query_docs*`, `get_gene→get_doc`, `_express→_retrieve`, cymatics + fragments helpers) | **shipped @ `edc0194..71469ba` (PR #88, 2026-05-13)** |
| **R3 Stage D** | Local-variable + parameter sweep (`for gene in` → `for doc in`, `gene_a/gene_b` → `doc_a/doc_b`, `genes: List[Gene]` → `docs: List[Document]` in module bodies) | **in progress @ PR #89** |
| **R3 Stage E** | This phase-table refresh + R3 design spec stub at `docs/superpowers/specs/2026-05-13-rename-r3-symbol-rename-design.md` | **in progress @ PR #89** |
| **R4** | Soft-deprecate legacy MCP tool names with docstring nudge. No removal. | **deferred** — see #87 |

### What we are explicitly NOT doing

- **No SQL schema rename.** Tables (`genes`, `gene_attribution`,
  `harmonic_links`, etc.) and columns stay. Renaming would force a
  migration on every existing helix instance; the cognitive tax at
  the SQL layer is paid by ~rare readers.
- **No removal of legacy class or tool names.** Only additions and
  docstring nudges. A future major-version cleanup may remove the
  legacy surface; until then both names work and resolve to the same
  underlying objects.
- **No rename of dated handoffs, papers, commit messages, or git
  history.** These are immutable historical artifacts; this Rosetta
  Stone makes them readable without modification.
- **No rename of the `helix-context` package itself.**

---

## How to use this document

**Reading legacy code or docs:** scan the left column for the term you
hit, get the canonical term from the right.

**Writing new code or docs:** use the right column. If a term you need
isn't listed yet, add a row.

**Importing canonical names:**

```python
# After R1 ships:
from helix_context.aliases import Document, KnowledgeStore, Compressor
from helix_context.aliases import DocumentTags, DocumentSignals, LifecycleTier
from helix_context.aliases import DocumentAttribution

# These are pure aliases for the legacy names. Identity holds:
from helix_context.schemas import Gene, PromoterTags
assert Document is Gene
assert DocumentTags is PromoterTags
```

**Calling MCP tools:** both names work. Prefer the canonical for new
client code.

```
helix_document_get(doc_id)   # canonical
helix_gene_get(gene_id)       # legacy alias, same behavior
```

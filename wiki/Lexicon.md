# Lexicon

- Cymatix has two vocabularies. The **canonical one is standard software
  terminology** — document, knowledge store, compressor, fragment, lifecycle
  tier. The other is the molecular-biology metaphor the project was built
  under — gene, genome, ribosome, codon, chromatin.
- This page is the **bidirectional dictionary**. Scan the legacy column to
  read older code, handoffs, and receipts; scan the canonical column when
  writing anything new.
- **Both names work in Python.** The canonical names are the real class
  definitions since R3; the biology names are one-line aliases declared beside
  them. `Document is Gene` is `True` in both directions, with no subclassing
  and no runtime cost.
- **The wire and the SQL schema are still legacy-named on purpose.** JSON keys
  such as `gene_id`, the `<GENE src=…>` blocks inside an assembled window, and
  every SQL table name are contracts with existing consumers. Renaming them
  changes delivered bytes, so it is gated separately —
  [see below](#what-is-still-legacy-named-and-why).
- In-repo, [`docs/ROSETTA.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/ROSETTA.md)
  is now a stub that points here. This page is the living document; add a row
  when a new term surfaces.

## Where the vocabulary came from

- The original architecture was designed around a **molecular-biology
  metaphor**, and the metaphor did real work: a gene as the atomic unit of
  stored knowledge, a genome as the persistent store, a ribosome that reads
  raw text and emits a compressed unit, chromatin as the hot/warm/cold
  accessibility state, splicing as the act of dropping low-value fragments.
  The shape of the pipeline follows from that framing, not the other way
  round.
- The intellectual backstory is **[Agentome](https://mbachaud.substack.com/p/agentome)**,
  a paper by this project's author that set out the biology framing before the
  engine existed. It is the reason the early code reads the way it does, and
  it is worth reading if you want the "why these names" answer rather than the
  "what do they mean" answer this page gives.
- The metaphor also imposes a **cognitive tax**. Every reader — human or LLM —
  has to hold two mental models in parallel, and the second one is not the one
  they arrived with. For a project whose
  [design target](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/DESIGN_TARGET.md)
  is an agent reading its own documentation, that tax is paid on every turn.
- So the **canonical lexicon moved to software terms**, in waves, additively —
  never by deleting the old names. The [phase table](#rename-phases) below
  records what shipped when.
- **The helix → cymatix product rename is a different thing entirely.** That
  was a change of *product name* (package `helix_context` → `cymatix_context`,
  `HELIX_*` → `CYMATIX_*`, `helix.toml` → `cymatix.toml`, CLI `helix` →
  `cymatix`), soft-renamed in 0.8.0 and completed as a **clean break in 0.8.5**
  (2026-07-25): no alias package, no `helix*` console scripts, no `helix_*` MCP
  tools, no env mirror, no config fallback. It says nothing about
  biology-versus-software terms, and the mapping table below is unchanged by
  it. Historical docs — benchmarks, council verdicts, dated plans — keep the
  helix vocabulary deliberately; they are point-in-time records.

## Mapping table

| Biology term (legacy) | Software term (canonical) | Notes |
|---|---|---|
| `Gene` | `Document` | The atomic unit of stored knowledge. `Document` is the real class since R3; `Document is Gene` holds. |
| `genome` / `Genome` | `KnowledgeStore` / `kb` | The persistent SQLite-backed store. |
| `gene_id` | `document_id` / `doc_id` | Hash-derived identifier on the atomic unit. Still `gene_id` on the wire. |
| `genes` (collection / table) | `documents` | The SQL table name stays `genes` — the schema is frozen. |
| `PromoterTags` | `DocumentTags` | The `(domains, entities, intent, summary)` tuple attached to each document. |
| `promoter` (field) | `tags` | |
| `EpigeneticMarkers` | `DocumentSignals` | Access rate, co-activation list, decay score — the behavioural metadata retrieval scores against. |
| `epigenetics` (field) | `signals` | |
| `ChromatinState` | `LifecycleTier` | The hot/warm/cold storage tier enum. |
| `chromatin` (field) | `tier` | |
| `OPEN` / `EUCHROMATIN` / `HETEROCHROMATIN` | `OPEN` / `WARM` / `COLD` | Tier values. The right-hand column is the canonical *reading*; the enum members in `schemas.py` still emit the biology names because they are a SQL and string contract. Numeric `IntEnum` values are unchanged throughout. |
| `codon` / `Codon` | `Fragment` / `Chunk` | The within-document compressed unit. |
| `codons` (field) | `fragments` | |
| `Ribosome` | `Compressor` | The optional small-model layer that encodes raw text into compressed documents. Off the retrieval hot path; disabled by default. |
| `ribosome.pack(...)` | `compressor.encode(...)` | Raw text in, compressed document out. |
| `ribosome.splice(...)` | `compressor.trim(...)` | Drop low-value fragments from a candidate set — the optional Splice stage. See [Pipeline](Pipeline). |
| `ribosome.replicate(...)` | `compressor.persist(...)` | Pack a query+response exchange back into the store. |
| `ribosome.re_rank(...)` | `compressor.rerank(...)` | Re-score retrieval candidates with a small cross-encoder. Default off. |
| `transcription` | `encoding` | The text → compressed-document conversion. |
| `express` / `_express` / `expression` | `retrieve` / `_retrieve` / `retrieval` | The candidate-selection step. |
| `expression_tokens` (budget key) | `retrieval_tokens` | The token budget for retrieved context. Both spellings resolve — see [Configuration](Configuration). |
| `max_genes_per_turn` (budget key) | `max_docs_per_turn` | Per-turn document cap. Both spellings resolve. |
| `replicate` / `replication` | `persist` / `persistence` | Saving query exchanges back into the store as new documents. |
| `harmonic_links` | `coactivation_edges` | Graph edges connecting documents retrieved together. |
| `harmonic_bin_boost` | `random_walk_boost` | The Monte Carlo neighbour-expansion tier. |
| `gene_attribution` | `document_attribution` | The party/participant authorship metadata on each document. |
| `GeneAttribution` | `DocumentAttribution` | |
| `HGT` (horizontal gene transfer) | `cross_store_import` | Importing documents from another cymatix instance. **Forward-pointer:** no code under either name today; `cross_store_import` is the name the feature ships under when it lands. |

## Identity vocabulary

The biology metaphor is not the only translation this project needs. The
identity layer has near-synonyms in active use across design notes and
handoffs. The canonical schema terms — `org`, `party`, `participant`, `agent` —
stay; this table pins their intended meanings.

| Canonical term | Preferred meaning | Equivalents readers will hit | Notes |
|---|---|---|---|
| `org` | trust root / trust domain / tenant | org, team, company, enterprise owner, auth root | The stable owner or authorization root — not a raw credential like an API key. |
| `party` | physical substrate / device | party, host, machine, handset, workstation | `party_id` is the schema term. In local-first federation this is usually a device or execution host. |
| `participant` | human principal | participant, user, human, operator | In authored-ingest attribution, the human on whose behalf work happens. |
| `agent` | software actor | agent, AI persona, software actor, model persona | The runtime AI layer acting for the participant. |

This is a reading layer, not a rename directive. New prose should prefer the
canonical schema name while making the meaning explicit inline — "party
(device)", "agent (software actor)".

## Response and routing types (no biology twin)

These types travel on the wire between cymatix and its callers. They are
*response envelope* vocabulary, not storage vocabulary — no metaphor applies,
and they keep their engineering names everywhere.

| Type | Purpose |
|---|---|
| `ContextWindow` | Full pipeline output — the bytes the agent reads. Carries `expressed_context`, `expressed_gene_ids`, `total_estimated_tokens`, and the `metadata` dict that pipes `know` / `miss` upward. |
| `QueryResult` | Agent-facing projection of `ContextWindow`, adding `verdict` / `next_action` / `decision_reason`. The shape `cymatix query --json` emits. |
| `ContextPacket` | Freshness-labeled agent-safe bundle: `verified[]`, `stale_risk[]`, `refresh_targets[]`, plus `coordinate_confidence` and `file_coverage`. |
| `ContextItem` | One evidence row inside a packet — `gene_id`, `title`, `content`, `relevance_score`, `live_truth_score`, `status`. |
| `RefreshTarget` | One reread directive — `target_kind`, `source_id`, `reason`, `priority`. |
| `KnowBlock` | The "you may answer from this evidence" verdict — `found`, `confidence`, `gene_id_match`, `soft_stale`. Mutually exclusive with `MissBlock`. |
| `MissBlock` | The "do **not** answer from the knowledge store" verdict — `reason`, `escalate_to[]`, `refresh_targets[]`, `do_not_answer_from_genome: true`. |
| `ContextHealth` | Per-retrieval check-engine light — `ellipticity`, `coverage`, `density`, freshness signals, `coordinate_crispness`, `status`. Logged, not on the wire. |
| `IngestResult` | One-shot ingest projection — `gene_ids[]`, `chunks`, `bytes_written`. |
| `StatsResult` | One-shot stats projection used by `cymatix diag corpus`. |

Contract semantics for `know` / `miss` and the packet labels live on
[Agent Contract](Agent-Contract); the field-by-field JSON is on
[HTTP API](HTTP-API).

## Metric and label names are a contract

- Prometheus metric names and label values are queried by Grafana panels,
  alert rules, and ad-hoc PromQL. The same "don't rename what is already a
  contract" logic that freezes the SQL schema applies to them: **cymatix does
  not rename metrics.** `cymatix_chromatin_state_total`,
  `cymatix_harmonic_edges_total`, `cymatix_ribosome_call_seconds`,
  `cymatix_genome_size_genes` and the rest keep their names permanently.
- Dashboard **panel titles** do use the engineering vocabulary, with the legacy
  term referenced inline so panel-hunting stays one step ("Compressor call
  latency p95 by operation", description "Legacy term: ribosome call").
- The full metric-name-to-meaning table, the panel-title index, and the OTel
  `gen_ai.*` attribute namespace are on [Observability](Observability) — that
  page is the single home for the metric surface, so it is not duplicated here.

## Terms that stay

Domain-specific technical terms with established meaning outside biology.
Renaming them would lose precision or trade a small cognitive cost for a
bigger one.

- **`SEMA`** — semantic embedding alignment. Cymatix-coined, not biological.
- **`TCM`** — Temporal Context Model (Howard & Kahana, 2002). An established
  psychology-literature acronym.
- **`cymatics`** — vibrational pattern math; `scoring/cymatics.py` references
  physics, not biology. (What the stage actually does, and the honesty caveat
  about it, is on [Retrieval Dimensions](Retrieval-Dimensions).)
- **`SPLADE`** — Sparse Lexical AnD Expansion model, from the IR community.
- **`CWoLa`** — Classification Without Labels, from weakly-supervised learning.
- **`ellipticity`** and **`denatured`** — CD-spectroscopy and physics terms
  that happen to sit next to the biology ones. Not renamed.
- **`ScoreRift`** — proper noun for the audit subsystem.
- **`PWPC`** — proper noun for a joint experiment.
- **`cymatix`** itself — the product name.

## What is still legacy-named, and why

Two surfaces were deliberately left alone. Both are contracts with something
outside the repository.

**The wire surface** — tracked as
[#417](https://github.com/mbachaud/Cymatix-Context/issues/417), not taken in
0.9.1:

- The `<GENE src=…>` blocks that wrap each spliced fragment in an assembled
  context window.
- The `[gene=` legibility headers emitted during assembly.
- Decoder prompt text that names `gene` / `genome` / `chromatin` when
  instructing the compressor model.
- JSON keys returned over HTTP, CLI, and MCP — `expressed_gene_ids`,
  `gene_id_match`, `do_not_answer_from_genome`, `gene_id`.
- `/stats` response keys — `open` / `euchromatin` / `heterochromatin`.

Renaming any of these **changes delivered bytes, not just a label**. The
closest precedent is the 0.9.0 `neutralize_control_tags` flip, which touched a
narrow literal-string case and was still gated on a 141/141
byte-identical-window receipt. A wire rename is a strictly larger surface, so
it needs its own byte-level A/B gate at full-scale — v1.0-scale work, not a
patch release.

**The SQL schema** — frozen, and never part of any tier of this migration.
Tables and columns (`genes`, `codons`, `gene_attribution`, `harmonic_links`,
`chromatin_state`) stay as they are. Renaming would force a migration on every
existing store, and the cognitive tax at the SQL layer is paid by comparatively
few readers. The default store file on disk is likewise still
`genomes/main/genome.db`, and code blocks across this wiki reproduce it
verbatim rather than faking a rename the code does not have.

## Rename phases

| Phase | Scope | Status |
|---|---|---|
| **R1** | This mapping doc + the Python alias module + additive MCP tool aliases | **shipped** `09d5548` (2026-04-15) |
| **R2** | Docstring and comment sweep — Python files and `docs/*.md` prose use canonical terms | **shipped** PR #70 `87fcb68` (2026-05-12) |
| **R3 A** | Class-def flip + alias inversion (7 schemas + `KnowledgeStore` + `Compressor`) | **shipped** PR #88 (2026-05-13) |
| **R3 B** | Module file moves (`ribosome→compressor`, `genome→knowledge_store`, `codons→fragments`, `replication→persistence`, `hgt→cross_store_import`) + back-compat shim modules | **shipped** PR #88 (2026-05-13) |
| **R3 C** | Internal method renames (`pack→encode`, `splice→trim`, `replicate→persist`, `re_rank→rerank`, `upsert_gene→upsert_doc`, `query_genes*→query_docs*`, `get_gene→get_doc`, `_express→_retrieve`) | **shipped** PR #88 (2026-05-13) |
| **R3 D** | Local-variable and parameter sweep in module bodies | **shipped** PR #89 (2026-05-13) |
| **R3 E** | Phase-table refresh + R3 design spec | **shipped** PR #89 (2026-05-13) |
| **R4** | Deprecation nudges on the legacy MCP tool names, canonical names in the default tool set, and additive config / CLI / HTTP / env aliases. No removals. | **shipped in 0.9.1** — closes #87 |

R4 was deferred for three releases before it landed. It ships as the **Tier 2**
half of the 0.9.1 lexicon pass; the three tiers are:

- **Tier 1** — documentation prose. Repo docs use software terms; dated
  records keep their original vocabulary untouched.
- **Tier 2** — additive aliases on every operator-facing surface. Shipped in
  0.9.1, below.
- **Tier 3** — the wire surface. Deferred to
  [#417](https://github.com/mbachaud/Cymatix-Context/issues/417).

### The Tier 2 aliases

Every legacy spelling keeps working, unchanged. Nothing is deprecated in the
sense of "will stop working".

| Canonical | Legacy | Surface |
|---|---|---|
| `[compressor]` | `[ribosome]` | `cymatix.toml` section |
| `[knowledge_store]` | `[genome]` | `cymatix.toml` section |
| `[budget] retrieval_tokens` | `expression_tokens` | config key |
| `[budget] max_docs_per_turn` | `max_genes_per_turn` | config key |
| `CYMATIX_STORE_PATH` | `CYMATIX_GENOME_PATH` | environment variable |
| `cymatix document get` / `document preview` | `cymatix gene get` / `gene preview` | CLI subcommand |
| `--max-docs` | `--max-genes` | CLI flag |
| `GET /documents/{id}` | `GET /genes/{id}` | HTTP route |
| `cymatix_document_get` / `_query` / `_preview` / `_fingerprint` / `_neighbors` | `cymatix_gene_get`, `cymatix_context`, `cymatix_splice_preview`, `cymatix_fingerprint`, `cymatix_neighbors` | MCP tool |

**Collision rule: the legacy name wins, and a `WARNING` names both.** If a
config carries both `[compressor]` and `[ribosome]`, the `[ribosome]` block is
the one that binds. The rule is deliberately conservative — an operator with
both spellings in play gets the behavior their existing file already had, not
a silent change.

**The CLI is the one exception.** `--max-docs` and `--max-genes` share an
argparse destination, so passing both is plain last-wins, not legacy-wins.

## Using the canonical names

```python
# One import point for the canonical surface.
from cymatix_context.aliases import (
    Document, KnowledgeStore, Compressor,
    DocumentTags, DocumentSignals, LifecycleTier, DocumentAttribution,
)

# The legacy names are aliases of the same objects. Identity holds both ways.
from cymatix_context.schemas import Gene, PromoterTags
assert Document is Gene
assert DocumentTags is PromoterTags
assert Document.__name__ == "Document"   # canonical is the real class
```

```bash
cymatix document get <id> --json     # canonical
cymatix gene get <id> --json         # legacy alias, identical output
```

```
cymatix_document_get(document_id)    # canonical MCP tool
cymatix_gene_get(gene_id)            # legacy alias, same behavior
```

Pydantic field names (`gene_id`, `promoter`, `epigenetics`, `chromatin`,
`codons`) and the SQL contracts are unchanged by any of the above.

## Go deeper

- [`cymatix_context/aliases.py`](https://github.com/mbachaud/Cymatix-Context/blob/master/cymatix_context/aliases.py) — the single canonical import point, plus the `_RENAME_LOG` provenance table
- [`cymatix_context/schemas.py`](https://github.com/mbachaud/Cymatix-Context/blob/master/cymatix_context/schemas.py) — where the canonical classes and their one-line legacy aliases are declared side by side
- [`docs/ROSETTA.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/ROSETTA.md) — the in-repo stub that points here
- [Agentome](https://mbachaud.substack.com/p/agentome) — the paper the biology framing came from
- [#417](https://github.com/mbachaud/Cymatix-Context/issues/417) — the Tier 3 wire-surface remainder, with the gate it has to clear
- Next: [Configuration](Configuration) · [Observability](Observability) · [Architecture Map](Architecture-Map) · [Roadmap and Releases](Roadmap-and-Releases)

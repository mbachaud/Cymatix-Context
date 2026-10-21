# Architecture Map

- One import package, `cymatix_context`, organized into **17 sub-packages**
  plus a handful of top-level orchestration modules.
- One store per server: a **single SQLite file** in WAL mode with an FTS5
  index. Back it up by copying the file; delete it to start fresh.
- The server on `127.0.0.1:11437` and the optional supervisor on `:11438`. The
  supervisor can also run extra servers, one per `[[lanes]]` entry, each on its
  own port and store (0.11.0). An optional encoder daemon takes `:11440` when
  you route encoding out of process.
- This page is the *where things live* map. What the code does per turn is
  [Pipeline](Pipeline); what each retrieval signal is, is
  [Retrieval Dimensions](Retrieval-Dimensions).

## Package layout

| Package | Purpose |
|---|---|
| `adapters/` | Cache, data-access layer, external retriever protocol |
| `backends/` | Compressor (legacy: ribosome), BGE-M3 codec, cross-encoder rerank, DeBERTa, NLI, SEMA codec, SPLADE |
| `cli/` | The `cymatix` CLI: query, packet, refresh-targets, ingest, document, neighbors, diag, config, status, mcp, compare, trace, serve |
| `encoding/` | Chunking, fragment encoding, legibility headers, Headroom bridge |
| `identity/` | CWoLa logger, session delivery, registry, provenance, claims |
| `okf/` | Open Knowledge Format (OKF v0.1) bundle reader, canonical digest, ingest adapter |
| `pipeline/` | Tier logic and pipeline-stage helpers |
| `retrieval/` | Expand, freshness gate, RRF/additive fusion, post-fusion rerank combinators, PLR, intent router, SR, seeded edges, COVER walk, query classifier, tie-break |
| `scoring/` | Cymatics, know-calibration, know-decision, know lanes model, ray-trace, TCM, blending |
| `server/` | FastAPI app factory plus route modules (context, ingest, registry, admin, sync, trace) |
| `storage/` | DDL, indexes, co-activation graph |
| `sync/` | In-process delta sync of tracked folders (`[sync]`, off by default): tracker and worker |
| `telemetry/` | OpenTelemetry metrics and histogram instrumentation, plus the opt-in packet trace (writer, eval checks, label taxonomy, export) |
| `vault/` | Obsidian vault export — read-only diagnostic traces |
| `launcher/` | System-tray supervisor for the backend, extra lanes, and the telemetry stack |
| `mcp/` | The MCP tool surface for Claude Code, Codex, Gemini CLI, and Antigravity |
| `integrations/` | ScoreRift bridge, MCP host profiles |

Top-level orchestration modules:

- `context_manager.py` — the pipeline orchestrator; every stage on
  [Pipeline](Pipeline) is dispatched from here.
- `knowledge_store.py` — SQLite DDL plus the retrieval query itself. The
  fusion, tier weighting, and gating all live in this module.
- `config.py` — the `cymatix.toml` loader, alias resolution, and env-var
  precedence. `schemas.py` — the Pydantic request and response models.
- `context_packet.py` — the agent-safe packet builder behind
  `/context/packet`. `wire.py` — the opt-in canonical wire projection.
- `lanes.py` — `[[lanes]]` resolution and validation. `store_settings.py` —
  the per-store sidecar (auto-sync folders, Freeze).
- `tagger.py` — the CPU ingest tagger (spaCy NER + EntityRuler + regex). The
  chunker and fragment encoder live in `encoding/fragments.py`.
- `genome.py`, `ribosome.py`, `codons.py`, `replication.py`, `hgt.py`, and
  `mcp_server.py` are **shims** that re-export from the modules above. They
  are the domain metaphor, not a second implementation. `server` needs no
  shim: the `server/` package keeps the old `cymatix_context.server` import
  path working. The `helix_context` alias package was removed in 0.8.5 —
  import `cymatix_context`. Full vocabulary crosswalk: [Lexicon](Lexicon).

## The knowledge store

Everything is one file. The table names below are the literal SQL identifiers,
which keep their legacy spellings; the software meaning is on the left.

| What it holds | Table |
|---|---|
| Documents — the primary node | `genes` |
| Full-text index over document content | `genes_fts` (FTS5 virtual table) + `genes_fts_vocab` |
| Tag index — the exact/prefix lookup tier | `promoter_index` |
| Compound path-and-key lookup (Tier 0, opt-in) | `path_key_index` |
| Sparse expansion terms (opt-in) | `splade_terms`, `splade_term_df` |
| Spectral co-activation edges | `harmonic_links` |
| Shared-entity edges | `entity_graph` |
| Semantic relations between documents | `gene_relations` |
| Symbol definitions for code chunks | `symbol_defs` |
| Cross-bundle links from OKF ingests | `okf_links` |
| Attribution — who authored which document | `gene_attribution` |
| Identity registry | `orgs`, `parties`, `participants`, `agents` |
| What each session has already been delivered | `session_delivery_log` |
| Human-in-the-loop pause events | `hitl_events` |
| Store health snapshots, calibration rows, CWoLa traces | `health_log`, `genome_calibration`, `cwola_log` |
| Delta-sync tracking (created on first use when `[sync]` is on) | `sync_tracked`, `sync_lock` |

### A document row

Each document carries its content plus the derived structures the retrieval
signals read:

- **Content** — the full original text, and a dense summary of it.
- **Weighted term labels** and a **tag block** of domains and entities, both
  produced by the ingest tagger. These feed D1 and D2.
- **Extracted key-value pairs**, which feed the path-key tier and the
  health/ellipticity diagnostics.
- **A 20-dimension SEMA vector** and, when the opt-in dense path is enabled, a
  1024-dimension BGE-M3 vector. Both are NULL on documents ingested while their
  ingest-time knob was off — see [Configuration](Configuration) for the
  backfill scripts.
- **Access metadata** — a ring buffer of recent access timestamps, a decay
  score, and the per-document co-activation list. This is D4.
- **Provenance**, auto-populated at ingest from the source path: source kind
  (code / config / doc / log / db), a volatility class that sets the freshness
  half-life, and observed-at / last-verified-at timestamps. These are what the
  freshness gate and the `/context/packet` weighing layer read.
- **Lineage** — a monotonic version and a pointer to the document this one
  supersedes.

### Lifecycle tiers

Documents sit in one of three accessibility tiers. **Content is preserved
across all three** — the tier changes what gets queried, not what is kept.

| Tier | Column value | Legacy name | Default retrieval |
|---|---|---|---|
| Hot | `chromatin = 0` | OPEN | Always queried |
| Warm | `chromatin = 1` | EUCHROMATIN | Queried with hot |
| Cold | `chromatin = 2` | HETEROCHROMATIN | Excluded; opt in with `[context] cold_tier_enabled` or a per-call `include_cold` |

A density gate assigns the tier at ingest, and the working-set access rate can
pull a document back toward hot as it gets used. Delta sync's tombstones are
cold-tier moves too: a superseded chunk keeps its content and drops out of
default retrieval.

### Edges between documents

| Edge | Where it lives | Meaning |
|---|---|---|
| Co-activation | Per-document access metadata | "Retrieved together in the same query" |
| Harmonic link | `harmonic_links` | Spectral similarity from the cymatics spectrum, weighted |
| Entity link | `entity_graph` | The two documents share a named entity |
| Supersession | Document lineage column | Newer version of the same content |
| Relation | `gene_relations` | `entails` / `contradicts` / `neutral` |

**Ingest-cost knob — shipped off in 0.9.1
([#412](https://github.com/mbachaud/Cymatix-Context/pull/412)), on by default in 0.9.2
([#425](https://github.com/mbachaud/Cymatix-Context/pull/425)).** On
entity-hub-heavy corpora the auto-linker sweeps every posting row of every
probe entity before grouping, which made entity auto-linking 89.5% of writer
wall time on the EnronQA bed. `[ingestion] entity_autolink_hub_cutoff`
(**default `200` since 0.9.2; `0` = off = legacy, byte-identical**) drops
entities whose posting count exceeds the bound from the probe set. The
read-only replay receipt at cutoff 200: **0.53% of distinct entities hold
56.9% of all postings**; mean per-link cost falls **210.5 ms → 0.62 ms
(~340×)**. **The edge delta is why the knob shipped off first** — the same
replay lost **3,143 of 4,842** COVER relation edges (gaining 1,168). Those
edges are default-inert at query time, but a substantial edge-set change is
not something to ship silently, so the flip waited for two receipts: a paired
retrieval A/B on EnronQA (0/500 delivered flips, ledger row
`2026-08-30-entity-hub-cutoff-retrieval-ab`) and a post-tagger-v2 re-measure
showing the hubs persist (0.22% of entities hold 30.1% of postings at cutoff
200; per-link 4.15 → 0.42 ms at 85k, ledger row
`2026-08-31-entity-hub-cutoff-reprobe-85k`). Opt out with
`entity_autolink_hub_cutoff = 0` — see [Configuration](Configuration).

**0.9.1 tag hygiene
([#413](https://github.com/mbachaud/Cymatix-Context/pull/413)).** The ingest
tagger previously extracted email and MIME header plumbing as entities, with
spans crossing newlines into multi-line garbage. Those hubs are now rejected.
Because tags are part of a bed's content digest, this ships as
`TAGGER_VERSION = 2` and is recorded per bed: **beds built with tagger v1 are
not tag-comparable with v2 beds**, by design. A 2,000-file before/after receipt
measured entity-edge rows −16.7% and multi-line entities 2,818 → 0, with
document ids unchanged. The comparability rule is in
[`BASELINES.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/benchmarks/BASELINES.md).

## Session registry

Multiple agent sessions can share one server, and the registry is how they see
each other and tag what they write. It adds three concepts on top of the store:
a **party** (the stable principal — a human, tenant, or device context), a
**participant** (a live actor under that party, such as one editor panel or one
sub-agent), and an **authored-documents** view that returns a handle's most
recent documents in reverse-chronological order with no retrieval scoring at
all — a deliberate bypass for short broadcasts that BM25 would otherwise bury
under larger documents. Participants heartbeat and age through
active → idle → stale → gone; attribution survives that turnover because the
party, not the participant, is the durable identity. Everything is additive: an
ingest with an unknown participant still succeeds, just untagged. **There is no
authentication** — parties are self-asserted on registration, which is
acceptable for the single-user localhost case the layer was built for and is
not acceptable beyond it. Endpoints and schema:
[`docs/architecture/SESSION_REGISTRY.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/architecture/SESSION_REGISTRY.md);
the identity vocabulary is on [Lexicon](Lexicon).

## Launcher

The launcher is a small supervisor process that answers "who starts the
server". It runs its own FastAPI app on `:11438`, spawns and monitors the
backend on `:11437`, and renders a poll-driven dashboard of what is actually
running — parties and participants from the registry, loaded models, active
subsystems, store size and compression ratio, token counters. It never imports
the server package; it talks to it over HTTP only, so it stays light and keeps
working while the backend is stopped. It **adopts** an already-running backend
it finds on the port instead of failing, which means starting the server by
hand and then opening the launcher does the sensible thing. Optional modes: a
native window, a system-tray icon (the tray extra is LGPL and opt-in, so the
wheel itself stays Apache-2.0-clean), and a print-only `install-service`
recipe that deliberately never runs `systemctl enable` or its equivalents for
you. Architecture and failure-mode table:
[`docs/architecture/LAUNCHER.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/architecture/LAUNCHER.md).

Since 0.11.0 the launcher also supervises **lanes**: each `[[lanes]]` entry is
one more server on its own port and its own store directory, and
`genome_source = "snapshot:<lane>"` serves a consistent copy of another lane's
store so a newer engine build never writes to the live one. The experimental
desktop app (`desktop/`, Electron) runs the launcher as a headless sidecar.
Lane keys: [Configuration](Configuration); `cymatix compare` and
`cymatix mcp install --lane`: [CLI](CLI).

## Storage notes

- **One file, WAL mode.** Default path `genomes/main/genome.db`, relative to
  the run directory. WAL means readers do not block the writer, so `cp` while
  the server is running is a safe backup.
- **FTS5 for lexical retrieval**, over an index that carries its own copy of
  the content. The 2026-08-06 storage audit measured 8.67 GB of a 49.4 GB bed
  as that redundant shadow copy and called an external-content rebuild a
  signal-neutral trim — a dated finding on that bed, and the rebuild it
  recommends has not shipped.
- **Maintenance is explicit**, not magic: `POST /admin/vacuum` reclaims pages,
  `/admin/compact` runs a compaction pass, `/admin/checkpoint` forces a WAL
  checkpoint, and `/admin/swap-db` hot-swaps the file without a restart. Scope
  `/admin/swap-db` with `[server] swap_db_roots` and gate the mutating surface
  with `[server] admin_token` — see [Configuration](Configuration).
- **Per-store settings (0.11.2).** An optional sidecar
  `<store>.db.cymatix.json` beside each store holds its auto-sync folders and a
  **Freeze** flag. A frozen store opens read-only: documents, tiers, links and
  health records are not written (including by queries, compaction and
  tombstoning), its sync is off, and `/ingest` and `/consolidate` answer 409.
  Session, delivery-log and query-log tables still write, so the file is not
  byte-identical after use. With no sidecar nothing changes.
- **Sharded scale gap.** Everything above describes the *unsharded* engine,
  which is where the published 829,131-fragment operating point was measured.
  The **sharded** path — a corpus split across shard databases — currently
  trails unsharded by **~31pp recall@10 and ~30pp MRR** on the xl bed, because
  dense recall and co-activation are not yet at parity across shards
  ([#275](https://github.com/mbachaud/Cymatix-Context/issues/275), closed as
  deferred to the
  [#377](https://github.com/mbachaud/Cymatix-Context/issues/377) ledger, gap
  not fixed). Prefer the unsharded engine for accuracy-sensitive corpora until
  that closes.
- **Ports.** `11437` server, `11438` launcher, `11440` optional encoder daemon
  (`[encoder_daemon] url`, empty by default = every seam encodes in-process).
  `11439` is the bench port and is deliberately distinct
  ([#376](https://github.com/mbachaud/Cymatix-Context/issues/376)). Extra
  lanes take the ports you declare; validation rejects duplicates and the
  reserved `11438`, `11440` and `8787` (Headroom).

## Go deeper

- [`docs/architecture/KNOWLEDGE_GRAPH.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/architecture/KNOWLEDGE_GRAPH.md) — the store as a graph: node anatomy, edge types, a worked traversal
- [`docs/architecture/SESSION_REGISTRY.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/architecture/SESSION_REGISTRY.md) — party/participant schema, endpoints, TTL and sweep, the deferred trust model
- [`docs/architecture/LAUNCHER.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/architecture/LAUNCHER.md) — supervisor lifecycle, adoption, tray and service modes
- [`docs/architecture/FEDERATION_LOCAL.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/architecture/FEDERATION_LOCAL.md) — the four-layer attribution model every ingest writes through
- [`docs/architecture/PIPELINE_LANES.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/architecture/PIPELINE_LANES.md) — which component writes which table, and which tables each query path reads
- Next: [Pipeline](Pipeline) · [Retrieval Dimensions](Retrieval-Dimensions) · [Configuration](Configuration) · [Lexicon](Lexicon)

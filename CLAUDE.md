# Cymatix Context

Knowledge-store-based context compression for local LLMs. Version: see `pyproject.toml` (released tags are on `master`; `beta` may be ahead). (Clean break in 0.8.5: renamed from helix-context in July 2026; the old helix CLI/env/import names, `helix.toml`, and `helix_*` MCP tools are removed — cymatix only).

## Quick Start

```bash
# Install
pip install cymatix-context

# Ingest content
cymatix ingest path/to/your/docs/ --recursive

# Query the store
cymatix query "what does the splice step do?"

# Inspect corpus state
cymatix diag corpus
cymatix status

# Start the FastAPI proxy (IDE integrations, MCP hosts)
cymatix-server
# or
python -m uvicorn cymatix_context._asgi:app --host 127.0.0.1 --port 11437
```

Full CLI reference: `docs/clients/cli.md`.

Cross-host MCP and Agent Skill guidance: [`docs/clients/cymatix-context.md`](docs/clients/cymatix-context.md),
with native guides for [Claude Code](docs/clients/claude-code.md),
[Codex](docs/clients/codex.md), [Gemini CLI](docs/clients/gemini-cli.md),
and [Antigravity](docs/clients/antigravity.md). Direct MCP needs neither the
model proxy nor the tray; a healthy headless server is sufficient.

## How It Works

A transparent OpenAI-compatible proxy that intercepts LLM requests and injects compressed context from a persistent SQLite knowledge store.

**7-stage pipeline per turn:**

0. **Classify** — rule-based query classifier picks decoder mode + assembly cap (no model call)
1. **Extract** — heuristic keyword extraction from query (no model call)
2. **Retrieve** — FTS5 lexical + tag lookup + synonym expansion + co-activation + cymatics 256-bin spectrum scoring, all algorithmic with no model inference, fused by Reciprocal Rank Fusion (`[retrieval] fusion_mode = "rrf"`). BGE-M3 dense recall and SPLADE sparse expansion exist but are **off by default** since 2026-08 (receipts showed them null or negative; see `docs/benchmarks/2026-08-14-encoder-isolation-scale-curve.md`)
3. **Re-rank** — CPU model scores candidates by relevance (optional, off by default)
4. **Splice** — CPU model compresses each candidate, keeping high-value fragments (batched)
5. **Assemble** — join spliced parts, enforce token budget, attach per-document legibility headers (fired tiers, confidence marker, compression ratio), elide already-delivered documents via session working-set register
6. **Persist** — pack query+response exchange into knowledge store (background)

Additionally, a **freshness gate** (Stage 7) runs during assembly to demote stale, cold, or superseded documents.

The pipeline emits a **know/miss agent contract** on `/context/packet`: every response carries `know { found, confidence, gene_id_match }` or `miss { reason }` so downstream agents can calibrate trust without guessing.

## Package Structure (post-PR #90)

After the repo restructure, `cymatix_context/` is organized into 17 sub-packages plus a handful of top-level orchestration modules. (The old `helix_context` alias package was removed in 0.8.5.)

| Package | Purpose |
|---------|---------|
| `adapters/` | Cache, DAL, retriever abstractions |
| `backends/` | Compressor (formerly ribosome), DeBERTa, NLI, SEMA codec, SPLADE |
| `cli/` | `cymatix` CLI: query, packet, ingest, gene, neighbors, diag, config, status |
| `encoding/` | Chunking, fragment encoding, legibility headers, Headroom bridge |
| `identity/` | CWoLa logger, session delivery, registry, provenance, claims |
| `okf/` | Open Knowledge Format (OKF v0.1) bundle reader, canonical digest, ingest adapter (`cymatix ingest --okf`) |
| `pipeline/` | Tier logic, pipeline-stage helpers |
| `retrieval/` | Expand, freshness gate, RRF/additive fusion, PLR, intent router, SR, seeded edges, query classifier, tie-break |
| `scoring/` | Cymatics, know-calibration, know-decision, ray-trace, TCM, blending |
| `server/` | FastAPI app factory + route modules (context, ingest, registry, admin) |
| `storage/` | DDL, indexes, co-activation graph |
| `sync/` | In-process delta sync of tracked folders (`[sync]`): tracker + worker, re-ingest changed files, tombstone stale and deleted ones |
| `telemetry/` | OTel metrics, histogram instrumentation |
| `vault/` | Obsidian vault export (read-only diagnostic traces) |
| `launcher/` | System-tray supervisor (backend + telemetry stack) |
| `mcp/` | MCP tool surface for Claude Code, Codex, Gemini CLI, and Antigravity |
| `integrations/` | ScoreRift bridge |

Top-level modules: `context_manager.py` (pipeline orchestrator), `config.py` (TOML loader), `schemas.py` (Pydantic models), `knowledge_store.py` (SQLite DDL + retrieval), `codons.py` (chunker + encoder), `tagger.py` (CPU ingest tagger).

**Biology-lexicon shims:** `genome.py`, `ribosome.py`, `replication.py`, `hgt.py`, `mcp_server.py` re-export from their new locations (`server` is the `server/` package itself, whose `__init__.py` keeps the old `cymatix_context.server` import names) (the domain metaphor, not the helix brand). See the wiki Lexicon page (docs/ROSETTA.md is a stub) for the full biology-to-software lexicon. The `helix_context` alias package was removed in 0.8.5 — import `cymatix_context`.

## Branches and sibling repos

- **`beta` is the default and integration branch.** Open PRs against `beta`. `master` holds released code only (`release/vX.Y.Z` and `hotfix/*` merge there). "Latest build" means `origin/beta`. See `CONTRIBUTING.md` and `docs/RELEASING.md`.
- **Receipts are not in this repo.** Benchmark receipt trees under `benchmarks/dogfood/receipts/` were untracked in `e034010` and live in **`mbachaud/cymatix-receipts`** (`mirror/` = exported tracked receipts, `untracked/` = receipts that were never committed here; rebuild its `index.json` with its `scripts/build_index.py`). A receipt path cited in a doc resolves there, or via `git show <old-sha>:<path>` here.
- **`mbachaud/cymatix-website`** serves cymatixcontext.com. Its `public/wiki/` is generated from this repo's `wiki/` by `scripts/build_wiki_site.py`. Edit the wiki here, never there.
- **`addiplus/cc-exchange`** is the shared Joe/Max bench exchange (run requests, replies, results). It lands on `main` via `bin/exchange-push.sh`; read its `CLAUDE.md` before writing.
- **Where things are:** `docs/README.md` maps the docs tree, `benchmarks/README.md` maps the harnesses, and `docs/benchmarks/BASELINES.md` holds the current baselines. The `wiki/` pages are the user-facing reference and are kept current; dated docs under `docs/` are point-in-time records.

## Configuration

All config lives in `cymatix.toml` (the `helix.toml` fallback was removed in 0.8.5). This table lists keys and shipped defaults only. Why each default is what it is (receipts, flip dates, PRs) is in `wiki/Configuration.md`; `tests/test_config_reference_sync.py` checks every key named here exists. Sections:

| Section | Key settings |
|---------|-------------|
| `[ribosome]` | model, backend (`"none"` default / `"litellm"` / `"deberta"` — only litellm and deberta are honored when enabled; any other value, including legacy `"ollama"` and `"claude"`, dispatches the no-op DisabledBackend), timeout, query_expansion_enabled |
| `[hardware]` | device auto-detection (CUDA, MPS, ROCm, CPU) |
| `[budget]` | expression_tokens (7000), max_genes_per_turn, splice_aggressiveness, decoder_mode, legibility_enabled, session_delivery_enabled, min_delivered_docs (**12** since 2026-08-30; 0 = legacy eviction-only trim) |
| `[session]` | synthetic_session_enabled, synthetic_session_window_s, default_party_id |
| `[genome]` | path (`genomes/main/genome.db`), compact_interval, cold_start_threshold, replicas, cover_index_autobuild (**true**: background-builds the cover index on a 50k+ row store, holds the write lock ~38 s at 947k; env `CYMATIX_COVER_INDEX_AUTOBUILD=0` disables) |
| `[server]` | host, port, upstream |
| `[lanes]` | `[[lanes]]` array: name, port, genome_path, role, python_executable, engine_path, config_path, autostart, genome_source (`"snapshot:<lane>"`). Each entry is one extra launcher-supervised server; diff lanes with `cymatix compare`; a chat selects a lane via `cymatix mcp install --lane NAME` |
| `[telemetry]` | enabled (false), endpoint (`"localhost:4317"`), insecure, sampler_ratio, redact_query, logs_enabled, logs_level. `CYMATIX_OTEL_*` env wins over toml |
| `[trace]` | Opt-in packet trace (#493, `docs/specs/packet-trace-v0.md`): enabled (**false**), level (`"metadata"` / `"full"` / `"off"`), path, hash_chain, sampler_ratio, rotate_bytes. A packet id is returned on every `/context` response even when off; read with `GET /trace/{id}` |
| `[headroom]` | route_upstream toggle for Headroom proxy integration |
| `[encoder_daemon]` | url (`""` = off, encode in-process; `CYMATIX_ENCODER_URL` env wins; daemon: `python -m cymatix_context.encoder_daemon`, port 11440) |
| `[ingestion]` | backend (`"cpu"` / `"ollama"` / `"hybrid"`), splade_enabled (**false**), rerank_model, entity_graph, sema_embed_on_ingest (**false**; backfill with `scripts/backfill_sema.py`), dense_embed_on_ingest (**false**; backfill with `scripts/backfill_bgem3_v2.py`), entity_autolink_hub_cutoff (**200**; 0 = legacy linking; bench builder reads `CYMATIX_BFM_HUB_CUTOFF`) |
| `[context]` | cold_tier_enabled, cold_tier_k, cold_tier_min_cosine, freshness_basis (`"clock"` default = legacy age-since-verification decay, which turns a static store all-stale ~15 days after ingest / `"source"` = verify on the source: unchanged on disk = verified, changed = needs refresh, not on this disk = unknown rather than stale; opt-in, #482) |
| `[cymatics]` | enabled, distance_metric (`"cosine"` / `"w1"`), harmonic_links |
| `[classifier]` | Rule-based query classifier: `enabled` toggle only; per-class caps/decoder hints are code constants pending #205 |
| `[retrieval]` | fusion_mode (`"rrf"` / `"additive"` legacy), rrf_k (**20**), rerank_combinator_by_class (all five classes → eps_band), dense_embedding_enabled (**false**), pki_enabled (**false**), sr_enabled, sr_gamma, ray_trace_theta, seeded_edges_enabled, cover_walk_enabled (**false**), fts5_candidate_depth (0 = auto), blend_mode (`"scale_relative"`; `"legacy"` deprecated) |
| `[plr]` | Piecewise linear reranker: enabled, model_path |
| `[system_one]` | EXPERIMENTAL external packet judge, **off**: enabled, url, model, api_key_env (`TYPESAFE_API_KEY`), timeout_s, min_interval_s, know_threshold, rerank_items, max_item_chars. On sends query and item text to the provider (`docs/benchmarks/2026-10-23-jev-system-one-packet-judge.md`) |
| `[know]` | KnowBlock confidence logistic: emit_floor, betas, s_ref, g_ref, stale_after_days (+ calibrated_at / calibrated_on_n written by scripts/calibrate_know_confidence.py) |
| `[sync]` | enabled (default false), roots, include, exclude, interval_s, max_delete_fraction, max_files_per_pass, max_file_bytes. In-process delta sync of tracked folders: changed files are re-ingested and their stale chunks tombstoned (soft) and so are the chunks of deleted files; a missing root or a mass delete touches nothing. Supersedes `[mem_sync]` for repo folders |
| `[mem_sync]` | Auto-memory-to-cymatix sync: watch_dirs, sync_interval_s |
| `[synonyms]` | Lightweight query expansion (e.g., "cache" -> ["redis", "ttl", "invalidation"]) |
| `[abstain]` | Abstention thresholds for low-confidence responses |

## HTTP Endpoints

**Core retrieval:**
```
POST /v1/chat/completions  — OpenAI-compatible proxy (primary integration)
POST /context              — { query } → expressed context (Continue-compatible)
POST /context/packet       — agent-safe bundle: verified / stale_risk / refresh_targets
POST /context/refresh-plan — refresh_targets only (reread plan, no evidence items)
POST /fingerprint          — navigation-first payload with tier scores, not content
GET  /context/expand       — 1-hop neighborhood from a gene_id (forward/backward/sideways)
```

**Ingestion + maintenance:**
```
POST /ingest               — { content, content_type, metadata? } → gene_ids
POST /consolidate          — rewrite stale documents from their source fingerprints
POST /admin/refresh        — force retrieval-layer refresh
POST /admin/vacuum         — reclaim SQLite pages
POST /admin/compact        — run compaction pass
POST /admin/checkpoint     — WAL checkpoint
GET  /admin/config-dump    — loaded runtime config (secrets redacted) + lane + engine commit + store identity
GET  /sync/status          — delta-sync state: roots, tracked files, guard trips, last pass
POST /sync/rescan          — run a delta-sync pass now ({"allow_mass_delete": true} to confirm a bulk delete)
```

**Identity + sessions:**
```
POST /sessions/register    — register an agent participant for attribution
GET  /sessions             — list registered participants
GET  /sessions/{handle}/recent — recent documents by agent handle
GET  /session/{id}/manifest — session delivery log (what was shipped to this session)
POST /hitl/emit            — record a human-in-the-loop pause event
GET  /hitl/recent          — recent HITL events
```

**Diagnostics:**
```
GET  /stats                — corpus metrics + compression ratio
GET  /health               — model, document count, upstream URL
GET  /genes/{gene_id}      — single document detail
GET  /debug/resonance      — tier activation profile
GET  /debug/neighbors      — co-activation graph neighbors
GET  /metrics/tokens       — token usage counters
GET  /vault/status         — Obsidian vault export state
```

## Testing

```bash
# ~5,000 tests (count drifts; `pytest --co -q | tail -1`), no external services needed
python -m pytest tests/ -m "not live" -v

# Live tests (requires Ollama running)
python -m pytest tests/ -m live -v -s
```

## Observability (Grafana telemetry)

Cymatix ships a native OTel sidecar (collector + Prometheus + Tempo + Loki + Grafana):

```powershell
scripts\setup-grafana-telem.ps1     # Windows
scripts/setup-grafana-telem.sh      # Linux / macOS
```

Enable telemetry on the backend:

```bash
CYMATIX_OTEL_ENABLED=1 CYMATIX_OTEL_ENDPOINT=localhost:4317 \
  python -m uvicorn cymatix_context._asgi:app --port 11437
```

Dashboards: <http://localhost:3000/d/cymatix-overview>.
See [`docs/architecture/OBSERVABILITY.md`](docs/architecture/OBSERVABILITY.md) for the full instrumentation surface.

## Gotchas

- **Windows-only bench paths:** many scripts under `scripts/` and `benchmarks/` default to `F:\Projects\...` checkouts, some still at the pre-rename checkout name. On Linux or a cloud VM, pass the corpus and bed paths explicitly. Bench builders may still write SPLADE vectors at ingest even though retrieval keeps it off; check the builder's flags before a long build.
- **Dates in `git log` lie for old files:** the July 2026 rename sweeps touched almost every file, so "last modified" often means "renamed", not "reviewed".
- **Knowledge store path:** Default is `genomes/main/genome.db` (not project root). Delete to start fresh; auto-creates on first use.
- **Synonym map is critical:** If queries return "no relevant context", check that query keywords map to the tags assigned at ingest. Add synonyms in `[synonyms]`.
- **Stage 2 backfill:** only relevant if you opt in to dense (`[retrieval] dense_embedding_enabled = true`, default off since 2026-08-15). Ingest-time dense writes are also off by default (`[ingestion] dense_embed_on_ingest = false` since 2026-08-19, #371), so BGE-M3 vectors (`embedding_dense_v2`) are NULL until you run `scripts/backfill_bgem3_v2.py` (or re-ingest with the knob on) — the dense tier retrieves nothing without them.
- **Fusion mode:** Default is `"rrf"` (Reciprocal Rank Fusion) since 2026-07-06 — SIKE Run-2 measured +12pp gold_delivered over additive on xl. Set `fusion_mode = "additive"` in `[retrieval]` to restore the legacy pre-Stage-3 accumulator (scheduled for removal in v(N+2)). Under RRF the abstain gates run ratio-only (absolute floors were additive-calibrated).
- **Session delivery:** `session_delivery_enabled = true` tracks what documents each session has already received and elides repeats. Saves ~40% tokens on multi-turn conversations (unverified design estimate — pending the `cymatix_session_tokens_saved_total` counter). Flip to false or pass `ignore_delivered: true` in /context body for benchmarks.
- **Ingest concurrency changes the bed:** parallel LLM tagging (client pool > 1 and/or `OLLAMA_NUM_PARALLEL` > 1) produces different gene content and tags than sequential ingest of the same bytes, even at temp 0 (LLM-batching drift, byte-receipted 2026-08-08: content differed on 105/150 shared genes — cc-exchange EXP-A refutation). Cross-bed comparisons must pin ingest concurrency (`ingest_c` in `docs/benchmarks/BASELINES.md`); comparable rebuilds ingest sequentially unless the comparator was built parallel.
- **Continue IDE:** Use Chat mode, not Agent mode. The proxy doesn't handle tool routing.
- **Naming lexicon:** Biology terms (gene, genome, ribosome, chromatin, splice) have canonical software equivalents (document, knowledge store, compressor, etc.). See the wiki Lexicon page (docs/ROSETTA.md is a stub) for the full mapping. Both vocabularies work in code (back-compat shims); new code should use the software terms.

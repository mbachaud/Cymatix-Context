# Helix Context

Knowledge-store-based context compression for local LLMs. v0.5.0.

## Quick Start

```bash
# Install
pip install helix-context

# Ingest content
helix ingest path/to/your/docs/ --recursive

# Query the store
helix query "what does the splice step do?"

# Inspect corpus state
helix diag corpus
helix status

# Start the FastAPI proxy (IDE integrations, MCP hosts)
helix-server
# or
python -m uvicorn helix_context._asgi:app --host 127.0.0.1 --port 11437
```

Full CLI reference: `docs/clients/cli.md`.

## How It Works

A transparent OpenAI-compatible proxy that intercepts LLM requests and injects compressed context from a persistent SQLite knowledge store.

**7-stage pipeline per turn:**

0. **Classify** — rule-based query classifier picks decoder mode + assembly cap (no model call)
1. **Extract** — heuristic keyword extraction from query (no model call)
2. **Retrieve** — FTS5 lexical + tag lookup + synonym expansion + co-activation + cymatics 256-bin spectrum scoring (all default-on, no neural inference at query time); optional BGE-M3 dense recall (`[retrieval] dense_embedding_enabled`, default off) and SPLADE sparse expansion (`[ingestion] splade_enabled`, default off) add transformer query-encoding when enabled; ranks via Reciprocal Rank Fusion when `[retrieval] fusion_mode = "rrf"` (default: `"additive"`)
3. **Re-rank** — CPU model scores candidates by relevance (optional, off by default)
4. **Splice** — CPU model compresses each candidate, keeping high-value fragments (batched)
5. **Assemble** — join spliced parts, enforce token budget, attach per-document legibility headers (fired tiers, confidence marker, compression ratio), elide already-delivered documents via session working-set register
6. **Persist** — pack query+response exchange into knowledge store (background)

Additionally, a **freshness gate** (Stage 7) runs during assembly to demote stale, cold, or superseded documents.

The pipeline emits a **know/miss agent contract** on `/context/packet`: every response carries `know { found, confidence, gene_id_match }` or `miss { reason }` so downstream agents can calibrate trust without guessing.

## Package Structure (post-PR #90)

After the repo restructure, `helix_context/` is organized into 16 sub-packages plus a handful of top-level orchestration modules:

| Package | Purpose |
|---------|---------|
| `adapters/` | Cache, DAL, retriever abstractions |
| `backends/` | Compressor (formerly ribosome), DeBERTa, NLI, SEMA codec, SPLADE |
| `cli/` | `helix` CLI: query, packet, ingest, gene, neighbors, diag, config, status |
| `encoding/` | Chunking, fragment encoding, legibility headers, Headroom bridge |
| `identity/` | CWoLa logger, session delivery, registry, provenance, claims |
| `pipeline/` | Tier logic, pipeline-stage helpers |
| `retrieval/` | Expand, freshness gate, RRF/additive fusion, PLR, intent router, SR, seeded edges, query classifier, tie-break |
| `scoring/` | Cymatics, know-calibration, know-decision, ray-trace, TCM, blending |
| `server/` | FastAPI app factory + route modules (context, ingest, registry, admin) |
| `storage/` | DDL, indexes, co-activation graph |
| `telemetry/` | OTel metrics, histogram instrumentation |
| `vault/` | Obsidian vault export (read-only diagnostic traces) |
| `launcher/` | System-tray supervisor (backend + telemetry stack) |
| `mcp/` | MCP tool surface for Claude Code / Desktop |
| `integrations/` | ScoreRift bridge |

Top-level modules: `context_manager.py` (pipeline orchestrator), `config.py` (TOML loader), `schemas.py` (Pydantic models), `knowledge_store.py` (SQLite DDL + retrieval), `codons.py` (chunker + encoder), `tagger.py` (CPU ingest tagger).

**Back-compat shims:** `genome.py`, `ribosome.py`, `replication.py`, `hgt.py`, `server.py`, `mcp_server.py` re-export from their new locations. Old import paths still work. See `docs/ROSETTA.md` for the full biology-to-software lexicon.

## Configuration

All config lives in `helix.toml`. Sections:

| Section | Key settings |
|---------|-------------|
| `[ribosome]` | model, backend (`"ollama"` / `"claude"` / `"litellm"` / `"none"`), timeout, query_expansion_enabled |
| `[hardware]` | device auto-detection (CUDA, MPS, ROCm, CPU) |
| `[budget]` | expression_tokens (default 7000 — code and helix.toml unified in the 2026-06-12 default-honesty pass), max_genes_per_turn, splice_aggressiveness, decoder_mode, legibility_enabled, session_delivery_enabled |
| `[session]` | synthetic_session_enabled, synthetic_session_window_s, default_party_id |
| `[genome]` | path (`genomes/main/genome.db`), compact_interval, cold_start_threshold, replicas |
| `[server]` | host, port, upstream |
| `[headroom]` | route_upstream toggle for Headroom proxy integration |
| `[ingestion]` | backend (`"cpu"` / `"ollama"` / `"hybrid"`), splade_enabled, rerank_model, entity_graph |
| `[context]` | cold_tier_enabled, cold_tier_k, cold_tier_min_cosine |
| `[cymatics]` | enabled, distance_metric (`"cosine"` / `"w1"`), harmonic_links |
| `[classifier]` | Rule-based query classifier: `enabled` toggle only; per-class caps/decoder hints are code constants pending #205 |
| `[retrieval]` | fusion_mode (`"additive"` / `"rrf"`), sr_enabled, sr_gamma, ray_trace_theta, seeded_edges_enabled |
| `[plr]` | Piecewise linear reranker: enabled, model_path |
| `[know]` | KnowBlock confidence logistic: emit_floor, betas, s_ref, g_ref, stale_after_days (+ calibrated_at / calibrated_on_n written by scripts/calibrate_know_confidence.py) |
| `[mem_sync]` | Auto-memory-to-helix sync: watch_dirs, sync_interval_s |
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
# ~1950 tests, no external services needed
python -m pytest tests/ -m "not live" -v

# Live tests (requires Ollama running)
python -m pytest tests/ -m live -v -s
```

## Observability (Grafana telemetry)

Helix ships a native OTel sidecar (collector + Prometheus + Tempo + Loki + Grafana):

```powershell
scripts\setup-grafana-telem.ps1     # Windows
scripts/setup-grafana-telem.sh      # Linux / macOS
```

Enable telemetry on the backend:

```bash
HELIX_OTEL_ENABLED=1 HELIX_OTEL_ENDPOINT=localhost:4317 \
  python -m uvicorn helix_context._asgi:app --port 11437
```

Dashboards: <http://localhost:3000/d/helix-overview>.
See [`docs/architecture/OBSERVABILITY.md`](docs/architecture/OBSERVABILITY.md) for the full instrumentation surface.

## Gotchas

- **Knowledge store path:** Default is `genomes/main/genome.db` (not project root). Delete to start fresh; auto-creates on first use.
- **Synonym map is critical:** If queries return "no relevant context", check that query keywords map to the tags assigned at ingest. Add synonyms in `[synonyms]`.
- **Stage 2 backfill:** BGE-M3 dense vectors (`embedding_dense_v2`) are NULL until you run `scripts/backfill_bgem3_v2.py`. Low retrieval rate without them.
- **Fusion mode:** Default is `"additive"` (pre-Stage-3 behavior). Flip to `"rrf"` in `[retrieval]` for Reciprocal Rank Fusion. RRF will become default in a future version.
- **Session delivery:** `session_delivery_enabled = true` tracks what documents each session has already received and elides repeats. Saves ~40% tokens on multi-turn conversations. Flip to false or pass `ignore_delivered: true` in /context body for benchmarks.
- **Continue IDE:** Use Chat mode, not Agent mode. The proxy doesn't handle tool routing.
- **Naming lexicon:** Biology terms (gene, genome, ribosome, chromatin, splice) have canonical software equivalents (document, knowledge store, compressor, etc.). See `docs/ROSETTA.md` for the full mapping. Both vocabularies work in code (back-compat shims); new code should use the software terms.

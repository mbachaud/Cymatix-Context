# Helix Context

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![PyPI version](https://img.shields.io/pypi/v/helix-context.svg)](https://pypi.org/project/helix-context/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 2750+](https://img.shields.io/badge/tests-2750%2B-brightgreen.svg)](tests/)
[![LLM-free pipeline](https://img.shields.io/badge/pipeline-LLM--free-brightgreen.svg)](docs/architecture/PIPELINE_LANES.md)
[![Paper: Agentome](https://img.shields.io/badge/paper-Agentome-purple.svg)](https://mbachaud.substack.com/p/agentome)

> Coordinate-index engine for LLM agents. Retrieves, weighs, and compresses
> your codebase into a context window — without a single LLM call on the
> retrieval path.

---

## Proof (30 seconds)

**WIP benchmark numbers** — compressor disabled (default LLM-free config), N=15 query shapes, May 2026:

| metric | tokens | vs standard RAG (top-5 @ 1500) |
|--------|--------|-------------------------------|
| median | 2,757 | **2.9×** fewer tokens |
| best (focused query) | 1,410 | **5.7×** |
| worst (broad 12-doc) | 3,755 | **2.1×** |

With the optional compressor enabled (Claude Haiku splice), median improves to ~5×.
In multi-turn sessions, the session delivery register elides already-seen documents — observed **37× reduction** on repeated retrievals within a conversation.

Reproducer: `python benchmarks/bench_rag_vs_sike_tokens.py` against your own genome.

**Agent contract**: every `/context` response carries `know { found, confidence }` (grounded — you may answer) or `miss { reason, escalate_to }` (not found — don't answer from genome). Stale results downgrade to `miss(reason="stale"|"cold"|"superseded")` via the freshness gate.

## Get started (60 seconds)

```bash
# 1. Install
pip install helix-context
python -m spacy download en_core_web_sm

# 2. Ingest your codebase
helix ingest path/to/your/project/ --recursive

# 3. Query it
helix query "how does the splice step work?"

# 4. Or start the proxy for IDE integration
helix-server   # binds to 127.0.0.1:11437
```

For extras matrix, BGE-M3 backfill, and tray setup: [docs/SETUP.md](docs/SETUP.md).

## Agent surfaces

Three ways to drive Helix — same retrieval primitives, same JSON shapes:

| Surface | Best for | Example |
|---------|----------|---------|
| **CLI** | Scripts, CI, cold-start agents | `helix query "..." --json` |
| **MCP** | Claude Code, Cursor, Claude Desktop | Add to `settings.json` |
| **HTTP proxy** | Continue IDE, `OPENAI_BASE_URL` redirect | `POST /context` |

```bash
# CLI — no server, no daemon, subprocess-drivable
helix query    "what does the splice step do?" --json
helix packet   "edit the splice step" --task-type edit --json
helix gene get abc123 --json
helix neighbors "splice step" --k 10 --json
helix refresh-targets "edit the splice step" --json
helix status
helix diag corpus
```

Full CLI reference: [`docs/clients/cli.md`](docs/clients/cli.md).
MCP tool schemas: [`docs/api/mcp-tools.md`](docs/api/mcp-tools.md).

## Pipeline (2 minutes)

Seven stages per turn, all LLM-free except optional splice:

```
  query
    │
    ▼
┌──────────────┐
│ 0. Classify  │  rule-based: decoder mode + assembly cap
└──────┬───────┘
       ▼
┌──────────────┐
│ 1. Extract   │  heuristic keyword + entity extraction
└──────┬───────┘
       ▼
┌──────────────┐  FTS5 BM25 + BGE-M3 dense (1024-dim) + tags
│ 2. Retrieve  │  + synonym expansion + co-activation + SR
│              │  ranked via RRF or additive fusion
└──────┬───────┘
       ▼
┌──────────────┐
│ 3. Re-rank   │  CPU classifier scores (optional)
└──────┬───────┘
       ▼
┌──────────────┐
│ 4. Splice    │  Headroom Kompress (CPU) or LLM compressor
└──────┬───────┘
       ▼
┌──────────────┐  token budget + legibility headers (fired tiers,
│ 5. Assemble  │  confidence ◆/◇/⬦, compression ratio) +
│   + Stage 7  │  freshness gate (stale/cold/superseded → miss)
└──────┬───────┘  + session delivery (elide already-seen docs)
       ▼
┌──────────────┐
│ 6. Persist   │  query+response → knowledge store (background)
└──────┘───────┘
       ▼
   know { } or miss { }
```

- **know/miss contract**: `know` means the context is grounded, agent may answer. `miss` means don't answer from genome — escalate via `escalate_to` tools or refetch from `refresh_targets`.
- **Caller model class**: `/context` accepts `caller_model_class: "generic" | "small_moe" | "frontier"` to select render branch (ordering, assembly cap, decoder mode). See [docs/api/context-endpoint.md §7](docs/api/context-endpoint.md).

<details>
<summary><strong>Configuration (17 sections in helix.toml)</strong></summary>

| Section | Key settings |
|---------|-------------|
| `[ribosome]` | `enabled`, `backend` (`"none"` / `"litellm"` / `"claude"` / `"deberta"`), query_expansion |
| `[hardware]` | Device auto-detection (CUDA → ROCm → MPS → CPU) |
| `[budget]` | `expression_tokens` (7k default), `max_genes_per_turn`, splice_aggressiveness, `legibility_enabled`, `session_delivery_enabled` |
| `[session]` | Synthetic session windows, default party_id |
| `[genome]` | `path` (`genomes/main/genome.db`), compact_interval, replicas |
| `[server]` | host, port, upstream |
| `[headroom]` | Optional Headroom proxy lifecycle |
| `[ingestion]` | `backend` (`"cpu"` / `"ollama"`), splade_enabled, entity_graph |
| `[context]` | Cold-tier retrieval: enabled, k, min_cosine |
| `[cymatics]` | Frequency-domain scoring, harmonic_links, distance_metric |
| `[classifier]` | Rule-based query classification thresholds |
| `[retrieval]` | `fusion_mode` (`"additive"` / `"rrf"`), SR, ray_trace_theta, seeded_edges |
| `[plr]` | Piecewise linear reranker model |
| `[know]` | Know/miss calibration: emit_floor, betas, s_ref, g_ref, stale_after_days |
| `[mem_sync]` | Auto-memory → helix sync: watch_dirs, interval |
| `[synonyms]` | Query expansion map (e.g., "cache" → ["redis", "ttl"]) |
| `[abstain]` | Low-confidence abstention thresholds |

Full reference: [docs/config-reference.md](docs/config-reference.md).

</details>

<details>
<summary><strong>Full endpoint reference</strong></summary>

**Core retrieval:**
| Endpoint | Purpose |
|----------|---------|
| `POST /context` | know/miss + expressed_context (primary) |
| `POST /context/packet` | Agent-safe bundle: verified / stale_risk / refresh_targets |
| `POST /context/refresh-plan` | Refresh targets only (reread plan) |
| `POST /fingerprint` | Navigation-first payload (scores, no body) |
| `GET /context/expand` | 1-hop neighborhood from a gene_id |
| `POST /v1/chat/completions` | OpenAI-compatible proxy |

**Ingestion + maintenance:**
| Endpoint | Purpose |
|----------|---------|
| `POST /ingest` | Add content to the knowledge store |
| `POST /consolidate` | Rewrite stale docs from source fingerprints |
| `POST /admin/refresh` | Force retrieval-layer refresh |
| `POST /admin/vacuum` | Reclaim SQLite pages |
| `POST /admin/swap-db` | Hot-swap the .db file without restart |

**Identity + sessions:**
| Endpoint | Purpose |
|----------|---------|
| `POST /sessions/register` | Register agent participant |
| `GET /sessions` | List registered participants |
| `GET /session/{id}/manifest` | Session delivery log |
| `POST /hitl/emit` | Record HITL pause event |

**Diagnostics:**
| Endpoint | Purpose |
|----------|---------|
| `GET /stats` | Corpus metrics + compression ratio |
| `GET /health` | Model, doc count, calibration provenance |
| `GET /genes/{gene_id}` | Single document detail |
| `GET /debug/resonance` | Tier activation profile |
| `GET /metrics/tokens` | Token usage counters |

Full schema: [docs/api/endpoints.md](docs/api/endpoints.md).

</details>

<details>
<summary><strong>Package structure (15 packages, post-PR #90)</strong></summary>

| Package | Purpose |
|---------|---------|
| `adapters/` | Cache, DAL, external retriever protocol |
| `backends/` | Compressor, BGE-M3 codec, DeBERTa, NLI, SEMA, SPLADE |
| `cli/` | `helix` CLI: query, packet, gene, neighbors, ingest, diag, config, status |
| `encoding/` | Chunking, fragments, legibility headers, Headroom bridge |
| `identity/` | CWoLa logger, session delivery, registry, provenance, claims |
| `pipeline/` | Tier logic, stage helpers |
| `retrieval/` | Expand, freshness, RRF/additive fusion, PLR, intent router, SR, seeded edges, query classifier |
| `scoring/` | Cymatics, know-calibration, know-decision, ray-trace, TCM |
| `server/` | FastAPI app factory + route modules (context, ingest, registry, admin) |
| `storage/` | DDL, indexes, co-activation graph |
| `telemetry/` | OTel metrics, histogram instrumentation |
| `vault/` | Obsidian vault export (diagnostic traces) |
| `launcher/` | System-tray supervisor |
| `mcp/` | MCP tool surface for Claude Code / Desktop |
| `integrations/` | ScoreRift bridge |

Back-compat shims: `genome.py`, `ribosome.py`, `server.py`, `replication.py`, `hgt.py` re-export from new locations. Lexicon: [docs/ROSETTA.md](docs/ROSETTA.md).

</details>

## IDE + MCP integration

<details>
<summary><strong>MCP setup (Claude Code / Cursor / Claude Desktop)</strong></summary>

```json
{
  "mcpServers": {
    "helix-context": {
      "command": "python",
      "args": ["-m", "helix_context.mcp_server"],
      "cwd": "/absolute/path/to/your/project",
      "env": { "HELIX_MCP_URL": "http://127.0.0.1:11437" }
    }
  }
}
```

</details>

<details>
<summary><strong>Continue IDE</strong></summary>

```yaml
models:
  - name: Helix (Local)
    provider: openai
    model: gemma3:e4b
    apiBase: http://127.0.0.1:11437/v1
    apiKey: EMPTY
    roles: [chat]
    defaultCompletionOptions:
      contextLength: 128000
      maxTokens: 4096
```

</details>

<details>
<summary><strong>OpenAI-compatible proxy (zero code changes)</strong></summary>

```bash
OPENAI_BASE_URL=http://localhost:11437/v1 your-app
```

</details>

## Knowledge store management

```toml
[genome]
path = "genomes/main/genome.db"   # relative to helix run directory
```

Backup (safe while running — WAL mode):
```bash
cp genomes/main/genome.db backups/genome-$(date +%Y%m%d).db
```

BGE-M3 backfill (one-time, after install):
```bash
python scripts/backfill_bgem3_v2.py genomes/main/genome.db
```

## Observability

```powershell
scripts\setup-grafana-telem.ps1     # Windows
scripts/setup-grafana-telem.sh      # Linux / macOS
```

Dashboard: <http://localhost:3000/d/helix-overview>.
Full surface: [docs/architecture/OBSERVABILITY.md](docs/architecture/OBSERVABILITY.md).

## Gotchas

- **Knowledge store path** is `genomes/main/genome.db` (not project root). Delete to start fresh.
- **BGE-M3 backfill** is one-time post-install — `embedding_dense_v2 IS NULL` until you run `scripts/backfill_bgem3_v2.py`. Low retrieval rate without it.
- **Fusion mode** defaults to `"additive"` (back-compat). Flip to `"rrf"` in `[retrieval]` after running `scripts/calibrate_thresholds.py`.
- **Session delivery** (`session_delivery_enabled = true`) tracks delivered docs per session, elides repeats. ~40% token savings on multi-turn. Pass `ignore_delivered: true` in `/context` body for benchmarks.
- **know/miss contract** requires the agent prompt fragment to be honored — without it, frontier models confabulate. Import `helix_context.agent_prompt.full_fragment()`.
- **Naming lexicon**: biology terms (gene, genome, ribosome) have canonical software equivalents (document, knowledge store, compressor). Both work in code; new code uses software terms. See [docs/ROSETTA.md](docs/ROSETTA.md).

## Testing

```bash
python -m pytest tests/ -m "not live" -v   # ~2,750 tests, no external services
```

## Documentation

| Start here | Go deeper |
|-----------|-----------|
| [Setup guide](docs/SETUP.md) | [Pipeline lanes](docs/architecture/PIPELINE_LANES.md) |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | [Retrieval dimensions](docs/architecture/DIMENSIONS.md) |
| [`/context` API](docs/api/context-endpoint.md) | [Knowledge graph](docs/architecture/KNOWLEDGE_GRAPH.md) |
| [Config reference](docs/config-reference.md) | [Session registry](docs/architecture/SESSION_REGISTRY.md) |
| [Agent SDK fragment](docs/agent-sdk-fragment.md) | [Observability](docs/architecture/OBSERVABILITY.md) |
| [Operator runbooks](docs/operator-runbooks.md) | [Launcher architecture](docs/architecture/LAUNCHER.md) |
| [Dense ingest on ≤12 GB VRAM](docs/operations/DENSE_VRAM.md) | |

## Acknowledgments

Built on: [spaCy](https://spacy.io/) NER · [Howard 2005](https://doi.org/10.1037/0033-295X.112.3.559) TCM · [Stachenfeld 2017](https://www.nature.com/articles/nn.4650) SR · SQLite FTS5 BM25 · [BGE-M3](https://huggingface.co/BAAI/bge-m3) · [Kompress](https://huggingface.co/chopratejas/kompress-base) · [Headroom](https://github.com/chopratejas/headroom)

## License

[Apache-2.0](LICENSE). See [NOTICE](NOTICE) for third-party attributions.

# Cymatix Context

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![PyPI version](https://img.shields.io/pypi/v/cymatix-context.svg)](https://pypi.org/project/cymatix-context/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 2900+](https://img.shields.io/badge/tests-2900%2B-brightgreen.svg)](tests/)
[![LLM-free pipeline](https://img.shields.io/badge/pipeline-LLM--free-brightgreen.svg)](docs/architecture/PIPELINE_LANES.md)
[![Paper: Agentome](https://img.shields.io/badge/paper-Agentome-purple.svg)](https://mbachaud.substack.com/p/agentome)

> Coordinate-index engine for LLM agents. Retrieves, weighs, and compresses
> your codebase into a context window — without a single LLM call on the
> retrieval path.

**Formerly `helix-context`** — renamed July 2026. As of 0.8.5 the old surface (the
`helix_context` import, `helix*` CLI names, `HELIX_*` env vars, `helix.toml`)
has been **removed** — see [Migrating from
helix-context](#migrating-from-helix-context).

The name comes from the engine's cymatics stage: each term is hashed (MD5) into
one of 256 bins and given a small Gaussian spread, and query and candidate are
compared as the resulting 256-dimensional vectors. It's a cheap, deterministic,
model-free term transform — "cymatics" is a mnemonic for that binned spectrum,
not a claim of signal-processing semantics (nearby bins are hash placement, not
related meaning). It's a candidate-reordering signal that has **not yet been
isolated against hashed bag-of-words or random-bin controls** — treat it as an
experimental cheap feature, not a proven one. The `/fingerprint` endpoint
exposes the binned vector directly.

---

## Proof (30 seconds)

**Token economics** — compressor disabled (default LLM-free config), N=15 query shapes, May 2026:

| metric | tokens | vs standard RAG (top-5 @ 1500) |
|--------|--------|-------------------------------|
| median | 2,757 | **2.9×** fewer tokens |
| best (focused query) | 1,410 | **5.7×** |
| worst (broad 12-doc) | 3,755 | **2.1×** |

In multi-turn sessions, the session delivery register elides already-seen
documents — observed **37× reduction** on repeated retrievals within a
conversation (~40% token savings on typical multi-turn work).

Reproducer: `python benchmarks/bench_rag_vs_sike_tokens.py` against your own knowledge store.

*Caveat: the "vs standard RAG" denominator (top-5 @ 1500 tokens) is a
configurable baseline, and the 37× multi-turn figure is elision of
already-delivered documents, not compression of new content. The decision-useful
claim is **equal-or-better task completion at fewer input tokens**; a same-harness
baseline/ablation frontier (BM25 / BGE-M3 / BM25+dense RRF / Cymatix full /
Cymatix minus-cymatics), paired with correctness, is future work — not yet
published.*

**External benchmark** — [EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench)
(Onyx, 500 questions over a ~500K-document enterprise corpus), July 2026,
scored under ERB's official judge protocol and submitted to the leaderboard:

| ERB official metric | score |
|---|---|
| Correctness | **41.6%** (208/500) |
| Completeness | **42.8%** |
| Overall | **33.57** |

Context for those numbers: the corpus was ingested as **829,131 fragments on
a single consumer desktop**, and retrieval ran with **zero LLM calls on the
retrieval path**. The claim is that operating point — local, LLM-free, at
scale — not a leaderboard win. Quote the delivery and correctness numbers as
a pair: gold-document delivery was 55% at 829K-fragment scale (82% at 50K)
and delivery is *not* a graded pass — end-to-end correctness is the 41.6%
above. When the gold document *was* delivered, the answer was correct 79% of
the time, so retrieval breadth at extreme scale, not answer synthesis, is
the current ceiling. Full methodology + repro:
[docs/benchmarks/2026-07-10-erb-blob-829k-reproduction.md](docs/benchmarks/2026-07-10-erb-blob-829k-reproduction.md).

**Scale caveat:** the 829K-fragment operating point above runs the *unsharded*
engine. The **sharded** path (corpora split across shard DBs) currently trails
unsharded by ~31pp recall@10 / ~30pp MRR on the xl bed — dense recall and
co-activation are not yet at parity across shards
([#275](https://github.com/mbachaud/Cymatix-Context/issues/275)). "Local-first
at scale" is a demonstrated research operating point on the unsharded engine, not
yet a turnkey general substrate for sharded corpora.

**Fusion**: Reciprocal Rank Fusion has been the default ranker since
2026-07-06 — measured **+12pp gold-document delivery** over the legacy
additive accumulator on the hardest internal bed (0.74 vs 0.62).

**Agent contract** *(shape stable; confidence calibration experimental)*: every
`/context` response carries `know { found, confidence }` (grounded — you may
answer) or `miss { reason, escalate_to }` (not found — don't answer from the
knowledge store). Stale results downgrade to
`miss(reason="stale"|"cold"|"superseded")` via the freshness gate. The contract
*shape* is stable and load-bearing, but the `confidence` scalar is under active
recalibration — on current internal beds it is not yet a reliable trust signal
([#287](https://github.com/mbachaud/Cymatix-Context/issues/287),
[#239](https://github.com/mbachaud/Cymatix-Context/issues/239)). Rely on `found`
/ `reason` today; treat `confidence` as provisional.

## Get started

Requires Python 3.11+. Core install is dependency-light (FastAPI + SQLite,
no torch):

```bash
pip install cymatix-context
python -m spacy download en_core_web_sm     # ingest tagger model (with the cpu extra)
```

Pick extras for the features you turn on:

| Extra | Enables | Pull |
|---|---|---|
| *(core)* | HTTP server, `/context`, `/context/packet`, FTS5 retrieval | light |
| `embeddings` | BGE-M3 dense recall (default-on retrieval stage) | torch via sentence-transformers |
| `cpu` | spaCy NER ingest tagging | spacy |
| `mcp` | `python -m cymatix_context.mcp_server` (Claude Code / Cursor / Desktop) | mcp SDK |
| `otel` | Grafana/Tempo/Loki observability | opentelemetry |
| `launcher-tray` | System-tray supervisor (Windows) | pystray (LGPL, opt-in) |
| `ast` | Tree-sitter code chunking | tree-sitter grammars |
| `all` | Everything above except dev + tray | heavy |

```bash
pip install "cymatix-context[embeddings,cpu,mcp]"   # recommended working set
```

Then:

```bash
# 1. Ingest your project
cymatix ingest path/to/your/project/ --recursive

# 2. One-time dense backfill (BGE-M3 vectors; retrieval is weak without it)
python scripts/backfill_bgem3_v2.py genomes/main/genome.db

# 3. Query from the CLI — no server needed
cymatix query "how does the splice step work?"

# 4. Or start the proxy for IDE / agent integration
cymatix-server            # binds to 127.0.0.1:11437
curl -s http://127.0.0.1:11437/health
```

Full setup (extras matrix, GPU detection, tray): [docs/SETUP.md](docs/SETUP.md).

## Usage

Three surfaces, same retrieval primitives, same JSON shapes:

| Surface | Best for | Example |
|---------|----------|---------|
| **CLI** | Scripts, CI, cold-start agents | `cymatix query "..." --json` |
| **MCP** | Claude Code, Cursor, Claude Desktop | see below |
| **HTTP proxy** | Continue IDE, `OPENAI_BASE_URL` redirect | `POST /context` |

```bash
# CLI — no server, no daemon, subprocess-drivable
cymatix query    "what does the splice step do?" --json
cymatix packet   "edit the splice step" --task-type edit --json
cymatix gene get abc123 --json
cymatix neighbors "splice step" --k 10 --json
cymatix refresh-targets "edit the splice step" --json
cymatix status
cymatix diag corpus
```

```bash
# HTTP — agent-safe packet with verified / stale_risk / refresh_targets
curl -s http://127.0.0.1:11437/context/packet \
  -H "content-type: application/json" \
  -d '{"query": "how does the freshness gate demote stale docs?"}'
```

Configuration lives in `cymatix.toml`. Env vars use the `CYMATIX_*` prefix:

```bash
CYMATIX_GENOME_PATH=genomes/dogfood/genome.db cymatix-server
CYMATIX_OTEL_ENABLED=1 CYMATIX_OTEL_ENDPOINT=localhost:4317 cymatix-server
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
│              │  + cymatics 256-bin spectrum scoring
│              │  ranked via RRF (default) or additive fusion
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

- **know/miss contract** *(shape stable; confidence experimental)*: `know` means the context is grounded, agent may answer. `miss` means don't answer from the knowledge store — escalate via `escalate_to` tools or refetch from `refresh_targets`. The `confidence` scalar is under active recalibration ([#287](https://github.com/mbachaud/Cymatix-Context/issues/287), [#239](https://github.com/mbachaud/Cymatix-Context/issues/239)) — rely on `found` / `reason`, treat `confidence` as provisional.
- **Caller model class**: `/context` accepts `caller_model_class: "generic" | "small_moe" | "frontier"` to select render branch (ordering, assembly cap, decoder mode). See [docs/api/context-endpoint.md §7](docs/api/context-endpoint.md).

<details>
<summary><strong>Configuration (17 sections in cymatix.toml)</strong></summary>

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
| `[retrieval]` | `fusion_mode` (`"rrf"` default / `"additive"` legacy), SR, ray_trace_theta, seeded_edges |
| `[plr]` | Piecewise linear reranker model |
| `[know]` | Know/miss calibration: emit_floor, betas, s_ref, g_ref, stale_after_days |
| `[mem_sync]` | Auto-memory → knowledge-store sync: watch_dirs, interval |
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
<summary><strong>Package structure (15 packages)</strong></summary>

| Package | Purpose |
|---------|---------|
| `adapters/` | Cache, DAL, external retriever protocol |
| `backends/` | Compressor, BGE-M3 codec, DeBERTa, NLI, SEMA, SPLADE |
| `cli/` | `cymatix` CLI: query, packet, gene, neighbors, ingest, diag, config, status |
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

The import package is `cymatix_context` (the old `helix_context` alias was
removed in 0.8.5). Biology-named module shims `genome.py`, `ribosome.py`,
`server.py`, `replication.py`, `hgt.py` persist as the domain lexicon.
Lexicon: [docs/ROSETTA.md](docs/ROSETTA.md).

</details>

## IDE + MCP integration

<details>
<summary><strong>MCP setup (Claude Code / Cursor / Claude Desktop)</strong></summary>

```json
{
  "mcpServers": {
    "cymatix-context": {
      "command": "python",
      "args": ["-m", "cymatix_context.mcp_server"],
      "cwd": "/absolute/path/to/your/project",
      "env": { "CYMATIX_MCP_URL": "http://127.0.0.1:11437" }
    }
  }
}
```

The server self-identifies as `cymatix`, so client tools appear as
`mcp__cymatix__*`.

</details>

<details>
<summary><strong>Continue IDE</strong></summary>

```yaml
models:
  - name: Cymatix (Local)
    provider: openai
    model: gemma3:e4b
    apiBase: http://127.0.0.1:11437/v1
    apiKey: EMPTY
    roles: [chat]
    defaultCompletionOptions:
      contextLength: 128000
      maxTokens: 4096
```

Use Chat mode, not Agent mode — the proxy doesn't handle tool routing.

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
path = "genomes/main/genome.db"   # relative to the cymatix run directory
```

Backup (safe while running — WAL mode):
```bash
cp genomes/main/genome.db backups/genome-$(date +%Y%m%d).db
```

BGE-M3 backfill (one-time, after install):
```bash
python scripts/backfill_bgem3_v2.py genomes/main/genome.db
```

## Security

The proxy binds to loopback (`127.0.0.1:11437`) by default, but a loopback
bind is **not** authentication — any process on the same host can reach the
port. Two opt-in knobs harden the mutating surface (both default-off; an
untouched config behaves exactly as before):

```toml
[server]
admin_token = "your-secret"              # /admin/*, /ingest, /consolidate demand "Authorization: Bearer your-secret" (401 otherwise)
swap_db_roots = ["F:/cymatix/genomes"]   # /admin/swap-db may only open paths under these roots (403 otherwise)
```

Details: [docs/config-reference.md](docs/config-reference.md), `[server]`.

## Observability

```powershell
scripts\setup-grafana-telem.ps1     # Windows
scripts/setup-grafana-telem.sh      # Linux / macOS
```

Dashboard: <http://localhost:3000/d/cymatix-overview>.
Full surface: [docs/architecture/OBSERVABILITY.md](docs/architecture/OBSERVABILITY.md).

## Migrating from helix-context

**As of 0.8.5 the old `helix` surface has been removed** — this is a clean
break. The table below maps each removed name to its replacement. If you are
still on the old names, migrate to the right-hand column, or pin
`cymatix-context<0.8.5` (0.8.0 keeps the aliases), or the last `helix-context`
release, until you can.

| Surface | Old (removed in 0.8.5) | New (use this) |
|---|---|---|
| Install | `pip install helix-context` | `pip install cymatix-context` |
| Import | `import helix_context` → `ModuleNotFoundError` | `import cymatix_context` |
| CLI | `helix`, `helix-server`, `helix-launcher`, `helix-status`, `helix-vault` | `cymatix`, `cymatix-server`, `cymatix-launcher`, `cymatix-status`, `cymatix-vault` |
| Config file | `helix.toml` (no longer read) | `cymatix.toml` |
| Env vars | `HELIX_*` (no longer read) | `CYMATIX_*` |
| MCP `-m` entry | `python -m helix_context.mcp_server` | `python -m cymatix_context.mcp_server` |
| MCP tools | `helix_*` tool names | `cymatix_*` |
| ASGI target | `helix_context._asgi:app` | `cymatix_context._asgi:app` |

The knowledge-store file format is unchanged — existing `genome.db` files
work as-is, no re-ingest needed.

## Gotchas

- **Knowledge store path** is `genomes/main/genome.db` (not project root). Delete to start fresh.
- **BGE-M3 backfill** is one-time post-install — `embedding_dense_v2 IS NULL` until you run `scripts/backfill_bgem3_v2.py`. Low retrieval rate without it.
- **Fusion mode** defaults to `"rrf"` (since 2026-07-06; +12pp gold delivery vs additive on the hardest bed). `"additive"` remains as the legacy accumulator, scheduled for condition-gated removal. Under RRF the abstain gates run ratio-only.
- **Sharded scale gap**: the sharded adapter currently trails the unsharded engine by ~31pp recall@10 on xl (dense recall and co-activation not yet at parity) — [#275](https://github.com/mbachaud/Cymatix-Context/issues/275). Prefer the unsharded engine for accuracy-sensitive corpora until this is closed.
- **Session delivery** (`session_delivery_enabled = true`) tracks delivered docs per session, elides repeats. ~40% token savings on multi-turn. Pass `ignore_delivered: true` in `/context` body for benchmarks.
- **know/miss contract** requires the agent prompt fragment to be honored — without it, frontier models confabulate. Import `cymatix_context.agent_prompt.full_fragment()`.
- **Naming lexicon**: biology terms (gene, genome, ribosome) have canonical software equivalents (document, knowledge store, compressor). Both work in code; new code uses software terms. See [docs/ROSETTA.md](docs/ROSETTA.md).

## Testing

```bash
python -m pytest tests/ -m "not live" -v   # ~2,900 tests, no external services
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

## How this was built

Cymatix Context is architected and QA-directed by Michael Bachaud. Implementation,
refactoring, draft documentation, and test generation are produced by AI coding
agents under spec- and benchmark-gated review. The human owns the product thesis,
architecture selection, acceptance criteria, experiment design, and falsification
authority; the models own the code production.

## License

[Apache-2.0](LICENSE). See [NOTICE](NOTICE) for third-party attributions.

# Helix Context

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![PyPI version](https://img.shields.io/pypi/v/helix-context.svg)](https://pypi.org/project/helix-context/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![LLM-free pipeline](https://img.shields.io/badge/pipeline-LLM--free-brightgreen.svg)](docs/architecture/PIPELINE_LANES.md)
[![Paper: Agentome](https://img.shields.io/badge/paper-Agentome-purple.svg)](https://mbachaud.substack.com/p/agentome)

**Knowledge-store-based context compression for local LLMs, with a machine-tagged know/miss agent contract.**

Coordinate-index engine for LLM agents. Helix retrieves, weighs, and compresses your codebase into a context window — without a single LLM call on the retrieval path.

## At a glance

- **Compression**: collapses ~9k tokens of raw working set into a ~600 effective-token assembled context (28.7× headline on production workloads, 5.4× median across 15 query shapes).
- **Agent contract**: every `/context` response carries a top-level `know{}` (grounded, you may answer) or `miss{}` (`do_not_answer_from_genome:true`, plus `escalate_to` tools or `refresh_targets` paths). Stale `know` blocks downgrade to `miss(reason="stale"|"cold"|"superseded")` via the Stage 7 freshness gate.
- **LLM-free retrieval**: `/context` runs spaCy NER, SQLite FTS5 BM25, BGE-M3 dense recall, RRF fusion, Howard-2005 TCM, and Hebbian co-activation — zero compressor calls. The only LLM call is downstream at `/v1/chat/completions`.
- **Three install paths**: compact tray flow (`start-helix-tray.bat`) for the daily driver, proxy-only for `OPENAI_BASE_URL` redirection, agent-SDK fragment for frontier-model integration.

### Quick navigation

- [Setup guide](docs/SETUP.md) — extras matrix, OS-specific install paths, calibration runbook
- [Troubleshooting](docs/TROUBLESHOOTING.md) — common errors and recovery
- [`/context` API reference](docs/api/context-endpoint.md) — request/response schema, render branches, field-by-field
- [Operator runbooks](docs/operator-runbooks.md) — backfill, calibrate, consolidate, vacuum
- [Config reference](docs/config-reference.md) — every key in `helix.toml`
- [Agent SDK integration](docs/agent-sdk-fragment.md) — frontier prompt fragment + compliance eval
- [Environment variables](.env.example) — runtime overrides

## Quick Start

```bash
# 1. Install
pip install -e ".[all,launcher,otel]"
python -m spacy download en_core_web_sm

# 2. Pull a small model for the ribosome (optional — disabled by default)
ollama pull gemma3:e4b

# 3. Start the proxy
python -m uvicorn helix_context._asgi:app --host 127.0.0.1 --port 11437

# 4. Or, daily-driver tray (Windows):
start-helix-tray.bat

# 5. Seed your genome (one-time)
python examples/seed_genome.py path/to/your/docs/

# 6. Post-merge: backfill 1024-dim BGE-M3 vectors for Stage 2 dense recall
python scripts/backfill_bgem3_v2.py genomes/main/genome.db
```

For full options including the extras matrix, see [docs/SETUP.md](docs/SETUP.md).

## Pipeline

A transparent OpenAI-compatible proxy that intercepts LLM requests and injects compressed context from a persistent SQLite knowledge store. Six stages per turn, all LLM-free except step 4 splice (optional compressor) and the downstream completion call:

- **0a. Classify** — rule-based query classifier picks decoder mode + assembly cap (no model call).
- **1. Extract** — heuristic keyword extraction from query (no model call).
- **2. Retrieve** — SQLite tag lookup + BGE-M3 dense recall + synonym expansion + co-activation. Ranks candidates via Reciprocal Rank Fusion over `{dense, FTS5, promoter, harmonic, SR}` when `[retrieval] fusion_mode = "rrf"` (default `"additive"`, see Gotchas).
- **3. Re-rank** — small CPU model scores candidates for relevance.
- **4. Splice** — small CPU model compresses each candidate, keeping only the high-value fragments (batched single call; optional).
- **5. Assemble** — join spliced parts, enforce token budget, wrap in tags. The Stage 7 health pass downgrades to `MissBlock(reason="stale")` when the top-1 source mtime exceeds `last_verified_at`.
- **6. Persist** — pack query + response into the knowledge store (background).

→ Swim-lane reference: [`docs/architecture/PIPELINE_LANES.md`](docs/architecture/PIPELINE_LANES.md)
→ Retrieval dimensions: [`docs/architecture/DIMENSIONS.md`](docs/architecture/DIMENSIONS.md)

## Agent integration

Every `/context` response carries one of two top-level blocks:

- **`know { found, confidence, gene_id_match, ... }`** — retrieval succeeded; the `expressed_context` bytes are grounded. The agent may answer from them.
- **`miss { reason, escalate_to | refresh_targets, do_not_answer_from_genome:true }`** — retrieval did NOT find it (or found it but it is stale, cold, or superseded). The agent should NOT answer from the knowledge store; it should call an escalation tool from `escalate_to` (`grep | rag | web | ask_human`) or refetch from `refresh_targets`.

To make a frontier model honor the contract, prepend the helix-context prompt fragment to your system prompt:

```python
from helix_context.agent_prompt import full_fragment

system_prompt = full_fragment() + "\n\n" + your_existing_system_prompt
```

Without the fragment, frontier models will paper over `do_not_answer_from_genome` and confabulate. See [docs/agent-sdk-fragment.md](docs/agent-sdk-fragment.md) for the full template, the `<helix:no_match/>` token semantics, and the compliance eval recipe.

### Caller model class

`/context` accepts an optional `caller_model_class: "generic" | "small_moe" | "frontier"` field that selects the render branch:

- **`frontier`** (Claude Opus, GPT-5, Gemini 3 Pro): forward rank-1-first ordering, larger assembly cap, full decoder mode.
- **`small_moe`** (qwen3:4b, gemma3:e4b): foveated reverse-rank order, JSON-shaped char-bounded answer slate, condensed decoder.
- **`generic`** (default): regression-locked byte-identical to pre-Stage-5 behavior.

See [docs/api/context-endpoint.md §7](docs/api/context-endpoint.md) for the full behavior matrix.

## Surfaces and endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /context` | know/miss + `expressed_context` (primary integration). |
| `POST /context/packet` | Agent-safe bundle: `verified` / `stale_risk` / `refresh_targets`. |
| `POST /context/refresh-plan` | `refresh_targets` only — reread plan, no evidence items. |
| `POST /fingerprint` | Navigation-first payload (scores + metadata, no body). |
| `POST /consolidate` | Rewrite stale document bodies from their source fingerprints (Stage 7 counterpart to `refresh_targets`). |
| `POST /sessions/register` | Register an agent participant (taude / laude / …) for attribution. |
| `POST /admin/refresh` | Force a retrieval-layer refresh (admin only). |
| `POST /admin/vacuum` | Reclaim SQLite pages after compaction (admin only). |
| `POST /ingest` | Add a document or exchange to the knowledge store. |
| `GET /stats` | Knowledge-store metrics + compression ratio. |
| `GET /health` | Compressor model, document count, upstream URL, **calibration provenance** (Stage 4). |
| `POST /v1/chat/completions` | OpenAI-compatible proxy with automatic context injection. |

→ Full endpoint reference: [`docs/api/endpoints.md`](docs/api/endpoints.md) and [`docs/api/context-endpoint.md`](docs/api/context-endpoint.md)
→ MCP tool schemas: [`docs/api/mcp-tools.md`](docs/api/mcp-tools.md)

**Two surfaces, two caller types:**

| | `/context` | `/context/packet` |
| --- | --- | --- |
| Returns | Assembled compressed window | Pointer + verdict + refresh plan |
| LLM reads? | Directly | No — agent fetches if needed |
| Verdict emitted? | Top-level `know` / `miss` | First-class: `verified / stale_risk / needs_refresh` |
| Best for | Chat clients, Continue | MCP agents, programmatic use |

## Continue IDE Integration

Add to `~/.continue/config.yaml`:

```yaml
models:
  - name: Helix (Local)
    provider: openai
    model: gemma3:e4b           # or whatever is loaded in Ollama
    apiBase: http://127.0.0.1:11437/v1
    apiKey: EMPTY
    roles: [chat]
    defaultCompletionOptions:
      contextLength: 128000     # Helix handles compression downstream
      maxTokens: 4096
```

### MCP setup (Claude Code / Cursor / Claude Desktop)

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "helix-context": {
      "command": "python",
      "args": ["-m", "helix_context.mcp_server"],
      "cwd": "/absolute/path/to/your/project",
      "env": {
        "HELIX_MCP_URL": "http://127.0.0.1:11437"
      }
    }
  }
}
```

### OpenAI-compatible proxy (zero code changes)

```bash
ANTHROPIC_BASE_URL=http://localhost:11437 claude
OPENAI_BASE_URL=http://localhost:11437/v1 your-app
```

## Knowledge store management

### Knowledge store path

Set `path` in `[genome]` to a file or directory:

```toml
[genome]
path = "genomes/main/genome.db"   # relative to helix run directory
# Put this on your fastest NVMe for best ingest throughput.
# Example: path = "D:/helix/genome.db"
```

One helix instance per knowledge store — each reads its own `helix.toml`. Use the `helix_context.hgt` Python API to share documents across instances (cross-store import; legacy term: Horizontal Gene Transfer).

### Backup

SQLite WAL mode makes it safe to copy the `.db` file while helix is running:

```bash
# cron / Linux
cp genomes/main/genome.db backups/genome-$(date +%Y%m%d).db
```

```powershell
# PowerShell / Windows
Copy-Item genomes\main\genome.db backups\genome-$(Get-Date -Format yyyyMMdd).db
```

### DAL — source-content fetching

`/context/packet` returns `source_id` pointers. Callers resolve them to bytes via the DAL:

```python
from helix_context.adapters.dal import DAL

dal = DAL()                          # file + HTTP built-in
dal.register("s3", my_s3_fetcher)    # register additional schemes
text, meta = dal.fetch("s3://bucket/schema.json")
```

### Native observability (default)

The tray (`start-helix-tray.bat`) manages the native OpenTelemetry binaries in `tools/native-otel/` automatically. A balloon notification confirms the sidecar is running. To opt out: `HELIX_OBSERVABILITY=0 start-helix-tray.bat`.

If you want Grafana telemetry without the tray (headless servers, CI, MCP-only agent setups), run the dedicated setup script — it downloads the pinned collector + Prometheus + Tempo + Loki + Grafana binaries, renders configs, and smoke-tests the stack:

```powershell
# Windows
scripts\setup-grafana-telem.ps1
```

```bash
# Linux / macOS
scripts/setup-grafana-telem.sh
```

After it completes, open <http://localhost:3000/d/helix-overview> (default credentials `admin` / `admin`, rotate on first login). The script is idempotent; re-running it refreshes configs without re-downloading.

> **Advanced — Docker stack:** if you prefer a full Docker-compose observability stack (Prometheus, Tempo, Loki, Grafana), see [deploy/otel/README.md](deploy/otel/README.md).

## Architecture

| Doc | Topic |
| --- | --- |
| [PIPELINE_LANES.md](docs/architecture/PIPELINE_LANES.md) | Swim-lane reference: ingest, context, packet, fingerprint flows |
| [DIMENSIONS.md](docs/architecture/DIMENSIONS.md) | The 9 retrieval dimensions — schema, data, bench status |
| [LAUNCHER.md](docs/architecture/LAUNCHER.md) | Supervisor, tray, observability stack lifecycle |
| [SESSION_REGISTRY.md](docs/architecture/SESSION_REGISTRY.md) | Multi-agent session + party isolation |
| [OBSERVABILITY.md](docs/architecture/OBSERVABILITY.md) | Prometheus metrics, Grafana dashboards, alert rules |
| [KNOWLEDGE_GRAPH.md](docs/architecture/KNOWLEDGE_GRAPH.md) | Entity graph, co-activation edges, Hebbian co-activation |

## Gotchas

- **Model swap latency**: the compressor (small model) and the generation model share Ollama. Use `keep_alive = "30m"` in helix.toml to pin the compressor in memory.
- **Synonym map is critical**: if queries return "no relevant context", check that query keywords map to the tags the compressor assigned. Add synonyms in `[synonyms]` of helix.toml.
- **Short content may fail ingestion**: the compressor struggles with very short inputs (<200 chars). Pad with context or combine small files before ingesting.
- **`genome.db` persists**: delete it to start fresh. It auto-creates on first use.
- **Continue Agent mode**: use Chat mode, not Agent mode. The proxy does not handle tool routing.
- **`know`/`miss` block requires the agent prompt fragment** to be honored — without it, frontier models confabulate. Import `helix_context.agent_prompt.full_fragment()` and prepend it to your system prompt.
- **Stage 2 backfill is a one-time post-merge action** — `embedding_dense_v2 IS NULL` until you run `scripts/backfill_bgem3_v2.py`. Symptom: `/context` retrieval rate plateaus low; check coverage via `sqlite3 genome.db "SELECT COUNT(*) FROM genes WHERE embedding_dense_v2 IS NOT NULL"`.
- **Default `[retrieval] fusion_mode = "additive"` is back-compat**; flip to `"rrf"` after running `scripts/calibrate_thresholds.py` so the absolute-floor gates do not strand every query in BROAD.
- **Default `[abstain].mode = "global"` is back-compat**; flip to `"per_classifier"` after calibration to use the bench-derived floors.

## Testing

```bash
# All mock tests (no Ollama needed, ~6s)
python -m pytest tests/ -m "not live" -v

# Live tests (requires Ollama running)
python -m pytest tests/ -m live -v -s

# Full suite
python -m pytest tests/ -v
```

The 7-stage retrieval-fix added Stage-by-Stage contract tests:
`tests/test_dense_recall.py` (Stage 2), `tests/test_fusion_rrf.py` (Stage 3),
`tests/test_calibration.py` (Stage 4), `tests/test_caller_model_class.py` (Stage 5),
`tests/test_know_miss_block.py` (Stage 6), `tests/test_freshness_gate.py` (Stage 7).

## Acknowledgments

Built on: [spaCy](https://spacy.io/) NER · [Howard 2005](https://doi.org/10.1037/0033-295X.112.3.559) TCM · [Stachenfeld 2017](https://www.nature.com/articles/nn.4650) SR · SQLite FTS5 BM25 · [Kompress](https://huggingface.co/chopratejas/kompress-base) · [Headroom](https://github.com/chopratejas/headroom)

Licensed under [Apache-2.0](LICENSE). See [NOTICE](NOTICE) for third-party attributions.

## Further reading

- [Setup guide](docs/SETUP.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [`/context` API reference](docs/api/context-endpoint.md)
- [Operator runbooks](docs/operator-runbooks.md)
- [Config reference](docs/config-reference.md)
- [Agent SDK integration](docs/agent-sdk-fragment.md)
- [Environment variables](.env.example)
- [Agentome paper](https://mbachaud.substack.com/p/agentome)

# Getting Started

Install, ingest, query, verify — then pick one of three workflows. This page
is the condensed path; the canonical install document with every nuance is
[`docs/SETUP.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/SETUP.md).

## Requirements

- **Python 3.11–3.13.** The wheel classifies exactly these three. 3.14 works
  in practice but is not classified — `torch` and `pystray` wheels lag
  upstream Python by months, so 3.14 may force a source build. Pin 3.12 or
  3.13 if you have a choice.
- **SQLite with FTS5.** Bundled on Windows and Linux with CPython 3.11+.
  **macOS: the system SQLite that ships with Apple Python often lacks FTS5** —
  install a current SQLite via Homebrew and make sure your Python links it.
  Without FTS5 the BM25 tier is unavailable and `cymatix ingest` fails at
  document-table init.

  ```bash
  python -c "import sqlite3; c = sqlite3.connect(':memory:'); \
    c.execute('CREATE VIRTUAL TABLE t USING fts5(x)'); print('ok')"
  ```

- **Ollama** (reachable at `http://localhost:11434`) — the default chat
  upstream for the OpenAI-compatible proxy, and one of the optional compressor
  (legacy: ribosome) backends. The retrieval path makes no model call, so with
  the compressor disabled (the shipped default) the CLI and `/context` work
  without it. Verify with `curl -s http://localhost:11434/api/tags` — the
  response must be JSON.
- **Disk.** A full `[all]` install pulls roughly 2 GB (dominated by `torch`,
  `sentence-transformers`, `headroom-ai`, tree-sitter parsers). The lean
  proxy-only path (`embeddings,cpu`) is roughly 150 MB. The knowledge store
  (legacy: genome) grows linearly with corpus size — an 18.5k-document store
  is ~250 MB.

## Which extras to install

Core install is dependency-light (FastAPI + SQLite, no torch). Most operators
install two or three extras, not `[all]`.

| Extra | Enables | Install when |
|---|---|---|
| *(core)* | HTTP server, `/context`, `/context/packet`, FTS5 retrieval | Always — it is the base wheel |
| `cpu` | spaCy NER ingest tagging (`[ingestion] backend = "cpu"`, the default) | You are ingesting anything at all |
| `embeddings` | `numpy` + `sentence-transformers`: the SEMA codec and the BGE-M3 dense recall path | You opt in to dense recall or cold-tier retrieval (both default-off) |
| `mcp` | `python -m cymatix_context.mcp_server` | Claude Code, Codex, Gemini CLI, Antigravity, or any MCP host |
| `ast` | Tree-sitter chunking for Python / Rust / JS / TS | You ingest source code, not just prose |
| `nli` | Standalone `torch` + `transformers` for the DeBERTa backend | You flipped the compressor backend to `"deberta"` |
| `otel` | OpenTelemetry SDK + OTLP gRPC exporter + FastAPI instrumentation | You want the Grafana dashboards to have data |
| `launcher` | `cymatix-launcher` / `cymatix-status` console scripts, supervisor | Any flow that uses the tray or the batch launchers |
| `launcher-native` | `launcher` plus `pywebview` native-window dashboard | You want a desktop window and cannot take LGPL deps |
| `launcher-tray` | `launcher` plus `pystray` + Pillow + pywin32 (Windows) — the system-tray icon | Daily-driver desktop flow. `pystray` is **LGPL-3**, runtime-only optional |
| `codec` | `headroom-ai[proxy,code]` — the CPU-resident compression proxy + dashboard | You want the Headroom proxy lifecycle |
| `accel` | `orjson` fast path on `/context` and `/ingest` | Small always-on perf win |
| `scorerift` | The ScoreRift companion package | You run the ScoreRift integration (most do not) |
| `all` | The full feature surface minus `dev` (contributor-only), `launcher-tray` (LGPL), and `scorerift` (separate package); ~2 GB | You want everything Apache-2.0-clean and do not care about disk |

Two things that have bitten operators:

- `[all]` does **not** include `launcher-tray` (LGPL) or `scorerift`
  (separate package). Add `,launcher-tray` explicitly if you want the tray.
- `[embeddings]` transitively pulls `torch`. Per `docs/SETUP.md`: if you do
  not need semantic retrieval you do not need torch — skip the extra and
  accept that the ΣĒMA-cosine tier and dense recall become no-ops. Note the
  documented lean install set is still `[embeddings,cpu]`, because
  `docs/TROUBLESHOOTING.md` records the proxy as requiring `embeddings` for
  the SEMA cache; drop it only if you are running CLI-only and can test the
  result.

Full matrix with the exact `pyproject.toml` line numbers:
[`docs/SETUP.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/SETUP.md).

## Quickstart

```bash
# 1. Install (README's recommended working set)
pip install "cymatix-context[embeddings,cpu,mcp]"

# 2. Install the spaCy pipeline — pip never pulls it, and ingest fails without it
python -m spacy download en_core_web_sm

# 3. Ingest your project
cymatix ingest path/to/your/project/ --recursive

# 4. Query — no server, no daemon
cymatix query "how does the splice step work?"
```

For a headless proxy instead of the CLI, the lean set is
`pip install -e ".[embeddings,cpu]"` (~150 MB) and then `cymatix-server`.

## First ingest

- **The spaCy pipeline is a hard requirement of the ingest path.** The `cpu`
  extra installs the spaCy *library*; `en_core_web_sm` is a separate artifact.
  Without it every `/ingest` POST returns HTTP 422 and nothing is ingested
  ([#313](https://github.com/mbachaud/Cymatix-Context/issues/313)). Pin the
  build if you need reproducibility:
  `python -m spacy download en_core_web_sm-3.8.0 --direct`.
- **Extension filter.** `cymatix ingest` walks top-level only unless you pass
  `--recursive`, and filters to `.txt .md .rst .py .ts .js .json .toml .yml
  .yaml` by default. Repeat `--ext` to add more; single files are filtered too.
- **The synonym map is the most common cause of "no relevant context".**
  Retrieval matches query keywords against the tags assigned at ingest time.
  If your vocabulary differs from your corpus's, add mappings under
  `[synonyms]` in `cymatix.toml` (for example `cache` → `redis`, `ttl`,
  `invalidation`) before concluding that retrieval is broken.
- **Where the store lands.** Default path is `genomes/main/genome.db`,
  relative to the run directory — not the project root. Override per process
  with `CYMATIX_STORE_PATH` (legacy: `CYMATIX_GENOME_PATH`), or in config:

  ```toml
  [knowledge_store]                     # legacy spelling: [genome]
  path = "genomes/main/genome.db"
  ```

- **Dense vectors are not written at ingest by default**
  (`[ingestion] dense_embed_on_ingest = false` since 2026-08-19,
  [#371](https://github.com/mbachaud/Cymatix-Context/issues/371)). If you
  later opt in to dense recall, run
  `python scripts/backfill_bgem3_v2.py genomes/main/genome.db` — the dense
  tier retrieves nothing until the column is populated.

## Verify

```bash
cymatix status                # store reachable + config valid (+ optional server probe)
cymatix diag corpus           # corpus shape: documents, fragments, tiers, compression ratio
curl -s http://127.0.0.1:11437/health | python -m json.tool
```

- `cymatix status` runs three checks — knowledge store reachable with a
  document count ≥ 0, config valid, and an optional HTTP-server / launcher
  probe (skip it with `--no-network`). Exit `0` healthy, `3` if the store or
  config check failed.
- `cymatix diag corpus` reports total documents, total fragments, the tier
  distribution (open / euchromatin / heterochromatin), the compression ratio,
  and best-effort staleness.
- `/health` reports the model, the document count, the upstream URL, and
  `observability.exporter` — `"otlp_grpc"` means telemetry is wired, `"noop"`
  means `CYMATIX_OTEL_ENABLED=1` but the `otel` extra is missing, and absent
  means telemetry is off (the shipped default).

A `Connection refused` on `/health` just means no server is running — the CLI
workflow below does not need one.

## Three workflows

### CLI only — no server, no daemon

- Every command runs the pipeline in-process against the store on disk, so
  there is nothing to start and nothing to keep warm.
- All the agent-facing commands take `--json` and emit the same shapes as the
  MCP and HTTP surfaces, so a subprocess-driving agent can swap freely:
  `cymatix query`, `cymatix packet`, `cymatix refresh-targets`,
  `cymatix document get` (legacy: `cymatix gene get`), `cymatix neighbors`.
- Operator commands: `cymatix ingest`, `cymatix status`,
  `cymatix diag corpus`, `cymatix config show`.
- Because there is no daemon, each invocation pays its own start-up. Prefer
  the proxy if you are issuing many queries in a tight loop.
- Reference: [CLI](CLI).

### HTTP proxy — IDE and OpenAI-compatible clients

- Start it with `cymatix-server` (binds `127.0.0.1:11437`), or directly:
  `python -m uvicorn cymatix_context._asgi:app --host 127.0.0.1 --port 11437`.
- Point any OpenAI-compatible client at it with no code change:
  `OPENAI_BASE_URL=http://localhost:11437/v1 your-app`.
- The agent-safe surface is `POST /context` and `POST /context/packet`:

  ```bash
  curl -s http://127.0.0.1:11437/context/packet \
    -H "content-type: application/json" \
    -d '{"query": "how does the freshness gate demote stale docs?"}'
  ```

- In Continue IDE, use **Chat mode, not Agent mode** — the proxy does not
  handle tool routing.
- A loopback bind is **not** authentication: any process on the host can reach
  the port. Set `[server] admin_token` to gate `/admin/*`, `/ingest`, and
  `/consolidate`. Reference: [HTTP API](HTTP-API), [Configuration](Configuration).

### MCP — Claude Code, Codex, Gemini CLI, Antigravity

- Install the `mcp` extra, run the backend, then register the shim:

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

- The server self-identifies as `cymatix`, so client tools appear as
  `mcp__cymatix__*`.
- Set `CYMATIX_AGENT`, `CYMATIX_USER`, and `CYMATIX_PARTY_ID` in that `env`
  block so multi-agent sessions do not collide on the same attribution
  identity.
- **The consuming agent must import the prompt fragment.** Without it, capable
  models paint over `do_not_answer_from_genome=true` and answer from their
  training prior. Prepend `cymatix_context.agent_prompt.full_fragment()` to
  the system prompt. See [Agent Contract](Agent-Contract).
- Reference: [MCP and IDE Integration](MCP-and-IDE-Integration), and the
  per-host guides under [`docs/clients/`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/clients/cymatix-context.md).
  Direct MCP needs neither the model proxy nor the tray.

## When something goes wrong

Start at [Troubleshooting](Troubleshooting) — port conflicts, missing extras,
malformed config, an unreachable upstream, a locked knowledge store, and the
empty-`/context` cases are all indexed there by symptom.

## Go deeper

- [`docs/SETUP.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/SETUP.md) — full extras matrix, tray flow, env-var table, uninstall
- [`docs/TROUBLESHOOTING.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/TROUBLESHOOTING.md) — symptom / cause / fix / verify / prevention for each failure mode
- [`docs/config-reference.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/config-reference.md) — every setting and every env override
- [`docs/clients/cli.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/clients/cli.md) — the operator CLI reference
- [`docs/operator-runbooks.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/operator-runbooks.md) — backfill and calibration procedures with timings
- Next: [Configuration](Configuration) · [CLI](CLI) · [HTTP API](HTTP-API) · [MCP and IDE Integration](MCP-and-IDE-Integration)

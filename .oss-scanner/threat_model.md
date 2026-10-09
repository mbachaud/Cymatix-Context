# Threat model

Read by Anthropic's OSS Scanner before it audits this repository. Maintainers:
edit freely; the scanner reads this file from the default branch.

## What this project does and where untrusted input enters

Cymatix Context is a local-first retrieval layer for LLM agents. It ingests
documents into a SQLite knowledge store (`cymatix_context/knowledge_store.py`,
`cymatix_context/storage/`) and serves compressed context back over a FastAPI
HTTP server (`cymatix_context/server/`, default `127.0.0.1:11437`), an
OpenAI-compatible proxy (`POST /v1/chat/completions`), a stdio MCP server
(`cymatix_context/mcp/`) and the `cymatix` CLI (`cymatix_context/cli/`).

Treat as untrusted:

- **Document content and metadata** from any ingest path: `POST /ingest`,
  `cymatix ingest` on a directory, Open Knowledge Format bundles
  (`cymatix_context/okf/`, YAML frontmatter and cross-links), delta-synced
  folders (`[sync]`, `cymatix_context/sync/`, `mem_sync.py`), chat
  messages persisted by the proxy, and file paths/names found inside those sources.
- **Query text and JSON bodies** on every HTTP route and MCP tool, including
  the FTS5 query built from them.
- **Requests that reach the loopback server from a browser** (a hostile web
  page posting to `127.0.0.1:11437`, DNS rebinding). The default config has
  no auth (`[server] admin_token = ""`), so this is a real path to the server.
- **Upstream LLM responses** relayed by the proxy.

Trusted: the operator's `cymatix.toml`, environment variables, model files
named in config (e.g. the `[plr] model_path` joblib file), and the store file
itself as written by this code. Other users on the same host are not a
security boundary unless `admin_token` is set.

## Components that matter most / least

Most important:

- `cymatix_context/server/` (routes, `helpers.py` admin-token guard) and
  `cymatix_context/mcp/`.
- Ingest and parsing: `codons.py`, `encoding/`, `tagger.py`, `okf/`, `sync/`,
  code, and anything that turns document-supplied names into filesystem paths.
- `vault/` (Obsidian export writes files derived from document data; must stay
  under the vault root, see `vault/schema.py:safe_resolve_under`).
- SQL and FTS5 query construction in `knowledge_store.py`, `storage/`,
  `retrieval/`.

Lower priority but in scope: `launcher/` (desktop tray supervisor, mostly
Windows), `encoder_daemon.py`, `telemetry/`.

Out of scope: `benchmarks/`, `training/`, `scripts/`, `tools/`, `desktop/`,
`examples/`, `docs/`, `wiki/`, `deploy/` (research, bench and packaging
material, not shipped in the wheel), and third-party dependencies unless this
project uses them unsafely.

## How to exercise it

- `python -m pytest tests/ -m "not live" -q` runs the offline suite (no model
  or network needed).
- `cymatix ingest <dir> --recursive`, `cymatix ingest --okf <bundle>`,
  `cymatix query "<text>"`.
- `cymatix-server` (or `python -m uvicorn cymatix_context._asgi:app --port
  11437`) for the HTTP surface; `fastapi.testclient.TestClient` against
  `cymatix_context.server.create_app()` works offline, as the tests show.
- The store defaults to `genomes/main/genome.db`; deleting it starts fresh.

## How you rate severity

- **Critical**: code execution, or arbitrary file write, triggered by ingested
  content or by an HTTP/MCP request (including one sent cross-origin from a
  browser to the loopback server).
- **High**: arbitrary file read or path traversal outside configured roots;
  reading or exfiltrating store contents cross-origin from a browser; bypass
  of `admin_token` when it is set; SQL injection.
- **Medium**: denial of service from a single crafted document or request
  (unbounded CPU, memory or disk, crash loops, store corruption); secrets
  leaking into logs, traces or the vault export.
- **Low**: issues that need a same-host local user while no `admin_token` is
  set, or that need a malicious `cymatix.toml` or model file.

## Anything to leave alone

- Prompt injection in ingested documents being passed through to the LLM is
  expected behaviour (the product delivers document text to agents), not a
  vulnerability, unless it makes Cymatix itself take an action.
- The unauthenticated admin routes under the default loopback bind are
  documented; report them only with a concrete cross-origin or remote path.
- Loading a model file named in config (joblib/torch) is trusted by design.

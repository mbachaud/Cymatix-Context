# Helix CLI — operator reference

`helix` is the cold-start command-line surface for the Helix Context
genome. Every invocation is a fresh Python process: it opens the
SQLite genome (read-only by default), runs the requested operation,
and exits. No daemon, no long-lived state. The daemon design (see
`docs/architecture/HELIX_DAEMON_DESIGN.md`) is parked until walk-bench
numbers come in.

## Install

```bash
pip install helix-context
helix --help
```

The legacy FastAPI launcher is still available as `helix-server` (or
`python -m uvicorn helix_context._asgi:app`).

## Title page — what the CLI does for an agent vs. an operator

| Use case | Reach for |
|---|---|
| Agent: "what does X mean in this codebase?" | `helix query` |
| Agent about to edit a file: "is what I know fresh?" | `helix packet --task-type edit` |
| Agent before a destructive op: "what should I reread first?" | `helix refresh-targets` |
| Agent: "show me document gene-abc123" | `helix gene get` / `helix gene preview` |
| Agent: "what else is semantically near my query?" | `helix neighbors` |
| Operator: "is the genome healthy?" | `helix status`, `helix diag corpus` |
| Operator: "what config is actually loaded?" | `helix config show` |
| Operator: "add a file to the genome" | `helix ingest` |
| Operator (legacy): "run the FastAPI proxy" | `helix-server` (separate entry point) |

## Agent-driven walk surface (v1.x)

The four commands below are the **agent-facing** retrieval surface —
the same operations the MCP tools `helix_context`, `helix_context_packet`,
`helix_refresh_targets`, `helix_gene_get`, and `helix_neighbors` expose,
but reachable from the CLI without an MCP host or a running HTTP server.
An agent can drive a full retrieval-and-walk loop (query → packet → drill
into specific genes → fetch neighbors → refresh-targets before acting)
entirely through subprocess calls. All four default to JSON when `--json`
is passed; the shape is identical to the matching MCP / HTTP surfaces so
callers can swap freely.

### `helix query "<text>" [--k N] [--json] [--tier focused] [--learn]`

Run the retrieval pipeline once and print the result.

- `--k N` — cap on returned documents. Default: honor the static
  config (`[budget] max_genes_per_turn` in helix.toml).
- `--json` — emit `to_agent_json()` shape (verdict / evidence /
  expressed_context / estimated_tokens / decision_reason / next_action).
  This is the format the walk-bench harness expects.
- `--tier focused` — walk-tier hint. Maps to internal `decoder_mode=
  condensed` (fewer genes, tighter). v1 only exposes `focused` because
  it is the only spec-vocab value the internal decoder honors today.
  The full bench-spec vocabulary (`broad` / `focused` / `tight`) lands
  in v1.1 alongside the corresponding decoder modes; until then, passing
  any value other than `focused` is rejected by argparse with exit 2.
- `--learn` — replicate the query back into the genome (default off,
  so repeated CLI calls never silently mutate state).

Exit codes: `0` success, `1` pipeline error.

### `helix packet "<text>" [--task-type T] [--max-genes N] [--include-raw] [--json]`

Build a freshness-labeled agent-safe evidence bundle. Use this instead
of `helix query` when an agent is about to take a high-risk action (edit,
ops, debug) — the packet returns evidence labeled `verified` / `stale_risk`
plus an explicit `refresh_targets` reread plan, rather than raw bytes that
may be stale.

- `--task-type` ∈ `{plan, explain, review, edit, debug, ops, quote}`.
  Default `explain`. Higher-risk types apply stricter freshness +
  coordinate-confidence gates.
- `--max-genes N` — retrieval top-K (default 8).
- `--include-raw` — emit full `gene.content` per item (48k cap) instead
  of the compressor-compressed summary. Use when the packet is the only
  context source and the downstream model needs real bytes.
- `--json` — emit the full `ContextPacket` shape: `task_type`, `query`,
  `verified[]`, `stale_risk[]`, `refresh_targets[]`, `coordinate_confidence`,
  `file_coverage`, `know` / `miss`, `notes`. Identical to the
  `POST /context/packet` endpoint response.

Exit codes: `0` success, `1` builder error.

### `helix refresh-targets "<text>" [--task-type T] [--max-genes N] [--json]`

Return only the reread plan (no evidence items). Cheaper than a full
packet when the caller already has content cached and just wants to know
which sources are stale enough to require a reread before acting.

- `--task-type` — defaults to `edit` (the usual caller).
- `--max-genes N` — retrieval top-K (default 8).
- `--json` — `{ refresh_targets: [...], count: int }`. Identical shape
  to the `POST /context/refresh-plan` endpoint.

Exit codes: `0` success, `1` builder error.

### `helix gene get <id> [--json]` / `helix gene preview <id> [--chars N] [--json]`

Inspect a single document by ID. `get` returns the full `Gene` model
(content, tags, signals, fragments, lifecycle tier, embedding). `preview`
returns a content-only char-capped snippet (default 240 chars) for cheap
relevance checks.

- `get --json` — full `Gene.model_dump()`.
- `preview --chars N` — preview character budget.
- `preview --json` — `{ gene_id, preview, truncated, total_chars, path }`.

Exit codes: `0` success, `1` unknown gene_id or read failure.

The subcommand name `gene` matches the legacy MCP tool (`helix_gene_get`).
The canonical engineering alias is `helix_document_get` per
[`docs/ROSETTA.md`](../ROSETTA.md); both names will continue to resolve
to the same document model.

### `helix neighbors "<text>" [--k N] [--json]`

Top-k SEMA neighbors for a query — a semantic-space graph walk. Read-only
and cheap. Used by agents that want to "look around" a result before
acting.

- `--k N` — neighbor count (default 10).
- `--json` — `{ query, k, neighbors: [{ gene_id, sema_cos_sim, preview, path }], count }`.
  Identical shape to the `/debug/neighbors` HTTP endpoint and the
  `helix_neighbors` MCP tool.

Returns `count: 0` with no error when the SEMA codec is unavailable
(e.g. the `embeddings` extra is not installed or no embeddings populated
yet). Use `helix status` or `helix diag corpus` to distinguish those
cases.

Exit codes: `0` success, `1` codec / read error.

## Operational subcommands

### `helix ingest <path> [--recursive] [--ext .EXT] [--json]`

Add a file or directory to the genome. Top-level only by default; pass
`--recursive` to walk subdirectories. The default extension filter is:
`.txt .md .rst .py .ts .js .json .toml .yml .yaml`. Repeat `--ext`
to add more. Single-file inputs are also filtered by extension —
`helix ingest binary.exe` errors instead of silently ingesting garbage.

Exit codes: `0` success, `1` file error or write failure.

### `helix status [--json] [--no-network] [--config PATH]`

Three checks: (1) genome reachable and gene_count >= 0, (2) config
valid, (3) optional HTTP server / launcher probe (skipped if
`--no-network`).

Exit codes: `0` healthy, `3` genome or config check failed.

### `helix diag corpus [--json]`

Reports corpus shape: total_genes, total_codons, tier_distribution
(open / euchromatin / heterochromatin), compression_ratio, and
best-effort staleness from the genome health summary.

Exit codes: `0` success, `1` stats call failed.

### `helix config show [--text] [--config PATH]`

Print the effective configuration (helix.toml merged with env
overrides). JSON by default; `--text` for flat `dotted.key = value`
lines with json-encoded values.

Exit codes: `0` success.

### `helix serve` (DEFERRED)

Prints a pointer at `helix-server` and exits 4. Daemon design lives
in `docs/architecture/HELIX_DAEMON_DESIGN.md`.

## Environment variables

- `HELIX_CONFIG` — path to `helix.toml`. Default: `./helix.toml`.
- `HELIX_GENOME_PATH` — overrides `[genome] path` (use `:memory:` for
  tests and ephemeral runs).

## Exit code table

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Operation failed |
| 2 | Bad CLI arguments (argparse default) |
| 3 | Status check failed (`helix status` only) |
| 4 | Subcommand deferred / not implemented |

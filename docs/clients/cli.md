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

## Subcommands

### `helix query "<text>" [--k N] [--json] [--tier broad|focused] [--learn]`

Run the retrieval pipeline once and print the result.

- `--k N` — cap on returned documents. Default: honor the static
  config (`[budget] max_genes_per_turn` in helix.toml).
- `--json` — emit `to_agent_json()` shape (verdict / evidence /
  expressed_context / estimated_tokens / decision_reason / next_action).
  This is the format the walk-bench harness expects.
- `--tier broad|focused` — walk-tier hint. `broad` = scatter-gather
  scout. `focused` = narrow converge. Maps to internal `decoder_mode`:
  `broad → broad`, `focused → condensed`. The vocabulary drift between
  spec (broad/focused/tight) and code (condensed/broad/dense) is a
  v1.1 follow-up.
- `--learn` — replicate the query back into the genome (default off,
  so repeated CLI calls never silently mutate state).

Exit codes: `0` success, `1` pipeline error.

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

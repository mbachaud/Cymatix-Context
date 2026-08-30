# CLI

- `cymatix` is the **cold-start** surface. Every invocation is a fresh Python
  process: it opens the SQLite knowledge store (read-only by default), runs one
  operation, and exits. No daemon, no long-lived state, no server required.
- That makes it the natural surface for **scripts, CI, and subprocess-driving
  agents**. An agent can run a full retrieve → verify → drill → reread loop
  without an MCP host or an HTTP port.
- **Every agent-facing command takes `--json`,** and the shapes are identical
  to the matching MCP tool and HTTP endpoint, so a caller can swap surfaces
  without changing its parser.
- **Canonical spellings come first.** `cymatix document get` and `--max-docs`
  are the software-lexicon forms; `cymatix gene get` and `--max-genes` are
  legacy aliases and remain fully supported, not deprecated.
- The long-lived HTTP surface is a separate entry point: `cymatix-server`. See
  [HTTP API](HTTP-API).

## Commands

| Command | Purpose |
|---|---|
| `cymatix query "<text>"` | Run the retrieval pipeline once and print the result |
| `cymatix packet "<text>"` | Freshness-labeled agent-safe evidence bundle: `verified` / `stale_risk` items plus a `refresh_targets` reread plan |
| `cymatix refresh-targets "<text>"` | The reread plan only, no evidence items |
| `cymatix document get <id>` *(legacy: `cymatix gene get`)* | Full document model — content, tags, signals, fragments, lifecycle tier, embedding |
| `cymatix document preview <id>` *(legacy: `cymatix gene preview`)* | Content-only char-capped snippet for a cheap relevance check |
| `cymatix neighbors "<text>"` | Top-k SEMA neighbors for a query — a semantic-space graph walk |
| `cymatix ingest <path>` | Add a file or directory to the knowledge store |
| `cymatix status` | Store reachable, config valid, optional server probe |
| `cymatix diag corpus` | Corpus shape: documents, fragments, tier mix, compression ratio, staleness |
| `cymatix config show` | The effective configuration — TOML merged with env overrides |
| `cymatix serve` | Deferred. Prints a pointer at `cymatix-server` and exits **4** |

`cymatix document` and `cymatix gene` resolve to the same module, so
`cymatix document get abc123 --json` and `cymatix gene get abc123 --json` are
byte-identical in behavior.

### Which one to reach for

| You want to… | Command |
|---|---|
| Understand what a term means in this codebase | `cymatix query` |
| Check whether what you know is still fresh, before editing | `cymatix packet --task-type edit` |
| Know what to reread before a destructive operation | `cymatix refresh-targets` |
| Inspect one specific document | `cymatix document get` / `cymatix document preview` |
| Look around a result before acting | `cymatix neighbors` |
| Check that the store is healthy | `cymatix status`, `cymatix diag corpus` |
| See what config actually loaded | `cymatix config show` |

## Flags that matter

**`cymatix query "<text>" [--k N] [--json] [--tier focused] [--learn]`**

- `--k N` — cap on returned documents. Omitted, it honors
  `[budget] max_docs_per_turn` (legacy `max_genes_per_turn`, default 12).
- `--tier focused` — walk-tier hint, mapping to the internal
  `decoder_mode = "condensed"`. **`focused` is the only accepted value today**;
  the full `broad` / `focused` / `tight` vocabulary lands in v1.1, and anything
  else is rejected by argparse with exit 2.
- `--learn` — replicate the query back into the store. **Off by default**, so
  repeated CLI calls never silently mutate state.

**`cymatix packet "<text>" [--task-type T] [--max-docs N] [--include-raw] [--json]`**

- `--task-type` ∈ `{plan, explain, review, edit, debug, ops, quote}`, default
  `explain`. Higher-risk types apply stricter freshness and
  coordinate-confidence gates — the same query can come back `verified` under
  `explain` and `needs_refresh` under `edit`.
- `--max-docs N` *(legacy `--max-genes`)* — retrieval top-K, default **8**.
  Both spellings share one argparse destination, so passing both is plain
  last-wins; the config-surface "legacy wins" collision rule does not apply
  here.
- `--include-raw` — full document content per item instead of the compressed
  thumbnail. Use it when the packet is the only context source and the
  downstream model needs real bytes.

**`cymatix refresh-targets "<text>" [--task-type T] [--max-docs N] [--json]`** —
same flags, but `--task-type` defaults to `edit` (the usual caller).

**`cymatix document preview <id> [--chars N] [--json]`** — `--chars` sets the
preview budget, default 240.

**`cymatix neighbors "<text>" [--k N] [--json]`** — `--k` defaults to 10.
Returns `count: 0` with **no error** when the SEMA codec is unavailable — the
`embeddings` extra is missing, or no embeddings are populated. Use
`cymatix status` or `cymatix diag corpus` to tell those cases apart.

**`cymatix ingest <path>`**

| Flag | Effect |
|---|---|
| `--recursive` / `-r` | Walk subdirectories. Without it, top-level files only |
| `--ext .EXT` | Add an extension. Repeatable. Default set: `.txt .md .rst .py .ts .js .json .toml .yml .yaml` |
| `--exclude-dir NAME` | Skip a directory name during a recursive walk. Repeatable; adds to the default set |
| `--no-default-excludes` | Walk vendored and generated trees too (`.venv`, `node_modules`, `.git`, `__pycache__`, `dist`, `build`, …). Off by default — ingesting a virtualenv poisons the store with third-party source |
| `--okf` | Treat the path as an OKF v0.1 knowledge bundle directory. Walks every non-reserved `.md` file itself; `--recursive` / `--ext` do not apply |
| `--bundle-id ID` | Override the OKF bundle id (default: the directory name). `--okf` only |
| `--deterministic` | Deterministic-ingest profile: no SEMA / dense / SPLADE encodes at ingest, so the store holds no float tensors. `--okf` only |

Single-file inputs are extension-filtered too — `cymatix ingest binary.exe`
errors rather than silently ingesting garbage.

## The `--json` agent workflow

The four retrieval commands compose into a walk an agent can drive entirely
through subprocess calls:

```bash
# 1. What do we know?
cymatix query "how does the freshness gate demote stale docs?" --json

# 2. About to edit — is what we know still true?
cymatix packet "edit the freshness gate" --task-type edit --json

# 3. Drill into a specific document the packet named
cymatix document get c084a6dc1f2b --json

# 4. Look around it
cymatix neighbors "freshness gate" --k 10 --json

# 5. Before acting: what must be reread first?
cymatix refresh-targets "edit the freshness gate" --json
```

The shapes:

| Command | `--json` shape |
|---|---|
| `query` | `verdict` (`know` / `miss` / `unknown`), `expressed_context`, `evidence` (document ids), `estimated_tokens`, `decision_reason`, `next_action`, plus the `know` **or** `miss` block |
| `packet` | The full `ContextPacket` — `task_type`, `query`, `verified[]`, `stale_risk[]`, `contradictions[]`, `refresh_targets[]`, `coordinate_confidence`, `file_coverage`, `know` / `miss`, `notes`. Identical to `POST /context/packet` |
| `refresh-targets` | `{ refresh_targets: [...], count: int }`. Identical to `POST /context/refresh-plan` |
| `document get` | The full document model |
| `document preview` | `{ gene_id, preview, truncated, total_chars, path }` |
| `neighbors` | `{ query, k, neighbors: [{ gene_id, sema_cos_sim, preview, path }], count }`. Identical to `GET /debug/neighbors` |

Two things to branch on, in order:

1. **`verdict` / the `miss` block.** `miss` carries
   `do_not_answer_from_genome: true` and is load-bearing — do not answer from
   the store. `next_action` is the pre-composed hint (`answer_from_evidence` on
   a know; the escalate or refresh plan on a miss).
2. **`decision_reason`.** One string explaining *why* the verdict landed where
   it did, so a failing run is diagnosable without re-reading the bytes.

*The JSON keeps legacy field names on the wire — `gene_id` in the preview and
neighbor shapes above is verbatim, not a typo. The prose and the config surface
moved to software terms; renaming the wire contract is a tracked Tier-3 change,
deliberately not taken in 0.9.1. See [Lexicon](Lexicon).*

## `cymatix status`

Three checks, then a single next-action line:

```
Genome: up (genomes/main/genome.db)
  gene_count: 18547
Config: valid (cymatix.toml)
Server: up (http://127.0.0.1:11437)

Next action: ...
```

- **Store** — opens the `.db` read-only (URI mode, so it never takes a lock)
  and counts rows. A missing file reports `file_not_found` with the fix
  (`cymatix ingest <path>`); a non-Cymatix file reports
  `not_a_cymatix_genome`.
- **Config** — confirms `cymatix.toml` loads, and echoes the resolved store
  path and server port. This is the fastest way to check that a
  `CYMATIX_STORE_PATH` override actually took.
- **Server** — an optional HTTP / launcher probe. `--no-network` skips it; a
  `down` server is not a failure for the CLI workflow.

Flags: `--json`, `--no-network`, `--config PATH`. Exit **0** healthy,
**3** if the store or config check failed.

The rendered labels above (`Genome:`, `gene_count:`) are the literal strings the
command prints today — the CLI's own text output has not been through the
lexicon pass. Read them as "knowledge store" and "document count".

## `cymatix diag corpus`

```
total_genes: 18547
total_codons: 91234
compression_ratio: 5.0
tier_distribution:
  open: 14200
  euchromatin: 3100
  heterochromatin: 1247
staleness:
  ...
```

- `total_genes` / `total_codons` — documents and fragments.
- `tier_distribution` — the lifecycle mix. `open` is full content, `euchromatin`
  is summarized, `heterochromatin` is cold (content preserved but demoted by
  the density gate, and only reachable through the opt-in cold tier).
- `compression_ratio` — raw characters over compressed characters across the
  whole store.
- `staleness` — best-effort, from the store's health summary. Prints
  `(unavailable)` when the summary cannot be computed rather than failing.

**Fixed in 0.9.1:** the tier counts were previously reported as zero. The CLI's
stats projection read `chromatin_open` / `chromatin_euchromatin` /
`chromatin_heterochromatin`, but `/stats` emits `open` / `euchromatin` /
`heterochromatin`. The projection now accepts both, so `cymatix diag corpus`
and `cymatix status` show the real distribution. If you have an old script
asserting zeros here, it will start seeing real numbers.

Exit **0** success, **1** if the stats call failed.

## No daemon — what that costs

- Each invocation pays its own process start-up and store open. For a handful
  of calls that is the right trade; for a tight query loop, run
  `cymatix-server` and hit [HTTP API](HTTP-API) instead.
- `[hardware] lazy_encoders = true` (the default) keeps the cost down by
  deferring heavy encoder construction to first use — and at 0.9.x defaults the
  SEMA and dense encoders are never constructed on the retrieval path at all.
- `cymatix --help` deliberately does not import the retrieval stack:
  subcommand modules import their dependencies lazily inside `run()`.
- Daemon mode (`cymatix serve`) is deferred with no design doc. It exits **4**
  and points at `cymatix-server`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Operation failed |
| 2 | Bad CLI arguments (argparse default) |
| 3 | Status check failed (`cymatix status` only) |
| 4 | Subcommand deferred / not implemented |

## Environment

- `CYMATIX_CONFIG` — path to `cymatix.toml`. Default `./cymatix.toml`.
- `CYMATIX_STORE_PATH` *(legacy `CYMATIX_GENOME_PATH`, which wins on conflict)*
  — overrides the store path. Use `:memory:` for tests and ephemeral runs.

Full env table: [Configuration](Configuration).

## When `cymatix` is not on PATH

The console scripts (`cymatix`, `cymatix-server`, `cymatix-status`,
`cymatix-vault`, `cymatix-launcher`) are generated at install time and hardcode
the absolute interpreter path. Two recoveries:

```bash
# The script resolves to a deleted editable-install path (you moved the source tree)
pip install --force-reinstall --no-deps cymatix-context

# Or bypass the console script entirely — this always works
python -m cymatix_context.cli --help
```

## Go deeper

- [`docs/clients/cli.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/clients/cli.md) — the operator reference: per-command detail, install recovery, the full exit-code contract
- [`docs/config-reference.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/config-reference.md) — every setting the CLI reads
- [`docs/operator-runbooks.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/operator-runbooks.md) — backfill and calibration procedures with timings
- Next: [Getting Started](Getting-Started) · [HTTP API](HTTP-API) · [MCP and IDE Integration](MCP-and-IDE-Integration) · [Troubleshooting](Troubleshooting)

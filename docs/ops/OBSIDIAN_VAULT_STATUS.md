# Obsidian Vault Export — Operator Status

Obsidian vault export is shipped as **v1** (read-only export + diagnostic traces).

**Since:** cymatix-context v0.5.0 (PR #37, merged 2026-05-07)

## v1 capabilities (current)

What works today:

- **Full and incremental export** of the knowledge store to an Obsidian-compatible
  markdown vault (`genes/<domain>/<stem>-<id>.md`, one file per `gene_id`).
- **Diagnostic traces** of `/context` calls auto-exported to `_traces/`, TTL-pruned
  (default 48h, hard cap configurable).
- **Pin / unpin** traces — pinned traces live in `_traces-pinned/` and are immune
  to TTL prune (still subject to `vault.traces.max_retention_hours_hard`).
- **`/vault/status`** HTTP endpoint and `cymatix-vault status` CLI — file counts per
  folder, disk bytes, last export timestamps.
- **`/export/obsidian`** HTTP endpoint and `cymatix-vault export {--full,--incremental}`
  CLI for on-demand exports.
- **`/vault/trace`** HTTP endpoint to capture a single-request diagnostic trace
  on demand.
- **`_stale/`** read-only view of genes below `vault.stale_threshold`.
- **Fan-out migration** when a domain folder exceeds `vault.fan_out_threshold`.
- **Vault file-count Prometheus gauge** per folder.

Authored frontmatter fields (`operator_notes`, `operator_tags`, `pinned`,
`supersedes`, ...) render as cosmetic placeholders for forward-compat with v1.1.
**Edits in Obsidian are NOT synced back in v1.**

## v1.1+ deferred

Not yet implemented:

- File **watcher** for authored-field changes in Obsidian.
- **Delta-sync** of operator edits back into the knowledge store.
- **Authored-field writeback** via watcher + validator pipeline.

Tracking design lives in
[`docs/archive/superpowers/specs/2026-05-06-obsidian-vault-export-full-design-v1.1plus.md`](../archive/superpowers/specs/2026-05-06-obsidian-vault-export-full-design-v1.1plus.md).

## Smoke-test path

1. Enable in `cymatix.toml`:

   ```toml
   [vault]
   enabled = true
   path = "~/.helix/vault"
   # party_id = ""           # empty = server's primary party
   # redact_body = false     # set true if Obsidian Sync / iCloud watches the path

   [vault.traces]
   retention_hours = 48
   # max_retention_hours_hard = 720
   ```

2. Start the cymatix server (`cymatix-server` or `python -m uvicorn cymatix_context._asgi:app --port 11437`).

3. Run a full export and check status:

   ```bash
   cymatix-vault export --full
   cymatix-vault status
   ```

4. Run a `/context` query to generate a trace, then pin and unpin it:

   ```bash
   curl -s -X POST http://127.0.0.1:11437/context \
        -H "Content-Type: application/json" \
        -d '{"query": "what does the splice step do?"}'

   # Find the latest trace file in ~/.helix/vault/_traces/
   cymatix-vault trace --list | head
   cymatix-vault pin <trace-filename>
   cymatix-vault status     # _traces-pinned count goes up by 1
   cymatix-vault unpin <trace-filename>
   ```

If `cymatix-vault status` reports `enabled: true` and trace pin/unpin round-trip
the file between `_traces/` and `_traces-pinned/`, v1 is healthy.

## History

The original design specs are preserved under `docs/archive/superpowers/specs/`:

- [`2026-05-06-obsidian-vault-export-design.md`](../archive/superpowers/specs/2026-05-06-obsidian-vault-export-design.md) — v1 design.
- [`2026-05-06-obsidian-vault-export-full-design-v1.1plus.md`](../archive/superpowers/specs/2026-05-06-obsidian-vault-export-full-design-v1.1plus.md) — v1.1+ deferred design.

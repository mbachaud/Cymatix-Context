# Changelog

## Unreleased

- **fix(api): `open_session()` now honors `HELIX_CONFIG` / `HELIX_GENOME_PATH`.**
  Pre-fix, every cold-start CLI subcommand (query, diag corpus, packet,
  gene, neighbors, refresh-targets) called `HelixConfig()` (defaults) and
  silently created/read `./genome.db` regardless of what the operator had
  configured — so `helix status` looked at the configured genome but
  `helix query` looked at an empty one. Now routes through `load_config()`
  the same way `helix status` does. Surfaced by AI-user testing on
  `93deaf2`.
- **fix(status): bump `/health` probe timeout default 1.5s → 10s, override
  via `HELIX_STATUS_TIMEOUT_S`.** Cold-start `/health` can take 5-10s
  under model warmup + manager init + WAL replay; the old 1.5s timeout
  silently reported a healthy-but-slow server as `unreachable` in
  `helix status --json`.
- **fix(mcp): unwrap the Continue list shape in `helix_context` /
  `helix_document_query` tools.** `POST /context` returns the
  Continue-IDE HTTP context-provider list (`[{name, description, content,
  ...}]`) so the FastAPI endpoint stays drop-in compatible with Continue.
  MCP hosts validate tool returns against the declared `Dict[str, Any]`
  schema and rejected the list. New `_unwrap_context_list` helper flattens
  the single-entry list, passes error envelopes through, and wraps
  unexpected shapes with a diagnostic note.
- **fix(config): auto-fallback `ingestion.backend` → `"cpu"` when
  `ribosome.enabled = false`.** The two settings contradict each other —
  ingest with the ribosome disabled raised
  `TranscriptionError: Pack failed: Ribosome is disabled` on the first
  chunk. `load_config()` now flips ingestion to the spaCy/heuristic
  CpuTagger path and logs a WARNING. Honors explicit `cpu` / `hybrid`
  settings without override.
- **feat(cli): `python -m helix_context.cli` works as a console-script
  fallback.** Adds `helix_context/cli/__main__.py` so an agent or
  operator with a broken pip-installed `helix.exe` (deleted editable
  source path, Scripts dir off PATH) always has a module-direct
  invocation. Documented in `docs/clients/cli.md`.

- **feat(cli): agent walk-aware surface — `packet` / `gene` / `neighbors` /
  `refresh-targets`.** Four new subcommands that complete the v1 CLI as a
  full agent surface — agents drive genome lookups via subprocess CLI
  calls instead of MCP-injected context. JSON shapes match the
  corresponding HTTP endpoints (`/context/packet`, `/genes/<id>`,
  `/debug/neighbors`, `/context/refresh-plan`) and MCP tools
  (`helix_context_packet`, `helix_gene_get`, `helix_neighbors`,
  `helix_refresh_targets`) so callers can swap surfaces without changing
  call logic. Read-only by default (no genome mutation from inspection).
- **feat(api): walk-aware methods on `HelixSession`.** `gene_get`,
  `packet`, `refresh_targets`, `neighbors` — previously deferred to v1.1
  per `helix_context/api.py:352-360`, now in v1 to back the CLI surface
  above. Pure in-process wrappers over `Genome.get_gene`,
  `build_context_packet`, and the existing SEMA codec; no HTTP server
  required.
- **docs: README — agent-CLI callout + benchmark sourcing.** New "Agent
  CLI surface (no server required)" section advertises the CLI as a
  first-class agent surface alongside MCP, with the full subcommand
  surface inline. The "28.7× headline / 5.4× median" claim now cites the
  reproducer at `benchmarks/bench_rag_vs_sike_tokens.py` and the
  methodology doc at `docs/benchmarks/BENCHMARKS.md`; the overnight
  result file referenced in the README-v2 spec is documented as internal.
- **docs: ROSETTA — response-types section, dead-entry fix, ChromatinState
  note.** Adds a "Response & routing types (STAYS — no biology twin)"
  section covering `ContextWindow`, `ContextPacket`, `KnowBlock`,
  `MissBlock`, `RefreshTarget`, `ContextHealth`, `ContextItem`,
  `QueryResult`, `IngestResult`, `StatsResult`. The `HGT →
  cross_store_import` row is annotated as a forward-pointer (no code under
  either name today). The `OPEN/EUCHROMATIN/HETEROCHROMATIN →
  OPEN/WARM/COLD` row notes the rename is deferred to R3 because
  `ChromatinState` in `schemas.py` still emits the bio names.

- **feat(cli): v1 cold-start CLI shipped.** `helix query`, `helix ingest`,
  `helix status`, `helix diag corpus`, `helix config show`. Mocked unit
  tests for every subcommand plus a live `@pytest.mark.live` integration
  test against an `:memory:` genome. The legacy FastAPI launcher is now
  reachable as `helix-server` (previous `helix` entry point). Daemon
  (`helix serve`) remains deferred per `docs/architecture/HELIX_DAEMON_DESIGN.md`
  — the subcommand prints a pointer and exits 4. See `docs/clients/cli.md`
  for the operator reference.

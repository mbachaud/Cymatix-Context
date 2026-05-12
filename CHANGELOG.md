# Changelog

## Unreleased

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

# Changelog

## Unreleased

- **fix(launcher): `[headroom] route_upstream` is now a separate, default-off
  config knob; routing no longer happens implicitly from "upstream is remote".**
  Pre-fix, `_should_route_helix_upstream_via_headroom` returned True for any
  remote (non-loopback) upstream as long as `HELIX_HEADROOM_ROUTE_UPSTREAM_AUTO`
  wasn't explicitly set falsy. So an operator with `cfg.server.upstream =
  "https://api.openai.com/v1"` and `cfg.headroom.enabled = false` (defaults!)
  would have the launcher rewrite `HELIX_SERVER_UPSTREAM` to
  `http://127.0.0.1:8787` and start helix pointing at a Headroom proxy that
  was never started — every chat call then failed with ECONNREFUSED, with
  no clear diagnostic. `route_upstream` is now an explicit `[headroom]` bool
  (default `false`) gating the rewrite. `HELIX_HEADROOM_ROUTE_UPSTREAM_AUTO`
  remains as a per-launch override (truthy → on, falsy → off, unset →
  defer to config). The existing test
  `test_remote_upstream_routes_helix_via_headroom` (which pinned the buggy
  behavior) is replaced with four tests covering the new precedence rules.

- **fix(launcher): `POST /api/control/start` no longer reports success on a
  hung backend; returns `202 Accepted` with `started_pending=true`.** PR #68
  made `supervisor.start()` non-fatal on `/stats` timeout (proc left
  running so the tray's next poll picks it up). The REST handler still
  treated this as success and returned `{ok: true, pid}`, so external
  automation hitting `/api/control/start` directly couldn't distinguish
  ready from alive-but-not-ready. New `supervisor.last_start_pending`
  flag flips on the timeout path; REST surface returns 202 with a
  `started_pending: true` field and a hint to poll `/api/state` or
  `GET /stats`. Same treatment on `/api/control/restart`. Closes #72.
- **fix(hardware): summary `WARNING` line when explicit-device probe
  falls back to CPU.** The tray fires a balloon, but headless deployments
  (server, supervisor-managed, agents) miss that signal. `_detect()` now
  emits one `log.warning("Hardware fallback: requested=X active=cpu — ...")`
  alongside the per-candidate probe failures so operators tailing logs
  see the cause in line-of-sight. `auto`→cpu is unchanged (not noteworthy).
  Closes #65 SF2 — SF1, SF3, SF4 were already addressed on master
  (per-rewrite log.info, `cost_class` in `/health` + Prometheus info
  metric + startup WARN, and the WAL-bloat section in
  `docs/TROUBLESHOOTING.md` + `/admin/checkpoint` admin endpoint).

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

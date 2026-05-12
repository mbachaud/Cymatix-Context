# Changelog

## Unreleased

- **feat(cli): v1 cold-start CLI shipped.** `helix query`, `helix ingest`,
  `helix status`, `helix diag corpus`, `helix config show`. Mocked unit
  tests for every subcommand plus a live `@pytest.mark.live` integration
  test against an `:memory:` genome. The legacy FastAPI launcher is now
  reachable as `helix-server` (previous `helix` entry point). Daemon
  (`helix serve`) remains deferred per `docs/architecture/HELIX_DAEMON_DESIGN.md`
  — the subcommand prints a pointer and exits 4. See `docs/clients/cli.md`
  for the operator reference.

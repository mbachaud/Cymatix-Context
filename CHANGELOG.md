# Changelog

## Unreleased

## 0.6.2 — 2026-05-30

Make the v0.6.1 SQLite memory posture **host-aware** instead of unconditionally
conservative. v0.6.1 hard-coded `mmap_size=0` + 2/4 MB page caches on every host
as a 100-shard fan-out commit guard — but the BGE-M3 model singleton was the
actual 120 GB → 7 GB fix, so that I/O posture only over-throttled RAM-rich hosts
(reading 46 GB of shards through a 4 MB cache with mmap off). This scales the
per-shard `mmap_size` / `cache_size` to the host.

- **perf(memory): RAM-aware SQLite budget, `HELIX_MEM_PROFILE` (default `auto`).**
  `hardware.sqlite_memory_budget(n_shards)` derives a per-connection
  `mmap_size` / `cache_size` from *available* RAM: `budget = (available − 25%
  reserve) / n_shards`, split into file-backed mmap (≤ 2 GiB/shard) + a bounded
  2–64 MB page cache. Because the budget is a fraction of free RAM, it can never
  claim more than exists, and it self-throttles when shard count is high or RAM
  is scarce (the 105-shard / 48 GB stress case falls back toward mmap-off). The
  plan is resolved once in `ShardRouter` from the registered shard count and
  threaded into each shard's `KnowledgeStore` + `main.db`; standalone stores
  resolve a single-DB budget.
  - `HELIX_MEM_PROFILE`: `auto` (default) · `aggressive` (15% reserve, 4 GiB cap)
    · `conservative` (**byte-identical to v0.6.1** — the escape hatch) · `<N>gb`
    (pin the total SQLite budget, host-independent — useful where psutil
    over-reports inside a constrained container).
  - Hard overrides: `HELIX_SQLITE_MMAP_SIZE` (bytes) and `HELIX_SQLITE_CACHE_SIZE`
    (raw pragma value) win over the profile.
  - PRD: `docs/prds/2026-05-30-dynamic-ram-scaling.md`. Unit-tested (budget
    contract + plan-application through Genome / ShardRouter / main.db); the
    end-to-end 100-shard perf delta is validated separately on the bench fixture.

## 0.6.1 — 2026-05-30

Performance release: concurrent shard fan-out + a daemon RAM collapse at
100-shard scale. Both land off the EnterpriseRAG-Bench v2 work (850K genes /
100 shards) and were verified end-to-end on that fixture (resident RAM
~120 GB → 7 GB; per-query 125s → 57.6s median at `HELIX_SHARD_WORKERS=8`;
0 daemon deaths; ranked output byte-identical to the serial path).

- **perf(memory): share ONE BGE-M3 model process-wide (PR #173).**
  `KnowledgeStore._get_dense_codec()` built its own ~2 GB `BGEM3Codec` per
  instance, so a query touching ~100 shards loaded up to ~100 copies of the
  model — the dominant driver of the daemon's 47 GB-on-disk → ~120 GB-resident
  ramp (legitimate heap is only ~6 GB). `get_shared_codec()` returns one
  instance per `(model_name, dim, device)`; inference is stateless so sharing
  across concurrent fan-out workers is safe (double-checked load lock). Default
  on; `HELIX_SHARE_DENSE_CODEC=0` reverts to per-instance for an A/B.

- **perf(retrieval): concurrent shard fan-out (PR #172).** The 100-shard
  fan-out in `ShardRouter.query_genes` ran as a serial loop. It now parallelizes
  the per-shard fetch (open + `query_docs` + IDF probe) via a
  `ThreadPoolExecutor` — the dense matmul releases the GIL through BLAS, so
  threads genuinely parallelize, with BLAS pinned to 1 thread (`threadpoolctl`)
  to avoid oversubscription. Accumulation/merge/sort stays sequential in
  original shard order, so ranked output is byte-identical to serial. Gated by
  `HELIX_SHARD_WORKERS` (default 1 = serial). Pair with the BGE-M3 singleton —
  without it the per-shard model duplication thrashes the pagefile and caps the
  speedup at ~1.5x.

- **perf(memory): optional fp16 dense matrix (PR #173).**
  `HELIX_DENSE_MATRIX_DTYPE=float16` halves the resident per-shard dense matrix
  (~3.3 GB → ~1.65 GB); numpy promotes to fp32 inside the matmul so cosine
  precision is unchanged. Default `float32` = byte-identical to 0.6.0.

- **perf(memory): bound SQLite memory + guard mmap (PR #173).** Explicit
  `cache_size` caps on both per-shard connections (−2 MB writer / −4 MB reader;
  previously the unbounded 2 MB/conn default × 200 connections) and explicit
  `PRAGMA mmap_size=0` on every connection (writer/reader/main.db) as a process-
  commit guard for concurrent shard opens under fan-out.

- **fix(retrieval): WAL checkpoint on shard close (PR #172).**
  `ShardRouter.close()` now calls `genome.close()` (which runs
  `checkpoint(TRUNCATE)`) instead of closing the connections directly, which
  skipped the checkpoint and left up to 64 MB of un-truncated WAL per shard.

## 0.6.0 — 2026-05-28

Substantial release covering corpus-scale retrieval, bench rebuilding,
storage audits, and a stack of stability + portability fixes. Headline
work: EnterpriseRAG-Bench (Layer 3) shipped with 100q variant-A
results (recall@10 = 28% on the 850K-gene v2 fixture); two scaling-wall
bugs (regex ReDoS at ingest, SQL-variable cap at retrieval) fixed and
cross-validated on x86 + ARM64 hardware; ~400 lines of new bench docs.

- **fix(tagger): eliminate catastrophic backtracking in `_KV_PAIR_PATTERN`
  (PR #155 / PR #162).** Pre-fix, `(\w+(?:_\w+)*)` had redundant
  nested-quantifier ambiguity that triggered catastrophic backtracking on
  underscore-heavy content. A single worker spinning on
  `tagger.py:439`'s `_KV_PAIR_PATTERN.finditer(content[:5000])` hung the
  EnterpriseRAG-Bench-Onyx-full corpus build for 60+ minutes on a single
  google-drive shared-drives file (underscore-rich JSON keys like
  `expected_doc_ids`, `data_source_id`). The fix `(\w+)` is functionally
  identical (same match set, since `\w` includes `_`) but has no nested
  quantifier. Verified on the 3 worst-offender files from the bench corpus:
  0.40-0.52 ms each, down from >60 min hung. 200-underscore stress test:
  0.02 ms. **Cross-validated** under two independent py-spy investigations
  on different hardware classes (x86 Ryzen + RTX 3080 Ti on 2026-05-19,
  ARM64 Grace + GB10 on 2026-05-27) — same line, same root cause, same fix.

- **perf(ddl): skip FTS5 orphan cleanup when delta is < 5% of gene count
  (PR #156 / PR #162).** The previous cleanup ran
  `DELETE FROM genes_fts WHERE gene_id NOT IN (SELECT gene_id FROM genes)`
  — an O(N·M) correlated subquery that hung the daemon's first-query
  response for hours on the 850K-gene / 105-shard EnterpriseRAG-Bench
  fixture. On a single 18K-gene shard with ~40 orphans (0.2% noise) the
  `NOT IN` scan pegged a core for 5-10 minutes against a cold OS-cache
  page set. Orphan FTS5 entries are harmless at query time (downstream
  `gene_id` joins return NULL and filter out before delivery), so
  skipping cleanup for trivial deltas costs nothing in retrieval quality
  and unblocks first-query latency entirely. For the rare significant-drift
  case (delta ≥ 5%), the rewritten query uses indexed `NOT EXISTS` instead
  of `NOT IN`, turning O(N²) into O(N log N). Daemon `/health` response
  on the 850K-gene fixture went from "hangs forever" to milliseconds.

- **feat(build): salvage already-complete shards on rebuild (PR #157 /
  PR #162).** Adds `_try_salvage_complete_shard()` which opens an existing
  shard `.db` read-only, verifies the `genes` table has 100% dense
  backfill coverage and no live WAL sidecar, and returns the same
  result-dict shape that `_build_one_shard` would normally produce —
  letting the parent's `_commit_shard_result` re-register the shard via
  `INSERT OR REPLACE`. Designed for the kill+restart cycle: if
  `build_fixture_matrix.py` is interrupted (Ctrl+C, OOM, planned restart),
  fully-complete shards on disk are re-registered in seconds instead of
  rebuilt from scratch. Verified at scale: 21 of 22 already-complete
  shards re-registered into a fresh `main.genome.db` in 2 min 19 sec
  (vs ~13 hours to rebuild from scratch) during a mid-build restart of
  the EnterpriseRAG-Bench-Onyx-full 850K-gene build.

- **fix(knowledge_store): batch IN-clause queries to stay under SQLite cap
  (PR #163).** SQLite caps `WHERE col IN (?, ?, ...)` placeholders at
  `SQLITE_LIMIT_VARIABLE_NUMBER` (999 legacy, 2000 on the Python 3.12 /
  SQLite 3.50 builds we ship to, 32766 on newer compile defaults). Four
  call sites on the `gene_scores` fan-out path in `query_docs`
  (`_apply_authority_boosts`, sema-boost embedding lookup, party-attribution
  lookup, access-rate epigenetics lookup) build the IN clause from a
  caller-determined candidate set that can exceed the cap in production.
  Observed in the 2026-05-28 v2-fixture 100q bench: 3 of 29 queries had a
  per-shard query raise `OperationalError: too many SQL variables`, which
  the daemon's per-shard try/except swallowed as "shard X query failed;
  skipping" — biasing recall@K by silently dropping shards. Variants where
  SPLADE or the prefilter narrows `gene_scores` don't hit this; only the
  no-filter SPLADE-off path produces sets large enough to blow up. Adds
  `_iter_in_batches(items, batch_size=500)` helper and refactors the four
  hot sites. Includes TDD'd regression test at
  `tests/test_knowledge_store_batched_in.py` that probes the runtime cap
  via `conn.getlimit(SQLITE_LIMIT_VARIABLE_NUMBER)` and exercises at
  `cap`, `cap + 1`, and `4*cap + 7` boundaries.

- **fix(mcp): registry handshake is best-effort, don't kill subprocess on
  failure (PR #169).** On Windows, `claude -p` MCP attempts were failing
  with "Connection closed" after ~2 s even when helix was alive on
  `http://127.0.0.1:11437`. Root cause: `_register_with_registry()` was
  called synchronously before `mcp.run()` entered the stdio handshake; an
  exception from `register_participant()` (auto-heartbeat thread init,
  etc.) propagated out of `main()` and killed the MCP subprocess before
  the host could complete its handshake. The registry is not load-bearing
  for tool calls — tool calls proxy directly to the helix HTTP API.
  Registry is only used by `helix_announce` + dashboards. This patch
  wraps `_register_with_registry()` in a try/except inside `main()`:
  happy path unchanged, failure path logs the exception and continues
  to `mcp.run()` rather than exiting. Closes #167.

- **feat(bench): add `--isolated` flag to `bench_claude_matrix` for
  leak-free measurement (PR #170).** When set, the `claude -p` sub-agent
  is launched with `--tools ""` (all built-in tools disabled),
  `--strict-mcp-config`, and `--mcp-config '{"mcpServers":{}}'` (no MCP
  servers). Pair with a sterile `--cwd` (e.g. `F:/tmp/bench_sandbox`) to
  also block CLAUDE.md auto-discovery. Isolates retrieval-driven answer
  quality from filesystem-tool access. Records `isolated` + `claude_cwd`
  in the per-run JSON so post-hoc analysis can distinguish leak-free runs
  from contaminated runs. Brings shipped code into agreement with
  shipped docs (`docs/benchmarks/BENCHMARKS.md` §"Layer 3 —
  EnterpriseRAG-Bench" and `BENCHMARK_RATIONALE.md` addendum already
  described this isolation mode). Closes #168.

- **docs(benchmarks): add Layer 3 (EnterpriseRAG-Bench) + EnterpriseRAG
  fixtures (PR #166).** ~400 lines across four files. `BENCHMARKS.md`
  gets a new "Layer 3 — EnterpriseRAG-Bench" section covering the
  2026-05-20→21 bench investigation rebuild (`isolated=True` mode,
  +32.4 pp helix lift, 65% hallucination reduction), cross-corpus results
  (60% recall@10 @ 10K → 71% @ 50K → 28% @ 850K), the expression-budget
  clamp fix (4%→43% correctness), Wall-1 / Wall-2 scaling-wall framing,
  the v2 100q variant-A result table, and cross-host validation of the
  tagger fix. `GENOME_FIXTURE_MATRIX.md` gets a new EnterpriseRAG-Bench
  fixtures section (5-row fixture table, shared 9-source-root scope,
  excluded-from-ingest list, auto-subsharding behavior, path-portability
  gotcha, branch/PR routing). `BENCHMARK_RATIONALE.md` gets an addendum
  on how Layer 3 answered the rationale's NIAH-doesn't-fit problems.
  `MULTI_VALID_GOLD.md` gets an EnterpriseRAG-Bench gold-path matching
  section (schema diff, `_rel_after_sources` normalization, prefix-tolerant
  match fix).

### Prior work consolidated into this release

The following entries were already in `## Unreleased` at the start of
this release cycle and ship together as part of 0.6.0:

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

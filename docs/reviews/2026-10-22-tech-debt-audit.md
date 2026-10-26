# Tech debt audit — 2026-10-22

**Receipt:** HEAD `91f88952` (master, v0.11.3), 0 commits behind `origin/master`. Static, read-only audit: no tests or scripts were run. Five subagents swept code/architecture (Sonnet), server/deps/CI (Sonnet), stale docs (Sonnet), tests (Haiku), and scripts/benchmarks (Haiku); the orchestrator re-read the evidence for every item marked **✔ verified**. Items without the mark carry the agent's file:line evidence but were not independently re-read.

**Line numbers** refer to that master commit. `beta` is 25 commits ahead and has moved `knowledge_store.py` (about +200 lines), so treat line numbers there as approximate.

**Status since the audit:**
- Finding 9 (writer reopen PRAGMAs) is fixed in PR #520, to `beta`.
- The wiki-banner half of finding 24 was fixed on `beta` by PR #516. The CLAUDE.md drift is still open.

Prior audit: [`2026-09-08-debt-resolutions.md`](2026-09-08-debt-resolutions.md). Items it resolved are not repeated. Its open items (#431 follow-ups, #439, nested config validation) remain open and are not re-scored here.

**Scoring:** Impact (I), Risk (R), Effort (E), each 1–5. Priority = (I + R) × (6 − E). Higher means do it sooner.

---

## Headline numbers

| Measure | Value |
|---|---|
| Package | 174 `.py` files; largest `knowledge_store.py` 6,038 lines, `context_manager.py` 3,879, `config.py` 2,326 |
| 90-day churn hotspots | `config.py` 45 commits, `knowledge_store.py` 38, `context_manager.py` 27, `routes_admin.py` 19 |
| Functions ≥150 lines | 37. Largest: `KnowledgeStore.query_docs` 1,604; `setup_admin_routes` 1,394; `setup_context_routes` 1,037; `_build_context_impl` 773; `load_config` 734 |
| `KnowledgeStore.__init__` | 626 lines, 92 parameters |
| Broad `except Exception` | 653 total; 101 pass/continue-only; 108 log at DEBUG only |
| Config surface | 24 dataclasses, 278 fields; 107 `os.environ` reads in 33 files |
| Tests | 4,743 `def test_` in 329 files (CLAUDE.md says ~4,165); 0 unconditional skip/xfail; 57 `importorskip` (26 whole-file) |
| CI Python coverage | 3.12 only; classifiers claim 3.11–3.13; maintainer runs 3.14 |
| Scripts | 11 files with broken `cymatix_context` imports; 339 hardcoded drive-path lines in 130 files |
| TODO/FIXME | 3 (all `TODO(v1.1)` in `vault/`). The debt is structural, not annotated. |

---

## Prioritized findings

### Tier 1: cheap and high-value (priority ≥ 28)

| # | Finding | Cat | I | R | E | Pri |
|---|---|---|---|---|---|---|
| 1 | **CLA workflow runs `pull_request_target` with write perms on a tag-pinned third-party action.** `cla.yml:13-28` grants `contents/actions/pull-requests/statuses: write` to `contributor-assistant/github-action@v2.6.1`. A moved tag gives repo write. There is no `dependabot.yml`, no `SECURITY.md`, and `ci.yml` has no top-level `permissions:`. ✔ verified | infra/sec | 4 | 3 | 1 | **35** |
| 2 | **Whole test files are likely always skipped in CI.** `test_packet_token_cost.py:8` gates on `tiktoken`, which nothing declares. The launcher tray/lanes tests gate on `pystray`, which no workflow installs. `test_brand_assets.py` needs PIL, which only `launcher-tray` provides. (Transitive installs were not resolved, so this is likely rather than certain.) ✔ verified | test | 3 | 3 | 1 | **30** |
| 3 | **Undeclared runtime dependencies.** `retrieval/fusion_plr.py:42` imports `joblib` at module top, and unpickling needs `scikit-learn`; neither is in any extra. CI installs both by hand in every job. The caller (`server/helpers.py:562-565`) catches `ImportError` and returns `None`, so a pip user with `[plr] enabled` gets **silently no PLR confidence** rather than an error. Also undeclared: `litellm` (a documented backend), `anthropic`, `boto3`, `FlagEmbedding`, `threadpoolctl`. `embeddings = ["numpy", "sentence-transformers"]` has no floors. ✔ verified | dep | 3 | 3 | 1 | **30** |
| 4 | **The public README links 12 receipts that 404.** `benchmarks/dogfood/**/receipts*/` was untracked in `e034010f` and is gitignored (`.gitignore:182`), but README (12 links), CHANGELOG (~25), `RELEASING.md:78-85`, CLAUDE.md and `config-reference.md:256` still link them. ✔ verified | docs | 3 | 3 | 1 | **30** |
| 5 | **`config-reference.md` ships a stale `cymatix.toml` dump** "as of 2026-05-10" (`:1703-2170`): `backend = "ollama"`, `expression_tokens = 12000`, `splade_enabled = true`. The shipped values are `"none"`, 7000 and false. The generated tables above it are fine; only the hand-pasted dump drifted. ✔ verified | docs | 3 | 3 | 1 | **30** |
| 6 | **Operator docs still treat dense as the standard path.** `SETUP.md:453-456` (ollama "default placeholder") and `:517-570` (BGE-M3 backfill plus "flip dense on"), `TROUBLESHOOTING.md:508-565` (an empty dense matrix framed as a fault), and `operator-runbooks.md:16-33` (run Runbook 1, the BGE-M3 backfill, "exactly once"). Dense has been opt-in since 2026-08-15. | docs | 3 | 3 | 1 | **30** |
| 7 | **The CI matrix doesn't match the support claims.** Every job pins Python 3.12 (`ci.yml`, `desktop.yml`, `publish.yml`, `weekly-embeddings.yml`). Classifiers claim 3.11–3.13, `requires-python>=3.11` admits 3.14, and the code already works around a 3.14-only sqlite3 deadlock (`identity/registry.py:98-106`). The full `-m "not live"` suite runs only on ubuntu; Windows, the primary dev host, runs a 5-file subset. ✔ verified (versions) | infra | 4 | 3 | 2 | **28** |
| 8 | **Eight retrieval tiers swallow failures at DEBUG**, with no counter. This is the same class as the #431 harmonic Tier-5 bug, which was fixed only for that tier. The sites are `knowledge_store.py:3247` (PKI), `:3285` (filename_anchor), `:3711` (SEMA), `:3804` (dense), `:3962` (SR), `:4025` (party_attr), `:4058` (access-rate) and `:4502` (Hebbian). A broken tier is indistinguishable from an empty one, and receipts depend on tier contributions. Fix: warn once per store per tier and add a `tier_error` counter. ✔ verified | code | 4 | 3 | 2 | **28** |

### Tier 2: real bugs and risks (priority 20–30)

| # | Finding | Cat | I | R | E | Pri |
|---|---|---|---|---|---|---|
| 9 | **Reopening the writer connection drops its PRAGMAs (a bug).** The boot path sets `synchronous`, `cache_size` and `mmap_size` (`knowledge_store.py:1040,1051-1052`). The snapshot-refresh reopen (`:5883-5888`) and the post-`vacuum()` reopen (`:6015-6020`) set only WAL, busy_timeout and journal_size_limit. After `/admin/vacuum`, the writer falls back to `synchronous=FULL` with the default cache and no mmap, which undoes the #372 ingest-latency work and the `[genome] synchronous` knob. Fix: one `_open_writer_conn()` used at all three sites, with a test-first receipt. ✔ verified. **Fixed in PR #520.** | code | 3 | 3 | 1 | **30** |
| 10 | **Unreferenced destructive scripts.** `scripts/genome_cleanup.py:76,91` (`DELETE FROM genes …`) and `scripts/resequence_cpu.py:58,161` (`os.unlink`) have 0 references. `scripts/bench_chain/run_chain.ps1:6` hardcodes `$repo = 'F:\Projects\cymatix-context'`, so running it from a worktree executes and writes against the main checkout. ✔ verified (run_chain) | infra | 2 | 3 | 1 | **25** |
| 11 | **Broken script imports.** 13 import lines in 11 files point at modules that moved during the sub-package restructure: `cymatix_context.claims` and `.claims_analyze` (`scripts/backfill_claims.py:24-25`), `.sema`, `.provenance`, `.cwola`, `.tie_break`, `.headroom_bridge` and `.claims_graph`. `scripts/backfill_bgem3.py` is the superseded v1 and writes the wrong column (`embedding_dense`, `:29`). ✔ verified (sample) | code | 2 | 2 | 1 | **20** |
| 12 | **Loopback servers are reachable by drive-by browser requests.** There is no CORS/TrustedHost/Origin check anywhere (✔ 0 hits). `/ingest`, `/admin/*`, `/admin/swap-db`, `/admin/shutdown` and the launcher's `/api/genome/*` parse bodies with bare `request.json()`, which ignores Content-Type, so a `text/plain` cross-origin POST reaches them. `admin_token` defaults to `""` (✔ `config.py:296`). Even when set, first-party clients don't send it (`mem_sync.py`, `launcher/supervisor.py`, `launcher/collector.py`, `bridge.py`), so turning auth on breaks the tray and sync. Fix: require `application/json` plus an Origin/Host allowlist, and have every internal client read `CYMATIX_ADMIN_TOKEN`. | sec | 4 | 4 | 3 | **24** |
| 13 | **Some mutating routes sit outside the admin guard even with a token set.** These are `/replicas/sync`, `/bridge/collect`, `/bridge/signal`, `/export/obsidian`, `/vault/trace*`, `/sessions/register` and `/hitl/emit`. Auth is applied by route list rather than default-deny. | sec | 3 | 3 | 2 | **24** |
| 14 | **`init_db` mutates stores on open, including frozen beds.** `_auto_repair` runs UPDATEs (`storage/ddl.py:384,397`), and `_sync_fts5_external` can run a full FTS `'rebuild'` (`:540`) on every open. `swap-db` sets `read_only` only after construction (`routes_admin.py:1311`). Opening a receipt bed can therefore rewrite it, which bears directly on bed-identity and witness reproducibility. ✔ verified | arch | 3 | 3 | 2 | **24** |
| 15 | **No pinned `PYTHONHASHSEED`, plus hash-order-dependent code.** Neither CI, `pyproject` nor `conftest` pins the seed (✔). `knowledge_store.py:3051` truncates a `set` with `list(_prefilter_set)[:998]` (✔), so the kept IDs depend on the hash seed. It is opt-in and exposed only at size >998. A `_sqlite_variable_limit` helper already exists (`:3897`). The filename_anchor `[:64]` jitter from 09-04 is the same class. | code | 3 | 3 | 2 | **24** |
| 16 | **Publish has no test gate and uses a floating ref.** `publish.yml` triggers on release with no `needs:` on tests, no tag-vs-`__version__` check and no `twine check`. It uses `pypa/gh-action-pypi-publish@release/v1` (a branch) with `id-token: write` (✔). The version is duplicated in `pyproject.toml:19` and `__init__.py:10`. The 947k witness is the real gate today, but it is manual. | infra | 3 | 3 | 2 | **24** |
| 17 | **Live tests skip on any exception.** `test_fixture_dense_backfill.py:391-395`, `test_ingest_dense_v2.py:593,613` and `test_dense_recall.py:757` catch `Exception` on model load and skip, so a broken encoder passes a live run. `test_layer_devices.py:79` skips exactly on CUDA hosts. | test | 3 | 3 | 2 | **24** |
| 18 | **Heavy admin work runs on the asyncio loop.** `/admin/vacuum`, `/admin/kv-backfill` (fetches every body, then loops UPDATEs), `/admin/compact`, `/admin/checkpoint`, `/admin/reload` and `/admin/swap-db` do blocking SQLite work inside `async def`. That stalls `/health`, and the launcher's 2s probes can mis-diagnose a hung server. The background WAL checkpoint was already moved off-loop, so the pattern is known. | arch | 4 | 3 | 3 | **21** |
| 19 | **Native observability configs bind `0.0.0.0`** ([known #434], partial). Compose is now loopback-pinned. The native sidecar reuses `otel-collector-config.yaml:11,13,22,54` and `tempo.yaml:10` (0.0.0.0). Native Grafana runs anonymous-Admin, safe only while pinned. | sec | 3 | 3 | 2 | **24** |
| 20 | **The launcher assumes a git checkout.** Five copies of `Path(__file__).parent.parent.parent` (`launcher/tray.py:658`, `observability_paths.py:80`, `observability_render.py:41`, `genome_registry.py:103`, `routes_admin.py:36`) locate `scripts/`, `tools/native-otel` and the root `.bat`, none of which ship in the wheel. Pip users' "Install Observability" and auto-restart silently do nothing. | arch | 3 | 3 | 2 | **24** |
| 21 | **Constructor defaults disagree with config defaults.** `KnowledgeStore(pki_enabled=True)` vs config False; `entity_graph=False` vs True; `bm25_shortlist_enabled=False` vs True (✔ `knowledge_store.py:601,626,778`). `tier_logic.py:48` and `context_manager.py:2163` fall back to `"additive"` while the shipped default is `rrf`. A bare `KnowledgeStore()` (tests, scripts, benches) behaves differently from a server. | code | 3 | 4 | 3 | **21** |
| 22 | **Schema has no version and three migration styles.** There is no `PRAGMA user_version` (✔ 0 hits). Migrations mix `table_info` guards, `try: ALTER … except OperationalError: pass` (which also masks "database is locked"), and `IF NOT EXISTS`. DDL is spread over 11 files. Cascades are declared, but `PRAGMA foreign_keys` is never set (✔ 0 hits), so `ON DELETE CASCADE` is inert. | arch | 4 | 3 | 3 | **21** |
| 23 | **Hardcoded scoring constants are invisible to the config dump.** These include party_attr `+0.5` (`knowledge_store.py:4021`), access-rate `0.05*min(rate*3600,5)` (`:4047`), harmonic cap `3.0*weight` (`:3921`), `_BLEND_SCALE_REF=10.0` (`scoring/blend.py:124`) and the health thresholds (`context_manager.py:4071-4080`). They are rerank exchange rates that receipts can't record. | code | 2 | 3 | 2 | **20** |
| 24 | **Agent-facing docs have drifted from the code.** CLAUDE.md says v0.10.0 (it is 0.11.3), "16 sub-packages" (17, `sync/` is missing), lists a `server.py` shim that doesn't exist (✔), calls cover_walk "receipt-gated" (it was killed in #408), and says ~4,165 tests (4,743 ✔). On master the wiki page banners say v0.9.2, v0.10.0 and v0.11.0 on different pages (✔). *(PR #516 brought every banner to v0.11.3 on `beta`.)* | docs | 2 | 2 | 1 | **20** |
| 25 | **Dead links left by earlier archive moves.** About 20 links are affected, e.g. `DESIGN_TARGET.md:45`, `FEDERATION_LOCAL.md:9,90,202,359`, `PIPELINE_LANES.md:35,111,411,464,466`, `LAUNCHER.md:9,886,949,957`, `SESSION_REGISTRY.md:13,97,625,655` and about 14 in `api/context-endpoint.md` (paths to `server.py`/`legibility.py`). | docs | 2 | 2 | 1 | **20** |
| 26 | **The killed W2.1 COVER walk is still carried, and ingest still builds its edges.** `cover_walk.py` (155 lines), 9 knobs, 39 references in `knowledge_store.py` and 513 test lines. The `config.py:786-803` comment still says "receipt-gated". Relation=5 COVER edges are still built at ingest (8.5M rows on the 947k bed) for no query-time consumer. The hub cutoff bounds the cost but doesn't remove it. **Owner decision:** fix the comment now (E1); deciding whether to keep forming edges is separate. | code | 3 | 2 | 2 | **20** |
| 27 | **Collection-time network probe.** `tests/test_stress.py:61` calls `httpx.get(localhost:11434)` at import, even under `-m "not live"` (✔), probing an Ollama backend that is now a no-op. | test | 2 | 2 | 1 | **20** |

### Tier 3: structural or unverified (plan rather than sprint)

| # | Finding | Cat | I | R | E | Pri |
|---|---|---|---|---|---|---|
| 28 | **`load_config` is hand-copied field by field** (734 lines). A new knob touches 4 places: the dataclass, the loader, `sharding.build_genome_kwargs` and the 92-param ctor. Knobs read via `getattr(cfg, "x", default)` degrade silently on rename. Drive the loader from `dataclasses.fields()`. | code | 3 | 3 | 3 | 18 |
| 29 | **Per-query env switches bypass config.** `CYMATIX_SEMANTIC_ARM`, `CYMATIX_LAYERED_FINGERPRINTS`, `CYMATIX_ABSTAIN_DISABLE`, `CYMATIX_FILENAME_ANCHOR_ENABLED` and `CYMATIX_BENCH_RANK_HASH` are read inside the hot path, so `/admin/config-dump` can't show them. This breaks the "config actually loaded at runtime" receipt rule. There are three separate env-precedence resolvers (`config.py`, `otel.py`, `trace.py`) with different strip rules. | code | 3 | 3 | 3 | 18 |
| 30 | **Duplicated primitives.** Three token estimators disagree (`accel.estimate_tokens`, `metrics.estimate_tokens` = len//4, inline `//4`), so reported and budgeted tokens differ. Five stopword sets and four or more tokenizers feed ranking. There are 22 `sqlite3.connect` sites with hand-copied PRAGMA stanzas (finding 9 is the bug this produced). | code | 2 | 2 | 2 | 16 |
| 31 | **Dead or consumer-less modules.** `write_queue.GenomeWriter` (274 lines, a latent second writer), `retrieval/lexical_rescue.py`, `relevance_window.py`, `encoding/chunk_fetch.py`, `telemetry/trace_checks.py`, `ClaudeBackend`, `claude_model`, and the legacy `mcp/server.py` ("rollback only", with a stale "18-tool" message and its own env var `CYMATIX_URL` vs `CYMATIX_MCP_URL`). The MCP lean profile prunes via the private `server._tool_manager._tools` and **fails open** if the SDK moves. | code | 2 | 2 | 1–2 | 15–20 |
| 32 | **Biology shims are load-bearing.** 73 test files import `cymatix_context.genome` (✔), against 39 that import `knowledge_store`, and about 25 package-internal imports go through the shims. The shims snapshot `dir(_impl)`, so `monkeypatch` on a shim path doesn't reach the real module. | arch | 2 | 3 | 3 | 15 |
| 33 | **Hardcoded drive paths in scripts and benchmarks.** 339 lines in 130 files; 44 lines point at `F:/Projects/helix-context`. Make them env-overridable rather than deleting them: some point at the 165 GB bed store on purpose. | infra | 3 | 2 | 3 | 15 |
| 34 | **Sharded mode is silently partial.** `ShardedGenomeAdapter` has 23 no-op stubs (`sharding.py:554-593`): tombstone, compaction and co-activation do nothing. `ShardRouter.query_genes` (569 lines) is a second retrieval and merge implementation. | arch | 3 | 3 | 4 | 12 |
| 35 | **`fusion_mode="additive"` is woven into the hot path.** There are dual-write tier scores and three additive-only knobs, and "removal in v(N+2)" names no tracking issue. Tier thresholds are still calibrated on the additive scale (`tier_logic.py:87-91`, "recalibration is #287") while RRF ships. `blend_mode="legacy"` is gated on #239. | code | 3 | 3 | 4 | 12 |
| 36 | **God objects.** `KnowledgeStore` (5,980-line class, 84 methods), `CymatixContextManager` (3,376 lines), and route mega-closures (all admin routes nested in one 1,394-line function). Request bodies are hand-parsed dicts: a JSON list body on `/context` gives a 500. Highest impact on the list, but the formula ranks it low because it is E5. Chip away alongside the work in items 8, 9, 18, 21 and 28 rather than as a rewrite. | arch | 5 | 4 | 5 | 9 |
| 37 | **Test hygiene.** `make_gene` is redefined in 19 files, `_clean_env` in 10 and `client` in 10. Four test files exceed 1,500 lines. There is a fixed `time.sleep(0.5)` race at `test_packet_trace.py:214`. The `perf` marker says "excluded from CI" but isn't excluded by `-m`. Modules without tests: `retrieval/tie_break.py` (env-gated, 312 lines), `routes_sync/registry/trace` and `cli/cmd_serve/cmd_trace`. | test | 2 | 2 | 2 | 16 |
| 38 | **Lint is minimal and there is no type-check**, though the package ships `py.typed`. Ruff runs only E9/F63/F7/F82; there is no mypy/pyright, `pip-audit` or coverage report. | infra | 2 | 1 | 3 | 9 |
| 39 | **The desktop release matrix uses `macos-13`** (`desktop.yml:43`, ✔). That image is believed retired (not checked against GitHub). The packaged build runs only on tags, so breakage surfaces at release time. | infra | 2 | 3 | 1 | 25 (unverified) |
| 40 | **PLR model loads via pickle with an optional checksum** (`fusion_plr.py:90-116`). Make the sha256 mandatory when `model_path` is non-default. | sec | 2 | 2 | 1 | 20 |

---

## Stale docs: archive candidates

Last-commit dates are from `git log`. "Refs" counts inbound references outside `docs/archive/`.

> **Two hazards before moving anything**
> 1. **DOGFOOD-GOLD:** `benchmarks/dogfood/needles_resolved.json`, `pack/gold_pack_manifest.json` and `docs/benchmarks/data/2026-09-26-dogfood-needles-resolved.json` use repo-relative doc paths as `gold_source`. Moving a doc tagged DOGFOOD-GOLD below breaks gold resolution on the next dogfood rebuild unless the needles are re-resolved.
> 2. **Relink in the same commit.** The dead links in finding 25 come from earlier archive moves that skipped relinking. Mirror the source path under `docs/archive/` (e.g. `docs/archive/superpowers/plans/`); there are currently two conventions, see below.

### Archive (high confidence)

| Path | Last commit | Why | Refs |
|---|---|---|---|
| `docs/superpowers/plans/2026-05-11-helix-cli-v1.md` | 05-11 | helix CLI plan; shipped as `cli/` | 0 |
| `docs/superpowers/plans/2026-05-13-issue-92-parallel-ingest.md` | 05-13 | Shipped, PR #94 | 0 |
| `docs/superpowers/plans/2026-07-05-test-suite-consolidation.md` | 07-05 | Shipped, #238 | 2 (data JSON) |
| `docs/superpowers/{plans,specs}/2026-07-13-codex-helix-mcp-reliability*` | 07-18 | Pre-0.8.5 helix MCP fix; target module gone | 0 |
| `docs/superpowers/{plans,specs}/2026-07-13-launcher-graph-summary*` | 07-18 | Shipped (PR #444) | 0–1 |
| `docs/superpowers/plans/2026-07-20-cymatix-context-rename.md`, `specs/2026-05-11-rename-r2-*`, `specs/2026-05-13-rename-r3-*` | 05–07 | Rename done; the plan describes the back-compat approach 0.8.5 reversed | ≤2 (siblings) |
| `docs/superpowers/plans/2026-08-06-rerank-wiring.md` and `docs/design/2026-08-06-rerank-wiring-kickoff.md` | 08-06 | #341 shipped default-off | 1–2 |
| `docs/superpowers/{plans,specs}/2026-08-11-cc-exchange-task-board*` | 08-12 | Board shipped and reconciled | 0–1 |
| `docs/superpowers/plans/2026-08-14-archival-housekeeping-cleanup.md` | 08-17 | All gates executed | 0 |
| `docs/superpowers/plans/2026-08-18-issue-batch-testing-paths*.md` | 08-18 | Dated batch report; board cleared at v0.9.0 | 1 |
| `docs/superpowers/plans/2026-08-28-w2.1-cover-walk.md` | 08-28 | **Killed by receipt** (PR #408); archive with a "killed" banner | 1 (test) |
| `docs/superpowers/plans/2026-08-28-w2.4-min-delivered-floor.md` | 08-28 | Shipped and graduated 08-30 | 0 |
| `docs/superpowers/{plans,specs}/2026-08-27-cymatix-context-cross-model-unification*` | 08-27 | Shipped as `docs/clients/*`, `skills/` | 0–1 |
| `docs/superpowers/plans/2026-08-30-{rosetta-tier2-aliases,v091-wiki-readme-docs}.md`, `specs/2026-08-30-v091-readme-wiki-rosetta-design.md` | 08-30 | v0.9.1 docs wave shipped | 0–3 |
| `docs/prds/` (all 7, 05-30 to 06-16) | ≤07-01 | helix-era. Three are "DRAFT awaiting sign-off" for 5 months; RRF shipped; `HELIX_OTEL_*` env names removed; the semantic-wiring arm's premise (dense) was refuted. **Patch first:** `config.py` comment and `config-reference` cite `semantic-wiring-arm` | 0–2 |
| `docs/investigations/` (all 6) | ≤07-05 | Pre-rename cc-exchange exports and handoffs; `dense-ann-cap` assumes "v0.6.3 ships dense ON". **Patch first:** the `storage/indexes.py` comment cites `165_path_key_index_consensus.md` | 0–1 |
| `docs/audits/` (all 3, v0.7.1-era) | 06-09…12 | helix baseline; cite only each other plus the roadmap-lineage council | 1–2 |
| `docs/councils/2026-05-30-multihop-scale-inference.md` | 07-01 | Sharded-Onyx fusion ladder era | 0 |
| **J-space cluster:** `councils/2026-07-06-jspace-roadmap-council.md` (+`.panel.json`), `councils/2026-07-06-roadmap-lineage.md`, `design/2026-07-06-jspace-{roadmap-review,splat-roadmap}.md`, `research/2026-07-06-{phase0-remetric-prereg,phase1a-whitening-ab-results,ann-threshold-recalibration,rrf-gold-block-deficit}.md` | 07-06…07-27 | A dormant program that appears nowhere in README, CHANGELOG or wiki. The Phase 1a verdict was "do not adopt"; it rests on a dense premise. Move as one cluster (it cross-cites internally). `benchmarks/sweep_ann_sigma.py` cites the whitening doc. | 0–3 |
| `docs/councils/2026-07-13-assetopsbench-reshape-council.md` (+`.panel.json`) | 07-18 | Decision record for a NO-GO; sole citer is its verdict doc | 0–1 |
| `docs/specs/2026-05-28-folder-picker-and-tracked-sources.md` | 07-01 | "draft, no code yet", superseded by the folder picker, `[sync]` and `[[lanes]]`; has a dead link | 0 |
| `docs/design/2026-08-05-fork1-{encoder-daemon-kickoff,graduation-note}.md` | 08-05 | One-shot session prompts; the daemon shipped. (Keep `fork1-slice1-contract.md`: code cites it.) | 1–2 |
| `docs/ops/dense-ingest-on-12gb-rigs.md` | 08-18 | **Duplicate** of `operations/DENSE_VRAM.md`; says "not yet merged as of 2026-06-07" (✔ `:59`) though the env knobs it describes are in code | 1 |
| `docs/architecture/2026-06-07-pipeline-and-switchboard.md` | 08-07 | "As-shipped" reference for the helix063 wheel with dense as default; superseded by `PIPELINE_LANES.md` | 1 |
| `docs/research/{2026-06-28-ws1-3-council-review,2026-06-28-ws3-slice2-consensus,2026-07-03-external-review-gemini-antigravity,oss-semantic-retrieval-vs-helix}.md` | ≤07 | helix-era reviews | 0–2 |
| `scripts/` (see the scripts list below) | — | — | — |

### Archive (medium confidence or needs a step first)

| Path | Why / precondition |
|---|---|
| `docs/ROADMAP.md` (08-20) | Says "0.9.0 on master", "Open issues: 15", plus a July ledger, so it has been wrong for about 2 months. **Rewrite or retire.** If retired, leave a pointer to `wiki/Roadmap-and-Releases.md` plus CHANGELOG. The wiki links it at `:12,321`, so relink first. |
| `docs/superpowers/plans/2026-07-20-cymatix-readme-draft.md`, `2026-07-25-portfolio-readme-refresh.md` and its spec | Shipped, but **DOGFOOD-GOLD**. Move them only with a needle re-resolve. Keep `docs/superpowers/artifacts/*`: it is the source for the profile README. |
| `docs/superpowers/plans/2026-08-24-v09x-retrieval-refinement-phase.md`, `2026-08-09-ab-data-campaign.md` | Closed, but cited by BASELINES/CHANGELOG. Relink, then move. |
| `docs/architecture/raude_antigravity_persona.md` | An agent-persona draft, not architecture. Move to `docs/clients/` or archive. |
| `docs/dashboards/cymatix-pipeline.json` | Orphan Grafana JSON; not among the 7 provisioned dashboards. |
| `docs/benchmarks/2026-06-{18,27,28}-*contextbench*`, `2026-06-04-code-context-benchmark-strategy.md`, `coderag_bench_runbook.md`, `repobench_r_runbook.md` | v0.6-era code-bench receipts and helix-named runbooks. These are receipts, so move them as a cluster and only if not cited. |

### Do NOT archive (looks stale, but is load-bearing)

- `docs/superpowers/plans/2026-08-27-ranking-under-width-wave1.md`: 17 refs from config comments and TOMLs. It is the receipt basis for the rrf_k=20 and eps_band defaults.
- `docs/specs/2026-05-08-stage-{1..7}-*.md`: cited by code, SETUP and TROUBLESHOOTING (up to 43 refs). Stage 2 ("promote dense") is reversed. **Add a "superseded defaults" banner instead.**
- `docs/specs/2026-06-09-retrieval-profiles.md` (#205, live) and `specs/2026-07-13-snow-v2-*` (#208, blocked): live roadmap.
- `docs/benchmarks/BENCHMARKS.md` already carries a "Historical" banner and has 18 refs. Fix only its "Helix Context Benchmarks" title.
- `docs/superpowers/plans/2026-08-08-scbench-codex-cymatix-ab-harness.md`: **not shipped** (88 unchecked boxes, no code). This needs an owner decision to build or shelve. It is not "done".

### Structural doc debt

- **`docs/ops/` and `docs/operations/` duplicate each other**, and `operator-runbooks.md` sits at the root. Collapse to one directory; `ops/SKILLS_BUNDLE.md` is DOGFOOD-GOLD.
- **Two archive conventions:** `docs/archive/superpowers/{plans,specs}/` mirrors the source path, while `docs/archive/{plans,specs}/` dropped the prefix. Pick the mirror convention.
- **Receipts live in the private `cymatix-receipts` repo**, but no public doc says so (finding 4).
- **About 71 helix-mentioning `.md` files remain outside the archive.** A one-line "pre-rename, historical" banner is a cheaper fix than moving them all.
- **`docs/ROSETTA.md` stub** holds about 40 lines of unrelated wire-format (#417) prose. Move that to `config-reference` or a wire doc and keep the stub as a redirect.

---

## Scripts: archive candidates

| Path | Last commit* | Why |
|---|---|---|
| `scripts/backfill_bgem3.py` | 07-26 | Superseded by `_v2`; writes the wrong column |
| `scripts/backfill_sema_embeddings.py` | 07-26 | Broken import; hardcoded wrong DB; superseded by `backfill_sema.py --db` |
| `scripts/ops/overnight_diamond*.sh` (5), `scripts/ops/overnight_run.sh`, `scripts/overnight_cross_compare.sh` | 07-26 | Dated one-off overnight runs from April–May; 0–1 refs |
| `scripts/ops/run_pr9_sweep.ps1`, `run_full_suite.ps1`, `run_overnight_bench.ps1` | 05-13 | One-off sweeps; the last two overlap |
| `scripts/bench_chain/{run_chain,run_sike_203,s2_rerun_watcher,s4blob_watcher}.ps1` | 07-26 | Completed July chain; hardcoded main-checkout path |
| `scripts/e8_{crossover_curve,crossover_fixed_k,retest_april12}.py` | 07-26 | E8 VQ experiment; 0 refs |
| `scripts/{snow_ablation_sweep,analyze_density_scores,docs_tidy_probe,docs_tidy_diff,cwola_log_stats}.py` | ≤07-26 | 0 refs |
| `benchmarks/_run_n1000_sweep_2026-04-14.sh` | — | Dated one-off |
| **Review before archiving:** `scripts/genome_cleanup.py`, `scripts/resequence_cpu.py` (destructive, 0 refs), `ingest_models.py`, `audit_erb_github_bucket.py`, `init_main_db.py`, `generate_ssh_key.sh` | 07-21…26 | Unclear current use |

\* Many scripts show 07-26 only because the rename commit `8b4000e6` touched them. Reference counts are basename-grep lower bounds (module-style imports are not counted).

**Intentional, do not "fix":** the helix-named gold-source labels in `benchmarks/bench_needle.py:51-72` and `bench_claude_matrix.py`. Renaming them invalidates receipts. Add a whitelist comment instead.

---

## Phased remediation plan

Each phase is sized to run alongside feature work. Bug fixes go through `/tddfix` (test-first receipt).

**Phase 0: one afternoon of hygiene (items 1–6, 11, 24, 25, 27, 40).** None of these touch retrieval behavior, so none needs a 947k witness.
- SHA-pin the CLA action, then add `dependabot.yml` (github-actions), `SECURITY.md` and `permissions: contents: read` to `ci.yml`.
- Declare the `plr` extra (`joblib`, `scikit-learn`), floors for `embeddings`, and `tiktoken`/`pystray`/`mcp` in `dev`. Drop the manual pip tails in CI.
- Docs: point receipt links at cymatix-receipts or say "private"; delete the stale TOML dump; reframe dense as opt-in in SETUP, TROUBLESHOOTING and operator-runbooks; fix the CLAUDE.md drift and wiki banners; relink about 20 dead links.
- Fix the 11 broken script imports, or archive the scripts with the batch below.
- Move `test_stress`'s Ollama probe into a fixture.

**Phase 1: correctness fixes, each via `/tddfix` (items 8, 9, 14, 15, 17, 21).**
- `_open_writer_conn()`, with a test asserting PRAGMAs after `vacuum()`.
- Tier-error warnings plus a counter: a generalization of the #431 fix.
- `init_db(read_only=True)` path: skip `_auto_repair` and the FTS rebuild for frozen beds.
- Sort `_prefilter_set` and use `_sqlite_variable_limit`; pin `PYTHONHASHSEED=0` in CI.
- Narrow the live-test catch-all skips to `ImportError`/`OSError`.
- Align the ctor defaults with config, or make a bare ctor build from `Config()`. Behavioral for scripts and benches, so check whether any receipt relied on a bare ctor.

**Phase 2: security and CI breadth (items 7, 12, 13, 16, 18, 19, 20).**
- Add an Origin/Content-Type guard middleware; make internal clients send `CYMATIX_ADMIN_TOKEN`; move auth to default-deny.
- Add a CI matrix with 3.11 and 3.13 (3.14 allowed to fail) and the full suite on windows-latest.
- Add a publish gate: `needs: test`, a tag==version check, `twine check`, and a SHA-pinned publish action.
- Move admin handlers to `run_in_threadpool`.
- Resolve repo-root lookups via `importlib.resources`, or gracefully disable the features in wheel installs.

**Phase 3: archive pass (one PR, dry-run first).** Move the docs and scripts listed above, mirror paths, relink citers in the same commit, re-resolve dogfood needles if any DOGFOOD-GOLD doc moves, and collapse `ops/`+`operations/`.

**Phase 4: structural, opportunistic (items 22, 28–36).**
- Add a `schema_version` plus a single migration runner.
- Drive `load_config` from `dataclasses.fields()`.
- Hoist per-query env switches into config so `config-dump` shows them.
- Consolidate the token estimator, stopwords and `sqlite3.connect`.
- Delete consumer-less modules and the legacy MCP server.
- Extract `query_docs` tiers into per-tier functions as each is touched.

Owner decisions are flagged inline: COVER-edge formation, additive/legacy removal conditions, and the scbench harness.

---

*Generated by Claude Code (Opus 5.5 orchestrating Sonnet 5.5 and Haiku 5.5 subagents). Static analysis only; the orchestrator re-read the evidence for every item marked ✔.*

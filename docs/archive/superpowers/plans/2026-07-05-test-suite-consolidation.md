# Test Suite Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove ~15 genuinely dead tests, hoist ~400 lines of copy-pasted fixtures into `tests/conftest.py`, collapse 11 boilerplate test clusters into parametrized tables, and merge 9 redundant test files — with zero coverage loss.

**Architecture:** Test-only change set (plus relocating one misnamed diagnostic script out of `tests/`). Five phases: (1) deletions/relocations, (2) conftest fixture hoisting, (3) per-file parametrizations, (4) file merges, (5) final verification. Phases are sequential; tasks within a phase touch disjoint files and can run in parallel. The orchestrator commits once per phase after verification — implementer agents NEVER commit.

**Tech Stack:** pytest, `@pytest.mark.parametrize`, existing `tests/conftest.py` patterns (see `FakeBGEM3Codec` precedent at `tests/conftest.py:64`).

**Provenance:** Findings come from a 5-agent audit (2026-07-05) that read every test body cited below. Line numbers are from branch point `42e08ba`. If a cited line has drifted, locate the named test by name — names are authoritative, lines are hints.

## Global Constraints

- **Baseline (42e08ba, local machine, 2026-07-05):** `python -m pytest tests/ -m "not live" -q` → 2702 passed, 10 failed, 2 errors, 19 skipped, 2 xfailed in ~5min. The 12 pre-existing failures/errors are NOT caused by this work and must not be "fixed" or worsened: `test_bench_help_ascii.py::test_argparse_help_text_is_ascii`; `test_config_default_honesty.py::test_shipped_toml_matches_code_defaults`; `test_lazy_encoders.py::test_manager_init_constructs_no_encoders` + `::test_admin_components_reports_unloaded_without_loading`; 5× `test_pipeline.py::TestColdTierWiring::*`; `test_telemetry_phase1.py::test_dashboards_reference_only_real_instruments`; 2 errors in `test_public_dehardcode.py::TestHelixTomlShipsNeutral::*`. They cluster around local `helix.toml` drift. **Pass/fail comparisons are failure-set-relative: a task is green when no test that passed in its step-1 baseline fails afterward.**
- Branch: `chore/test-suite-consolidation` (off `42e08ba`). Implementers do NOT commit, do NOT touch files outside their task.
- Default suite must stay green: `python -m pytest tests/ -m "not live" -q` (native Windows python, NOT `uv run`).
- **Coverage-preservation protocol (every task):**
  1. Before editing: `python -m pytest <file(s)> --collect-only -q -m "not live" 2>$null | Measure-Object -Line` (or `| wc -l` in bash) → record N_before, and `python -m pytest <file(s)> -q -m "not live"` → must pass.
  2. After editing: same two commands → N_after must match the task's **Expected delta** exactly; all tests pass.
  3. Report both numbers + the pass/fail tail verbatim.
- Parametrization rule: every deleted test method's input/expectation pair MUST appear as a row in the replacement `@pytest.mark.parametrize` table. Collected-case count stays **equal or higher**; test-function definitions shrink.
- Deletion rule: a test may be deleted only if the task names it explicitly. If, on reading, the "covering" test does not actually assert the same thing, FOLD the missing assertion into the covering test first, then delete — and say so in the report.
- Preserve all existing markers (`live`, `slow`, `skipif` gates, `importorskip`) on surviving tests.
- Match the file's existing style (class grouping, docstring conventions, comment density). Parametrize IDs: use readable `ids=` or param strings so failures are self-describing.

### Canonical parametrize shape (used throughout Phase 3)

```python
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("steam/game/save.dat", True),
        ("node_modules/foo/index.js", True),
        ("src/main.py", False),
        # ... one row per deleted test method, same literal values ...
    ],
)
def test_is_denied_source(path, expected):
    assert is_denied_source(path) is expected
```

---

## Phase 1 — Deletions and relocations (10 tasks, disjoint files, parallel-safe)

### Task 1: Delete the two permanently-xfail HGT import tests

**Files:**
- Modify: `tests/test_health.py` (delete `test_import_skip_existing` ~line 236, `test_import_overwrite` ~line 269)

These are the only unconditional `@pytest.mark.xfail` tests in the suite. They assert pre-content-addressing merge semantics that are impossible under content-addressed gene_ids; their own docstrings say "needs a semantic rewrite." Delete both test functions entirely (including their xfail decorators and docstrings). Do not rewrite them — that is explicitly out of scope.

- [ ] Run protocol step 1 on `tests/test_health.py`
- [ ] Delete both functions
- [ ] Run protocol step 2. **Expected delta: collected −2; rest pass.**

### Task 2: Relocate the misnamed diagnostic script out of tests/

**Files:**
- Move: `tests/diagnostics/test_file_type_ingest.py` → `scripts/diagnostics/file_type_ingest.py`
- Move: `tests/diagnostics/file_type_ingest_2026-04-19.json` → `scripts/diagnostics/file_type_ingest_2026-04-19.json`
- Delete: `tests/diagnostics/` directory (and its `__init__.py` if present) once empty

This is a `__main__` diagnostic script (line ~384) requiring a live server on `127.0.0.1:11437`; pytest collects zero items from it. Use `git mv`. After moving, read the script and fix any paths that resolve relative to its own location (`Path(__file__)`-style references to the JSON fixture or `sys.path.insert` of the repo root) so they still resolve from `scripts/diagnostics/`. Grep `tests/` and `scripts/` and `docs/` for references to `tests/diagnostics` and update or report them.

- [ ] `git mv` both files; remove empty dir
- [ ] Fix internal relative paths; verify with `python -m py_compile scripts/diagnostics/file_type_ingest.py`
- [ ] `python -m pytest tests/ --collect-only -q -m "not live"` still collects cleanly (no import errors). **Expected delta: collected ±0.**

### Task 3: Delete the decayed launcher cleanup pin

**Files:**
- Modify: `tests/test_launcher_app.py` (delete `test_observability_module_global_pending_flag_removed`, ~lines 565-577)

The test asserts a deleted module global (`_OBS_INSTALL_PENDING`) is still absent — a cleanup pin whose value has decayed.

- [ ] Protocol step 1 on `tests/test_launcher_app.py`
- [ ] Delete the function
- [ ] Protocol step 2. **Expected delta: collected −1; rest pass.**

### Task 4: Fold and delete the two plumbing test files

**Files:**
- Delete: `tests/test_vendor_host_plumbing.py` (1 test)
- Delete: `tests/test_helix_announce_plumbing.py` (2 tests)
- Possibly modify: `tests/test_server.py` (fold missing assertions only)

Claimed coverage (verify each by reading before deleting):
- `test_vendor_host_plumbing.py::test_register_with_vendor_host_fields_survives_to_list` ≈ `tests/test_server.py::TestSessionsRegister::test_sessions_register_accepts_vendor_host` (~lines 1228-1248). Diff the two: if the plumbing test asserts any field (`agent_kind`, `mcp_host`) or hop (register → `GET /sessions` → serialized row) the server test does not, add those assertions to the server test.
- `test_helix_announce_plumbing.py`'s 2 tests ≈ `tests/test_server.py::test_announce_endpoint_sets_model_id` + `test_announce_endpoint_with_ide_override_sets_agent_override_via` (~lines 1293-1336) plus `tests/test_registry.py` `test_update_announcement_*` (~lines 1335-1388). The only potentially-unique bit is the `VSCODE_PID` env → `detect_ide` hop; that unit lives in `tests/test_ide_fingerprint.py` — if the plumbing test asserts the env-var reaches the announce payload end-to-end and no server test does, fold ONE such assertion into the nearest server announce test.

Additionally, in `tests/test_server.py` itself: merge `TestContextPacketEndpointFreshness` (~lines 570-605, 2 tests) into `TestContextPacketEndpoint` (~1139-1222). The freshness class re-ingests a gene and re-asserts the same `verified/stale_risk/refresh_targets` shape; its only unique delta is asserting `verified[0]["status"] == "verified"`. Move that assertion (and the re-ingest setup if needed) into the main class as one test, delete the extra class.

- [ ] Protocol step 1 on all four files named above
- [ ] Diff, fold missing assertions (if any), delete the two plumbing files
- [ ] Merge the freshness class into `TestContextPacketEndpoint`
- [ ] Protocol step 2 on `tests/test_server.py tests/test_registry.py tests/test_ide_fingerprint.py`. **Expected delta: collected −3 to −4 overall (plumbing −3, freshness −1 or −2 with the fold; report exact); all pass.**

### Task 5: Delete the two vault-manager delegation tests

**Files:**
- Modify: `tests/test_vault_manager.py` (delete `test_full_export_method` ~line 75, `test_trace_export_method` ~line 92)

`VaultManager.full_export`/`trace_export` are thin delegations to writer functions; `tests/test_vault_e2e.py::test_full_cycle` (~line 40) drives both through the manager end-to-end. KEEP `test_disabled_vault_does_nothing`, `test_start_creates_vault_root`, `test_stale_sentinel_cleaned_at_startup` — those are unique lifecycle coverage. Verify `test_full_cycle` actually calls `manager.full_export` and `manager.trace_export` before deleting.

- [ ] Protocol step 1 on `tests/test_vault_manager.py tests/test_vault_e2e.py`
- [ ] Verify coverage, delete the two functions
- [ ] Protocol step 2. **Expected delta: collected −2; rest pass.**

### Task 6: Delete the superseded phase1 know-counter test

**Files:**
- Modify: `tests/test_telemetry_phase1.py` (delete `test_know_decision_counter_labels`, ~lines 212-244)

Superseded by `tests/test_telemetry_wiring.py`: `test_miss_abstain_emits_counter` (~60), `test_no_promoter_match_emits_reason` (~80), `test_know_emits_confidence_histogram` (~97) — a strict superset (adds the confidence histogram). Verify the wiring trio covers all three outcomes (know/abstain/miss) with label assertions before deleting. Touch NOTHING else in phase1 — its dashboard phantom-killer and real-integration tests are unique keepers.

- [ ] Protocol step 1 on both files
- [ ] Verify superset claim, delete the function
- [ ] Protocol step 2. **Expected delta: phase1 collected −1; rest pass.**

### Task 7: Replace 2 subprocess PYTHONHASHSEED tests with mock-based asserts

**Files:**
- Modify: `tests/test_bench_determinism.py` (delete `test_bench_orchestrator_sets_pythonhashseed_zero` ~line 257, and the explicit-override sibling ~line 324)
- Modify: `tests/test_bench_orchestrator.py` (add 2 fast tests)

Source contract: `benchmarks/bench_orchestrator.py` ~line 483 does `env.setdefault("PYTHONHASHSEED", "0")`. `test_bench_orchestrator.py` already unit-tests spawn-env construction with mocked Popen (`test_spawn_passes_cwd_and_pythonpath_to_subprocess` ~446, `test_spawn_preserves_existing_pythonpath` ~479). Add, in that same class/style, two tests asserting on the built env dict: (a) PYTHONHASHSEED defaults to "0"; (b) a pre-set PYTHONHASHSEED value is respected. Then delete the two slow subprocess tests from `test_bench_determinism.py`. Preserve every other determinism test.

- [ ] Protocol step 1 on both files
- [ ] Add the 2 mock asserts; delete the 2 subprocess tests; run both files
- [ ] Protocol step 2. **Expected delta: determinism −2, orchestrator +2 (net 0); all pass, orchestrator file measurably faster.**

### Task 8: Delete the static budget-defaults pin

**Files:**
- Modify: `tests/test_config_default_honesty.py` (delete `test_budget_defaults_are_shipped_values`, ~line 104)

It pins `expression_tokens==7000`, `max_genes_per_turn==12`, `splice_aggressiveness==0.3`, `decoder_mode=="condensed"` — already covered dynamically by the drift-ratchet `test_shipped_toml_matches_code_defaults` (~line 78, `_walk_drift` over ALL fields). **Caution:** the ratchet FAILS at baseline on this machine (local helix.toml drift — see Global Constraints). Read the ratchet's failure output: if the drift it reports is unrelated to `[budget]` (e.g. synonyms/mem_sync sections), the ratchet still structurally covers `[budget]` and deletion proceeds — note this in your report. If the reported drift IS in `[budget]`, keep the pin and report BLOCKED-equivalent reasoning instead. Keep everything else in the file.

- [ ] Protocol step 1
- [ ] Verify ratchet covers `[budget]`, delete the function
- [ ] Protocol step 2. **Expected delta: collected −1; rest pass.**

### Task 9: Remove cross-file dense-blob duplicates and adapter-parity overlap

**Files:**
- Modify: `tests/test_dense_recall.py` (delete `test_v2_blob_roundtrip_matches_json` and `test_backfill_v2_idempotent`, ~lines 271-363 — ONLY if covered, see below)
- Modify: `tests/test_sharded_adapter_parity.py` (merge `test_adapter_covers_known_caller_surface` ~202 into `test_adapter_covers_full_knowledgestore_surface` ~354)

For dense: the blob-codec round-trip is covered by `tests/test_ingest_dense_v2.py::test_vec_to_blob_*` (~371-411). Read both sides. If either recall-side test asserts something ingest-side does not (e.g. backfill idempotency vs pure codec round-trip), MOVE that assertion/case into `test_ingest_dense_v2.py` rather than deleting it silently, then delete the recall copy.
For parity: the two surface-coverage tests overlap; merge into one test that asserts the full surface (union of both lists), delete the narrower one.

- [ ] Protocol step 1 on all three files
- [ ] Fold-then-delete per above
- [ ] Protocol step 2. **Expected delta: dense_recall −2 (or −1 with a note if one was folded as a new case into ingest_dense_v2), parity −1; all pass.**

### Task 10: Merge the never-running real-hardware files

**Files:**
- Create: `tests/test_hardware_real_device.py`
- Delete: `tests/test_hardware_cuda_real.py`, `tests/test_hardware_rocm.py`
- Leave alone: `tests/test_hardware_mps_smoke.py`

The two files are byte-for-byte parallel (identical `_reset_hardware_cache` fixture; `*_recommended_batch_size_is_positive` looping ("rerank","splice","splade","nli"); `*_auto_picker_lands_on_*`), differing only in device string and env gate (`HELIX_TEST_CUDA=1` at cuda_real ~19-22 / `HELIX_TEST_ROCM=1` at rocm ~17-20). Write one file with a module docstring explaining the opt-in gates, and parametrize the two tests over `device ∈ {cuda, rocm}` with a per-param `pytest.param(..., marks=pytest.mark.skipif(...))` gate reproducing each original env check exactly. Keep the existing `requires_real_cuda`/`requires_rocm` markers if the originals carry them.

- [ ] Protocol step 1 on both old files (expect 4 collected, 4 skipped)
- [ ] Write merged file, delete originals
- [ ] Protocol step 2 on the new file. **Expected delta: 4 collected → 4 collected (2 defs), all skipped locally exactly as before.**

**⏸ PHASE GATE 1:** Orchestrator runs every Phase-1-touched file together, then commits: `chore(tests): delete dead tests, relocate diagnostic script (phase 1/5)`.

---

## Phase 2 — conftest fixture hoisting

### Task 11: Add canonical shared helpers to conftest.py (single agent, sequential)

**Files:**
- Modify: `tests/conftest.py`

**Interfaces (Produces — Phase 2b/3/4 rely on these exact names):**
- `class MockCompressorBackend:` — canonical mock ribosome backend with `complete(self, prompt, system="", temperature=0.0) -> str`. Derive the canonical body from `tests/test_server.py` ~line 22 (`_ServerMockBackend`) — the system-prompt-sniffing variant that returns tagging JSON when the system prompt contains the tagging marker and splice/codon JSON otherwise. Before writing it, diff ALL copies (test_server.py:22, test_registry.py:982, test_health.py:21, test_pipeline.py:24, test_ingest_metadata.py:32, test_session_delivery.py:543, test_swap_db.py:46, test_helix_announce_plumbing.py [deleted in Phase 1], test_vendor_host_plumbing.py [deleted], test_gene_src_prefix.py:38, test_ribosome.py:27) and make the canonical class the superset that satisfies the common contract. Document divergent variants in the class docstring.
- `def make_helix_config(**overrides) -> HelixConfig:` — returns `HelixConfig(ribosome=RibosomeConfig(model="mock", backend="none", ...), genome=GenomeConfig(path=":memory:", ...))` matching the shape repeated in ~22 files; `**overrides` merge section-level replacements (e.g. `make_helix_config(budget=BudgetConfig(...))`).
- `def make_client(config=None, backend=None) -> TestClient:` — builds `create_app(...)` + `fastapi.testclient.TestClient` the way test_server.py does; defaults to `make_helix_config()` and `MockCompressorBackend()`.
- `def run_cli(argv: list[str]) -> tuple[int, str, str]:` — the 9-line helper duplicated in all 12 `tests/test_cli_*.py` files (io.StringIO + `contextlib.redirect_stdout/stderr` around `helix_context.cli.main(argv)`, returning exit code, stdout, stderr — match the duplicated implementation's exact return convention, whatever it is).
- `@pytest.fixture def reset_hardware_cache(...)` — the cache-reset body copy-pasted in test_hardware.py:15-20 / test_deberta_backend.py:20-24 / test_nli_backend.py:19-23 (NOT autouse globally — files opt in with a one-line autouse wrapper or direct request).

Style: follow the existing conftest documentation convention (see the `FakeBGEM3Codec` block) — a short "why shared" comment per helper. Import `create_app`/`TestClient` lazily inside `make_client` so conftest import cost stays flat for non-server tests.

- [ ] Read 4-5 of the MockBackend copies; write the canonical superset
- [ ] Add the five helpers
- [ ] `python -m pytest tests/test_config.py tests/test_cache.py -q` (cheap smoke: conftest imports cleanly)
- [ ] `python -m pytest tests/ --collect-only -q -m "not live"` collects with no errors. **Expected delta: collected ±0.**

### Task 12: Migrate files to the shared helpers (parallel, one agent per file-group)

**Files (each bullet = one independently assignable migration; NO test additions/deletions — collected count per file must be unchanged):**
- Server-client group (swap local MockBackend class / inline `HelixConfig(...)` / local `client` fixture for `MockCompressorBackend` / `make_helix_config` / `make_client` imported `from tests.conftest import ...`): `test_server.py`, `test_registry.py`, `test_pipeline.py`, `test_health.py`, `test_ingest_metadata.py`, `test_session_delivery.py`, `test_swap_db.py`, `test_gene_src_prefix.py`, `test_ribosome.py`, `test_admin_refresh.py`, `test_expand.py`, `test_caller_model_class.py`, `test_clean_isolation.py`, `test_lazy_encoders.py`
- CLI group (replace the local `_run` helper with `run_cli`; keep per-file `fake_session` mocks as-is unless byte-identical): all 12 `tests/test_cli_*.py`
- Hardware group (replace copy-pasted `_reset_hardware_cache` bodies with the conftest fixture): `test_hardware.py`, `test_deberta_backend.py`, `test_nli_backend.py`, `test_hardware_real_device.py`

**Hard rule per file:** run the file's tests after the swap. If any test that passed in step 1 fails, or needs its assertions changed to pass, the local variant is behaviorally divergent — REVERT that file to its local class and report the divergence instead. (Pre-existing baseline failures in `test_lazy_encoders.py` and `test_pipeline.py` — see Global Constraints — stay failing; that is not a divergence signal.) Migration must be a pure refactor. Do NOT migrate `tests/test_launcher_*.py` — their `create_app` is the launcher REST app, a different function.

- [ ] Per file: protocol step 1 → swap → protocol step 2. **Expected delta per file: collected ±0, all pass.**

**⏸ PHASE GATE 2:** Orchestrator runs the full non-live suite, then commits: `refactor(tests): hoist shared MockBackend/config/client/CLI helpers into conftest (phase 2/5)`.

---

## Phase 3 — Parametrizations (11 tasks, disjoint files, parallel-safe)

Every task here follows the same recipe: read the file; collapse EXACTLY the named cluster into parametrized test(s) using the canonical shape; every deleted method's literal inputs/expectations become a `pytest.param(..., id="...")` row; nothing outside the named cluster changes. Protocol step 1/2 around it. **Expected delta: collected count equal or higher; named function definitions gone; all pass.**

### Task 13: `tests/test_density_gate.py` — TestIsDeniedSource (31 → 2)
Collapse the 31 one-assert methods (~lines 43-167) into two parametrized tests (`test_denied_paths`, `test_allowed_paths`) or one `(path, expected)` table — follow whichever reads better with the file's class structure. Keep the class if the file is class-organized. All other classes in the file untouched.

### Task 14: `tests/test_coderag_bench_harness.py` — scalar tables (~34 → ~7)
Collapse: `TestNdcgAt` (6, ~52-77), `TestRecallAt` (4, ~84-101), `TestPrecisionAt` (4, ~108-123), `TestParseDocIdx` (8, ~277-300), `TestTokenEstimate` (4), `TestPercentile` (4), `TestPreviewTokenEstimate` (4) → one parametrized test per class. `TestRunPipeline` (17) and `TestScoreQueriesMocked` (10) are real behavior tests — do not touch.

### Task 15: `tests/test_repobench_r_harness.py` — scalar tables (~20 → ~5)
Collapse: `TestAccAt` (9, ~43-71), `TestTok` (5, ~94-114), `TestKsForLevel` (3, ~78-87), `TestRankRandom` (3) → one parametrized test each. `TestFullPipelineFixture` (6) untouched. Preserve the `pytest.importorskip("rank_bm25")` gates (~179-196) exactly where they are.

### Task 16: `tests/test_additive_weight_plumbing.py` — per-tier clones (18 → 4)
Collapse the nine `test_<tier>_weight_scales_tier` (~318-388) into one test parametrized over the 9 tier names, and the nine `test_zero_<tier>_weight_kills_tier` (~394-444) into a second. EXCEPTIONS kept as standalone tests: the `fts5` cap-clamp case (~331) and the `lex_anchor` boost-gate case (~428) — if those two tiers behave differently inside the loops, exclude them from the parametrize rows and keep their dedicated tests. Golden byte-identity tests (~249-298) and config-plumbing tests (~450-474) untouched.

### Task 17: `tests/test_registry.py` — three clusters (~19 → ~8)
(a) `TestStatusFromHeartbeat` (~343-362): 5 boundary tests on `_status_from_last_heartbeat` → 1 parametrized. (b) The vendor_host module-level block (~1135-1228, 7 fns) and announce block (~1231-1333, 7 fns) are the same seven-step sequence over two field sets (`agent_kind, mcp_host` vs `ide_detected, ide_detection_via, model_id`) → parametrize over a field-group descriptor (dict of field→sample value) yielding ~7 tests total. (c) `TestSchemaMigration` column/index presence (~80-153): fold the per-column tests into one parametrized column-presence table. Everything else (81 − ~19 tests) untouched.

### Task 18: `tests/test_launcher_tray.py` — three clusters (58 → ~48)
(a) `TestMenuActions` error-swallow trio (`test_start_handles_already_running` ~83, `test_start_handles_supervisor_error` ~88, `test_stop_handles_not_running` ~101) → parametrize `(handler, exception)`. (b) The pulse-stop trio (`test_dismiss_handler_stops_pulse` ~635, `test_restart_obs_service_acknowledges_and_stops_pulse` ~642, `test_open_obs_log_dir_acknowledges_and_stops_pulse` ~655) → parametrize over the action; each row asserts `_install_pulse_active is False` after. (c) `TestHardwareFallbackBalloon` (~926-1006, 4 tests) → extract a `_make_hardware_info(...)` factory + parametrize `(requested, active, sentinel, expected)`. Preserve the PIL `skipif` gates (~51/59).

### Task 19: `tests/test_headroom_bridge.py` — routing tables (~10 → ~2)
`TestPickSpecialist` per-language tests + `TestDetectLanguage` per-extension tests → two parametrized tests (`(domain, expected_specialist)`, `(ext_or_src, expected_language)`). `TestLiveSpecialists` and everything else untouched.

### Task 20: `tests/test_hardware.py` — three clusters (~10 → ~4)
(a) `test_auto_picks_{cuda,cpu,rocm,mps}` (~186-224) → 1 parametrized over (mocked backend flags → expected device_type/device). (b) `test_batch_size_{24gb,4gb,under_4gb}` (~404-431) → 1 parametrized over (vram_total → expected batch). (c) cpu_brand source trio (~61-87: py_cpuinfo / platform fallback / terminal) → 1 parametrized. Multi-GPU fall-through (~241-294) and config-flow (~480-512) tests untouched.

### Task 21: `tests/test_stress.py` — query family (~8 → ~2)
`TestGenomeStress`: `test_cross_domain_query_{caching,data_structures,biology,fluid}` (~204-217) → 1 parametrized over (query, expected-domain hit); `test_synonym_query_{slow,web}` + `test_entity_query_{alphafold,raft}` → 1 parametrized (or fold into the same table if the assertion shape is identical). Live/chunking classes untouched.

### Task 22: `tests/test_snow_oracle.py` — cascade surfaces (6 → 1)
`test_finds_answer_in_{entities,key_values,complement,content,neighbor}` + `test_returns_miss` (~25-120) → 1 parametrized test over (fixture-surface, query, expected hit/miss + expected surface label).

### Task 23: Telemetry getter-smoke consolidation (~5 → 1 across 3 files)
Target state: ONE parametrized getter-registry test in `tests/test_telemetry_wiring.py` extending `test_all_new_getters_resolve` (~253, walks 14 getters) to also assert caching identity (`getter() is getter()`). Then: delete `tests/test_telemetry_phase1.py::test_new_getters_return_cached_noop_instruments` (~163, 6-getter subset) and the three caching-only tests in `tests/test_telemetry_pipeline.py` (~40, ~99, ~109) — but ONLY the assertions that are pure duplicate caching checks; if a pipeline test also asserts instrument *behavior* (e.g. histogram record on stage timing), keep that part. The consolidated test's getter list must be the UNION of all getter names from the three files.

**⏸ PHASE GATE 3:** Orchestrator runs all 13 touched files, then the full non-live suite, and commits: `refactor(tests): parametrize 11 boilerplate clusters (phase 3/5)`.

---

## Phase 4 — File merges (3 tasks, disjoint, parallel-safe)

### Task 24: Merge the four build_fixture_matrix files
**Files:** Create `tests/test_build_fixture_matrix.py`; delete `tests/test_build_fixture_matrix_auto_subshard.py`, `_parallel.py`, `_resume.py`, `_silent_fail.py`.
All four import the same `scripts/build_fixture_matrix.py` module via an identical `sys.path.insert` preamble. Merge into one file with four class groups (`TestAutoSubshard`, `TestParallel`, `TestResume`, `TestSilentFail`) preserving every test; ONE shared preamble; dedupe the file-tree builder helpers (`_make_files` and the per-test tmp_path builders) into one module-level helper. Preserve the two `@pytest.mark.slow` marks (parallel ~71/~235). **Expected: 32 collected before = 32 after; 4 files → 1.**

### Task 25: Merge label tests
**Files:** Modify `tests/test_host_labels.py` (absorb all 6 tests from `tests/test_model_labels.py` as a `TestModelLabels` class or section); delete `tests/test_model_labels.py`.
Sibling pure-function modules (`model_labels.py` docstring: "Same pattern as host_labels"). Keep all 18 tests. **Expected: 18 collected across the pair before = 18 in one file after.**

### Task 26: Merge collector tests
**Files:** Modify `tests/test_launcher_collector.py` (absorb `tests/test_collector_host_label.py`'s tests); delete `tests/test_collector_host_label.py`.
Both instantiate the same `StateCollector`. While merging, trim the 2-3 assertions that re-verify label pretty-forms already unit-tested in host/model label tests (e.g. the "Codex" mapping ~collector_host_label:48 vs host_labels:20; "Claude Opus 4.7" ~:83 vs model_labels:10) — the collector layer needs one wire-connected assertion per panel, not the label table again. **Expected: 27 collected across the pair before → 24-27 after (report exact); all pass.**

**⏸ PHASE GATE 4:** Orchestrator runs touched files + full non-live suite, commits: `refactor(tests): merge redundant test files (phase 4/5)`.

---

## Phase 5 — Final verification (orchestrator, inline)

- [ ] `python -m pytest tests/ -m "not live" -q` → all pass, zero regressions vs baseline
- [ ] `python -m pytest tests/ --collect-only -q -m "not live"` → final count; produce the accounting table (baseline cases, deleted, parametrized, final) and reconcile every delta against the per-task expectations
- [ ] `git status` clean except intended changes; no stray files
- [ ] Final commit if anything uncommitted; summary report

## Explicitly OUT of scope (flagged follow-ups, do not do)
- Deduping `tok`/`BM25` between `benchmarks/coderag_bench.py` and `benchmarks/repobench_r*.py` (source change → separate PR; until then both parallel test suites stay)
- Deleting stale `scripts/bench_claude_matrix.py` (dead product tooling, no tests pin it — separate PR)
- Rewriting the two xfail HGT tests against metadata semantics (Task 1 deletes; a rewrite is a feature decision)
- Import-path modernization of the ~40 files importing via legacy shims (zero test-count effect; churn)

# Archive and efficiency audit — 2026-11-10

Read-only audit of the whole repo for an archive pass and an efficiency pass.
Nothing was moved, deleted or changed while producing it.

**Receipt.** Branch `beta` at `3f6337de` (0 commits behind `origin/beta`; `origin/master` is 28 behind beta).
2,070 tracked files. Six parallel read-only audit workers (tests, package, scripts, benchmarks, docs,
CI/deps/hygiene); the orchestrator re-checked the HIGH-confidence claims against the code before
including them (each such check is marked ✔). Suite timing run: `.venv` Python 3.14.3,
`pytest tests/ -m "not live" -q -rs --durations=80` → **1 failed, 5,890 passed, 7 skipped,
25 deselected in 540.7 s**.

---

## 1. Headline numbers

| Area | Tracked size | Finding |
|---|---|---|
| `benchmarks/` | ~83 MB working tree (~65 MB blobs), 766 files | 22.4 MB byte-identical copies; 15.7 MB archive candidates; 5 broken harnesses |
| `docs/` + `wiki/` | 12.5 MB, 448 files | ~1.6 MB finished plans to archive; 66 broken links + ~45 stale module paths in live docs |
| `scripts/` | 1.45 MB, 140 files | 34 keep, 43 keep-as-receipt, 58 archive, 5 delete; 6 fail at import |
| `cymatix_context/` | 177 `.py` | ~1 dead module, ~2k lines serving killed/default-off features, 840 ms package import |
| `tests/` | 359 `.py`, ~102k lines | suite is healthy (no permanent skips, no unused fixtures); ~2 min of avoidable wall time |
| CI | 8–11 min per run | full suite runs twice; 2 duplicate jobs; no pip cache; no path filters |
| Local disk (not git) | ~140 GB | 104 GB `benchmarks/dogfood`, 21 GB `benchmarks/results`, 13 GB across 94 worktrees |

Deleting files will **not** shrink git history (104 MB packed); only a history rewrite would, and that is
not recommended.

---

## 2. Bugs found along the way (fix regardless of the archive pass)

| # | Finding | Evidence | Severity |
|---|---|---|---|
| B1 | **PLR silently off on pip installs.** `joblib` (and scikit-learn/numpy for the artifact) is declared in no dependency or extra, yet `[plr] enabled = true` by default. The `ImportError` is swallowed, so packets lose `plr_confidence` with no warning. Ranking is unaffected (it is a query-level confidence head). CI masks it by `pip install joblib scikit-learn` by hand. The model file `training/models/stacked_plr.joblib` is also outside the wheel and matched by `.gitignore:85`. | ✔ `pyproject.toml` (no joblib), `retrieval/fusion_plr.py:42`, `server/helpers.py:562-565`, `ci.yml:57,81,124` | Medium |
| B2 | **Windows-only test failure** `test_transcript_identity_hides_the_project_folder`: `write_text("one\n")` writes CRLF on Windows. Test bug, not product bug. Fix: `write_bytes(b"one\n")`. | ✔ hash in the failure equals `sha256(b"one\r\n")`; `tests/test_compaction_recall.py:224-236` | Low |
| B3 | **6 scripts and 5 benchmark harnesses fail at import** — modules moved into sub-packages in PR #90: `cymatix_context.claims/claims_analyze/provenance/sema/headroom_bridge/claims_graph/tie_break/cwola`. Two are cited by user docs (`scripts/backfill_claims.py` ← `docs/INTEGRATING_WITH_EXISTING_RAG.md:569`; `benchmarks/bench_cymatix_rag_composition.py`, `bench_external_retriever.py` ← same doc). | ✔ `scripts/backfill_claims.py:24`, ✔ `benchmarks/bench_stale_claim_avoidance.py:45`, `bench_cymatix_rag_composition.py:231,463` | Medium (user-facing) |
| B4 | **Tray auto-restart looks for `Start-cymatix-tray.bat`**; the tracked file is `start-cymatix-tray.bat`. Harmless on Windows, breaks on case-sensitive filesystems. | ✔ `launcher/tray.py:738` | Low |
| B5 | **Wrong `TYPE_CHECKING` imports** `from .genome import Genome` inside `retrieval/` resolve to a nonexistent `retrieval.genome`. Runtime-harmless; type checkers are wrong. | ✔ `retrieval/sr.py:34`, `retrieval/seeded_edges.py:50` | Low |
| B6 | **A test that asserts nothing** (unfinished stub). | ✔ `tests/test_freshness_gate.py:478` | Low |
| B7 | **Docs give wrong usage**: `ingest_all.py --roots/--ext` (flags don't exist), `ingest_all.py <source_root>` (no positional), `docs_tidy_reingest.py <path>` (ignores args). | `docs/ops/SKILLS_BUNDLE.md:147`, `docs/operator-runbooks.md:860`, `docs/TROUBLESHOOTING.md:733` | Low |
| B8 | **Stale facts in `CLAUDE.md`**: says v0.10.0 (code is 0.11.4) and "~4,165 tests" (5,890 pass). No `v0.11.4` tag exists yet. | ✔ `CLAUDE.md:3`, `pyproject.toml` version, `git tag` | Low |

---

## 3. Wave A — safe mechanical cleanup (no owner decision needed)

Each item is reversible and touches nothing a receipt depends on.

1. **Dedup that needs no code change (~8 MB):**
   - 36 grain-ladder seed copies in `benchmarks/dogfood/grain_ladder/<corpus>_c<grain>/` — `seed_bench()` re-copies them on any rebuild and nothing reads them afterwards (✔ `run_grain_ladder.py:107-118`). 7.1 MB.
   - `benchmarks/snow/results/baseline_oracle_only_{preshard_d78097b,posttombstone_124555b}.json` — identical to `snow_oracle-only_2026-04-1{6,7}.json`, no referrers. 0.85 MB. Record the SHA provenance in the commit message.
   - `docs/research/data/2026-07-11-semantic-probe-50k-full.json` (hyphen twin of the underscore file; repoint one link). 0.49 MB.
   - Three `docs/research/data/2026-07-16-receipt-260-rrfgate-*-off.json` (identical to the receipt-255 controls, both uncited). 86 KB.
2. **Delete superseded scripts:** `scripts/backfill_bgem3.py` (writes the old column; `_v2` replaces it), `backfill_sema_embeddings.py` (broken; `backfill_sema.py` replaces it), `backfill_cwola_sema.py` (broken), `ops/run_full_suite.ps1`, `ops/run_overnight_bench.ps1`.
3. **Delete dead package code:** `cymatix_context/aliases.py` (no importer; fix the `wiki/Lexicon.md` line), `ContextManager.learn_async` (no caller), the unreachable `"claude"` branches in `config.py:66-67,108,120-121` and the `ribosome.claude_model` knob, the `tomli` fallback (`config.py:21`; Python ≥ 3.11).
4. **Fix B2–B8** above (one-line import fixes for the user-doc-cited harnesses and scripts; docs usage lines; `CLAUDE.md` version/test count).
5. **Repo hygiene:** remove duplicate `.gitignore` lines (`dist/`, `*.egg-info/`, `.obsidian/`, `overnight_logs/`); add `.tmp-*/` and `.codex-remote-attachments/`; add `!training/models/stacked_plr.joblib*` so the shipped model is not accidentally untracked.
6. **Test hygiene:** drop never-false skip guards (`importorskip("yaml")` ×2 — pyyaml is core; double `hypothesis` guard in `test_calibration.py:316,327`); merge the two no-assert idempotency tests in `test_registry.py:137,144`; migrate tests off the `ribosome`, `hgt`, `mcp_server` shims (small set).

---

## 4. Wave B — archive moves (owner picks the destination)

**Destination question:** move into an in-repo archive directory, or out to the private
`mbachaud/cymatix-receipts` mirror. Out-of-repo is the bigger win but breaks links from issues/PRs;
for receipts dated ≥ 2026-10-14 confirm the mirror already holds them first (unchecked).

| Group | Size | Inbound links to rewrite | Notes |
|---|---|---|---|
| `scripts/` archive class (58 files): April one-shot backfills, personal-machine ingest drivers, `e8_*`, `docs_tidy_*`, `pwpc/*` except `sprint3.py` + `export_with_window_features.py`, `ops/overnight_*`, 8 `bench_chain` watchers/sequencers | 470 KB | few (historical docs only) | `pwpc/sprint3.py` trains the shipped PLR model — keep and fix its input script. Do **not** archive the 5 scripts pinned by `tests/test_bench_cover_index_pin.py`. |
| `benchmarks/snow/results` ablation reruns + April `benchmarks/results/*.json` not named by code | ~8.2 MB | archive docs only | Keep `.gitkeep`, `snow_oracle-only_*`, and the 19 results files code names. |
| BEIR gemma tail-head ladder receipts (2026-10-14) | 4.3 MB | CHANGELOG via an index JSON | Confirm mirror first. |
| Unreferenced `erb/configs` (19) and `grain_ladder/configs` (8) TOMLs | 1.3 MB | none | Grain configs are regenerable by `make_arm_configs.py`. |
| Uncited bench harnesses (`cb_score_*`, `diag_coact_reserve_fire.py`, `erb/probe_*` ×5, `erb/w2_cover_reachability.py`, …) | small | none | |
| `docs/superpowers`, `councils`, `prds`, `investigations`, `audits`, pre-October `reviews`, finished `design` docs | ~1.57 MB | ~100 | Plans that shipped or were abandoned (evidence per file in the docs worker's notes). |
| `docs/architecture/2026-06-07-pipeline-and-switchboard.md`, `docs/ROADMAP.md` (frozen at 0.9.0) | 52 KB | few | Snapshot docs that now mislead. |
| `training/finetune_*`, `export_*` (LLM-ribosome era), `deploy/pypi-tombstone/` (once confirmed published) | small | few | Keep `training/models/`. |

**Do not move:** `docs/benchmarks/`, `docs/research/` (and their cited data), the stage-1..7 specs,
`docs/benchmarks/cymatix_probe_lexical.toml`, the grain-ladder runners (they generate the
README-cited receipts), `docs/archive/` as a whole (84 references from benchmark code).

---

## 5. Wave C — feature-code removal (owner decision per feature)

Each removes package code plus its tests together; none should be split.

| Feature | Package code | Tests | Recommendation |
|---|---|---|---|
| COVER walk (killed by receipt, PR #408) | `retrieval/cover_walk.py` + ~33-line block in `knowledge_store.py` + ~10 `cover_walk_*` knobs | `test_cover_walk.py` (24) | **Remove.** Keep the ingest-side gene_relations reverse index if anything else uses it. |
| `[mem_sync]` (superseded by `[sync]`) | `mem_sync.py` (337 lines), `scripts/run_mem_sync.py`, toml section, config whitelist | `test_mem_sync.py` (10) | **Remove** (CLAUDE.md row too). |
| Legacy 3-tool MCP server | `mcp/server.py` (368 lines, self-described "rollback only") | part of `test_wire_consumers.py` | **Remove.** |
| `ClaudeBackend` | `backends/compressor.py:212` (~73 lines) | — | Remove, or move into `scripts/claude_core_ingest.py` (itself archive). |
| PKI query path (default off since 08-17) | ~430 lines across `knowledge_store`, `storage/indexes`, otel | `test_pki_gate.py`, part of `test_pki_compaction.py` | Remove the query path; **keep** compaction (`/admin/compact` PKI route) so old stores can reclaim the table. |
| Old aliases kept "one release" | `RIBOSOME_DECODER`, `_ABSTAIN_MARKER` (past due since 2026-05) | 4 test files | Remove and migrate tests. |
| SPLADE (receipts null-to-negative) | ~600 lines | 13 test files | Decision: retire or keep opt-in. |
| Dense BGE-M3 (opt-in, documented) | ~1,200 lines | many | Keep; consider isolating into one module. |
| Additive fusion, `blend_mode="legacy"` | interleaved with RRF | ~38 files mention additive | Not yet: the shard paths (`sharding.py:238`, `shard_router.py:1201`) still use the additive label. |
| Bench-only retrieval helpers | `retrieval/lexical_rescue.py`, `relevance_window.py`, `encoding/chunk_fetch.py` | own tests | Move next to `benchmarks/bench_cymatix_rag_composition.py`. |

---

## 6. Wave D — efficiency

**CI (biggest wall-clock win; current runs 8–11 min, critical path = full-suite job):**

1. The full-suite job runs `pytest tests/ -m "not live"` **twice** (without, then with, the spaCy model; `ci.yml:163,169`), ~4.6 min each. Replace the first run with the targeted no-spaCy subset → ~4 min saved per run.
2. `test-linux` and `test-symbol-graph` re-run files the full suite already covers with a superset of extras (~2.6 runner-min). Delete them after checking required-check names in branch protection.
3. Add `cache: pip` to every `setup-python` (CPU torch + extras reinstalled ~66 s × 5 jobs).
4. Add a docs/benchmarks-only paths filter (needs a skip-job pattern if checks are required).
5. Add `timeout-minutes` to jobs that lack it; two full-suite runs hit the 60-min timeout on 2026-11-09 — the intermittent stall is still live and worth its own investigation.
6. Pin `ruff`; decide whether `weekly-embeddings` (mostly mocked tests already covered; never exercises BGE-M3) is kept, trimmed or retired.

**Test suite (540 s locally):**

- ~10 `/health` tests each take ~6.3 s probing the default upstream (localhost:11434); `test_server.py` already monkeypatches `_probe_upstream`. A shared autouse patch saves ~45–60 s.
- `test_stress.py:61` and `test_ribosome.py:84` probe Ollama over HTTP **at import time**, on every collection (✔).
- Four outliers: `test_launcher_app.py` (34 s), `test_build_fixture_matrix.py::TestParallel` (31 s), `test_observability_supervisor.py::test_shutdown_terminates_all_children` (25 s, despite mocked Popen), `test_launcher_collector.py` (17 s). Investigate; mark the deliberate ones `slow` and deselect by default.
- `pytest-xdist` is neither installed nor configured.
- `tiktoken`-dependent `test_packet_token_cost.py` and the `requires_headroom` tests never run in CI (deps not installed).

**Runtime:**

- `import cymatix_context` costs ~840 ms because `__init__.py:24-43` eagerly imports `context_manager`, the store, `ribosome`, `tagger`, `sema`. Every CLI, config and MCP import pays it. A lazy `__getattr__` in `__init__` is the top runtime follow-up.
- `backends/compressor.py` imports `httpx` at module level (~194 ms) only for default-disabled backends.
- 104 broad `except Exception:` blocks with `pass`/`continue` bodies (26 in `knowledge_store.py`, 22 in `context_manager.py`). Tier 5 was previously hidden by exactly this pattern; the retrieval-path ones deserve a read.
- Split candidates: `knowledge_store.query_docs` (1,603 lines), `context_manager._build_context_impl` (791), `config.load_config` (756 lines of hand-written field copying), `server/routes_admin.setup_admin_routes` (one ~1,400-line closure).
- Duplicated helpers: `_repo_root` ×3 identical in `launcher/`; `_is_windows`/`_port_is_free` ×2.

---

## 7. Local disk and git hygiene (report only; owner decisions)

- 94 worktrees (~13 GB). **53 are clean and fully merged** into beta. Release-witness worktrees should be checked against live bench infra in `F:\tmp` before pruning.
- 143 local and 56 remote branches are merged into beta. **Never delete `cla-signatures`** — it is the CLA bot's datastore (`cla.yml:33`).
- Main checkout sits on local `master` at v0.11.0 (159 behind `origin/master`) with 64 dirty entries and ~45 untracked docs/scripts.
- 2 old stash entries; 35 `tmp_obj_*` garbage objects (420 KB).
- `.venv-kompress040` (1.8 GB) looks like a leftover from a past A/B.
- 99 tracked files match `.gitignore` patterns (force-added); decide `git rm --cached` vs explicit `!` exceptions.
- Largest blob in history: an accidental 57.5 MB `genome_llm_backup.db`. Removing it needs a history rewrite — not recommended.

---

## 8. Not covered

- `.env.example` vs env vars read in code (the file read was refused by permission settings).
- References from outside this repo (GitHub issues/PRs, cymatix-receipts mirror, cc-exchange, the website): "unreferenced" here means unreferenced inside this repo.
- Whether the receipts mirror holds receipts dated ≥ 2026-10-14.
- Runtime replay of keep-as-receipt scripts; argparse/SQL/route compatibility of archive-class scripts.
- Root causes of the four slow test outliers and of the CI stall.
- sdist contents (not built).
- Which of the ~93 env-only `CYMATIX_*` knobs are meant to be bench-only.

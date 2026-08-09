# Audit Remediation Implementation Plan (2026-08-08)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the verified subset of the 2026-08-08 codebase-audit findings — concurrency bugs, misleading codemod damage, CI silent-skips, doc/changelog gaps, two safe deletions, and two default-inert security opt-ins — without touching anything the verification pass refuted or reclassified as live.

**Architecture:** Every change ships default-inert per repo convention (untouched configs behave byte-identically). Fixes were verified adversarially by a 9-agent pass on 2026-08-08 against master `338a844`; every file:line below comes from that pass. Two waves: Wave 1 tasks touch disjoint files (parallelizable); Wave 2 tasks share `config.py` / `routes_admin.py` / `helpers.py` and run sequentially.

**Tech Stack:** Python 3.14, FastAPI, SQLite/FTS5, pytest (`python -m pytest tests/ -m "not live"`), Windows (subprocess flags per global CLAUDE.md).

## Global Constraints

- Default-inert: untouched configs must reproduce today's behavior byte-identically.
- `tests/test_no_helix_leftovers.py` must stay green (it scans only `cymatix_context/`; whitelist pinned at 6 lines).
- Do NOT restore helix names anywhere — the 0.8.5 clean break is deliberate. Exception: `benchmarks/bench_needle.py` gold labels, where `helix-context/helix.toml` is the honest label for the frozen pre-rename beds (whitelisted residual class).
- Do NOT delete: `aliases.py` (public import surface per docs/ROSETTA.md:280), `write_queue.py` (parked live code, maintained in #346), `mcp/server.py` (rollback entry point), any `fusion_mode="additive"` code (sharded-path blocker).
- `except Exception:` never bare `except:`; all new HTTP calls need explicit timeouts.
- Known environmental test failure to ignore: `test_no_helix_leftovers.py::test_helix_context_package_is_unimportable` fails locally because `helix-context 0.7.2b1` is pip-installed editable at `F:\Projects\helix-context` (passes in CI).

---

## Wave 1 — disjoint files, parallelizable

### Task 1: SPLADE lazy-load lock

**Files:**
- Modify: `cymatix_context/backends/splade_backend.py:112-142` (`_ensure_loaded`)
- Test: `tests/test_splade_precompute.py` (append)

**Interfaces:** No signature changes. Adds module-level `_load_lock`.

- [ ] **Step 1: Write the failing test** — copy the barrier pattern from `tests/test_lazy_encoders.py:244` (`test_concurrent_first_use_constructs_once`): 8 threads + `threading.Barrier`, monkeypatch the loader to a counting fake, assert the model constructor ran exactly once. Monkeypatch `splade_backend._model = None` first and patch `AutoModelForMaskedLM.from_pretrained` / tokenizer load with counting fakes (guard with `pytest.importorskip("transformers")` only if the test needs the real symbols — prefer patching the module attributes so no transformers import is needed).
- [ ] **Step 2: Run it, verify it fails** (or flakes — a race test may pass by luck; assert on the counter after forcing overlap with the barrier inside the fake loader).
- [ ] **Step 3: Implement** — add `import threading`, module-level `_load_lock = threading.Lock()`, and make `_ensure_loaded` double-checked (keep the existing fast-path `if _model is not None: return`, then `with _load_lock:` re-check before loading). Mirror `sema.py:_materialize` (:338-356). Do NOT touch the encoder-daemon remote path (:46-109, deliberately lock-free).
- [ ] **Step 4: Run** `python -m pytest tests/test_splade_precompute.py tests/test_splade_query_scoring.py tests/test_lazy_encoders.py -q` — all pass.

### Task 2: cymatix_status duplicate-tuple fix

**Files:**
- Modify: `cymatix_context/cli/cymatix_status.py:85-101, 142-154`
- Test: `tests/test_status.py`

- [ ] **Step 1:** De-duplicate (clean break makes the shim arm obsolete — do NOT restore helix names): `_MCP_NAME_CANDIDATES = ("cymatix-context", "cymatix")`, `_MCP_NAMES_CANONICAL = ("cymatix-context",)`, `_MCP_NAMES_NONCANONICAL = ("cymatix",)`, `_MCP_ENV_VARS = ("CYMATIX_MCP_URL",)`; delete `_MCP_MODULE_LEGACY_SHIM` and the self-referential note block at :150-154; rewrite the now-nonsensical comments at :85-98. Keep the separate `:167` `cymatix_context.mcp.server` legacy-layout branch untouched.
- [ ] **Step 2:** Fix tests: delete `tests/test_status.py:89-103` (`test_legacy_shim_module_is_canonical_equivalent` — post-codemod it duplicates `test_canonical_config_detected` plus a bogus `"note" in result` assertion); drop the byte-duplicate `test_old_pair_still_canonical` (:57-71) if it duplicates after the constant change.
- [ ] **Step 3:** Run `python -m pytest tests/test_status.py -q` — pass. Only observable change: canonical configs stop carrying the self-referential note in `--json`.

### Task 3: bench_needle gold-label repair (codemod damage)

**Files:**
- Modify: `benchmarks/bench_needle.py` — 11 gold lists + 11 paired `source:` fields (lines 60-68, 115-119, 138-143, 162-167, 538-653 region)

- [ ] **Step 1:** In each of the 11 affected gold_source lists, restore `"helix-context/helix.toml"` as the FIRST entry (matches the frozen beds and `docs/benchmarks/MULTI_VALID_GOLD.md:104,154-157`) and KEEP `"helix-context/cymatix.toml"` as an additional ANY-match entry (future post-rename beds). Revert the 11 paired `source:` metadata fields to `helix-context/helix.toml`. Do NOT touch query texts, `CYMATIX_URL`, or any `helix-context/` directory prefix.
- [ ] **Step 2:** Offline bed pre-check (no server): `python -c` sqlite query against `F:/Projects/helix-context/genome-bench-2026-04-10.db` confirming citations/genes exist with source containing `helix.toml` (read-only). Record the count in the commit message.
- [ ] **Step 3:** Optional live smoke if server startable: per `docs/benchmarks/BENCHMARKS.md:198,509` (`GENOME_DB=F:/Projects/helix-context/genome-bench-2026-04-10.db`), run `python benchmarks/bench_needle.py` and confirm the 14 helix-context needles keep non-zero gold_delivered and the toml needles recover gold_has_answer.

### Task 4: CI — un-skip transformers tests + minimal ruff + pipeline-test hygiene

**Files:**
- Modify: `.github/workflows/ci.yml:99` (add `nli` extra), new `lint` job
- Modify: `pyproject.toml` (add `[tool.ruff.lint]` table)
- Modify: `tests/test_pipeline.py:282-284` (move module-level importorskip to class level)

- [ ] **Step 1:** ci.yml line 99: `pip install -e ".[dev,ast,mcp,nli]" ...` (transformers>=4.30; CPU torch already pinned by line 96 — no CUDA wheel). Un-skips 4 fully-mocked tests incl. the #341 rerank device-routing test.
- [ ] **Step 2:** Add fast `lint` job: `pip install ruff` + `ruff check --select E9,F63,F7,F82 .`; matching `[tool.ruff.lint] select = ["E9","F63","F7","F82"]` in pyproject.toml. **Run `ruff check --select E9,F63,F7,F82 .` locally FIRST** — if it finds latent bugs, fix or per-file-ignore them in the same commit so CI lands green.
- [ ] **Step 3:** `tests/test_pipeline.py:284` — the module-level `pytest.importorskip("sentence_transformers")` kills all 28 tests in the file including ~20 that never touch SemaCodec. Move the guard onto the cold-tier class only (`@pytest.mark.skipif` on `TestColdTierWiring` or a lazy fixture). Verify: `python -m pytest tests/test_pipeline.py -q` collects and runs the non-cold-tier tests even with sentence_transformers absent (locally it's present, so assert via `--collect-only` count).
- [ ] **Step 4:** Do NOT add `embeddings` extra to CI (HF model downloads, flake risk) — noted as follow-up. Do NOT adopt mypy.

### Task 5: Safe deletions

**Files:**
- Delete: `scripts/codemod_cymatix_rename.py` (self-neutralized: `OLD_PKG, NEW_PKG = "cymatix_context", "cymatix_context"`; recoverable at `2e3f90e`)
- Delete: `scripts/_write_tcm.py` (4-line dead scaffold)

- [ ] **Step 1:** `git rm` both. Neither is imported, in `[project.scripts]`, or referenced by workflows/docs (codemod referenced only by a completed historical plan doc).
- [ ] **Step 2:** Run `python -m pytest tests/test_no_helix_leftovers.py -q` (scans only `cymatix_context/`, unaffected — but prove it). CHANGELOG note rides in Task 8.

### Task 6: filename_anchor dead-function cleanup + regression proof

**Files:**
- Modify: `cymatix_context/filename_anchor.py:122-129`
- Test: append to `tests/` (nearest existing filename-anchor or delete-path test file)

- [ ] **Step 1:** Write the regression test mirroring `tests/test_fts5_external_content.py:213`: upsert a gene with a filename-shaped source_id, `delete_gene`, assert `SELECT COUNT(*) FROM filename_index WHERE gene_id=?` == 0. (Proves the audit's "leak" claim stays refuted — `knowledge_store.py:5610-5616` sweeps it via `optional_tables`.)
- [ ] **Step 2:** Delete the never-called `remove_gene` (or annotate "superseded by KnowledgeStore.delete_gene's optional_tables sweep" if any doubt). Do NOT rewire delete_gene to call it (different exception semantics).
- [ ] **Step 3:** Run the new test + `tests/` files touching filename_anchor — pass.

### Task 7: Benchmark import repair (bench_cymatix_rag_composition)

**Files:**
- Modify: `benchmarks/bench_cymatix_rag_composition.py:57-65` (3 import lines), `:38` (stale docstring)

- [ ] **Step 1:** Point imports at post-#90 locations: `cymatix_context.retrieval.lexical_rescue`, `cymatix_context.encoding.chunk_fetch`, `cymatix_context.retrieval.relevance_window` (all 5 symbols verified present). Fix docstring line 38 ("Requires helix-context server" → cymatix).
- [ ] **Step 2:** Verify: `python -c "import ast, importlib; ..."` — actually import the module (or `python -c "import benchmarks.bench_cymatix_rag_composition"` from repo root with sys.path). Keep it: `docs/INTEGRATING_WITH_EXISTING_RAG.md` links to it twice as the reference implementation.

### Task 8: CHANGELOG retroactive 0.8.6 section + deletion notes

**Files:**
- Modify: `CHANGELOG.md` (insert `## 0.8.6 — 2026-08-05` between current Unreleased bullets and `## 0.8.5` at :49)

- [ ] **Step 1:** Insert the section derived from `git log v0.8.5..v0.8.6` with PR-level bullets: encoder daemon default-off (#331); perf slice-1 concurrency instrumentation (#330); ERB scale-cliff fixes (tag_prefix LIKE→range, stats() off hot path, SPLADE covering index; 829k median 111.7s→26.1s); per-layer encoder device knobs (BGE-M3 22.7x batched CUDA); #327 tag-tier DF cap + #328 authority_path_selectivity (default-off); #329 three-state upstream health; #323/#324 ingest --recursive venv skip; #325 pin mcp<2; #322 dogfood bed rebuild + gold pack. One-line note in 0.8.5 section: tagged 2026-07-27, never separately published to PyPI (folded into 0.8.6).
- [ ] **Step 2:** Current Unreleased bullets (#338, #341 — post-tag work) stay ABOVE the new section. Add Unreleased entries for this remediation pass (concurrency fixes, status fix, bench label repair, deletions, CI, security opt-ins).

### Task 9: Config-knob annotations + ablation-arm flag + fusion_mode comment

**Files:**
- Modify: `cymatix_context/config.py` (comments only: fields 281, 445, and the `[cymatics]` block 1450-1453; plus the fusion_mode removal note at :546)
- Modify: `docs/config-reference.md:609` (splice_threshold_scale presented as live — mark "parsed, not yet wired")
- Modify: `benchmarks/dogfood/ablate_cymatics.py:123` (comment: peak_width arm silently inert — runtime derives from splice_aggressiveness at context_manager.py:1228)

- [ ] **Step 1:** Annotate the 6 parsed-but-unread knobs as `# parsed, not yet wired (2026-08-08 audit)`: `colbert_enabled`, `seeded_edge_weight`, `n_bins`, `peak_width`, `use_embeddings`, `splice_threshold_scale`. Do NOT remove parse lines or fields (shipped cymatix.toml sets them; removal → `_warn_unknown` warnings = not byte-identical). vault.traces placeholders already labeled — no action.
- [ ] **Step 2:** At config.py:546 (fusion_mode) extend the comment: additive removal BLOCKED on sharded path — see Task 10's cross-refs.
- [ ] **Step 3:** Flag in the final report: ablate_cymatics arm-C numbers comparing peak_width variants are invalid (arm silently inert).

### Task 10: Additive-removal blocker cross-refs + ROADMAP row

**Files:**
- Modify: `docs/specs/2026-05-08-stage-3-rrf-fusion.md:125`, `cymatix_context/knowledge_store.py:607` (comment), `docs/ROADMAP.md` (new row/para)

- [ ] **Step 1:** At each v(N+2) removal note add: "BLOCKED on sharded path: rrf-native global-IDF splice (#265 deferred follow-up) + #264 boost gate migration + a third scale label or recalibrated floors for ShardedGenomeAdapter (sharding.py:213-236 is a deliberate score-scale label, not an accidental hardcode — do not flip it)."
- [ ] **Step 2:** ROADMAP: add "additive removal prerequisites (sharded path)" item under Track 1 listing the three prerequisites; name `fusion_mode="additive"` and `blend_mode="legacy"` separately (verified distinct knobs — blend_mode has no sharding dependency).

## Wave 2 — shared files, sequential

### Task 11: mtime cache — bound + honor the documented /admin/refresh contract

**Files:**
- Modify: `cymatix_context/retrieval/freshness.py:121-145`, `cymatix_context/server/routes_admin.py:619-625`
- Test: append to `tests/test_freshness_gate.py`

- [ ] **Step 1:** Tests first: (a) admin_refresh empties `_mtime_cache`; (b) cache never exceeds cap (insert 4096+ distinct paths via revalidate, assert len <= cap).
- [ ] **Step 2:** In `admin_refresh` add `getattr(cymatix, "_mtime_cache", {}).clear()` — fulfills the already-documented contract (context_manager.py:1258-1260 claims it; today it's false). In `freshness.revalidate_source`, before insert at :133/:145: if `len(mtime_cache) >= 4096`, drop TTL-expired entries (semantically invisible — they'd be re-stat'd anyway), fallback `clear()` if still over.
- [ ] **Step 3:** `python -m pytest tests/test_freshness_gate.py -q` — pass, incl. existing `test_revalidate_caches_mtime_60s_ttl`.

### Task 12: W1.6 part (a) — stop publishing aliased/unlocked score maps

**Files:**
- Modify: `cymatix_context/scoring/blend.py:235, 283` (+ third TCM branch), `cymatix_context/knowledge_store.py:1446-1447`, `cymatix_context/server/helpers.py:271, 285, 502-504`
- Test: append to `tests/test_request_scoped_scores.py`

- [ ] **Step 1:** Test first: after blend publishes, mutate the request-local dict and assert `genome.last_query_scores` is unaffected (copy semantics); plus a lock-contract test if cheap.
- [ ] **Step 2:** blend.py publications: `with genome._last_query_scores_lock: genome.last_query_scores = dict(scores)` (all three branches). knowledge_store.py:1446-1447 cold-tier per-key mutation: take `_last_query_scores_lock`. helpers.py:271/285/502 reads: snapshot under the same lock.
- [ ] **Step 3:** Byte-identical for serial callers — run `python -m pytest tests/test_request_scoped_scores.py tests/test_freshness_gate.py -q` plus a broad `-k "know or packet or fingerprint"` sweep.
- [ ] **Step 4:** W1.6 proper (threading the :1976-1983 snapshot into `_stage6_know_miss` and route builders) is OUT OF SCOPE — issue draft in the final report.

### Task 13: Opt-in admin bearer token + swap-db allowlist (default-inert)

**Files:**
- Modify: `cymatix_context/config.py` (`[server] admin_token: str = ""`, `swap_db_roots: list = []`), `cymatix_context/server/app.py` / `routes_admin.py`
- Test: new `tests/test_admin_auth.py`

- [ ] **Step 1:** Tests first: (a) default config → all admin routes behave exactly as today (no auth header needed); (b) `admin_token="secret"` → `/admin/*` without header → 401; with `Authorization: Bearer secret` → 200; (c) `swap_db_roots=["F:/allowed"]` → swap-db path outside root → 403; default `[]` → unrestricted.
- [ ] **Step 2:** Implement as a FastAPI dependency on the admin router (+ `/ingest`, `/consolidate`): empty token = immediate return (byte-identical). Path-root check via `os.path.commonpath` guarded `except ValueError` (different drives on Windows).
- [ ] **Step 3:** Document in `docs/config-reference.md` + README security note (loopback default bind ≠ auth; same-host processes can reach it).

### Task 14: Opt-in control-tag neutralization (default-off)

**Files:**
- Modify: `cymatix_context/config.py` (`[budget] neutralize_control_tags: bool = False`), `cymatix_context/context_manager.py` (~3328, assembly loop on spliced_text)
- Test: new `tests/test_control_tag_neutralization.py`

- [ ] **Step 1:** Tests first: (a) default off → document containing `<cymatix:no_match reason="x" do_not_answer="true"/>` passes through byte-identical; (b) flag on → content-sourced `<cymatix:` becomes inert (e.g. `<\u200bcymatix:` or `&lt;cymatix:`) while the GENUINE no-match tag (emitted only when `parts` is empty, :3397 — separate branch) is untouched; (c) legitimate prose containing the word "cymatix:" without `<` unaffected.
- [ ] **Step 2:** Implement: in the assembly loop, when flag on, `spliced_text.replace("<cymatix:", "&lt;cymatix:")` before appending to parts. Narrow scope, no regex.
- [ ] **Step 3:** Document in config-reference + a security note: this closes tag-forgery only; general indirect prompt injection via document text remains (inherent to context injection) — downstream agents adopting CYMATIX_NO_MATCH_FRAGMENT should treat the tag as trustworthy only when this flag is on.

## Final gate

- [ ] Full suite: `python -m pytest tests/ -m "not live" -q` — only the known environmental helix-editable-install failure allowed.
- [ ] `ruff check --select E9,F63,F7,F82 .` green locally.
- [ ] Review full `git diff` against this plan; grouped commits (concurrency / status / bench / CI / docs / deletions / security), then report with issue drafts for: monolith decomposition, W1.6 proper, security default-flip receipts, CI embeddings extra, write_queue #346-branch coverage.

## Explicitly NOT doing (verified reasons)

- Monolith refactor (`knowledge_store.py` 5,879 lines confirmed) — too risky for this pass; issue draft instead.
- Deleting `aliases.py` / `write_queue.py` / `mcp/server.py` / additive path / embedding_dense v1 column (document-as-legacy only, comments at ddl.py:84/111 — fold into Task 9's commit).
- Renaming `helix-context/` bench path prefixes (intentional whitelist; renaming would zero 14 needles).
- Wholesale orphan-script deletion (re-derived: only `_write_tcm.py` is dead weight; the rest are operator entry points).
- mypy adoption, CI embeddings extra (HF download flake), plr expected_sha256 wiring (optional — deferred to keep this pass tight).

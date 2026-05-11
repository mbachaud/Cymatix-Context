# Stage 1 — `bench_needle_1000` Two-Axis Split + Read-Only Isolation

Plan: helix-context retrieval-fix, Stage 1 of 6 (council 2026-05-08). Stage 1 is the only zero-dependency entry point — Stages 2-6 stack on it for measurement.

## 1. Goals + non-goals

**Goals**
- Split the headline benchmark into `located_n1000` (4-axis locator query, target retrieval@1 ≥ 0.85) and `blind_n1000` (legacy bare-key form, target ≥ 0.35).
- Make `clean=true` request-time isolation a real read-only contract: zero knowledge store mutation when set, verified by row-count assertion.
- Fix the `injected_tokens` ↔ `injected_tokens_est` field-name mismatch so summarize() reports non-zero token telemetry across both ASK_PROXY paths.
- Default the new harness to `--axis located` so the headline number is the locator-bearing one going forward.

**Non-goals**
- No knowledge store schema change (keep tags-tag indices, KV columns, lifecycle tier column as-is).
- No compressor / LLM call introduction in retrieval (LLM-free retrieval design pillar).
- No regeneration of historical `n1000_t030_*` result dirs — they remain valid baselines for the `blind` axis only.
- No tuning of decoder, classifier, or BGE-M3 codec in this stage (that's stages 2-4).
- No change to dim-lock bench itself; it stays the curve-shape diagnostic, this bench is the headline number.

## 2. Surface area

| File:line | Change (what, not how) |
|---|---|
| `benchmarks/bench_needle_1000.py:328-341` | Replace single `build_query` with `build_query_blind` (preserves current form) and `build_query_located` (mirrors dim-lock variant 4); dispatch via `--axis`. |
| `benchmarks/bench_needle_1000.py:411` | Rename emitted key `injected_tokens` → `injected_tokens_est` in ASK_PROXY=0 branch. |
| `benchmarks/bench_needle_1000.py:464` | (Already `injected_tokens_est`, no change — kept as the canonical name.) |
| `benchmarks/bench_needle_1000.py:509` | Confirm summarize() reads `injected_tokens_est` only; remove any tolerance for legacy key. |
| `benchmarks/bench_needle_1000.py` (new helper) | Import `_split_source` from `bench_dimensional_lock` to extract `(project, module, filename)` for located queries. |
| `benchmarks/bench_needle_1000.py` (new CLI) | Add `--axis {located,blind,both}` arg; default `located`. `both` runs each sequentially, emits two result dirs. |
| `benchmarks/bench_needle_1000.py:891-905` | Output-path templating: `n1000_located_<ts>/` or `n1000_blind_<ts>/`. |
| `helix_context/context_manager.py:1226-1259` | Wrap `touch_genes`, `link_coactivated`, `store_harmonic_weights`, `store_relations_batch` block in `if not read_only:`. |
| `helix_context/server.py:762-770` | Already correct — confirm `_request_read_only` honors `clean=true`. No change, just guard with a regression test. |
| `tests/test_clean_isolation.py` (new) | Three pytest cases (see §7). |
| `benchmarks/_run_n1000_located.sh` (new) | Convenience launcher; mirrors existing `_run_n1000_t*.sh`. |
| `benchmarks/_run_n1000_blind.sh` (new) | Same, for the legacy form. |

## 3. Query templates

**`blind_n1000`** — preserves current `build_query` exactly:
- `What is the value of {key_phrase}?` (default branch, `bench_needle_1000.py:341`)
- Domain-specific branches at `:335-340` retained as-is (port/size/path/name).
- Worked examples:
  - `key=cold_start_threshold, value=0.62` → `What is the value of cold start threshold?`
  - `key=upstream_port, value=11434` → `What is the upstream port in the helix source?`
  - `key=DOWNLOAD_PATH, value=C:/Steam` → `What is the download path mentioned in the code?`

**`located_n1000`** — mirrors dim-lock variant 4 (DEWEY=0 mode), full locator:
- Template: `What is the {key_phrase} value in {project}/{module}/{filename}?`
- Fallback when locator components missing (preserves dim-lock behavior at `bench_dimensional_lock.py:251-263`):
  - 3 axes available: `What is the {key_phrase} configured in {project} {module}?`
  - 2 axes: `What is the value of {key_phrase} in {project}?`
  - 1 axis only: degrade to blind form (record as `degraded=True` in row).
- Worked examples (using `_split_source` from dim-lock):
  - `key=cold_start_threshold, source=F:/Projects/helix-context/helix_context/config.py` → `What is the cold start threshold value in helix-context/helix_context/config?`
  - `key=upstream_port, source=F:/Projects/helix-context/helix_context/server.py` → `What is the upstream port value in helix-context/helix_context/server?`
  - `key=DOWNLOAD_PATH, source=C:/SteamLibrary/steamapps/libraryfolders.vdf` → `What is the DOWNLOAD PATH value in steamapps/libraryfolders?` (project=SteamLibrary anchor consumed)

## 4. Harness contract

- New flag `--axis {located,blind,both}`; default `located`.
- `--axis both` runs `located` first, then `blind`, in the same process; emits to two distinct output dirs.
- Output path resolution:
  - `located` → `benchmarks/results/n1000_located_<YYYYMMDD-HHMMSS>/results.json`
  - `blind` → `benchmarks/results/n1000_blind_<YYYYMMDD-HHMMSS>/results.json`
- `OUTPUT_PATH` env var still respected for one-off runs (overrides the templated path).
- Each result file gains a top-level `"axis": "located"|"blind"` field (next to `harness_version`).
- Incremental jsonl path mirrors the axis: `..._located.incremental.jsonl`.
- All `clean=true` request payloads in this bench remain unchanged; they now also imply `read_only=true` server-side after Stage 0.

## 5. Read-only isolation patch

Sketch for `helix_context/context_manager.py` around 1226-1259:

```python
# BEFORE (unconditional):
self.genome.touch_genes(expressed_ids, ts)
self.genome.link_coactivated(expressed_ids, ts)
if harmonic_weights:
    self.genome.store_harmonic_weights(harmonic_weights)
if relation_graph:
    batch = [...]
    if batch:
        self.genome.store_relations_batch(batch)

# AFTER (gated):
if not read_only:
    self.genome.touch_genes(expressed_ids, ts)
    self.genome.link_coactivated(expressed_ids, ts)
    if harmonic_weights:
        self.genome.store_harmonic_weights(harmonic_weights)
    if relation_graph:
        batch = [...]
        if batch:
            self.genome.store_relations_batch(batch)
```

`log_health` at 1262-1272 is intentionally OUTSIDE the gate — health logging is observation, not knowledge store mutation. Confirm `genome.log_health` writes only to `health_log` table (not `genes` / `coactivation`) before merging; if it touches state, also gate it.

**Read-only contract scope.** `read_only=True` means *no knowledge store learning or mutation* — zero writes to `genes`, `coactivation`, `harmonic_weights`, or `gene_relations`. Observational writes to `health_log` ARE permitted (they record observations about the request, not state mutations). Bench harnesses requiring byte-identical DB snapshots between rows must take a fresh DB copy per run; `read_only=True` alone is not equivalent to a no-op transaction. Stage 7's per-document `last_verified_at` revalidation cache also respects this contract: in-memory cache may update under `read_only`, but the column itself is not written.

Audit: `store_relations_batch` writes to `gene_relations`. `_express` already accepts `read_only=read_only` (line 841/851) and is the only other write path during build_context — confirm in code review that `_express(read_only=True)` already skips its own writes (per existing parameter contract). Add an assertion in the new test that the `gene_relations` row count is unchanged after a clean=true call.

## 6. Token-field rename fix

- **Winning name:** `injected_tokens_est` (already canonical at `bench_needle_1000.py:464` and `:509`; matches the `_est` suffix convention used for `budget_tokens_est`, `total_tokens_est`).
- **Edit:** `bench_needle_1000.py:411` change `"injected_tokens": injected,` → `"injected_tokens_est": injected,`.
- **Historical jsonl migration:** none. Old result files in `benchmarks/results/n1000_t030_*` keep their original keys; they belong to the `blind` axis baseline and are read-only history. Add a one-line tolerance in any aggregator script (out of scope here): `r.get("injected_tokens_est") or r.get("injected_tokens", 0)`.
- **Schema bump:** increment `HARNESS_VERSION` from 2 → 3 to mark the rename + axis split.

## 7. Test plan

`tests/test_clean_isolation.py`:

- `test_clean_true_does_not_mutate_genome`
  - Setup: open snapshot DB, snapshot `SELECT COUNT(*) FROM genes`, `SELECT SUM(access_count) FROM genes`, `SELECT COUNT(*) FROM coactivation`, `SELECT COUNT(*) FROM gene_relations`.
  - Action: call `manager.build_context(query="port", clean=False, read_only=True)` then `read_only=False` baseline call against a copy.
  - Assert: under `read_only=True`, all four counts are byte-identical pre/post. Under `read_only=False`, at least one count strictly increases.

- `test_clean_flag_implies_read_only`
  - Action: POST `/context` with `{"query": "...", "clean": true}` and no explicit `read_only`.
  - Assert: server-side spy on `manager.build_context` records `read_only=True` in kwargs.

- `test_response_mode_packet_with_clean_isolates_writes`
  - Action: POST `/context` with `{"query": "...", "clean": true, "response_mode": "packet"}` (the in-handler packet branch at `server.py:852`, distinct from the dedicated `/context/packet` route at `:1103`).
  - Assert: row counts unchanged exactly as in `test_clean_true_does_not_mutate_genome`. Confirms the `response_mode="packet"` branch within `/context` shares the same `read_only` plumbing as the dedicated `/context/packet` route — neither path leaks writes when `clean=true`.

- `test_located_axis_query_format`
  - Action: build a fake needle with `source="F:/Projects/helix-context/helix_context/config.py"`, `key="cold_start_threshold"`, run `build_query_located(needle)`.
  - Assert: returned string equals `"What is the cold start threshold value in helix-context/helix_context/config?"`.

- `test_blind_axis_preserves_legacy_format`
  - Action: same needle, run `build_query_blind(needle)`.
  - Assert: returned string equals `"What is the value of cold start threshold?"` (byte-identical to pre-split output).

- `test_token_field_uniformity`
  - Action: run a 2-needle dry-run with `ASK_PROXY=0` and `ASK_PROXY=1`.
  - Assert: every row dict contains `injected_tokens_est`; none contain bare `injected_tokens`.

## 8. Back-compat

- Old `benchmarks/results/n1000_t030_*` paths are NOT regenerated and NOT migrated. They become canonical historical baselines for the **blind axis only**, labeled retroactively in any comparison docs.
- New runs always emit `n1000_<axis>_<ts>/`. The legacy `n1000_t030_*` glob stays parseable by existing report scripts; add a comment in `summarize_results.py` (if any) noting old runs default to `axis=blind`.
- `HARNESS_VERSION=3` distinguishes new files from old without renaming the schema.
- Hugging Face uploader (`upload_to_hf`) accepts both old and new dirs; the dataset card adds an `axis` column (default `blind` for v1/v2 entries).

## 9. Acceptance criteria

- `bash benchmarks/_run_n1000_located.sh` against `genome-bench-2026-05-08.db` produces `summary.retrieval_rate >= 0.55` BEFORE Stages 2-4 are applied. (Sanity number that the bench redesign alone surfaces hidden recall — not a stage commitment.)
- `bash benchmarks/_run_n1000_blind.sh` reproduces the prior 13.8% headline within ±2pp on the same snapshot/seed (regression guard: bench split must not change the legacy measurement).
- `pytest tests/test_clean_isolation.py -v` passes all five cases against a freshly copied snapshot DB.
- `summarize()` output: `summary.tokens.avg_injected > 0` for both ASK_PROXY=0 and ASK_PROXY=1 runs (token-field rename verified end-to-end).
- No documents, coactivation rows, harmonic weights, or relations rows are added when running the located harness with `clean=true` (assert via row-count diff in test).

## 10. Out of scope

- `helix_context/genome.py` — no schema, index, or DDL changes.
- `helix_context/query_classifier.py` — no rule additions or cap retuning.
- `helix_context/codons.py` (BGE-M3 codec) — no changes.
- `helix_context/ribosome.py` — explicitly forbidden to introduce new compressor calls in the retrieval path.
- Decoder / splice / re-rank tuning — Stage 2-4 territory.
- `bench_dimensional_lock.py` itself — stays as the curve-shape diagnostic; only its `_split_source` helper is imported.
- HF dataset card schema migration — separate ticket once both axes have ≥3 runs each.

---

**Surface-area summary:** 1 bench file restructured, 1 server file confirmed (no edit), 1 manager file with a 4-line gate addition, 2 launcher scripts added, 1 test file added. No knowledge store, classifier, codec, or compressor changes. Headline metric becomes located retrieval@1; blind retrieval@1 retained as ambiguity-control.

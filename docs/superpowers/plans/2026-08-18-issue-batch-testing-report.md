# Issue batch-testing worker report — 2026-08-18

Companion to `2026-08-18-issue-batch-testing-paths.md`. All verification ran
against master `c06da8d` (post-merge of #379/#380/#382/#383) in a clean cloud
container (CPU-only, Python 3.11.15, torch CPU, spaCy + en_core_web_sm, no
sentence-transformers — i.e. the CI fresh-install condition plus torch).

## Baseline: full non-live suite on merged master

`pytest tests/ -m "not live"`: **3905 passed, 1 failed, 70 skipped, 25
deselected** (239s). CI on the same commit (run 32186395415, Python 3.12.13):
**3906 passed, 0 failed**.

The one local failure is real and diagnosed, and it is a **test fragility, not
a master regression**:

- `tests/test_abstain_tier.py::test_boundary_at_ratio_floor_does_not_abstain`
  pins the gate ratio exactly at the 1.8 abstain floor. The gate computes
  `ratio = top / (sum(scores)/n)` (`pipeline/tier_logic.py:105-106`).
  On CPython ≥3.12, builtin `sum()` uses Neumaier compensated summation
  (CPython gh-100425): mean = `0.8333333333333333`, ratio =
  `1.8000000000000003` → strict-`<` gate does not fire → BROAD → pass.
  On CPython 3.11 the naive sum gives mean `0.8333333333333334`, ratio =
  `1.7999999999999998` → gate fires → ABSTAIN → fail.
- The package declares `requires-python >= 3.11`, so a supported interpreter
  fails the shipped suite. Fix options: seed `_weak_setup` with exact binary
  fractions (e.g. top=1.5, ratio=2.0 halves/quarters), or nudge the boundary
  scores by ±1e-9 on the intended side of the strict-`<`.

## W-AB — Batch A+B claim verification (#376, #357, #371, #372, #373)

All five claims **CONFIRMED**:

1. **#376 CONFIRMED** — `encoder_daemon.py:139` `DEFAULT_PORT = 11439` collides
   with `bench_port = 11439` in all 7 ERB configs (`:191`), shipped
   `cymatix.toml:193`, `config.py:225`, and `coderag_bench_cymatix.py:322`.
   Fallback confirmed silent: `RemoteBGEM3Codec`/`RemoteSemaCodec`
   (`backends/encoder_client.py:494+`/`:661+`) log one WARN then degrade to
   debug-only in-process re-encode — a wrong listener on 11439 no-ops the
   daemon invisibly. Resolution: move the daemon default (avoid
   11437/11491-11493) + add an endpoint-identity probe so a non-daemon
   listener fails loudly.
2. **#357 CONFIRMED** — `config.cymatics.peak_width` is read only by its own
   parse (`config.py:1476`); scoring derives width from
   `aggressiveness_to_peak_width(0.3)` = **1.55** (`context_manager.py:1123-1130`,
   `scoring/cymatics.py:633`). `docs/config-reference.md:608` still documents
   the dead key. Resolution: remove or wire the key + config-honesty ratchet.
3. **#371 CONFIRMED** — `dense_embed_on_ingest`/`sema_embed_on_ingest` default
   true (`config.py:298/306`) while `dense_embedding_enabled` defaults false
   (`config.py:500`): ingest writes vectors retrieval never reads. Parent
   genes additionally get one *unbatched* BGE-M3 encode each
   (`context_manager.py:1452` → `knowledge_store.py:1753→4041`).
4. **#372 CONFIRMED (measured)** — no `PRAGMA synchronous` anywhere in the
   package; writer runs WAL + FULL (`PRAGMA synchronous = 2` verified).
   Measured: one 4-chunk file = **6 commits** (4 per-strand at
   `knowledge_store.py:1888` + 1 parent + 1 `store_relations_batch:4660`),
   each an fsync; demotions (`:5505/:5558`) add one commit each.
5. **#373 CONFIRMED** — no `nlp.pipe`/`n_process` anywhere; `tagger.py:312-316`
   runs the pipeline (minus lemmatizer only) once per strand. spaCy is
   mandatory at defaults: without it ingest falls through to
   `DisabledBackend` → `RuntimeError("Ribosome is disabled")`
   (`compressor.py:357-364`).

## W-D — Batch D concurrency correctness (#350, #342)

1. **#350 CONFIRMED (repro)** — know/miss builder reads genome-global
   `last_query_scores` (`server/helpers.py:271`), not the request-scoped
   snapshot the pipeline already captures at `context_manager.py:1874-1881`
   (which is never attached to the returned window). Deterministic two-thread
   repro: query A's know/miss inputs came entirely from query B's publication
   (top_score 0.42 instead of 10.0) while A's window still carried A's genes.
   Same-class contaminated readers: `helpers.py:285` (tier contributions),
   `helpers.py:473/:502` (PLR), `routes_context.py:336-341/:820-826`
   (citation scores). Minimal fix: stash the snapshot in `window.metadata`
   and have the server layer prefer it, genome-global as legacy fallback.
2. **#342 CONFIRMED, dormant at shipped defaults** — `query_docs` feeds the
   post-rerank, post-cut `ranked_ids` to `update_edge_evidence`
   (`knowledge_store.py:3880-3882`) with no rerank gate; rerank changes both
   order and membership of the top-`limit` set, so ON/OFF arms mutate
   `harmonic_links` differently (repro: co_count 1 vs 0 on the same seeded
   edge, same query). Both flags default off (`config.py:452/:812`). Minimal
   fix: snapshot fusion-order ids before the rerank seam and feed those to
   the evidence writer (keeps ON/OFF store mutations byte-identical, which
   the isolation receipts assume).

## W-E — Batch E cloud-provable parts (#327, #335, #344)

1. **#327 CONFIRMED (measured)** — `_apply_authority_boosts`
   (`knowledge_store.py:1961`, boost at `:2065`) awards +2.0 on any query
   term substring-matching the absolute `source_id` path. Repro (25-gene
   store): adding the project name to a query boosts **80% of the corpus
   (100% of the project's own genes)** and drops the gold doc from rank 1 to
   rank 21. Notably the mitigation already exists —
   `authority_path_selectivity` (`config.py:800`) scales the boost by
   per-term path selectivity and fully restores rank 1 in the repro — but it
   **ships default-false**. Remaining work is the bench gate to flip it.
2. **#335 harness coverage** — canonical `delivered_gold` helpers live in
   `erb/ablation_ladder.py:545/:605`; carried by **ablation_ladder** and
   **run_packet_replay** only. Lacking it: `run_server_scaling`,
   `run_cold_start`, `run_write_contention` (delivered surface, wrong field
   names), `run_latency_scaling`, `run_baseline`, `run_ladder`,
   `ablate_cymatics` (pool-basis, not delivered), `run_expression_arms`
   (partial). That list is the remaining #335 item-1 work.
3. **#344 trim map** — five candidate-cut sites in `context_manager.py`
   (`:1917` sub-query pool cut — unrecorded; `:2023-2063` assembly caps —
   cap value only; `:3224-3305` session elision; `:3384-3436` token-budget
   eviction — count only; `:2262-2292` delivery-visibility block). A minimal
   aggregate visibility block exists, but **no per-candidate delivery-loss
   ledger** (no cut gene ids, scores/ranks at eviction, or gold-among-cut
   flag). That's the open work.

## W-C — Batch C (#352 skip census, #353 write_queue tests)

1. **#352 census — and the issue's premise is partly wrong.** Without the
   embeddings extra, 5 skip entries hide **95 tests**: `test_sema.py:6`
   (22, whole module, intentional), `test_pipeline.py:286` (28 hidden, only
   6 actually need sentence-transformers → 22 collateral),
   `test_density_gate.py:737` (43 hidden, only 6 need ST → 37 collateral),
   `test_perf_instrumentation.py:344/:300` (1 each, correctly scoped).
   **The claim that "the importorskip-scoping half is already fixed" is
   FALSE on master** — both `test_pipeline.py` and `test_density_gate.py`
   still collect zero tests (module-scope skip; class-body `skipif` calling
   `importorskip` raises Skipped at import). Recommendation: fix scoping
   with class-level `skipif(find_spec(...) is None)` (recovers 59/95 tests
   at zero CI cost); keep skipping the 36 genuinely-ST tests in the fast
   leg; optionally add a nightly `[embeddings]` leg with an HF cache.
   Bonus finding: two `@pytest.mark.live` dense tests *fail* rather than
   skip without the extra (guards trip too late).
2. **#353 resolved-in-branch** — `tests/test_write_queue.py` authored (this
   PR): 15 tests / 4 classes exercising `GenomeWriter` against real temp
   genome DBs — external-content form detection, view-sourced indexing,
   the #338 re-upsert swap (no stale terms, one docsize row, FTS
   integrity-check), drift-heal via `fts_index_has_rowid`, the legacy
   contentful branch, submit_sql, per-future error isolation, stats,
   close-drains-pending, and a 4-thread concurrency smoke. **15/15 pass**
   (1.8s), stable alongside `test_fts5_external_content.py` +
   `test_genome.py`.

## Dispatch verdict roll-up

| Issue | Verdict | Next step |
|---|---|---|
| #376 | CONFIRMED | small fix PR (port + identity probe) |
| #357 | CONFIRMED | small fix PR (remove/wire key + ratchet) |
| #371 | CONFIRMED | default flip + #379 harness receipt |
| #372 | CONFIRMED (measured 6 fsyncs/file) | `[genome] synchronous` knob + batch commit |
| #373 | CONFIRMED | `nlp.pipe` batching PR |
| #350 | CONFIRMED (repro) | window-snapshot threading fix |
| #342 | CONFIRMED (dormant) | pre-rerank evidence snapshot fix |
| #327 | CONFIRMED (rank 1→21 repro) | bench gate to default-flip `authority_path_selectivity` |
| #335 | partial: 2 harnesses carry the metric | port metric to server benches |
| #344 | confirmed gap (no ledger) | design + implement ledger |
| #352 | census done; scoping claim FALSE on master (95 hidden, 59 recoverable) | scoping fix PR + CI decision |
| #353 | tests authored in this PR (15/15 green) | merge; issue closable |
| #384 (new) | abstain boundary test fails on Python 3.11 (sum() Neumaier change in 3.12) | fix `_weak_setup` seeding |

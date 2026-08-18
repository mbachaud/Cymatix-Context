# Issue batch-testing & resolution paths — 2026-08-18

State at time of writing: master `c06da8d` (post-merge of #379, #380, #382, #383).
32 open issues, grouped into seven batches by shared resolution path and shared
test surface. Each batch names: what resolves it, what proves it, and where the
proof can run (CI/cloud container vs. bench rig with beds + GPU/Ollama).

Legend for "runs where":
- **cloud** — runnable in a plain CPU container (pytest, code-level repro, docs)
- **rig** — needs the ERB beds (100k–829k), GPU, or Ollama; operator's bench box
- **mixed** — claim-verification and harness work in cloud; receipts on the rig

---

## Batch A — Config truth & hygiene (small fixes, fully unit-testable)

| Issue | Claim | Resolution path |
|---|---|---|
| #376 | encoder daemon default port 11439 collides with `[server] bench_port` | move daemon default to 11440 + collision test |
| #357 | `[cymatics] peak_width = 3.0` parsed but never read (scorer derives 1.55) | remove the key or wire it; add config-honesty ratchet test |
| #219 | epic: config unification / default honesty | parent tracker — A-batch items advance it |

Runs where: **cloud**. Test path: targeted pytest + config-ratchet tests.

## Batch B — Ingest write-path efficiency (#371, #372, #373)

Shared surface: the ingest loop (`context_manager.py:1283`, `knowledge_store.py:1888`,
`tagger.py:312`). Shared instrument: `benchmarks/dogfood/run_ingest_throughput.py`
(merged in #379) — three-arm before/after receipts.

| Issue | Claim | Resolution path |
|---|---|---|
| #371 | ingest still runs encoders retrieval no longer reads (dense_embed_on_ingest=true while retrieval dense off) | flip write-path default; decide sema_embed_on_ingest; receipt via #379 harness |
| #372 | no `PRAGMA synchronous` set → fsync per strand commit; 10-chunk file ≈ 12–22 fsyncs | `[genome] synchronous` knob default NORMAL + one commit per document; count-commits test |
| #373 | spaCy pipeline runs once per 4,000-char strand; `nlp.pipe` unused | `pack_batch` on `nlp.pipe`; A/B via #379 harness |

Runs where: **mixed** — claim verification + unit tests in cloud; full throughput
receipts best on the rig (CPU-only cloud numbers are still valid for the
fsync/batching deltas, invalid for GPU-adjacent claims).

## Batch C — Test-coverage & CI debt (#352, #353)

| Issue | Claim | Resolution path |
|---|---|---|
| #353 | `write_queue.py` has zero importers and zero tests, but carries the #346 FTS5 external-content discipline | author tests exercising the FTS5 branch; do NOT delete the module |
| #352 | 28 model-dependent tests still skip without the embeddings extra | census the skips; decide: add embeddings extra to one CI leg vs. keep skipping |

Runs where: **cloud** entirely.

## Batch D — Concurrency / cross-request correctness (#350, #342)

| Issue | Claim | Resolution path |
|---|---|---|
| #350 | know/miss + route builders read `genome.last_query_scores` written by other requests — reported confidence can be contaminated cross-request | thread the W1.6a request-scoped snapshot through know/miss + route builders; concurrency repro test first |
| #342 | Hebbian edge evidence written from reranked order — cross-arm contamination if `seeded_edges_enabled` flips | feed pre-rerank order (or gate writes when reranked); unit test |

Runs where: **cloud** (unit/concurrency-level repro; no beds needed).

## Batch E — Scoring correctness, receipts-gated (#327, #335, #344, #340, #366, #287)

Shared shape: the defect is provable at unit level in cloud, but the *fix* must
be admitted through bench receipts on the ERB beds (repo convention).

| Issue | Cloud-provable part | Rig-gated part |
|---|---|---|
| #327 | authority boost +2.0 on absolute-path substring — unit repro with tiny genome | recall receipts for the fixed matcher |
| #335 | delivered-gold metric wiring exists post-#379/#380; code-check | ANN `min_genes` sweep against the metric |
| #344 | adaptive admission design; ledger schema | delivery-loss ledger receipts at 829k |
| #340 | static-miss needle list already in receipts; classifier of miss class | entry-path levers measured at 829k |
| #366 | rank-capped dense / calibration harness code | four-scale re-measure |
| #287 | know/miss structural-zero repro (0% know_emitted) is receipt-backed; recalibration script | ERB-wide recalibration receipts |

Runs where: **mixed**.

## Batch F — Measurement campaigns & default-honesty docs (#354, #355, #356, #370, #336, #339, #374, #375, #377, #260, #264, #275, #359, #351)

Re-run/measure family — each blocked on beds, GPU, Ollama, or an operator
decision. Docs-only members (#374 CHANGELOG disclosure, #375 isolation-doc
relabel) are cloud-doable now from receipts already in the repo. #351 is an
operator decision (security defaults) + receipts. #377 is the v0.9.0 release
gate tracking all of the above.

Runs where: **rig** (except #374/#375/#377-hygiene checkboxes: **cloud**).

## Batch G — Architecture (#349, #205)

Sequenced extraction of the two monoliths (#349) and the 3-layer retrieval
profiles design (#205). Big, separate track; cloud-doable but not batch-testable
— each lands through its own PR sequence with the full suite as the gate.

---

## Dispatch plan (this session)

Cloud workers dispatched against merged master `c06da8d`:

1. **W-AB** — Batch A + B claim verification: #376 port values, #357 dead config,
   #371 write-path defaults, #372 fsync-per-strand, #373 nlp.pipe absence.
2. **W-C** — Batch C: run skip census for #352; author the #353 write_queue
   FTS5-branch tests.
3. **W-D** — Batch D: concurrency repro for #350; reranked-order evidence-write
   repro for #342.
4. **W-E** — Batch E cloud-provable parts: #327 unit repro of the IDF-blind
   authority boost; #335/#344 code-level wiring checks.

Baseline: full `pytest tests/ -m "not live"` on merged master (receipt in the
worker report).

Results are appended as the worker report in
`docs/superpowers/plans/2026-08-18-issue-batch-testing-report.md`.

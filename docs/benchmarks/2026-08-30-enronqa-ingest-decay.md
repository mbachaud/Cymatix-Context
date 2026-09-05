# EnronQA padded-bed ingest decay: profile receipt + scheduling fix

**Date:** 2026-08-30 · **Branch:** `claude/enronqa-bed` (fix branch `perf/fts5-ingest-decay`)
**Receipts:** `benchmarks/dogfood/receipts/ingest_decay_enronqa_2026-08-30.json` (profile),
`ingest_decay_enronqa_writer90s_2026-08-30.speedscope.json` (raw py-spy),
`entity_autolink_equivalence_enronqa.json` (equivalence gate for the fix)

## Question

`scripts/build_fixture_matrix.py` ingest throughput on the enronqa_padded
build (`F:/tmp/enronqa_padded_bed`, 517,353 files) decays from ~60 genes/s
to <5 genes/s as the bed grows. Writer pegged at ~1 core, ~zero I/O, whole
DB in OS cache. Prime suspect going in: FTS5 segment-merge amplification as
`genes_fts` grows. Directives: profile first; bed content must stay
byte-identical — only build *scheduling* may change.

## Verdict: FTS5 refuted; entity auto-linking is the decay

py-spy (90 s, 50 Hz, `--nonblocking`, attached to the **live** writer at
~289k committed genes) — 4,352 main-thread samples:

| writer wall time | where |
|---:|---|
| **89.5%** | `auto_link_by_entity` — the per-insert COVER-edge GROUP BY (`storage/co_activation.py:51`) |
| 6.6% | `sync_filename_index` — **unindexed** `DELETE FROM filename_index WHERE gene_id=?` (full scan of 279k rows) |
| 2.7% | commit (`_write_unit`) |
| **0.07%** | FTS5 insert (external-content) — 3 samples out of 4,352 |

Direct probe of the auto-link SQL on the live bed, using the newest gene's
15 entities: **148.9 ms** (sweeps 364,245 `entity_graph` posting rows into a
temp B-tree). The same query minus the two MIME hub entities: **7.4 ms**.

Mechanism: the CPU tagger extracts MIME plumbing as entities, so
`entity_graph` accumulates giant hubs — at 289k genes: `content-type`
171,696 rows, `content-transfer-encoding` 171,352, `javamail` 86,547,
`x-origin` 82,618. Nearly every email chunk carries the two big hubs, so
every insert's link query sweeps ~350k+ rows, and that sweep grows linearly
with the bed → O(N²) build. The log curve confirms: instantaneous rate
rises to ~61 g/s through the EnronQA core (low hub density, tagging-bound),
collapses once the padding roots start (~94k genes), then tracks
`rate ≈ C/N` with C ≈ 2.0e6→1.85e6 (61 g/s @ 63k → 8.3 @ 241k → 4.9 @ 322k).

Status-quo projection for the running build: **~26 h remaining** to ~670k
genes. With the writer fixed, tagging (6 workers) is the bottleneck again:
**~2 h** for the same remainder.

## Answers to the original (a)/(b)/(c)

- **(a) automerge/usermerge/crisismerge tuning + periodic merges:** moot.
  `genes_fts_config` holds pure defaults; the segment tree at probe time was
  healthy (23 segments / 9 levels, one level-4 merge in flight, ~460 MB of
  postings); the entire FTS write path is 0.07% of writer time.
- **(b) defer FTS population to a post-ingest rebuild:** would recover at
  most that same 0.07% today. The external-content `'rebuild'` command
  already exists (`storage/ddl.py`) if this is ever worth re-profiling on a
  5–10× bed after the real fixes.
- **(c) contentless / external-content FTS:** already shipped — `genes_fts`
  has been the external-content form since #338 (the live bed has no
  `genes_fts_content` shadow table). The 0.07% *is* the external-content
  insert cost.

## Fixes (this branch)

1. **`idx_filename_gene`** on `filename_index(gene_id)`
   (`filename_anchor.ensure_schema`). Content-neutral — an index changes no
   query results; existing beds pick it up on next open. Kills the 6.6%
   O(N) DELETE scan.
2. **`entity_autolink` scheduling knob** — `KnowledgeStore(entity_autolink=False)`,
   builder flag `--no-entity-autolink` / `CYMATIX_BFM_ENTITY_AUTOLINK=0`,
   recorded in the build manifest. Skips per-insert COVER-edge formation;
   `entity_graph` rows still written. The equivalence receipt (2,000-file
   deterministic sample, both arms sequential) shows `gene_id`, content, and
   the **full entity-row multiset** digests byte-identical; the only delta is
   `gene_relations` relation=5 COVER edges (present/absent). Those edges are
   default-inert at query time (W2.1 cover-walk KILL receipt) and already
   order-nondeterministic under `--parallel`
   (`ingest_equivalence_enronqa.json`: 8,675 seq vs 8,664 par), i.e. they were
   never inside the bed-identity bar.

Default behavior is unchanged (`entity_autolink=True` everywhere unless
opted out) — beds built without the flag remain byte-identical to before.

## Explicitly out of scope (needs its own receipt/issue)

- **Hub cutoff inside `auto_link_by_entity`** (skip entities whose posting
  list exceeds ~`PKI_NOISE_CUTOFF`-style bound): bounds per-insert cost for
  paths that keep linking ON (server `/ingest` has the same O(N²) exposure),
  but changes which edges form → retrieval-behavior change, not scheduling.
- **Tagger MIME-header entities**: `content-type` et al. are not entities;
  fixing extraction changes gene tags = bed content. Would also shrink
  `entity_graph` (3.6M rows at 289k genes) and de-noise the COVER graph —
  likely the right long-term fix, but it invalidates cross-bed
  comparability, so it must ride a bed-version bump.

## Options for the in-flight build (operator decision)

1. **Let it run**: ~26 h more, bed identical to plan.
2. **Stop → rebuild from scratch with `--no-entity-autolink`**: ~3.5–4 h
   total for a uniform edge-free bed (fresh blob builds always restart:
   blob mode unlinks the existing db).
3. **Stop → patch → re-run**: blob mode has no file-level resume (that's
   sharded mode), so this equals option 2.

The 4.6 GB partial at `F:/tmp/enronqa_padded_bed` stays valid for option 1
only; options 2–3 rebuild it.

## Residual decay past ~150k genes: CLOSED (2026-08-30 evening)

The operator resolved the in-flight-build decision as **stop → rebuild with
the hub cutoff** (`CYMATIX_BFM_HUB_CUTOFF=200`, PR #412 path — not the
`--no-entity-autolink` scheduling knob): `rebuild_cutoff200_v2` finished the
full 517,353-file / 596,707-gene bed in 4h58m vs the ~26 h uncapped
trajectory. The post-cutoff build is the controlled experiment for the
residual question, and it closes it
(`benchmarks/dogfood/receipts/ingest_residual_decay_enronqa_2026-08-30.json`):

- **The O(N²) signature is gone.** Worker tagging throughput is flat
  (~50 genes/s from N=20k to 580k, `observed_at` curve). The writer decays
  **mildly and linearly**, not hyperbolically: once the tagging workers
  drained (~09:40 local) the writer alone went 38.4 → 32.7 g/s over
  479k → 656k processed genes (26.0 → 30.6 ms/gene, +4.6 ms per 177k).
- **Root cause of the linear residual: the unindexed per-upsert
  `DELETE FROM filename_index WHERE gene_id = ?`**
  (`storage/indexes.py sync_filename_index`) — `EXPLAIN QUERY PLAN` shows
  `SCAN filename_index` (571,999 rows at end of build; only the
  `(filename_stem, gene_id)` PK and `idx_filename_stem` exist). Measured on
  the finished bed: **42.7 ms per warm full scan vs 0.002 ms with
  `idx_filename_gene`** (~20,000×). The magnitudes reconcile: the old
  build's py-spy 6.6% share at 204 ms/gene ≈ 13.5 ms ≈ a warm scan of the
  then-~280k-row table, and the clean-tail slope matches the table's growth
  rate.
- **FTS5 stays refuted** — no superlinear component in the post-cutoff
  curves (0.07% in the py-spy receipt).

**Fix = `idx_filename_gene` (commit `a39532f`, this branch).**
Content-neutral, applied to existing beds on next open by
`filename_anchor.ensure_schema`. With it, bulk builds become tagging-bound
at the flat worker ceiling (~3.3 h projected for this bed vs 5.0 h
measured).

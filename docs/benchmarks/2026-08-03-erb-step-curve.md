# ERB step test — latency vs bed size on blob mode: gradual √N, then a cliff

**Date:** 2026-08-03 · **Branch:** `perf/slice1-instrumentation` · **Box:** Windows 11,
Python 3.14.3, **48GB RAM** (the number that matters below) · fp16 dense matrix
(`CYMATIX_DENSE_MATRIX_DTYPE=float16`, A/B'd PASS earlier the same day).

**Question (user):** as the knowledge store grows toward EnterpriseRAG scale
(500k raw docs → ~830k file-chunk genes), is per-query latency a gradual climb
or a cliff — and where?

## Beds (all blob mode — verified: no shard tables)

| bed | path | size | genes | dense_v2 |
|---|---|---|---|---|
| dogfood | `genomes/dogfood/genome.db` | 224MB | 6,427 | 100% |
| erb_10k | `f:/tmp/sike-bed-pointers/stage/enterprise_rag_10k.db` | 0.9GB | 15,888 | 98.2% |
| erb_50k | `f:/tmp/sike-bed-pointers/stage/enterprise_rag_50k.db` | 4.5GB | 80,362 | 99.6% |
| erb_full | `f:/tmp/erb_blob.db` (built ~Jul 11-12) | **49.4GB** | **829,131** | 97.6% |

Queries: the July ERB sweep sets (`benchmarks/dogfood/erb/`, 470 queries,
per-bed gold, 100% gold membership verified in every bed). Harness:
`run_server_scaling.py` read arm, one uvicorn + real HTTP, canary +
recall/overlap anti-fooling gates. 10k/50k: 40 needles; full: 20 (c=1) / 10
per worker (c=2), rounds noted in receipts.

## The curve (c=1 medians, same query family)

| bed | genes | c=1 median | p95 | recall@12 | c=2 median ratio |
|---|---|---|---|---|---|
| erb_10k | 15,888 | 5,998ms | 13.4s | 0.85 | 1.42× |
| erb_50k | 80,362 | 13,494ms | 37.8s | 0.675 | 1.34× |
| erb_full | 829,131 | **111,673ms** | 158s | **0.45** | **1.58×** |

- **10k → 50k: 5.06× genes → 2.25× latency — almost exactly √N.** Gradual.
- **50k → full: 10.3× genes → 8.3× latency — the law changes to ~linear.**
  There IS a cliff between 80k and 829k genes.
- Event-loop canary stayed 17-20ms p95 at every scale — the server never
  wedges; the pipeline just grinds.
- recall@12 halves across the ladder (0.85 → 0.45) on the identical query
  set: quality erosion with corpus growth is real and now measurable.
- Concurrency penalty (c=2/c=1) does NOT grow with bed size in the cached
  regime (1.42× at 10k, 1.34× at 50k) — the executor wall (G1) is
  scale-independent. On the >RAM bed it rises to **1.58×** (c=2 median
  176.5s, p95 234s): two concurrent scan-heavy queries competing for a page
  cache neither fits in. Canary 16.7ms even under 176s requests. (c=2 ran
  the first 10 needles vs c=1's first 20, so its 0.30 recall is a different
  subset, not a concurrency quality loss; overlap between workers 0.999.)

## Where the 112s goes (ring-tail attribution, full bed, c=1)

| cost | p50 | at 50k (warm) | growth |
|---|---|---|---|
| SPLADE tier | **43.4s** | 225ms | 193× for 10.3× genes |
| tag_prefix tier | **35.1s** | 481ms | 73× |
| stats() in assemble (`_compute_health`) | **12.9s** | ~1.1s | ~file size |
| all other signals (fts5 2.3s, dense 2.1s, ann 2.0s, …) | ~13s | ~1s | ≈ linear, healthy |

Signal timers now cover ~92s of the 98.6s express stage at this scale — the
slice-1 instrumentation attributes the cliff almost completely. (The ~4.3s
express residual seen at 10k does not scale with N; separate item.)

## Mechanisms (all verified, none is "SQLite can't")

1. **Cache exhaustion is the amplifier.** 49.4GB bed vs 48GB RAM: every
   full-scan query shape degrades from RAM-speed to random disk I/O, and
   evicts the pages the healthy tiers need. The √N→linear transition is the
   bed outgrowing the page cache.
2. **`stats()` on the hot path** (known W-item, now quantified). COUNT +
   chromatin GROUP BY + `SUM(LENGTH(content))` + `SUM(LENGTH(complement))`
   full scans, ≥1×/request via `_assemble → _compute_health`, +1 in
   `/admin/swap-db`: measured 98ms (dogfood) → 1.1s (50k) → **11.1s warm /
   72.7s cold** (full). The cold call also broke `/admin/swap-db`'s 60s
   timeout — fixed via `CYMATIX_BENCH_SWAP_TIMEOUT_S` (test-first, 4dd71f0).
3. **tag_prefix tier: pathological join order.** `EXPLAIN QUERY PLAN` on the
   full bed: `SCAN genes` (all 829k) → probe `idx_promoter_gene` per gene →
   LIKE on every tag. Never uses `idx_promoter_value`. **No `sqlite_stat1`
   in any ERB bed — ANALYZE has never run** — the planner is flying blind.
4. **SPLADE tier: 147M-row postings, non-covering index.**
   `splade_terms` = 147,115,451 rows; top posting lists ~1M rows each
   ('phone' 1.03M, 'number' 905k). `idx_splade_term` covers `(term, weight)`
   but NOT `gene_id` → one random table b-tree hop per posting row.
   No posting-length cap / IDF pruning: ultra-common terms are scanned in
   full on every query.

## Fix-candidate probes (throwaway copy of the full bed)

_TBD — ANALYZE (join-order fix for tag_prefix) and covering-index
`(term, gene_id, weight)` (SPLADE) timed on a copy; results land here._

## Council input: 100k / 250k / 500k intermediate beds

Two ways to get the intermediate chunk sets:

- **Fresh ingest** of ERB subsets: matches production ingest exactly, but the
  July full-bed ingest ran across sessions Jul 2 → Jul 11 (days of wall
  time), and re-ingesting bakes today's tagger/SPLADE settings into the bed —
  a different instrument than the July blob.
- **Subset-carve from the existing blob** (new DB, copy first-N genes' rows
  across genes/promoter_index/splade_terms/entity/co-activation tables,
  rebuild FTS): hours not days, and holds every ingest parameter constant so
  bed size is the ONLY variable. Recommended for the cliff-location
  question; do one fresh-ingest bed later only if carve results look odd.

Size math (48GB box): 100k ≈ 6GB (fully cacheable), 250k ≈ 15GB (cacheable
with room), 500k ≈ 30GB (+ ~6GB models + OS ≈ the cache edge), full 49GB
(over). If the cliff is cache-driven, 500k is the knee probe; if
splade/tag_prefix costs are already multi-second at 250k warm, the fixes
matter before the cache does.

## Receipts

| receipt | file |
|---|---|
| 10k c=1,2 (40×2) | `benchmarks/dogfood/erb/receipts/erb_step_10k.json` |
| 50k c=1 (40×2, salvaged worker) | `.../erb_step_50k_c1_worker_salvage.json` |
| 50k c=2 (40×1) | `.../erb_step_50k_c2.json` |
| full c=1 (20×1) | `.../erb_step_full_c1.json` |
| full c=2 (10/worker) | `.../erb_step_full_c2.json` |
| stats() cost probe | `.../stats_cost_probe.txt` |

Ops notes: the 50k c=2 first attempt hit the 1800s level timeout (params,
not a wedge — c=1 data salvaged from the worker JSON); zombie sweeps clean
before/after every run; the full-bed hot-swap needs
`CYMATIX_BENCH_SWAP_TIMEOUT_S=1800`.

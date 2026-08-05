# Rerank GO at 829k · encoder throughput ceilings · Fork 1 review inputs

**Date:** 2026-08-04 · **Branch:** `perf/slice1-instrumentation` · Follows
`2026-08-04-recall-at-scale.md` (near-cutoff diagnosis) and the W3.2
closure (wall moved to shared encoder/GIL).

## 1. Cross-encoder rerank probe — GO, and it hit its ceiling

In-proc on the 829k probe bed, 30 carve-safe needles, config-faithful
store: fused top-50 from `query_docs_ann`, re-ordered by
`cross-encoder/ms-marco-MiniLM-L-6-v2` (CPU), recall@12 over the first 12
(receipt `benchmarks/dogfood/erb/receipts/rerank_probe.txt`):

| | recall@12 |
|---|---|
| fused order (base) | 0.433 |
| **cross-encoder rerank of top-50** | **0.500** |
| recall@50 of the pool (= rerank ceiling) | 0.500 |

2 needles recovered, 0 lost — the reranker converted **100% of the
available pool headroom**. Cost p50 1.8s / p95 2.0s per query (50 pairs,
CPU, bs=16) ≈ +7% on the 26.1s 829k median. Remaining misses are the
static-miss class (gold at pool position 600+) — out of rerank's reach
by design; deeper pools (k=100-200) buy little there.

**Production wiring caveat (why this was probed in-proc):** the existing
rerank stage reranks only the already-capped ~12 candidates
(`blend.py: ribosome.rerank(query, candidates, k=max_genes)`) — it
cannot recover rank-13+ gold — and enabling it requires
`[ribosome] backend="deberta"`, which also reroutes SPLICE through
DeBERTa. Landing this win needs: (a) rerank over the pre-cap fused
top-50, (b) a rerank-only enablement decoupled from the splice backend.
That is the implementation ticket this probe green-lights.

## 2. Encoder throughput on this box (Ryzen 5800X, 8C/16T, 48GB)

**torch 2.13.0+cpu — CUDA not available: the RTX GPU is idle; the entire
encoder stack runs on CPU.** (See "cheapest lever" below.)

| encoder | single | 2 threads | 4 threads | batched |
|---|---|---|---|---|
| BGE-M3 (bottleneck) | 8.2/s | 10.5/s | 10.5/s | **18.6/s** (bs=16) |
| SPLADE | 18.8/s | 21.2/s | 21.0/s | — |
| SEMA/MiniLM | 139/s | 153/s | — | — |

Threading saturates at 2 — classic GIL/BLAS behavior; batching is the
real lever (2.3× for BGE). A daemon interleaving one bundle
(BGE+SPLADE+SEMA) per query on these cores sustains **~9.3 bundles/s**
(1/18.6 + 1/21.2 + 1/153 ≈ 0.108s), or up to 18.6/s if BGE alone binds.

**Worker ceilings** (`supported ≈ bundle_rate × per-query latency`):

| bed class | pipeline latency | workers before encoder saturation |
|---|---|---|
| dogfood (0.73s) | 0.73s | **~7 (interleaved) to ~13 (batched, BGE-bound)** |
| erb_100k (5.1s) | 5.1s | ~47-95 |
| erb_829k (26.1s) | 26.1s | ~240-485 |

Receipt: `benchmarks/dogfood/erb/receipts/encoder_throughput.txt`.

**Fork 1 go/no-go vs the review's 3.5 qps bar: GO with 2.6× margin**
(~9.3 bundles/s ≫ 3.5). Corollary: today's in-process 1.7 qps plateau is
far below raw encoder capacity — the wall is single-process contention
*around* the encoders (GIL glue, tokenization, shared caches), which is
precisely what the process split removes. And the RAM-comfortable
worker count (N=4 without sharing; 8-12 with the mmap sidecar) sits
well inside the encoder ceiling at every bed class.

**Cheapest lever on the board:** the torch wheel is `+cpu`. Installing
the CUDA build and letting `[hardware]` auto-detect would put BGE-M3 on
the idle RTX GPU — typically 5-20× encode throughput, no architecture
change. Worth an A/B before any daemon work.

## 3. Fork 1 design review (subagent, full report in PR discussion)

Verified seams (minor drift): `open_read_source` sharding.py:563; write
no-op checklist :518-555; `get_shared_codec` bgem3_codec.py:261;
`BenchServer(repo_root=...)` unchanged. Stale claims corrected: raw
`.genome.conn` = **40 uses / 6 production files** (registry.py exactly
30), not 52/10; `PRAGMA data_version` invalidation **does not exist —
must be built**. "Resurrect GenomeWriter" understates the writer daemon
~10×: it covers only genes/promoter/FTS with stale positional-insert
shapes; 22 locked write methods plus registry/CWoLa/delivery/splade
surfaces are uncovered. Top risks: registry raw-conn writes in request
routes; session-delivery read-your-writes across workers (sync-ack
writes + session affinity); learning writes silently vanishing on ro
workers (async learning-event envelope to the writer); cross-process
cache invalidation (data_version poll + mmap fp16 dense sidecar — also
collapses N matrix copies to one physical 1.7GB, making N=8-12 RAM-easy);
encoder daemon relocating the GIL wall (mitigated: batch bundles, 2+
replicas, warmup gating). **Scope: ~24-41 person-days**, writer daemon +
worker wiring on the critical path.

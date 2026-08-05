# Recall at scale — diagnosis: near-cutoff losses in bloated pools; #327 discipline is null under RRF

**Date:** 2026-08-04 · **Branch:** `perf/slice1-instrumentation` · Companion to
`2026-08-04-erb-postfix-ladder.md` (which measured the erosion: recall@12
0.60 → 0.533 → 0.40 → 0.40 across 100k/250k/500k/829k on a fixed
141-needle set).

## Diagnosis (receipt mining + config-faithful in-proc probes)

Per-needle trajectories from the ladder worker receipts (n=20 at c=1),
then in-proc `query_docs_ann` replays on the 100k carve and 829k probe
with gold located in the scored pool:

1. **Gold is NEVER missing from the scored pool** — at any scale, on any
   probed needle. Pool depth is not the problem.
2. **Scale losses are near-cutoff rank slips, cliff-like in delivery**:
   the four needles lost between 100k and 829k hold delivered ranks 1-9
   at 100k and sit around pool positions ~3-70 at 829k — just outside
   the 12 delivered. (Caveat: pool positions read from the additive
   accumulator; delivery order is RRF's — class boundaries are robust,
   exact positions approximate.)
3. **A static-miss class exists at every scale** (8/20 needles): gold at
   pool position 600-3300 even at 100k, degrading to 5k-55k at 829k.
   One gold scores on `tag_prefix` alone. This is needle-vs-content
   semantic mismatch, not corpus dilution.
4. **The pools are bloated**: 9k-63k scored genes at 100k; **93k-294k at
   829k — up to 35% of the corpus scores on a single query.** Everything
   downstream (fusion sort, freshness gate, know calibration) pays for
   it; it is also plausibly the bulk of the 829k express residual (~8s).
5. Gold dense vectors are healthy (coverage 100% on gold; spot-checked
   cosine 0.70 for a delivered gold).

## A/B: #327 tag discipline at 829k — NULL

Arm: `tag_idf_enabled=true, tag_df_cap=0.10, splade_df_cap_fraction=0.10`
vs the post-fix ladder 829k baseline (same 20 needles, same bed):

| | baseline | #327 arm |
|---|---|---|
| recall@12 | 0.40 | **0.40** |
| median | 26.1s | 25.8s |
| needles flipped | — | **0** |

Consistent with the branch's earlier dogfood finding (6c33d86: IDF
scaling null; DF-cap at the Arm-D ceiling): under RRF's rank-based
fusion, tag-flood magnitude discipline has no remaining effect. Receipt:
`benchmarks/dogfood/erb/receipts/erb_829k_327arm.json`.

## Ranked next levers (not yet run)

1. **Rerank over the fused top-K** — the rerank stage runs in ~6ms at
   829k (PLR/DeBERTa effectively inactive). Near-cutoff gold at
   positions 13-70 is exactly what a real reranker over top-50/100
   recovers. First experiment: `[plr] enabled` (if the trained model
   generalizes off-dogfood) else DeBERTa rerank on top-50; budget
   +100-300ms/query.
2. **Dense-leg depth scaling** — `pool_size=500` fixed = top 0.06% at
   829k vs 0.5% at 100k. Scale k with corpus (e.g. `max(500, N//200)`);
   cheap to A/B; the fp16 matmul at 829k costs ~2s so deeper argpartition
   is nearly free.
3. **Pool hygiene as a latency+precision play** — per-tier candidate
   admission caps (don't let tag/fts tiers admit 100k+ genes into
   scoring); shrinks fusion cost and the noise floor together. Likely
   where the 829k express residual lives.
4. **Static-miss class** — separate track: these need better semantic
   matching (chunk-level dense, query expansion), not ranking fixes.

## Probe-fidelity lesson (cost: one wasted diagnosis pass)

Raw `KnowledgeStore(path)` construction silently disables retrieval legs
the server enables from config (`dense_embedding_enabled` ctor default is
False vs config default True; same for `splade_enabled`). The first
diagnosis pass concluded "failing gold is lexical-only" — an artifact.
In-proc probes must pass config-matching knobs explicitly; a
`KnowledgeStore.from_config()` helper would remove the trap.

## W3.2 closure (same session)

Executor c-curve at workers 2/4/8 on dogfood (receipts
`benchmarks/dogfood/receipts_w32/`): c=1 identical, qps plateaus ~1.7
at every size, larger pools worsen p95/canary. Pre-fix G1 blamed the
2-thread pool (qps 1.46 ceiling); post-fix the same pool reaches 1.76 —
**the wall moved to the shared encoder/GIL layer.** Default stays 2; the
next concurrency lever is Fork 1's process split (encoder daemon + N
read workers).

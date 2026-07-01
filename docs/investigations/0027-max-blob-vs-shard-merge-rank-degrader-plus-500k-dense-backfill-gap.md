---
thread: embedding-upgrade
turn: 27
author: max
ts: 2026-06-15T04:23:25Z
title: New blob-vs-sharded A/B isolates the cross-shard MERGE as a 2-4x rank-degrader (independent corroboration of your L1c) -- dense fires in BOTH arms, gold usually IN-pool but ranked far worse; correction: sharded dense is NOT adapter-disabled (it runs per-shard, router republishes tier_contrib['dense']); corpus-side membership observable for your never-surfaced 84 -- the 500K ERB corpus is MAJORITY un-dense-backfilled (slack shard 103936/390966 = 27% dense, slack ~78% of corpus); new verified sharded->blob merge tool offered as an Exp-E substrate; ERB blob-vs-shard A/B building now
---

Joe -- caught 0026. The Exp-D reversal lands cleanly: threshold-down @200_lt +0, the dense/fusion lever moves everything that moves, L1c promoted to binding. We ran an orthogonal experiment from the sharding side that converges on the same conclusion and adds a corpus-side membership datum you can act on now. Same scope discipline as yours: v0.6.3 frozen build, ribosome/reranker OFF, and I flag what I cannot certify.

## 1. New A/B -- blob vs sharded, dense live in BOTH arms: the cross-shard MERGE is a 2-4x rank-degrader

Setup (a controlled membership-vs-ranking isolation): same corpus served two ways -- (a) BLOB = one unified genome, (b) SHARDED = routing-DB + per-shard genomes via ShardRouter. Identical genes, identical dense vectors (the blob is built FROM the shards, vectors copied byte-identical -- see Sec 4). Dense ON, cap lifted (ann_threshold_max_genes=500, ann_similarity_threshold=0), CPU BGE-M3, additive fusion. Two code corpora (auto-generated genome-derived needles, path-token-scrubbed, one gold/query):

| corpus | arm | R@1 | R@5 | R@10 | MRR | within R@10 | cross R@10 |
|---|---|---|---|---|---|---|---|
| medium (6 proj, ~17.5K) | blob | 0.253 | 0.373 | 0.433 | 0.307 | 0.458 | 0.333 |
| medium | sharded | 0.080 | 0.280 | 0.347 | 0.154 | 0.350 | 0.333 |
| xl (12 proj, ~45.5K) | blob | 0.263 | 0.375 | 0.413 | 0.310 | 0.406 | 0.438 |
| xl | sharded | 0.063 | 0.125 | 0.150 | 0.090 | 0.125 | 0.250 |

Read: the blob OUT-RANKS the sharded genome 2-4x at R@1/MRR, while R@10 is much closer on medium (gold usually IS in the pool, just ranked far deeper) and collapses on xl. Crucially the hit is NOT concentrated in cross-project queries -- within is hurt as much or more -- so this is **not** a routing-coverage story. It's a **merge/fusion ranking** story: the cross-shard merge (per-shard BM25 IDF renormalization #118 + the #121 doc-type boost + a max_genes*2 merge-cut) re-orders materially worse than a single unified scorer over the same docs.

This is an independent, sharding-layer corroboration of your L1c "the in-pool OR-scorer/fusion is the binding lever." Your CANARY/V2a moved golds via the within-pool dense weight; our blob-vs-shard moves them via the merge stage. Both say: golds are admitted, then the fuser/merger buries them. On a sharded serve there are now TWO buriers stacked -- the within-shard additive fuser AND the cross-shard merge.

Dense-fired diagnostic (tier_contrib['dense'] present per /fingerprint, your own observable): blob 100% (medium) / 93.8% (xl); sharded 93.8% (medium) / 81.2% (xl). The sharded drop is concentrated on cross-project queries (medium 83%, xl 67% dense-fired vs blob 100%) -- a real but secondary routing-gate coverage loss on top of the dominant merge-ranking penalty.

## 2. A corpus-side membership observable for your never-surfaced 84: the 500K corpus is MAJORITY un-dense-backfilled

Building the ERB blob (Sec 4) surfaced this: in the fresh 500K sharded genome, the dominant **slack shard has 390,966 genes but only 103,936 dense vectors (27% backfilled)**. Slack is ~78% of the corpus, so the **majority of the 500K corpus has embedding_dense_v2 = NULL** -- dense literally cannot recruit those docs at any threshold or weight.

This is a direct, computable membership observable that bears on your (b)-vs-(c) and your 84 never-surfaced: **a gold whose doc has a NULL dense vector can never be a dense recruit, independent of threshold/scorer.** Suggested check you can run now off the genome (no re-embed, no serve): for each of the 84 never-surfaced golds, is embedding_dense_v2 NULL? That partitions the 84 into "un-backfilled -> coverage/backfill problem (a pipeline gap, your L1a's cousin)" vs "backfilled-but-buried -> your L1c scorer problem." It also reinforces #213: slack-root is both under-sharded AND under-backfilled.

## 3. Correction we owe the thread: sharded dense is NOT adapter-disabled -- it runs per-shard

A static read of ShardedGenomeAdapter looks like dense is OFF on sharded genomes (adapter pins _dense_embedding_enabled=False; query_docs_ann returns []; context_manager's dense branch therefore goes lexical). I initially carried that as "sharded serves have no semantic tier." **Live test refutes it:** dense fires on sharded serves (tier_contrib['dense'] non-zero, Sec 1). The adapter only disables the ADAPTER-level federated ANN; each routed shard's own Genome.query_docs runs its dense retrieval and the router republishes the dense contributions (shard_router republishes tier_contrib; knowledge_store writes tier_contrib['dense']=cosine). So on a sharded build dense IS live wherever vectors exist -- relevant to anyone reasoning about membership/wiring on the sharded path.

## 4. New capability: a verified sharded->blob merge (offered as an Exp-E substrate)

We built and verified a sharded->blob converter (you've done blob->sharded; this is the reverse): recursively ATTACHes every per-shard genome, copies all content tables preserving embedding_dense_v2 as a raw BLOB (byte-identical, no re-embed), de-dupes gene_id, rebuilds FTS5, and asserts blob_gene_count == sum(shards) and dense-count preserved. Verified on xl: 45,532 genes merged, dense byte-identical, FTS rebuilt. (One gotcha worth your note: live shards carry -wal/-shm sidecars and a plain mode=ro open throws "disk I/O error"; reads use immutable=1.)

Why it matters for your asks: it gives a clean "unified scorer vs cross-shard merge" A/B on ANY genome with zero re-embed, and it's a candidate Exp-E substrate -- a build where you can hold membership fixed and vary only the merge/fusion stage. Happy to hand it over.

## 5. Scope / what's pending (disclose-and-carry)

- CODE corpora (medium/xl), auto-generated needles (genome-derived, path-scrubbed, single gold/query) -- NOT the 125-semantic prose set; treat as a fusion/merge-mechanism probe, not a semantic-recall number.
- v0.6.3 frozen build, ribosome/reranker OFF (same condition as your Exp-D), CPU dense, cap lifted. xl recall on an 80-needle subset (the 12-shard fan-out is ~4s/query on CPU).
- The **ERB 500K blob-vs-shard A/B is building now** (sharded->blob merge of all 101 shards). I'll report prose-corpus numbers next -- but flagging up front that BOTH ERB arms inherit the 43%-dense-backfill ceiling from Sec 2, so it measures the sharding/merge delta, not a clean semantic recall.

## 6. Net / asks back

1. The merge stage is a second, sharding-specific instance of your L1c burier. If the Layer-2 prose serve is sharded (the 500K is), the cross-shard merge is in the live path between admission and surfacing -- worth instrumenting alongside the within-pool fuser.
2. The 84-gold NULL-dense-vector check (Sec 2) is a membership observable you can compute today, no instrumentation build needed -- it directly splits your (b)/(c) for the backfill-limited fraction.
3. Offer: the sharded->blob tool + a blob-arm of the 500K so you can A/B unified-vs-merged on the actual prose corpus once it finishes.

-- max

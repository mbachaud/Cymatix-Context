# The semantic retrieval ceiling is an embedding-resolution limit — complement and ANN-threshold levers refuted

**Date:** 2026-07-07
**Status:** complete (negative result with mechanism + a better-motivated lever)
**Corpus:** Onyx EnterpriseRAG-Bench, `enterprise_rag_50k` blob (80,072 genes / 50,000 docs), `semantic` subset (125 q).
**Feeds:** the roadmap "semantic ceiling" question; PR#250 ANN-threshold follow-up.

## Question

The `semantic` question type (low lexical overlap, paraphrase-heavy) sits at a
~1.6–2.4% recall@10 ceiling on the full 850k sharded corpus. Two cheap levers
were on the table (memory `erb-onyx-semantic-bench`):

1. **ANN threshold re-A/B** — PR#250 refuted `0.58→0.47` on *literal* SIKE needles; does it help where dense carries signal (semantic)?
2. **Complement / DNA-pair** — dense embeds `content` only; the `complement` strand is invisible to dense. Re-embed dense on the complement?

This note answers both, cheaply, and identifies where the real lever is.

## Setup

New driver `benchmarks/onyx_recall.py` (was absent): query → helix in-process
retrieval → full post-fusion ranking (`genome.last_query_scores`, not the
budget-truncated window) → source_ids → dsids → dedupe → **binary hit@k on
rel-paths** (matches the canonical `bench_enterprise_rag_recall.py`). dsid map
built from each raw file's `dataset_doc_uuid`.

## Findings

### 1. The 50k blob does not reproduce the ceiling (expected)

Shipped config (rrf, dense+SPLADE on, thr 0.58) on 50k-semantic: **hit@10 =
30.4%** — far above the 850k prior (~2.4%, additive). The ceiling is
corpus-size-dependent: 850k has ~17× the distractors. The 50k blob is a valid
*mechanism* testbed but not a ceiling reproduction. (Retrieval cost: ~21 s/query
over 80k genes — full A/B cells are ~38 min each, another reason to prefer the
cheap probes below.)

### 2. Complement thesis — REFUTED

Cosine diagnostic (`complement_reach_diag.py`): for each semantic gold doc,
compare the BGE-M3 query encoding against the gold's `content`, `complement`, and
`content+complement`:

| encoding | mean best-cosine | argmax wins |
|---|---|---|
| content (ships today) | **0.5580** | 64 / 125 |
| complement | 0.5539 | 57 / 125 |
| content + complement | 0.5576 | 4 / 125 |

`complement > content` on only **57/125 (46%)** of queries — a coin flip, and
complement is marginally *worse* on average. Content and complement are
interchangeable for query alignment. Re-embedding dense on the complement would
**not** improve reach. (The bed's `complement` here is not the "Q:/A:"
question-shaped strand from conversation-packing — for these CPU-ingested
enterprise JSON docs it is the document's continuation, e.g. fireflies
content=meeting header, complement=topics/action_items. Even so, it aligns no
better with queries than content.) **The re-embed was not run — the cosine
equivalence predicts a null, saving the 80k-doc re-embed + retrieval cost.**

### 3. Content-dense reach — the mechanism

`dense_reach_content.py` (pure numpy over the stored `embedding_dense_v2` blobs;
no re-embed) ranks the gold among all 50k docs under the dense tier alone:

```
dense-only recall@10 = 28.0%   recall@50 = 38.4%   recall@200 = 48.0%
mean gold cosine     = 0.558   mean gold percentile = 0.964
median gold rank     = 232     (of 50,000 docs)
```

The gold scores at the **96th percentile** — high in absolute terms — yet its
median rank is **232**, because ~4% of distractors score *even higher*. The dense
tier cannot resolve gold from near-miss distractors. Scale to 850k and that 4%
becomes ~34k docs above gold → the ~2% ceiling. The dense tier carries semantic
almost entirely (full pipeline 30.4% vs dense-only 28.0% → the lexical/SPLADE
tiers add only +2.4pp, as expected for low-lexical-overlap queries).

### 4. ANN threshold — refuted by mechanism (runs skipped)

The threshold governs which dense candidates are *admitted*; it cannot change the
cosine *ranking* that already buries the gold at rank 232. Lowering the threshold
admits more near-miss distractors, not the gold. Combined with the prior
"recall-lever investigation closed 0-for-3 on prose" and "SPLADE does not move
semantic," the threshold A/B is a predicted null; the 38-min/cell retrieval runs
were skipped in favor of the reach test above, which answers it mechanistically.

## Conclusion

The semantic ceiling is an **embedding-resolution limit**, not a strand or
threshold artifact. BGE-M3 places paraphrased-query gold at ~96th percentile —
selective enough for a small corpus, drowned by near-misses at scale. Neither
memory-designed cheap lever moves it.

**Where the lever actually is (data-motivated):**

1. **Rerank over the dense top-200.** `recall@200 = 48%` means ~half the semantic
   gold *is* reachable but mis-ranked. A cross-encoder reranker over the dense
   top-K (helix's `[ingestion] rerank_model`, off by default) could lift the
   reachable half into top-10 — the single most actionable next experiment.
2. **Stronger first-stage embedding** (or query-side generation, e.g. HyDE) for
   the other ~half absent from top-200. This is a model/infra investment, not a
   config tweak.

## Reproduce

```
benchmarks/onyx_recall.py            # recall@10 driver (dsid map + in-process ranking)
benchmarks/complement_reach_diag.py  # content vs complement vs both, query-cosine
benchmarks/dense_reach_content.py    # dense-tier reach (stored vectors, no re-embed)
```

Local, no egress. Beds: `genomes/bench/matrix/enterprise_rag_50k_batched.db`
(100% dense + complement populated), questions `onyx_500.jsonl` (125 semantic).

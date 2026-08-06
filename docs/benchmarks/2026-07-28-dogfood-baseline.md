# Dogfood bed baseline — cymatix ranks itself worst (2026-07-28)

First baseline on the rebuilt dogfood bed (`genomes/dogfood/genome.db`, four
owned repos). Retrieval only — no decoder, no consumer ladder, no judge.
Scored by gene_id set membership, so no path matching is involved.

Build + tooling: [`benchmarks/dogfood/README.md`](../../benchmarks/dogfood/README.md).
Raw data: `data/2026-07-28-dogfood-baseline-k12.json`, `…-k50.json`.

## Bed

| | genes | files |
| --- | ---: | ---: |
| cymatix-context | 5,654 | 824 |
| driftwatch | 518 | 214 |
| scorerift | 150 | 70 |
| MaxExpressKit | 105 | 69 |
| **total** | **6,427** | **1,177** |

Identity `eb2c7403176ce36c…`, gold `bfe2447612b321eb…` (full digests in
`data/2026-07-28-dogfood-gene-id-summary.json`). Contamination checks are zero
on `.venv`, `node_modules`, `aider_polyglot` and `helix-context`.

**cymatix-context is 88% of the bed by gene count.** Hold that number; it is
the whole finding.

## Headline

| metric | k=12 (shipped `max_genes_per_turn`) | k=50 |
| --- | ---: | ---: |
| recall@1 | 0.333 | 0.333 |
| recall@3 | 0.500 | 0.500 |
| recall@5 | 0.500 | 0.500 |
| recall@k | **0.667** | **1.000** |
| MRR | 0.434 | 0.446 |
| median rank when delivered | 1.5 | 4.5 |

**recall@50 = 1.000.** Every needle's gold is in the pool. Nothing is missing
from the bed, nothing is unretrievable, and no needle is mis-specified. The
entire deficit at the shipped budget is **ranking**.

## The split

Ranks, grouped by which repo the needle asks about:

| repo asked about | needles | ranks |
| --- | ---: | --- |
| scorerift | 3 | 1, 1, 7 |
| driftwatch | 4 | 1, 1, 2, 3 |
| MaxExpressKit | 3 | 1, 1, 2 |
| **cymatix-context** | **8** | **6, 6, 23, 23, 29, 30, 31, 41** |

Ten of the twelve non-cymatix needles land in the top 3. **Six of the eight
cymatix needles land past k=12** and would never be delivered under the shipped
budget.

Cymatix retrieves well about every repo in its store except the one it is
88% made of.

## Reading it

The obvious confound is corpus share, and it is almost certainly the mechanism
rather than something specific to cymatix as a subject. A query about
`fusion_mode` competes against 5,654 genes of cymatix's own documentation —
config reference, CLAUDE.md, README, roadmap, design memos, changelog — much of
which restates the same defaults in near-identical language. A query about
DriftWatch's default Ollama tag competes against 518 genes with far less
internal repetition.

Two supporting observations:

- The gold sets scale the same way. `cymatix_fusion_default` has **121 gold
  genes** (1.9% of the bed) and still lands at rank 30; `mek_marketplace_name`
  has **2** and lands at rank 1. More gold made it *harder*, not easier — the
  signature of a field of near-duplicates splitting the score mass rather than
  a retrieval miss.
- The two cymatix needles that do clear k=12 (`proxy_port`, `expression_tokens`,
  both rank 6) are the ones whose anchor is a rare literal token, where lexical
  scoring still discriminates inside the crowd.

**Scoring caveat, and it cuts against the result.** Every gene chunked out of a
gold *file* counts as gold here, not just the chunk containing the answer. That
is deliberately generous. The misses survive that generosity, which makes them
stronger, not weaker.

## Why this matters beyond the dogfood bed

This reproduces the **#260 "retrieved-but-unusable"** pattern on a corpus where
every confound is controlled: we own all four repos, gold is derived and
verified rather than declared, and scoring is set membership rather than path
matching. #260 measured gold delivered at rank 9 under a budget seating 5 on a
850K-gene external bed; this is the same shape at 6.4K genes on our own content.

It also lines up with the collaborator's cell-5 decomposition
(`spark-erb-receipts` 0011): gold present in the pool, absent from the
deliverable window, recoverable by **depth** rather than by better encoding.
There, plain FTS at depth 1000 recovered 45 of 62 missing golds while dense
recovered 0 at any fetchable depth.

Same prescription lands here: `[retrieval] fts5_candidate_depth` and the
`max_genes` budget are the levers this bed is asking about, not the encoders.

## What this does not establish

- **No claim that 0.667 is good or bad.** It is a first measurement on a new
  bed with no prior to compare against. Its value is as a fixed point for the
  depth sweep, not as a score.
- **No causal proof of the corpus-share mechanism.** The 88%/12% split and the
  gold-set-size inversion are consistent with it and nothing here contradicts
  it, but the controlled test is a balanced bed, which this is not.
- **Retrieval only.** Whether a consumer answers correctly given delivery is a
  separate measurement and deliberately not mixed in.

## Next

1. **`fts5_candidate_depth` sweep on this bed** (50 / 200 / 1000), replicating
   the cell-5 ladder. This bed is now the cheap local instrument for it — 18
   verified needles, ~900 ms/query, ~17 minutes per full sweep arm.
2. **Balanced-bed control.** Rebuild with cymatix-context down-sampled to
   roughly the other three, and re-measure. If the cymatix ranks collapse toward
   the others, corpus share is confirmed as the mechanism.
3. Feed both into #205 Layer 1 — per-corpus depth is exactly the auto-calibration
   this bed can now parameterise.

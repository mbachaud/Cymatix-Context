# Candidate-set cascade map — where the pool is widened, where it is cut, and whether PKI / cymatics shape it

Date: 2026-08-09 · Branch `claude/ab-data-cymatics-pki-dff9c6` · analysis-only (no
production code touched).

> **Port provenance (2026-09-01).** Authored 2026-08-09 on the campaign
> branch `claude/ab-data-cymatics-pki-dff9c6` as commit `daaa6af` (the
> branch head; never a PR) and ported to `beta` verbatim except for this
> block, the "where" column of the Sources table, and the ledger links —
> the addendum this document calls "Addendum 5" was renumbered
> **Addendum 7** when it was ported to master on 2026-08-19 (PR #388), and
> the links below point at that heading. The campaign-branch receipts this
> map is derived from (`wave3b_*`, `wave3d_ladder_blob_*`, `wave0a_*`,
> `delivered_gold_ab_erb10k*`, ~5.4 MB) are **not** in this repository:
> they are archived in the `cymatix-receipts` repository under
> `mirror-branches/claude__ab-data-cymatics-pki-dff9c6/benchmarks/dogfood/erb/receipts/`
> (exported from the branch head; see that repo's
> `mirror-branches/BRANCHES.md`). Companion plan:
> `docs/superpowers/plans/2026-08-09-ab-data-campaign.md` (ported alongside).
> Frozen SHAs cited in this document are campaign-branch commits.

**Question this answers.** The PKI and cymatics delete decisions were taken on
*recall* evidence
([ledger Addendum 7, authored as Addendum 5](2026-08-06-retrieval-layer-ledger.md#addendum-7-2026-08-09-ported-2026-08-19-pki--tags-armed-and-measured-at-two-scales-354-re-run-methodology-results)).
This document asks the different question: are either of them functioning as
**pool-shaping** (admission) devices rather than scoring devices, and would a
strong precise signal have more leverage in an explicit staged cascade than it
has in the shipped topology.

**Sources.** All numbers are re-derived here from committed receipts and from
the code at HEAD, not copied from the ledger:

| receipt | bed | needles | arms | frozen SHA | where (as of 2026-09-01) |
|---|---|---|---|---|---|
| `wave3b_ladder_100k_pass{A_forward,B_reversed}.json` | `erb_100k.db`, 6.45GB | 141 carve | 6 | `7294526` | `cymatix-receipts` → `mirror-branches/claude__ab-data-cymatics-pki-dff9c6/benchmarks/dogfood/erb/receipts/` |
| `wave3d_ladder_blob_pass{A_forward,B_reversed}.json` | `erb_blob_probe.db`, 56.7GB / 829k | 30 `blob_probe30` | 3 | `46ce3f6` | same mirror directory |
| `wave3d_cymatics_354_100k_carve.json` | `erb_100k.db` | 141 carve | 6 | `46ce3f6`+`e983618` | this repo, `benchmarks/dogfood/erb/receipts/` (ported by PR #388) |
| `storage_audit_{100k,blob}.json` | `erb_100k.db` / `erb_blob.db` | — | — | — | this repo, `benchmarks/dogfood/erb/receipts/` |

Both ladder receipts record `config` = **this worktree's `cymatix.toml`**, so
every config constant cited below is the one those runs executed under.

**Evidence grades used throughout.**
**[M] MEASURED** — read off a receipt or off code at HEAD.
**[D] DERIVED** — arithmetic on measured quantities, with the derivation shown.
**[I] INFERRED** — a reasoned step beyond the data; the gap is named.
**[U] UNTESTED** — no data exists; the run that would produce it is specified.

---

## 0. Three corrections the receipts forced on the brief

Recording these first because this campaign's rule is that the receipt wins.

1. **"Wide fan-out followed by a cliff" is right about the fan-out and wrong
   about where the cliff is.** The brief (and ledger item 3, and #344) describe
   a pool of 93k–294k scored genes at 829k. That number is real, but it
   describes the **cost object** (`gene_scores`, the accumulator every tier
   writes into), not the **ranking object**. A `bm25_shortlist` post-filter at
   `knowledge_store.py:3785` cuts `gene_scores` to the **BM25 top-50** *before*
   fusion runs, and RRF only ever ranks what survives it. The pipeline is
   already a two-stage cascade — it is just an undocumented one whose narrow
   stage is chosen by BM25 alone. **[M]**, §2.

2. **"PKI's bonus is diluted across ~900 candidates" is not how the shipped
   ranker works.** Under `fusion_mode = "rrf"` (shipped default) the 12.0 cap
   never reaches the ranking. RRF contributes `weight / (k + rank_t(d))`
   (`retrieval/fusion.py:174`) — a function of the document's rank *inside*
   PKI's own list and **independent of that list's length**. A document PKI
   ranks 1st gets `1.0/61` whether PKI returned 4 candidates or 5,357. **[M]**,
   §5.

3. **`lex_anchor` and `tag_exact` admit the identical document set.** Their
   per-query `tier_genes` counts are equal on **141/141** needles at 100k and
   **30/30** at 829k. Code confirms why: `lex_anchor` (`knowledge_store.py:3533`)
   selects `promoter_index WHERE tag_value = ?` — the same table and the same
   predicate as Tier 1 `tag_exact`. They differ only in score shape (IDF vs flat
   3.0) and RRF weight (1.5 vs 3.0). This strengthens the ledger's "the tags
   gate leaks through `lex_anchor`" (#355) from *the table is still read* to
   **the admission set is bit-identical**. **[M]**

---

## 1. Where the candidate set is WIDENED

### 1a. Per-tier fan-out, both scales

Baseline arm, pass A. `tier_genes[t]` = number of gene_ids for which tier `t`
wrote a `tier_contrib` entry (`ablation_ladder.py:1586`), captured at
`knowledge_store.py:3753` — i.e. **before** the BM25 shortlist. All tiers fire
on 141/141 and 30/30 needles. **[M]**

| tier | 100k median | mean | min–max | 829k median | mean | min–max | governing depth |
|---|---:|---:|---|---:|---:|---|---|
| `tag_prefix` | **17,317** | 25,374 | 261–81,350 | **129,372** | 139,845 | 45,370–292,407 | no LIMIT |
| `lex_anchor` | **12,437** | 18,070 | 100–62,694 | **82,798** | 96,455 | 23,746–221,881 | no LIMIT |
| `tag_exact` | **12,437** | 18,070 | 100–62,694 | **82,798** | 96,455 | 23,746–221,881 | no LIMIT |
| `authority` | 11,184 | 14,230 | 228–64,728 | 70,843 | 78,799 | 29,654–182,618 | derived — scores the pool, admits nothing |
| `fts5` | 2,000 | 2,000 | 1,996–2,000 | 2,000 | 2,000 | 2,000 | `_fts_fetch_depth = limit*2` |
| `splade` | 2,000 | 2,000 | 1,998–2,000 | 2,000 | 2,000 | 2,000 | `limit*2` (`:3214`) |
| `pki` | **900** | 1,209 | 4–5,357 | **2,939** | 3,358 | 590–7,343 | no LIMIT |
| `dense` | 500 | 500 | 500 | 500 | 500 | 500 | `min(dense_pool_size, limit*4)` |
| sum-of-tiers | 59,183 | 81,453 | 5,193–277,359 | 371,678 | 419,411 | 138,887–820,910 | — |

**The brief's prior-wave figures verify exactly** (tag_prefix 17.3k, lex/tag_exact
12.4k, authority 11.2k, fts5/splade 2000, pki 900, dense 500). **[M]**

The sum-of-tiers column double-counts documents found by several tiers; #344's
"93k–294k scored genes" is the *union* and is consistent with a 371k median sum
at 829k. **[I]**

### 1b. Unique admission — the number that matters

The receipts store per-tier **counts**, not per-tier **id sets**, so set
differences are not directly computable. But one tier gives an exact handle.

`_apply_authority_boosts` (`knowledge_store.py:2025-2167`) iterates
`gene_scores.keys()` and *"only raises the ceiling on already-scored documents,
never adds new candidates"* — verified at `:2055` and `:2152`. Its `tier_genes`
count is therefore a **function of pool membership**. And it is the *only* tier
whose count moves between the baseline and `no_pki` arms — `fts5`, `splade`,
`tag_exact`, `tag_prefix`, `lex_anchor` and `dense` are bit-identical. **[M]**

So `Δauthority = authority(baseline) − authority(no_pki)` counts exactly the
PKI-uniquely-admitted genes that earn an authority boost. And PKI's predicate
makes that "exactly the PKI-uniquely-admitted genes, full stop": PKI matches
`path_token IN (query tokens)` (`:2856`), where `path_token` is a split of
`source_id` (`:172-182`); authority's first leg credits `+2.0` when a query term
is a substring of `source_id` (`:2119-2129`). A PKI hit therefore *implies* an
authority boost. **[D]**

| | 100k (141 needles) | 829k (30 needles) |
|---|---:|---:|
| PKI candidates, median | 900 | 2,939 |
| **PKI unique admission, median** | **46** | **112** |
| mean / min / max | 88.7 / 0 / 995 | 192.0 / 9 / 822 |
| unique fraction of PKI's own output, median | **5.6%** | **3.4%** |
| queries where PKI's unique admission is 0 | 5 / 141 | 0 / 30 |

**PKI's admission contribution is real but small: ~94–96% of every PKI candidate
was already in the pool from another tier.** **[D]**

The same computation for `no_tags` gives Δauthority median 1,509 (100k) /
13,038 (829k), but that arm is **not** a clean subtraction — removing the tag
tiers arms the previously-inert `sema_boost` cycle on 68/141 queries (ledger
Addendum 7, authored as 5), which admits its own candidates. Do not read the tags number as
unique admission. **[M]**

**Not derivable from these receipts:** unique admission for `fts5`, `splade`,
`tag_prefix`, `dense` — no arm removes them, and no receipt carries id sets. §10
gives the probe.

---

## 2. Where the candidate set is NARROWED — the actual stage table

Shipped constants, all read from this worktree's `cymatix.toml` /
`cymatix_context/config.py`, on the **dense-on ANN path** (`_dense_embedding_enabled`
true and `query_text` present → `context_manager.py:3006`), which is the path
both ladders exercise (`ann_dense_leg` / `ann_body_fetch` appear in every
receipt's `signal_ms`).

The entry point is `query_docs_ann(max_genes=12)`, which calls `query_docs`
**with `max_genes = dense_pool_size = 500`** (`knowledge_store.py:4392-4395`).
That substitution is what sets every inner depth:
`limit = max_genes * 2 = 1000` (`:2712`), `_fts_fetch_depth = limit * 2 = 2000`
(`:2719`).

| # | stage | typical N in | typical N out | rule / constant | file:line |
|---|---|---:|---:|---|---|
| 0 | BM25 pre-filter | — | *inert* | `bm25_prefilter_enabled = false` | `:2723` |
| 1 | Tier 0 PKI | index | 900 / 2,939 | no LIMIT; `path_token ∧ kv_key ∈ query tokens`; bonus `10/max(card,2)`, capped 12.0, pairs with card > `PKI_NOISE_CUTOFF` skipped | `:2846-2934` |
| 2 | Tiers 1+2 tags | index | 12,437 + 17,317 / 82,798 + 129,372 | no LIMIT | `:3064`, `:3089` |
| 3 | Tier 3 FTS5 | index | 2,000 | `LIMIT _fts_fetch_depth`; `fts5_candidate_depth = 0` → auto → `limit*2` | `:3148` |
| 4 | Tier 3.5 SPLADE | index | 2,000 | `limit = limit*2` | `:3214` |
| 5 | dense leg (in-tier) | matrix | 500 | `k = min(dense_pool_size, limit*4) = min(500, 4000)` | `:3434` |
| 6 | `lex_anchor` | index | 12,437 / 82,798 | no LIMIT; identical set to `tag_exact` | `:3533` |
| 7 | authority | pool | pool | scores only, admits nothing | `:2025` |
| 8 | harmonic / SR / coact | pool | pool + ε | pull-forward over `gene_scores`; `coact_expand` median 0.1ms | `:3576`, `:3613` |
| **9** | **`bm25_shortlist` post-filter** | **≈59k / ≈372k (sum-of-tiers)** | **≤ 50** | **`bm25_shortlist_enabled = true`, `bm25_shortlist_size = 50`; `genes_fts MATCH (OR of query terms) ORDER BY rank LIMIT 50`; `gene_scores` and `tier_contrib` both filtered** | **`:3785-3814`** |
| 10 | RRF fusion | ≤ 50 eligible | ≤ 50 | `eligible_ids = gene_scores.keys()`; `score = Σ_t w_t/(60+rank_t)`; RRF gate inert (`rrf_gate_enabled = false`) | `:3839-3877`, `fusion.py:174` |
| 11 | fused truncation | ≤ 50 | ≤ `_fuse_limit` = 1000 | `combine_rerank(..., _fuse_limit)`; `_fuse_limit = limit` because rerank is off | `:3838`, `:3868` |
| 12 | cross-encoder rerank | — | *inert* | `rerank_enabled = false` (commented out in toml → default False) | `:3912` |
| 13 | ANN union | ≤ 50 lex + 500 dense | ≤ 550 | lex-only ids pinned at `threshold − 0.01` | `:4431-4444` |
| **14** | **ANN gate** | **≤ 550** | **8–12 (obs.)** | **`apply_ann_gate`: admit while `sim ≥ 0.58` or `len(kept) < min_genes=1`, **break** on first non-rescued below-threshold, cap at `max_genes = 12`; then append up to `dense_pool_floor_genes = 8` dense ids if fewer than 8 dense-scored survived** | `:405-490`, `:4478` |
| 15 | blend / cymatics | 8–12 | 8–12 | re-scores + **re-sorts**; `candidates[:max_genes]` is a no-op here (see §3) | `scoring/blend.py:211-250` |
| **16** | **budget tiers** | **8–12** | **2 / 3 / 6 / 8 / 12** | **hard floor: drop score < 0.15·top; then TIGHT (ratio ≥ 3.0) → 3, FOCUSED (1.8–3.0) → 6, BROAD → `max_genes`; under RRF the absolute floors are bypassed, ratio gates only** | `pipeline/tier_logic.py:91-176` |
| 17 | classifier assembly cap | ≤ 12 | ≤ 2/5/6/8 | `assembly_max_genes_cap` — arithmetic 2, factual 5, procedural 6, multi_hop 8; can only lower | `context_manager.py:2133`, `query_classifier.py:181-218` |
| 18 | splice + budget trim | = stage 17 | = stage 17 | `expression_tokens = 7000`, `splice_aggressiveness = 0.3` | — |
| 19 | delivered window | | **median 3** | — | — |

### Measured stage sizes at the tail (100k, 141 needles, baseline) **[M]**

`gate_kept` (stage 14 output): **12** on 84 needles, **9** on 11, **11** on 1,
**8** on 45. `floor_appended` > 0 on **17/141** (max 7). At 829k: 12 on 26, 8 on 4;
floor fired once.

`splice_n_candidates` == `delivered_count` on **every** needle:
**3** ×77, **8** ×28, **2** ×16, **12** ×7, **6** ×5, **4** ×3, **5** ×2, **9** ×1,
**None/0** ×2 (the empty-window class). Median delivered = **3**.

### The independent corroboration for stage 9

The BM25 shortlist is not visible in `tier_genes` (published at `:3753`, one line
*before* the filter). But it leaves a fingerprint the receipts do record:

> **The maximum gold rank observed anywhere is 61 at 100k and 58 at 829k — and it
> is bit-identical across all six arms, including `no_pki` and `no_tags`.** **[M]**

The full 100k tail is `[51, 52, 53, 53, 56, 56, 56, 56, 57, 59, 60, 61]`, the same
twelve values in `baseline`, `no_pki`, `no_tags`, `pki_w2`, `pki_w3`, `pki_w4`.
If the ranked pool were the ~59k-candidate accumulator, deleting the tag tiers
(which removes tens of thousands of candidates) could not leave the rank tail
byte-identical. A ceiling of ~50 BM25 survivors plus the handful of dense-only
candidates the blend appends at score 0 predicts exactly this. **[D]**

**Consequence.** Everything in stages 1–8 is *admission into a scoring
accumulator that is then thrown away except for its intersection with BM25's top
50*. The 17,317 tag_prefix candidates, the 900 PKI candidates and the 2,000
SPLADE candidates matter **only** for documents BM25 also ranks in its top 50.
The cost is paid at full fan-out; the ranking benefit is collected at 50.

---

## 3. Is cymatics a pool device? **NO — verdict holds, and the delete is free**

### Code

`scoring/blend.py:211-238`: the cymatics block iterates `for doc in candidates`,
computes `flux_score_dispatch(...) * 0.5`, multiplies (`scale_relative`, the
shipped mode) or adds the bonus into the score map, and calls
`candidates.sort(...)`. **It never appends to or removes from `candidates`.**
**[M]**

`blend.py:240-250`: `if len(candidates) > max_genes: candidates = candidates[:max_genes]`
— the only prune in the function, described in-code as *"a plain truncation"*.

**The ledger's claim is confirmed, with one refinement it should carry.** The
ledger says cymatics *"re-scores within a fixed-size pool (`candidates[:max_genes]`
runs identically), never prunes"*.

- *"never prunes"* — **confirmed [M]**.
- *"`candidates[:max_genes]` runs identically"* — **confirmed on this bed [D]**,
  because `gate_kept ≤ 12 = max_genes` on 141/141 and 30/30 needles, so the
  truncation is a no-op regardless of order. It is *not* structurally
  guaranteed: `apply_ann_gate` appends the #214 dense floor **after** the
  `max_genes` cap (`knowledge_store.py:457-459`), so a pool of up to
  `max_genes + 8 = 20` is reachable, and there the cymatics re-sort would decide
  who is dropped.
- **What the ledger phrasing under-states:** cymatics mutates `last_query_scores`
  and the candidate *order*, and both feed stage 16, whose 15%-of-top hard floor
  and top-3 TIGHT cut are score- and order-dependent. **Cymatics can change what
  is delivered without changing pool size.** **[M]** It is measured not to,
  materially — see below — but "cannot affect delivery" would be a wrong
  statement of the mechanism.

### Cost — the number that decides the economics

| | value | source |
|---|---|---|
| storage | **0 GB — no table, no index** | `storage_audit_{100k,blob}.json` list 14 layers; there is no cymatics layer. Spectra are computed from `promoter.domains/entities` behind `@lru_cache(maxsize=512)` (`scoring/cymatics.py:227`) |
| wall time, 141 needles | A (on) **807.2s** vs B (off) **830.3s** → cymatics **ON is 2.8% faster** | `wave3d_cymatics_354_100k_carve.json` |
| per-signal ms | **no entry** — the blend runs outside the store's `_sig` collector; not separable from wall noise | both ladder receipts |

Full arm set: A 807.2s, B(off) 830.3s, C(bow) 770.5s, D(seeds) 765.2 / 766.3 /
767.1s. The 65s spread across arms that differ only in bin assignment is the
noise floor; the on-vs-off contrast (−23.1s, wrong sign) sits inside it. **[M]**

### Verdict

**Cymatics is not a pool-shaping device and cannot be defended on pool-shaping
grounds.** It also cannot be attacked on cost grounds: deleting it frees **0 GB
and 0 measurable ms**, against a measured effect of r@12 and r@5 *identical*
across on / off / random-bin / collapsed-spread, and an MRR effect
(0.4957–0.5028) inside a 0.0035 random-bin control band. **The economics of this
delete are "you gain nothing and lose nothing"** — which is an argument for
leaving it alone and spending the review budget on the 8.56GB tier, not an
argument for either action. KEEP-CHEAP stands.

---

## 4. Is PKI a pool device? **YES for documents in general, NO for gold**

### It admits — that part of the hypothesis survives

`knowledge_store.py:2910`: `gene_scores[gid] = gene_scores.get(gid, 0) + capped`.
PKI writes into the accumulator with `.get(gid, 0)`, so it **creates** entries.
It is structurally an admission device, unlike cymatics. **[M]**

Measured magnitude (§1b): **median 46 uniquely-admitted genes/query at 100k,
112 at 829k**. Non-zero on 136/141 and 30/30 queries. **[D]**

### But the admission never carries gold

The decisive test. For every needle, compare gold's presence in the scored pool
between `baseline` and `no_pki`:

| bed | pass | needles with gold found (baseline) | gold **lost** when PKI removed | gold **gained** | delivered-gold lost | delivered-gold gained |
|---|---|---:|---:|---:|---:|---:|
| 100k | A forward | 116 / 141 | **0** | **0** | **0** | **0** |
| 100k | B reversed | 116 / 141 | **0** | **0** | **0** | **0** |
| 829k | A forward | 27 / 30 | **0** | **0** | **0** | **0** |
| 829k | B reversed | 27 / 30 | **0** | **0** | **0** | **0** |

**Of the 116 (100k) and 27 (829k) gold documents that were retrieved, the number
introduced to the pool ONLY by PKI is exactly ZERO — at both scales, in both arm
orders.** **[M]**

This is the answer to the brief's critical follow-up, and it is **consistent
with the `no_pki` arms** rather than merely compatible with them: the arms show
9 better / 9 worse / 123 same at 100k and 8/3/19 at 829k — *rank* movement with
*zero* membership movement, which is exactly what a tier that admits nothing new
and re-ranks weakly produces.

Two structural reasons this is not a fluke:
1. PKI's unique admissions are 5.6% / 3.4% of its output — it mostly re-finds
   what FTS5, SPLADE and the tag tiers already have. **[D]**
2. Even that 5.6% must clear stage 9 to reach the ranker. A PKI-unique document
   that BM25 does not rank in its top 50 is discarded before fusion. **[I]** —
   the receipts do not carry the intersection, but the filter is unconditional
   in code.

And the ledger's static-miss result closes the remaining escape: the 25/141
needles whose gold never scores anywhere are **bit-identical across all six
arms**, and PKI fires on all 25.

### Cost

| | 100k | 829k |
|---|---:|---:|
| storage | **1.213 GB / 20.26%** of the 5.99GB bed | **8.555 GB / 18.52%** of the 46.2GB bed |
| own-cost median, first-position pass | 31.5 ms | 148.3 ms |
| own-cost median, last-position pass | 33.5 ms | 128.5 ms |

(`path_key_index` + `idx_pki_lookup` + `idx_pki_gene`. Per ledger, `idx_pki_lookup`
is EQP-proven unused and 38% of rows are above `PKI_NOISE_CUTOFF` — #165 is a
separate, score-invariant saving.)

### Verdict

**PKI is a pool device that admits ~46–112 documents/query no other tier finds,
none of which has ever been a gold document in 171 measured needles across two
scales. The REMOVE verdict is safe on admission grounds as well as recall
grounds.**

---

## 5. The regime question — would a strong precise signal have more leverage in a tight cascade?

### 5a. The fusion math does not work the way the brief assumes

`retrieval/fusion.py:174`: `contribution = weight / (k + rank)`, `k = 60`,
summed over tiers. **[M]** The denominator contains the document's rank inside
that tier's list. It does **not** contain the list's length. So the premise
*"PKI's 12.0 bonus is diluted across ~900 candidates"* is false twice over:

- the 12.0 cap is an **additive-mode** quantity. Under `fusion_mode = "rrf"` the
  additive `gene_scores` is used for **eligibility** (`eligible_ids`, `:3846`)
  and for **PKI's internal ordering**, never for the ranking itself;
- there is no per-tier normalisation by candidate count anywhere in the Fuser.

Shrinking the pool from 900 to 250 therefore changes PKI's RRF contribution to a
given document **only** through where that document sits in PKI's own ranking —
and a pool cut that removes documents PKI scored *below* gold does not move gold
at all.

### 5b. What actually caps PKI's leverage

Max contribution at tier-rank 1, `w/(60+1)`:

| tier | weight | rank 1 | rank 60 | rank 900 |
|---|---:|---:|---:|---:|
| splade | 3.5 | 0.0574 | 0.0292 | 0.00365 |
| fts5 | 3.0 | 0.0492 | 0.0250 | 0.00313 |
| tag_exact | 3.0 | 0.0492 | 0.0250 | 0.00313 |
| tag_prefix | 1.5 | 0.0246 | 0.0125 | 0.00156 |
| lex_anchor | 1.5 | 0.0246 | 0.0125 | 0.00156 |
| dense | 1.0 | 0.0164 | 0.0083 | 0.00104 |
| **pki** | **1.0** | **0.0164** | 0.0083 | 0.00104 |

Sum of the seven firing tiers at rank 1 = 0.2377. **A *perfect* PKI — gold at
rank 1 of its own list — contributes 6.9% of the fused score of a document every
tier ranks first.** **[D]** PKI's weight is the joint-lowest in the store. In
the current fusion it cannot be decisive at any pool size; the ceiling is set by
`weight`, not by width.

`k = 60` is the real width-sensitivity knob, and it cuts the other way from the
brief's intuition: contributions are near-flat for ranks ≫ 60 (rank 900 is 1/16
of rank 1, not 1/900). Fusion is *already* insensitive to deep-rank noise. That
is why the tag tiers' 17k–129k fan-out does not wreck the ranking — and also why
narrowing the pool buys less rank leverage than one would expect.

### 5c. One code-level precision defect worth naming

`_pki_ranked` is built from `capped = min(bonus, 12.0)` (`:2909-2912`), and the
Fuser breaks ties by `gene_id` ascending (`fusion.py:156`). Every document whose
raw PKI bonus reaches 12.0 therefore lands in an **alphabetically ordered block**
at the head of PKI's own list. The cap is applied exactly where the signal is
strongest, converting PKI's most confident hits into an arbitrary order. Reaching
12.0 needs ≥3 near-unique pairs (per-pair max is `10/max(card,2) = 5.0`), so the
frequency is unknown. **[M]** for the mechanism, **[U]** for how often it bites —
probe in §10.

### 5d. What the receipts CAN and CANNOT settle

**CAN settle [M]:**
- PKI admits nothing that gold needs, at either scale (§4).
- The fused ranking is *not* computed over 900–17,000 candidates. It is computed
  over ≤50 (§2). The "wide fusion" the regime question assumes **does not exist
  in the shipped path**.
- Raising PKI's leg weight over that ≤50 population degrades recall monotonically
  (§6/Q8).

**CANNOT settle [U]:**
- Where gold sits in PKI's *own* ranked list versus the fused ranking (§Q7). No
  receipt carries per-tier ranks.
- Whether a **non-RRF** use of PKI — a hard sort key, or a tie-break confined to
  fused-score ties, over a pre-filtered pool — behaves differently from an
  upweighted RRF leg. Structurally different operator; never run.

**The honest bottom line for Q5.** The regime change the brief imagines has, to a
large degree, *already happened* — the ranker sees ~50 candidates, not 900 — and
PKI's ranking of gold in that regime is measurably no better than the fused
ranking's, since removing PKI entirely leaves recall flat-to-better at both
scales with zero membership change. **The data does not support a defence of
PKI, and I am not going to manufacture one.** The narrow residual claim that
survives is in §Q8.

---

## Q7 — per-tier precision at depth: NOT ANSWERABLE from these receipts

The per-needle record schema is exactly:
`needle, gold_ranks, rank_of_first_gold, recall_hit, recall_contribution,
wall_ms, signal_ms{12}, delivered_gold, delivered_count, delivered_gold_rank,
delivered_gold_full, delivered_gold_elided, delivered_gold_full_rank,
final_rank_of_first_gold, final_recall_hit, gate_kept, floor_appended,
splice_n_candidates, tier_genes{8}`.

`tier_genes` is a **count** (`ablation_ladder.py:1586-1589` increments a counter
per tier per gene). **There is no per-tier rank vector anywhere in the receipts,
and no way to reconstruct one.** Stating this plainly, as instructed, rather than
estimating.

### The probe that would produce it

The brief's read of the call sites is correct and the change is small.

1. **Store** (`knowledge_store.py`): every tier already materialises its
   `(gene_id, raw_score)` list immediately before `fuser.add_tier(...)` —
   `_pki_ranked` `:2934`, `_tag_exact_ranked` `:3106`, `_tag_prefix_ranked`
   `:3129`, `_fts5_ranked` `:3186`, `_splade_ranked` `:3257`, dense `:3450`,
   `_lex_anchor_ranked` `:3556`. Add one publication:
   `self.last_tier_ranks[tier] = {gid: rank}` built with the existing
   `retrieval.fusion.rank_by_score` helper (`fusion.py:228`) — which already
   exists for precisely this purpose ("Convenience for callers that want to
   inspect ranks directly (e.g. benchmarks, eval harnesses)") and uses the
   Fuser's tie-break, so the ranks line up bit-for-bit. Publish under
   `_last_query_scores_lock` beside `last_tier_contributions` (`:3751-3753`).
   Guard behind an env flag so the serving path pays nothing.
2. **Harness** (`ablation_ladder.py`, beside `tier_genes` at `:1586`): for each
   gold id, record `{tier: rank_or_None}` plus `len(tier_list)`. Also record
   `len(gene_scores)` before and after the BM25 shortlist — that single integer
   makes stage 9 measured instead of derived.
3. **Run**: the existing 141-needle 100k pass, baseline arm only. No new bed, no
   new needles, no arm matrix. Single pass, ~10 minutes at the observed 3.5s p50.

That produces, per tier: gold's rank within the tier, the tier's list length,
gold's fused rank, and — critically — **whether gold was in BM25's top 50 at
all**. It answers Q7, and it converts §2 stage 9 and §4's point (2) from derived
to measured. It is the single highest-value run in this document.

---

## Q8 — does the weight sweep settle the late-precision question?

The sweep (ledger Addendum 7, authored as 5): `pki_weight` 1.0 → 2.0 → 3.0 → 4.0 gives
regressions on **14 / 28 / 33** needles, monotonically, with r@12 falling below
baseline at w4. **[M]**

**What it tested, precisely.** It scaled PKI's RRF leg weight, i.e. `w/(60+rank)`
with `w` from 1.0 to 4.0, over the eligible set. At w4 PKI's rank-1 contribution
is 0.0656 — **larger than fts5's rank-1 0.0492 and second only to nothing**;
PKI becomes the strongest single leg in the store at the head of its list. **[D]**

**Why this is more informative than it looks.** The brief's framing distinguishes
"the wide stage where PKI's bonus lands on ~900 documents including many false
positives" from "a pre-filtered 50-candidate pool where the false-positive
population is largely gone". §2 shows **that distinction is much smaller than
assumed: the eligible population the sweep operated on was already ≤50**,
pre-filtered by BM25. The sweep was not run over 900 documents. It was run over
roughly the pool size the cascade proposal targets. **[D]**

So: **the weight sweep is meaningfully informative about giving PKI decisive
influence over a ~50-candidate pre-filtered pool, and the answer it gives is
"this makes retrieval worse, monotonically".** That is a real result against the
late-precision hypothesis, not merely a result against upweighting a wide leg.

**Three ways it still falls short of settling it — stated so the claim is not
stretched.**
1. **Operator mismatch.** An RRF leg at w4 *adds mass everywhere in its list*,
   including at rank 300. A tie-break confined to fused-score ties, or a
   two-key sort `(fused_bucket, pki_score)`, touches only documents the primary
   ranker cannot separate. Monotone degradation of the former does not
   arithmetically imply degradation of the latter. **[U]**
2. **Pre-filter mismatch.** The ≤50 the sweep saw is *BM25's* top 50. A cascade's
   250→50 would be selected by fusion over dense+sparse. Whether PKI's
   false-positive rate is similar on those two 50s is unknown. **[U]**
3. **Cap-induced ties.** §5c: upweighting a tier whose head block is ordered by
   `gene_id` amplifies an arbitrary ordering. Part of the w2/w3/w4 regression may
   be that artefact rather than PKI's signal being wrong. Not separable without
   the Q7 probe. **[U]**

**Verdict on Q8: the sweep is *substantially* informative and points against the
hypothesis — considerably more so than the brief allows, because the shipped
fusion is already narrow — but it does not close it, because it tested a
different operator.** The distinguishing run is in §10.

---

## Q9 — what the staged design would look like, from measured behaviour

Assigning each tier by the two things actually measured here: **how much it
widens the pool** and **what it costs**.

| tier | fan-out 100k / 829k | own-cost 100k / 829k (steady state) | storage (blob) | measured role | staged placement |
|---|---:|---:|---:|---|---|
| `fts5` | 2,000 / 2,000 | 353 / 2,054 ms | 11.39 GB | recall; **also is stage 9's gate** | **wide** — and it already silently decides the pool |
| `splade` | 2,000 / 2,000 | 239 / 3,528 ms | 12.26 GB | recall | **wide**, but the most expensive object in the store |
| `dense` | 500 / 500 | 17 / 118 ms | 3.11 GB | recall, corpus-scale-sensitive | **mid** — `dense_pool_size` is the knob (#344 B) |
| `tag_prefix` | 17,317 / 129,372 | 87 / 1,433 ms | (2.58 GB shared) | widest tier in the store; removing it is r@12-neutral-to-positive | **delete-candidate** — width without demonstrated benefit |
| `tag_exact` | 12,437 / 82,798 | 63 / 984 ms | (shared) | **identical admission set to `lex_anchor`** | **collapse** — one of the two is redundant (#356) |
| `lex_anchor` | 12,437 / 82,798 | 58 / 1,204 ms | (shared) | identical set, different weight | **collapse**; owns the real 2.58GB question (#355) |
| `pki` | 900 / 2,939 | 34 / 129 ms | **8.56 GB** | admits 46/112 unique, **0 of them gold** | **delete** — neither recall nor precision role survives |
| `authority` | 11,184 / 70,843 | (in-pool) | 0 | pure re-rank, admits nothing | late, already correct |
| `cymatics` | 0 | ~0 | **0 GB** | re-orders in place | late, free, indistinguishable from random binning |

**Pure recall (must run wide):** `fts5`, `splade`, `dense`.
**Pure precision (would belong late):** `authority`, `cymatics`, and `pki`
*if* its ranking were any good — §4 says it is not.
**Neither, therefore delete-candidates regardless of staging:** `pki` (0 gold
admissions, 8.56 GB), `tag_prefix` (largest fan-out, removal neutral-to-positive),
and one of `tag_exact`/`lex_anchor` (bit-identical admission sets).

**On the brief's "a precision tier moved late gets cheaper" point.** For PKI this
is **true for CPU and false for storage**. A late form would be
`SELECT path_token, kv_key FROM path_key_index WHERE gene_id IN (<=50 ids)` — a
bounded `idx_pki_gene` lookup, far cheaper than the current unbounded
`path_token ∈ … AND kv_key ∈ …` scan. But the *inverse-cardinality weight is the
signal*, and `pair_count` is computed from **the full result set of that
unbounded query** (`:2872-2879`). Restricting to 50 genes makes every observed
pair look rare and the IDF weighting collapses. Serving a narrow PKI therefore
needs either (a) the full materialised table anyway, or (b) a new, much smaller
`(path_token, kv_key) → cardinality` summary table — hundreds of MB rather than
8.56 GB, since it stores pairs and not the pair×gene cartesian. **So the 8.56 GB
argument survives re-positioning only if that summary table is built. [I] — the
cardinality-table size is an estimate from the schema, not measured.**

---

## Q6 — does the 1000 → 250 → 50 → 16 → 0 sketch match anything real?

### Against today's measured stages

| sketch | closest real stage | measured | match? |
|---|---|---|---|
| ~1000 | `limit = max_genes*2 = 1000` — the *nominal* inner cap | never binding; tiers deliver 59k–372k sum-of-tiers into the accumulator | **no** — the real first stage is 10–300× wider |
| 250 | — | nothing sits here | **no** |
| 50 | **`bm25_shortlist_size = 50`** | ≤50 eligible for fusion | **yes, exactly — and it already exists** |
| 16 | `max_genes_per_turn = 12` (+ ≤8 dense floor → ≤20) | `gate_kept` 8–12 | **yes, within one binding** |
| 0 | assembled window | median 3 delivered | **yes** |

**Three of the sketch's five levels already exist as shipped constants.** The
sketch's real content is the two that do not: an explicit, *bounded and
diversity-aware* first stage (instead of an unbounded accumulator), and a 250
stage between BM25's 50 and the tiers' tens of thousands.

The one place the sketch is materially wrong is the top: the shipped first stage
is not 1000, it is **unbounded**, and the 50 is chosen by **BM25 alone** — no
tier diversity, no dense representation (dense re-enters only later, at the ANN
union). That is a stronger criticism of the current topology than the sketch
makes.

### Against what is already designed / filed

**#344 "Adaptive candidate admission and delivery-loss ledger before final
context trim" (OPEN) is this proposal, already written.** Verified by fetching
the issue. It contains:

- the same three-way separation the sketch implies, stated as a design rule:
  *"Admission: which documents are allowed into expensive fusion/refinement.
  Ranking: how admitted candidates are ordered. Delivery: which ranked candidates
  survive budget tiers, classifier caps, compression, and assembly. Do not use a
  single adaptive top-K knob for all three."*
- **§A, the per-query loss ledger** — gold's state at 7 boundaries (present in
  each tier → admitted to the fused pool → rank before/after refiners → budget
  tier → classifier cap → spliced text → delivered). This is a **superset of the
  Q7 probe**; the Q7 probe is its first two boundaries and could ship as a slice
  of it.
- **§B, adaptive admission not unconditional widening** — per-tier caps, a global
  candidate budget, corpus-scaled dense depth, gap-triggered expansion, and *"a
  diversity floor so one noisy tier cannot occupy the full first pool"*.
- **§C**, stage-specific action split by loss class.
- the same 93k–294k pool figure, and the finding that on probed scale-loss
  needles *"gold was never absent from that pool"* — the same shape as §4's
  zero-gold-flip result.

**Ledger static→dynamic items 3 and 4** are the sizing half: item 3
"pool-size-adaptive admission caps — the bloated-pool fix (93k–294k scored genes/
query at 829k); top-M-union cut is safe except for the static-miss rescue class",
item 4 "`dense_pool_size` 0=auto corpus scaling (`max(500, N/200)`)" — which at
829k gives 4,145 rather than the shipped 500.

**Genuinely new in this document, not in #344 or the ledger:**
1. **The BM25 top-50 post-filter is the de-facto narrow stage, and it is not
   mentioned in either.** #344 reasons about a 93k–294k pool being what fusion
   ranks. It does not. Any admission policy designed against the accumulator
   will be measuring an object the ranker discards. **This should be added to
   #344 before its §B policies are compared.**
2. **`tag_exact` and `lex_anchor` are the same admission set** (141/141, 30/30) —
   changes the framing of #355/#356 from "attribute the tiers" to "one of them is
   redundant".
3. **The measured unique-admission method** (authority-delta, §1b) — a way to
   quantify any tier's unique admission from a single ablation arm, with no
   id-set plumbing.
4. **PKI's 12.0 cap creates gene_id-ordered ties at the head of its own tier
   ranking** (§5c).

### The standing warning from ledger claim 1 — restated with the new stage table

> *"do NOT size-gate dense off without first capping the lex-branch candidate
> list."*

The stage table sharpens why. If dense is skipped, `query_docs_ann` returns
`lex_pool[:max_genes]` (`knowledge_store.py:4421-4422`) — the ANN gate, the
dense floor, and the union all vanish, and the *only* thing standing between the
59k/372k accumulator and the delivered window is the BM25 top-50 plus the budget
tiers. The lex branch's candidate list must be capped **and given a diversity
floor** before any dense-degrade path is viable, exactly as claim 1 says and as
#344 §B's diversity-floor bullet anticipates.

---

## What would have to be true for the PKI delete to be WRONG

Falsifiers, ordered by how cheaply each can be checked. If none holds, the delete
is safe.

1. **A gold document exists that only PKI admits.** *Status: refuted at both
   scales.* 0 pool-entry flips and 0 delivery flips across 116 + 27 gold-found
   needles, both arm orders. **[M]** *Would have to be true:* the 141-needle
   carve set and the `blob_probe30` set both systematically exclude the query
   family PKI was built for — template/KV lookups like *"what is the value of
   cymatix_port?"* (`knowledge_store.py:2823`). **This is the strongest
   surviving objection**, because those needles were never in the bed. *Test:*
   a KV-lookup needle family, ~40 needles, `baseline` vs `no_pki`, 100k. **[U]**

2. **PKI ranks gold better than the fusion does, and a non-RRF operator could
   exploit that.** *Status: untested.* §Q8 shows an upweighted RRF leg makes
   things monotonically worse over an already-narrow pool, which is evidence
   against but not proof. *Test:* the Q7 probe, then a `pki_tiebreak` arm that
   sorts only within fused-score ties. **[U]**

3. **PKI's unique admissions matter to non-gold quality** — grounding,
   corroboration, answer completeness. *Status: outside every metric run.* r@12,
   MRR and delivered-gold are all gold-centric. *Test:* an answer-containment or
   faithfulness A/B; the runbook exists at
   `docs/benchmarks/2026-07-06-faithfulness-experiment-runbook.md`. **[U]**

4. **The BM25 shortlist is not actually active**, making the fusion genuinely
   wide and every regime argument above wrong. *Status: derived, not directly
   measured.* Chain: `cymatix.toml:370-371` (`true`, `50`) → `config.py:1508` →
   `context_manager.py:980` → `knowledge_store.py:3785` guard satisfied
   (`bm25_prefilter_enabled = false` at toml:374; `_fts_available` true since the
   FTS tier returns 1,996–2,000 rows on every needle), plus receipt `config`
   pointing at that file, plus the arm-invariant rank ceiling of 61. *Test:* one
   `len(gene_scores)` before/after print — the Q7 probe carries it. **[U]**
   (Low residual risk, but it is the load-bearing derivation in this document
   and should be nailed rather than trusted.)

5. **Removing PKI degrades something at a scale not measured.** *Status:
   measured at 100k (141) and 829k (30 of 469); one config profile; one needle
   family.* At blob scale one needle = 0.033 r@12.

**Not a falsifier:** cost. Deleting PKI frees 8.56 GB / 18.5% of the blob and
~129–148 ms/query at 829k, and the ledger's #165 case (unused `idx_pki_lookup`,
38% provably-inert rows) is independent of recall in both directions.

---

## 10. What I could not settle, and the exact runs that would

| open question | why unanswerable here | run that answers it |
|---|---|---|
| **Q7** per-tier rank of gold vs fused rank | receipts carry `tier_genes` **counts**, no rank vectors | the §Q7 probe: publish `last_tier_ranks` via the existing `fusion.rank_by_score`, record gold's per-tier rank + list length + `len(gene_scores)` pre/post-shortlist. Baseline arm, 141 needles, 100k, ~10 min. **Also converts stage 9 and falsifier 4 to measured.** |
| unique admission for `fts5`, `splade`, `tag_prefix`, `dense` | no ablation arm; the authority-delta trick needs one | same probe, emitting `tier_contrib` **key-set cardinality per gene** (`{n_tiers: count}`), or the id-set union directly |
| PKI as a **tie-break** rather than an RRF leg | never built; a different operator from `pki_weight` | a `pki_tiebreak` arm: sort key `(fused_score, pki_score)` applied only within equal fused scores, over the gate-kept 8–12. Cheap — no new index |
| how often PKI's 12.0 cap creates gene_id-ordered ties | not instrumented | histogram of raw (uncapped) PKI bonus; one counter at `:2909` |
| whether a KV/template needle family rescues PKI | that family is absent from both beds | ~40 KV-lookup needles on `erb_100k.db`, `baseline` vs `no_pki`. **This is the run the PKI delete is actually waiting on**, if anything is |
| cymatics' effect on *delivery* (not rank) | `wave3d_cymatics_354_100k_carve.json` carries r@k/MRR only; no `delivered_gold` | re-run the 354 arms through `ablation_ladder.py` so #335 fields ship. Low value — 0 GB, 0 ms at stake |
| whether a 250-candidate mid-stage helps | no such stage exists to ablate | #344 §B, and it should be designed against the **post-shortlist** pool, not the accumulator |

---

## Summary of verdicts

| question | verdict | grade |
|---|---|---|
| Is cymatics a pool device? | **No.** Never adds or removes members; the `[:max_genes]` cut is a no-op on both beds. It *does* reorder into the budget-tier cut, so "cannot affect delivery" would be wrong — but that effect is measured at/below a 0.0035 control band. Cost to delete: **0 GB, 0 ms**. KEEP-CHEAP. | [M]/[D] |
| Is PKI a pool device? | **Yes for documents — median 46 (100k) / 112 (829k) unique admissions/query — and No for gold: zero gold documents were admitted only by PKI, at either scale, in either arm order.** REMOVE stands, now on admission grounds too. | [M]/[D] |
| Would a strong precise signal have more leverage in a tight cascade? | **The tight cascade is already here** — fusion ranks ≤50 candidates, not 900. RRF contribution is rank-based and width-independent; PKI's weight-1.0 ceiling is 6.9% of a rank-1-everywhere document. The receipts do not support a defence of PKI. | [M]/[D] |
| Does the 1000→250→50→16→0 sketch match reality? | **50 / 16 / 0 already exist** (`bm25_shortlist_size`, `max_genes_per_turn`, the delivered window). 1000 and 250 do not — the top of the pipeline is unbounded, not 1000. The design is filed as **#344**, which is a superset of the sketch, except that #344 targets the 93k–294k accumulator rather than the BM25-50 the ranker actually consumes. | [M] |

---

*Analysis-only. No file under `cymatix_context/` was modified. Every constant
cited is at HEAD; every receipt figure is reproducible from the committed JSON.*

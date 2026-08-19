# 2026-08-14 — Encoder isolation scale curve (dense / SPLADE / PKI vs the lexical floor)

**Question:** with abundant all-on data showing small or null layer effects, what does each
encoder layer contribute *in isolation* — one layer enabled at a time over an all-off base —
and how does that change with scale?

**Design:** inverse of the `no_*` ablation ladder. Base config
`benchmarks/dogfood/erb/configs/enc_all_off.toml` (shipped `cymatix.toml` with
`dense_embedding_enabled`, `splade_enabled`, `pki_enabled` all `false`; sema/MiniLM and
everything else untouched; RRF fusion). Three `VALIDATION_SIGNAL_PRESENT` enable arms —
`dense_on`, `splade_on`, `pki_on` — each proven applied per run (signal key absent at the
all-off baseline, present in the arm; dense fires both `dense` and `ann_dense_leg`).
Order-reversed paired runs at every scale (baseline always first — inline validation needs
it; treatment order reversed between the pair). 30 needles, k=12,
`CYMATIX_DISABLE_LEARN=1`. Harness: `ablation_ladder.py` at `bench/encoder-isolation-ab`.

Beds: `f:/tmp/erb_carve/erb_{100k,250k,500k}.db` (first-N-gene carves of the blob — the
carve line shares one resolved needle set) and `f:/tmp/erb_blob.db` (829k, blob-resolved
needle set — **not needle-comparable with the carve line**, only arm-vs-floor within it).

## Results — every scale replicated exactly in both run orders

`r@12` = candidate-pool recall (score-map basis), `fr@12` = **final-order recall** — gold
within the top-12 of `genome.last_ranked_ids`, the ranked order retrieval hands downstream
(post-rerank when it ran). *[2026-08-19 correction (#375): earlier revisions labeled fr@12
"final (delivered) recall, the metric that matters" — that was wrong. fr@12 is NOT the
delivered basis; the post-budget-trim delivery slice is `delivered_gold_rate`, present in
every receipt below but originally unpublished. The delivered table and a basis note are in
the correction addendum at the end of this doc.]*

| scale | arm | r@12 | fr@12 | Δfr vs floor | med_rank | p50 |
|---|---|---|---|---|---|---|
| 100k | baseline (all-off) | 0.867 | 0.867 | — | 4.0 | ~10.0s |
| 100k | dense_on | 0.867 | **0.600** | **−0.267** | 4.0 | ~3.9s |
| 100k | splade_on | 0.867 | 0.867 | 0 | 2.5 | ~10.5s |
| 100k | pki_on | 0.867 | 0.867 | 0 | 3.5 | ~9.7s |
| 250k | baseline | 0.800 | 0.800 | — | 4.0 | ~12.0s |
| 250k | dense_on | 0.833 | **0.533** | **−0.267** | 3.5 | ~5.0s |
| 250k | splade_on | 0.833 | **0.833** | **+0.033** | 2.0 | ~14.9s |
| 250k | pki_on | 0.833 | 0.800 | 0 | 3.0 | ~12.7s |
| 500k | baseline | 0.733 | 0.767 | — | 3.0 | ~15.7s |
| 500k | dense_on | 0.700 | **0.433** | **−0.334** | 3.0 | ~12.0s |
| 500k | splade_on | 0.733 | 0.733 | −0.034 | 3.0 | ~17.6s |
| 500k | pki_on | 0.733 | 0.733 | −0.034 | 2.0 | ~15.7s |
| 829k | baseline | 0.767 | 0.767 | — | 2.0 | ~17.4s |
| 829k | dense_on | 0.800 | **0.567** | **−0.200** | 2.0 | ~15.9s |
| 829k | splade_on | 0.767 | 0.767 | 0 | 2.0 | ~20.9s |
| 829k | pki_on | 0.767 | 0.767 | 0 | 2.0 | ~18.6s |

Receipts: `benchmarks/dogfood/erb/receipts/enc_iso_{100k,250k,500k,829k}_run{1,2}_{fwd,rev}.json`.

## Findings

1. **Dense recall harms final ordering at every scale — the one large, universal effect.**
   Δfr@12 (final-order basis) vs floor: −0.267 / −0.267 / −0.334 / −0.200 (that is 6–10
   of 30 needles losing gold from the final top-12 order). Dense sometimes *adds* a
   candidate to the pool (+0.033 at 250k and 829k) but its scoring then displaces gold
   from the final top-12. The effect replicates in all eight runs and dwarfs the ±0.033
   one-needle noise band. At 500k it loses even pool recall (0.700 vs 0.733).
   *[2026-08-19 (#375): **HOLDS on the delivered basis**, and is larger at carve scales —
   Δ delivered_gold_rate vs floor: −0.333 / −0.333 / −0.333 / −0.200.]*

2. **The lexical floor is most of the product.** All-off scores 0.767–0.867 fr@12 across
   scales on these probes; no isolated layer beats it on final ordering anywhere except
   SPLADE's one needle at 250k. *[2026-08-19 (#375): **HOLDS on the delivered basis**
   (floor 0.700–0.833), but the exception changes: on delivered, SPLADE's 250k cell is
   *below* floor (−0.133), and the only arm ever above floor is pki_on at 100k (+0.033,
   one needle, inside the noise band).]*

3. **SPLADE is the only layer that ever earns anything, and only at mid-scale:**
   +0.033 fr@12 + best med_rank (2.0) at 250k; rank gains at 100k (2.5 vs 4.0);
   nothing at 500k/829k (metrics identical to floor).
   *[2026-08-19 (#375): **INVERTS on the delivered basis.** The +0.033 at 250k was
   final-order only — originally mislabeled "delivered". On `delivered_gold_rate`,
   250k is SPLADE's *worst* cell: 0.667 vs floor 0.800 (**−0.133**, 4 of 30 needles —
   outside the ±0.033 one-needle band), identical in both run orders. SPLADE ranks the
   gold it delivers higher (the med_rank gains are real, rank-basis) while pushing gold
   out of the delivered window on 4 needles. Full delivered curve vs floor:
   0 / −0.133 / −0.033 / 0 (n=30), −0.015 at 829k n=469. On delivered, SPLADE never
   earns anything at any scale, and mid-scale is where it loses most — there is no
   mid-scale niche. See the correction addendum.]*

4. **PKI is null at every scale** (rank-only wiggles at carve scales; at 829k
   byte-identical to floor on every reported metric). Consistent with the 2026-08-12
   `pki_ab` active-null receipts (`e9fd4e8`). *[2026-08-19 (#375): **HOLDS on the
   delivered basis, within noise** — delivered-identical to floor at 250k/500k/829k,
   +0.033 at 100k (one needle; the grid's only positive delivered cell, still inside
   the noise band). The 2026-08-17 exact-shipped-default confirm at n=469
   (`postflip_default_confirm_829k.json`, delivered basis) later measured PKI as
   −1 delivered needle — null-to-hair-negative, default flipped false in #370.]*

5. **At 829k every non-dense arm reports IDENTICAL metrics to the floor** — at the scale
   the product cares about, the isolated read-gates buy nothing on this probe set.
   *[2026-08-19 (#375): **HOLDS on the delivered basis** — splade_on, pki_on, and floor
   all 0.700 delivered at 829k/n=30.]*

6. **Dense is modestly faster at scale** (ANN branch short-circuits the full lexical
   scan: p50 15.9s vs 17.4s at 829k; 2.5× faster at 100k) — a trap: the fastest arm is
   also the worst-delivering one. *[2026-08-19 (#375): latency is basis-independent —
   **HOLDS**, and "worst-delivering" is now literally true on the delivered basis.]*

## Caveats

- **n=30 probes per scale.** One needle = 0.033. Dense's −0.200..−0.334 is 6–10 needles —
  robust. Every SPLADE/PKI delta *on the fr@12 basis* is 0–1 needle — inside the noise
  band. *[2026-08-19 (#375): NOT true on the delivered basis — SPLADE's 250k delivered
  delta is −0.133, 4 needles, outside the band. See the correction addendum.]*
- The 829k floor here (0.767) is NOT comparable to the published 55% gold delivery at
  829k — different needle sets (30-probe vs the 500-q official set). A 500-needle blob
  confirm is the natural hardening step. *[2026-08-19 (#375): the gap was also partly
  basis: 0.767 is fr@12. The floor's true delivered rate is 0.700 at n=30 and 0.563 at
  n=469 — the latter lands right on the published ~55%.]*
- All beds were BUILT with dense vectors + SPLADE expansions present; these are
  read-gate isolations. Ingest-side isolation exists for SPLADE
  (`erb_carve/erb_829k_nosplade.db` is already carved) but was not run here.
- `fr@12 > r@12` on the 500k baseline (0.767 vs 0.733) is a known harness property
  (final set is not a strict subset of the candidate top-k slice); it is consistent
  across arms and does not affect arm-vs-floor deltas.
- *[2026-08-19 (#375)]* The floor was measured with `pki_enabled = false`
  (`enc_all_off.toml:462`) — at measurement time (2026-08-14) the one knob off the
  then-shipped default. This resolved itself on 2026-08-17 when #370 flipped the PKI
  default `false` after an exact-shipped-default confirm at full power
  (`postflip_default_confirm_829k.json`, n=469, delivered basis: PKI on = 0.5608 vs
  0.5629 off) — the all-off base now *is* the shipped default.

## 2026-08-15 addendum — full-469 dense confirmation (hardening step 1: DONE)

The 30-probe result hardened on the full blob needle set (469 needles, two repeat runs,
identical output both runs — `enc_iso_829k_dense_confirm_run{1,2}.json`):

| arm | r@12 | fr@12 | med_rank | p50 |
|---|---|---|---|---|
| baseline (all-off floor) | 0.659 | 0.663 | 3.0 | ~22–26s |
| dense_on | 0.678 | **0.456** | 2.0 | ~19s |

- **Final-order recall −0.207** (0.663 → 0.456): ~97 of 469 needles lose gold from the
  final top-12 order when dense is enabled. At n=469 the one-needle band is ±0.002 —
  this is not noise of any kind. *[2026-08-19 (#375): originally mislabeled "delivered
  recall". The true delivered basis moves **−0.141** (`delivered_gold_rate` 0.563 →
  0.422; 264 → 198 of 469 needles) — smaller than the final-order delta but the same
  verdict at overwhelming power.]*
- The mechanism in full view: dense **adds** +0.019 pool recall (~9 needles found that
  lexical misses) and ranks the gold it keeps *higher* (med_rank 2.0 vs 3.0) — but its
  scores flood the final top-12 with plausible-but-wrong candidates on ~10× more
  needles than it rescues.
- The 30-probe estimate (−0.200) matched the full-set truth (−0.207) almost exactly;
  the probe floor (0.767) vs full floor (0.663) confirms the probe set skews easy.

## 2026-08-15 addendum 2 — ingest-side SPLADE A/B (nosplade bed, n=469)

Bed pair: `erb_blob.db` (147,115,451 `splade_terms` rows) vs
`erb_carve/erb_829k_nosplade.db` (zero splade tables; **identical** 829,131 genes and
identical dense backfill, 809,255 — single-variable pair, verified). Same 469 needles,
same all-off base config. Receipts: `enc_iso_829k_nosplade_run{1,2}.json` vs the
dense-confirm baselines. (The older all-on-frame receipts — `full469_nosplade_c1` 0.6759
vs `splade_ab_blob` 0.7667 — are NOT a usable A/B: n=469 vs n=30 needle mismatch, and
both predate the 2026-08-09 receipt-invalidation boundary.)

| bed | arm | r@12 | fr@12 | med_rank |
|---|---|---|---|---|
| blob (expansions present) | floor | 0.659 | 0.663 | 3.0 |
| nosplade | floor | 0.659 | 0.663 | 3.0 |
| nosplade | splade_on | 0.659 | 0.663 | 3.0 |

- **The floors are byte-identical across beds** (both repeats): removing the 147M-row
  expansion index changes nothing about non-splade retrieval — SPLADE storage is cleanly
  separated (no FTS leakage), and the expansions contribute nothing passively.
  *[2026-08-19 (#375): holds on the delivered basis too — `delivered_gold_rate` 0.5629
  (264/469) byte-identical across all three cells and both repeats.]*
- **`splade_on` over the empty index validated and did nothing**: boot created the empty
  `splade_terms` table, the query-side tier ran (signal key present — validation PASS),
  and every metric stayed identical. The SPLADE pathway is wholly ingest-dependent, and
  an empty index is a proven no-op rather than an error.
- Remaining open cell for the SPLADE default decision: **blob-bed `splade_on` at n=469**
  (query-side over the real expansions, full power). At n=30 it measured zero; the
  invalidated all-on data hinted +9pp. That cell decides `splade_enabled` and the #333
  storage reclaim (~8.5 GB/bed).

## 2026-08-16 addendum 3 — blob-bed `splade_on` at n=469: the SPLADE verdict

The last open cell (query-side SPLADE over the real 147M-row expansion index, full
needle set). Receipts: `enc_iso_829k_splade_confirm_run{1,2}.json` — both repeats
identical:

| arm | r@12 | fr@12 | med_rank | p50 |
|---|---|---|---|---|
| floor | 0.659 | 0.663 | 3.0 | ~24.3s |
| splade_on | 0.655 | **0.657** | 3.0 | ~27–29s |

**Null-to-negative**: −0.004 pool (~2 needles), −0.006 final-order (~3 needles), ~15%
slower, at full statistical power. *[2026-08-19 (#375): the −0.006 was originally
labeled "delivered" but is fr@12; the true delivered basis is **−0.0149**
(`delivered_gold_rate` 0.5629 → 0.5480, 264 → 257 of 469 needles) — same direction,
2.5× larger.]* The +9pp hint from the pre-invalidation all-on data
does not reproduce under current config. Combined with addendum 2 (the index contributes
nothing passively), the complete SPLADE account at 829k: ingest-side expansions cost
~8.5 GB/bed and query-side reading of them costs latency and a transformer encode for
zero-to-negative retrieval effect. (The floor has now replicated byte-identically
across 6 independent runs and two beds: 0.659 / 0.663 / 3.0 — and 0.5629 delivered.)

**Action taken**: `[ingestion] splade_enabled` default flipped `false` (2026-08-16),
joining the dense flip — the default retrieval path is now fully neural-free. The #333
storage-reclaim case (dropping `splade_terms` from existing beds) now has its receipt.
~~Remaining scale caveat: SPLADE's one delivered needle at 250k (addendum: the main table)
suggests a mid-scale niche; anyone chasing it should re-run the 250k pair ingest-side.~~
*[2026-08-19 (#375): **withdrawn** — the "one delivered needle at 250k" was the
final-order basis mislabeled. On the true delivered basis the 250k cell is SPLADE's
worst (−0.133 vs floor, 4 needles, both run orders); the receipts show a mid-scale
delivered *penalty*, not a niche. The default-off decision stands with a stronger,
not weaker, delivered-basis case; there is no receipt-backed reason to chase 250k.]*

## Implication

The receipt-backed case for flipping `dense_embedding_enabled` default-off is now a
four-scale monotone replicated result, not a single A/B. Recommended sequence before the
flip ships: (1) 500-needle blob confirm of the dense arm only (floor + dense_on, paired);
(2) decide whether SPLADE keeps its default on the strength of a one-needle mid-scale
win; (3) the flip PR cites this doc + the eight receipts. *[2026-08-19 (#375): item (2)'s
premise was a final-order artifact — on the delivered basis there never was a mid-scale
win to weigh (250k is −0.133). The decision itself was settled by addendum 3
(`splade_enabled` flipped false 2026-08-16) and only gets stronger on the corrected
basis.]*

## 2026-08-19 correction (#375) — the delivered basis, published

The tables above publish `final_recall_at_k` (fr@12). Earlier revisions of this doc
called that "final (delivered) recall, the metric that matters" — a mislabel. Every
receipt in this doc also carries the true post-budget-trim delivery metric,
`delivered_gold_rate`, which was never published here. This section publishes it, from
the receipts (doc-only erratum; receipts unchanged, both run orders identical in every
cell).

**Basis note — what each metric measures** (both defined in
`benchmarks/dogfood/erb/ablation_ladder.py`):

- `fr@12` / `final_recall_at_k` — gold within the top-k of `genome.last_ranked_ids`,
  the **final ranked order** retrieval hands downstream, post-rerank when it ran
  (`final_rank_of_first_gold` :559, `final_order_fields` :582, per-arm rollup :1228).
  It measures ordering, *before* the token-budget trim.
- `delivered_gold_rate` — gold within `window.expressed_gene_ids`, the **post-budget-trim
  delivery slice**: what the model actually received (`delivered_gold_fields` :545–556,
  per-needle read :1150, per-arm rollup :1216/:1234). This is the delivered basis.
- `pool_delivery_gap` = `recall_at_k` − `delivered_gold_rate` is already in every
  receipt; a nonzero gap is the tell that the two bases diverge.

**`delivered_gold_rate`, all cells, floor + every arm** (n=30 scales from
`enc_iso_{scale}_run{1_fwd,2_rev}.json`; n=469 rows from the confirm pairs; every value
identical across both runs of its pair):

| scale (n) | floor | dense_on | Δ | splade_on | Δ | pki_on | Δ |
|---|---|---|---|---|---|---|---|
| 100k (30) | 0.833 | 0.500 | **−0.333** | 0.833 | 0 | 0.867 | +0.033 |
| 250k (30) | 0.800 | 0.467 | **−0.333** | 0.667 | **−0.133** | 0.800 | 0 |
| 500k (30) | 0.733 | 0.400 | **−0.333** | 0.700 | −0.033 | 0.733 | 0 |
| 829k (30) | 0.700 | 0.500 | **−0.200** | 0.700 | 0 | 0.700 | 0 |
| 829k (469, confirms) | 0.563 | 0.422 | **−0.141** | 0.548 | −0.015 | — | — |

(829k n=469: floor and dense_on from `enc_iso_829k_dense_confirm_run{1,2}.json`,
splade_on from `enc_iso_829k_splade_confirm_run{1,2}.json` — same floor, byte-identical
0.5629 across all four receipts plus both nosplade runs.)

**What changes and what doesn't, per finding:**

| finding | on delivered basis |
|---|---|
| 1 (dense harms) | **holds, larger** — −0.333 at all three carve scales, −0.200/−0.141 at 829k |
| 2 (floor is most of the product) | **holds** — the stated exception (SPLADE 250k) inverts; only pki_on 100k (+0.033, 1 needle, noise) ever exceeds floor |
| 3 (SPLADE earns at mid-scale) | **INVERTS** — 250k is −0.133 (4/30 needles, outside the one-needle band, both run orders); curve 0 / −0.133 / −0.033 / 0, −0.015 at n=469 |
| 4 (PKI null) | **holds within noise** — 0 at 250k/500k/829k, +0.033 at 100k (1 needle) |
| 5 (829k non-dense arms = floor) | **holds** — all 0.700 |
| 6 (dense faster) | **holds** — latency is basis-independent |

What survives of finding 3: SPLADE's rank improvements (med_rank 2.0 at 250k, 2.5 at
100k) are real on the rank basis, and the fr@12 +0.033 at 250k is what the final-order
receipts genuinely show. But the delivered receipts show the opposite sign at the same
cell — SPLADE reorders the window so that 4 needles' gold falls below the budget trim.
"SPLADE is the only layer that ever earns anything" is false on the basis that reaches
the model; the default-off flip (addendum 3) is *strengthened* by this correction, and
the suggestion to re-run the 250k pair in search of a mid-scale niche is withdrawn.

Why the mislabel matters beyond this doc: fr@12 and `delivered_gold_rate` can disagree
in both magnitude and sign (dense at n=469: −0.207 vs −0.141; SPLADE at 250k: +0.033 vs
−0.133) because the budget trim is not rank-monotone across arms. Any future doc quoting
"delivered" numbers must quote `delivered_gold_rate` / `delivered_gold_queries`, not
`final_recall_at_k`.

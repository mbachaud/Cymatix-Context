# Dogfood investigation ladder — what moves rank, and what cannot (2026-07-28)

Follow-up to [the baseline](2026-07-28-dogfood-baseline.md), which established
that gold is **in the pool but ranked too deep**: recall@12 = 0.667, recall@50
= 1.000. This ladder asks which knob moves rank.

14 arms x 18 needles on `genomes/dogfood/genome.db` (6,427 genes, identity
`eb2c7403176ce36c…`). Retrieval only, gene_id set membership, ranks recorded
within the top-50 scored. Harness `benchmarks/dogfood/run_ladder.py`; raw data
in `data/2026-07-28-dogfood-ladder-*.json`.

Every arm reports the cymatix/other split separately, because an average over
this bed hides the entire effect.

## Results

| arm | recall@12 | cymatix@12 | other@12 | MRR | cymatix median rank |
| --- | ---: | ---: | ---: | ---: | ---: |
| **baseline** (rrf, additive combinator, dense_w 4.0) | **0.667** | **0.250** | 1.000 | **0.446** | 26.0 |
| fts_depth=50 | 0.667 | 0.250 | 1.000 | 0.446 | 26.0 |
| fts_depth=200 | 0.667 | 0.250 | 1.000 | 0.446 | 26.0 |
| fts_depth=1000 | 0.667 | 0.250 | 1.000 | 0.446 | 26.0 |
| dense_w=0.0 | 0.667 | 0.250 | 1.000 | 0.446 | 26.0 |
| dense_w=8.0 | 0.667 | 0.250 | 1.000 | 0.446 | 26.0 |
| dense_w=16.0 | 0.667 | 0.250 | 1.000 | 0.446 | 26.0 |
| rerank=fused_tier | 0.611 | 0.125 | 1.000 | 0.418 | 23.5 |
| rerank=eps_band | 0.611 | 0.125 | 1.000 | 0.418 | 23.5 |
| rerank=off | 0.611 | 0.125 | 1.000 | 0.418 | 23.5 |
| fusion=additive | 0.556 | 0.000 | 1.000 | 0.342 | 37 |
| additive + dense_w=0.0 | 0.556 | 0.000 | 1.000 | 0.335 | 43 |
| additive + dense_w=4.0 | 0.556 | 0.000 | 1.000 | 0.342 | 37 |
| additive + dense_w=16.0 | 0.556 | 0.000 | 1.000 | **0.403** | 39.0 |

**Nothing beat the shipped defaults.** Every arm is neutral or worse.

## Four findings

### 1. Candidate depth is a byte-identical null — and that is the useful part

`fts5_candidate_depth` at 50 / 200 / 1000 produces **identical ranks, recall and
MRR** to the auto default. Not "small effect" — identical.

This was the predicted outcome and it matters because it **separates this bed's
regime from the collaborator's cell-5 result**. On their 850K bed, gold was
never admitted to the pool and FTS depth 1000 recovered 45 of 62 missing golds.
Here gold is already admitted and already scored; widening admission cannot
reorder what is already in. Two different failure modes that both present as
"gold not delivered."

**So the cell-5 prescription does not transfer to this bed**, and we should stop
treating `fts5_candidate_depth` as the general answer to a delivery gap. It is
the answer to an *admission* gap. Diagnose which one you have first — the
recall@k curve does it: if recall@large-k is already 1.000, depth is not your
lever.

### 2. `dense_additive_weight` is inert under the shipped RRF default

Under `fusion_mode="rrf"`, sweeping the weight 0.0 → 8.0 → 16.0 changes
**nothing** — identical ranks. Under `fusion_mode="additive"` the same sweep
does move MRR (0.335 → 0.342 → 0.403) and median rank (43 → 37 → 39).

The knob is **fusion-conditional**, which its name in hindsight says plainly:
under RRF the dense leg contributes by *rank*, so an additive weight has nothing
to attach to.

This has a consequence beyond this bed. **#203's sweep — the one that set the
current 4.0 default and measured recall 0.58 → 0.64 on erb10k — was run under
additive**, before RRF became the default in 2026-07-06. Its conclusion does not
transfer to the shipped configuration, and the "raise to 6" deferred to #205
would be a no-op today. The knob is exposed in `config.py` and documented in
`docs/config-reference.md` with no indication that it is dead under the default
fusion mode.

Worth confirming on a second bed before acting, but if it holds, either the
knob should warn when set under RRF, or an rrf-native dense weight needs to
exist.

### 3. RRF is doing real work — it is the best arm measured

`fusion=additive` is **worse across the board**: recall@12 0.556 vs 0.667,
cymatix recall 0.000 vs 0.250, MRR 0.342 vs 0.446, cymatix median rank 37 vs 26.
Under additive, **not one** cymatix needle reaches the shipped budget.

This independently corroborates the 2026-07-06 RRF default flip (SIKE Run-2
measured +12pp gold_delivered on xl) on a completely different corpus, and it
means the legacy additive path scheduled for removal in v(N+2) is not quietly
better on code-heavy content.

### 4. The three non-default rerank combinators are identical to each other

`fused_tier`, `eps_band` and `off` produce **the same** recall, MRR and median
rank (0.611 / 0.418 / 23.5) — different from `additive` (the default, 0.667),
but indistinguishable from one another.

Two readings and this ladder cannot separate them: either all three collapse to
the same code path under RRF (plausible — they govern how raw tier bonuses
combine, and #255 established those bonuses are additive-scale artefacts that
RRF fusion largely neutralises), or the knob is only partly threaded. **Flagged,
not concluded.** It deserves a direct code trace before anyone reads a
preference into it.

Note the direction: the default `additive` combinator is the *best* of the four
here, which is mild counter-evidence to #255's concern that its raw bonuses
dominate RRF ordering — on this bed they help.

## Budget curve

Delivery as a function of `max_genes`, from the baseline ranks:

| k | all | cymatix | other |
| ---: | ---: | ---: | ---: |
| 1 | 0.333 | 0.000 | 0.600 |
| 3 | 0.500 | 0.000 | 0.900 |
| 5 | 0.500 | 0.000 | 0.900 |
| 8 | 0.667 | 0.250 | 1.000 |
| **12** (shipped) | **0.667** | **0.250** | **1.000** |
| 20 | 0.667 | 0.250 | 1.000 |
| 25 | 0.778 | 0.500 | 1.000 |
| 30 | 0.889 | 0.750 | 1.000 |
| 40 | 0.944 | 0.875 | 1.000 |
| 50 | 1.000 | 1.000 | 1.000 |

```
cymatix ranks: 6, 6, 23, 23, 29, 30, 31, 41
other   ranks: 1, 1, 1, 1, 1, 1, 2, 2, 3, 7
```

The non-cymatix needles saturate at **k=8** and are flat at 1.000 in *every arm
of the ladder* — completely insensitive to all four axes. There is no headroom
there and no knob touches it.

The cymatix needles are flat from k=8 to k=20 and only start recovering at
**k=25**, reaching 1.000 at k=50. Buying the deficit with budget alone costs
roughly **4x the shipped `max_genes_per_turn`** — every turn, for every query,
to fix eight queries. That is the honest price of the "just raise the budget"
option, and it is why it is not a recommendation.

## What this rules out, and what is left

**Ruled out as levers on this bed:** candidate depth (null), dense additive
weight (inert under the default), fusion mode (RRF already the best), rerank
combinator (default already the best of four).

**Not yet tested — and the remaining hypothesis.** Nothing in this ladder
touches *why* cymatix's own documentation crowds out its own gold. The
gold-set-size inversion from the baseline still stands: 121 gold genes lands at
rank 30, 2 gold genes lands at rank 1. That is the signature of near-duplicate
documents splitting score mass, and no reweighting of legs fixes it because
every leg sees the same near-duplicates.

That points at **deduplication / diversity** rather than weighting:

1. **Balanced-bed control first.** Rebuild with cymatix-context down-sampled to
   roughly the other three and re-measure. If the cymatix ranks collapse toward
   the others, corpus share is confirmed as the mechanism and the target is
   clear. This is the single highest-value next run and it is a ~40 min rebuild.
2. **Near-duplicate diagnosis.** For a failing needle, inspect what actually
   occupies ranks 1-12. If they are other chunks restating the same default,
   that is direct evidence.
3. **MMR-style diversity or near-duplicate collapse at assembly** — untested,
   and the only family of fix this ladder has not ruled out.

## Caveats

- **One bed, one corpus shape.** The dense-weight and rerank findings are
  properties of this measurement; they need a second bed before they drive a
  default change. The depth null is safer to generalise because it follows from
  recall@50 = 1.000, which is a structural fact about the bed rather than a
  tuning result.
- **Retrieval only.** No claim about answer correctness.
- **Generous gold.** Every chunk of a gold file counts as gold, so all these
  numbers are upper bounds and the misses survive that generosity.
- **n=18.** Small. Directions here are hypotheses to test, not settled results —
  which is exactly why the balanced-bed control comes next rather than a
  default flip.

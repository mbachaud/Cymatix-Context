# EnronQA paraphrase crater — decomposition, mechanism, and the discrimination kill (2026-08-30)

**Question.** The v0.9.1 gate sweep measured rephrased needles trailing originals on
delivered gold: −4.0pp on the 85k `enronqa_v2` bed and −8.8pp on the 597k
`enronqa_padded` bed (floor arm: −4.8pp / −8.0pp). Wave-2 left "EnronQA rephrased
split" as a live crater lane. What is the crater made of, and does any in-charter
(neural-free) fix have a target?

**Receipts.** `benchmarks/dogfood/receipts/paraphrase_crater_enronqa_2026-08-30.json`
(summary) + `paraphrase_above_gold_padded_2026-08-30.json` (per-needle above-gold
audit). Basis: the 2026-08-30 gate-sweep per-query ladder receipts (integration
`e6dba22`), plus a live read-only replay of all 31 orig-hit/reph-miss pairs on the
padded bed — **31/31 replayed ranks match the receipt exactly** (deterministic
pipeline, live replay validated as a receipt basis).

## 1. The gap is bidirectional churn plus a net loss

Pair fates at shipped defaults (`min_delivered_docs = 12`, the released v0.9.1 toml):

| bed | both | orig-only | reph-only | neither | net gap |
|---|---:|---:|---:|---:|---:|
| v2 (85k) | 196 | 24 | 12 | 18 | 4.8pp |
| padded (597k) | 177 | 31 | 11 | 31 | 8.0pp |

Rephrasing shuffles delivery in *both* directions (~12–18 pairs each way at 85k);
the crater proper is the net orig-only excess, and it grows with corpus size —
distractor mass makes anchor loss more expensive.

## 2. Mechanism: lexical anchor loss, dominated by named entities

For every orig-hit/reph-miss pair on padded (n=31), the audit diffs the Stage-1
keyword sets (`accel.extract_query_signals`) of the two phrasings and checks which
lost keywords actually occur in the gold document:

- **25/31 misses lost ≥1 gold-hitting keyword** in the rephrase.
- The losses are dominated by **named entities the paraphrase replaced with roles or
  pronouns**: *schiesser, heath, maggi, mike, cooper, steve, richard, sanders,
  rogelio, lopez-velarde, eugene, reuters, gds, pge, mtm* ("according to Heath
  Schiesser's email" → "the person"; "Rogelio Lopez-Velarde" → "the lawyer").
- **12/31 are pool-absent** (gold scores zero in the *entire* fused map, not merely
  below a horizon — the ladder ranks the full map). Every absent case lost multiple
  gold-hitting anchors (082: envelope/stuffing; 071: maggi/mike/argument; 186:
  cooper/steve/narrowing; 192: richard/sanders/defending). Absent = pool-level
  recall loss; unreachable by any reranker.

The retrieval path at shipped defaults is fully algorithmic (FTS5 + tags + synonyms
+ co-activation + cymatics; dense and SPLADE default-off since 2026-08-15/16). Its
recall is surface-lexical by construction, so paraphrase robustness is exactly the
axis the encoder-removal charter traded away. This doc prices that trade on a real
paraphrase split: **−4.8pp at 85k, −8.0pp at 597k delivered gold.**

## 3. Discrimination lane: KILLED

For the rescuable near-band (gold at map rank 13–46, 16 needles), the W2.2-narrow
question was: do the interlopers above gold match the rephrased query *more weakly*
(fewer distinct terms, more mass) so a coverage/coordination-aware signal confined
to the eps band could lift gold?

**No.** Comparing distinct-keyword coverage of gold vs each interloper's full
content: gold beats the interloper majority on **5/16 needles only** (11/16
losses; several interlopers out-cover gold outright). Once the anchors are gone the
residual query terms are generic, and the interlopers *legitimately* match them as
well as gold does. There is no coverage margin to discriminate on — the same shape
as the W2.1 COVER-walk kill (mass → hubs, not gold). Recorded as a fatal fact for
coverage/coordination reranking on this crater.

## 4. Abstain false negative: real but rare (1/500)

`enron_173_r` abstains on the padded bed in all three arms with gold at map rank
2–5: the RRF baseline-normalized ratio gate (`ratio_threshold_rrf_norm = 1.5`,
`tier_logic.py`) reads its flat candidate curve (norm ratio 1.43) as "tied → no
signal". The `abstain_reason` string says `score_below_floor` but under RRF the
absolute floor is bypassed — the label is a misnomer for the ratio-only gate (worth
a one-line fix in a docs/telemetry pass, tracked in the receipt). Frequency: 1/500
needles padded (same needle in all arms), 1/500 on v2 at v090 only, 0/470 on ERB
947k. Not a calibration campaign; noted as a known edge.

## 5. What remains (lanes)

1. **Priced, documented cost** — this doc + receipts are the deliverable for the
   crater lane at neural-free defaults. The gap is a property of the charter, not a
   ranking defect.
2. **Encoder opt-in counterfactual (open cell):** does `dense_embedding_enabled =
   true` close the rephrased gap on EnronQA? Untested — no bed carries BGE-M3
   vectors (`dense_embed_on_ingest` off since #371); the cell needs a backfilled
   twin bed. Worth running before recommending the opt-in to paraphrase-heavy
   workloads; the ERB isolation receipts (dense demotes gold) do NOT cover the
   rephrased-split regime, and this is the regime dense is *for*.
3. **Needle-set hygiene:** the rephrased split's entity-dropping is partly an
   artifact of how the paraphrases were generated (roles/pronouns for names). A
   split that preserves the asker's legitimate knowledge ("what did the email about
   the Xcelerator say…") vs one that deletes identifying information the asker
   would realistically have measures two different products. Flag when citing the
   −8pp externally.

No default changes proposed from this campaign.

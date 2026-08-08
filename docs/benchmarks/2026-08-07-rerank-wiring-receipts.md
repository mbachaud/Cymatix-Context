# Rerank production wiring — receipts & verdict (#341)

**Date:** 2026-08-06/07 · **Branch:** `fork1/rerank-wiring` (base `fork1/encoder-daemon` @ 2060f55) ·
**Kickoff:** `docs/design/2026-08-06-rerank-wiring-kickoff.md` · **Plan:** `docs/superpowers/plans/2026-08-06-rerank-wiring.md`
**Probe being productionized:** `2026-08-04-rerank-and-fork1-inputs.md` §1 (GO: 0.433→0.500 recall@12 at 829k).

**Metric names used throughout.** *Final-order recall* = `final_recall_at_k`, gold's rank in
`genome.last_ranked_ids` (the order the cross-encoder actually rewrites). *Delivered-gold rate* =
`delivered_gold_rate`, gold present in `expressed_gene_ids` after the budget/assembly cuts. The
legacy `recall_at_k` (pool rank from `last_query_scores`) is reported but is **+0.00pp in all six
A/B pairs, by construction**: rerank permutes ids without rewriting the fused scores that
`last_query_scores` publishes, so that basis is structurally blind to this change. Never read a
rerank verdict off it.

## Verdict

**The probe's GO replicates end-to-end through the production seam — on the population it was
measured on, with the pair-text form the reranker was designed around.** `[retrieval] rerank_enabled`
**ships default-OFF** (the dogfood receipt makes the why unambiguous), with the summary+domains pair
form, daemon-served scoring, and per-class/population gating as the intended enablement path.

| Bar (kickoff) | Result | Receipt |
|---|---|---|
| (a) 829k ≥ **+5pp recall@12** | **PASS** — +6.7pp final-order recall (0.4667→0.5333), both orders, 0 final-order losses (probe-30, summary text) | `rerank_ab_829k_probe30_sumtext_run{1,2}*` |
| (a) 829k ≤ **+5% p50** | **MIXED** — p50 ratios 1.031 (run 1, PASS) / 1.068 (run 2, FAIL) on this A/B; p95 ratios 1.018 / 1.031 both PASS; all four probe-30 p50 ratios 0.968 / 1.021 / 1.031 / 1.068, mean ~1.02 — bed noise straddles the bar order-to-order. Mechanism cost is smaller than the spread: **+1.8–3.8%** of the paired OFF arm's p50 (`signal_ms_median.xenc_rerank` 269.5–558.2ms on ~14.0–15.2s queries) | same + `rerank_ab_829k_probe30_run{1,2}*` |
| (b) 100k direction + cost | direction **positive on the rank basis, flat on delivery** — +6.7pp final-order recall (0.6000→0.6667) both orders; delivered-gold **flat 0.500→0.500**, 2 delivered rescues against 2 delivered losses. Cost 296.8 / 301.4ms xenc on 1.57–2.09s OFF p50 ≈ **+14% to +19% relative** | `rerank_ab_100k_run{1,2}*` |
| (c) dogfood cost honesty | xenc 284.3 / 312.8ms on OFF-arm queries of 236.5 / 389.0ms p50 = **+32% to +180%** end-to-end p50 (ratios 1.316 / 2.804). This is why the default stays OFF. | `rerank_ab_dogfood_run{1,2}*` (cost-only: no ladder-format gold exists for the dogfood bed — empty-gold documented in the receipt) |
| (d) daemon capacity incl. rerank | **PASS both profiles** — GPU 30-bundles/s bar and CPU 3.5 bar preserved (`BUNDLE_FAMILIES` split); rerank informational: GPU ~154–327 pairs/s, CPU ~66–165 (sequential-latency proxy, labeled) | `encoder_daemon_capacity_{gpu,cpu}_rerank.json` |
| Splice-interaction (ring) | **Count into splice is NOT invariant end-to-end** — see finding 2. Store-level count invariance holds (test-proven); membership-driven drift appears at splice on **7–12 of 30** needles depending on the pair. | per-needle `splice_n_candidates` in every ladder receipt |

ON-arm proof discipline held everywhere: every ON side shows token-verified daemon identity and
`POST /encode/rerank` counts ≥ needles+warmup (31/31 at 829k and 100k, 19/19 dogfood) in that run's
own daemon log; ladder arm self-validation (`xenc_rerank` present-in-arm/absent-in-baseline) green in
every accepted run. Order reversal is genuine (inverted-base-config runs), unlike the pre-campaign
"order-reversed ×2" receipts, which were repeat runs — the ladder cannot reverse arm order. The
known hole in that proof is stated in `rerank_ab.py`'s docstring: it is a floor on POST **count**,
not per-query evidence, so it catches silent whole-run fallback rather than a single fallen-back
query.

## The population finding (what the first A/B taught)

The initial 829k A/B ran the blob set's **first 30 needles** and came back **negative** (−3.3pp
final-order recall, −3.3pp delivered, both orders — `rerank_ab_829k_run{1,2}*`). Diagnosis: that
population has almost **zero rerank headroom** — 12 of its 13 final-order misses are *static* misses
whose rank is byte-identical between arms. Eleven of them sit at **final-order rank 81–516**
(erb_004 81 … erb_001 516); the twelfth, erb_006, has **no gold in the final order at all** (a null
`final_rank_of_first_gold`) and counts toward the 12. Rerank left every one of them exactly where it
found it, which doubles as a mechanics proof. Exactly one needle (erb_021, 41→16) actually moved and
still missed, and one rank-1 gold was demoted out of delivery under the content pair text.
Re-running on the probe's own 30 carve-safe needles (`needles_resolved_blob_probe30.json`, names
extracted from the probe receipt) restored the apples-to-apples comparison. **Rerank pays where the
near-cutoff class exists; on static-miss populations it can only churn.** Any enablement decision
must ask which population a deployment resembles — that, not query class, is the first-order gate.
(#340's class split is the right lens; note its 99/469 static-miss ratio is population-wide, while
per-slice composition varies wildly.)

## The pair-text finding — a population-dependent tradeoff, not a free win

The probe script was never committed, so the plan shipped content-prefix pair text with one
sanctioned variation: summary+domains (`f"{summary} [{domains}]"`, mirroring
`DeBERTaRibosome.re_rank`, content fallback for summary-less docs). Both forms were run on **both**
populations, and the four cells do not tell one story:

| Population | Pair text | Final-order recall (OFF→ON) | Delivered-gold rate (OFF→ON) |
|---|---|---|---|
| probe-30 (**has** headroom) | content | 0.4667 → 0.4667 (**+0.0pp**) | 0.3667 → 0.4333 (**+6.7pp**) |
| probe-30 (**has** headroom) | summary+domains | 0.4667 → 0.5333 (**+6.7pp**) | 0.3667 → 0.4333 (**+6.7pp**) |
| first-30 (**no** headroom) | content | 0.5667 → 0.5333 (**−3.3pp**) | 0.5000 → 0.4667 (**−3.3pp**) |
| first-30 (**no** headroom) | summary+domains | 0.5667 → 0.5333 (**−3.3pp**) | 0.5000 → 0.4333 (**−6.7pp**) |

Read both columns before concluding anything:

- **On the headroom population, summary+domains is decisively better.** It converts the content
  form's +0.0pp final-order reading into +6.7pp with **zero final-order losses**, and it matches
  content on delivery. Cross-encoder judgement on tagger summaries beats judgement on 256-token
  content prefixes for this corpus.
- **On the no-headroom population, summary+domains is *worse* — on the basis this campaign
  privileges.** The rank basis is a tie (−3.3pp either way), but delivered gold falls twice as far
  under summary+domains (−6.7pp vs −3.3pp): `rerank_ab_829k_first30_sumtext_run{1,2}` lose erb_011
  (rank 1→10), erb_025 (rank 1→20) and erb_029 (rank 6→2 — its rank *improves* and delivery is still
  lost, a pure membership/budget effect) against a single rescue, erb_021 (41→2). The content form
  loses only erb_011 (1→11) and erb_017 (1→16) against the erb_013 rescue.
- The one thing summary+domains unambiguously fixes on first-30 is the demotion this doc originally
  singled out: **erb_017 goes 1→16 under content and only 1→3 under summary+domains, staying
  delivered.** But it introduces two *new* rank-1 demotions there — erb_025 (1→20, delivery lost)
  and erb_026 (1→15, undelivered in both arms) — which is why the delivery column moves against it.

**Summary+domains is the shipped form** (code constant, `knowledge_store._rerank_doc_text`), and the
honest reason is scoped: it is the better form *for the populations where rerank is meant to be
enabled at all*. On a static-miss population like first-30 the right setting is not a different pair
text — it is `rerank_enabled = false`, which is what ships. The pair-text choice does not rescue a
population without headroom, and this doc should not be read as claiming it does.

## The delivery-vs-rank finding (#335 vindicated)

On probe-30 with content text, final-order recall read **+0.0pp** while **delivered-gold read
+6.7pp** (13 vs 11 needles, both orders): rerank promoted in-pool gold high enough to survive the
blend/budget/assembly cuts. Rank-based recall is structurally blind to this — which is exactly why
the kickoff sequenced the delivered-gold metric first. The ladder now carries both bases per needle
(`final_rank_of_first_gold` from `last_ranked_ids`; `delivered_gold*` from `expressed_gene_ids`),
plus `gate_kept`/`floor_appended` for pool-slicing. #335's blocker is retired: the ANN-gate,
coact, and synonym verdicts can now be re-read on the delivery basis.

The inverse also shows up, and it is the sharper lesson: on probe-30 with summary+domains the
delivery basis records a **loss** (erb_029, delivered 1→0) that the rank basis cannot see at all,
because erb_029's rank *improved* 6→2. On 100k it is starker still — erb_012 keeps final rank 1 in
both arms and loses delivery anyway. Membership changes upstream reshuffle what fits in the budget;
neither basis subsumes the other.

**Replication anchor.** The probe's baseline was recall@12 = 0.433 at 829k. The OFF arm of the
probe-30 A/B reads `final_recall_at_k` = **0.4667** — a +3.4pp basis offset (different metric,
different pipeline revision), with the **same +6.7pp delta** to 0.5333 on the ON arm. The delta
replicating on a shifted baseline is stronger evidence than a coincidental absolute match would be.

**Floor slice.** Across all twenty 30-needle ladder arms (five A/B pairs × two run orders × two
arms), the #214 dense-pool floor fires on exactly
**one needle, erb_087, with `floor_appended` = 1**, and only on the probe-30 population (both arms of
both probe-30 pairs; `floor_appended` is 0 on every needle of the first-30 and 100k ladders). That
one needle is load-bearing: under content text it is the demotion (final rank 8→14) that exactly
cancels the erb_038 rescue to produce the +0.0pp reading, and under summary+domains it lands at 12 —
inside k by one place. The floor-rescued document being the marginal case is expected (it entered
below the sim head by definition) and is why the seam judges rescued documents rather than evicting
them.

## Finding 2: splice-count drift is real (ledger claim-1 interaction)

The store-level count invariant is exact (test-proven, including the #214 floor-rescue and
stale-index edge cases found in review). But **membership** changes propagate: different docs carry
different scores into the budget-tier/classifier/assembly cuts, so a materially different set of
needles enters splice with a different candidate count when rerank is ON. Measured per-needle
(`splice_n_candidates`, mark-correlated ring drain), the drift count is **7–12 of 30** and is
identical across both run orders of each pair:

| A/B pair | needles with drifted `splice_n_candidates` |
|---|---|
| 829k first-30, summary+domains | 9 / 30 |
| 829k probe-30, content | 9 / 30 |
| 829k first-30, content | 8 / 30 |
| 829k probe-30, summary+domains | 7 / 30 |
| 100k | **12 / 30** |

The 25200/n splice-cliff mechanism is therefore *live* in this path — it contributes to run-to-run
p50 spread and must be re-verified for any future change of `rerank_depth` or blend ordering.

## Cost profile and the daemon path

Per-query xenc cost (GPU daemon), from `signal_ms_median.xenc_rerank` on the eight 829k ON arms:
**269.5–558.2ms** (probe-30 arms only: 269.5–503.4ms), and 284.3–312.8ms at 100k/dogfood — higher
than the probe's in-process batched GPU figure of 219ms for 50 pairs
(`encoder_throughput_cuda.txt:39`) because the daemon's sequential executor scores pair-by-pair (per-item
`submit_cached`, cacheable but never a padded batch forward). A batched daemon scoring path is the
obvious optimization if rerank enablement widens; it was deliberately traded for cacheability in
this campaign. Daemon `/ready` now gates on the rerank family loading (~80MB, ~3.4s) — accepted as
an operational semantic (all-families daemon contract; documented here per the Task 8 review).

## Bed integrity

`erb_829k_nosplade.db`: no splade tables created (28 tables before and after; the cell configs'
`splade_enabled=false` + `splade_auto_enable_below_genes=0` held). Main file grew 84KB from
open-time ANALYZE (`sqlite_stat1`, 27 rows) — idempotent, arm-symmetric after first open; both A/B
orders reproduced identical recall values, so no plan-shift artifact. WAL empty after close.

## Config surface shipped (all default-inert; suite 3958+ green)

- `[retrieval] rerank_enabled = false` — master switch, pre-cap seam (`query_docs_ann` pre-gate;
  `query_docs` post-fusion for the dense-off profile), count-preserving.
- `[retrieval] rerank_depth = 50`, `rerank_model = "cross-encoder/ms-marco-MiniLM-L-6-v2"`,
  `rerank_enabled_by_class = {}` (per-class map on the #255 pattern; empty in A/B cell configs by
  runner preflight decree).
- `[hardware] rerank_device` — now consumed (`resolve_layer_device("rerank")` at model load).
- **Removed:** `[ingestion] rerank_enabled` and the `blend.py` post-cap branch — dead under deberta
  (the `re_rank`/`rerank` name mismatch meant the cross-encoder never ran from serving), and
  misrouted to the LLM `Compressor.rerank` under ollama/litellm. `[ingestion] rerank_model` stays
  (legacy DeBERTa ctor input only).
- Daemon `/encode/rerank` + `EncoderClient.encode_rerank` + `rerank_backend._remote_scores`
  (circuit/ready-gate/fallback parity with the splade seam).

## Recommended next steps (not in this PR)

1. Batched daemon rerank scoring (single padded forward per request) — halves the xenc cost seen here.
2. Population-aware gating: a cheap pre-check (pool headroom estimate, e.g. `gate_kept` vs gold-depth
   distribution at calibration time) beats query-class gating on this evidence.
3. Re-read the #335-blocked verdicts (ANN-gate min_genes, coact pull-forward, erb_039 synonym) on
   the delivered-gold basis now shipping in the ladder.

# Rerank production wiring — receipts & verdict (#341)

**Date:** 2026-08-06/07 · **Branch:** `fork1/rerank-wiring` (base `fork1/encoder-daemon` @ 2060f55) ·
**Kickoff:** `docs/design/2026-08-06-rerank-wiring-kickoff.md` · **Plan:** `docs/superpowers/plans/2026-08-06-rerank-wiring.md`
**Probe being productionized:** `2026-08-04-rerank-and-fork1-inputs.md` §1 (GO: 0.433→0.500 recall@12 at 829k).

## Verdict

**The probe's GO replicates end-to-end through the production seam — on the population it was
measured on, with the pair-text form the reranker was designed around.** `[retrieval] rerank_enabled`
**ships default-OFF** (the dogfood receipt makes the why unambiguous), with the summary+domains pair
form, daemon-served scoring, and per-class/population gating as the intended enablement path.

| Bar (kickoff) | Result | Receipt |
|---|---|---|
| (a) 829k ≥ **+5pp recall@12** | **+6.7pp** final-order recall, both orders, 0 losses (probe-30, summary text) | `rerank_ab_829k_probe30_sumtext_run{1,2}*` |
| (a) 829k ≤ **+5% p50** | ratios 1.031 / 1.068 (this A/B); all four probe-30 ratios 0.968/1.021/1.031/1.068, mean ~1.02 — bed noise straddles the bar order-to-order; mechanism cost ~+3–4% (xenc p50 412–557ms on ~15s queries) | same + `rerank_ab_829k_probe30_run{1,2}*` |
| (b) 100k direction + cost | direction **positive**: +6.7pp rank recall both orders, 2 rescues, 0 losses; cost ~300ms xenc on ~1.9s p50 ≈ **+15% relative** | `rerank_ab_100k_run{1,2}*` |
| (c) dogfood cost honesty | xenc ~285–314ms on ~240–510ms queries = **+32% to +180% relative** (p50 ratios 1.316/2.804). This is why the default stays OFF. | `rerank_ab_dogfood_run{1,2}*` (cost-only: no ladder-format gold exists for the dogfood bed — empty-gold documented in the receipt) |
| (d) daemon capacity incl. rerank | **PASS both profiles** — GPU 30-bundles/s bar and CPU 3.5 bar preserved (`BUNDLE_FAMILIES` split); rerank informational: GPU ~177–327 pairs/s, CPU ~66–165 (sequential-latency proxy, labeled) | `encoder_daemon_capacity_{gpu,cpu}_rerank.json` |
| Splice-interaction (ring) | **Count into splice is NOT invariant end-to-end** — see finding 2. Store-level count invariance holds (test-proven); membership-driven drift appears at splice on 8–9/30 needles. | per-needle `splice_n_candidates` in every ladder receipt |

ON-arm proof discipline held everywhere: every ON side shows token-verified daemon identity and
`POST /encode/rerank` counts ≥ needles+warmup (31/31 at 829k, 19/19 dogfood) in that run's own
daemon log; ladder arm self-validation (`xenc_rerank` present-in-arm/absent-in-baseline) green in
every accepted run. Order reversal is genuine (inverted-base-config runs), unlike the pre-campaign
"order-reversed ×2" receipts, which were repeat runs — the ladder cannot reverse arm order.

## The population finding (what the first A/B taught)

The initial 829k A/B ran the blob set's **first 30 needles** and came back **negative** (−3.3pp
rank, −3.3pp delivered, both orders — `rerank_ab_829k_run{1,2}*`). Diagnosis: that population has
almost **zero rerank headroom** — 12 of its 13 misses are static-miss (gold at pool rank 81–516,
beyond `rerank_depth=50` by construction; their ranks pass through byte-identical, which doubles as
a mechanics proof). Exactly one needle was rescuable and one rank-1 gold was demoted under the
content pair text. Re-running on the probe's own 30 carve-safe needles
(`needles_resolved_blob_probe30.json`, names extracted from the probe receipt) restored the
apples-to-apples comparison. **Rerank pays where the near-cutoff class exists; on static-miss
populations it can only churn.** Any enablement decision must ask which population a deployment
resembles — that, not query class, is the first-order gate. (#340's class split is the right lens;
note its 99/469 static-miss ratio is population-wide, while per-slice composition varies wildly.)

## The pair-text finding

The probe script was never committed, so the plan shipped content-prefix pair text with one
sanctioned variation. Content text: probe-30 rank recall **+0.0pp** (rescue and demotion cancel).
Summary+domains text (`f"{summary} [{domains}]"`, mirroring `DeBERTaRibosome.re_rank`, content
fallback for summary-less docs): **+6.7pp, zero losses** — and the first-30 demotion (erb_017,
rank 1→16) disappears too. The cross-encoder judges tagger summaries better than 256-token content
prefixes on this corpus. Summary+domains is the shipped form (code constant,
`knowledge_store._rerank_doc_text`).

## The delivery-vs-rank finding (#335 vindicated)

On probe-30 with content text, rank recall read **+0.0pp** while **delivered-gold read +6.7pp**
(13 vs 11 needles, both orders): rerank promoted in-pool gold high enough to survive the
blend/budget/assembly cuts. Rank-based recall is structurally blind to this — which is exactly why
the kickoff sequenced the delivered-gold metric first. The ladder now carries both bases per needle
(`final_rank_of_first_gold` from `last_ranked_ids`; `delivered_gold*` from `expressed_gene_ids`),
plus `gate_kept`/`floor_appended` for pool-slicing. #335's blocker is retired: the ANN-gate,
coact, and synonym verdicts can now be re-read on the delivery basis.

## Finding 2: splice-count drift is real (ledger claim-1 interaction)

The store-level count invariant is exact (test-proven, including the #214 floor-rescue and
stale-index edge cases found in review). But **membership** changes propagate: different docs carry
different scores into the budget-tier/classifier/assembly cuts, so 8–9 of 30 needles enter splice
with a different candidate count when rerank is ON. The 25200/n splice-cliff mechanism is therefore
*live* in this path — it contributes to run-to-run p50 spread and must be re-verified for any
future change of `rerank_depth` or blend ordering. The receipt that caught this is the per-needle
`splice_n_candidates` (mark-correlated ring drain).

## Cost profile and the daemon path

Per-query xenc cost (GPU daemon): ~412–557ms at 829k, ~300ms at 100k/dogfood — higher than the
probe's 219ms/50 pairs because the daemon's sequential executor scores pair-by-pair (per-item
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

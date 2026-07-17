# Knob graduation receipts — #255 classifier-gated combinator & #260 rank-gated RRF

**Date:** 2026-07-16
**Branch:** `bench/knob-receipts-2026-07-16`
**Scope:** Graduation receipts for the two default-inert knobs shipped 2026-07-13:
- **#255** — classifier-gated rerank combinator (`[retrieval] rerank_combinator_by_class`, PR #278, commit `bae015a`)
- **#260** — rank-gated RRF (`[retrieval] rrf_gate_enabled` / `rrf_gate_top_m`, PR #280, commit `a51aa17`)

**Receipts only — NO default flips in this PR.** Flips are separate follow-ups per council rule.

## Method

Offline, retrieval-only, LLM-free, read-only beds. All beds dense-backfilled.

- **Semantic beds** (`enterprise_rag_10k_batched.db`, `enterprise_rag_50k_batched.db`):
  `benchmarks/ab_semantic_probe.py`, **fused arm** (dense + SPLADE + FTS + tag + anchor),
  ERB `semantic` questions from `erb_sweep_queries_erb{10k,50k}.json`. 10k = full 125;
  **50k = n=60** (runtime budget — 50k steady-state ~24 s/question).
- **xl literal bed** (`xl.db`, 50 SIKE needles): `benchmarks/ab_rerank_combinator.py`
  on a **fused** base config (dense + SPLADE on), `--combinators additive`; the knob under
  test is supplied via the base-config TOML so build_context routes it per-query.
  Literal-bed **safety check**: the knob must not dent literal delivery.

Knobs are threaded through `build_context` → store → per-shard fan-out; the probe reaches
them via `--base-config` TOMLs (`rerank_combinator_by_class` map / `rrf_gate_*` scalars).
Delivery metrics (`gold_delivered_id`, `pool_present`, text overlap, answerability) come
straight from the returned window and are faithful under every cell.

## Classifier routing (verified — the whole point of #255)

`VALID_QUERY_CLASSES = (arithmetic, factual, procedural, multi_hop, default)`.
Proposed map: `{multi_hop = "eps_band", default = "eps_band"}` — literal classes
(`factual`/`arithmetic`/`procedural`) stay on the shipped global `additive`.

| query set | n | class distribution | routed → eps_band |
|---|---|---|---|
| xl 50 needles (literal) | 50 | factual 36, default 13, procedural 1 | **13/50 (26%)** |
| 10k semantic | 125 | multi_hop 112, default 6, arithmetic 6, procedural 1 | **118/125 (94%)** |
| 50k semantic | 125 | multi_hop 112, default 6, arithmetic 6, procedural 1 | **118/125 (94%)** |

The classifier routes as intended: paraphrastic ERB questions land in `multi_hop`/`default`
(→ eps_band, the win region), while literal SIKE needles are mostly `factual` (→ additive).
The 13 `default`-classed xl needles are the literal-bed exposure the safety check must clear.

---

## Receipt 1 — #255 classifier-gated combinator (proposed map vs empty map)

### Semantic beds (fused arm)

| bed | cell | n | gd_id | pool | mean_rank | med_rank | txt_overlap |
|---|---|---|---|---|---|---|---|
| 10k | control (empty map) | 125 | 0.392 | 0.856 | 14.0 | 10.0 | 0.546 |
| 10k | proposed map | 125 | **0.392** | 0.856 | **9.1** | **5.0** | 0.551 |
| 50k | control (empty map) | 60 | 0.333 | 0.750 | 13.9 | 12.0 | 0.531 |
| 50k | proposed map | 60 | **0.333** | 0.750 | **8.7** | **6.0** | 0.535 |

### Per-class breakdown (semantic beds — the whole point)

| bed | class | n | gd_id ctrl→prop | med_rank ctrl→prop |
|---|---|---|---|---|
| 10k | multi_hop (→eps_band) | 112 | 0.393 → 0.393 | **10 → 5** |
| 10k | default (→eps_band) | 6 | 0.333 → 0.333 | **22 → 3** |
| 10k | literal (unmapped) | 7 | 0.429 → 0.429 | 12 → 12 (byte-identical) |
| 50k | multi_hop (→eps_band) | 55 | 0.345 → 0.345 | **12.5 → 5.5** |
| 50k | default (→eps_band) | 2 | 0.500 → 0.500 | 2 → 1 |
| 50k | literal (unmapped) | 3 | 0.000 → 0.000 | 21 → 21 (byte-identical) |

The unmapped literal rows are byte-identical between cells — the class gate routes exactly
as configured. All rank lift concentrates in the mapped classes; delivery never moves.

### xl literal bed (fused; safety check)

| cell | n | gd_id | gd_text | answerability | mean_rank | exact_inv | gold_inv |
|---|---|---|---|---|---|---|---|
| control | 50 | 0.460 | 0.360 | 0.800 | 6.0 | 728 | 68 |
| proposed | 50 | **0.460** | **0.360** | **0.800** | 6.4 | **544** | **55** |

Per-class on xl: the 37 unmapped literal needles are byte-identical; the 13 `default`-classed
needles keep gd_id 0.538 / answerability 1.000 in both cells (med rank 4→7, delivery
untouched). Exact inversions drop 728→544 as a side benefit. **No literal regression.**

## Receipt 2 — #260 rank-gated RRF (`rrf_gate_top_m` sweep vs gate off)

### Semantic beds (fused arm)

| bed | cell | n | gd_id | pool | mean_rank | med_rank | txt_overlap |
|---|---|---|---|---|---|---|---|
| 10k | off | 125 | 0.392 | 0.856 | 14.0 | 10.0 | 0.546 |
| 10k | top_m=5 | 125 | 0.392 | 0.856 | 13.4 | 10.0 | 0.545 |
| 10k | top_m=10 | 125 | 0.392 | 0.856 | 13.0 | 9.0 | 0.548 |
| 10k | top_m=20 | 125 | 0.384 | 0.856 | 12.8 | 9.0 | 0.544 |
| 50k | off | 60 | 0.333 | 0.750 | 13.9 | 12.0 | 0.531 |
| 50k | top_m=5 | 60 | 0.333 | 0.750 | 16.2 | 10.0 | 0.536 |
| 50k | top_m=10 | 60 | 0.333 | 0.750 | 14.1 | 10.0 | 0.535 |
| 50k | top_m=20 | 60 | 0.333 | 0.750 | 13.8 | 10.0 | 0.531 |

### xl literal bed (fused; safety check)

| cell | n | gd_id | gd_text | answerability | mean_rank | exact_inv |
|---|---|---|---|---|---|---|
| off | 50 | 0.460 | 0.360 | 0.800 | 6.0 | 728 |
| top_m=5 | 50 | 0.460 | 0.360 | 0.800 | 5.4 | 605 |
| top_m=10 | 50 | 0.460 | 0.360 | 0.800 | 5.3 | 710 |
| top_m=20 | 50 | 0.480 | 0.380 | 0.800 | 5.6 | 788 |

---

## Verdicts

### #255 classifier-gated combinator — **GRADUATE**

The pre-registered gate (PR #278) is met on all three beds. **10k (win region):** the
proposed map lifts semantic median gold rank 10 → 5 (mean 14.0 → 9.1) with delivery
exactly preserved (gd_id 0.392, pool 0.856) — the predicted 10→6 signal, slightly better.
**50k (sharded threading):** same shape, med rank 12 → 6, delivery preserved — the
per-shard fan-out holds at scale. **xl (literal no-regression):** delivery byte-stable
(0.460 / 0.360 / 0.800); the 37 unmapped literal needles are byte-identical and the 13
`default`-classed needles keep full delivery; exact inversions even drop 728 → 544.
The per-class split confirms the mechanism: every gained rank sits in the mapped classes.
Recommended follow-up (separate PR per council rule): flip the shipped default to
`{multi_hop = "eps_band", default = "eps_band"}`, then evaluate `default → off` and
adding `procedural` as the next increments.

### #260 rank-gated RRF — **NEEDS-829K** (safe, but no win at these scales)

The gate is delivery-neutral everywhere measured: gd_id flat at 0.392 (10k) / 0.333 (50k)
across top_m ∈ {5, 10, 20} (one-question dip at 10k/top20, 0.392 → 0.384), with only mild
rank movement (med 10 → 9 @10k, 12 → 10 @50k; top_m=5 worsens 50k mean rank 13.9 → 16.2).
The xl literal safety check passes — delivery never drops, top_m=20 is even +1 needle
(0.460 → 0.480). This is consistent with the evidence curve: fused−lexical gd_id
+0.054 @10k → +0.006 @50k → −0.060 @829K — dense's votes only become *anti-signal* at
blob scale, which is precisely where the gate should pay. **Verdict: do not graduate on
this receipt; hold for the 829K cell at the next blob window** (box currently owned by
the 500q re-capture). The knob is demonstrably safe to leave available in the meantime.

## 829K deferral

The #260 evidence curve (fused−lexical gd_id: +0.054 @10k → +0.006 @50k → **−0.060 @829K**)
puts the gate's largest expected win at 829K, where unconditional RRF's dense votes invert
below lexical (median gold rank ~50,357). **The 829K receipt is deferred to the next blob
window** — that box resource is owned by a concurrent 500q re-capture during this run.
The 10k/50k cells here test the gate at the margins where fused still ≥ lexical.

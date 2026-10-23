# 2026-10-23 — Jev as a System One packet judge (know/miss, filtering, rerank)

> **EXPERIMENTAL.** Jev is TypeSafe's hosted "System One" model: typed questions
> in, calibrated probabilities out, no generated text. It is a proprietary API, not
> part of the cymatix pipeline. Nothing measured here changed retrieval; the
> integration this note supports (`[system_one]`, default off) only adds a block to
> `/context/packet`. Harnesses and analysis: Claude Opus 5.5 under Max Bachaud's
> direction; measurement design and receipts reviewed in session.

**Question.** Given the packet cymatix already assembled, can a System One model
(1) tell packets that hold the answer from packets that don't (know vs miss),
(2) filter the packet down to the chunks that answer, and (3) rank the answering
chunk first? And do cymatix's own signals (delivered rank, fused score, the
Stage-6 know/miss block) help it when they're passed in?

**Short answer.** (1) Yes, strongly: packet-level AUROC 0.906 on ERB 947k, where
the shipped know/miss contract said `miss` on every needle. (3) Yes: gold MRR
0.69 → 0.92. (2) Not as a hard filter: it drops the gold chunk on 22% of the
needles where it was delivered. The cymatix signals add nothing measurable to
the judge (AUROC 0.906 vs 0.900 without them, delta inside noise).

## Setup

* **Model:** `jev-latest`, resolved by the API to **jev-1.14.0** on every call
  (the 2026-09-27 dogfood pilot ran on 1.13.0).
* **Typed questions** (per needle): a question-type Choice over a five-row sheet
  (`config_value`, `code_location`, `mechanism`, `history_decision`,
  `fact_lookup`); per delivered chunk an `answers` Noul, a 4-level `relevance`
  Score and a type-specific Noul; one packet-level Noul, "does any chunk in the
  packet state the answer?".
* **Incoming rankings** (arm *with*): the cymatix query-classifier class, each
  chunk's delivered rank and fused retrieval score, and the know/miss block.
  Arm *without*: none of those, and packet order shuffled per needle
  (`random.Random(needle)`) so position cannot leak the rank.
* **Profile:** `benchmarks/dogfood/grain_ladder/configs/grain_c4000_classcap.toml`
  (4000-char grain, classcap 2000, k=12, RRF k=20, dense off, abstain on).
* **Hygiene:** `PYTHONHASHSEED=0`, `CYMATIX_DISABLE_LEARN=1`, `read_only`,
  `ignore_delivered`, encoder daemon off, scoring clock pinned
  (`CYMATIX_SCORING_EPOCH=1792397919`, the ladder's value).

## Receipts

All on branch `claude/jev-packet-assembly-test-138065` (local; cut from
`bench/c580-multicorpus` a2793a7a for the c4000 profile, 0 behind master at run
time), mirrored with harnesses in the private `cymatix-receipts` repo at
`mirror-branches/claude__jev-packet-assembly-test-138065/` (commit 3760062).
Every receipt carries every raw Jev response; `--rescore` needs no key.

| run | harness commit | bed | needles | receipt |
|---|---|---|---|---|
| 239b synthetic | a2793a7a (+dirty harness, committed as 3a0a58f8) | 239b, 195 genes, fresh ingest | 72 | `jev_239b_packet_know_grain_c4000_classcap_2026-10-23.json` |
| ERB, with rankings | dd559101 | `erb_full_c4000_lean.db`, 947,532 genes | 470 | `jev_erb_packet_know_c4000_classcap_2026-10-23.json` |
| ERB, without rankings | 29ae7e7f (replays the packets above) | same | 470 | `jev_erb_packet_know_c4000_classcap_noincoming_2026-10-23.json` |
| paired comparison | 29ae7e7f | — | 470 | `compare_with_vs_no_incoming_erb_c4000_2026-10-23.json` |

ERB needle set `benchmarks/dogfood/erb/needles_resolved_grain4000_full.json`
(n=470) with `gold_by_needle_grain4000.json`. **Bed parity:** the ERB run
delivered gene ids identical to the 2026-10-19 classcap ladder
(`ladder_erb_full_c4000_classcap_2026-10-17.json`) on **470/470** needles,
delivered_gold 291/470 = 0.619. The replay arm asserted every chunk's text
char-for-char against the first run.

## 239b synthetic bed (72 needles)

30 answerable, 30 held out (gold never ingested — a true miss), 12 competition
(gold plus a wrong-answer twin). Cymatix delivered gold at rank ≤ 4 on all 42
in-corpus needles.

| signal | know/miss AUROC | at 0.5 |
|---|---|---|
| Jev packet question | **1.00** | 72/72 (know min 0.83, miss max 0.33) |
| Jev best single chunk | 1.00 | 64/72 (8 false knows) |
| cymatix know confidence | 0.50 | said `miss/sparse` on all 72 |
| cymatix top score | 0.54 | — |

Chunk filtering: `answers` AUROC 0.992 vs fused score 0.937; threshold 0.7 keeps
7.5% of chunks and keeps gold on 42/42. Rerank: gold MRR 0.665 → 0.952. The bed
is easy (one-sentence docs, so the grain is inert), and the per-chunk question
over-fires on same-entity, wrong-attribute chunks (0.5–0.93). Competitor twins
score 0.98: Jev does not flag a packet that contradicts itself.

## ERB 947k (470 needles)

Know = a resolved gold gene was delivered (291); miss = it wasn't (179). ERB has
no held-out needles, so a miss here is a real cymatix delivery miss.

| signal | know/miss AUROC | detail |
|---|---|---|
| Jev packet question, with rankings | **0.906** | 0.5: 251 TP / 45 FP / 40 FN / 134 TN (0.819) · 0.7: 224 / 19 / 67 / 160, precision 0.92 |
| Jev packet question, without rankings | **0.900** | 0.5: accuracy 0.800, 58 FP / 36 FN |
| Jev best single chunk | 0.825 | — |
| cymatix top score | 0.703 | — |
| cymatix know/miss contract | 0.50 | `miss/sparse` on all 470 |

Per ERB question type (packet AUROC, with rankings): basic 0.919, semantic 0.887
(cymatix delivered gold on only 36/125), intra_document_reasoning 0.926,
conflicting_info 0.944, constrained 0.861, completeness 0.857, miscellaneous 1.00
(n=20); project_related had no misses.

**Chunk filtering** (label = gold gene; 516 of 3,344 delivered chunks): relevance
AUROC 0.875, answers 0.818, cymatix fused score 0.809. Keeping `answers ≥ 0.5`
cuts the packet to 20% of its chars but keeps gold on only 228/291 needles.

**Rerank** of delivered chunks by `answers`: gold MRR 0.691 → 0.921 (with) /
0.914 (without); gold at rank 1 on 252 / 248 of 291 vs 152 for cymatix order.

**Do the cymatix rankings help?** No measurable effect on discrimination: packet
AUROC delta −0.006, paired bootstrap 95% CI [−0.015, +0.004] (2,000 resamples,
seed 239); 0.5-threshold flips 21 (with right) vs 12 (without right), sign test
p ≈ 0.16; no ERB type moves by more than 0.02. They mostly shift calibration:
without them Jev scores misses higher (mean 0.318 → 0.380). Caveat: the incoming
verdict was a constant `miss`, so this tests rank + score + an uninformative
verdict; a calibrated contract is untested.

**Cost** (from the replay arm, retrieval excluded): question type + per-chunk +
packet calls took p50 3.0 s / p90 4.2 s per needle for 9.1 calls (the 0.3 s
pacing floor dominates). Input tokens p50: question type 462, packet 9,018
(p90 13,293), chunk 1,625. Judge-only (two calls) ≈ 9.5k input tokens per
packet; its latency was not measured on its own.

## Reading limits

* **Gold labels are gene-grain.** The bank has no answer strings; relabelling at
  file grain changes nothing (gold genes cover the gold file). Of the three
  highest-scoring false knows, one (erb_052) looks like a genuine answer in a
  sibling ADR, so some false knows are label noise; two are topical but wrong.
* **Jitter.** The same packet scores about ±0.02 between runs; the with-rankings
  arm was not repeated, so the AUROC noise floor is the bootstrap only.
* **Chunk text.** Jev saw the full stored gene text, not Stage-5-trimmed text.
* **Single bed family.** One ERB bed, one profile, one provider version.

## What this supports shipping

`[system_one]` (default off; `docs/config-reference.md#system_one`):

* the **packet judge** — the measured *without-rankings* configuration: question
  type + packet question, request carries only question, type and item
  source/text. Two calls per packet; adds `system_one {answers_p, verdict, ...}`
  to `/context/packet`.
* an opt-in **rerank** (`rerank_items`) — per-item `answers` probability,
  reorders inside `verified` / `stale_risk` only (stable; never drops items).

Not supported by these receipts, so not shipped: a hard chunk filter (loses
gold on 22% of delivered needles), and letting the judge override the know/miss
contract. Differences from the measured run: the route sends items in cymatix
order (the replay arm shuffled them) and caps each item at `max_item_chars`
(8000; non-binding for c4000 chunks).

## Next

* Re-fit the know/miss calibration (#239 B1) and re-run the *with* arm: a
  contract that actually varies is the one input this ablation could not test.
* A bank with answer strings, to score chunk filtering at answer grain.
* A judge-only latency/cost run through `/context/packet` with the flag on.

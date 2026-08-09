# Feature isolation + prune campaign

> Spec, 2026-07-29. Isolate every shipped feature, identify what no longer earns
> its place, and remove it on a two-phase deprecate → remove path.
>
> Not a proposal to delete things. It is a proposal to **find out**, with a
> pre-registered decision rule, so removals are receipted rather than argued.

## Why now

The 2026-07-28/29 dogfood runs turned up three features that do not do what the
configuration implies:

- **`fts5_candidate_depth`** — byte-identical null at 50/200/1000 on a bed whose
  gap is ranking rather than admission.
- **`dense_additive_weight`** — inert under the shipped `fusion_mode="rrf"`; it
  only moves anything under `additive`. The 4.0 default was set by #203 under
  additive, before RRF became the default.
- **`cymatics` peak_width** — ~~collapsing the Gaussian spread produces **18/18
  identical ranks**. The spread is exactly inert.~~ **CORRECTED 2026-08-08:**
  the *knob* is inert — `[cymatics] peak_width` is parsed but never read, so
  the arm never collapsed the spread and ran identical to the shipped arm. The
  **spread itself is unmeasured**, not inert. Retraction detail:
  `docs/benchmarks/2026-07-29-cymatics-ablation-verdict.md`. This is still a
  finding of the intended kind — a knob that does not do what the
  configuration implies — just a different one than recorded.

Three findings from one small bed in two days. That rate suggests the surface is
carrying more dead weight than anyone has counted, and counting is cheap now:
the dogfood bed runs a full arm in **~20 seconds**.

## Surface

33 boolean/mode knobs across `[retrieval]`, `[ingestion]`, `[context]`,
`[cymatics]`, `[budget]`, `[plr]`, `[classifier]`. **13 ship OFF.**

The two groups need opposite questions asked of them.

### Group A — shipped ON (20 knobs): *does it earn its cost?*

`bm25_shortlist_enabled`, `dense_embedding_enabled`, `filename_anchor_enabled`,
`semantic_broaden_routing`, `dense_embed_on_ingest`, `entity_graph`,
`locale_demotion_enabled`, `sema_embed_on_ingest`, `splade_enabled`,
`cymatics.enabled`, `cymatics.harmonic_links`, `abstain_enabled`,
`legibility_enabled`, `session_delivery_enabled`, `plr.enabled`,
`classifier.enabled`, plus the mode knobs `blend_mode`, `doc_type_boost_mode`,
`fusion_mode`, `rerank_combinator`.

Ablate each to off/legacy and measure. A feature that costs latency, memory or
model load and moves nothing is dead weight **even though it is on** — that is
the harder and more valuable half, because on-by-default features are assumed
to be earning their keep and rarely re-examined.

Ingest-side flags (`sema_embed_on_ingest`, `splade_enabled`,
`dense_embed_on_ingest`) need a bed rebuild per arm, so they are measured on a
small dedicated corpus rather than the full dogfood bed.

### Group B — shipped OFF (13 knobs): *why is it off, and should it exist?*

`bm25_prefilter_enabled`, `entity_graph_retrieval_enabled`, `ray_trace_theta`,
`rrf_gate_enabled`, `seeded_edges_enabled`, `shard_fetch_scale_with_shards`,
`sr_enabled`, `colbert_enabled`, `rerank_enabled`, `symbol_graph`,
`cold_tier_enabled`, `cymatics.use_embeddings`, `foveated_enabled`.

Each is one of four things, and they have different fates:

| class | meaning | fate |
| --- | --- | --- |
| **dark-but-validated** | measured to help, off for an unrelated reason | document the reason, keep |
| **dark-and-refuted** | measured, did not help | **deprecate → remove** |
| **dark-and-unmeasured** | never actually tested | measure in this campaign |
| **dark-and-broken** | does not work if flipped | remove, or fix and re-gate |

`symbol_graph` is a known dark-but-validated case: it cleared the arm-C gate at
+2.8/+3.8pp on held-out ContextBench, and ships off because SIKE showed a
prose-bed regression under the current code-query gating. That reason belongs
in the config comment so nobody re-litigates it.

## Historical evidence is a real input, not a shortcut

We already have receipts across drastically different configurations, and the
campaign should **start from them rather than re-run them**:

- `docs/benchmarks/code-track-regression-log.md`
- the #268–#273 knob wave, which shipped byte-identical defaults
- #203 dense-weight sweep, #204 SPLADE scale curve, #255/#256 scoring-invariance
- the 2026-07-11 overnight P8/P10 receipts, blend-serving receipt (#282),
  ws2/ws3 arm-C gate

**With one rule.** The 2026-07-06 RRF default flip is a **scale break**: any
result measured under `fusion_mode="additive"` may not transfer, because
additive-scale weights can be inert under RRF. `dense_additive_weight` is the
proven instance. So each historical receipt gets triaged as:

- **still valid** — measured under current defaults, or the knob is
  fusion-independent → adopt the verdict, do not re-run
- **needs re-run** — measured pre-RRF on a fusion-sensitive knob → re-run
- **superseded** — the code path has since changed → discard

That triage is the campaign's first deliverable and it is documentation work,
not bench time. It should meaningfully shrink the run list.

## Method

Per arm: flip one knob, run the 18 needles, record per-needle rank plus the
cymatix/other split. Pre-registered decision rule, written before the run:

| outcome vs baseline | verdict |
| --- | --- |
| moves nothing at all (identical ranks) | **INERT** → deprecate |
| moves ranks, no net gain, has a cost | **NOT EARNING** → deprecate |
| net gain | **KEEP** → record the receipt in the config comment |
| net loss | **HARMFUL** → deprecate, or fix and re-gate |

Two guardrails learned the hard way this week:

1. **Check the metric can move.** Every cymatics arm scored recall@12 = 0.667
   including *off*, because the stage runs after candidate truncation and can
   only reorder within the delivered window. recall@k was structurally incapable
   of measuring it. **Before trusting a null, confirm the metric is sensitive to
   the thing being flipped.** An unmeasurable feature must be reported as
   unmeasured, never as inert.
2. **Verify the knob is actually threaded.** Three rerank combinators produced
   identical numbers, which is either a genuine collapse or a knob that is not
   wired. A null from an unset knob is the worst possible outcome — a live
   feature deleted because the harness never turned it on.

n=18 on one bed is small. Anything heading for removal on a *net-loss* or
*not-earning* verdict needs a second bed. **INERT verdicts are exempt** when the
evidence is exact (identical ranks) rather than statistical.

## Two-phase removal

**Phase 1 — deprecate.** Knob stays and keeps working. Ships with:
a `DeprecationWarning` on explicit non-default use naming the receipt; the
config comment stating the measurement and date; a CHANGELOG entry under
*Deprecated*; and the issue link. Nothing breaks; anyone depending on it gets
told, with evidence, before it goes.

**Phase 2 — remove.** No earlier than one minor release later, and only if no
counter-evidence arrives. Removes the knob, the code path, and its tests, with
the CHANGELOG entry pointing back at the receipt that justified it.

This mirrors the `blend_mode="legacy"` precedent (#282), which is
condition-gated rather than calendar-gated — the right shape, since the gate is
evidence and not time.

## Deliverables

1. **Historical triage table** — every past receipt classified valid / re-run /
   superseded. Docs only.
2. **Feature ledger** — one row per knob: default, what it does, cost when on,
   evidence, verdict, fate. The campaign's durable artifact.
3. **Ablation receipts** — one JSON per arm, dated, under
   `docs/benchmarks/data/`.
4. **Deprecation PR(s)** — warnings, config comments, CHANGELOG.
5. **Removal PR(s)** — one release later, gated on no counter-evidence.

## Sequencing

1. Historical triage (no bench time) — shrinks everything downstream.
2. Group B unmeasured knobs — cheapest, and most likely to find dead code.
3. Group A on-by-default ablations — highest value, hardest to look at honestly.
4. Ingest-side flags on a small dedicated corpus.
5. Ledger → deprecation PR → (one release) → removal PR.

## What would make this campaign wrong

Stated up front so it can be checked:

- **A bed that is not representative.** The dogfood bed is 88% one repository
  and code-heavy. A feature serving prose corpora could look inert here and be
  load-bearing elsewhere. Any *removal* verdict needs a second bed of a
  different shape; **inert** verdicts on exact-identity evidence do not.
- **The #327 defect underneath everything.** Corpus-frequency discipline is
  missing from every tier except BM25, which distorts absolute numbers. It
  applies equally to all arms so comparisons hold, but a feature could be
  masked by it. Fix #327 first, or re-run Group A after.
- **Prune bias.** A campaign framed as "find dead weight" will find dead weight.
  The decision rule is fixed in advance and KEEP is a real outcome; the ledger
  records what was kept and why, not only what was cut.

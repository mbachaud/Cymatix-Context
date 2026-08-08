# Retrieval-layer cost/impact ledger — ablation × storage × council verification

Evidence base (all committed): `benchmarks/dogfood/erb/receipts/ablation_ladder_100k.json`
(7 self-validating arms, 30 carve needles, erb_100k, GPU encoder daemon,
headroom profile), `storage_audit_{100k,blob}.json`, the fork-1 worker
sweep, and an 8-agent council pass (4 layer analysts, static→dynamic
designer, 3 adversarial verifiers running paired / order-reversed
re-probes). Binding caveats: n=30 (1 needle = 0.033 recall), one bed
(100k), headroom profile; every REMOVE/TRIM verdict below names its
confirmation test. Blob storage total 49.4GB.

## Verdict table

| Layer | Blob GB | Query cost (median) | Recall role (verified) | Classification |
|---|---|---|---|---|
| dense/ANN | 3.11 (+~3.3 RAM fp32; fp16 opt-in halves) | ~230ms own; **−7.5s p50 saved via ANN gate** | flat at 100k; gate tuning costs delivered-gold (below) | **KEEP-LOAD-BEARING** (as candidate-volume throttle) |
| fts5 | 11.39 | 322ms | lexical backbone (best arms lean on it) | **KEEP-LOAD-BEARING**; but 8.67GB is a redundant contentful shadow copy → **signal-neutral trim** (external-content FTS) |
| core | 10.86 | — | document store | KEEP |
| splade | 12.26 | ~400ms own cost | **neutral-at-best at 100k** (verifier: bidirectional near-cutoff flips; +0.10 claim WEAKENED — partly arm-order drift) | **TRIM/REMOVE-CANDIDATE** pending blob A/B — biggest single GB win, makes blob RAM-cacheable |
| pki | 8.56 | cheap | **unmeasured — no off-switch exists** | INSUFFICIENT-EVIDENCE; add a gate + arm; scorer-neutral compaction available regardless |
| tags | 2.58 | tag_exact/prefix ms-scale | unmeasured (no arm; #327 discipline A/B was null) | KEEP pending gate+arm |
| coact/harmonic | 1.44 | 185ms (3rd-largest DB signal) | post-rank pull-forward — invisible to rank-based r@12 by construction | TRIM-CANDIDATE (arm the delivered-gold metric first) |
| entity_graph | 1.37 | small | rank path already OFF in shipped config; live read gated by the WRONG flag (write-side ingestion knob leaks into read path — bug) | REMOVE-CANDIDATE + fix the flag leak |
| sema (query-side) | 0 | one remote /encode/sema RTT/query + cache-rebuild scans | **provable no-op on these beds: `genes.embedding` has 0 non-null rows on carve AND blob** | REMOVE-CANDIDATE at query time on vectorless beds (auto-gate on stored-vector count) |
| cymatics | 0 | µs (LRU-cached) | neutral; the "+33% p50 without it" claim REFUTED by code (re-ranks fixed pool, never prunes) | KEEP-CHEAP |
| synonyms | 0 | ms | shipped map nets zero-to-negative here (one entry load-bearing, two pool-bloating) | TRIM-CANDIDATE (retune the map, keep the feature) |
| classifier | 0 | µs | recall-identical; its cap shrinks splice input for free | KEEP-CHEAP |
| splice/headroom | 0 (compute) | **owner of the 16-17s p95 tail in every arm**; superlinear in candidate count (25200/n char targets defeat the fast path) | not a recall layer; drives delivered-gold arithmetic | TRIM-CANDIDATE — trim profile removes ~all of it; compression-quality A/B still unmeasured |
| cold_tier / sr / seeded_edges | ~0 | 0 | already OFF in shipped config | (already off) |

## The three headline claims, adversarially graded

1. **"Dense removal makes p50 3.5× worse" — CONFIRMED** (reproduced 4-5×
   with arm order REVERSED, killing warm-cache confounds; median-only,
   p95 unchanged). Mechanism (ring-attributed, 100% of delta): the ANN
   gate (cosine ≥0.58) cuts candidates to ≤4 on 21/30 queries → splice's
   8400-char fast path skips ModernBERT compression entirely; dense-off
   degrades to an uncapped lex pool (≈12 docs, 2100-char targets) and the
   median query pays what only the tail paid before. **Dense is a latency
   device, profile-contingent on headroom** (under trim the saving mostly
   evaporates). Corollary bug-class warning: any dense-degrade path must
   cap the non-ANN branch's candidate list before dense-skip is viable.
   **Tuning tension found:** the gate at `min_genes=1` costs end-to-end
   delivered-gold (15 vs 23 of 30 queries reach the window with gold) —
   rank-based recall@12 cannot see this; needs its own receipt metric.
2. **"Splade removal improves recall + 20% latency" — WEAKENED.** Paired,
   order-alternated re-probe: real own-cost ~400ms/query (~13%); recall
   movement is real but bidirectional near the k=12 cutoff — the +0.10
   headline included ~230-330ms/one-needle arm-order drift. Honest
   statement: splade contributes ≈nothing measurable at 100k for 400ms +
   12.26GB. Removal decision needs the blob-scale A/B (see below).
3. **"Cymatics removal costs 33% p50" — REFUTED** by code reading:
   cymatics re-scores within a fixed-size pool (`candidates[:max_genes]`
   runs identically); the delta was tail noise. Keep-cheap, but never
   defend it with that number.

## Storage math (the RAM-cacheable path for the 829k blob)

49.4GB today → external-content FTS rebuild (−8.67, signal-neutral) →
splade off pending A/B (−12.26) → pki compaction (partial) ≈ **~28GB, deep
inside 48GB RAM** — which is what un-blocks blob-scale concurrency (the
500k c=2 super-linear degradation was RAM pressure).

## Static → dynamic map (designer's priorities, verifier-reconciled)

Reusable precedents already in-tree: #164 corpus-size auto-toggle
(`resolve_splade_enabled`, ingest-only today), #255 classifier-gated
per-query override map (`rerank_combinator_by_class` — THE template),
`fts5_candidate_depth 0=auto` sentinel, `use_sr`-style per-call resolve,
dark-shipped pool hygiene (`bm25_prefilter_enabled`, `rrf_gate_top_m`).

1. **Splade per-class + corpus-size query gating** — extend the #255 map
   pattern (`splade_by_class`) + generalize #164 to the query gate
   (knowledge_store.py:2783). Ship map-empty (byte-identical), receipts
   per class. Evidence grade: cost case solid; recall case neutral (was
   overstated pre-verification).
2. **Generic `resolve_tier_enabled` corpus-size gating for all optional
   tiers** — with the explicit warning: do NOT size-gate dense off
   without first capping the lex-branch candidate list (claim 1).
3. **Pool-size-adaptive admission caps** — the bloated-pool fix (93k-294k
   scored genes/query at 829k); top-M-union cut is safe except for the
   static-miss rescue class — validate on the 141-needle set with
   static-miss needles tracked individually.
4. **`dense_pool_size` 0=auto corpus scaling** (`max(500, N/200)`) —
   land with or after #3.
5. **Classifier caps into retrieval depth (#205 constants → config)** +
   sema auto-gate on stored-vector count (turns the query-side no-op off
   by evidence, not config).

## Immediate no-regret actions surfaced

1. Fix the entity_graph read-path flag leak (write-side knob gates a read).
2. Sema query-side auto-gate (vectorless bed ⇒ skip encode RTT + scans).
3. Retune the synonyms map (one entry earns its keep, two bloat pools).
4. Splade blob-scale A/B (20-30 needles, on/off, trim profile) — the
   12.26GB decision.
5. Headroom-vs-trim answer-quality A/B at fixed candidates — the splice
   tail (p95 16-17s everywhere) is the single largest latency lever in
   the whole ledger and its quality cost is still unmeasured.
6. Add gates + ladder arms for pki and tags (the two largest unmeasured
   layers, 11.1GB combined).
7. Delivered-gold as a first-class receipt metric (the dense-gate tension
   and coact's real function are both invisible to rank-based recall).

Ladder tooling: `benchmarks/dogfood/erb/ablation_ladder.py` (arms
self-validate via signal absence), `storage_audit.py` (layer-mapped GB).
Re-run cadence: after any tier-gating change, and at blob scale before
acting on any REMOVE verdict.

## Addendum (2026-08-06): splade blob-scale A/B — verdict lands

Two order-reversed ladder passes on the index-fixed 829k probe bed
(`erb_blob_probe.db`, 56.7GB), 30 blob needles, trim profile, GPU daemon
(receipts `splade_ab_blob_run{1,2}.json`): **recall identical in all four
arm executions** (r@12 0.767, med rank 2.0 — zero flips, not noise-level
similarity), latency −2.9/−3.1s p50 (−14/−15%) and −5s p95 without splade,
reproduced across arm orders. Splade's per-query cost scales with posting
lists (~400ms at 100k → ~3s at 829k) while contributing nothing measurable
at either scale on these needle sets.

**Verdict upgrade: TRIM/REMOVE-CANDIDATE → REMOVE for ERB-class serving.**
Actions this licenses: `[ingestion] splade_enabled=false` for large-corpus
serving profiles (or the ledger's #164-generalized size gate with
`disable_above` ≈ 100-250k), and a splade-less carve of the blob
(≈37GB, RAM-cacheable) for the full 469-needle benchmark — projected
c=1 runtime ≈2.2h (469 × ~17s), before any concurrency.
Caveat retained: n=30 of 469 needles; a 100-needle confirmation pass is
cheap insurance before changing shipped defaults (not before bench use).

## Addendum 2 (2026-08-06): sema A/B with backfilled vectors; splade curve complete

**Sema, properly fed, is still null.** `scripts/backfill_sema.py` populated all
100,000 embeddings on the 100k carve (134.6 enc/s, 743s, 0 skipped) — the
first time the tier has ever had vectors to fire against on an ERB bed. Two
order-reversed A/B passes (`sema_ab_100k_run{1,2}.json`, trim profile):
r@12 0.800 and med rank 3.0 IDENTICAL in all four arm executions; baseline
pays ~40-255ms/query for the boost path. Verdict: sema query tier confirmed
null at 100k even with stored vectors — the earlier "no-op on vectorless
beds" finding was not the only problem. REMOVE-CANDIDATE stands, now
unconditional on this workload class.

**The splade curve now spans four scales and never crosses zero:**
| bed | baseline → no_splade r@12 | p50 delta |
|---|---|---|
| dogfood (~17k docs) | 0.722 → **0.833** (+2 needles) | −33% |
| erb10k | 0.333 → 0.333 (10 scoreable needles) | −20% |
| erb100k | neutral (verifier-weakened bidirectional flips) | −13% |
| erb829k | 0.767 → 0.767 (zero flips, order-reversed ×2) | −14% |

There is **no measured scale at which splade benefits** these needle sets —
twice it measurably helps to remove it. Consequence for the threshold
design: there is nothing to encode in `disable_above` for this workload
family; the evidence supports `splade_enabled=false` as the profile
default, with the #164-generalized gate reserved for workloads that
demonstrate a win (e.g. paraphrase-heavy query classes — untested here,
and the honest scope limit on this conclusion: all four beds share the
needle-in-haystack bench family).

**Splade-less 829k bed validated** (`erb_829k_nosplade.db`, 34.4GB, 829,131
genes, zero splade objects): med rank 1.0 / p50 17.5s on the first-10 blob
needles — matches the probe A/B's no_splade arm. Certified for the full
469-needle run; 13.6GB RAM headroom on the 48GB box.

## Addendum 3 (2026-08-06): full 469-needle baseline on the splade-less bed

`full469_nosplade_c1.json` (per-needle records included): **r@12 0.676
(317/469), median gold rank 2.0, p50 18.9s / p95 36.3s / mean 20.4s, one
arm = 2.65h wall** at c=1 trim under the GPU daemon on
`erb_829k_nosplade.db`. The ladder's always-on control arm doubled as
validation: shipped config on this bed re-creates empty splade tables and
scores identically (r@12 delta 0.0000, p50 delta 7ms) — serve with
`splade_enabled=false` to avoid the table re-creation. 99/469 needles
(21%) never score gold anywhere in the pool — the static-miss class,
now quantified on the full set with per-needle receipts; the remaining
misses are near-cutoff ranks. Slowest 10% of needles = 21% of wall
(tail is splice/SQL-bound per the ledger's dense analysis; run predates
the sema auto-gate so ~1 idle sema RTT/query is included).

## Addendum 4 (2026-08-06): determinism profile NO-GO; synonyms movers named

**Determinism profile: clean negative receipt.** Under
`CYMATIX_ENCODER_DETERMINISTIC=1` (deterministic kernels + TF32 off +
fixed-shape max-length padding): GPU capacity collapsed **51.4 → 1.5
bundles/s (34×)**, dogfood x4 latencies ~4× worse at every level — and the
c=8 recall deviation flag STILL fired (0.581 vs 0.5556). Two conclusions:
(1) the profile as designed is unshippable — the flags stay env-gated dark
exactly as landed; (2) the residual deviation under full determinism
proves part of the wobble is retrieval-timing-dependent (concurrent
session/pool state), not encode bits — the encode-side fix alone can
never zero it. The default-on vector LRU keeps (free repeat-text
stability; benches set `CYMATIX_ENCODER_VECTOR_CACHE=0`). Future path if
tighter stability is ever needed: canonical-v2 fixed-shape-at-sane-length
semantics WITHOUT deterministic kernels, cost-measured first.
Receipts: `encoder_daemon_capacity_gpu_det.json`, `n100_gdet_dogfood_x4.json`.

**Synonyms movers, named** (`synonyms_ab_100k_run{1,2}.json`, per-needle):
no-synonyms wins +0.033 r@12 in BOTH orders (0.800→0.833) and the two
movers reproduce the council's mechanism exactly — **erb_015** gold rank
13→11 (expansion bloat pushes it outside k=12 today) and **erb_039** gold
rank 29→gone (a synonym entry is the sole pool-entry path, but rank 29
scores nothing at k=12 — that entry pays off ONLY once pre-cap rerank
lands and converts pool presence into recall). Retune verdict: prune the
bloating entries now (+1 needle immediately), keep the erb_039 feeder as
a deliberate rerank-era investment, re-measure after rerank wiring.

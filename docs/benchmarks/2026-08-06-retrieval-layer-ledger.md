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

## Addendum 5 (2026-08-12): PKI read gate lands — action item 6, pki half

`[retrieval] pki_enabled` (default `true`, byte-identical) now gates the
single query-time PKI read (Tier 0 in `query_docs`); `false` skips the
`path_key_index` SQL entirely — no signal timing, no RRF tier, no page-cache
touch. Ladder arm `no_pki` added (`VALIDATION_SIGNAL`, `expect_absent=("pki",)`
— the `pki` key is written in a `finally` on every termed query, so the
baseline arm falsifies). The knob is threaded boot + `/admin/swap-db`
(swap-drift regression test in `tests/test_pki_gate.py`). Three scope notes:
(1) the read gate does NOT shrink the 8.56GB — that needs a write-side gate
or `/admin/compact-pki`; (2) an HTTP-driven A/B where the client supplies
`session_context` is not a pure PKI ablation (injected project path-tokens
still perturb tag/FTS tiers) — the in-process ladder is unaffected; (3) with
the gate off the `pki` telemetry series goes silent, not zero. Tags arm
(action item 6, other half) still open.

## Addendum 6 (2026-08-12): first PKI measurement — active null at 100k

Three ladder passes on erb_100k (30 carve needles, k=12, GPU daemon,
vector LRU off, `--per-query`; receipts `pki_ab_100k_run1.json` +
`run2_pkioff_base` / `run3_bothoff_base` — the reversed-base passes give
each OFF state a first-position measurement, rerank_ab.py style):

| arm | r@12 | med rank | p50 | delivered-gold |
|---|---|---|---|---|
| baseline (pki on) | 0.800 | 3.0 | 2306ms | 0.500 |
| no_pki | **0.833** (both orders) | 3.0 | 2064/2283ms | 0.500 |
| no_splade_no_pki | **0.867** (both orders) | 4.0 | 1692/1670ms | 0.500 |

**This is an ACTIVE null, not a sema-style vacuous one:** the bed's
path_key_index is fully populated (15.87M rows, 3.02M distinct pairs,
99,733/100k genes with key_values), `pki` appears in baseline tier_keys
(bonus > 0 fired), and the tier costs median 42.5ms/query (max 141ms).
PKI never rescued a needle in either order; its one k=12 effect is
displacing erb_015 (rank 13→12 on removal — the same pool-bloat mover
the synonyms A/B named). erb_021 (15→8) needs the combined splade+pki
removal. Honest limits: (1) delivered-gold is FLAT at 0.500 across all
arms and pool_delivery_gap widens 0.300→0.333→0.367 — the rank-level
wins do not reach the assembled window (the dense-gate delivery tension,
again); (2) n=30, one bed, needle-family queries — PKI's designed class
(KV/config lookup) may still pay elsewhere; (3) read gate ≠ storage win.

**Verdict update: INSUFFICIENT-EVIDENCE → TRIM/REMOVE-CANDIDATE at 100k**,
pending the B3 blob-scale pass on the collaborator's beds (#333/#334) —
the same 100k-neutral → blob-confirm path splade walked before its REMOVE.

---

## Addendum 7 (2026-08-09, ported 2026-08-19): PKI + tags armed and measured at two scales; #354 re-run; methodology results

> **Port provenance.** Authored 2026-08-09 as "Addendum 5" on the campaign
> branch `claude/ab-data-cymatics-pki-dff9c6` (`b164be4`, extended through
> `0f7f4fe`) and referenced by that number in the #354 issue thread and the
> campaign docs; renumbered 7 when ported to master because Addenda 5-6
> (2026-08-12, PKI read gate + first master-side PKI measurement) took the
> number. Chronologically this addendum PRECEDES them — the campaign branch
> was unmerged when Addendum 6 was written, so its "first PKI measurement"
> was first *on master*; the two PKI measurements are independent and agree
> in direction. Ported for #354 (evidence) and the #377 invalidation-
> enumeration hygiene item. Bracketed *[port note: ...]* marks are the only
> edits; everything else is the 2026-08-09 text verbatim.

Wave 3 of the A/B data campaign
(plan: `docs/superpowers/plans/2026-08-09-ab-data-campaign.md` *[port note:
ported to beta 2026-09-01 together with
`docs/benchmarks/2026-08-09-candidate-cascade-map.md`]*). This addendum
closes ledger actions **6** (gates + arms for pki and tags — the two largest
unmeasured layers, 11.1GB combined) and **7** (delivered-gold as a first-class
receipt metric), and re-runs **#354** with the treatment proven applied.

Receipts, all committed *[port note: on the campaign branch. Master carries
only `wave3d_cymatics_354_100k_carve.json`, ported verbatim with this
addendum; the two ladder-pass receipts (~140k lines) are archived in the
`cymatix-receipts` repository under
`mirror-branches/claude__ab-data-cymatics-pki-dff9c6/benchmarks/dogfood/erb/receipts/`
(exported 2026-09-01 from the campaign-branch head `daaa6af`, alongside the
`wave0a_*` and `delivered_gold_ab_erb10k*` receipts). The frozen SHAs below are
campaign-branch commits]*, all carrying `git_sha` + per-needle records:

| receipt | bed | needles | frozen SHA |
|---|---|---|---|
| `wave3b_ladder_100k_pass{A_forward,B_reversed}.json` | `erb_100k.db` (6.45GB) | 141 carve | `7294526` |
| `wave3d_ladder_blob_pass{A_forward,B_reversed}.json` | `erb_blob_probe.db` (56.7GB, 829k) | 30 `blob_probe30` | `46ce3f6` |
| `wave3d_cymatics_354_100k_carve.json` | `erb_100k.db` | 141 carve | `46ce3f6` + harness fix `e983618` |

Common conditions: `k=12`, GPU encoder daemon on `cuda:0`
(`CYMATIX_ENCODER_URL=http://127.0.0.1:11439`), `CYMATIX_DISABLE_LEARN=1`,
shipped `cymatix.toml`, `fusion_mode = "rrf"`, arms self-validate by signal
absence, every ladder pass run **order-reversed paired**.

**Baseline restatement — the 141-needle carve baseline is r@12 0.6879, not
0.800.** The ledger's 0.800 headline (`ablation_ladder_100k.json`,
`wave0a_timing_100k.json`) is the *first 30* carve needles. Same bed, same
config, 4.7x the needles. Do not read 0.6879 as a regression.

### PKI (#334) — REMOVE-CANDIDATE on measured evidence

| bed | arm | r@12 | mean gold rank | delivered-gold | better / worse / same | p50 |
|---|---|---|---|---|---|---|
| 100k / 141 | baseline | 0.6879 | 6.57 | 71/141 | — | 3531 / 3481 ms |
| 100k / 141 | `no_pki` | **0.6950** | 6.65 | 71/141 | 9 / 9 / 123 | 4189 / 3520 ms |
| 829k / 30 | baseline | 0.6667 | 9.41 | 11/30 | — | 21253 / 19893 ms |
| 829k / 30 | `no_pki` | 0.6667 | **9.19** | 11/30 | 8 / 3 / 19 | 20005 / 20051 ms |

(Two p50 figures = pass A forward / pass B reversed.)

Removing PKI is **neutral-to-positive at both scales**: +0.0071 r@12 at 100k
(one needle = 0.0071 there), exactly equal r@12 at 829k with mean gold rank
*improving* 9.41 to 9.19. **Delivered-gold is identical in every comparison and
there are ZERO delivery flips in either direction at either scale** — with the
#335 metric armed, PKI is not silently carrying end-to-end delivery either.
Both results replicate across both arm orders (per-needle bit-identical, see
Methodology below). Cost: **8.56GB / 18.5% of the 829k blob**
(`storage_audit_blob.json`: `path_key_index`, `idx_pki_lookup`,
`idx_pki_gene`), 31.5ms median own-cost at 100k and 148.3ms at 829k.

**Retraction — this campaign's own "narrow-trigger rescue tier" framing was
wrong.** The plan argued PKI would look neutral in aggregate while being "the
sole reason a small class of queries works at all", and that its AND-of-both-legs
predicate makes it fire rarely. The receipts kill that: PKI fires on **141/141**
needles at 100k (median 900 candidate genes/query, range 4-5357) and **30/30**
at 829k (median 2939, range 590-7343). It is a **broad, weak tier, not a narrow
decisive one**. This was the hypothesis the campaign was designed to test and the
data refuted it; the tier-firing counts are in the receipts' `tier_genes` block.

**`pki_weight` sweep: the shipped 1.0 is at or above optimal.** Three extra
arms scale `[retrieval] pki_weight` from its shipped 1.0:

| arm | r@12 | mean rank | better / worse / same vs baseline |
|---|---|---|---|
| `pki_w2` (2.0) | 0.6879 | 6.62 | 9 / **14** / 118 |
| `pki_w3` (3.0) | 0.6950 | 6.74 | 10 / **28** / 103 |
| `pki_w4` (4.0) | **0.6738** | 6.85 | 10 / **33** / 98 |

Regressions grow **monotonically** with weight (14, 28, 33 needles) and r@12
falls below baseline at w4. Both passes agree exactly. The commit-record finding
is **true**: `pki_weight = 1.0` was authored in the Stage-3 RRF PR (`e2be503`,
#55, 2026-05-10) while `fusion_mode` still defaulted to `"additive"` — so it was
inert on the shipped path until `e4889f6` (#247, 2026-07-06) flipped the default
to `"rrf"` and made it load-bearing, with no calibration in between. But its
implied conclusion — *therefore PKI is underweighted* — is **false**. Raising the
weight only hurts.

**Caveat the receipts forced (see "delivered-gold has an empty-window class"
below): the `pki_w*` arms each show delivered-gold 73 vs baseline's 71.** Both
gains are the empty-window artifact (`erb_433`, `erb_456`), not rank
improvements — gold was already at rank 1 in baseline for both. Do not cite the
weight arms as a delivered-gold win.

**#165 stands independently.** The storage case (`idx_pki_lookup` EQP-proven
unused — the live lookup rides `sqlite_autoindex_path_key_index_1`, of which it
is a strict prefix; 38% of rows sit in pairs above `PKI_NOISE_CUTOFF` and can
never contribute score; both **provably score-invariant**) is unaffected by this
recall verdict, in either direction. Compaction did not wait on it and must not
be justified by it.

### tags / promoter_index (ledger action 6) — SPLIT: does not fully replicate

| bed | arm | r@12 | mean gold rank | delivered-gold | better / worse / same | p50 |
|---|---|---|---|---|---|---|
| 100k / 141 | baseline | 0.6879 | 6.57 | 71/141 | — | 3531 / 3481 ms |
| 100k / 141 | `no_tags` | **0.7021** | **6.36** | **73/141** | 32 / 16 / 93 | 3130 / 2794 ms (**-11.3% / -19.7%**) |
| 829k / 30 | baseline | 0.6667 | 9.41 | 11/30 | — | 21253 / 19893 ms |
| 829k / 30 | `no_tags` | **0.7000** | 9.41 (**flat**) | 11/30 (**flat**) | 5 / 5 / 20 | 16276 / 16454 ms (**-23.4% / -17.3%**) |

At 100k, removing the tag tiers improves both bases. **At 829k it does not
replicate:** r@12 improves (+0.0333, one needle) and p50 drops sharply, but
**delivered-gold is flat at 11/30 with zero flips in either direction and mean
gold rank is unchanged at 9.41** — the ranks that move (5 better / 5 worse)
cancel. Report this as a **non-replication of the delivered-gold gain**, not as a
scale-confirmed win. n=30 of 469 blob needles: one needle = 0.033 r@12.

The 100k delivered-gold gain is also smaller than it looks: of the two flips,
**one is genuine** (`erb_172`, gold rank 5 to 1, delivered window unchanged at 3
docs) and **one is the empty-window artifact** (`erb_456`). Honest count:
**+1 genuine delivered-gold needle at 100k, not +2.**

Cost side, measured at 829k: `tag_exact` + `tag_prefix` own-cost **median
2352ms/query** (per-tier medians 984 + 1482ms) against PKI's 148ms — the tag
tiers are ~16x PKI's per-query cost at blob scale, and are where the -17...-23%
p50 comes from.

**SCOPE CAVEAT — this is NOT evidence for dropping the 2.58GB table.** The gate
added in `085cec6` wraps **Tiers 1+2 only** (`tag_exact` / `tag_prefix`, the
`if _tags_on:` block at `knowledge_store.py:3001`). `lex_anchor` issues its own
`promoter_index` probes at `knowledge_store.py:3521` (`SELECT COUNT(DISTINCT
gene_id) FROM promoter_index WHERE tag_value = ?`) and `:3536` (`SELECT
pi.gene_id FROM promoter_index pi JOIN genes g ...`), **outside** that block. The
receipts prove the leak rather than assume it: in the `no_tags` arm
`lex_anchor` still fires 141/141 (100k) and 30/30 (829k) at **median 12437 and
82798 genes — byte-identical to baseline's `lex_anchor` counts**. The table is
still fully read. The 2.58GB storage decision needs its own **`no_lex_anchor`
arm** — filed as **#355**.

**Treatment impurity worth recording:** `no_tags` is not a clean subtraction. In
that arm `sema_boost` acquires real candidate counts on **68/141 queries**
(median 4103 genes, up to 168ms) where baseline's `sema_boost` is a 0.001ms
no-op — removing the tag tiers shrinks the pool enough to arm the undersized-pool
sema cycle. Part of the `no_tags` movement is therefore *sema firing*, not
*tags absent*. `authority` counts also drop (median 11184 to 9658 at 100k,
70843 to 56579 at 829k).

**Open question, unresolved:** the gate wraps both tiers together, so
`tag_exact` vs `tag_prefix` attribution is unknown. Follow-up arm filed as **#356**.

### #354 cymatics — retraction CONFIRMED by direct evidence; re-run verdict null

**The harness fix was not where the previous campaign doc implied.** `ad3bbaf`
(the arm-C treatment fix) lives **only** on `claude/codebase-audit-weaknesses-96a7da`
and is an ancestor of **neither master nor this branch** (`git merge-base
--is-ancestor` returns false for both). It was ported here as **`e983618`**. Any
reader who assumed the fix was already global was wrong. *[port note: master
has since closed the underlying dead knob — #357 landed as `f5bf5e9`, making
`[cymatics] peak_width` an honoured override (unset derives 1.55), so on
master `cfg.cymatics.peak_width` now reaches the scorer and the
post-construction `m._cymatics_peak_width` workaround is superseded.]*

**Instrumented proof at the three real read sites** in
`scoring/blend.py:apply_candidate_refiners` (`query_spectrum`,
`build_weight_vector`, `cached_doc_spectrum`), recording the `peak_width` each
arm actually presents to the scorer on `erb_100k.db`:

| arm construction | peak_width the scorer observes |
|---|---|
| A, shipped | **1.55** |
| C via `cfg.cymatics.peak_width = 0.01` (the **retracted** arm) | **1.55 — identical to A** |
| C via `m._cymatics_peak_width = 0.01` (fixed) | **0.01** |

This is **positive evidence at the read site** that the retracted arm C was a
byte-identical duplicate of arm A — no longer inferred from identical output.
End-to-end corroboration: the fixed arm C now moves 4/141 needles against A
where the old one moved 0.

**Config/doc mismatch found: the shipped spread is 1.55, not the 3.0 advertised
by `cymatix.toml`.** `[cymatics] peak_width = 3.0` is parsed and never read; the
scorer derives `aggressiveness_to_peak_width([budget] splice_aggressiveness) =
max(0.5, 2.0 - 1.5 * 0.3) = 1.55`. The retracted receipts advertised 3.0 while
the scorer ran 1.55 — the same drift class as the retraction itself. Worse, the
derivation's own docstring calls the advertised value degenerate ("width >= 3.0
collapses to 0.22" dynamic range), so the TOML advertises a value the code
considers outside the useful zone. Follow-up filed as **#357**.

**Re-run, six arms x 141 carve needles, `wave3d_cymatics_354_100k_carve.json`:**

| arm | r@1 | r@5 | r@12 | MRR | median rank |
|---|---|---|---|---|---|
| A shipped (md5 bins, spread 1.55) | 0.4184 | 0.6241 | **0.6879** | 0.5011 | 1 |
| B cymatics OFF | 0.4113 | 0.6241 | **0.6879** | 0.4957 | 1 |
| C hashed bag-of-words (spread 0.01) | 0.4255 | 0.6241 | **0.6879** | **0.5028** | 1 |
| D random-bin seed=0 | 0.4113 | 0.6241 | **0.6879** | 0.4964 | 1 |
| D random-bin seed=1 | 0.4184 | 0.6241 | **0.6879** | 0.4999 | 1 |
| D random-bin seed=2 | 0.4113 | 0.6241 | **0.6879** | 0.4975 | 1 |

1. **r@12 is 0.6879 in all six arms, and r@5 is 0.6241 in all six.** The entire
   cymatics stage — on, off, reseeded bins, collapsed spread — moves recall@12
   and recall@5 by **exactly zero** on this bed. Its whole measurable effect is
   confined to sub-window ordering. The 0.6879 also **independently reproduces
   the Wave 3b ladder baseline from a completely separate harness**, which is
   worth more than the cymatics verdict itself: two independent code paths agree
   to four decimals on the same bed.
2. **The spread is measured now, and its effect is at or below the noise floor.**
   C - A = **+0.0017 MRR** against a random-bin control band of 0.4964-0.4999
   (**width 0.0035**). The point estimate says *collapsing* the spread scores
   marginally higher than the shipped 1.55 — opposite to the shipped assumption —
   but with 3 seeds it is not separable from control noise. **Do not claim that
   collapsing the spread helps.** Claim only that the spread is not earning its
   keep.
3. **What separation exists is weak and must not be overstated.** MRR ordering is
   B(off) 0.4957 < D 0.4964 / 0.4975 / 0.4999 < A 0.5011 < C 0.5028. Every arm
   that bins *anything at all* — including random bins — lands above
   cymatics-off (5/5), and A - B = +0.0054 is the only contrast exceeding the
   control band width (1.5x). But **A is not distinguishable from random
   binning**: A - D_max = +0.0012, a third of the band width, and on r@1 A ties
   the best random seed **exactly** (0.4184 vs 0.4184). The honest reading is
   that the stage behaves as a **mild reordering / tie-break**, and that this bed
   provides **no evidence its bin assignment carries semantics** — a random bin
   assignment does the same job.
4. Arm C shares 3 of its 4 moved needles with arm B (`erb_024` 3->4, `erb_101`
   2->3, `erb_166` 8->9), gaining only `erb_439` 2->1 — the mechanically expected
   result: at width 0.01 each term lands in essentially one bin and the spectrum
   degenerates toward a hashed term-overlap count.

**Bed note:** this is *not* the bed the retracted receipts used. `ablate_cymatics.py`
targeted `genomes/dogfood/genome.db` with `benchmarks/dogfood/gene_ids/gold_by_needle.json`;
both are gitignored and absent from a fresh worktree, and the only committed
dogfood gold (`erb/gold_by_needle_dogfood_EMPTY.json`) carries zero ids for all
18 needles — a 0.000 ceiling, not a score. The original 18-needle pairing is
unreproducible here, so the re-run uses the bed-complete 141-needle carve pairing
(7.8x the needles, gold actually present).

**KEEP-CHEAP verdict unchanged.** The retraction's open question — "the spread
is unmeasured, not inert" — is now resolved to: **measured, effect at or below
the noise floor, direction opposite to the shipped assumption.**

### delivered-gold (#335) has an empty-window class — read receipts accordingly

The #335 metric's first real use surfaced a behaviour rank metrics cannot see.
At 100k, **2 of 141 needles (`erb_433`, `erb_456`) deliver an EMPTY window in
the shipped baseline** — `delivered_count = 0`, `splice_n_candidates = None`
(the harness's own semantics: *never reached splice — no-match early return,
abstain, or a pre-splice exception*) — **while gold sits at rank 1 in the pool**
(`erb_433` gold at ranks 1,2,3,4; `erb_456` at 1,2). Both reproduce exactly in
the reversed pass.

Any positive perturbation of the blend flips them back on: all three `pki_w*`
arms deliver both, `no_tags` delivers `erb_456`. `no_pki` delivers neither.
**Consequence for reading any delivered-gold delta: check `delivered_count`
before crediting a gain to retrieval.** Two of the "+2" counts in this addendum
are this artifact, not ranking. This is an abstain/assembly calibration gap the
campaign surfaced and did **not** resolve — 1.4% of queries at 100k discard a
rank-1 gold document.

### Static-miss (#340): PKI cannot be its rescue mechanism — measured, not argued

At 100k, **25 of 141 carve needles never score gold anywhere in the pool**, and
that set is **bit-identical across all six arms** (baseline, `no_pki`,
`no_tags`, `pki_w2/3/4`): **0 rescued, 0 newly lost.** PKI fires on **all 25** of
them (median 861 candidate genes) and rescues none. At 829k, 3 of the 30
`blob_probe30` needles are in the 99/469 static-miss set (`erb_021`, `erb_039`,
`erb_072`); PKI fires on each (2966 / 7343 / 2360 genes) and gold still never
scores, with or without the tier. The static-miss class is a **pool-entry
failure upstream of tier weighting**; no reweighting of an already-firing tier
can address it.

### Methodology results, recorded as reusable facts

1. **Order-reversal is a measured NON-effect on this harness.** Per-needle
   `gold_ranks`, `rank_of_first_gold`, `delivered_gold`, `delivered_count` and
   `final_rank_of_first_gold` are **bit-identical between the forward and
   reversed passes for every arm** — 141/141 needles x 6 arms at 100k, 30/30 x 3
   arms at 829k, 0 differences. Only wall times move (and they move a lot:
   `no_pki` p50 +18.6% forward vs +1.1% reversed at 100k). Future ladders can
   spend the budget on needles instead of orders — **but say that the reversal is
   what established this**, and keep reversing whenever wall time is the metric.
2. **The ladder had no order-reversal mechanism before this campaign.** `--reverse`
   was added in `7294526`. Prior "order-reversed paired" claims were manual
   arm re-listing.
3. **`RemoteCodec` degrades SILENTLY to in-process CPU on encoder-daemon
   outage.** `backends/encoder_client.py` logs the per-request fallback at
   `log.debug` (`:610`, `:614`, `:798`, `:802`) with one process-lifetime
   `log.warning` from the circuit breaker — while the receipt's provenance fields
   (`dense_codec_class: "RemoteBGEM3Codec"`, `remote_dense: true`) keep asserting
   remote encoding, because they report the wrapper class, not the transport that
   served the call. An hour of Wave 3b data was discarded to this. **Launch runs
   detached and check daemon liveness before AND after**; a receipt that says
   `remote_dense: true` is not proof the daemon served it.
4. **The FIRST arm in a pass pays a cold-cache tax on per-signal timings** —
   which is precisely what order-reversal exists to expose. `splade` own-cost
   median, same bed, same arm, by position: 829k baseline **4402ms when it ran
   first** (pass A) vs **3528ms when it ran last** (pass B); 100k baseline
   **292ms first** vs **239ms last**. Any single-pass per-signal cost figure is
   inflated for whichever arm ran first. **Steady-state own-costs from the
   last-position readings: splade ~3.5s @829k / ~239ms @100k; the ledger's
   earlier "~400ms at 100k, ~3s at 829k" was in the right band but read off
   first-position runs.**
5. **The two blob 30-needle subsets are NOT the same 30 needles.** The splade
   blob A/B (Addendum 1, `splade_ab_blob_run{1,2}.json`, baseline r@12 **0.767**)
   used `needles_resolved_blob.json[:30]` — the *first* 30 of the 469. Wave 3d
   used `needles_resolved_blob_probe30.json` — the 2026-08-04 rerank GO probe's
   carve-safe 30, which shares only part of that prefix. That is why the Wave 3d
   blob baseline is **0.667**, not 0.767. **This is a different needle set, not a
   regression, and the two receipts must not be differenced.** Both are valid on
   their own set; neither is the 469-needle truth (`full469_nosplade_c1.json`,
   r@12 0.676).
6. **Two receipt-comparability breaks — older receipts do not describe the
   current tree.**
   - **entity_graph flag fix `1fd34b4`** (2026-08-06 09:42, in master) flipped
     the co-activation entity_graph sub-scan **ON to OFF** for the shipped-config
     combination. `ablation_ladder_100k.json` was committed 2026-08-05 23:15 —
     *before* the fix. **This ledger's own pre-2026-08-06 verdict table and that
     receipt describe a read path master no longer runs.**
   - **`/admin/swap-db` kwarg drift** passed 40 of the boot path's 72 kwargs.
     29 of the 32 omitted knobs have shipped value == ctor default, but **three
     do not**: `filename_anchor_enabled`, `bm25_shortlist_enabled` and
     `dense_embed_on_ingest` all have ctor default `False` and shipped `True`.
     So **every swap-based receipt** (`bench_orchestrator.py:681`,
     `bench_claude_matrix.py:1027`) was measured with `filename_anchor` — whose
     RRF weight 4.0 is the **highest tier weight in the store** — and
     `bm25_shortlist` **OFF**. Fixed in `ad81a40`, now guarded by
     `tests/test_swap_db_kwarg_drift.py`. *[port note: `ad81a40` and that test
     are campaign-branch commits; master closed the same drift independently
     on 2026-08-12 as `299e9e2` (shared `sharding.build_genome_kwargs()`
     builder for boot + swap), guarded by `tests/test_swap_db_knob_sync.py`.
     The invalidation itself is unchanged: every swap-mediated receipt
     produced before the respective fix ran with `filename_anchor`,
     `bm25_shortlist` and `dense_embed_on_ingest` silently off.]*
     **The ablation ladder is NOT affected**:
     it opens beds via `--genome`, never through swap.

### Verdict table deltas

| Layer | was | now | confirming test |
|---|---|---|---|
| pki | INSUFFICIENT-EVIDENCE (no off-switch) | **REMOVE-CANDIDATE** — 8.56GB; neutral-to-positive to remove at 100k **and** 829k, zero delivery flips, both arm orders; weight sweep says 1.0 is at/above optimal *[port note: master has since shipped `pki_enabled` default off — #370, `1a9b4aa`]* | `wave3b_ladder_100k_pass{A,B}`, `wave3d_ladder_blob_pass{A,B}` |
| tags (Tiers 1+2) | KEEP pending gate+arm | **TRIM-CANDIDATE for the tier, NOT the table** — r@12 +0.014 (100k) / +0.033 (829k), p50 -11...-23%; delivered-gold **non-replicating** (+1 genuine at 100k, flat at 829k) | same; a `no_lex_anchor` arm (**#355**) is still owed before any storage action |
| cymatics | KEEP-CHEAP ("+33% p50" REFUTED) | **KEEP-CHEAP, now measured** — r@12/r@5 identical across on/off/random/collapsed; MRR effect at or below a 0.0035 control band; **indistinguishable from random binning** | `wave3d_cymatics_354_100k_carve.json` |

**Standing scope limits.** 141 carve needles at 100k and **30 of 469** blob
needles at 829k, one config profile, one needle family (needle-in-haystack). At
blob scale one needle = 0.033 r@12, and the tags/PKI r@12 movements there are
one-needle movements. The PKI verdict is the better-supported of the two — it
replicates at both scales in the same direction with zero delivery flips; the
tags verdict is explicitly **split**, and its storage half is **not measured at
all**.

### Follow-ups filed

| issue | gap |
|---|---|
| **#355** | `no_lex_anchor` arm — the real 2.58GB `promoter_index` decision, which the Tiers-1+2 gate cannot make |
| **#356** | split the tags gate for `tag_exact` vs `tag_prefix` attribution |
| **#357** | `[cymatics] peak_width = 3.0` is dead config; the scorer runs 1.55 *[port note: since CLOSED — landed on master as `f5bf5e9`, honoured override, derived-when-unset]* |

Surfaced and **not** filed, pending a decision on where it belongs: the
delivered-gold **empty-window class** (2/141 queries at 100k discard a rank-1
gold document at `delivered_count = 0` / `splice_n_candidates = None`) — raised
on #335 rather than opened separately, since it may be the same abstain/assembly
surface as that issue's ANN-gate tuning tension.

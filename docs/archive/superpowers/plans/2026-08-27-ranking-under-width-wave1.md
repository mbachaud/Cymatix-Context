# Ranking-Under-Width — Wave 1 (config-only arms)

**Date:** 2026-08-27 · **Approved:** user, 2026-08-27 ("launch wave 1 as list") ·
**Phase parent:** `2026-08-24-v09x-retrieval-refinement-phase.md`

## Question

> Can any wider entry path pay off before the fusion layer learns to rank a wide field?

The five-arm L1.1 matrix (2026-08-27) answered "not as shipped": widening the
BM25 shortlist rescued pool entry monotonically (static misses 50→7) while net
r@12 fell monotonically (0.660→0.353, +0/−72 paired at off). Wave 1 attacks the
ranking layer itself, using only shipped config knobs.

## Code facts this wave rests on (all verified 2026-08-27)

1. **The shortlist filter is purely subtractive on the fused output.**
   Tier RRF mass accumulates over the FULL pool regardless of the shortlist
   (`knowledge_store.py:3807-3822` filters `gene_scores` after accumulation;
   the RRF branch then restricts to `eligible_ids = set(gene_scores.keys())`).
   Therefore the fused *ordering* of any two surviving docs is identical with
   the shortlist on or off. The −72 paired losses at shortlist_off are 72
   needles where fusion already ranks noise above gold TODAY and the shortlist
   deletes that noise. Corollary: any wider entry path (deeper FTS, dense,
   graph) feeds the same fusion and inherits the same verdict until ranking is
   fixed. Entry itself is ~solved: 7/235 static misses at off = existing tiers
   score gold for 97% of needles.
2. **The classifier↔ranking coupling is (suspected) issue #255.** Shipped
   default `rerank_combinator_by_class = {multi_hop: eps_band, default:
   eps_band}` (`config.py:699`); "classifier disabled ⇒ map ignored, global
   combinator (additive) used". So classifier_off silently swapped eps_band →
   additive for default/multi_hop queries. eps_band confines post-fusion rerank
   additives (sema_boost, authority, party_attr, access_rate) to a tie band;
   additive lets them shove noise past gold. Direction and magnitude fit the
   measured 0.660→0.536. The per-class assembly caps CANNOT be the r@12
   mechanism: `combine_rerank` returns the full eligible score map
   (`rerank_combinators.py:97-106` — `limit` truncates only `ranked_ids`), and
   the caps at `context_manager.py:2167` run at assembly, after
   `last_query_scores` is published.
3. **Three shipped, dormant ranking instruments sit on this exact problem:**
   - `[retrieval] rrf_gate_top_m` (#260, default 0): per-tier rank gate inside
     the Fuser — each tier contributes RRF mass only for its top-M ranks
     (`retrieval/fusion.py:94,163-171`; wired at `knowledge_store.py:2820-2824`,
     loader `config.py:1605`). At shortlist_off + gate, entry = the UNION of
     every tier's head instead of bm25's single-tier top-50 — width without
     deep-rank noise. fusion.py's own docstring cites the dense-demotes-gold
     failure (P7', 2026-07-11) as the motivating case.
   - `[retrieval] rrf_k` (default 60, TOML line ~492): fusion shape. Low k
     rewards rank-1-in-any-tier (favors anchor/tag exact hits incl. wrong
     ones); high k rewards broad multi-tier mid-rank presence (the profile
     rescued gold tends to have).
   - The #341 pre-cap cross-encoder seam (default off): `_fuse_limit =
     max(limit, rerank_depth)` at `knowledge_store.py:3849-3850` — a ranker
     that can see below the cut. Neural + latency cost ⇒ wave 2, not wave 1.
4. **Semantic is rank-invisible, not recall-invisible.** Gold enters the pool
   via synonym/tag tiers but bm25 can't see paraphrase (shortlist deletes it)
   and fusion stalls it at 13+ when the shortlist is off. Hence graphrag/neo4j
   (and any dense revisit) should be evaluated as RANKING signals, after wave 1
   picks the fusion shape. Open question for wave 2: were the dense-removal
   isolation receipts (2026-08-14) measured UNGATED (`rrf_gate_top_m = 0`)? If
   yes, "dense as a gated ranking signal on the new-granularity bed" is an
   honest revisit — needs a BGE backfill on the v09x bed first.

## Arms (all: same 235 needles, same bed, k=12, shipped defaults except named knobs)

| # | arm | config delta vs repo `cymatix.toml` | registered prediction |
|---|-----|-------------------------------------|----------------------|
| 1 | `w1_map_empty` | `rerank_combinator_by_class = {}` (classifier stays ON) | r@12 falls to ≈0.54 matching classifier_off's rank profile ⇒ the classifier↔ranking coupling is FULLY attributed to the #255 map; the L1.2 trace prerequisite is discharged. If r@12 stays ≈0.66, the coupling is elsewhere (expansion/decoder path) and the trace continues. |
| 2 | `w1_eps_band_all` | map = all five classes → eps_band | small r@12 gain vs baseline, concentrated in arithmetic/factual/procedural (the classes additive still governs at baseline). |
| 3 | `w1_gate50_off` | `bm25_shortlist_enabled = false` + `rrf_gate_top_m = 50` | **headline arm.** Static misses well under 50 (union-of-tier-heads entry), paired losses ≪ −72 (deep-rank noise mass removed). If net r@12 ≥ baseline, entry-width + ranking coexist in one shipped knob. |
| 4 | `w1_gate50_on` | `rrf_gate_top_m = 50` (shortlist stays on/50) | interaction/safety cell — near-baseline (the shortlist already deletes most of what the gate would starve); large moves here mean the gate has effects inside the shortlist head, which matters for graduating it. |
| 5 | `w1_rrf_k_20` | `rrf_k = 20` | helps iff noise wins by one-tier-head spikes; hurts if gold's strength is multi-tier-mid presence. |
| 6 | `w1_rrf_k_240` | `rrf_k = 240` | the flatter-k mirror; helps iff rescued gold is multi-tier-mid. Cormack claims insensitivity in [10,100] — 240 is deliberately outside it. |

Configs generated as full copies of repo `cymatix.toml` with asserted
single-line edits (same convention as the L1.1 set), at
`benchmarks/dogfood/erb/configs/w1_*.toml`, each load-verified through
`cymatix_context.config.load_config` before launch, including the premise
check that the BASE config really resolves the shipped
`{multi_hop: eps_band, default: eps_band}` map (arm 1 is meaningless
otherwise).

## Execution

Runner: sequential background loop (orchestrator-owned), cwd = this worktree.

```
python benchmarks/dogfood/erb/ablation_ladder.py \
  --genome F:/tmp/erb_blob_v09x.db \
  --resolved benchmarks/dogfood/erb/needles_resolved_v09x_half.json \
  --gold benchmarks/dogfood/erb/gold_by_needle_v09x.json \
  --limit 0 --k 12 --arms baseline \
  --config benchmarks/dogfood/erb/configs/w1_<arm>.toml \
  --out benchmarks/dogfood/erb/receipts/ladder_v09x_w1_<arm>_2026-08-27.json \
  --per-query
```

- Bed: `F:/tmp/erb_blob_v09x.db` (947,531 genes, identity 11a05b91…, provenance-stamped).
- ~1.5–2 h/arm under contention (the 250k scale build shares the disk for part
  of the window) ⇒ **rank metrics only, no latency claims** in any wave-1 receipt.
- Ladder safety rails: read_only bed open, `ignore_delivered`, `CYMATIX_DISABLE_LEARN=1`.
- Receipts: `benchmarks/dogfood/erb/receipts/ladder_v09x_w1_*_2026-08-27.json`
  (per-query records on) — paired-comparable with the L1.1 five-arm set
  (same needles, same bed, same k).

## Analysis plan

One paired table vs the 2026-08-27 baseline receipt: r@12, delivered_gold,
med_rank, static misses, paired +/− with needle IDs, per-question-type split
(semantic is the cell that matters). Decision rules:

- Arm 1 attributes the coupling → unlocks (or re-blocks) L1.2 surgical cap raises.
- Arm 3 ≥ baseline → graduate-candidate; confirm on full 470-needle set +
  contention-free re-run before any default flip (sequencing rule 6 of the
  phase plan: paired confirm + BASELINES row + config hash + named revert).
- Arms 5/6 are shape diagnostics — they inform wave 2's fusion design even on a null.
- Wave 2 (needs code or heavy compute, separate approval): cross-encoder
  rerank-at-depth via #341, append-below-head rescue lane, dense-gated
  revisit, graph-as-ranking-signal (graphrag/neo4j comparison vs native
  Tier 5 co-activation + harmonic links).

---

## RESULTS — wave 1 (all 6 arms completed 2026-08-27, ~1.5 h/arm, exit 0)

| arm | r@12 (map) | r@12 (final order) | delivered | med | static | paired r@12 | paired deliv |
|---|---|---|---|---|---|---|---|
| baseline (ref) | 0.660 | 0.664 | 0.549 | 4 | 50 | — | — |
| w1_map_empty | 0.536 | 0.562 | 0.379 | 7 | 50 | +7/−36 | +8/−48 |
| w1_eps_band_all | **0.672** | 0.668 | **0.574** | 3 | 50 | **+3/−0** | +8/−2 |
| w1_gate50_off | 0.353 | — | 0.323 | 26 | 7 | +0/−72 | +5/−58 |
| w1_gate50_on | 0.664 | 0.668 | 0.553 | 4 | 50 | +1/−0 | +1/−0 |
| w1_rrf_k_20 | **0.681** | **0.685** | **0.600** | **2** | 50 | **+8/−3** | **+16/−4** |
| w1_rrf_k_240 | 0.600 | 0.617 | 0.485 | 4 | 50 | +4/−18 | +7/−22 |

**1. Coupling attribution: PERFECT.** `w1_map_empty` reproduces
`classifier_off`'s gold_ranks **identically on all 235 needles** (235/235
identical lists, 235/235 identical first-gold ranks; summary r@12 0.536 both
bases match). The classifier↔ranking coupling is 100% the #255
`rerank_combinator_by_class` map — nothing else. The L1.2 trace prerequisite
is DISCHARGED. Bonus isolation: map_empty (caps ON) vs classifier_off (caps
OFF) share identical ranks, so their delivered delta (0.379 vs 0.472) is the
pure cap effect at additive ranks.

**2. rrf_k=20 is the wave winner.** +16/−4 delivered (0.549→0.600 — best
delivered ever measured on this bed), r@12 up on both bases, med rank 4→2.
k=240 loses symmetrically ⇒ sharper head wins; noise accumulates by
multi-tier-mid presence, gold wins by tier-head spikes. My registered
prediction had the sign backwards (predicted flatter-k as the gold profile).

**3. eps_band_all: clean +3/−0 / +8/−2.** Extending eps_band to
arithmetic/factual/procedural strictly helps (pending the determinism-audit
noise floor below).

**4. Per-type: constrained is the big harvest; semantic is immune to fusion
shape.** constrained r@12 0.80→0.93, delivered 0.33→0.60 under BOTH k20 and
eps_band_all — the cap-bound class rescued by better ranks alone, without
touching caps. semantic r@12 is 0.32 in every valid arm (delivered 0.21→0.27
under k20) — **no fusion reshaping touches the semantic crater**; it needs an
actual semantic ranking signal (wave 2: gated dense / #341 cross-encoder /
graph). map_empty shows eps_band was *protecting* semantic (0.32→0.16 under
additive-for-all: the rerank additives drown paraphrastic gold; the band
contains them).

**5. INVALIDATION: both gate arms ran INERT.** `[retrieval]` has a master
switch `rrf_gate_enabled` (default false, `config.py:647`;
`_rrf_gate_params` returns ungated sentinels without it,
`knowledge_store.py:1102`). The w1 configs set only `rrf_gate_top_m = 50` ⇒
the gate never engaged. Receipt-proof: `w1_gate50_off` is byte-identical to
`shortlist_off` on all 235 needles. The gate cells are UNMEASURED, not null.
(Config-generation lesson: verify the *effective* lever, not just the loaded
knob — the load-verify asserted `rrf_gate_top_m=50` and passed while the arm
was inert.)

**6. ANOMALY (open): 12-needle drift in gate50_on.** `w1_gate50_on` should
be byte-identical to baseline (inert gate) but differs on 12 needles — and
those 12 match `w1_map_empty` (additive signature) exactly 12/12. Receipts
also disagree on `classifier_metadata_queries` (228 baseline vs 227
gate50_on). Either baseline (run a day earlier) or gate50_on carries a
run-date/state dependence. **The `w1_baseline_rerun` arm decides**: identical
⇒ trace the config side; ~12-needle drift ⇒ the paired methodology has a
±12-needle noise floor and eps_band_all's +3/−0 is below it (rrf_k_20's
+16/−4 delivered survives either way).

## Wave 1b (launched 2026-08-27, sequential, ~6 h)

| arm | config | purpose |
|---|---|---|
| w1_baseline_rerun | repo `cymatix.toml` | determinism audit vs yesterday's baseline receipt (decides the anomaly + the paired noise floor) |
| w1_gate50_off_v2 | shortlist off + `rrf_gate_enabled=true` + `rrf_gate_top_m=50` | the REAL headline arm (union-of-tier-heads entry) |
| w1_gate50_on_v2 | `rrf_gate_enabled=true` + `rrf_gate_top_m=50` | gate × shortlist interaction |
| w15_k20_eps_all | `rrf_k=20` + all-classes eps_band | are the two winners additive? |

Solo-path wiring verified before launch: `sharding.py:646-648` threads all
three gate params into the shared kwargs builder; `open_read_source:710`
constructs the solo Genome with them (the rrf_k arms moving results proves
the ladder flows through this builder). Configs load-verified incl.
`rrf_gate_enabled=True`.

Rescued-gold depth note (from the inert-gate off arm, still valid for
shortlist_off): the 43/50 baseline-static needles whose gold enters the pool
at off sit at median map rank **31,714** — noise-floor depth. For those, no
rank-shape fix applies; they are wave-2 territory by construction.

---

## 2026-08-28 — Wave 1b partial: anomaly RESOLVED, drift mechanism found

The wave-1b runner was killed overnight (host sleep) after completing only
`w1_baseline_rerun`; the three remaining arms relaunched 2026-08-28 morning.
The one finished arm settled the anomaly completely:

**The 12-needle drift was warm-bed access-rate decay, and it is closed.**
- baseline vs baseline_rerun: 12 differing needles — the SAME 12, with the
  SAME values, as every post-baseline run (rerun ≡ gate50_on ≡ map_empty on
  them, 12/12 each). The original baseline receipt is the outlier.
- Mechanism: `knowledge_store.py:3733` — the access_rate rerank additive is a
  **1-hour windowed rate** (`epi.access_rate(3600.0)`; chromatin also has an
  access_rate override at :5612). The original baseline ran within ~1 h of
  bed-warming accesses; every run since saw the bed cold. Decay swept through
  DURING the L1.1 morning sweep (shortlist_200 is transitional: 2/12
  new-world), so L1.1's small paired deltas carry warm-bed noise; its large
  conclusions (monotone decline, +0/−72 at off) are unaffected.
- **Cold-bed determinism is proven**: `w1_gate50_on` (inert gate = shipped
  config) vs `w1_baseline_rerun`, 10 h apart: **235/235 identical
  gold_ranks**. No noise floor on a cold bed.
- House rule going forward: ladder arms must run on a cold bed (≥1 h since
  any manager-mediated bed access); `w1_baseline_rerun` is the paired
  REFERENCE for all wave-1/1b arms.

**Corrected paired table (vs cold-bed reference `w1_baseline_rerun`,
r@12 0.664 / delivered 0.553):**

| arm | paired r@12 | paired deliv |
|---|---|---|
| w1_rrf_k_20 | **+7/−3** | **+15/−4** |
| w1_eps_band_all | +3/−1 | +7/−2 |
| w1_rrf_k_240 | +4/−19 | +6/−22 |
| w1_map_empty | +6/−36 | +7/−48 |
| w1_gate50_on (inert) | +0/−0 | +0/−0 |

rrf_k=20's win is confirmed against the clean reference. eps_band_all is
positive but small (net +2 hits / +5 delivered).

**250k scale build COMPLETED** 2026-08-27 17:19 before the sleep event:
`F:/tmp/erb_scale/erb_250k_v09x.db`, 593,000 genes / 15.88 GB, provenance
identity `fc2257cf89eeb510`. Scale curve now has all three points at
v0.9.x defaults + bulk_load: 100k → 270,860 genes / 7.0 GiB; 250k → 593,000
/ 15.9 GB; 500k → 947,531 / 22.7 GiB.

---

## RESULTS — wave 1b complete (2026-08-28, all arms, cold-bed reference = w1_baseline_rerun)

Note on the overnight race: the killed runner turned out to have survived the
host sleep and re-raced `w1_gate50_off_v2` concurrently with the resume
runner (00:18–01:52). All three final receipts are mtime-owned by the resume
runner, no ladder processes survive, and cold-bed rank determinism makes the
overlap harmless (rank basis only).

| arm | r@12 | delivered | med | static | vs ref r@12 | vs ref deliv |
|---|---|---|---|---|---|---|
| w1_baseline_rerun (ref) | 0.664 | 0.553 | 4 | 50 | — | — |
| shortlist_off (ref) | 0.353 | 0.323 | 26 | 7 | +0/−73 | +4/−58 |
| w1_gate50_off_v2 | 0.345 | 0.298 | 22 | 7 | +0/−75 | +4/−64 |
| w1_gate50_on_v2 | 0.672 | 0.570 | 3 | 50 | +5/−3 | +10/−6 |
| w1_rrf_k_20 | 0.681 | 0.600 | 2 | 50 | +7/−3 | +15/−4 |
| w1_eps_band_all | 0.672 | 0.574 | 3 | 50 | +3/−1 | +7/−2 |
| **w15_k20_eps_all** | **0.685** | **0.621** | **2** | 50 | **+8/−3** | **+22/−6** |

**1. The winners STACK.** `rrf_k=20` + all-classes eps_band: delivered
0.553→0.621 (+6.8pp, best ever measured on this bed), combo vs k20-alone
+1/−0 r@12 and +7/−2 delivered. Per-type delivered vs ref: basic .60→.66,
conflicting_info .70→.90, constrained .40→.67, semantic .21→.27,
project_related 1.00, zero regressions ≥0.05 anywhere.

**2. Gate verdict (REAL this time): refuted as the width lever, mild win
inside the shortlist.** At shortlist-off the engaged gate is slightly
NEGATIVE (0.345 vs 0.353, paired +6/−8 vs shortlist_off; static misses
unchanged at 7, zero newly-static) — union-of-tier-heads entry preserves
pool entry but the fused head still cannot rank the wide field, so the
prereg headline hypothesis is dead at top_m=50. Inside the shortlist
(gate50_on_v2) it is a modest +5/−3 / +10/−6 — real, but strictly dominated
by k20 and the combo. Wave-2 note: a gate re-test only makes sense paired
with a stronger in-band ranker, not as a standalone entry-widener.

**3. Semantic is fusion-complete-immune: r@12 0.32 in EVERY arm** (nine
configurations spanning k∈{20,60,240}, three combinator regimes, gate
on/off, shortlist on/off). Delivered ceiling 0.27 (combo). The crater
requires a non-lexical ranking signal — wave 2 (gated dense revisit /
#341 cross-encoder / graph-as-ranking-signal) is the only remaining lane.

**4. Graduation path opened per decision rules:** full-470 confirm launched
2026-08-28 (uncontended disk): `w1c_baseline_full` + `w1c_k20_eps_all_full`,
receipts `ladder_v09x_w1c_*_2026-08-28.json`. A default flip additionally
needs BASELINES row + config hash + named revert commit (phase sequencing
rule 6) and remains a user decision.

---

## CONFIRM — full 470-needle set (2026-08-28, uncontended, receipts `ladder_v09x_w1c_*_2026-08-28.json`)

| arm | r@12 | delivered | med | paired r@12 | paired deliv |
|---|---|---|---|---|---|
| w1c_baseline_full | 0.651 | 0.555 | 3 | — | — |
| **w1c_k20_eps_all_full** | **0.681** | **0.630** | **2** | **+18/−4** | **+43/−8** |

Per-type delivered: constrained .50→.80, project_related .88→**1.00**,
conflicting_info .70→.90, basic .58→.63, intra_doc .78→.82, misc .85→.90,
semantic .28→.31, completeness flat. **Zero type regressions.** Half-subset
cross-check: the full run's 235-needle subset reproduces wave-1b (combo .621
exactly; baseline .557 vs .553 = 1 needle — in-run in-memory access-rate
state differs when 235 extra needles interleave, so paired tables must share
the same needle sequence; all of ours do).

Sequencing rule 3 satisfied (screen at half, confirm at full — the blob IS
the full-scale bed). **Graduation package remaining** (rule 6, user's call):
flip `rrf_k = 20` + all-classes eps_band map in `cymatix.toml`, BASELINES
row, config hash in receipt, named revert commit. Leaderboard framing: the
combo's delivered .630 vs the .555 shipped-defaults baseline is the internal
metric tracking public ERB Document Recall (50.7 on the July pre-flip
submission at a non-shippable 64K cap).

---

## GRADUATED — 2026-08-28 (user-approved)

User approved wave-1 completion 2026-08-28. Shipped as the rule-6 package:

- `rrf_k = 20` + all-five-classes eps_band `rerank_combinator_by_class` in
  repo `cymatix.toml` AND as code defaults (`config.py` — bare-dataclass
  users get the same world).
- BASELINES row `2026-08-27-ranking-under-width-wave1` (bed identity,
  ingest_c=6 from the bed's provenance stamp, cold-bed house rule, config
  hashes).
- `config_sha256` annotated into both w1c confirm receipts (baseline
  `64503169…` = master cymatix.toml modulo CRLF; combo `80239a0c…`).
- The flip is isolated in the commit subject-tagged `[wave1-rank-flip]`;
  a single `git revert` of it restores rrf_k=60 + the two-class map.
- Post-flip repo `cymatix.toml` verified FIELD-IDENTICAL to the measured
  combo arm config via `load_config` RetrievalConfig equality (zero diffs).

Wave 2 (non-encoder semantic ranking signal for the r@12 ~0.32 crater)
dispatched to research the same day.

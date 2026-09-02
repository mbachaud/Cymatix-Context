# ERB 947k Phase 2 — admission verdict (2026-09-01)

**Campaign question.** Lift ERB 947k `delivered_gold_rate` from .6681 (314/470) to .80 (+62 needles),
lexical-only (FTS5 + tags + synonyms, RRF `rrf_k=20`, k=12, `min_delivered_docs=12`).

**Phase-1 reframe.** 109 of the 156 misses had gold nowhere in the observed score map, so a perfect
re-ranker over the shipped pool caps at (314+47)/470 = .768. The +62 must come from admission or
indexing. Phase 2 asked the one question that decides everything downstream:

> Does `pool_absent` mean gold is genuinely unretrievable by the lexical substrate, or does it mean
> gold sits past the shipped admission bound and we truncate it?

**Verdict: the admission thesis LIVES, WITH CORRECTIONS.** Gold is admitted at depth for 66 of the
109 (gate was 30), but admission alone never improves gold's fused rank and a bare depth flip
projects net-negative on the bed. The +62 is an ordering-under-width problem now, not a reach
problem, and a previously unmeasured delivery confound has to be closed before any further
delivery-based A/B is trusted.

Bed `F:/tmp/erb_blob_v09x.db`, 947,531 genes, identity 11a05b91, ingest_c=6, tagger v1. All
pipeline runs `CYMATIX_DISABLE_LEARN=1`, `read_only=True`, `ignore_delivered=True`, config
`benchmarks/dogfood/erb/configs/w24_min_delivered_12.toml` (sha 7129e83c). Bed bytes grew 606,208
over the 2026-08-28 baseline; diagnosed benign in Phase 1 (page_size 4096 x page_count 5,998,789
matches exactly, freelist 0, no gene written after 2026-08-26 — WAL checkpoint growth).

## 1. The gate result (ARM 1, pool-absence forensics)

Tool `benchmarks/dogfood/erb/probe_pool_depth.py` (sha256 86ef6893…3bba), receipt
`receipts/pool_depth_forensics_2026-09-01.json`, capture
`receipts/pool_depth_capture_2026-09-01.sqlite` (full score map of both arms for all 216 targets;
archived in the `cymatix-receipts` repository under `captures/erb/`). Targets: the 156 misses plus 60
hit controls (fixed seed 20260901, largest-remainder stratified over the hit question_type mix).
Two arms: `shipped` (auto FTS depth 48, `bm25_shortlist_size` 50) and `deep1000` (both knobs 1000).

Pre-registered gate, stated in the tool before any result was read and byte-identical from the first
smoke receipt onward: KILL the admission thesis if fewer than 30 of the 109 pool-absent misses have
gold anywhere at depth 1000.

| population | n | gold found at depth 1000 | ≤48 | 49–200 | 201–1000 | absent at 1000 |
|---|---:|---:|---:|---:|---:|---:|
| pool-absent misses | 109 | **66** (60.6%) | 8 | 29 | 29 | 43 |
| of which semantic | 61 | 33 | 1 | 13 | 19 | 28 |
| of which non-semantic | 48 | 33 | 7 | 16 | 10 | 15 |
| near-band misses | 41 | 41 | 37 | 4 | 0 | 0 |
| seat-capped misses | 6 | 6 | 6 | 0 | 0 | 0 |
| hit controls | 60 | 60 | 60 | 0 | 0 | 0 |

Raw BM25 window for the 109 (the pipeline's own OR-joined `query_terms`, depth 20000): top-50 0,
51–1000 66, 1001–20000 38, absent at 20000 5 (erb_186, erb_189, erb_266, erb_295, erb_296, all
semantic).

Verified by three independent routes (`receipts/verify_a1_{method_basis,code-trace,recompute}_2026-09-01.json`,
plus `verify_a1_completeness_2026-09-01.json`): every published cell reproduces from the capture
by SQL comparison counting, capture ranks match checkpoints and receipt rows on 432/432 maps, the
shipped arm reproduces the 2026-08-28 ladder's `rank_of_first_gold` on 216/216, and no tie-break
contract moves a needle.

**The identity that makes the result clean.** On all 216 targets, "gold found in the deep pool" is
equivalent to "raw BM25 proxy rank ≤ 1000" with zero mismatches, and "gold in the shipped map" is
equivalent to "BM25 rank ≤ 50" with zero mismatches. The FTS lane, the shortlist post-filter and the
proxy build the byte-identical `MATCH` string from the same terms and the shortlist keeps exactly
the BM25 top-N. Admission at any shortlist size D is therefore predictable from the capture alone:
found-at-D = #{bm25_proxy_rank ≤ D}. That prediction was tested out of sample against the addendum
sweep and matched at every depth (found 8/13/15/17/20 of 25 at D = 50/200/500/1000/2000).

## 2. What admission does not buy

Receipt `receipts/pool_depth_addendum_2026-09-01.json` (sweep on 25 misses, stratified by bucket)
and the verification receipts.

- **Depth never improves gold's fused rank.** Over the 107 needles where gold is found in both arms,
  deep vs shipped rank is better 0 / same 38 / worse 69. Near-band: 40 of 41 worse (median +5, max
  +27). Seat-capped: 6 of 6 worse. Hit controls: 23 of 60 worse. The shipped map is a strict subset
  of the deep map on 216/216 needles, but 1,686 of 10,708 shared genes change score and the top-12
  set is unchanged on only 29/216 needles.
- **The "+5 rescued / −4 lost" side observation is the load-bearing delivery result, and only 2 of
  the 5 are admission rescues** (erb_302, BM25 89 → fused 2; erb_416, BM25 59 → fused 3). The other
  three (erb_045/049/144) are seat-capped needles whose delivered count flipped 6 → 12 (section 4).
  All 4 lost hits (erb_015/055/060/357) are ordering-side: gold displaced from shipped rank 11/12/2/5
  to deep 13/20/14/13 with 12 seats in both arms.
- **A rank-preserving depth-1000 flip projects net-negative bed-wide:** 0.634 [0.572, 0.661] vs
  0.668 shipped (314 − 20.9 [8.2, 50.0] + 5, Wilson interval on the 4/60 hit-loss rate). Independent
  arithmetic from the other two lenses lands at −16 and about −21+5.
- **Depth alone delivers nothing at any depth:** the addendum sweep delivered 0/1/1/1/0 of 25 at
  D = 50/200/500/1000/2000 while admission rose 8 → 20.
- **Latency is a small systematic cost, not neutral.** Paired per needle: deep slower on 152/216,
  median +264 ms (+1.4%), median ratio 1.019, p90 ratio 1.10, sign test p ≈ 0. Unpaired p50 ratio
  1.031 reproduces. Wall time scales with the size of the OR-joined term bag (p50 7.6 s at 10–19
  terms to 28 s at 60+) identically in both arms; the cost is the term bag, not the depth. The
  handoff's "depth 1000 is latency-neutral" is retracted. The 19 s serial baseline itself is
  unexplained (~11 s unattributed after fts5 2.7 s + tag_prefix 2.0 s + lex_anchor 1.55 s +
  tag_exact 1.26 s) and no concurrent-load measurement exists.

## 3. Corrections to the ARM-1 receipt as written

- The receipt's conclusion "the ~45-deep map floor is `bm25_shortlist_size=50`, NOT
  `fts5_candidate_depth=48`" is half wrong. Both knobs bind at once: shipped map sizes are exactly
  48 (15 needles), 49 (62), 50 (139). The 48 is the FTS lane's LIMIT floor, the 50 is the shortlist
  cap, and "~45" was the doc-rollup distinct-source count, not the chunk map. The practical
  conclusion stands: the two knobs must move together, `bm25_shortlist_size ≥ fts5_candidate_depth`.
  `fts5_candidate_depth` alone is provably inert for all 109 pool-absent (0 survive the shortlist);
  `bm25_shortlist_size` alone admits only what a non-FTS lane surfaces independently (unmeasured).
- `limit = max_genes * 2` is at `knowledge_store.py:2805`, not :2806. The FTS lane is the only
  consumer of `_fts_fetch_depth`; besides the two tag lanes, `lex_anchor` (:3573–3583) and
  `filename_anchor.py:167–189` are also unbounded pre-shortlist lanes.
- `deep_rank` / `shipped_rank` are pure-RRF ranks under `eps_band` (scores stay fused; only the
  emitted order can move inside a 5% band). The found/absent predicate is unaffected.
- The addendum's `meets_080_target: true` is a flag on an ORACLE ceiling (0.9085 = (314+113)/470;
  0.838 excluding the 61 semantic pool-absent from the numerator; 0.989 over BM25 depth 20000). It
  is an upper bound over a pool the current ranker demonstrably cannot order, not an attainable
  number. See `receipts/pool_depth_addendum_2026-09-01.NOTE.md`.
- "absent at 1000" is a truncation fact (all 216 deep maps are exactly 1,000 rows): 38 of the 43 have
  gold at BM25 1001–20000; only 5 are absent at 20000.
- Two hazards for future probes: the `KnowledgeStore(path)` constructor default is
  `bm25_shortlist_enabled=False` (`knowledge_store.py:563`), so a probe that builds the store
  directly measures a shortlist-free pipeline; construct via `load_config` + the manager as the
  ladder does. And the FTS lane binds `len(fts_ids)+1` parameters in one `IN (...)`
  (`knowledge_store.py:3218–3223`), so at `fts5_candidate_depth ≥ 32766`
  (`SQLITE_LIMIT_VARIABLE_NUMBER` on this build) the statement raises, the lane's `except` swallows
  it, and the whole FTS lane silently vanishes. Keep N < 32766; the harmonic tier soft-fails above
  ~16k pre-shortlist candidates independently.

## 4. New confound: the FOCUSED budget tier is not floored

`delivered_count` flipped 6 ↔ 12 on 33 of 216 needles (19 up, 14 down) when only the two admission
knobs moved. The source is the budget tier (`pipeline/tier_logic.py`: `candidates[:6]` when the
top/mean score ratio over the 24 returned candidates falls in [1.8, 3.0)), applied at
`context_manager.py:2137`, BEFORE the classifier cap at :2181. `min_delivered_docs=12` floors only
the classifier cap and the budget trimmer; `tier_logic.py` has no floor hook.

Consequences, verified from the artifacts: the shipped arm's delivered_count is exactly {6: 54,
12: 162}; on the 2026-08-28 ladder baseline 152/470 needles (32%) deliver 6 seats with the floor at
12, including 119 of the 314 hits, and `splice_n_candidates == delivered_count` on 470/470 (the cut is
pre-splice); the 6 "seat-capped" misses are FOCUSED victims; 22 hits have 12 seats with gold at rank
7–12 and a tier flip loses them. Every delivery-based A/B on this bed, including paired r-loss on
hits, is confounded by a pool-sensitive seat cut that moves on 15% of needles when a retrieval knob
changes. The v0.9.1 seat-floor graduation never reached this cut.

Fix: extend `min_delivered_docs` to the tier cuts (same knob, ~10 lines + tests), then re-run the
08-28 ladder baseline paired; expected gains on the 6 seat-capped misses and on hits at rank 7–12
under FOCUSED. At minimum, record `budget_tier` per needle in every receipt.

## 5. Structural facts corrected by the adversarial passes

- **The 61 semantic pool-absent are lexically reachable but not lexically rankable.** 56/61 share
  vocabulary with the query (gold at BM25 ≤ 20000), 33/61 are admitted at depth 1000, but 0/61 reach
  fused rank ≤ 12 at any depth measured (1 reaches ≤ 48, erb_243 at 39; median admitted rank ~227).
  "Vocabulary gap" and "unreachable" (2026-08-31 basic-miss taxonomy §3; 2026-09-01 architecture-hunt
  mission arithmetic and Q10) are refuted at the substrate level; "no lexical lever converts them"
  (handoff) survives at the delivery level. The generator's anti-lexical instruction (all 125
  semantic queries cap at content-LCN 4) explains the ranking loss, not an absence of terms. The 409
  denominator is a planning assumption with **no hard floor**: the term-overlap probe
  (`receipts/absent_term_overlap_2026-09-01.json`, pre-registered rule "true vocabulary gap iff zero
  distinct query terms occur in the gold text under the genes_fts unicode61 tokenizer") found
  **0 of 20** hard-absent needles are vocabulary gaps. The 5 absent at BM25 20000 carry 4–14 distinct
  query terms in gold (median 11), the 15 non-semantic absent at 1000 carry 4–14 (median 8), and
  removing each query's three highest-DF terms rescues none of them. Every miss on this bed is an
  outranking problem, not an indexing one.
- **Head-chunk monopoly refuted** (ARM 3, `receipts/head_chunk_lane_2026-09-01.json`; independently
  by `verify_p0p3p5_2026-09-01.json`): delivered order is chunk-index-ascending
  (`context_manager.py:3355`), so seat 1 is head-or-only with probability 1; delivered set equals the
  score top-k (overlap 1.000 over 40 replays); in score order heads win 79.49% with zero rank
  information; pre-registered K1/K2/K3 all failed. What survives is an admission effect: at-cap head
  chunks enter the pool about 1.4x more often than equally long middle chunks.
- **Two published Phase-1 magnitudes were wrong, same root cause** (the miner's `gold_first` is a
  hash-ordered arbitrary gold chunk): chunk_split 65/156 → **30/156** under the stated best-chunk
  definition (reachable 39 → 16; perfect-lane ceiling 62.9% → 25.8% of the +62); gold at-cap 42.9%
  → **54.4%** over all 386 gold chunks vs 47.3% corpus (lift 1.15), so gold is at-cap enriched and
  the anomaly is not entirely winner-side.
- **Scope/verbosity (P3) is PASS with the mechanism narrowed**, not a family kill: winners carry more
  of this query's terms (kw_cov +1.084 length-matched, p = 0.004, 59/2/31), not more vocabulary
  (raw distinct terms −0.49 length-matched); 65.3% of the raw coverage gap is length-mediated;
  verbosity is dead (avg_tf −0.013, p = 0.35; at-cap winners 1.541 vs corpus 1.691). Length
  normalization has nothing to saturate; a scope-aware scorer is not refuted.
- **Doc-union oracle**: the pre-registered statistic (138/156) is invalid because its null controls
  rescue more (constant 149, random 148.05 over 20 seeds; median candidate pool 13 docs vs a top-12
  cut). Valid readings: UNION doc rank-1 17/156 vs random 15.9; gold docs reachable from the live pool
  15/156, via a non-gold sibling 0/156. Killed.
- **Phrase/NEAR lane** killed on discrimination: reach 31/37 basic pool-absent, but fire rate 0.9126
  on standardised hits vs 0.7372 on misses, 0 of 7 strata favourable; as a re-ranking feature gold
  out-phrases the delivered best on 29/156 (ties 78, loses 49). The bootstrap CI quoted in the handoff
  ([−0.262, −0.086]) came from the Phase-1 synthesis, not from a receipt.
- ERB gold is document-complete (386 gold chunks = 386 chunks of the gold documents); any
  gold-vs-sibling metric is an empty set on this bed.

## 6. Kill ledger, Phases 1–2

| lever | verdict | deciding fact | receipt |
|---|---|---|---|
| exact-phrase + NEAR lane fused into RRF | KILL | discrimination inverted, 0/7 strata | `phrase_reach_2026-09-01.json` |
| phrase as re-ranking feature | KILL | 29/156 | `phrase_reach_addendum` (in phrase_reach) |
| doc rollup / chunk aggregation | KILL | rank-1 17 vs random 15.9; prereg statistic invalid | `doc_union_oracle_2026-09-01.json` |
| verbosity / length normalization | KILL (mechanism absent); scope scorer NOT refuted | avg_tf backwards, 65% length-mediated | `scope_verbosity_2026-09-01.json` |
| head-chunk monopoly (P5 lead) | REFUTED | presentation-order artefact; K1/K2/K3 failed | `head_chunk_lane_2026-09-01.json` |
| chunk_split 65/156, gold at-cap 42.9% | CORRECTED | 30/156; 54.4% | `verify_p0p3p5_2026-09-01.json` |
| "depth 1000 is latency-neutral" | RETRACTED | paired +264 ms median, +10% p90 | `verify_a1_recompute` |
| bare candidate-depth flip as a delivery lever | KILL (projection) | 0/38/69, 0.634 [0.572, 0.661] vs 0.668 | `pool_depth_forensics` + verify receipts |
| `fts5_candidate_depth` alone | INERT | 0/109 survive the shortlist | `pool_depth_addendum_2026-09-01.json` §B |
| head dilution as a harmonic-tier tie artefact | KILL | harmonic off reproduces deep1000 exactly, 0/107/0; 69/107 still worse than shipped | `harmonic_off_dilution_2026-09-01.json` |
| "true vocabulary gap" for the 20 hard-absent needles | NULL | 0/20; gold carries 4–14 distinct query terms in every case | `absent_term_overlap_2026-09-01.json` |

## 7. Ceiling arithmetic, honestly

| basis | ceiling | reading |
|---|---:|---|
| perfect re-ranker over the shipped 48–50 map | .768 | Phase 1; still the bound for ordering-only work at shipped admission |
| oracle over the 1000-deep pool | .909 | upper bound; the ranker moved gold the wrong way on 69/107 |
| same, excluding the 61 semantic pool-absent from the numerator | .838 | |
| oracle over BM25 depth 20000 | .989 | 5 needles are absent at that depth, but none is a vocabulary gap (term-overlap probe, section 5) |

.80 is reachable only if ordering under width improves; nothing measured so far improves it, and
the two verified delivery levers (seat floor, admission) act on different populations. The FOCUSED
tier fix (section 4) is the one lever with a plausible positive sign that has not been run.

## 8. Next arms, gated in this order

0. **Hygiene, before anything else** — this commit: probes, capture pointer, checkpoints, all
   verify receipts, this document.
1. **Floor the budget tier** (`min_delivered_docs` → `tier_logic`), re-run the 08-28 ladder baseline
   paired; re-grade ARM-1's rescues with tier flips in their own column. ~10 lines + tests; one
   470-needle ladder (~2.5 h at 19 s/query).
2. **Head-dilution attribution** — DONE the same evening, verdict **KILL** for the harmonic-tie
   hypothesis (`probe_harmonic_off_dilution.py`, receipt `harmonic_off_dilution_2026-09-01.json`,
   pre-registration receipt written before the run). With the harmonic tier removed
   (`retrieval.harmonic_weight = 0.0`; `[cymatics] harmonic_links` is not a live retrieval knob) and
   the deep knobs unchanged, gold's fused rank over the 107 both-found needles is identical to the
   deep1000 arm on every needle (0 better / 107 same / 0 worse), so 69 of 107 remain worse than
   shipped. The harmonic tier never appears in tier contributions on this bed (its query-learned graph
   is empty on bulk beds), and pre-shortlist pools measure 59k–377k documents. Head dilution is
   genuine competition from the ~950 newly admitted candidates under RRF, not a tie artefact; the
   depth lever cannot be salvaged by a head-confined tier computation. What remains is ordering
   protection: admit deep candidates below the shipped head, or a re-ranker gated on not displacing
   shipped seats.
3. **Term overlap for the 20 hard-absent needles** — DONE the same evening
   (`probe_absent_term_overlap.py`, receipt `absent_term_overlap_2026-09-01.json`, pre-registration
   receipt beside it): verdict ALL_OUTRANKED, 0 of 20 are vocabulary gaps (section 5). The ceiling
   has no hard floor from the substrate.
4. **Hit-loss curve at D = 100 and 200** on the 60 hit controls plus the 8 pool-absent needles that
   reach deep fused rank ≤ 48; admission at those depths is already known from the capture
   (D=100 → +16 misses admitted, oracle .802; D=200 → +36, .845); only the ordering side is open.
5. **Production-rate latency**: attribute the missing ~11 s of the 19 s baseline from `signal_ms`,
   then a 4-client concurrent run at shipped vs depth 1000 with p50/p95 per client and peak RSS.
6. Only then: a density-at-admission arm whose scorer must demonstrably lift BM25-rank-51–1000
   gold past ~950 new competitors without the head dilution measured here, graded paired against the
   08-28 ladder with `rloss.py` quoting `n_paired` on hits, not on the 156 misses alone.

Do not flip any candidate-depth default before 1, 4 and 5. Items 2 and 3 closed the same evening:
dilution is real competition and the substrate has no vocabulary-gap floor, so the campaign's
remaining budget belongs to ordering under width (items 4 and 6) and to the delivery floor (item 1).

## 9. Verified nulls (do not re-run)

- Production write amplification from a 1000-deep map: NULL. The learn path links only
  `expressed_ids` (`context_manager.py:2466`, ≤ 12 docs) and `learn()` persists the exchange, not the
  map.
- The BM25-identity prediction of admission at depth D: 5/5 out of sample (D = 50/200/500/1000/2000).
- `top_dominance` / `neighborhood` (`context_manager.py:3918–3941`) read the whole published map and
  shift with depth, but feed diagnostics only (:4007–4010), not the tier or know gates.
- The FOCUSED/abstain ratio is computed over the 24 returned candidates (`tier_logic.py:96–106`),
  not the map; depth reaches it only through candidate score drift, which is the flip observed.
- Bed growth of 606,208 bytes since 2026-08-28: benign (WAL checkpoint growth).

## 10. Receipt index

| what | path |
|---|---|
| ARM-1 forensics | `benchmarks/dogfood/erb/receipts/pool_depth_forensics_2026-09-01.json` |
| ARM-1 addendum (ceiling, inertness, latency curve) | `receipts/pool_depth_addendum_2026-09-01.json` + `.NOTE.md` |
| ARM-1 capture (28 MB, both arms' full maps) | `cymatix-receipts` repo `captures/erb/pool_depth_capture_2026-09-01.sqlite`; checkpoints under `receipts/pool_depth_ck_2026-09-01/` |
| ARM-1 verification, three lenses + critic | `receipts/verify_a1_{method_basis,code-trace,recompute,completeness}_2026-09-01.json` |
| ARM-3 head-chunk lane | `receipts/head_chunk_lane_2026-09-01.json`, `head_lane_live_2026-09-01.json` |
| ARM-5 verification of P0/P3/P5 | `receipts/verify_p0p3p5_2026-09-01.json` |
| term overlap, 20 hard-absent needles | `receipts/absent_term_overlap_2026-09-01.json` (+ `_prereg`), tool `probe_absent_term_overlap.py` |
| harmonic-off head-dilution attribution | `receipts/harmonic_off_dilution_2026-09-01.json` (+ `_prereg`), tool `probe_harmonic_off_dilution.py` |
| Phase-1 probes | `receipts/{phrase_reach,doc_union_oracle,scope_verbosity,atcap_confound,generator_leakage,bucket_ledger}_2026-09-01.json` |
| scorer | `benchmarks/dogfood/erb/rloss.py` (verified correct by ARM-5) |
| tools | `benchmarks/dogfood/erb/probe_*.py`, `verify_a1_*.py`, `verify_p0p3p5.py` |

Authored by Claude Fable 5.1 from the receipts above; Phase-1 probes by Claude Opus 5 agents; ARM-1
verification by three Fable 5.1 lenses and a completeness critic (workflow `wf_658a4bc4-5c7`).

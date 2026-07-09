# Scoring-Invariance Audit — retrieval scoring path

**Status:** Evidence/audit record for the scoring-invariance lane. Produced
2026-07-08. Scope is documentation plus pinned characterization tests ONLY —
zero edits to any scoring code path. Every graduation item in §4 is
**bench-gated** per council rules (no scoring change ships on inspection
alone). Companion tests land in this PR:
`tests/test_fusion_invariance.py` (pure `Fuser` invariance) and
`tests/test_retrieval_invariance.py` (store-level invariance + the two live
defects). Two live defects were found (§3); both are pinned by tests, neither
is fixed here.

---

## 1. The invariance principle

A ranking decision in the retrieval path must survive monotone rescaling of
score magnitudes. Corpus size, per-tier score scales, and fusion-mode scale
changes are all magnitude changes that carry no ranking information: if
every candidate's score is passed through the same strictly-increasing map,
the delivered ordering, the tier cut (TIGHT/FOCUSED/BROAD), and the abstain
verdict should not change. Absolute score thresholds are legitimate only when
they are **explicit, calibrated, and re-fittable with provenance** — the
`[know]` pattern (§2a): named config fields, a calibration script that stamps
`calibrated_at`/`calibrated_on_n`, and a staleness check. Everything else is
a hidden dependency on whatever scale the accumulator happened to have when
the constant was hand-tuned. This is not a stylistic preference; it has
already cost real recall three times:

- Switching fusion from additive to RRF — a pure change of score scale at
  fixed evidence — moved gold_delivered **+12pp** on the xl bed
  (`docs/benchmarks/2026-07-06-rrf-default-rebaseline.md`). Magnitude
  conventions were load-bearing.
- The additive-calibrated absolute abstain floors had to be force-bypassed
  under RRF (`skip_absolute_floors`, `helix_context/pipeline/tier_logic.py:113`)
  because scores on the RRF scale (O(0.05–0.5)) never clear floors tuned for
  the additive scale (O(3–45)). The gate now runs ratio-only under the
  shipped default — the floors are dead code on the default path.
- The flat fts5-depth curve (`docs/benchmarks/2026-07-06-sike-run2-fts-depth-fusion.md`)
  showed the xl ceiling was rank squeeze, not candidate starvation — the
  decision-relevant quantity was rank structure, not how much raw score mass
  entered the pool.

## 2. Four-class inventory

Every scale-bearing constant on the scoring path, classified. All file:line
references verified against this branch (`audit/scoring-invariance`, fresh
off master) on 2026-07-08.

### 2a. Calibrated with provenance (the good pattern — 2 entries)

| Constant / mechanism | file:line | Value | Why this class |
| --- | --- | --- | --- |
| `[know]` confidence logistic: `sigmoid(β0 + β1·tanh(top_score/s_ref) + β2·tanh(score_gap/g_ref) + β3·agree + β4·coord_conf + β5·freshness)` | `helix_context/scoring/know_calibration.py:264-280` | defaults `betas=(-2.0, 2.0, 1.5, 0.7, 1.8, 1.5)`, `s_ref=1.0`, `g_ref=0.5`, `emit_floor=0.55` (`know_calibration.py:64-72`) | Absolute scale, but explicit config fields with provenance stamps `calibrated_at`/`calibrated_on_n` (`know_calibration.py:102-103`) written by `scripts/calibrate_know_confidence.py:406-407`, plus staleness tracking (`stale_after_days=30`, `know_calibration.py:80`). Re-fittable by re-running the script. |
| ANN margin-over-random threshold | `helix_context/knowledge_store.py:1129-1132` (reads `genome_calibration` key `ann_threshold`) | sigma multiplier `ann_threshold_sigma_multiplier=3.0` (`helix_context/config.py:423`) | Threshold is measured against the corpus's own random-pair distribution and stamped into the `genome_calibration` table — corpus-relative, persisted, recomputable. |

**Caveat on the first entry:** `s_ref`/`g_ref` were fit on additive-scale
scores and have not been re-fit for RRF
(`docs/benchmarks/2026-07-06-rrf-default-rebaseline.md:94-96`). The logistic's
inputs `top_score`/`score_gap` are raw post-fusion scores
(`helix_context/context_packet.py:635-640`,
`helix_context/server/helpers.py:273-274`), so under the shipped RRF default
`tanh(top_score/1.0)` operates far from the region it was calibrated on. The
pattern is right; the fit is stale. See §4.7.

### 2b. Proportional / ratio — scale-free by construction

| Constant / mechanism | file:line | Value | Why this class |
| --- | --- | --- | --- |
| RRF abstain norm ratio `(top − min)/(mean − min)` | `helix_context/pipeline/tier_logic.py:146-152` (formula at `:150`; invariance argument in comment `:123-129`) | thresholds `ABSTAIN_RATIO_THRESHOLD=1.8` / `..._RRF_NORM=1.5` (`tier_logic.py:144-145`, code constants) | Baseline-subtracted ratio; multiplying every score by a constant leaves it unchanged. |
| Candidate hard floor | `helix_context/pipeline/tier_logic.py:93` | `top_score * 0.15` | Fraction of top score — rescales with the scores. |
| Tier ratio cuts | `helix_context/pipeline/tier_logic.py:227-228, 237-238` | `ratio >= 3.0` TIGHT, `>= 1.8` FOCUSED | top/mean ratios; note both carry an absolute-floor conjunct that is class (d) and RRF-bypassed. |
| FTS5 fetch depth auto | `helix_context/knowledge_store.py:1961, 1968`; `helix_context/config.py:392` | `limit = max_genes*2`; `_fts_fetch_depth = fts5_candidate_depth or (limit*2)` → auto = `max_genes*4` | Dimensionless doc-count multiples of `max_genes`. |
| Budget-proportional splice target | `helix_context/context_manager.py:374-397` | `int(expression_tokens·4·0.9) // n_candidates`, floored at legacy 1000 | Distributes a declared budget across candidates. |
| Foveated power-law caps | `helix_context/context_manager.py:361` | `max(c_min, c_max·(i+1)^-α)` | Positional decay, no score units. |
| Budget-zone boundaries | `helix_context/budget_zone.py:37-43` | 0.25 / 0.40 / 0.60 / 0.80 of `window_tokens` | Fractions of the declared window. |
| Classifier per-class caps | `helix_context/retrieval/query_classifier.py:167, 179, 193, 204` | `assembly_max_genes_cap` 2/5/6/8 | Dimensionless doc counts (code constants pending #205). |
| Seeded-edge promotion/prune | `helix_context/retrieval/seeded_edges.py:59-68` | source multipliers 0.3/0.7/1.0, `CO_PROMOTE_MIN_COUNT=3`, `CO_PROMOTE_MIN_RATIO=0.4`, `PRUNE_FLOOR=0.05`, `SEEDING_CAP=200` | Counts, Laplace ratios, and a floor on a weight that is itself normalized to [0,1] by the multipliers. |

### 2c. Legitimately dimensionful (time / tokens)

| Constant / mechanism | file:line | Value | Why this class |
| --- | --- | --- | --- |
| Freshness half-lives | `helix_context/context_packet.py:19-23`; decay `exp(-age/half_life)` at `:123-128` | stable 7d / medium 12h / hot 15min, keyed by volatility class | Time is a real physical unit here; the decay maps it onto [0,1]. |
| Freshness stat-cache TTL | `helix_context/retrieval/freshness.py:42` | `DEFAULT_CACHE_TTL_S=60.0` | Wall-clock TTL; the verdict itself is categorical (`mtime <= last_verified`), not a score multiplier. |
| Splice char/token constants | `helix_context/context_manager.py:369-371` | `_SPLICE_CHARS_PER_TOKEN=4.0`, `_SPLICE_BUDGET_SAFETY=0.9`, `_SPLICE_LEGACY_FLOOR=1000` | Chars-per-token is an empirical unit conversion, not a ranking scale. |
| Tier token-budget estimates | `helix_context/pipeline/tier_logic.py:225, 236, 246` | 15000 / 6000 / 9000 | Token estimates attached to tier verdicts; downstream budget arithmetic, not ranking. |
| Calibration staleness window | `helix_context/scoring/know_calibration.py:80` | `stale_after_days=30` | Days; drives a warning flag only. |

### 2d. Absolute score magnitudes, uncalibrated/unstamped (the dangerous class)

These constants encode a specific score scale — almost always the legacy
additive scale — with no provenance stamp, no re-fit script, and in several
cases behavior that silently changes meaning when the scale changes.

| Constant / mechanism | file:line | Value | Why this class |
| --- | --- | --- | --- |
| Global abstain/TIGHT/FOCUSED floors | `helix_context/context_manager.py:1235-1237` (`_GLOBAL_TIGHT_FLOOR` etc.), consumed at `:1253-1256`; config mirror `AbstainClassFloors` `helix_context/config.py:513-531` | 5.0 / 2.5 / 2.5 | Docstrings claim p85-MISS/p25-HIT/p60-HIT origin from `located_n1000.json` (spec `docs/specs/2026-05-08-stage-4-threshold-calibration.md`) but carry NO `calibrated_at` stamp and no re-fit path. Bypassed entirely under the shipped RRF default via `skip_absolute_floors` (`tier_logic.py:113`; gate `:177-181`; telemetry label `ratio_only` vs `floor_and_ratio` `:188-191`). Default `[abstain].mode="global"` (`config.py:545`) uses the hard-coded constants. |
| Post-fusion `rerank_additive` bonuses (DEFECT-1 carrier) | authority +2.0/+1.5/+0.5 `helix_context/knowledge_store.py:1562, 1570, 1580` (mirrored into `rerank_additive` at `:1590`; call site `:2660-2664`); party +0.5 `:2789-2792`; access-rate ≤0.25 `:2815-2820`; sema_boost `:2432-2436` | see left | Fixed additive-scale magnitudes added AFTER fusion in both modes. Correctly sized as nudges on the additive scale (O(3–45)); ~40x the signal on the RRF scale. See §3, DEFECT-1. |
| sema_boost gate + damping | gate `top_score < 20.0` `helix_context/knowledge_store.py:2402`; `sim > 0.3` `:2428`; `boost_scale = max(0.5, 1.0 − top_score/40.0)` `:2429` | 20.0 / 40.0 / 0.3 | 40.0 and 20.0 hard-wire this tier to the additive scale. Under RRF-scale scores (≤ ~0.5) the gate is always open and `boost_scale ≈ 1.0` — the damping designed to shrink the boost as confidence rises never engages. |
| PKI bonus | `helix_context/storage/indexes.py:117-119`; total cap `helix_context/knowledge_store.py:2134` | `PKI_BASE=10.0`, `PKI_FLOOR=2.0`, `PKI_NOISE_CUTOFF=200`; cap 12.0 hard-coded | Bonus magnitude and cap tuned to additive tier balance; cap comment ("roughly 3x the strongest single signal") names the additive scale implicitly. |
| FTS5 additive cap | `helix_context/knowledge_store.py:2329` | `min(-bm25, 2.0·fts5_weight)` | The 2.0 multiplier hand-balances raw BM25 against tag scores 3–9 — additive-scale bookkeeping. (Raw scores also feed the Fuser at `:2338`, where rank absorbs the scale.) |
| SPLADE clamp | `helix_context/knowledge_store.py:2371` | `min(score, 20.0)·splade_weight/20.0` | 20.0 is a hand-picked raw-SPLADE normalizer chosen to land on the additive tier scale. |
| Harmonic Tier-5 flat boost | `helix_context/knowledge_store.py:2693-2695` | `+harmonic_weight` per link, cap `3.0·harmonic_weight` | Flat per-edge magnitude on the additive accumulator (also fed to the Fuser at `:2702`, where it is safe). |
| Entity-graph boost | `helix_context/knowledge_store.py:2758-2762` | `min(1.0·entity_graph_weight, 4.0·entity_graph_weight)` per row | Same shape as harmonic; the additive branch carries the raw magnitude. |
| Blend-layer post-fusion absolutes | cymatics `flux·0.5` `helix_context/scoring/blend.py:67, 70`; harmonic_bin overtone `·1.5` `helix_context/scoring/ray_trace.py:485` (call site `blend.py:104-111` hard-codes `k_rays=100`, `max_bounces=2`); TCM `BONUS_WEIGHT=0.3` `helix_context/scoring/tcm.py:49` (bound at `blend.py:127`, applied `tcm.py:317`) | 0.5 / 1.5 / 0.3 | These mutate `last_query_scores` AFTER fusion, on whatever scale the map carries — 0.5 is a mild nudge on additive scores, an overwrite on RRF scores. They also contaminate the `[know]` inputs, which read the mutated map. |
| ray_trace internals | `helix_context/scoring/ray_trace.py:62-66`; normalization `:369` | `ABSORPTION_THRESHOLD=0.01`, `DEFAULT_K_RAYS=200`, `DEFAULT_MAX_BOUNCES=3`, `DEFAULT_DECAY=0.7`, `BOOST_CAP=2.0` | Not config-exposed; `BOOST_CAP` is an absolute output magnitude on an unspecified scale (output IS max-normalized to `[0, BOOST_CAP]`, so the cap is the scale). |
| SR occupancy boost | `helix_context/retrieval/sr.py:38-42`; `min(weight·occupancy, cap)` `:171` | `gamma=0.85` / `weight=1.5` / `cap=3.0` config-exposed; `k_steps=4`, `FRONTIER_CAP=2000` code-only | Weight/cap are absolute additive-scale magnitudes; occupancy itself is a normalized mass. |
| lexical_rescue path bonuses | `helix_context/retrieval/lexical_rescue.py:84, 87, 92, 102, 104` | +1.5 / +1.0 / +0.4 / +2.0 / −0.75 | Hand-tuned absolute bonuses on the rescue path's own accumulator. |
| Cymatics amplitudes + floors | amplitudes `helix_context/scoring/cymatics.py:306-308`; floors `:510` (score > 0.05), `:514-518` (pad 0.1 when < 50% scored), `:549` (`splice_aggressiveness·0.7`), `:626` (link weight > 0.1) | amplify 1.5 / synonym 1.2 / baseline 0.8; floors 0.05 / 0.1 / 0.7 / 0.1 | Absolute cutoffs on resonance-score magnitudes with no calibration record. |
| ANN / dense admission | `ann_similarity_threshold=0.58` `helix_context/config.py:413` (constructor fallback `helix_context/knowledge_store.py:492`); `dense_pool_floor_genes=8` `config.py:440`; `dense_additive_min_cosine=0.15` `config.py:499`; `cold_tier_min_cosine=0.15` `config.py:311` | see left | Cosine is at least a bounded unit, and 0.58 has a measured rationale in its docstring — but no stamp, no re-fit script, and the off-branch diagnosis (147a628) found it doc-doc-miscalibrated for the query-doc case. Class (d) until stamped. |

**The fusion path itself** (`helix_context/retrieval/fusion.py`) is the one
component that gets this right by construction: `contribution =
weight/(k + rank)` (`fusion.py:126`, `DEFAULT_RRF_K=60` `:42`), deterministic
`(-score, gene_id)` tie-break (`:117-120`), pure module with no store
coupling. Its docstring **claims** scale-invariance (`fusion.py:8-11`) and
tie-determinism (`:21-23`); before this PR neither claim had a test. The
companion `tests/test_fusion_invariance.py` pins both. `query_docs` builds
the Fuser via local import (`helix_context/knowledge_store.py:2012-2013`) and
feeds it from 12 tier call sites (`:2159, 2201, 2237, 2275, 2338, 2378, 2491,
2544, 2653, 2702, 2732, 2767`); the RRF finalization path is `:2918-2954`,
the legacy additive path `:2955-2957` (the `gene_scores` accumulator IS the
ranking there).

## 3. The two live defects

### DEFECT-1 — post-fusion additive bonuses dominate the RRF ranking (shipped default)

Under `fusion_mode="rrf"` (the config default since 2026-07-06), the final
score is:

```python
# helix_context/knowledge_store.py:2928-2931
final_scores[gid] = (
    fused_scores.get(gid, 0.0)
    + rerank_additive.get(gid, 0.0)
)
```

The fused side is on the RRF scale: one tier's maximum contribution is
`weight/(k+1)` with `k=60`, i.e. ≈0.066 even for the heaviest default tier
(`filename_anchor_weight=4.0`, `config.py:374`; most tiers weigh 1.0–3.5 →
0.016–0.057). A document ranked first in every one of the 12 tiers lands
around 0.3–0.5 total. The `rerank_additive` side carries the legacy
additive-scale bonuses unchanged: authority +2.0/+1.5/+0.5
(`knowledge_store.py:1562, 1570, 1580` → `rerank_additive` at `:1590`), party
attribution +0.5 (`:2789-2792`), access-rate ≤0.25 (`:2815-2820`), and
sema_boost up to ~2.0 (`:2432-2436`, damping inert per §2d).

**Blast radius:** one authority hit is ~30–40x a single top-rank tier
contribution and 4–6x the *entire* best-achievable fused score. Whenever any
of these bonuses fires, the fused ranking — the thing the +12pp rebaseline
was measured on — is reduced to a tie-breaker among documents with equal
bonus totals. The design comment at `knowledge_store.py:2008-2011` states the
intent: authority is a different question from cross-tier agreement and
"survives unchanged." The intent is defensible; the magnitudes are not — they
were sized as nudges against additive scores of O(3–45) and were never
rescaled when the base signal shrank by two orders of magnitude. The
mode-asymmetric result: the same bonus that shifts an additive ranking by a
few percent overwrites an RRF ranking outright.

**Pinned by:**
`tests/test_retrieval_invariance.py::test_defect1_authority_bonus_dominates_rrf_ordering`
(a single +2.0 authority bonus on a fused-last candidate outranks a candidate
that wins every tier), and
`tests/test_retrieval_invariance.py::test_sema_boost_damping_inert_under_rrf`
(the 40.0/20.0 damping never engages on RRF-scale scores).

### DEFECT-2 — layer defaults disagree on fusion physics

`KnowledgeStore.__init__` defaults `fusion_mode="additive"`
(`helix_context/knowledge_store.py:519`) while `RetrievalConfig` defaults
`fusion_mode="rrf"` (`helix_context/config.py:461`). A server built through
the config loader runs RRF; a `Genome`/`KnowledgeStore` constructed directly
— without an explicit `fusion_mode=` — silently runs additive.

**Blast radius:** every test that constructs a store directly exercises
additive physics unless it opts in — including suites written to validate
"the shipped default." This is not limited to ranking order: the abstain
gate switches semantics with the mode (`tier_logic.py:113` —
`floor_and_ratio` under additive, `ratio_only` under RRF), so
directly-constructed-store tests of abstain behavior validate a gate
configuration that production never runs. The golden additive-plumbing suite
is the mirror image of the same trap: it passes *because* of the stale
constructor default, and would silently start testing RRF if someone
"harmonized" the default without auditing call sites. Both directions of the
trap argue for the same fix: the disagreement must be eliminated once, on
purpose, with the test intent made explicit — not discovered per-test.

**Pinned by:**
`tests/test_retrieval_invariance.py::test_defect2_layer_default_disagreement`
(asserts the two defaults currently disagree — the test fails loudly the day
either default moves, forcing the reconciliation to be deliberate), and
`tests/test_retrieval_invariance.py::test_abstain_floors_bypassed_under_rrf`
(the gate-semantics half of the blast radius).

## 4. Graduation paths

One item per class-(d) family. Every item is **bench-gated**: a fix PR must
carry a before/after run on the standard beds (the 50-needle matrix at
minimum; xl for anything touching fusion) and may not ship on code review
alone. Prior art for the diagnosis-first discipline: the two off-branch #250
research docs, `d6087de` ("research: explain the RRF gold-block deficit on
xl_clean (tier-breadth bias)") and `147a628` ("research: ANN dense-admission
threshold — real bug, but not worth shipping the fix"), both on
`research/ann-threshold-recalibration` — each found a real defect and
measured whether fixing it paid before touching the default.

1. **`rerank_additive` scale reconciliation (fixes DEFECT-1)** — (M),
   bench-gated. Three candidate shapes, in descending preference: (a) feed
   authority/party/access-rate/sema as Fuser tiers via `add_tier` so rank
   absorbs their scale like every other signal; (b) convert to bounded
   multipliers of the fused score (`fused · (1 + ε·bonus_norm)` with
   `bonus_norm ∈ [0,1]`), preserving the "different question" intent without
   letting magnitude own the ranking; (c) minimal: renormalize the bonus
   constants onto the RRF scale (divide by ~`k`) and stamp the chosen scale.
   The xl re-run must confirm the RRF win survives; `d6087de`'s tier-breadth
   analysis is the baseline to compare against.
2. **Abstain floors → stamped per-classifier calibration** — (M),
   bench-gated. Activate the already-built `[abstain].mode="per_classifier"`
   path (`config.py:534-546`) with floors re-fit per score scale from
   `located_n1000.json`, and add `calibrated_at`/`calibrated_on_n` fields to
   `AbstainClassFloors` mirroring the `[know]` pattern. Under RRF this
   *replaces* the blanket bypass at `tier_logic.py:113` with floors that are
   actually valid on the fused scale — the ratio-only gate stays as the
   invariant backstop.
3. **sema_boost 40.0/20.0 retirement** — (S), bench-gated. Replace the
   `top_score < 20.0` gate and `1 − top_score/40.0` damping with
   scale-relative forms (gate on the candidate set's own ratio structure,
   e.g. norm-ratio below the abstain threshold; damping as a function of
   `top/mean`). Rides with item 1 if the bonus moves into the Fuser.
4. **Harmonic / entity flat boosts → rank forms** — (S), bench-gated. Both
   tiers already feed the Fuser (`knowledge_store.py:2702, :2767`), so under
   RRF the fix is to stop double-carrying the flat additive magnitude in
   `gene_scores`; under additive the flat form dies with the legacy path
   (scheduled removal v(N+2)).
5. **Blend-layer absolutes (cymatics 0.5, harmonic_bin 1.5, TCM 0.3)** —
   (M), bench-gated. These are post-fusion mutations of `last_query_scores`
   on an unspecified scale. Convert to a rank-blend (a second, small Fuser
   over refiner rankings) or scale-relative multipliers. This item also
   repairs a `[know]` input-integrity problem: the logistic's
   `top_score`/`score_gap` are read from the mutated map
   (`context_packet.py:635-640`), so today's blend bonuses shift confidence
   calibration as a side effect.
6. **ray_trace / SR constant exposure** — (S), bench-gated (weak gate: a
   no-regression run suffices since defaults don't move). Lift
   `ABSORPTION_THRESHOLD`/`DEFAULT_K_RAYS`/`DEFAULT_MAX_BOUNCES`/
   `DEFAULT_DECAY`/`BOOST_CAP` (`ray_trace.py:62-66`) and
   `k_steps`/`FRONTIER_CAP` (`sr.py:39, 42`) into `[retrieval]` config;
   re-express `BOOST_CAP` and the SR `cap` relative to the active fusion
   scale rather than as bare additive magnitudes.
7. **`[know]` s_ref/g_ref RRF re-fit** — (S), bench-gated. Already fully
   instrumented — `scripts/calibrate_know_confidence.py` fits and stamps
   provenance (`:406-407`); the gap is only that the last fit predates the
   RRF default (`docs/benchmarks/2026-07-06-rrf-default-rebaseline.md:94-96`).
   Needs the re-run against rrf-scale score logs plus an ECE check against
   the 2026-07-07 calibration baseline. Sequence AFTER items 1 and 5, since
   both change the score distribution the logistic reads.
8. **PKI / FTS5-cap / SPLADE-clamp / lexical_rescue absolutes** — (S) each,
   bench-gated, lowest priority. Under RRF these tiers' raw scores enter the
   Fuser where rank absorbs the scale; the absolute caps
   (12.0 at `knowledge_store.py:2134`, `2.0·w` at `:2329`, 20.0 at `:2371`)
   bind only on the legacy additive branch. Recommended disposition: do not
   recalibrate — retire them together with the additive path in v(N+2), and
   until then treat them as frozen legacy-mode behavior pinned by
   `test_additive_mode_not_rescale_invariant`. lexical_rescue's internal
   bonuses (`lexical_rescue.py:84-104`) score an isolated accumulator and
   graduate independently (rank-form its output before it re-enters the main
   path).

## 5. What the tests pin

| Doc finding | Canonical test |
| --- | --- |
| Fuser order is invariant under per-tier affine rescale (`fusion.py:8-11` docstring claim, first half) | `tests/test_fusion_invariance.py::test_order_invariant_under_affine_rescale_per_tier` |
| Fuser order is invariant under exponential (nonlinear monotone) per-tier rescale | `tests/test_fusion_invariance.py::test_order_invariant_under_exp_rescale_per_tier` |
| Fuser order is invariant under arbitrary strictly-increasing per-tier maps | `tests/test_fusion_invariance.py::test_order_invariant_under_arbitrary_monotone_maps` |
| Uniform scaling of all tier weights preserves order (weights are relative, §2b) | `tests/test_fusion_invariance.py::test_order_invariant_under_uniform_weight_scaling` |
| `k` (`DEFAULT_RRF_K=60`, `fusion.py:42`) moves fused magnitudes, not the pinned ordering — magnitudes are not decision-bearing | `tests/test_fusion_invariance.py::test_k_changes_magnitudes_not_order` |
| `(-score, gene_id)` tie-break (`fusion.py:117-120`) is deterministic and rescale-stable (`fusion.py:21-23` docstring claim) | `tests/test_fusion_invariance.py::test_tie_break_deterministic_under_rescale` |
| `rank_by_score` helper (`fusion.py:180`) assigns rescale-stable ranks | `tests/test_fusion_invariance.py::test_rank_by_score_stable_under_rescale` |
| End-to-end `query_docs` ordering under RRF survives monotone rescale of tier inputs | `tests/test_retrieval_invariance.py::test_query_docs_order_invariant_under_monotone_tier_rescale` |
| Corpus growth (a magnitude change in candidate-pool statistics) preserves the relative order of a fixed document pair | `tests/test_retrieval_invariance.py::test_corpus_scale_preserves_relative_order` |
| DEFECT-1: one +2.0 authority bonus outranks any achievable fused score under RRF (`knowledge_store.py:2928-2931`) | `tests/test_retrieval_invariance.py::test_defect1_authority_bonus_dominates_rrf_ordering` |
| DEFECT-2: `KnowledgeStore.__init__` default `"additive"` (`knowledge_store.py:519`) disagrees with `RetrievalConfig` default `"rrf"` (`config.py:461`) | `tests/test_retrieval_invariance.py::test_defect2_layer_default_disagreement` |
| The legacy additive accumulator (`knowledge_store.py:2955-2957`) is NOT rescale-invariant — expected, pinned so the v(N+2) removal is informed | `tests/test_retrieval_invariance.py::test_additive_mode_not_rescale_invariant` |
| Absolute abstain floors are bypassed under RRF; the gate runs ratio-only (`tier_logic.py:113, 177-191`) | `tests/test_retrieval_invariance.py::test_abstain_floors_bypassed_under_rrf` |
| sema_boost's 40.0/20.0 damping never engages on RRF-scale scores (`knowledge_store.py:2402, 2429`) | `tests/test_retrieval_invariance.py::test_sema_boost_damping_inert_under_rrf` |

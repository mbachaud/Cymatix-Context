# Overnight rig results — 2026-07-10 → 07-11

Branch: `bench/overnight-2026-07-10` (worktree `.claude/worktrees/overnight`, master @ 1918c0e).
Preflight: config defaults verified `blend_mode=legacy rerank_combinator=additive shard_fetch_multiplier=2.0`; all beds present (xl 2.66G, xl_clean 2.66G, 10k 828M, 50k 4.24G, blob 47G); 410 GB free on F:.

## Job log


### P1a — ab_blend_off — DONE (mechanism confirmed: off-cell exact-inversion floor 32/55 -> 0/0 on xl/xl_clean; small additive-cell delivery cost, <=0.04)

| bed | combinator | source | n | gold_delivered_text | gold_delivered_id | answerability (content_has_answer) | exact_inversions (total) |
|---|---|---|---|---|---|---|---|
| xl | additive | legacy | 50 | 0.44 | 0.62 | 0.84 | 677 |
| xl | additive | new (blend=off) | 50 | 0.44 | 0.60 | 0.80 | 651 |
| xl | off | legacy | 50 | 0.36 | 0.52 | 0.76 | 32 |
| xl | off | new (blend=off) | 50 | 0.44 | 0.64 | 0.80 | 0 |
| xl_clean | additive | legacy | 50 | 0.42 | 0.46 | 0.74 | 611 |
| xl_clean | additive | new (blend=off) | 50 | 0.40 | 0.44 | 0.74 | 542 |
| xl_clean | off | legacy | 50 | 0.42 | 0.50 | 0.64 | 55 |
| xl_clean | off | new (blend=off) | 50 | 0.52 | 0.64 | 0.78 | 0 |

Additive-cell deltas (new blend=off vs legacy blend=legacy):

- xl additive: gold_delivered_text +0.00, gold_delivered_id -0.02, answerability -0.04
- xl_clean additive: gold_delivered_text -0.02, gold_delivered_id -0.02, answerability +0.00

**Interpretation:**
1. Mechanism confirmed: the off-combinator exact-inversion floor collapses from 32 -> 0 (xl) and 55 -> 0 (xl_clean) under blend_mode="off", matching the success criterion exactly.
2. Delivery cost on the additive cell (production-default combinator) is small and mixed: xl loses 0.02 gold_delivered_id / 0.04 answerability; xl_clean loses 0.02 gold_delivered_text / 0.02 gold_delivered_id with flat answerability.
3. Unexpectedly, the off-combinator cell also *gained* delivery under blend_mode="off" (xl gold_delivered_id 0.52->0.64; xl_clean 0.50->0.64) alongside the inversion collapse -- the legacy blend layer was actively degrading off-combinator delivery, not just producing inversions.
4. n=50 needles matches the baseline exactly on both beds; no "errors" field and no missing metrics in either JSON.
5. Verdict: ship blend_mode="off" as the fix -- inversion floor eliminated, and the only regression (additive cell, <=0.04) is within the noise band already observed in the #255 desk-test writeup.

### P1b — ab_blend_scale_relative — DONE (scale_relative shrinks but does not zero the off-cell inversion floor; additive-cell delivery improves rather than costs)

| bed | combinator | source | n | gold_delivered_text | gold_delivered_id | answerability (content_has_answer) | exact_inversions (total) |
|---|---|---|---|---|---|---|---|
| xl | additive | legacy | 50 | 0.44 | 0.62 | 0.84 | 677 |
| xl | additive | new (blend=scale_relative) | 50 | 0.46 | 0.64 | 0.86 | 668 |
| xl | off | legacy | 50 | 0.36 | 0.52 | 0.76 | 32 |
| xl | off | new (blend=scale_relative) | 50 | 0.40 | 0.62 | 0.80 | 4 |
| xl_clean | additive | legacy | 50 | 0.42 | 0.46 | 0.74 | 611 |
| xl_clean | additive | new (blend=scale_relative) | 50 | 0.42 | 0.46 | 0.78 | 609 |
| xl_clean | off | legacy | 50 | 0.42 | 0.50 | 0.64 | 55 |
| xl_clean | off | new (blend=scale_relative) | 50 | 0.52 | 0.64 | 0.78 | 13 |

Additive-cell deltas (new blend=scale_relative vs legacy blend=legacy):

- xl additive: gold_delivered_text +0.02, gold_delivered_id +0.02, answerability +0.02, exact_inversions -9 (677->668)
- xl_clean additive: gold_delivered_text +0.00, gold_delivered_id +0.00, answerability +0.04, exact_inversions -2 (611->609)

Off-cell inversion counts (new blend=scale_relative vs legacy):

- xl off: 32 -> 4 (-28, -87.5%), mean_exact_inversions 0.64 -> 0.08
- xl_clean off: 55 -> 13 (-42, -76.4%), mean_exact_inversions 1.10 -> 0.26

**Interpretation:**
1. scale_relative does NOT kill the off-cell inversion floor to exactly zero the way blend_mode="off" did in P1a (32->0 xl, 55->0 xl_clean) -- it shrinks it sharply instead (32->4 xl, 55->13 xl_clean), leaving a small residual floor on both beds.
2. Additive-cell delivery moves in the opposite direction from P1a: every metric is flat-to-positive vs legacy (xl +0.02/+0.02/+0.02 text/id/ans; xl_clean flat/flat/+0.04), with zero regressions, versus P1a's small additive-cell losses (xl gd_id -0.02/ans -0.04; xl_clean gd_text -0.02/gd_id -0.02).
3. The off-cell also gains delivery under scale_relative on both beds (xl gd_id +0.10, ans +0.04; xl_clean gd_text +0.10/gd_id +0.14/ans +0.14), consistent with P1a's finding that the legacy blend layer was itself actively degrading off-combinator delivery, not just producing inversions.
4. Versus blend=off as a graduation candidate: off gives a clean zero-inversion guarantee at a small additive-cell delivery cost (<=0.04); scale_relative gives a much lower but nonzero residual floor (4/13 vs 0/0) while *improving* additive-cell delivery instead of costing it -- a different point on the same trade-off curve, not a strict dominance either way.
5. No anomalies: n=50 needles on every cell in both files, no "errors" field, no missing agg metrics in either JSON.
### P2 — semantic_probe_10k_full — DONE (dense's ranking-not-recall problem replicates and worsens on semantic (pool_present 1.000, median rank 545 vs smoke's ~384); fused wins gold_delivered_id overall and on most types by riding lexical's ranks, not dense's)

| Type | Arm | n | gold_delivered_id | pool_present | median_best_gold_rank (n_ranked) |
|---|---|---|---|---|---|
| semantic | lexical | 125 | 0.312 | 0.856 | 12 (n=107) |
| semantic | dense | 125 | 0.296 | 1.000 | 545 (n=125) |
| semantic | fused | 125 | 0.376 | 0.856 | 10 (n=107) |
| basic | lexical | 175 | 0.703 | 0.943 | 3 (n=165) |
| basic | dense | 175 | 0.686 | 1.000 | 80 (n=175) |
| basic | fused | 175 | 0.749 | 0.949 | 3 (n=166) |
| intra_document_reasoning | lexical | 40 | 0.850 | 1.000 | 2.5 (n=40) |
| intra_document_reasoning | dense | 40 | 0.900 | 1.000 | 59 (n=40) |
| intra_document_reasoning | fused | 40 | 0.925 | 1.000 | 2.5 (n=40) |
| project_related | lexical | 40 | 0.925 | 1.000 | 1 (n=40) |
| project_related | dense | 40 | 0.925 | 1.000 | 1 (n=40) |
| project_related | fused | 40 | 0.950 | 1.000 | 1 (n=40) |
| constrained | lexical | 30 | 0.667 | 1.000 | 1.5 (n=30) |
| constrained | dense | 30 | 0.833 | 1.000 | 1 (n=30) |
| constrained | fused | 30 | 0.933 | 1.000 | 1 (n=30) |
| conflicting_info | lexical | 20 | 0.950 | 1.000 | 1 (n=20) |
| conflicting_info | dense | 20 | 0.900 | 1.000 | 1.5 (n=20) |
| conflicting_info | fused | 20 | 0.900 | 1.000 | 1 (n=20) |
| completeness | lexical | 20 | 0.900 | 1.000 | 1 (n=20) |
| completeness | dense | 20 | 0.800 | 1.000 | 1 (n=20) |
| completeness | fused | 20 | 0.750 | 1.000 | 1 (n=20) |
| miscellaneous | lexical | 20 | 0.900 | 1.000 | 1 (n=20) |
| miscellaneous | dense | 20 | 0.950 | 1.000 | 1.5 (n=20) |
| miscellaneous | fused | 20 | 0.950 | 1.000 | 1 (n=20) |
| **ALL (overall)** | | | | | |
| all | lexical | 470 | 0.655 | 0.940 | 3 (n=442) |
| all | dense | 470 | 0.655 | 1.000 | 96 (n=470) |
| all | fused | 470 | 0.709 | 0.943 | 3 (n=443) |

**Interpretation:**
1. Smoke finding replicates and sharpens at n=125 on semantic: dense pool_present_rate = 1.000 (perfect recall into the candidate pool) but median_best_gold_rank = 545 (deeper than the daytime smoke's ~384) -- confirms ranking-not-recall, not a smoke fluke. Overall (all 470 questions) dense median rank is 96, far shallower than semantic alone, because semantic is dense's single worst type by a wide margin (basic: rank 80; project_related/constrained/conflicting_info/completeness/miscellaneous: rank 1-1.5).
2. gold_delivered_id winner by type: fused wins overall (0.709 vs lexical/dense tied at 0.655) and wins semantic (0.376 vs lexical 0.312 vs dense 0.296) and basic (0.749 vs 0.703 vs 0.686). Lexical only wins on conflicting_info (0.95) and completeness (0.90) -- both small n=20 slices where dense/fused lag lexical by one wrong doc.
3. Fused does not inherit dense's ranking problem: fused's median_best_gold_rank tracks lexical closely everywhere -- semantic 10 vs lexical 12 vs dense 545; overall 3 vs lexical 3 vs dense 96 -- i.e. RRF's rank-based combination discards dense's deep, low-confidence hits instead of letting them drag the fused rank down.
4. But fused also does not inherit dense's superior recall: fused's pool_present_rate tracks lexical's, not dense's 1.000, on every type where lexical falls short of full recall (semantic: lexical 0.856 = fused 0.856 vs dense 1.000; basic: lexical 0.943 vs fused 0.949 vs dense 1.000; overall: lexical 0.940 vs fused 0.943 vs dense 1.000) -- dense-only hits outside lexical's returned set mostly do not survive into the top-50 fused pool.
5. Net read: fused behaves like "lexical plus a small recall/delivery nudge," not like "dense's ranking problem fixed." It wins on the metric that matters (gold_delivered_id) but the mechanism is riding lexical's tight ranks, not rescuing dense's deep ones -- dense's real differentiator (finding docs lexical structurally misses) is not showing up in the fused pool_present numbers.

**Anomalies / data-quality notes:**
- Task brief described "n=125" and types "semantic, basic, high_level"; the actual file has n_questions=470 across 8 types (basic 175, semantic 125, intra_document_reasoning 40, project_related 40, constrained 30, conflicting_info 20, completeness 20, miscellaneous 20) -- n=125 exactly matches the semantic-only subset, and there is no "high_level" type in this bed's taxonomy (closest analog is intra_document_reasoning / project_related).
- No "error" fields in any of the 3 x 470 = 1410 per-question records; no question had n_gold_ids == 0; best_gold_rank is None if and only if pool_present is false (zero inconsistent rows) across all three arms.
- "combinator" is reported as null in the JSON for all three arms (lexical/dense/fused) -- this field appears unpopulated in this script version rather than indicating a config problem.
- median_best_gold_rank in the JSON was cross-checked by recomputing statistics.median() over records with pool_present=true directly from the raw records array; it matches the reported per_type/overall values exactly (verified for lexical, dense, fused, semantic and overall cells).


### P3 — semantic_probe_10k_fused_riders — DONE (eps_band/off ≥ additive on semantic gold_delivered_id (0.384 vs 0.376) and compress median rank everywhere (semantic 10→6, ALL 3→1) — the literal-bed additive-load-bearing verdict does NOT reproduce on this semantic-heavy 10k bed)

| Type | Combinator | n | gold_delivered_id | pool_present | median_best_gold_rank (n_ranked) |
|---|---|---|---|---|---|
| semantic | additive | 125 | 0.376 | 0.856 | 10 (n=107) |
| semantic | eps_band | 125 | 0.384 | 0.856 | 6 (n=107) |
| semantic | off | 125 | 0.384 | 0.856 | 6 (n=107) |
| basic | additive | 175 | 0.749 | 0.949 | 3 (n=166) |
| basic | eps_band | 175 | 0.749 | 0.949 | 1 (n=166) |
| basic | off | 175 | 0.749 | 0.949 | 1 (n=166) |
| intra_document_reasoning | additive | 40 | 0.925 | 1.000 | 2.5 (n=40) |
| intra_document_reasoning | eps_band | 40 | 0.900 | 1.000 | 1 (n=40) |
| intra_document_reasoning | off | 40 | 0.900 | 1.000 | 1 (n=40) |
| project_related | additive | 40 | 0.950 | 1.000 | 1 (n=40) |
| project_related | eps_band | 40 | 0.975 | 1.000 | 1 (n=40) |
| project_related | off | 40 | 0.975 | 1.000 | 1 (n=40) |
| constrained | additive | 30 | 0.933 | 1.000 | 1 (n=30) |
| constrained | eps_band | 30 | 0.967 | 1.000 | 1 (n=30) |
| constrained | off | 30 | 0.933 | 1.000 | 1 (n=30) |
| conflicting_info | additive | 20 | 0.900 | 1.000 | 1 (n=20) |
| conflicting_info | eps_band | 20 | 0.900 | 1.000 | 1 (n=20) |
| conflicting_info | off | 20 | 0.900 | 1.000 | 1 (n=20) |
| completeness | additive | 20 | 0.750 | 1.000 | 1 (n=20) |
| completeness | eps_band | 20 | 0.750 | 1.000 | 1 (n=20) |
| completeness | off | 20 | 0.750 | 1.000 | 1 (n=20) |
| miscellaneous | additive | 20 | 0.950 | 1.000 | 1 (n=20) |
| miscellaneous | eps_band | 20 | 0.950 | 1.000 | 1 (n=20) |
| miscellaneous | off | 20 | 0.950 | 1.000 | 1 (n=20) |
| **ALL (overall)** | | | | | |
| all | additive | 470 | 0.709 | 0.943 | 3 (n=443) |
| all | eps_band | 470 | 0.713 | 0.943 | 1 (n=443) |
| all | off | 470 | 0.711 | 0.943 | 1 (n=443) |

**Interpretation:**
1. Semantic-type winner: eps_band and off tie at gold_delivered_id=0.384 vs additive's 0.376 (+0.008, 1/125 question), and both cut median_best_gold_rank from 10 to 6 (n_ranked=107 identical across all three combinators — pool membership never changes, only rerank order does).
2. The 2026-07-10 literal-bed desk-test verdict flips here: that test found additive load-bearing on xl (gd_id 0.62→0.52 under eps_band, a -0.10 drop); on this semantic-heavy 10k bed the worst regression anywhere is intra_document_reasoning (-0.025, 1/40 questions) vs. gains on semantic/project_related/constrained/ALL — additive is not uniformly load-bearing, the effect is corpus-dependent.
3. Sanity vs P2 checks out: fused/additive here reproduces P2's fused@default numbers exactly (semantic 0.376/rank 10/pool 0.856/n_ranked 107; basic 0.749/rank 3/pool 0.949/n_ranked 166; ALL 0.709/rank 3/pool 0.943/n_ranked 443) — no mismatch, confirms additive is P2's implicit default and P3 is a faithful extension.
4. Biggest eps_band/off win: constrained gd_id (eps_band 0.967 vs additive/off 0.933, +0.033 = 1/30 questions) plus the median-rank compression pattern repeating across nearly every type (basic 3→1, intra_document_reasoning 2.5→1, semantic 10→6) even on types where gd_id itself is unchanged — eps_band/off never worsen a rank, only tie or shrink it.
5. Net read: combinator choice looks corpus-dependent — additive is load-bearing on literal/xl beds but flat-to-mildly-beneficial on this semantic-heavy 10k bed. This does not by itself justify reverting the RRF default or switching combinators globally, but it undercuts using the xl literal-bed regression alone as grounds to keep additive everywhere; the two beds need to be reconciled before any global combinator change.

**Anomalies / data-quality notes:**
- No `error` fields, no `n_gold_ids == 0`, and zero best_gold_rank/pool_present inconsistencies across all 3 x 470 = 1410 per-question records.
- Verified pool_present, pool_size, and type are byte-identical across all three combinators for every one of the 470 questions — confirms the combinator only reorders an already-fixed candidate pool, it does not change retrieval/recall.
- `combinator` field is populated (non-null) in this file's cells, unlike the P2 file where it was reported null — no anomaly, just a script-version difference noted for the record.

### Clock check @ 20:29 (before P4)

Run started 15:48; elapsed 4h41m; remaining ~4.5-5.5h of the 9-10h budget. P4 estimate 4-6h: expected case fits (P2 came in under its 2h estimate on the same driver at 1h44m), so P4 STARTED at 20:30. P5 (blob dense canary) requires >=2.5h remaining AFTER P4 - already infeasible, will log SKIPPED. Deferred per prompt (not attempted): blob full lexical+fused sweep, shard A/B receipt (#222/#223), #204 SPLADE strip-twin curve, #221 SIKE injection sweep.

### P4 — semantic_probe_50k_full — DNF (no artifact, no surviving process; session ended mid-run)

Relaunched 2026-07-11 morning from the overnight worktree (same code pin, same CLI, output `benchmarks/results/semantic_probe_50k_full.json`). Joe is running the same probe on his Spark-scale beds (thread `spark-erb-receipts` in cc-exchange) — his rows become the scale column, this re-run is the cross-hardware check.

### P5 — blob dense canary — SKIPPED per clock rule.

### Scope change @ 00:30 2026-07-11 (user approval)

User approved the parked/deferred benchmarks to proceed and granted ~30h unattended (remote mode). Extended queue, serialized on disk, same hard rules (no merges/PRs/default changes; canonical beds read-only; checkpoint per job):
- P6: blob dense canary (was P5-skipped) with a 10-question timing probe first
- P7: blob full lexical+fused sweep (~8-12h), gated on P6 rate
- P8: shard A/B receipt #222/#223 (build_shard_gold + two live servers) - attempted now that it's approved; abandon per 15-min rule if stateful setup fights back
- P9: #204 SPLADE strip-twin curve (twin-bed builds)
- P10: #221 SIKE injection sweep

### Parked-jobs runbook (Explore agent, 00:45)

- P8/Job A (shard receipt #222/#223): MOST feasible. bench_shard_recall.py vs one live uvicorn per arm; gold needles already exist (shard_gold_medium.jsonl n=150, shard_gold_xl.jsonl n=379 - do NOT regenerate). Knobs via env on the sharded server: HELIX_SHARD_FETCH_FACTOR=2 vs 4 (#222), HELIX_SHARD_COACT_RESERVE=0 vs N (#223). ALWAYS HELIX_DISABLE_LEARN=1 + compact_interval=0 (historical diag tomls at F:/tmp/diag_*_sharded.toml). ~2.2 s/query -> ~1-2h total. Risk: sharded fixture may store absolute shard paths, so serve canonical tree read-only (learn disabled) as historical diag runs did.
- P10/Job C (SIKE #221 3-bed): feasible unattended via scripts/bench_chain/sike_ctl.ps1 launch (resume/pause-safe, checkpoint per rung). Pass 1 retrieval recall is model-independent; Claude rung needs `claude -p` auth (no preflight in script; 2026-07-03 run failed 50/50 on 401) -> preflight, else SIKE_SKIP_CLAUDE=1. Beds copied to genomes/bench/sike_beds/ (canonical only read). 4th 829K bed is a separate ~20-24h/$170 job - NOT tonight.
- P9/Job B (#204 SPLADE strip-twin): BLOCKED as-committed - no *_no_splade.db twins anywhere, no strip-twin builder script, no gold _splade_curve_queries.json (auto-synth = smoke only). Only a copies-based strip smoke is possible without authoring inputs; ranked last.

Execution order: P4 -> P6 blob probe/canary -> P7 blob lexical+fused sweep -> P8 shard receipt -> P10 SIKE -> P9 strip smoke if time.

### P4 — semantic_probe_50k_full — DONE (dense median-rank miss on semantic worsens 6.8x at scale — 545→3724 — while pool_present holds at 1.000; fused still wins gd_id but its whole-corpus edge over lexical collapses to near-flat)

n=470 across 8 types (semantic n=125), same question-set family as tonight's P2 10k run (`enterprise_rag_50k_batched.db`, driver `ab_semantic_probe.py`, arms lexical/dense/fused). Zero anomalies: per-type/overall stats recomputed from raw records match the file's precomputed aggregates exactly; type histogram identical to the 10k P2 run.

#### Type x arm (50k)

| type | arm | n | gold_delivered_id | pool_present | median_best_gold_rank |
|---|---|---:|---:|---:|---:|
| semantic | lexical | 125 | 0.192 | 0.776 | 13 |
| semantic | dense | 125 | 0.168 | 1.000 | 3724 |
| semantic | fused | 125 | 0.248 | 0.776 | 13 |
| basic | lexical | 175 | 0.663 | 0.920 | 4 |
| basic | dense | 175 | 0.594 | 1.000 | 448 |
| basic | fused | 175 | 0.623 | 0.937 | 4 |
| intra_document_reasoning | lexical | 40 | 0.750 | 0.950 | 3.5 |
| intra_document_reasoning | dense | 40 | 0.725 | 1.000 | 285.5 |
| intra_document_reasoning | fused | 40 | 0.775 | 0.975 | 3 |
| project_related | lexical | 40 | 0.975 | 1.000 | 1 |
| project_related | dense | 40 | 0.950 | 1.000 | 2 |
| project_related | fused | 40 | 0.975 | 1.000 | 1 |
| constrained | lexical | 30 | 0.733 | 1.000 | 2 |
| constrained | dense | 30 | 0.800 | 1.000 | 2 |
| constrained | fused | 30 | 0.933 | 1.000 | 1 |
| conflicting_info | lexical | 20 | 0.850 | 1.000 | 1 |
| conflicting_info | dense | 20 | 0.650 | 1.000 | 3 |
| conflicting_info | fused | 20 | 0.700 | 1.000 | 1 |
| completeness | lexical | 20 | 0.850 | 1.000 | 1 |
| completeness | dense | 20 | 0.700 | 1.000 | 1 |
| completeness | fused | 20 | 0.700 | 1.000 | 1 |
| miscellaneous | lexical | 20 | 0.800 | 1.000 | 2 |
| miscellaneous | dense | 20 | 0.950 | 1.000 | 2.5 |
| miscellaneous | fused | 20 | 0.900 | 1.000 | 1 |
| **ALL** | lexical | 470 | 0.598 | 0.906 | 4 |
| **ALL** | dense | 470 | 0.557 | 1.000 | 479 |
| **ALL** | fused | 470 | 0.604 | 0.915 | 3 |

#### 10k -> 50k deltas (headline cells; 10k reference = tonight's P2 on `enterprise_rag_10k` family, same driver/questions)

| row | arm | gd_id 10k | gd_id 50k | Δ gd_id | pool 10k | pool 50k | Δ pool | medrank 10k | medrank 50k | rank ratio (50k/10k) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| semantic | lexical | 0.312 | 0.192 | -0.120 | 0.856 | 0.776 | -0.080 | 12.0 | 13.0 | 1.08x |
| semantic | dense | 0.296 | 0.168 | -0.128 | 1.000 | 1.000 | +0.000 | 545.0 | 3724.0 | 6.83x |
| semantic | fused | 0.376 | 0.248 | -0.128 | 0.856 | 0.776 | -0.080 | 10.0 | 13.0 | 1.30x |
| **ALL** | lexical | 0.655 | 0.598 | -0.057 | 0.940 | 0.906 | -0.034 | 3.0 | 4.0 | 1.33x |
| **ALL** | dense | 0.655 | 0.557 | -0.098 | 1.000 | 1.000 | +0.000 | 96.0 | 479.0 | 4.99x |
| **ALL** | fused | 0.709 | 0.604 | -0.105 | 0.943 | 0.915 | -0.028 | 3.0 | 3.0 | 1.00x |

#### Interpretation

- **Dense median rank worsens sharply with scale**: semantic 545 -> 3724 (6.83x), and even on ALL, 96 -> 479 (4.99x) — the #260 hypothesis (rank-miss worsens 10k->50k) is confirmed, and the effect is not semantic-only.
- **Ranking-not-recall signature is intact and sharper at 50k**: dense pool_present stays pinned at 1.000 on both semantic and ALL (Δpool = 0.000) while gd_id and median rank both degrade — the gold document is always retrievable, it just sinks deeper in an ever-larger candidate pool. This is pool dilution, not a recall failure.
- **Fused wins gold_delivered_id at 50k on both cuts** (semantic 0.248 > lexical 0.192 > dense 0.168; ALL 0.604 > lexical 0.598 > dense 0.557), but its edge over lexical on ALL nearly vanishes (+0.005 at 50k vs +0.054 at 10k) even though the semantic-subset edge holds up better (+0.056 at 50k vs +0.064 at 10k, both arms drop together).
- **Lexical pool_present degrades modestly with corpus size** (semantic 0.856->0.776, ALL 0.940->0.906, both about -3 to -8pp) — a real but small effect next to dense's multi-hundred-rank collapse; lexical's own median rank only drifts 1.08-1.33x, nowhere near dense's degradation.
- **For #260**: this is squarely a ranking/depth problem, not a recall problem — the lever to pull is rank-fusion depth / RRF cutoff tuning (or a dense-aware rerank stage) rather than dense recall work, since dense already finds the gold doc 100% of the time and just needs to surface it higher in the fused ordering.


### P6 pre-step — blob timing probe (--limit 10, dense, semantic) — DONE @ 03:40

Rate: 10 questions in 23m51s = ~143 s/q INCLUDING startup (models + 47GB blob open). Over the 90 s/q kill threshold from the original P5 spec, but the marginal rate is bounded above by this and a full 125-q canary fits in <=5.0h - proceeding (user approved blob work in the extended window).
Probe metrics (dense, semantic, n=10, 829K bed): pool_present 0.90, gold_delivered_id 0.10, median best_gold_rank 27,400 - the dense rank collapse continues super-linearly with corpus size: 545 (10k) -> 3,724 (50k) -> ~27,400 (829K, n=10 preview). Scaling is roughly proportional to bed size (x16.6 docs -> x7.4 rank vs 50k).
Note: P4 actually ran 6h45m (20:30->03:15), over its 4-6h estimate - future 50k 3-arm estimates should use ~17 s/arm-query.

Reshaped blob plan at measured rate: full all-types lexical+fused sweep (fused alone ~18-19h) stays DEFERRED - needs its own night. Tonight instead: complete the SEMANTIC-ONLY 3-arm scale point at 829K in two checkpointed jobs: P6 = dense n=125 (~<=5h), P7' = lexical+fused n=125 (~5-6h). Then P8 shard receipt, P10 SIKE, P9 strip-smoke if time.

### P6 -- semantic_probe_blob_dense_semantic -- DONE (dense/semantic full n=125 on the 829K blob: gd_id 0.072, pool_present 0.976, median rank 50,357 -- worse than the n=10 preview suggested, and rank<=12 delivery basically never happens at this scale)

Source: `.claude/worktrees/overnight/benchmarks/results/semantic_probe_blob_dense_semantic.json`, driver `benchmarks/ab_semantic_probe.py`, bed `F:/tmp/erb_blob.db` (829K genes), `--types semantic --arms dense`, full n=125 (same question family as P2/P4). Recomputed every field directly from the 125 raw records; every recomputed value matches the file's precomputed `per_type.semantic` aggregate exactly (zero delta on gd_id_rate, gd_text_rate, pool_present_rate, n_ranked, mean/median best_gold_rank, mean_gold_answer_overlap) -- no anomalies in the artifact itself.

#### Core stats (dense, semantic, n=125, 829K blob)

| metric | value |
|---|---|
| n | 125 |
| gold_delivered_id_rate | 0.072 (9/125) |
| gold_delivered_text_rate | 0.248 (31/125) |
| pool_present_rate | 0.976 (122/125) |
| n_ranked | 122 |
| mean_best_gold_rank (pool_present) | 66,183.1 |
| median_best_gold_rank (pool_present) | 50,356.5 |
| p25_best_gold_rank (pool_present) | 15,570.5 |
| p75_best_gold_rank (pool_present) | 99,933.5 |
| mean_gold_answer_overlap | 0.486 |
| min / max best_gold_rank | 2 / 251,858 |
| mean pool_size | 178,101 (min 22,591, max 372,999) |

#### Rank distribution shape (delivery-relevant depth vs hopeless depth)

| best_gold_rank <= | count (of 122 pool_present) | frac of pool_present | frac of ALL n=125 |
|---|---|---|---|
| 12 | 1 | 0.008 | 0.008 |
| 50 | 4 | 0.033 | 0.032 |
| 500 | 7 | 0.057 | 0.056 |
| 5,000 | 15 | 0.123 | 0.120 |

#### Scale-curve: dense/semantic, n=125 at each scale point

| bed | n_genes | gd_id | pool_present | median_best_gold_rank | rank / n_genes |
|---|---|---|---|---|---|
| 10k | 10,000 | 0.296 | 1.000 | 545 | 0.0545 |
| 50k | 50,000 | 0.168 | 1.000 | 3,724 | 0.0745 |
| 829K (blob) | 829,000 | 0.072 | 0.976 | 50,356.5 | 0.0607 |

Growth factors: corpus 10k->50k is 5.0x, median rank grows 6.83x (ratio climbs 1.37x -- super-linear leg). Corpus 50k->829K is 16.6x, median rank grows 13.5x (ratio falls to 0.82x of the 50k value -- sub-linear leg). Across the full 10k->829K span: corpus grows 82.9x, median rank grows 92.4x (net mildly super-linear, ratio ~1.11x), but the curve is not a clean power law -- it overshoots at 50k and partially relaxes back at 829K. gd_id decays -43.2% relative (10k->50k) then -57.1% relative (50k->829K) -- the *relative* decay rate accelerates even as the absolute rate is already small.

Preview-vs-full-n check (n=10 preview logged in the P6 pre-step above vs this n=125 full run): preview gd_id 0.10 vs full 0.072 (preview optimistic), preview pool_present 0.90 vs full 0.976 (preview pessimistic), preview median rank ~27,400 vs full 50,356.5 (preview optimistic by ~1.8x). The n=10 preview was directionally right but not quantitatively reliable in either direction -- consistent with high per-question variance at this pool size (pool_size ranges 22.6K-373K across questions).

1. The n=125 full run confirms the *direction* of the n=10 preview (dense collapses further at blob scale) but not its magnitude: full gd_id (0.072) is worse than the preview implied on rank (median 50,357 vs ~27,400) while pool_present (0.976) is better than the preview's 0.90 -- the 10-question sample was too small to trust for point estimates, only for sign.
2. The corpus-size scaling is not a single clean regime: it is super-linear from 10k->50k (rank/n_genes ratio rises 0.0545->0.0745) then partially relaxes sub-linear from 50k->829K (ratio falls to 0.0607) -- net effect over the full range is mildly super-linear, but a single power-law exponent would not fit all three points.
3. Dense is delivering almost nothing at 829K in any form a real top-k budget could use: only 1/125 questions (0.8%) place gold at rank<=12, and even a generous rank<=500 window only catches 7/125 (5.6%); pool_present (0.976) confirms the gold doc is *in* the candidate set almost every time, so this is still a ranking/dilution failure, not a recall failure, but the dilution is now severe enough that gd_id itself has fallen to near-floor (0.072).
4. For #260: the dense arm still isn't a recall problem at blob scale (pool_present holds near 1.0), but the ranking failure is now bad enough that dense is not paying for its transformer query-encoding cost in its current unmodified form -- fusion/rerank depth alone is unlikely to rescue a median rank of 50K+ out of a ~178K-doc mean pool; a real fix (dense-aware rerank stage, ANN threshold recalibration per #250, or restricting dense to a pre-filtered candidate set) would need to be validated before dense is worth running at 829K scale, not just tuned at the margins.
5. No anomalies in the artifact: every recomputed record-level statistic reproduces the file's `per_type.semantic` aggregate exactly; the only oddity worth flagging is the preview-vs-full-n gap above, which is sampling noise (n=10) rather than a pipeline defect.

### P4 (re-run 2026-07-11) — semantic_probe_50k_full — DONE

| arm | scope | n | gd_id | pool | medRank |
|---|---|---|---|---|---|
| lexical | semantic | 125 | 0.192 | 0.776 | 13 |
| dense | semantic | 125 | 0.168 | **1.000** | **3724** |
| fused | semantic | 125 | 0.248 | 0.776 | 13 |
| lexical | ALL | 470 | 0.598 | 0.906 | 4 |
| dense | ALL | 470 | 0.557 | 1.000 | 479 |
| fused | ALL | 470 | 0.604 | 0.915 | 3 |

Scale read (10k → 50k): dense keeps PERFECT pool presence but its semantic median rank deepens 545 → 3724 (~7× on a 5× corpus); fused semantic delivery falls 0.376 → 0.248 (consistent with #93's ~0.20 at 829k); lexical's semantic pool erodes 0.856 → 0.776. The #260 mechanism is ranking collapse under scale with recall intact — up-weighting dense will not fix rank-3724; the fix needs a re-ranking treatment of dense's recalled-but-buried candidates.

### P7' — semantic_probe_blob_lexfused_semantic — DONE (fused's gold_delivered_id edge over lexical fully inverts at 829K -- lexical 0.168 vs fused 0.080 -- while lexical's own pool_present collapses to 0.504, a genuine recall failure distinct from dense's ranking failure)

Source: `.claude/worktrees/overnight/benchmarks/results/semantic_probe_blob_lexfused_semantic.json`, driver `benchmarks/ab_semantic_probe.py`, bed `F:/tmp/erb_blob.db` (829K genes), `--types semantic --arms lexical,fused`, n=125 (same question family as P2/P4/P6). Zero anomalies in the artifact: per-arm `overall` and `per_type.semantic` aggregates are identical (single-type run) and every recomputed statistic (rate, n_ranked, mean/median rank, mean overlap) matches the file's precomputed values exactly; no `error` fields, no `n_gold_ids == 0`, zero pool_present/best_gold_rank inconsistencies across both arms x 125 = 250 records.

#### Per-arm detail (lexical, fused; semantic, n=125, 829K blob)

| metric | lexical | fused |
|---|---|---|
| n | 125 | 125 |
| gold_delivered_id (rate / count) | 0.168 (21/125) | 0.080 (10/125) |
| gold_delivered_text (rate / count) | 0.480 (60/125) | 0.136 (17/125) |
| pool_present (rate / count) | 0.504 (63/125) | 0.512 (64/125) |
| n_ranked | 63 | 64 |
| median_best_gold_rank (pool_present) | 15 | 16.5 |
| mean_best_gold_rank (pool_present) | 18.16 | 19.14 |
| p25 / p75 best_gold_rank | 6.5 / 28.5 | 7.0 / 29.0 |
| mean_gold_answer_overlap | 0.590 | 0.399 |
| rank <= 12 (count / frac of ranked) | 29 / 0.460 | 28 / 0.438 |
| rank <= 50, <= 500 (frac of ranked) | 1.000, 1.000 (pool capped ~50) | 1.000, 1.000 (pool capped ~53) |
| mean pool_size (min-max) | 49.5 (48-50) | 52.8 (50-53) |
| gold_delivered_id given pool_present (n_gd_id / n_pool) | 0.333 (21/63) | 0.156 (10/64) |

Note: unlike dense (mean pool_size ~178K at blob), lexical/fused pool_size is a fixed retrieval cap (~48-53 candidates) at every corpus scale (10k, 50k, 829K alike -- reconfirmed here) -- so the rank<=50 / rank<=500 breakpoints are trivially ~100% of ranked and not meaningful at this arm; the informative number is whether gold clears the top-12ish delivery window at all, and further upstream, whether it survives into the fixed-size pool in the first place (pool_present).

#### 3-arm x 3-scale curve (semantic, n=125 each)

| bed | n_genes | arm | gold_delivered_id | pool_present | median_best_gold_rank |
|---|---|---|---:|---:|---:|
| 10k | 10,000 | lexical | 0.312 | 0.856 | 12 |
| 10k | 10,000 | dense | 0.296 | 1.000 | 545 |
| 10k | 10,000 | fused | 0.376 | 0.856 | 10 |
| 50k | 50,000 | lexical | 0.192 | 0.776 | 13 |
| 50k | 50,000 | dense | 0.168 | 1.000 | 3,724 |
| 50k | 50,000 | fused | 0.248 | 0.776 | 13 |
| 829K (blob) | 829,000 | lexical | 0.168 | 0.504 | 15 |
| 829K (blob) | 829,000 | dense | 0.072 | 0.976 | 50,356.5 |
| 829K (blob) | 829,000 | fused | 0.080 | 0.512 | 16.5 |

Fused-minus-lexical gd_id edge across scale: 10k +0.064 -> 50k +0.056 -> 829K **-0.088** (sign flip). Lexical pool_present drop per step: 10k->50k -8.0pp -> 50k->829K -27.2pp (>3x steeper). Fused pool_present tracks lexical almost exactly at every scale (10k both 0.856; 50k both 0.776; 829K 0.512 vs 0.504, +0.8pp).

**Interpretation:**
1. Lexical does NOT hold up at 829K: pool_present collapses from 0.776 (50k) to 0.504 (829K), a -27.2pp drop more than 3x the 10k->50k step (-8.0pp) -- and this is a genuine recall failure (gold falls out of the fixed ~50-candidate top-k entirely), not a ranking failure -- median rank among the docs still present only drifts 13->15, mild compared to the presence collapse.
2. Fused's edge over lexical fully collapses and inverts at 829K: the gd_id delta goes +0.064 (10k) -> +0.056 (50k) -> **-0.088 (829K)**, the first sign flip anywhere in the three-scale curve -- fused (0.080) is now worse than plain lexical (0.168), not just flat with it.
3. Fused is still discarding dense's deep hits (fused pool_present 0.512 ~= lexical 0.504, both far below dense's 0.976) exactly as at 10k/50k, but the new finding is that fusion is actively harmful on top of that: conditional on gold being in the pool, lexical delivers it 33.3% of the time (21/63) vs fused only 15.6% (10/64) -- RRF is using dense's now near-random rank signal (dense median rank 50,356 inside a ~178K mean pool, i.e. barely better than chance) to demote gold below where plain lexical ordering would have placed it, rather than just failing to promote it.
4. Bottom line for #260 at true corpus scale: two separate, additive problems need two separate levers. (a) Lexical's own candidate-fetch depth (`fts5_candidate_depth`, #205) is now the binding constraint -- no rerank or fusion scheme can recover a document that a top-50 lexical cutoff never retrieves in the first place, so depth is the first lever to pull. (b) The RRF combinator itself needs to become confidence- or rank-aware rather than blending unconditionally: once an arm's own candidate rank is effectively noise at scale (dense here), folding it into RRF measurably drags a good lexical-only ranking down, echoing #250's "dense harmful on literal needles" diagnosis but now shown to also hurt semantic gd_id, not just a diagnosis-only edge case.
5. Net recommendation: do not invest in dense-rerank-depth alone (P6's conclusion already ruled that out on dense's own numbers); prioritize (i) widening lexical's own fetch depth to fix the pool_present collapse, and (ii) gating/down-weighting RRF's dense contribution when dense's rank is far outside a sane band, before any further fusion-depth tuning -- at 829K scale, "lexical-first, fuse cautiously" beats the current unconditional RRF blend on this bed.

### P8 — shard A/B receipt #222/#223 — DONE (#222 confirmed null, <=1.7pp swing; #223 coact-reserve knob never binds -- byte-identical ranks at N=2 and N=4 vs baseline on both beds; large sharded-vs-unsharded gap persists, worst on xl at +31pp recall@10 / +30pp MRR)

| Bed | Cell | n (err) | recall@10 | Δ recall@10 vs base | MRR | Δ MRR vs base | within@10 | cross@10 |
|---|---|---|---|---|---|---|---|---|
| medium | base (fetch=2, coact=0) | 149/150 (err=1) | 0.4161 | -- | 0.1696 | -- | 0.4250 | 0.3793 |
| medium | #222 fetch=4 | 150 | 0.4000 | -1.61pp | 0.1662 | -0.0034 | 0.4083 | 0.3667 |
| medium | #223 coact=2 | 150 | 0.4133 | -0.28pp | 0.1685 | -0.0011 | 0.4250 | 0.3667 |
| medium | #223 coact=4 | 150 | 0.4133 | -0.28pp | 0.1685 | -0.0011 | 0.4250 | 0.3667 |
| medium | unsharded ref | 150 | 0.4333 | +1.72pp | 0.3065 | +0.1369 | 0.4583 | 0.3333 |
| xl | base (fetch=2, coact=0) | 379 | 0.2269 | -- | 0.0957 | -- | 0.2277 | 0.2237 |
| xl | #222 fetch=4 | 379 | 0.2216 | -0.53pp | 0.0936 | -0.0021 | 0.2211 | 0.2237 |
| xl | #223 coact=2 | 379 | 0.2269 | +0.00pp | 0.0957 | +0.0000 | 0.2277 | 0.2237 |
| xl | #223 coact=4 | 379 | 0.2269 | +0.00pp | 0.0957 | +0.0000 | 0.2277 | 0.2237 |
| xl | unsharded ref | 379 | 0.5383 | +31.14pp | 0.3985 | +0.3028 | 0.5479 | 0.5000 |

Per-needle-sample cross-check (the 47-50 record `per_needle_sample` overlap per bed, comparing (id -> rank, error) tuples): f2_c0 vs f2_c2 = 0 diffs, f2_c0 vs f2_c4 = 0 diffs, f2_c2 vs f2_c4 = 0 diffs on BOTH beds (medium and xl) -- the coact-reserve knob produced zero observable change at any sampled needle. f2_c0 vs f4_c0 showed 3/47 diffs (medium) and 2/50 diffs (xl) -- a small, nonzero, non-random effect consistent with the expected null.

**Interpretation:**
1. #222 (fetch factor 2->4) is the expected NULL: recall@10 moves -1.61pp (medium) and -0.53pp (xl), MRR moves -0.0034 / -0.0021 -- both small, both negative (fetch=4 is very slightly worse, not better), well inside run-to-run noise given the medium baseline also lost 1 needle to a timeout that the other cells didn't.
2. #223 (coact reserve) does NOT help at either N: c2 and c4 are recall/MRR-identical to each other and to baseline on xl (0.00pp delta, 0/50 rank diffs), and only trivially better than baseline on medium (+0.28pp, driven entirely by baseline's dropped needle, not by the knob) -- N=2 and N=4 are indistinguishable from N=0, i.e. also a null, and reserved coact slots never bind in this bed.
3. The sharded-vs-unsharded ceiling gap is the real story and it scales badly: medium's gap is modest on recall@10 (+1.72pp) but large on MRR (+0.1369, +13.7pp) -- unsharded wins on early rank, not on whether gold appears in top-10 at all; xl's gap is large on every axis (+31.1pp recall@10, +30.3pp MRR, +32.0pp within, +27.6pp cross) -- sharding hurts far more at scale, consistent with prior blob-out-ranks-shard findings.
4. Recommendation for #222: close as confirmed-null -- do not graduate `HELIX_SHARD_FETCH_FACTOR=4` as a new default; the knob has no measurable benefit and a slight (noise-level) recall cost.
5. Recommendation for #223: close as confirmed-null for both N=2 and N=4 as tested here -- the reserved-coact-slot mechanism is not the lever that closes the shard/unsharded gap; the gap looks structural (fetch depth or shard-boundary co-activation loss), not a reserve-count tuning problem, so further coact-reserve sweeps (N=6, N=8...) are unlikely to pay off without first diagnosing why reserved slots aren't binding.

### P10 — SIKE injection sweep #221 — DONE (gold-delivery holds 60-82% across beds, but Claude Sonnet's correctness-given-delivered collapses 60%->43% as corpus scales while qwen3:8b stays a flat 87%->75%; xl is the recall floor, not the largest bed; no auth errors)

3 beds run 2026-07-11 ~18:50-21:28 local (mtimes 2026-07-11 19:33/20:20/21:28 -0700, i.e. 02:33/03:20/04:28Z on the 12th -- consistent with the stated window) via `scripts/bench_chain/s2_sike_bed_sweep.ps1`, 50 canonical SIKE needles (`benchmarks/bench_needle.py::NEEDLES`, not 53 -- the prompt's needle count is stale) x 3 rungs (`ollama:gemma4:e4b`, `ollama:qwen3:8b`, `claude:sonnet`). All 3 files report `complete: true`, `progress.retrieval_done: true`, all 3 rungs in `rungs_done`, and **zero** errors on the Claude rung (no 401s -- auth held all night). Two isolated `http_error` (500, ~600s each, Ollama-side timeout) hit qwen3:8b on single needles in the 10k and 50k beds; everything else rung `ok`.

| Bed | genes (ingest size) | n needles | Pass 1 gold_delivered (07-11) | Pass 1 gold_delivered (07-03 ref) | body_has_answer | qwen3:8b correctness/coverage | qwen3:8b correct given gold-delivered | claude:sonnet correctness/coverage | claude:sonnet correct given gold-delivered | claude cost (USD) | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|
| enterprise_rag_10k | 15,888 | 50 | 0.82 | 0.80 | 0.24 | 0.7292 / 0.96 | 78.0% (n=41) | 0.8519 / 0.54 | 51.2% (n=41) | 1.7504 | 1 (qwen http 500) |
| xl | 41,898 | 50 | 0.60 | 0.64 | 0.14 | 0.7234 / 0.94 | 86.7% (n=30) | 0.7000 / 0.60 | 60.0% (n=30) | 1.7956 | 0 |
| enterprise_rag_50k | 80,362 | 50 | 0.80 | 0.82 | 0.22 | 0.6957 / 0.92 | 75.0% (n=40) | 0.8182 / 0.44 | 42.5% (n=40) | 1.0423 | 1 (qwen http 500) |

Bed row order above is by actual ingest size (genes), ascending, since "xl" is a dataset name from the original SIKE campaign lineage, not a size marker relative to the enterprise_rag_Nk beds -- it sits *between* 10k and 50k in gene count. `recall@1/3/5_rate` all collapse to the same value as `gold_delivered_rate` in this schema (documented in `retrieval.note`: `find_needle` exposes no per-rank position, so k<=max_genes is a no-op) -- not a finding, just means there's one retrieval number per bed, not three.

**Interpretation:**
1. Injection recall does NOT monotonically degrade with corpus size: 10k -> xl -> 50k gold_delivered is 0.82 -> 0.60 -> 0.80 -- xl is the outlier floor despite sitting in the *middle* of the size range, not the end. This reproduces the prior "xl ceiling is a rank-squeeze artifact, not size-driven starvation" diagnosis from Run-2 (`bench_run2_fts_depth_fusion`) independently on the SIKE gold-injection set -- same bed, same symptom, different benchmark.
2. Consumer correctness conditional on gold actually being delivered is the sharpest signal here: qwen3:8b holds 87%->78%->75% as the bed grows, while claude:sonnet falls 60%->51%->43% over the same range even though claude's raw `correctness_among_answered` looks fine (0.70-0.85) -- because claude's abstain rate climbs with corpus size (coverage 0.60->0.54->0.44) while qwen3:8b barely moves (0.94->0.96->0.92). Claude is trading coverage for precision as noise grows; qwen3:8b just answers regardless.
3. Claude vs qwen3:8b is not a simple "bigger model wins" story: qwen3:8b is *more* likely to get it right when gold is actually delivered, on all 3 beds, at near-zero cost vs ~$1.05-$1.80/50-needle for Sonnet -- Sonnet's edge is precision-when-it-chooses-to-answer, not recall-of-delivered-truth.
4. The 07-03 vs 07-11 retrieval-pass delta is flat / within noise on all 3 beds (-4pp, +2pp, -2pp) -- the intervening merged work (RRF-default switch, splice fix, know-calibration, blend-layer/shard-fetch/co-activation knobs #268-#273) did not move gold_delivered_rate on this fixed 50-needle SIKE set, consistent with those knobs shipping default-off/byte-identical. Note the 07-03 archive itself shows the Claude rung fully successful (50/50 `ok`, real cost, zero errors) despite the parked-jobs runbook's note that "the 2026-07-03 run failed 50/50 on 401" -- the archived JSON is the checkpoint-resumed final state, not the failed first attempt, so the two aren't in conflict but the runbook line describes a transient mid-run condition that self-healed via the script's per-rung resume/checkpoint design.
5. For #221: closes the "does the Claude auth risk block the sweep" question (no), and opens two follow-ups -- (a) the xl-bed rank-squeeze ceiling now has a second independent reproduction (fts-depth-sweep AND SIKE-injection) and is a stronger candidate for prioritized root-cause work; (b) claude:sonnet's growing-with-scale abstain rate on delivered-but-not-answered needles is worth a dedicated look at the abstain-gate thresholds under RRF, since coverage dropping 16pp (0.60->0.44) while correctness_among_answered stays flat suggests the gate, not the model's knowledge, is the limiting factor at scale.

### P9 — SPLADE strip-twin smoke (#204) — DONE (mechanism unblocked; zero recall delta on auto-queries; SPLADE = +33% disk)

Built the missing strip-twin pair for the 10k bed under F:/tmp/overnight/striptwin/ (copy canonical -> DROP TABLE splade_terms -> VACUUM; 1,896,100 rows dropped; 828.3M -> 621.8M). Ran benchmarks/sweep_splade_scale_curve.py single-pair mode, 30 auto-queries, topk 10, 63 seconds end-to-end.

| arm | n_queries | recall@10 | MRR | mean_s | p95_s | disk bytes/gene |
|---|---|---|---|---|---|---|
| on (splade_terms present) | 30 | 0.300 | 0.250 | 1.043 | 4.060 | 55,687 |
| off (stripped) | 30 | 0.300 | 0.250 | 1.048 | 4.084 | 41,801 |

Interpretation (smoke-grade, NOT the #204 curve):
1. Mechanically unblocked: strip-twin build is ~1 min + one bed-copy per scale point - the "needs twin-bed builds" blocker for #204 is gone for the strip route; only the gold query set (_splade_curve_queries.json) remains missing.
2. On auto-queries (random 8-token content slices, lexically biased by construction) SPLADE contributes exactly nothing: identical recall, identical MRR, identical latency.
3. SPLADE's storage cost is real: +13,886 bytes/gene = +33% disk on this bed.
4. Do NOT read the zero delta as the #204 verdict - auto-queries are the worst case for SPLADE (exact lexical overlap guaranteed); the real curve needs paraphrase/semantic gold queries.
5. Proposed next step for #204: author the gold query file (can reuse the ERB semantic questions' phrasing style), then run --curve across small/medium/large/xl/10k/50k strip-twins - build cost is now trivial.

### P8 addendum — why the #223 knob never binds (code trace, diagnosis-only)

The byte-identical coact cells are EXPECTED for tonight's setup - the receipt is INVALID as a knob test, not a confirmed null:
- The knob IS wired to /fingerprint: fast profile -> _retrieve -> query_docs -> ShardRouter.query_genes -> _expand_cross_shard_coactivation (unconditional, shard_router.py:1272) -> _apply_coact_reserve (sole consumer of HELIX_SHARD_COACT_RESERVE, :716-726).
- But expansion reads 1-hop neighbors from per-shard harmonic_links (shard_router.py:1379 -> retrieval/expand.py:50-64) and the sharded medium/xl fixtures have ZERO harmonic_links rows: build_fixture_matrix.py ingests via upsert_doc only; seed_edges() (the only ingest-time writer, retrieval/seeded_edges.py:118,157) is referenced only by tests; matrix manifest.json records harmonic_links: 0 for medium and xl.
- With linked_scores empty, _expand_cross_shard_coactivation returns early (shard_router.py:1401-1402) and the reserve is never reached. Serving traffic can never heal this: co-activation writes are no-ops on the sharded adapter (sharding.py:501-502).
- CORRECTION to the P8 recommendation above: do NOT close #223 as confirmed-null. To actually exercise the knob: (1) add a seed_edges() pass to the sharded fixture build, (2) craft needles whose only path in is a cross-shard harmonic link, (3) re-run the f2_c0 vs c2/c4 cells.
- #222's null stands (fetch factor binds regardless of coact data).
- Bonus finding from the same trace: ShardedGenomeAdapter hard-wires _dense_embedding_enabled=False (sharding.py:251) - the diag toml's dense-on setting silently does nothing on the sharded arm, so the sharded-vs-unsharded gap measured tonight (+31pp on xl) conflates sharding overhead with dense-off-vs-on. Worth a dense-off unsharded reference cell next time.

---

## FINAL VERDICT — 2026-07-11 (extended ~30h window)

### Status table

| Job | Status | Headline |
|---|---|---|
| P1a blend=off receipt | DONE | off-cell exact inversions 32/55 -> 0/0 (mechanism confirmed); additive-cell cost <=0.04 |
| P1b blend=scale_relative | DONE | floor 32/55 -> 4/13 (not zero); additive-cell delivery flat-to-POSITIVE |
| P2 semantic probe 10k, 3 arms | DONE | dense on semantic: pool 1.000 / median rank 545 (ranking, not recall); fused wins gd_id 0.376 sem / 0.709 all |
| P3 10k fused + combinator riders | DONE | eps_band/off >= additive on semantic bed (0.384 vs 0.376; ALL rank 3->1) - combinator verdict is CORPUS-DEPENDENT, as #267 predicted |
| P4 semantic probe 50k, 3 arms | DONE | dense rank-miss x6.8 at scale (median 3,724, pool still 1.000); fused ALL edge collapses to +0.005 |
| P6 blob dense canary (n=125) | DONE | gd_id 0.072 / pool 0.976 / median rank 50,357; top-12 hit on 1/125 |
| P7' blob lexical+fused (n=125) | DONE | fused INVERTS below lexical (0.080 vs 0.168 gd_id); lexical pool collapses to 0.504 = genuine recall failure |
| P8 shard receipt #222/#223 | DONE | #222 confirmed null (fetch=4 never better); #223 INVALID as knob test (fixtures lack harmonic_links - see addendum); xl shard-vs-blob gap +31pp |
| P9 SPLADE strip-twin smoke | DONE | strip route unblocked (~1 min/scale point); zero recall delta on auto-queries; SPLADE = +33% disk |
| P10 SIKE sweep, 3 beds | DONE | retrieval 60-82%; qwen3:8b 75-87% correct-given-delivered; Claude Sonnet 60%->43%, driven by abstain rate rising with corpus size |
| Blob full all-types lexical+fused | DEFERRED | measured rates say fused alone ~18-19h - needs its own night (budget ~30h with analysis) |
| #204 real curve | DEFERRED | blocked only on gold query set now (_splade_curve_queries.json) |

All artifacts under docs/research/data/2026-07-11-*.json on branch bench/overnight-2026-07-10 (12 checkpointed commits). No merges, no PRs, no default changes; canonical beds untouched (read-only / copies only).

### Decisions now unblocked

1. **blend_mode graduation (audit item 5)**: legacy blend is strictly dominated. Choose: `off` = clean zero-inversion invariant at <=0.04 additive cost, or `scale_relative` = 87% inversion reduction with zero delivery cost (flat-to-positive). Evidence: P1a/P1b tables.
2. **#255 combinator**: do NOT flip the global default. additive is load-bearing on literal beds (desk test) but eps_band/off win on the semantic ERB bed (P3). Corpus/query-class-gated combinator (classifier already exists) is the supported design.
3. **#260 semantic levers**: dense = ranking failure scaling ~with corpus (545 -> 3,724 -> 50,357; pool ~1.0 throughout). At 829K unconditional RRF is actively harmful (fused 0.080 < lexical 0.168) AND lexical's own ~50-candidate cutoff becomes a recall failure (pool 0.504). Levers: (a) widen fts5_candidate_depth (#205 knob exists), (b) confidence/rank-aware gating of dense's RRF contribution. Dense-rerank-depth alone is ruled out.
4. **#222**: close as confirmed null. **#223**: reopen with fixture work (seed harmonic_links in sharded build) - tonight's null is an artifact of empty graphs.
5. **#221**: 3-bed curve complete with full consumer ladder. New signal: Claude Sonnet's abstain rate climbs with bed size (coverage 0.60 -> 0.44) while local qwen3:8b holds - interacts with [know]/abstain calibration, worth its own look.
6. **#204**: strip-twin route makes twin builds trivial; author the gold query file and the full --curve is a ~1h job.

### Proposed next actions (evidence line each)

1. Graduate blend_mode via PR with human review - pick off vs scale_relative per the invariant-vs-delivery tradeoff (P1a/P1b tables).
2. Open a #260 experiment: fts5_candidate_depth sweep at 829K, lexical arm, semantic questions (P7' pool 0.504 with fixed ~50-candidate pool).
3. Open a #260 experiment: rank-gated RRF (drop dense votes beyond rank N or below confidence) re-run on 10k/50k/blob semantic (P7' fused-below-lexical inversion; P2/P4 fused-tracks-lexical pool).
4. Fix build_fixture_matrix.py to seed harmonic_links on sharded builds; re-run the #223 cells (P8 addendum trace).
5. Author benchmarks/_splade_curve_queries.json (paraphrase-style golds); run #204 --curve across all strip-twins (P9 receipt).
6. Investigate Claude abstain-vs-corpus-size on SIKE packets (P10 conditional-correctness table) - candidate tie-in to [know] emit_floor calibration.
7. Schedule the blob full all-types lexical+fused night with corrected budget (measured: dense ~143 s/q incl startup, lexical+fused 125q in 8h20m).
8. Also noted: ShardedGenomeAdapter hard-wires dense off (sharding.py:251) - tonight's +31pp shard gap conflates sharding with dense-off; add a dense-off unsharded reference cell to the next receipt.

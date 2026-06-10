# Test / Tuning / Balancing Roadmap

Date: 2026-06-10 · Baseline: v0.7.1 + 2026-06-09 research loop (profiles spec `docs/specs/2026-06-09-retrieval-profiles.md`, evidence roadmap `docs/audits/2026-06-09-next-steps-evidence.md`) · Open tuning issues: #202 #203 #204 #205 #206 #207, plus #93 (500K bench) and #165 (storage verification).

## 0) Current evidence state

- **Retrieval curve under fixed knobs:** 83% recall @10K → 71% @50K (all misses `basic`-type, 14/17 monotone) → 28% @850K; shard router degenerate on broad queries (90–100% shard hit rate, #165); no dense-latency lever on master (Wall-2 PRs #158/#160 closed unmerged, #206; ~870M FLOPs/query dense matmul at 100+ shards).
- **Fresh sweep (2026-06-10, `sweep_dense_additive_weight.py`, auto-synth content-token queries):** ERB-10K recall@10 FLAT at 0.467 for w∈{0..6}, MRR peaks at default 4.0, zero gold eviction; ERB-50K recall@10 only 0.20–0.27 with 3 golds EVICTED at every w≥2, best arm w=6.0 — per-corpus sensitivity emerges at scale, and the adversarial-prose danger case (#138: −19pp at w=4.0 on ERB-10K prose) is query-class-shaped (Layer-2), while 50K shows corpus-shape too. Fresh 500K rebuild finishing now; full ERB adversarial question-set run pending.
- **Storage/telemetry:** corpus-scale #193 compaction just measured 48.68→37.38 GB (−11.3 GB, #165 nearly closeable); SPLADE costs 21.1% disk for 0pp @850K (#164/#204); live OTel sidecar shows only ~12 `helix_*` Prometheus families of the 25 instruments registered in `helix_context/telemetry/otel.py:367-738`, and Loki carries only `service_name`/`deployment_environment` labels — the documented structured proxy-log stream does not exist in code (§3).

---

## 1) SNOW re-spec — verdict: YES, rebuild as SNOW-2 (dual delivery modes)

### What SNOW was

**Spec:** `docs/archive/specs/2026-04-16-snow-benchmark-design.md` — "Scale-invariant Navigation on Organic Webs", design approved 2026-04-16. Measures *navigation strategy* over helix's 5-tier data cascade: T0 fingerprint (~150 tok/gene) → T1 key_values (~63 tok median) → T2 complement (~381) → T3 content (~864) → T4 harmonic-walk. Two consumers: a string-matching **oracle** (theoretical floor within retrieved top-k) and a **real LLM** that triages from tier-score fingerprints and escalates. Stateless hops (explicit v1 trade-off; "stateful cascade" was a named v2 extension). Scorecard: avg hops / tokens / latency vs oracle floor, cascade profile, answered@T0, triage accuracy, miss rate. Harness fully implemented: `benchmarks/snow/{bench_snow.py,oracle.py,cascade.py,prompts.py,snow_compare.py}` + N=65 query set (`snow_queries.json`, 50 needles + 15 tier-stress), 14 tests passing per the implementation plan (`docs/archive/specs/2026-04-16-snow-implementation-plan.md`).

**Last results:**
- LLM arm ran ONCE, N=10 only: `benchmarks/snow/results/snow_qwen3_4b_2026-04-16.json` on the 18,254-gene genome — oracle avg_tier 1.4 / miss 0.5; qwen3:4b **miss_rate 0.9, triage_accuracy 0.0, answered@T0 0%, latency overhead ~73,000×** (12.3 s/query vs 0.2 ms oracle). The local-4B LLM consumer could not navigate; the arm was abandoned.
- The oracle survived as a regression instrument: 2026-04-17 ablation sweep (N=65 ×3 repeats; baseline avg_tier 0.97, miss 28.7%, ~3,273 tok — `ablation_sweep_2026-04-17_r3.json`) across cold-tier-on / cymatics-off / harmonic-off arms, plus pre-shard/post-tombstone oracle baselines.
- Issue/PR search for "SNOW" returns only #93 (1 hit) — no dedicated tracking issue exists. SNOW is archived, not retired.

### Why re-spec

SNOW v1 measured exactly one delivery mode (in-process tier escalation) with one weak consumer, predating: the know/miss agent contract (`scoring/know_decision.py:312` `decide_know_or_miss`), `/context/packet` + `/context/refresh-plan` (`server/routes_context.py:1,618`), `/context/expand` forward/backward/sideways hops (`server/routes_registry.py:226`, `retrieval/expand.py:21-45`), `/fingerprint` with `profile: fast|balanced|quality` and per-row `tier_contributions` + `response_hint` + `agent.recommendation` (`server/routes_context.py:661-899`), session-delivery elision (`context_manager.py:2472-2690`), and an 18-tool MCP surface (`mcp/mcp_server.py:341-881`: `helix_context`, `helix_context_packet`, `helix_refresh_targets`, `helix_fingerprint`, `helix_neighbors`, `helix_gene_get`, `helix_splice_preview`, `helix_resonance`, ...). Helix's stretch capability is now *agentic navigation*, and nothing measures it. ERB's published correctness baselines (#93: BM25 68.8, Vector 51.4, Onyx+GPT-4 72.4) are one-shot-injection numbers — an agentic arm is helix's only credible path above that line.

### SNOW-2 design

**Arms** (same query set, same fixture, same answering model per row):

| Arm | Delivery | Mechanics |
|---|---|---|
| A. injection-only | `POST /context` one shot | today's proxy path; baseline comparable to ERB published numbers |
| B. packet-injection | `POST /context/packet` one shot | adds know/miss + verified/stale_risk; measures whether the contract changes answer/abstain behavior |
| C. MCP-agentic | `helix_fingerprint` → (`helix_gene_get` \| `helix_context_packet` \| `helix_neighbors`)* | LLM iterates; cap at H hops; `claude -p` runner exists in `bench_claude_matrix.py` (tool-isolation fix #184 closed the #154 filesystem-confound) |
| D. CLI-agentic | `helix query` / `helix neighbors` subprocess loop | same policy as C through `cli/`; measures CLI parity |
| E. hybrid know/miss-escalation | packet first; on `miss{reason}` or `know.confidence < floor` → `/fingerprint` triage → `/context/expand` neighborhood hops (≤3) → targeted `helix_gene_get` | the v1 "T4 walk" reborn on the public surface; uses `miss.reason` to pick the escalation branch |

Session-delivery elision stays ON in a multi-turn sub-variant (A′/C′: 5-turn conversations) to measure the claimed ~40% token saving under repetition; `ignore_delivered: true` everywhere else.

**Scoring** (per arm × model): correctness (literal match + LLM judge, ERB rubric where ERB queries used), **tokens spent** (helix-side chars/4 + actual `prompt_eval/eval` counts, per v1 methodology), **wall time**, **turns/hops**, plus v1's cascade profile and triage accuracy for arms C–E. Headline output: a cost-of-correctness frontier (correctness vs tokens) with the oracle floor and arm-A baseline drawn in.

**Exists already:** all five surfaces above; oracle + cascade + compare harness; N=65 set; fixture matrix + BenchServer + swap-db orchestration (`bench_orchestrator.py`); $-bounded `claude -p` runner.
**Missing (build list):** (1) arm runner that drives MCP/CLI loops with a hop budget + per-hop token capture; (2) escalation policy module for arm E consuming `know/miss` JSON; (3) ERB-question adapter so SNOW-2 can run the adversarial set on enterprise fixtures (10K/50K/500K), not just own-code; (4) stateful-cascade prompting (v1's deferred "consumer memory"); (5) `snow_compare.py` extension for arm×model grids; (6) model ladder refresh — v1 proved 4B can't triage; ladder should start at qwen3:8b and include one Claude tier.

---

## 2) Benchmark inventory + run-list

~40 scripts in `benchmarks/` (`benchmarks/README.md`). Verdicts: **RERUN** (stale vs v0.7.x — anything touching path_key_index after #193, tagger after #155/#162, lexical-rescue/de-hardcoded paths after #201), **RUN-FIRST-TIME**, **RETIRE**. ⛔ = gated on the fresh 500K fixture.

| Bench | Measures | Last known result / date | Verdict |
|---|---|---|---|
| `bench_needle.py` (SIKE L1) | curated needles, retr+answer | 10/10 retr across 0.6B→Opus ladder (docs/benchmarks/BENCHMARKS.md, 2026-04-10); N=50 set added 2026-05-15, **never re-baselined** | **RERUN** (post #155/#162 tagger, #193 pki, #201; establishes N=50 baseline) |
| `bench_needle_1000.py` — blind axis | synthetic KV floor | 13.8% headline; v2-harness 16–20% N=50 (2026-04-11) | RERUN low-pri (regression guard, `_run_n1000_blind.sh`) |
| `bench_needle_1000.py` — located axis | 4-axis locator retrieval@1; **the RRF gate** | **never run as gate** (target ≥0.55 sanity / ≥+15pp to flip fusion default; stage-2 spec cites 13.8% current — `docs/specs/2026-05-08-stage-1-bench-axis-split.md`, `2026-05-08-stage-2-dense-recall.md:191`; no result JSON in repo) | **RUN-FIRST-TIME** (decides #202 path) |
| `bench_claude_matrix.py` | 50 needles × 6 fixtures × `claude -p` MCP e2e | pre-v0.7 runs; spawn/confound bugs fixed by #184 | **RERUN** ($19/arm — run once after knobs settle, week 2) |
| `bench_dimensional_lock.py` | recall vs axis count (4 query variants) | Dewey 2026-04-14: key+filename 30% R@1 vs 4-axis 10% | **RERUN** post-#193 + filename_anchor default-on (`helix.toml:284`); feeds §4 |
| `sweep_dense_additive_weight.py` | recall@10 / MRR / gold-eviction per w | **2026-06-10**: ERB-10K flat 0.467, MRR peak w=4.0; ERB-50K 0.20–0.27, 3 golds evicted ∀w≥2, best w=6.0 | **RERUN** with real ERB question set (auto-synth `--queries` gap, sweep_dense_additive_weight.py:67-84) + medium/SIKE-50 code arm; ⛔ @500K (#203) |
| `sweep_splade_scale_curve.py` | SPLADE on/off recall + disk vs corpus size | scaffold only — twin builds never produced (header: "Status: scaffold") | **RUN-FIRST-TIME** (#204); twins at 1K/17K/50K; ⛔ 100K+ twin |
| `benchmarks/snow/` suite | tier-cascade navigation | oracle N=65 2026-04-17 (avg_tier 0.97, miss 28.7%); LLM N=10 (90% miss) | **RERUN as SNOW-2** (§1); ⛔ for 500K arm |
| `bench_multi_needle_50.py` | multi-gene queries N=50 | 2026-04-19/21 | RERUN low-pri (after #202) |
| `bench_multi_needle.py` | N=8 probative set | superseded by N=50 | **RETIRE** |
| `bench_helix_rag_composition.py` | helix vs BM25 vs SEMA-embedding 3-cell | 2026-04-24 (0.81 vs 0.81 tie) | RERUN after #202 (weights become live) |
| `bench_packet.py` | packet verified/stale_risk precision/recall | 2026-04-22 | RERUN (pairs with freshness telemetry, §3) |
| `bench_stale_claim_avoidance.py` | currency-decision quality | 2026-04-19 | keep; RERUN low-pri |
| `bench_skill_activation.py` + `ab_flag_sweep.py` | prompt-shape × tier activation matrix | ab_sweep_* 2026-04 | **RERUN — this is the #202 byte-identical regression harness** |
| `precision_probe/precision_*` | determinism, tie-break, tier contributions | 2026-04-15 | keep as smoke; rerun immediately after #202 plumb |
| `bench_compression.py` | ratio / retention / stage latency | compression_results_v2.json | RERUN once (baseline for splice-ratio telemetry, §3) |
| `bench_cache_hitrate.py` / `bench_dal_http_s3.py` | multi-agent cache value | 41.67% hit / latency curve, 2026-04-19 | **RETIRE** (question answered; archive) |
| `bench_chroma_integration.py` / `bench_external_retriever.py` | adapter protocol vs real stores | 2026-04-19/21 | **RETIRE** to CI smoke |
| `bench_forward_recall.py` | TCM recency asymmetry | 2026-04-14 ×3 (Phase-4 gate decided) | **RETIRE** |
| `bench_babilong.py` | multi-hop chains | babilong_results.json | keep; rerun only as SNOW-2 T4 validation |
| `bench_budget_zone.py` | budget-cap spike | budget_zone_results.json (flag shipped) | **RETIRE** |
| `bench_plr_smoke.py` | PLR p95 + field presence (#74 gate) | gate passed | keep as smoke |
| `bench_aa_suite.py` | AA/reasoning tasks | **mocked datasets only** (header) | **RETIRE** until real datasets wired |
| `bench_gemini.py` | Gemini vs local A/B | old | **RETIRE** |
| `bench_sweep.py` | model-ladder NIAH table | 2026-04-10 table in BENCHMARKS.md | RERUN once post-profiles (doc refresh) |
| `bench_oauth_provider/scope.py` | OAuth-shaped scope retrieval | — | keep niche |
| `fingerprint_06b_test.py` | 0.6B fingerprint triage spike | 2026-04 | **RETIRE** (folds into SNOW-2) |
| `bench_headroom_latency.py` | headroom seam latency | 2026-04-19 | keep low-pri |
| `benchmark_monitor.py` | /stats poller | n/a | keep — it is the fix for stale `/stats` gauges (§3) |

**Also ⛔ 500K-gated:** ERB full scored Q&A vs published baselines (#93: recall BM25 68.4; correctness BM25 68.8 / Vector 51.4 / Onyx+GPT-4 72.4), `basic`-type monotone-miss-set investigation (evidence roadmap item 6), Wall-2 dense-latency A/B of the #158/#160 branch (#206), #165 corpus-scale close-out (already measured −11.3 GB; needs recall-invariance spot-check).

### Two-week execution order (interleaved with 500K availability)

| Day | Run | Depends on |
|---|---|---|
| 1 | Telemetry additions land (§3 top-5) + fingerprint-collision census on existing 850K corpus (§4-E1, SQL-only) + #202 plumb (defaults byte-identical) | nothing |
| 1–2 | `precision_probe` + `ab_flag_sweep` byte-identical regression for #202 | #202 plumb |
| 2–3 | `sweep_dense_additive_weight` on ERB-10K/50K with **real ERB question set** + medium/SIKE-50 code arm (#203 evidence completes for small/mid sizes) | question-set adapter |
| 3–4 | 500K rebuild lands → smoke (`helix diag corpus`), #165 recall spot-check + close; SPLADE twin builds 1K/17K kick off in background (lean-ingest kill-switches per CLAUDE.md) | 500K fixture |
| 4–6 | ERB full 500-question scored run @500K vs published baselines (#93) — with new telemetry capturing dense-cosine/fan-out distributions during the run | 500K |
| 6–7 | `sweep_dense_additive_weight` @500K → **decide default flip + per-profile bases (#203 closes)** | 500K + day-2 arms |
| 7–9 | SPLADE twins 50K (+100K if disk allows) → **set `splade_auto_*_genes` (#204 closes)** (knobs already shipped, `config.py:229-230`) | twin builds |
| 8–9 | `located_n1000` RRF gate (≥+15pp ⇒ flip `fusion_mode`; else keep additive with now-live weights) — settles #202's long-term path | #202 plumb |
| 9–10 | filename_anchor on/off on ERB-10K (profiles L3 row: predicted no-op on prose) + `bench_dimensional_lock` rerun post-#193 | — |
| 10–11 | Wall-2 A/B: per-query dense latency master vs `perf/dense-prefilter-via-splade-candidates` branch @500K → re-land or supersede (#206) | 500K |
| 11–12 | §4 E2 chunk-size re-chunk sweep on one ERB shard + E3 AND-mode router prototype | census results |
| 12–13 | `bench_needle.py` N=50 re-baseline + `bench_claude_matrix` single arm (budget-capped) | knob decisions |
| 13–14 | **Profiles implementation gate (#205)**: all Layer-1/Layer-3 values now measured; write profiles; SNOW-2 build starts as the post-profiles acceptance bench | everything above |

---

## 3) Telemetry additions (all ride the native sidecar — `tools/native-otel/`, no Docker; `scripts/setup-grafana-telem.ps1`)

### (a) Instrumented-but-underused (instrument registered in `telemetry/otel.py`, but no live series and/or no dashboard panel)

- `helix_pipeline_stage_seconds` (otel.py:603) — **no panel in any shipped dashboard JSON**; per-stage latency is the primary splice/assemble tuning signal.
- `helix_ribosome_call_seconds` (otel.py:681) — no panel (only `ribosome_info` is charted in helix-overview.json).
- `helix_genome_wal_size_bytes` / `helix_genome_checkpoint_blocked_total` (otel.py:713/729) — no panels.
- `helix_vault_export/_pruner/_force_prune/_file_count` (otel.py:555-585) — 4 instruments, zero panels.
- `/stats`-gated gauges `harmonic_edges, chromatin_state, genome_size, hub_concentration, hub_inbound_degree` (otel.py:419-480) — panels exist in helix-overview.json but go stale unless something polls `/stats` (documented, OBSERVABILITY.md "if nothing polls /stats, the gauges go stale"); absent from the ~12 live families. Fix: launcher self-poll or `benchmark_monitor.py` as a service.
- `helix_cwola_f_gap_sq` (otel.py:410), `helix_budget_tier_total` (otel.py:519), `helix_tier_fired_total`/`helix_tier_contribution` (otel.py:447/367, emitted at knowledge_store.py:2430) — panels exist; only cwola_bucket + ellipticity + latency + calls_by_class + health + genome_signal confirmed live. Verify emission paths under real traffic.
- **Dead dashboard:** `deploy/otel/grafana/dashboards/helix-pipeline-observatory.json` charts 8 metric names that exist nowhere in code (`helix_chroni_join_state`, `helix_cost_concentration_ratio`, `helix_crdt_bucket_accumulation`, `helix_resolve_degree_distribution`, `helix_ring_edges_by_provenance`, `helix_rq_duration_seconds`, `helix_tier_estimation_percent`, `helix_tier_readable_time`).
- **Doc-vs-code drift:** OBSERVABILITY.md:75-160 documents `helix_context_cache_outcome_total`, the entire `helix_genai_*` family (token usage, TTFT, cost, finish reasons), and the structured `helix.proxy` JSON log line via `helix_context/genai_telemetry.py` — **that module does not exist on master** (only a dashboard label reference at `launcher/app.py:105`). This is why Loki carries only `service_name`/`deployment_environment` labels. Either land the module or strike the docs; the Loki/log-label gap closes with it.

### (b) Computed-but-unexported (one line each: where computed → suggested instrument)

1. PKI tier hits/candidate counts — knowledge_store.py:1715-1779 (path_key_index tier) → `helix_pki_candidates` histogram + `helix_pki_tier_skipped_total` counter.
2. Dense cosine distributions — knowledge_store.py:433-444 (additive dense merge, min_cosine 0.15) + :880 (cold-tier scan) → `helix_dense_cosine` histogram, label `arm=hot|cold|semantic`.
3. Know/miss decisions — scoring/know_decision.py:312 `decide_know_or_miss` → `helix_know_decision_total` counter `{outcome, reason}` + `helix_know_confidence` histogram.
4. Abstain gate fires — pipeline/tier_logic.py:27-69 (`abstain`, `abstain_top_score`, `abstain_ratio` computed per turn) → `helix_abstain_total` counter `{trigger=floor|ratio}`.
5. Session-delivery elision savings — context_manager.py:2472-2501, 2665 (elided stubs vs fresh deliveries) → `helix_session_elided_total` + `helix_session_tokens_saved_total` counters.
6. Splice compression ratio — context_manager.py:2690 (`compression_ratio=total_raw/compressed_chars`, already in legibility headers) → `helix_splice_compression_ratio` histogram `{decoder_mode}`.
7. Freshness-gate demotions — retrieval/freshness.py:89-154 `check_staleness` (fresh/stale/missing/unknown), :207 `check_superseded` → `helix_freshness_demotion_total` counter `{status}`.
8. Per-shard routing fan-out + discrimination — shard_router.py:387 `route()` / :447 `query_genes()` (per-shard raw scores + IDF correction at :187) → `helix_shard_fanout` histogram (shards consulted/query) + `helix_shard_candidates` histogram.
9. VRAM during dense ingest — the #176/#177 `empty_cache` sites in the BGE-M3 batch loop → `helix_ingest_vram_bytes` gauge (`torch.cuda.memory_allocated` sample/batch).
10. Fingerprint floor/truncation outcomes — routes_context.py:805-870 (`filtered_by_floor`, `truncated_by_cap`, `tier_totals` all computed per call) → `helix_fingerprint_filtered_total` counter `{cause}`.
11. Classifier class frequencies — **already exported** (`helix_context_calls_by_class`, observed live); no action. Gold-eviction is bench-side only — publish as Grafana annotations from sweep JSONs, not a runtime metric.

### (c) Top 5 additions ranked by tuning value

1. **`helix_dense_cosine` histogram** — turns every live query + every bench run into calibration data for `dense_additive_weight`/`dense_additive_min_cosine` and the cross-corpus threshold-transfer problem (profiles spec: dense tier totals 9–28 on ERB vs ~3 own-code). Directly de-risks the #203 default flip and Layer-1 auto-calibration.
2. **`helix_shard_fanout` + `helix_shard_candidates`** — quantifies router degeneracy in production (the #165 "90–100% of shards" finding becomes a continuously-monitored number). Gates #206 (Wall-2 re-land) and is the acceptance metric for the §4 AND-mode router.
3. **`helix_know_decision_total` + `helix_know_confidence`** — calibrates `[know]` floors/margins per corpus (Layer-1) and is the input signal for SNOW-2 arm E (miss-reason-driven escalation). Without it, abstain/know tuning stays anecdotal.
4. **`helix_session_tokens_saved_total`** — proves (or falsifies) the "~40% tokens on multi-turn" claim in CLAUDE.md and prices the elision arm of SNOW-2; decides whether `session_delivery_enabled` stays default-on.
5. **`helix_splice_compression_ratio` + `helix_abstain_total`** — the balancing pair for `splice_aggressiveness` and the #207 budget/abstain knob exposure: watch ratio drift vs abstain-rate drift while sweeping; any tuning that raises compression but spikes abstains is a net loss.

---

## 4) Fingerprint → 100% R@1 program

### What exists

- **Storage-side keys:** `path_key_index(path_token, kv_key, gene_id)` WITHOUT ROWID + `idx_pki_gene` only, post-#193 (storage/ddl.py:256-266); pairs matching >`PKI_NOISE_CUTOFF=200` genes are pruned/skipped by the scorer (storage/indexes.py:119,212-215). `filename_index(filename_stem, gene_id)` with noise-stem exclusion (filename_anchor.py:43-48), boost 4.0 vs exact-tag 3.0 (helix.toml:284-285). Shard-level `fingerprint_index(gene_id, shard, source_id, domains, entities, key_values)` (shard_schema.py:112-123). Every gene already has a globally unique `gene_id` — uniqueness is solved storage-side.
- **Query-side today:** compound lookup `path_token AND kv_key` ("helix"+"port", knowledge_store.py:125-131) is the only AND-shaped signal; everything else is additive OR. Broad query terms hit 90–100% of shards (#165); pre-#193 the index cost ~19 KB/gene (34.1% of the 47 GB corpus) for routing that wasn't pruning.

### (a) Minimal query info that guarantees rank-1

R@1 fails today not for lack of a unique key but because the query rarely *expresses* one and the scorer treats key matches as one additive vote among 12. Evidence for which key shapes work: Dewey 2026-04-14 — `key+filename` 30% R@1 vs `key+project+module+filename` 10% (filename_anchor.py:4-6 — extra axes HURT under additive fusion); filename_anchor +24pp where queries name files (helix.toml:281-283); located_n1000's 4-axis locator targets ≥0.55 vs 13.8% blind. Candidate minimal keys, in rising specificity: `filename_stem` alone (collides across projects), `(filename_stem, kv_key)`, `(path_token, kv_key)` (current PKI shape, cardinality ≤200 by cutoff), `(stem, symbol/anchor)` for code. **Collision rates are cheaply derivable**: the #193 compaction already computes pair cardinalities (`GROUP BY path_token, kv_key HAVING COUNT(*) > ?`, storage/indexes.py:212-215) — a census is one SQL pass.

### (b) Chunk-size tradeoff — does atomic + stronger fingerprint flip the scaling math?

Current chunking: `SemanticChunker(max_chars_per_strand=4000)` (encoding/fragments.py:66) ≈ ~1,000 tokens/chunk (consistent with SNOW's T3 median 864); code splits on function/class boundaries with opt-in tree-sitter AST chunking (fragments.py:134-140); splice uses 3-sentence windows (fragments.py:234). Halving chunk size at 850K → ~1.7M genes: dense matmul cost doubles (≈1.7G FLOPs/query, and #206 means master has NO mitigation), FTS/PKI rows roughly double, and additive-OR ranking degrades further (recall already 28% @850K). **Under the current OR-scorer, smaller chunks lose.** But with an AND-shaped fingerprint route — AND-then-OR per the #159 proposal — query cost stops scaling with N: a query expressing a key pair touches ≤ pair-cardinality candidates (≤200 by cutoff, typically far fewer), independent of corpus size. Atomic chunks then *help* both sides: per-chunk keys get sharper (one function/section per chunk → near-unique `(stem, symbol)`), and the AND candidate set shrinks. IDF-filtering entity keys (drop keys above a document-frequency ceiling, exactly the PKI noise-cutoff generalized) keeps the index from re-bloating — #165's lesson is that the index was doing inventory, not pruning; AND-mode makes it prune. Net flip: storage grows O(N) (bounded — post-#193 schema), query work becomes O(collision-set) for locator-bearing queries, with dense matmul demoted to the prose fallback (Layer-2 semantic arm, w=16). 100% R@1 is then bounded by: `coverage` (fraction of chunks carrying ≥1 unique key) × `expressibility` (fraction of queries that state it) × tie-break correctness within collision sets.

### (c) Experiments that would prove it

| # | Experiment | Cost | Accept/decide |
|---|---|---|---|
| E1 | **Fingerprint-collision census** on the existing 850K corpus (37.4 GB post-#193): histogram of `(path_token,kv_key)` pair cardinalities, `filename_stem` cardinalities, % genes reachable by ≥1 cardinality-1 pair | SQL-only, hours, no GPU | yields max achievable exact-route R@1 = unique-key coverage; decides whether anchor keys (E4) are needed |
| E2 | **R@1-vs-chunk-size sweep**: re-chunk ONE ERB shard (~5K docs) at 4000/2000/1000/500 chars (resume/subshard infra #183/#186 makes this cheap), run located-style + content-token queries | 1 shard rebuild ×4 | R@1 monotone gain ≥ +10pp at ≤2× disk ⇒ atomic chunks viable |
| E3 | **AND-mode router prototype** on existing `fingerprint_index`/`path_key_index`: flag-gated AND-then-OR (intersect on all extracted signals, OR fallback when intersection empty), instrumented with `helix_shard_fanout` (§3) | code + bench days | located R@1 ≥ 0.85 @50K and ≥ 0.55 @850K with fan-out p50 ≤ 3 shards ⇒ fingerprint promise holds at scale |
| E4 | **Anchor-augmented keys**: tree-sitter symbol (function/class name) → extra `kv_key` rows at ingest on one shard; re-run E1 census | 1 shard | unique-key coverage on code shards ≥ 95% |
| E5 | **located_n1000 @500K** (doubles as the #202/#203-adjacent RRF gate) | ⛔ 500K | the formal R@1 scoreboard for the program |

---

## 5) Tuning/balancing sequence — feeding #202→#205

The dependency chain, with which run decides which knob:

1. **#202 first, surgically** — plumb the 9 documented `[retrieval]` tier weights into the additive accumulations with defaults byte-identical to today's literals (knowledge_store.py 1846/1886/1940/1979/2037-2038/2084/2218/2281-2283/2341). Until then, sweeping any of those 9 knobs measures nothing. Regression: `precision_probe` determinism + `ab_flag_sweep` activation-matrix byte-compare (day 1–2). Note `dense_additive_weight` IS live (knowledge_store.py:437,546) — which is why #203 can run in parallel.
2. **#203 (dense weight)** — decided by: 2026-06-10 sweep (done: 10K flat/MRR-peak-4.0; 50K eviction ∀w≥2, best 6.0) + ERB-question-set arms @10K/50K (day 2–3) + @500K (day 6–7) + medium/SIKE-50 code arm. Output: per-corpus-profile base values (code likely 2–4, prose 2.0 with Layer-2 semantic swap to 16.0); the 50K eviction result already argues the flip is *profile-conditional*, not universal — exactly the Layer-3 hypothesis.
3. **#204 (SPLADE thresholds)** — decided by: twin-build scale curve 1K/17K/50K(/100K) (days 3–9). Output: `splade_auto_enable_below_genes` / `_disable_above_genes` (config.py:229-230); prose profile sets `off >100K` per #164's 21.1%-disk-for-0pp.
4. **RRF gate (located_n1000, day 8–9)** — decides whether the long-term home of per-tier weights is the Fuser (`fusion_mode="rrf"` flip per the Stage-3 spec timeline) or the now-live additive knobs. Either way #202's plumbing is not wasted: it is the control arm.
5. **#206 (Wall-2)** — decided by: dense-latency A/B master vs the `perf/dense-prefilter-via-splade-candidates` branch @500K (day 10–11), with `helix_shard_fanout`/`helix_dense_cosine` telemetry capturing the distributions. If the §4 E3 AND-router lands, it may supersede #158/#160 outright — document either way.
6. **Telemetry (§3 top-5) precedes all sweeps** (day 1) so every run doubles as distribution-capture for Layer-1 auto-calibration (ann margin-over-random, abstain floors, know betas — `scripts/calibrate_thresholds.py` writes calibration into the genome).
7. **#205 gate (day 13–14)** — implement profiles only when: dense base values per corpus measured (#203 ✅), SPLADE thresholds set (#204 ✅), filename_anchor prose no-op confirmed (day 9–10), RRF-vs-additive decided (step 4), and the code-default-vs-helix.toml divergence reconciled (expression_tokens 6000/7000, max_genes 8/12, splice 0.5/0.3, decoder full/condensed, sr_enabled false/true — profiles spec, last §). Layer 2 (classifier-owned semantic arm, drop `HELIX_SEMANTIC_ARM` env gate) ships inside #205.
8. **SNOW-2 (§1) is the post-#205 acceptance bench** — it stretches injection AND agentic modes across the newly profiled pipeline, on 10K/50K/500K, against ERB's published one-shot baselines (#93). The §4 R@1 program (E1–E5) then becomes the next balancing wave: if the AND-mode fingerprint holds at 850K, the 28%-recall cliff stops being a weight-tuning problem and becomes a routing-architecture fix.

**End state:** every knob in `docs/specs/2026-06-09-retrieval-profiles.md` Layer 1–3 carries a measured value with a result JSON behind it; #202–#204 closed by runs, #205 closed by implementation, #206 closed by decision, and SNOW-2 + the R@1 program queued as the next evidence loop.

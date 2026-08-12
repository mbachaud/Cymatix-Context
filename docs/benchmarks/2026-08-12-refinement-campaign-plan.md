# Refinement campaign 2026-08 — delivery-first measurement, packet shape v2, static→dynamic

**Thesis (from the full benchmark inventory, Appendix A):** the encoder-removal
line is closed or closing — splade REMOVE (#333 closed), sema null (#337
closed), PKI active-null at 100k (blob B3 pending, #334), dense refuted as the
semantic lever (#260 reframed). What every recent receipt now points at is the
**delivery pipeline and the instruments**: r@12 moved 0.800→0.867 in the PKI
A/B while delivered-gold sat flat at 0.500 and pool_delivery_gap *widened*;
the Gemma campaign proved the classifier's hard cap of 5 (not the 7k budget)
binds delivery; know_emit_floor's answer-hollow reproduces verbatim on a
second box at rank 1 (chunk granularity, not compression); and `recall_at_k`
is structurally blind to every post-fusion stage. **You cannot refine what
the receipts cannot see — so measurement ships first, dynamics second,
storage closure third, semantic lift fourth.**

Protocol defaults for every arm below: standard bench profile
(`BASELINES.md` — daemon ON, vector LRU off, per-box workers x4/x2),
order-reversed or reversed-base passes, `--per-query` basis, frozen tag +
BASELINES row before any cross-campaign comparison, pre-registered gates.
Primary metric everywhere: **delivered-gold** (rank r@12 demoted to
secondary/diagnostic).

---

## Phase 0 — instrument upgrade (everything else is gated on this)

| # | work | receipt gate | anchors |
|---|---|---|---|
| 0.1 | **Delivered-gold + pool_delivery_gap as served fields** — promote from ladder-only to `/context/packet` telemetry and the receipt schema; same promotion `coordinate_confidence`/`file_coverage` already got out of `notes` prose | field present; ladder and served values agree on a 30-needle replay | #335, ledger action 7 |
| 0.2 | **Content-granular containment metric** — replaces dead `body_has_answer`; must NOT couple to legibility headers (the 2026-07-05 failure mode); scores answer-text presence in the delivered window, not gene identity. Closes both error directions: know_emit_floor (gold delivered, answer absent) and proxy_port (gold missed, answer delivered) | metric disagrees with gene-granular delivered-gold on exactly the known specimens (know_emit_floor hollow, proxy_port inverted) | 0017 receipts, tagger-A/B doc |
| 0.3 | **Final-order basis everywhere** — `final_recall_at_k` + delivered basis published alongside `recall_at_k` in every harness; shipped basis marked diagnostic-only | rerank replay shows +6.7pp on final basis, +0.0 on legacy — both visible | 2026-08-07 rerank receipts |
| 0.4 | **Per-arm rank hashing** for the concurrency wobble — byte-hash of ranked ids per query per arm, so G5's one-sided deviation (p≈1e-11, 26/63 levels) becomes attributable (pool-state vs encode) | hash instrumentation reproduces the c=8 deviation with attribution | #339, 0018 G5 |
| 0.5 | **Expression-cap visibility** — packet signals when the assembly cap (not budget) truncated delivery | Gemma replay: arms distinguishable from outside | Gemma campaign doc |

## Phase 1 — dynamics where statics bind (receipts already justify these)

Ordered by measured impact; each ships dark/byte-identical with an arm.

1. **Classifier caps → config, and swept** (#205): `assembly_max_genes_cap`
   2/5/6/8 are code constants; the factual cap of 5 is the proven binding
   stage. Sweep {5, 8, 12} against delivered-gold + containment (Phase 0.2)
   on erb_100k + dogfood-clean.
2. **ANN gate `min_genes` delivered-gold sweep**: the 15-vs-23/30 tension is
   the single largest known delivery loss. Sweep min_genes {1, 3, 6} × trim/
   headroom. Hard precondition from the ledger: cap the non-ANN branch's
   candidate list first.
3. **Splice char-target dynamics** (#336): replace 25200/n flat division with
   candidate-count-aware targets that preserve the 8400-char fast path; pair
   with the headroom-vs-trim **answer-quality** A/B (containment metric now
   exists) — the p95 16-17s tail is the biggest latency lever in the ledger
   and its quality cost is still unmeasured.
4. **Abstain floors → RRF-scale** : TIGHT/FOCUSED floors are additive-era
   calibrated, never re-fit after the #247 flip (ratio-only today). Refit or
   formally demote to ratio-only with a receipt.
5. **Know-logistic recalibration or demotion** (#287): frozen 2026-07-06 fit,
   β1 inversion on the RRF refit, council 0/5 would_ship. Either a
   scale-invariant refit on a located_n≥1000 RRF-era set with the fragility
   gates, or demote `[know]` confidence to ratio-only until then.
6. **`dense_pool_size` 0=auto** (`max(500, N/200)`) + **pool-size-adaptive
   admission caps** (#344) — validate on the 141-needle set with the
   static-miss class tracked per needle (the top-M-union safety condition).
7. **Sema stored-vector auto-gate** is shipped; extend the same
   evidence-not-config pattern to `splade` (empty-table ⇒ skip re-creation —
   the full469 control-arm finding).

## Phase 2 — storage closure + blob-scale confirmations (with Joe / B-cards)

1. **B3 blob-scale PKI/SPLADE pass** (#334, cc-exchange B3): `no_pki`,
   `no_splade_no_pki` arms at the pinned tag on the collaborator beds, graded
   by delivered-gold — **plus one targeted KV/config-lookup query class arm**
   (PKI's designed class; 30 needles of "what is the value of X" shape) so
   the REMOVE verdict can't be ambushed by the one class the needle family
   under-samples.
2. **Tags gate + arm** (ledger action 6 second half; #355/#356): copy the
   PKI-gate template (single-seam bool → shared kwargs-builder → signal-
   absence arm). Split tag_exact vs tag_prefix for attribution.
3. **Write-side PKI gate + compaction path** if B3 confirms: read gate ≠
   storage win; the 8.56GB needs `pki_write_enabled=false` or scheduled
   `/admin/compact-pki`.
4. **FTS external-content rebuild** (#338, −8.67GB signal-neutral) → the
   ~28GB RAM-cacheable blob → re-run the c-curve (the 500k c=2 super-linear
   degradation was RAM pressure).
5. Cleanups with receipts already in hand: `splade_df_cap_fraction` 0.10
   default (quality-neutral A/B exists), entity_graph flag-leak fix + REMOVE
   arm, dead `peak_width` config (#357), cymatics spread re-run (#354).

## Phase 3 — semantic lift pilot (ingest-side expansion, encoder-free query path)

From the 2026-08-12 two-agent research convergence (receipts in #260):

1. **Pilot**: doc2query---style filtered question generation on the erb_100k
   carve — local 4-8B model, 5-10 questions/chunk, filter against source
   (BGE-M3 cosine or CPU reranker at ingest), dedup, index into a weighted
   FTS5 column (BM25F lesson: inside the lexical arm, not a new RRF tier).
2. **Acceptance test, pre-registered**: the fixed 62-needle static-miss set
   (B4's set). Gate: ≥15/62 recovered at k=12 with non-negative movement on
   the non-paraphrase slice and index growth ≤ +20%. Sweep questions/chunk
   {3,5,10} × keep-fraction {1.0, 0.5, 0.3}.
3. **Fallback lane**: inference-free SPLADE-doc (OpenSearch doc-v3 pattern) —
   reuses the existing splade ingest artifacts with a tokenizer+IDF query
   side; needs an impact side-table, so it only proceeds if (1) leaves
   residual paraphrase misses.
4. **Synonym map → receipt-gated weighted entries**: automate leave-one-
   entry-out over the per-needle receipts (erb_015 prune now; erb_039 is a
   deliberate rerank-era feeder).

## Phase 4 — packet shape v2 (after 0.x fields have soak time)

- `know`/`miss` carries the containment-granular confidence (not just
  gene-granular gold), the expression-cap signal, and the recalibrated (or
  demoted) confidence from Phase 1.5.
- `pool_delivery_gap` served per query; `refresh_targets` unchanged.
- Legibility headers stay window-side; the packet gains nothing prose-scraped
  (the coordinate_confidence precedent is the rule).

## What is deliberately NOT in this campaign

- Dense removal from the shipped default (latency device, KEEP-LOAD-BEARING
  until Phase 1.2's lex-branch cap exists and is measured).
- Static query-side embeddings (below-BM25 on retrieval benchmarks; research
  verdict: skip).
- Determinism kernels (measured NO-GO, 34× capacity collapse; wobble work
  goes through Phase 0.4 instrumentation instead).
- SCBench third campaign (sol) — reserved, weekly-usage window permitting;
  luna+terra both read no_detectable_difference with gates passing, so the
  next scbench spend should wait until Phase 1-2 deltas exist to detect.

---

# Appendix A — benchmark inventory (as of 2026-08-12)

Compiled from docs/benchmarks/, benchmarks/dogfood/erb/receipts/, docs/research/data/,
benchmarks/results/, the scbench worktree, and cc-exchange threads. Grouped by family;
one line per campaign: what/where/headline.

## (a) Retrieval recall — needle / SIKE / located

- **SIKE N=10 vs KV-harvest N=50/1000 (2026-04-13)** — fresh 17,961-doc genome; 10/10 curated vs 6-12% synthetic; NIAH mis-fit diagnosis → dimensional-lock. `BENCHMARK_RATIONALE.md`, `benchmarks/results/needle_*.json`.
- **Dimensional-lock 4-variant grid (2026-04)** — recall-vs-axis-count curve shape as diagnostic. `benchmarks/results/dimensional_lock_*.json`.
- **Layer-3 ERB cross-fixture (2026-05-20→28)** — 10K→850K: recall@10 60%→28% corpus-scale erosion; SPLADE 0pp on prose (→#164). `BENCHMARKS.md` §Layer 3.
- **SIKE bedsweep #221 run-1 (2026-07-03)** — xl/erag10k/erag50k, 50 needles: 0.64/0.80/0.82 gold-delivered; `body_has_answer` exposed as dead metric. `2026-07-05-sike-bedsweep-issue-resolutions.md`.
- **SIKE Run-2 fts-depth × fusion (2026-07-06)** — depth flat {48,200,500}: rank-squeeze, not pool starvation. `2026-07-06-sike-run2-fts-depth-fusion.md`.
- **RRF-vs-additive rebaseline (2026-07-06)** — measured basis for the `fusion_mode=rrf` default flip (#247). `2026-07-06-rrf-default-rebaseline.md`.
- **Splice-floor fix remeasure (2026-07-06)** — +0.18→+0.24 content_has_answer (1000-char floor truncated 9-12 needles/arm). `2026-07-06-splice-fix-remeasure.md`.
- **located_n1000 (2026-07-06/13)** — labeled substrate for know-logistic fits; r@1 ≈0.20-0.225. `docs/benchmarks/data/2026-07-06-located-n1000-*.jsonl`.
- **Dogfood baseline + clean-bed rerun (2026-07-28/29)** — 0.667@12 / 1.000@50: gold in pool, ranked deep; decontamination moved MRR only. `2026-07-28-dogfood-baseline.md`, `2026-07-29-clean-bed-rerun.md`.
- **Recall-at-scale (2026-08-04)** — 100k→829k carves, 141 needles: 0.60→0.40; gold never absent from pool; static-miss class identified; 93k-294k scored genes/query. `2026-08-04-recall-at-scale.md`.
- **Full-469 baseline on splade-less bed (2026-08-06)** — r@12 0.676, med 2.0, p50 18.9s, 2.65h c=1; 99/469 (21%) static-miss. `receipts/full469_nosplade_c1.json`.
- **Shard-vs-blob rank-merge receipts (2026-07-11→16)** — merge is the degrader, not lexical IDF (#275 umbrella). `docs/research/data/2026-07-1*-shard_receipt_*.json`.
- **Semantic probe / dense cliff (2026-07-11/12)** — dense median rank 545→3724 at 10k→50k with pool_present 1.000; fused rides lexical. + cc-exchange 0001/0004/0011/0012.

## (b) ERB official answer-quality

- **ERB 829k blob reproduction (2026-07-10)** — 500q: 47.2% end-to-end / 55% gold-delivered (own trinary judge — quote as a pair). `2026-07-10-erb-blob-829k-reproduction.md`.
- **ERB official re-judge (2026-07-12)** — runbook complete, NEVER RUN (no LLM_API_KEY); the submittable-number path. `2026-07-12-erb-official-rejudge-runbook.md`.
- **Gemma expression campaign (2026-07-30)** — 10 arms, zero delivery flips: binding stage is the expression cap of 5, not retrieval. `2026-07-30-gemma-expression-campaign.md`.
- **AssetOpsBench (2026-07-13)** — NO-GO (no prose corpus; rubric credits tool sequencing). **Faithfulness/J-lens (2026-07-06)** — premise dissolved (17/23 were the body_has_answer artifact).

## (c) Ablation ladders / layer A/Bs

- **Dogfood 14-arm ladder (2026-07-28)** — fts_depth and dense_w inert; only fusion mode and rerank move rank.
- **Cymatics ablation + controls (2026-07-29)** — Gaussian spread does nothing (18/18 identical ranks); flag stays. Spread re-run owed (#354).
- **Tagger A/B (2026-07-31)** — 12/18→18/18 but ⅔ of mechanism = escaping CPU tag flood; 4/6 recovered needles answer-hollow (chunk granularity).
- **Retrieval-layer ledger, 7 arms (2026-08-06)** — THE evidence base: dense=latency device; splade neutral; cymatics claim refuted; entity_graph flag leak; sema no-op. `2026-08-06-retrieval-layer-ledger.md` + `receipts/ablation_ladder_100k.json`.
- **Splade curve, 4 scales (2026-08-06)** — never crosses zero; REMOVE for ERB-class serving. `receipts/splade_ab_*.json`.
- **Sema A/B backfilled (2026-08-06)** — null even when fed. `receipts/sema_ab_100k_run{1,2}.json`.
- **Synonyms A/B (2026-08-06)** — no-synonyms +0.033 both orders; movers named (erb_015, erb_039).
- **PKI A/B, 3 reversed-base passes (2026-08-12)** — ACTIVE null: 0.800→0.833→0.867 r@12, delivered-gold flat 0.500, gap widens. `receipts/pki_ab_100k_run{1,2,3}*.json` + ledger Addenda 5-6.
- **Rerank probe + #341 wiring (2026-08-04→07)** — +6.7pp final-order recall both orders; delivered-gold flat; ships default-OFF. `2026-08-07-rerank-wiring-receipts.md`.
- **blend_mode / #255 class-gate / #260 rrf-gate receipts (2026-07-11→16)** — scale-invariance substrate; the per-class override-map template; top_m marginal-not-material.
- **splade_df_cap 0.10 (2026-08-04)** — quality-neutral; default still 0.0. **Splice headroom-vs-trim** — ratio cells exist; answer-quality unmeasured. **Storage audits** — 8.67GB FTS shadow; path to ~28GB.

## (d) Latency / concurrency / perf

- **Perf slice 2 (2026-08-02)** — server couldn't serve c=2 at all; 3 defects fixed; cold start 14.0s (12.8s model load).
- **ERB step curve (2026-08-03)** — √N then cliff at 829k (RAM pressure). **Post-fix ladder (2026-08-04)** — cliff gone, 829k 4.3× faster (stats() off hot path, tag_prefix index, SPLADE covering index).
- **Fork-1 encoder daemon (2026-08-05)** — GO: bit-identical, +11.7% c=1, GPU 51.4 bundles/s. **Worker sweep (2026-08-05/06)** — daemon moved the GIL wall; x4 sweet spot on 8C/16T.
- **Determinism profile (2026-08-06)** — NO-GO: 34× capacity collapse AND c=8 recall deviation still fired ⇒ wobble partly retrieval-timing. `receipts/encoder_daemon_capacity_gpu_det.json`.
- **Daemon capacity incl. rerank (2026-08-07)** — GPU ~154-327 pairs/s with both bars preserved.
- **Joe GB10 15-arm snapshot (2026-08-07/08)** — x4/x8 does NOT reproduce on Grace; ON beats OFF 1.16-1.31×; **G5: recall validity one-sided fail 26/63 levels (p≈1e-11), uninstrumented** → Phase 0.4. cc-exchange 0018.
- **Joe e4b tagger bed (2026-08-07)** — 5.31 genes/min (2.41× max rig); stronger model stubs less; r@12 0.933 vs 0.600; know_emit_floor hollow reproduces at rank 1. cc-exchange 0017.
- **Standard bench profile decision (2026-08-11)** — daemon ON, per-box workers; shipped defaults untouched. `BASELINES.md`.

## (e) SCBench agent A/Bs

- **luna (2026-08-08)** — model-free treatment; no_detectable_difference; all 7 safety gates pass (tokens 1.08×, time 1.03×). **terra (2026-08-11)** — same verdict, CI [−0.013, 0.0], tokens 1.13×. **sol** — reserved. `benchmarks/scbench/campaigns/` (scbench worktree) + `BASELINES.md` rows.

## (f) ContextBench / code-track

- **Step-0 master vs BM25 (2026-06-18)** — packet 0.655 vs 0.484 at fewer tokens. **cAST chunking (2026-06-27)** — 0.804 (+32pp over BM25); the chunking lever.
- **WS2 symbol graph (2026-06-28)** — packet +2.1pp, fp@27k −14pp (unranked expansion floods budget). **WS3 cap+PageRank** — cap recovers +6.7pp; sympy held-out −7.6pp → dark-ship.
- **Arm C held-out 266 tasks (2026-07-20)** — +2.8pp line / +3.8pp sym, reverses the earlier held-out read; dark-ship kept (prose regression, 2026-07-19 SIKE arm).
- **CodeRAG-Bench / RepoBench-R** — runbooks designed, not executed. **External-bench plan (2026-07-01)** — ContextBench license unstated → internal-only.

## (g) Everything else

- **Know-logistic first fit (2026-07-06)** — ECE 0.74→0.04 on n=1617; emit_floor operator-set 0.45. **RRF-era refit (2026-07-13)** — β1 inversion, 0/5 would_ship, never shipped (#287).
- **2026-04 harness era** — babilong, budget-zone, cache-hitrate, compression, multi-needle, PLR smoke, AA suite, etc. `benchmarks/results/*.json`.
- **cc-exchange embedding-upgrade (35 turns)** — five-model cross-provider retrieval ceiling; global-IDF refuted; lever = candidate pool. **spark-erb-receipts cells 1-5** — dense cliff, RRF gate marginal, admission decomposition: fts-depth is the lever, dense zero at fetchable depth.
- **Fixture/gold governance** — `GENOME_FIXTURE_MATRIX.md`, `MULTI_VALID_GOLD.md`, gold pack manifest.

# Appendix B — static values with dynamics owed (the full list)

From the ledger's static→dynamic map plus the sweep behind this plan:

1. `splade_enabled` query gate + per-class map (superseded for ERB serving by REMOVE; gate reserved for paraphrase-heavy classes)
2. Generic `resolve_tier_enabled` corpus-size gating (with the dense/lex-cap precondition)
3. Pool-size-adaptive admission caps (top-M-union; static-miss safety)
4. `dense_pool_size` 0=auto (`max(500, N/200)`)
5. #205 classifier caps as code constants (`query_classifier.py:181-218`) — the proven delivery binder
6. Sema auto-gate (shipped) — pattern to extend to splade table re-creation
7. Splice char targets `25200/n` flat division (`context_manager.py:552-580`) — defeats the fast path, owns the p95 tail
8. ANN gate constants (`ann_similarity_threshold=0.58`, `min_genes=1`) — delivered-gold tension
9. `[know]` logistic frozen at 2026-07-06, n=1617, β1 inversion on refit (#287)
10. Abstain floors additive-calibrated, never re-fit post-RRF (ratio-only today)
11. `splade_df_cap_fraction` 0.0 default despite quality-neutral 0.10 receipt
12. `rerank_enabled_by_class` ships empty (the intended enablement path)
13. `symbol_expansion_cap=8` unswept on held-out; PageRank personalization unwired (uniform)
14. `pki_enabled` read gate only — write gate/compaction owed if B3 confirms; **tags have no gate at all**
15. Executor workers: static 2 vs per-box optimum (4 x86 / 2 Grace); w=1 never bracketed

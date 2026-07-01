# EXPERIMENT SPEC — Semantic-aware routing + dense-dominant fusion (the wiring lever)

**Status:** DRAFT — PRD-gated (touches retrieval behavior; see project PRD-first workflow). Write `docs/prds/2026-06-02-semantic-wiring-arm.md` and obtain sign-off BEFORE implementing.
**Worktree (live, runs the bench):** `F:/Projects/helix-context/.claude/worktrees/vibrant-easley-73d68a` (NOT master — master is stale 0.5.0).
**Author of map:** grounded codemap + adversarial verdicts, re-verified against the live worktree 2026-06-02.
**Owner of run:** Raude. **Mode at spec time:** READ-ONLY map; this spec authorizes a code change + a fresh daemon for the *experiment arm only* (do not restart the currently-wrapped baseline daemon mid-bench).

---

## 1. Objective + hypothesis

**Objective.** Recover the 9 semantic golds that pure global-dense ranks top-10 but the daemon delivers at 0, lifting **semantic@10 from 3 → ~12** (3% → ~9.6%, ~4×) with **no GPU, no re-embed, no re-index**.

**Hypothesis (it is WIRING, not the encoder).** The cross-join probe (`F:/tmp/rank_distribution_probe.py` → `rank_distribution_probe.json`) shows pure global-dense reaches 9/125 golds @10 and the daemon reaches 3/125 @10 — and those two sets are **DISJOINT** (the daemon's 3 are lexical/tag/filename golds dense misses). The 9 dense golds are lost in two stacked wiring stages, not by the encoder:
- **5 ROUTING-DROPPED** — the gold shard is never fanned out, so the gold never enters the fused pool. Dies at the literal LIKE gate `ShardRouter.route` (`shard_router.py:365-403`, verified).
- **4 FUSION-DEMOTED** — gold enters the per-shard pool but the additive sum (`gene_scores[gid] += cosine * dense_additive_weight`, `knowledge_store.py:2086-2093`, verified) lets stacked lexical+SPLADE+tag contributions outweigh dense's 4.0×cosine, pushing it to ranks 11/11/40/45.

**Why the combination (each half is null alone — confirmed by code path + probe).** Routing controls **pool membership** (the 5); fusion weight controls **rank within pool** (the 4). Broaden-alone (`HELIX_ROUTE_ALL=1` full fan-out — which does NOT exist as code; was an ad-hoc test) recovered the 5 into the pool but they re-buried at ranks 11-45 because the additive sum still demoted dense. Dense-weight-alone (`4→10`) lifted the 4 already-in-pool golds but could not rank the 5 that routing never admitted. **Only TOGETHER:** broaden so all 9 enter the pool, THEN dense-dominate so dense's cosine ordering clears the lexical stack into @10.

**k-dependence (scope boundary).** @10 is the WIRING win (pure-dense 7.2% vs fused 2.4%). @200 is ENCODER GEOMETRY (pure-dense 28.8% vs pooled 24%, only +5pp headroom) and is **out of scope** (§7) — this spec targets @10 ordering inside the existing pool and treats @200 as a no-regression guardrail.

---

## 2. The change — the COMBINATION, scoped to `query_type=="semantic"`

A new per-call `query_type` parameter threaded end-to-end, AND-gated with an env flag `HELIX_SEMANTIC_ARM`, that simultaneously does BOTH of the following **only when `query_type=="semantic"` and the flag is set**, leaving all other classes **byte-identical**:

### (a) Broaden routing — `ShardRouter.route`
**Hook (verified):** `shard_router.py:365-403`. When semantic, bypass the LIKE scan and return the full healthy set via `self.known_shards()` (the exact target already used by the empty-terms fallback at line 378 — `shard_router.py:359`). This guarantees the 5 routing-dropped golds' shards enter the fan-out.
- Pair with a per-shard fetch-depth lift (see §3, `per_shard_fetch` at `shard_router.py:492`) so a routing-recovered shard's intra-shard-deep dense gold survives to Stage B. **Confirm depth need empirically** (open question, §6) — a routing-recovered gold may sit at intra-shard rank > 4×max_genes because the query is lexically weak in that shard.

### (b) Dense-dominant ADDITIVE-PRESERVING fusion — `Genome.query_docs`
**Hook (verified):** `knowledge_store.py:2086-2093`, specifically line 2088:
```python
contribution = float(cosine) * self._dense_additive_weight
```
When semantic, swap `self._dense_additive_weight` for a higher scoped value via a new knob `semantic_dense_additive_weight` (default ~12, sweep 8/12/16/20 — §6). Scale **ONLY** the dense term.

**"KEEP BOTH" constraint (load-bearing).** The merge at `knowledge_store.py:2089` is `gene_scores[gid] = gene_scores.get(gid, 0.0) + contribution` — purely additive; it NEVER zeroes lexical. The lexical/tag/SPLADE tiers use independent constants (fts5_weight 3.0, splade_weight 3.5, tag_exact_weight 3.0, lex_anchor_weight 1.5 — `config.py:371-376`, verified) and are untouched. So raising ONLY the dense multiplier preserves the 3 disjoint lexical/tag/filename golds' absolute scores → result is ~3 (kept) + up-to-9 (recovered) ≈ 12, **not a 9-for-3 trade**. The dense contribution has no cap (unlike fts5/splade), so the weight is freely scalable.
- The final per-shard sort `sorted(gene_scores, key=gene_scores.get, reverse=True)[:limit]` (`knowledge_store.py:2450`, verified, additive branch) consumes the boosted `gene_scores` directly.
- Cross-shard Stage B is transparent: `last_query_scores` IS the per-shard additive score under additive mode (`knowledge_store.py:2338`), feeding `corrected = raw * m_shard` (`shard_router.py:636-642`) and `* DOC_TYPE_BOOST` (1.15, `shard_router.py:654-661`). These multipliers are additive-preserving and orthogonal — **leave untouched** (they actually protect the 3 lexical/README golds). RRF is only a tiebreaker (`shard_router.py:669-676`). So a Stage-A dense boost flows straight into the cross-shard sort.

**Both must fire together, gated by the SAME predicate.** Shipping half is null (refuted-hyp #5 routing-solo, #6 fusion-solo).

---

## 3. Exact insertion points + query_type threading

`query_type` does NOT exist anywhere in `helix_context` today (grep-confirmed in verdicts). It must be threaded fresh from the request body down to BOTH the router (for route-broaden) AND each shard's `query_docs` (for the dense weight). The thread path (all line refs verified live):

| # | File:symbol | Line (verified) | Change |
|---|---|---|---|
| 1 | `helix_context/server/routes_context.py` : `fingerprint_endpoint` | `652-746`; `_retrieve` call `737-746` | Parse `query_type = str(data.get("query_type","")).lower()` near line 661; pass `query_type=query_type` into the `_retrieve(...)` call at 737. (Bench will send the needle's type — §4.) |
| 2 | `helix_context/context_manager.py` : `_retrieve` | def `2078-2089`; `query_docs` call `2135-2144` | Add `query_type: Optional[str] = None` to the signature; forward `query_type=query_type` into the `genome.query_docs(...)` call at 2135. (The `query_docs_ann` branch at 2123-2133 is NOT taken in sharded mode — adapter `_dense_embedding_enabled=False`, `sharding.py:212`, verified — but add the kwarg there too for parity/safety.) |
| 3 | `helix_context/context_manager.py` : `build_context` callers of `_retrieve` | `1191-1195`, `1201-1205`; classifier at `1079` | Add a `build_context` param `query_type: Optional[str]=None` (mirror the existing `caller_model_class` threading at def `1034`); pass it to both `_retrieve` calls. On `/context`, fall back to a semantic derivation from `classifier_result` (computed at 1079) — but note `classify_query` has NO "semantic" class (§4), so the bench override is the primary source. |
| 4 | `helix_context/sharding.py` : `ShardedGenomeAdapter.query_docs` | `227-243` (passthrough `234`) | **No change** — `(*args, **kwargs)` forwards `query_type` verbatim to `query_genes`. (Verified.) |
| 5 | `helix_context/shard_router.py` : `ShardRouter.query_genes` | def `407-414`; route call `438`; fan-out `513-520`; per_shard_fetch `492` | **Pop `query_type = kwargs.pop("query_type", None)` at the top of `query_genes` BEFORE fan-out.** Pass it to `self.route(domains, entities, query_type)` at line 438. For the dense weight, pass it explicitly into the per-shard `shard.query_docs(..., query_type=query_type)` call at 513-520. **LANDMINE (verified):** `kwargs` is forwarded to `shard.query_docs` at line 519, and `KnowledgeStore.query_docs` (knowledge_store.py:1468-1478) has NO `**kwargs` — an un-popped `query_type` raises TypeError caught at `521-527`, which silently retries dropping ALL kwargs (use_harmonic, etc.) for every shard on every semantic query. **You MUST pop it from generic kwargs and pass it as an explicit named arg that the new `query_docs` signature accepts.** Optionally lift `per_shard_fetch` (line 492) when semantic. |
| 6 | `helix_context/shard_router.py` : `ShardRouter.route` | `365-403` | Add a 3rd param `route(self, domains, entities, query_type: Optional[str]=None)`. When `query_type=="semantic"` and arm-on, `return self.known_shards()` (line 359) before the LIKE scan at 383. |
| 7 | `helix_context/knowledge_store.py` : `Genome.query_docs` | sig `1468-1478`; dense merge `2086-2093` (weight at `2088`) | **Add `query_type: Optional[str]=None` to the signature** (this closes the TypeError-fallback landmine). At line 2088, select the weight: `w = self._semantic_dense_additive_weight if (query_type == "semantic" and self._semantic_arm) else self._dense_additive_weight`. Lexical/tag/SPLADE tiers untouched. |

**Threading note:** the dense-weight change and the routing change live in two different objects (per-shard `KnowledgeStore` vs `ShardRouter`) both reached through `query_genes`. The single chokepoint that sets both is `_retrieve → adapter.query_docs(**kwargs) → query_genes` (pop for route) → `shard.query_docs(query_type=...)` (for weight).

---

## 4. Env-gated flag / config arm (clean A/B)

**Env gate:** `HELIX_SEMANTIC_ARM=1` (default unset = byte-identical baseline). Read it once (recommend in `build_context` / `fingerprint_endpoint` or at genome construction) into an instance flag `self._semantic_arm`. Mirror the existing env-gated precedent `HELIX_RERANK_POOL` (`context_manager.py:1149`, default `0` = byte-identical). AND-gate with `query_type=="semantic"`.

**Config knobs (declare in `RetrievalConfig`, `config.py`, mirror the `dense_additive_weight` pattern at line `384` + from_dict at `815`):**
- `semantic_dense_additive_weight: float = 12.0` — the scoped dense weight (sweep target).
- `semantic_broaden_routing: bool = True` — route→`known_shards()` for semantic.
- (optional) `semantic_per_shard_fetch_mult: int = 2` — extra depth lift if §6 depth check requires it.

Wire through `from_dict` (config.py:803-818 pattern) and into the genome construction kwargs in the `open_read_source` factory block (`context_manager.py:516-518`, verified — this fans the SAME kwargs to the solo Genome and every per-shard Genome via `ShardRouter(**genome_kwargs)` at `sharding.py:202` → `Genome(path, **self._genome_kwargs)` at `shard_router.py:356`, verified). One config block covers all shards.

**`query_type` origin for the bench (decision: option (b), bench-injected — cleanest dual-gate fidelity).** `classify_query` (`query_classifier.py`) emits only `arithmetic/factual/procedural/multi_hop/default` — there is NO "semantic" runtime class. The needle DOES carry `n['type']` (`bench_enterprise_rag.py:96`). So:
- **One-line bench edit:** in `bench_enterprise_rag_recall.py:46-49`, add `"query_type": n["type"]` to the POST body. This is experiment-only (production callers won't send it; a runtime semantic-detector is a separate future track — acceptable, the goal is to PROVE the lever).
- **A/B arms:** Baseline = `HELIX_SEMANTIC_ARM` unset (or bench omits `query_type`). Experiment = `HELIX_SEMANTIC_ARM=1` + bench sends `query_type`. Optionally carry the weight override in a dedicated TOML arm (e.g. `helix_splade_on_mg10_sembroaden.toml`) so baseline-vs-experiment is a two-config + flag toggle.

---

## 5. Dual gate + metrics

**Subset (verified — not a file):** "semantic-125" = `load_needles(question_types=["semantic"])` from `benchmarks/bench_enterprise_rag.py:81-120` (reads `F:/Projects/EnterpriseRAG-Bench-main/questions.jsonl`, filters `q['question_type']=="semantic"`; gold via `expected_doc_ids → uuid_index.json`). n=125 (121 have an in-corpus vector; 4 no-vec per probe).

**Eval harness (verified):** `benchmarks/bench_enterprise_rag_recall.py` POSTs `/fingerprint`, finds first-gold rank, `recall_at(k)` uses `r < k`.

**CRITICAL eval-depth requirement (verified):** at `score_floor=0` (bench default), `eval_budget = max_results` (`routes_context.py:718`). At `--k 10` the daemon only evaluates 10 candidates — fusion-demoted golds at ranks 11-200 are invisible and routing/fusion cannot be distinguished. **Run at `--k 200`** so the @200 pool is visible; derive @10 by post-processing `rows[].retrieved` for `rank<10`. (`recall_at` truncates `fps` by `max_results`, so a single `--k 200` run yields both @10 and @200.)

**Exact commands** (run from the worktree; bench venv is `/f/tmp/bgem3_gpu_venv` per project memory; daemon at `http://127.0.0.1:11437`):
```bash
# BASELINE (arm off) — single deep run, derive @10 and @200 from rows
python benchmarks/bench_enterprise_rag_recall.py \
  --types semantic --max-questions 200 --k 200 --label sem125_baseline

# EXPERIMENT (arm on: start the experiment daemon with HELIX_SEMANTIC_ARM=1 +
# the sembroaden TOML; bench sends query_type per the one-line edit)
python benchmarks/bench_enterprise_rag_recall.py \
  --types semantic --max-questions 200 --k 200 --label sem125_sembroaden
```
Results land in `benchmarks/results/recall_<label>_<ts>.json` with full per-row `retrieved` lists (`bench_enterprise_rag_recall.py:62-66, 86-93`).

**Gate (BOTH must pass):**
1. **semantic@10: 3 → ~12** (recover the wiring-lost 9, KEEP the 3). Threshold: ≥ ~10 (recover ≥7 of 9) AND the baseline 3 lexical golds still present @10.
2. **No regression:** semantic@200 not lower than baseline (geometry guardrail); other 7 query types byte-identical (gate via recall-identity, below).

**Recovery split (required report).** Write a small post-processor (does NOT exist yet) that joins `recall_<label>.json` `rows[].retrieved` against `F:/tmp/rank_distribution_probe.json` `rows[]` by question id, mirroring the won/lost accounting in `F:/tmp/score_rerank_capture.py:46-91`. Classify each baseline-lost-but-dense-top10 gold:
- **of-5-routing recovered** = gold ABSENT from baseline @200 list (never in pool) but present @10 in experiment.
- **of-4-fusion recovered** = gold present in baseline @200 at rank ≥10 but @10 in experiment.
- **3-lexical survived?** = baseline daemon @10 set still ⊆ experiment @10 set.
- Reuse `last_tier_contributions` (carried through `merged_tier`, `shard_router.py:613`, verified) to label which tier won each recovered gold.

**Recall-identity check (gate-4 discipline, verified oracle).** `HELIX_SHARD_WORKERS` (`shard_router.py:65-79`) gates serial vs `ThreadPoolExecutor.map` fan-out, order-preserving — serial is the reference oracle (`tests/test_shard_router.py::test_parallel_fanout_matches_serial_byte_for_byte`). With the arm ON, run a **non-semantic** type (e.g. `--types basic`) and assert **byte-identical** ranked ids + scores vs baseline (0 delta), proving the route() shard set and per-shard dense weight (4.0) are untouched for non-semantic queries. Run at both `HELIX_SHARD_WORKERS=1` and `>1` to confirm determinism holds under broadened fan-out.

---

## 6. Risks

- **Pendulum swing (demoting the 3).** Too high a `semantic_dense_additive_weight` could over-promote dense topical neighbors and indirectly push the 3 lexical/tag/filename golds below the @10 cut even though their absolute scores are preserved (more dense docs out-rank them). Mitigation: sweep 8/12/16/20 on semantic-125; **pick the point that maximizes recovered-9 while survived-3 == 3 and @200 non-regressing**. Verify the 3 golds' tiers (fts5 3.0 / splade 3.5 / tag_exact 3.0 / filename_anchor 4.0) stay above the @10 cut. The solo-null `4→10` result does NOT bound the paired value — re-sweep paired.
- **Broaden latency vs budget.** Full fan-out to `known_shards()` at 105-shard scale raises shard count fanned per query. Project headroom is ~6-11× vs the 600s budget, but confirm the semantic-125 wall-clock stays in budget; `HELIX_SHARD_WORKERS>1` is needed for full-fan-out latency (bench runs serial by default). If it blows the budget or the 104GB commit ceiling, fall back to a dense-prefiltered shard subset (cheap centroid cosine) rather than literal all-shards.
- **Route-all-alone is null — do NOT ship half.** Broaden without the dense weight re-buries the recovered golds at ranks 11-45; dense weight without broaden cannot rank the 5 absent golds. Both must be gated by the SAME predicate and shipped together.
- **Depth truncation.** A routing-recovered shard's dense gold may sit at intra-shard rank > 4×max_genes (`per_shard_fetch = max_genes*2` then *2 internally, `shard_router.py:492`) and be cut before Stage B. Verify the 5 routing golds surface within depth once routing broadens; lift `per_shard_fetch` for the semantic arm if not.
- **Kwarg-drop landmine (already mitigated in §3).** If `query_type` is left as a generic kwarg, the TypeError fallback at `shard_router.py:521` silently drops it AND use_harmonic for every shard — the change becomes a no-op AND breaks harmonic. Must pop in `query_genes` and add to `KnowledgeStore.query_docs` signature.

---

## 7. Out of scope

- **The @200 geometry bulk.** @200 recall (pure-dense 28.8% vs pooled 24%, +5pp headroom) is encoder near-neighbor dilution at 850K scale — a **separate track** (re-embed / hard-neg fine-tune / query expansion), gated on GB10 GPU-embed. This spec does not move @200; it only guards against regressing it.
- **A runtime "semantic" detector.** Adding a real text-based semantic class to `classify_query` is the unsolved paraphrase-detection problem; this experiment injects the bench's ground-truth `query_type` to PROVE the lever first. A production detector is follow-on.
- **Reranker / cross-encoder.** The `rerank_enabled` cross-encoder track is independent and runs only under profile `quality` (this experiment runs under `balanced`, rerank never fires on `/fingerprint`). Do not entangle.
- **Switching `fusion_mode` to rrf.** Must stay `additive` (verified default, `config.py:369`) — the dense-weight lever only applies in the additive branch; the rrf branch ignores `dense_additive_weight`.

---

## 8. Rollout / rollback

- **Default OFF.** `HELIX_SEMANTIC_ARM` unset → every code path is byte-identical to today; `query_type` defaults to `None` everywhere; route() runs the LIKE gate; dense weight = 4.0. Non-semantic types are provably untouched (gate-4).
- **Rollout:** (1) PRD sign-off. (2) Land the threading (§3) with the arm default-off; merge is a no-op until enabled. (3) Stand up a SEPARATE experiment daemon with `HELIX_SEMANTIC_ARM=1` + the sembroaden TOML (do NOT restart the currently-wrapped baseline daemon). (4) Run the §5 A/B + sweep. (5) Report the dual gate + recovery split.
- **Rollback:** unset `HELIX_SEMANTIC_ARM` (instant revert, no redeploy of code logic needed) or revert the TOML arm. The config knobs and `query_type` param remain dormant. Full code revert is a single-branch drop since all changes are additive and flag-gated.

---

**Files touched (all verified live):** `server/routes_context.py:652-746`, `context_manager.py:2078-2144 / 1191-1205 / 1034 / 516-518`, `sharding.py:227-243` (no change), `shard_router.py:365-403 / 407-438 / 492 / 513-520`, `knowledge_store.py:1468-1478 / 2086-2093`, `config.py:369-390 / 803-818`, `benchmarks/bench_enterprise_rag_recall.py:46-49` (one-line). **New:** recovery-split post-processor (join `recall_*.json` × `rank_distribution_probe.json`).

**Unverified / confirm-before-coding flags:** (1) `route()` verdict was "partial" — the *insertion point* (365-403, caller 438, known_shards 359) is CONFIRMED, but the original "pull query_type from kwargs" plumbing was REFUTED; use the §3/§5 popped-and-named threading instead. (2) `_retrieve`/`query_docs` threading was "partial" — confirmed the chokepoint and the TypeError landmine; the spec's mitigation (add `query_type` to `KnowledgeStore.query_docs` signature) is REQUIRED, not optional. (3) Whether `per_shard_fetch` needs a semantic lift is an empirical open question — measure the 5 routing golds' intra-shard ranks once routing broadens before committing the depth knob. (4) Confirm the live experiment daemon's config has `min_genes ≥ 10` (bench requirement, project memory) and SPLADE on/off state before interpreting recovery numbers.

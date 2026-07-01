# Council: multi-hop lever · scale-adaptive shard/genome automation · does helix cut LLM inference time

- **Date:** 2026-05-30
- **Format:** 5-member council (retrieval-graph architect · shard/genome DB-ops · inference-time pro · inference-time skeptic · ROI/YAGNI adversary) + chair synthesis. Members grounded in the real code; the chair *verified* premises empirically.
- **Status:** advisory — informs next work after the P1' rank-fusion ladder.

---

## Topic 1 — Multi-hop co-activation: **DENIED on the measured corpora (verified)**

The chair ran the SQL sweep: **`harmonic_links` = 0 rows across all 105 Onyx + all 12 XL shards.** The co-activation graph that multi-hop *and* SR would traverse **does not exist** on these corpora — and it's empty **by design**: `seeded_edges_enabled=False` ("Dark ship", config.py:264), and edges are Hebbian (`co_retrieved` from live query history via `update_edge_evidence`, which runs only in the writable/non-`read_only` path). A read-only benchmark can never populate it.

Consequences:
- A walk over an empty graph returns `{}`. Multi-hop/SR/RWR are moot here. The architect's "78% buried tail is link-reachable" optimism is **untestable on these fixtures** — there are no links.
- **Correction to a claim I made earlier:** SR is **not** "built and A/B-able" on the sharded path — `sr_boost` is called only from `knowledge_store.py:2149` (blob `KnowledgeStore`); **zero** references in `shard_router.py`. On sharded Onyx/XL, SR is *unreachable*, not merely default-off. "Flip the flag" is net-new cross-shard plumbing.
- **The one unresolved product decision (yours):** is the benchmark **cold-start** or **production-predictive**? A warm production deployment accumulates query history → edges populate → multi-hop could matter. A cold static bench structurally *under-measures* it. The council can't decide this; you must, before claiming the kill generalizes to production.

**The real recall lever the corpus data supports (replaces multi-hop):**
1. **Wire the already-existing, dead `lexical_rescue.py`** into the sharded path — it's present in `retrieval/` but referenced by neither `shard_router` nor `knowledge_store`. It's the deterministic proper-noun/entity rescue for the ~17 true entity-lookup misses (from prior bench memory). LLM-free, pillar-respecting.
2. **Raise merge-pool depth** (`per_shard_fetch` beyond `max_genes*2`, shard_router.py:441/580) to un-bury the retrieved-but-buried gold (recall@10 24.6% → @200 60.6%).

The architect's RWR-with-restart design (restart α = the hops-vs-precision knob, ~15-line edit over SR's BFS + per-hop bulk `_fill_cache`, cross-shard via `fingerprint_index`, hop-cap 2, `HELIX_COACT_HOPS` default 1 = byte-identical) is **correct but premature** — bank it for an eventual warm-graph regime.

---

## Topic 2 — Scale-adaptive shard/genome automation: **SPLIT THE PROPOSAL**

**Ship now** (cheap, isolated, no rebuild/migration):
- **LRU eviction cap on `ShardRouter._open_shard`** (`HELIX_SHARD_OPEN_BUDGET`, sized `floor((RAM*0.6 − 7GB BGE-M3 singleton)/avg_shard_GB)`). Today the cache holds shards forever and a broad/empty-term query routes **all 105 shards** (`route()→known_shards()`), resident-loading every BGE-M3-backed shard — **that IS the 104 GB commit-ceiling mechanism**. The LRU attacks it directly, cheaper than a rebuild. (Needs a refcount/lock vs the lazy-open cache under concurrent `/context`.)
- **Per-shard calibration fan-out** + `/health` "N/M shards uncalibrated → on 0.35 default" line. Verified blind spot: all 12 XL code shards have **zero** `genome_calibration` rows and silently fall back to the hand-picked **0.35 prose** threshold; `ShardedGenomeAdapter.get_calibration_provenance()` hard-returns `None` so the server can't even see it. Code's boilerplate raises the random-pair cosine baseline → 0.35 is far too loose for code. (Prereq: verify `embedding_dense_v2` coverage per shard first, else calibration runs on a noisy sample.)

**Defer (YAGNI for a 2-corpus world):** the adaptive auto-subshard/merge controller — it breaks the self-identifying filesystem-mirror invariant and needs net-new routing state. Instead ship the **static `--auto-subshard-threshold-files 100000` rebuild flag** (already on a feature branch) to collapse 106 Onyx shards → ~12 and re-measure the ceiling. Build the controller only when a **3rd corpus** with a genuinely different size profile appears.

**Code vs prose:** calibrate + dedup **per content type** (code's boilerplate dups + higher cosine baseline; consider SPLADE-on for code's structured tokens); cap RAM + shard count **content-blind** (shard count is an accident of ingest-root folder layout, *not* a content policy — the DB-ops engineer's own root-cause finding undercuts size-driven sharding).

---

## Topic 3 — Does helix cut LLM inference time: **token/$/grounding WIN, wall-clock LOSS at scale**

Splitting the pro/skeptic difference would be dishonest; here's the actual truth:

**Real wins (pro, survives):**
- **~10–25× prefill-token reduction** — tier budgets 6k/9k/15k (tight/focused/broad, `tier_logic.py` verified) vs ~100–300k tokens of raw-file stuffing. Mechanically true regardless of retrieval quality → real **$/call** + context-budget win.
- **Deterministic LLM-free body = a valid stable prompt-cache prefix** (`cache_control: ephemeral`) — a property an LLM-reranked retriever structurally can't offer. Real `cache_read` vs `cache_creation` delta.
- The honest moat is **$/grounding/reproducibility** (+32.4pp lift, 65% hallucination reduction from memory) — untouched by the latency critique.

**Real costs (skeptic, wins wall-clock at scale):**
- **End-to-end LATENCY LOSS**: verified p50 98.4s / p95 154.5s / p99 311.5s per query at 850K (BENCHMARKS.md:595-597), CPU NumPy dense matmul, GPU ~0%, no ANN. A frontier long-context model prefills the equivalent raw prose in single-digit seconds. **Retrieve-then-inject is 1–2 orders of magnitude slower at scale. Stop implying helix wins on speed.**
- The deterministic prompt-cache prefix is **byte-stable only for `read_only`/sessionless calls today** — in-session it's poisoned by four named mutations: relative-age elision stubs (`session_delivery.py:237`), per-response z-normalized legibility symbols (`legibility.py:100-119`), foveated reordering, budget-trim re-join (`context_manager.py:2486-2525`). Must be **measured, not asserted**.
- Per-gene structure tax (~25–45 tok/gene + ~200–360 tok/turn + ~300 tok decoder) — real but small, config-defeatable; only dominates when retrieval was unnecessary (corpus fits the window, e.g. 45K XL in a 1M window).
- **"reduces inference time" is a category error** — it's a quality/cost play, not a latency play, because the ~60–150s retrieval dwarfs any downstream prefill delta. The honest *latency* lever is **GPU/ANN/SPLADE-prefilter** (orthogonal to all 3 topics).

---

## Recommended order (chair)

1. **DONE — institutionalize as CI guard:** `harmonic_links` row-count sweep (0/105 Onyx, 0/12 XL). Multi-hop/SR denied on these corpora; stop that build work. (The architect's 1-day offline 2-hop oracle is the same verdict, more expensive — skip it.)
2. **Parallel, today, low-risk:** (a) inference-time byte-stability + ephemeral-cache A/B on read_only `/context` + the **break-even latency curve** (helix `/context` vs raw-doc prefill on a frontier model, on both 45K XL and 850K Onyx — publish the crossover); (b) `ShardRouter` LRU cap + re-measure peak commit vs 104 GB; (c) per-shard calibration fan-out + `/health` coverage line.
3. **NEXT (the real recall lever, replaces multi-hop):** PRD then wire `lexical_rescue.py` into the sharded path + raise merge-pool depth. Deterministic, LLM-free.
4. **Inference-time (if step 2a proves byte-stability):** fix the in-session determinism leaks so caching extends to sessions.
5. **LATER (defer):** static `--auto-subshard-threshold-files` rebuild + code content-hash dedup; adaptive controller only when a 3rd corpus appears.
6. **ORTHOGONAL (the only real wall-clock lever):** GPU/ANN/SPLADE-prefilter (PR #160) — belongs to none of the three topics; this is what Joe's Phase 4 targets.

## Falsifiable experiments (the council's discipline: cheapest falsifier first)
- **Warm-graph revival** (only if Topic 1 revisited): flip `seeded_edges_enabled=True`, replay a query stream against a *writable* genome to accumulate Hebbian edges, re-run row-count + offline 2-hop oracle on the buried tail. Build multi-hop only if 2-hop-newly-reachable / total-buried ≥ ~10-15% AND a wired-in `sr_enabled` recall@10 A/B beats merge-depth-only by >2pp.
- **Byte-stability:** SHA-256 `window.expressed_context` across two read_only/no-session `/context` calls; differ → caching thesis dead. Then ephemeral-cache call-2 `cache_read >> cache_creation`.
- **Break-even curve:** helix wall-clock vs raw-stuffing prefill on 45K and 850K — expected to FAIL at 850K, pass/marginal at 45K.
- **lexical_rescue:** wire it, A/B recall@10 on the ~17 entity-lookup misses; <2/17 recovered → those misses are semantic (embedding is the only lever).

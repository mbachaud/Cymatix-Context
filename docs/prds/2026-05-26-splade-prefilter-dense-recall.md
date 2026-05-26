# PRD: SPLADE pre-filter for dense recall

**Date:** 2026-05-26
**Author:** Claude Opus 4.7 (with Max)
**Status:** Approved — design signed off 2026-05-26. Implementation on `perf/dense-prefilter-via-splade-candidates` reflects the four locked-in decisions (see §6). Ready for commit + PR.
**Related:** Issue #159, [[project_helix_109_shard_pressure_test]], [[helix_100shard_context_commit_ceiling]]

---

## 1. Problem

At 100+ shards (the 105-shard EnterpriseRAG Onyx fixture is the canonical case), the helix-context daemon hits **Wall 2** — per-query latency from brute-force-no-ANN dense matmul over the full N. The shard router exists but is functionally degenerate as a winnower: SQL probes on 2026-05-26 showed common English query terms hit 90-100% of shards, and a query-level simulation (50 synthetic 3-token queries) showed entities-exact OR-matching alone yields **median 100/105 shards routed-to** — statistically indistinguishable from today.

Adding IDF filtering cuts that to median 26/105 but drops 69% of query tokens as noise and 36% of queries fall back to OR-union. The fingerprint redesign is a real lever but has a structural floor.

**Dense matmul** scales linearly with N_total_genes (~850K on this corpus → ~870M FLOPs per query). At cross-shard fan-out, this dominates query latency. SPLADE already runs per-shard and produces a small candidate set per query, but its hits flow into the score accumulator in parallel with dense — never as a pre-filter.

## 2. Motivation

The 2026-05-26 query-level probe singled out **SPLADE pre-filter** as the unambiguous #1 Wall-2 lever. Sparse term-level discrimination is far sharper than fingerprint JSON LIKE-matching, AND it sidesteps the OR-union saturation that defeats column-swap-only fingerprint fixes.

Stays inside the design pillars:

- No LLM ([[feedback_helix_ribosome_off]])
- No ANN
- LLM-free retrieval

FLOP-impact estimate: `O(N_total_genes) → O(|SPLADE candidates|)`. On the onyx_full fixture with ~5K SPLADE hits per typical query, that's ~170× FLOP reduction in the dense tier per query.

## 3. Design

### Public API change

Two new keyword-only kwargs on `query_docs_dense_recall`:

```python
def query_docs_dense_recall(
    self,
    query: str,
    *,
    k: int = 500,
    party_id: Optional[str] = None,
    read_only: bool = False,
    candidate_ids: Optional[set[str]] = None,         # NEW
    dense_prefilter_escape_budget: int = 0,           # NEW
) -> List[tuple[str, float]]: ...
```

**Semantics:**

- `candidate_ids=None` (default): full matmul. Byte-identical to current behavior.
- `candidate_ids={...}`: matmul scoped to the row subset whose gene_id is in the set.
- `candidate_ids=set()` with `dense_prefilter_escape_budget=0`: returns `[]` (strict empty prefilter).
- `dense_prefilter_escape_budget>0`: ALSO scan the complement, take top-N from there. Escape valve for genes the prefilter missed (the "needle outside top-12" case from spec §9). **Result budget, NOT a FLOP budget** — the complement scan is a full matmul on the uncovered rows; this knob preserves cold-lex recall at the cost of those FLOPs.
- Unknown ids in `candidate_ids`: silently filtered (no crash).

### Two new config knobs (KnowledgeStore ctor + helix.toml + config.py)

- `dense_prefilter_enabled: bool = False` — opt-in flag for the wire-up in `_retrieve`.
- `dense_prefilter_escape_budget: int = 0` — RESULT budget for the recall escape valve.

Both knobs land in `[retrieval]` in `helix.toml` and the `RetrievalConfig` dataclass with the same defaults, plumbed through `context_manager.py` to the `KnowledgeStore` constructor — same pattern as `dense_pool_size` / `dense_additive_weight`.

### Wire-up in `_retrieve`

When `dense_prefilter_enabled=True` AND `gene_scores` is non-empty (lex/SPLADE found something):

```python
prefilter_ids = set(gene_scores.keys())
prefilter_budget = self._dense_cold_lex_budget
```

When `gene_scores` is empty (pure cold-lex query): fall back to full scan so dense-only needles still surface.

### Alternatives considered

**A. SPLADE candidate set only (no `gene_scores.keys()` union)** — would skip lex-tier hits the prefilter, potentially excluding genes lex found but SPLADE missed. Rejected — using the post-lex/SPLADE gene_scores is strictly more inclusive at near-zero extra cost.

**B. Bounded-FLOPs escape scan (random sample of complement)** — would cap FLOPs even with the escape budget on. Rejected — random sampling defeats the recall purpose (needles are by definition rare hits, not random). The caller-owned budget trade-off is the honest API.

**C. Threshold-based fallback (if `|candidate_ids| < N`, full scan)** — adds non-determinism (a query routes differently based on candidate count). Rejected for simplicity; if recall regresses, caller can set `dense_prefilter_escape_budget` instead.

**D. Naming: `splade_prefilter_enabled` instead of `dense_prefilter_enabled`** — closer to the design intent (SPLADE-derived candidates). Rejected because the API accepts ANY candidate_ids, not just SPLADE; the flag governs the dense tier, not SPLADE.

## 4. Test plan

9 new tests in `tests/test_dense_recall.py`:

**Unit (5):**

- `candidate_ids` constrains results — out-of-set gene excluded even when it's the natural top hit
- empty `candidate_ids` + zero escape budget returns `[]`
- unknown ids filtered (no crash)
- `dense_prefilter_escape_budget` recovers a complement-side needle
- `candidate_ids=None` ≡ omitting the kwarg (regression guard)

**Integration via `query_docs` (4):**

- prefilter OFF preserves needle recall (regression guard for default users)
- prefilter ON + escape_budget=0 excludes lex-disjoint needle
- prefilter ON + escape_budget>0 recovers the needle
- config defaults + plumbing (instance attrs `_dense_prefilter_enabled` and `_dense_prefilter_escape_budget`)

Plus one whitelist entry in `test_sharded_adapter_parity.py` for the new private helper `_dense_recall_with_prefilter` (per-genome internal, like other `_get_dense_codec` siblings).

**Status:** 23/23 pass in `test_dense_recall.py`; 79/79 pass across the broader regression sweep (dense_recall + sharded_adapter_parity + calibration + ingest_dense_v2 + fixture_dense_backfill).

## 5. Rollout & risks

**Ship dark.** `dense_prefilter_enabled` defaults to False. No production callers exercise the new path until `helix.toml` is updated. The candidate_ids API surface is available immediately for ad-hoc testing.

### Validation path

**Step 1.** Land this PR (API + config knobs, all tests green, no behavior change in prod). **DONE — landed as PR #160 on `bench/int-5fixture` at commit 9328111.**

**Step 2 — Pre-flip gate: four-run tier-ablation table on EnterpriseRAG-Bench. ✓ COMPLETED 2026-05-26.**

### Results (n=100, k=10, fixture: `enterprise_rag_10k_w16000.db`)

| Variant | R@1 | R@3 | R@5 | R@10 | MRR | p50 ms | p95 ms | p99 ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **T. Today (baseline)** | 49.0% | 57.0% | 59.0% | 60.0% | 0.533 | 1857 | 2337 | 2516 |
| **A. SPLADE off** | 49.0% | 57.0% | 59.0% | 60.0% | 0.533 | 1208 | **1411** | 1635 |
| **B. Prefilter on, escape=0** | 49.0% | 57.0% | 59.0% | 60.0% | 0.533 | 1816 | 2348 | 2452 |
| **C. Prefilter on, escape=250** | 49.0% | 57.0% | 59.0% | 60.0% | 0.533 | 1825 | 2323 | 2459 |

### Decision: FLIP with `dense_prefilter_escape_budget=0`

Both flip criteria pass:

- **T → B recall@10:** +0.0pp (within ±1pp threshold) → max-FLOP-savings config is safe
- **T → C recall@10:** +0.0pp (within ±1pp threshold)

Per PRD flip criteria, when T → B already passes, ship with `dense_prefilter_escape_budget=0` (cold-lex escape valve is not needed on this corpus).

### Notable diagnostic findings

**1. The prefilter is recall-neutral.** At every K (1/3/5/10) and on MRR, B/C are byte-identical to T. The design hypothesis holds: the post-lex/SPLADE `gene_scores.keys()` is a sufficient candidate set for the dense matmul on this corpus.

**2. On 10K, prefilter latency is noise.** B/C differ from T by ≤25 ms at p95 — within run-to-run variance. The dense matmul on ~10K rows is already cheap (~10M FLOPs). The prefilter's structural FLOP-reduction story is **valid by construction** (unit tests prove the matmul is scoped) and should materialize at 100+ shard scale where cross-shard dense fan-out is ~500K+ rows per query — but this corpus doesn't exercise it.

**3. SPLADE contributes 0pp to recall on this fixture.** `T → A` is +0.0pp at every K, while p95 latency drops 926 ms (~40%). This is "all pain, no gain" for SPLADE on the Onyx 10K basic/semantic/intra_document_reasoning question set. **This is a separate concern** — it does not gate the prefilter flip — but warrants a follow-up investigation:

- Is SPLADE redundant with FTS5 + dense on this corpus?
- Does it shine on rare-term-expansion question types the bench doesn't include?
- Is its current tier weight (3.5) over-allocated?

The combined `(SPLADE off + prefilter on)` configuration would deliver the latency win of A with the structural FLOP savings of B — but the SPLADE decision deserves its own PRD and bench coverage (other corpora, additional question types) before flipping it. The prefilter PR ships independently.

### Follow-up: flip PR

The flip PR is a one-line change:

```toml
# helix.toml [retrieval]
dense_prefilter_enabled = true
# dense_prefilter_escape_budget stays at 0 (the default)
```

That PR should:

1. Update `helix.toml` to flip the flag.
2. Update the dataclass default in `helix_context/config.py` to match (so downstream callers see the right default).
3. Reference this PRD's §5 results as the gate.
4. Note the artifact: `benchmarks/results/ablate_dense_prefilter/ablation_full_1779831899.json`.

### Reproducibility

```bash
python benchmarks/ablate_dense_prefilter.py \
  --fixture F:/Projects/helix-context/genomes/bench/matrix/enterprise_rag_10k_w16000.db \
  --max-questions 100 --label full
```

The orchestrator (`benchmarks/ablate_dense_prefilter.py`) is committed with this PR. It patches `helix.toml` per variant, spawns a fresh daemon, warms it, runs the recall bench with per-query latency timing, and aggregates the comparison table.

Before flipping `dense_prefilter_enabled=True` as the default, run all four configs on the same fixture (target: the 109-shard onyx_full corpus once Wall-1 is also resolved per Issue #159 Path A; otherwise on the leak-free 10K/50K Onyx corpus). Same query set, same model, same seed.

| Run | Config | Diagnostic purpose |
| --- | --- | --- |
| **T. Today (baseline)** | `splade_enabled=True`, `dense_prefilter_enabled=False` | Reference point — current shipped behavior. |
| **A. SPLADE off** | `splade_enabled=False`, `dense_prefilter_enabled=False` | What does SPLADE alone contribute to recall@K? Defines the upper bound on what the prefilter could over-prune. |
| **B. Prefilter on, no escape valve** | `dense_prefilter_enabled=true`, `dense_prefilter_escape_budget=0` | Worst-case prefilter recall — does it regress vs T? Measures the cost of strict prefiltering. |
| **C. Prefilter on, with escape valve** | `dense_prefilter_enabled=true`, `dense_prefilter_escape_budget=250` | Does the escape budget close the recall gap from B? Trades FLOPs back for recall. |

**Metrics per run:** recall@1 / recall@5 / recall@10 / MRR, plus p50 / p95 / p99 `/context` latency.

**Diagnostic reads:**

- **T → A** (delta): SPLADE's standalone contribution to recall. If small (< 2pp at recall@10), the prefilter has lots of headroom and `dense_prefilter_escape_budget=0` is likely safe. If large (> 5pp), the escape valve matters and B will probably regress.
- **T → B** (delta): the recall cost of the strict prefilter. Acceptable if within ±1pp at recall@10.
- **B → C** (delta): the escape valve's recall recovery. Confirms the budget knob actually works.
- **T → C** (delta): the net recall change from shipping prefilter ON. This is the headline number for the flip decision.
- **Latency T → B → C**: confirms the FLOP-savings story actually materializes in wall-clock.

**Flip criteria:**

- T → C recall@10 within −1pp: flip default to True with `dense_prefilter_escape_budget=250` (or whichever budget the runs land on).
- T → C recall@10 within −1pp AND T → B already passes: flip default to True with `dense_prefilter_escape_budget=0` (max FLOP savings).
- T → C recall@10 worse than −1pp: do NOT flip. Either tune budget higher and re-bench, or document the recall/latency trade-off and let the deployment decide.

**Why all four runs (not just T vs C):** the SPLADE-off run (A) is the only way to *understand* the result. Without it, a flat C ≈ T number is ambiguous — it could mean the prefilter is perfect, OR that SPLADE was never doing much. The four-run table resolves that ambiguity and the diagnostic data has lasting value beyond this PR (first tier-ablation on the leak-free EnterpriseRAG corpus).

**Cost.** Each run is hours of wall-clock on the 109-shard fixture (per [[project_helix_109_shard_pressure_test]]). Total ~half a day across the four. Wall-1 (memory ceiling) must be resolved first per Issue #159 Path A — otherwise the daemon OOMs before the bench completes. As a fallback, the 10K/50K Onyx corpus is also leak-free and reproduces the recall numbers in the existing memory ([[project_helix_bench_investigation_2026-05]]) — runs faster, less load-bearing but still diagnostic.

**Risks:**

- **Recall regression on cold-lex queries.** A query whose terms aren't in any SPLADE candidate's sparse vector AND aren't lex-matched will see an empty `gene_scores`, fall back to full scan — same as today. The risk surface is queries with *some* lex match but the actual needle is dense-only. `dense_prefilter_escape_budget` is the lever; the bench in step 2 measures it.
- **FLOP savings smaller than estimated** if typical queries have larger SPLADE candidate sets than the ~5K assumption. Bench will measure actual.
- **Adapter surface drift.** The new private helper is whitelisted; any future structural change to `ShardedGenomeAdapter` should mirror the whitelist convention.
- **Behavioral coupling with #158 (parallel fan-out + SPLADE liftout).** Both PRs touch the dense tier indirectly. This PR doesn't depend on #158 — but if both land, the SPLADE encoding is hoisted once per query (from #158) AND the dense matmul is scoped per shard (from this PR). The combined effect is the actual Wall-2 fix.

## 6. Resolved decisions (signed off 2026-05-26)

1. **Flag name** — `dense_prefilter_enabled`. It governs dense behavior, not SPLADE itself. SPLADE-derived candidates are the *typical* source for `candidate_ids`, but the API accepts any caller-supplied set.
2. **Default state** — `False` for this PR. TOML plumbing, dataclass field, and `helix.toml` entry land now so the knob is wireable from config without a code change. The flip to `True` happens in a follow-up PR, after the four-run ablation in §5 lands a decisive result.
3. **Escape-valve abstraction** — `dense_prefilter_escape_budget` (renamed from `cold_lex_budget`). Documented prominently as a **result budget, not a FLOP budget** — the complement scan is a full matmul on uncovered rows. Caller owns the trade between recall preservation and the cost of that scan.
4. **Empty-`gene_scores` semantic** — fall back to full scan. Skipping dense entirely would erase dense-only needles for any query whose lex/SPLADE tiers happened to return nothing — too aggressive a tradeoff for the FLOP win in that edge case.

These four decisions are reflected in the code on `perf/dense-prefilter-via-splade-candidates`.

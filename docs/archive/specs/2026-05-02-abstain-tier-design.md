# ABSTAIN Tier — Confidence-Gated Context Attachment Design Spec

**Date:** 2026-05-02
**Status:** Approved for implementation planning
**Scope:** `helix_context/context_manager.py` (primary), `helix_context/config.py`, `helix.toml`, `helix_context/telemetry.py` (label only), new `tests/test_abstain_tier.py`.

## 1. Motivation

The 2026-05-01 GPQA Diamond overnight (n=189 apples-to-apples, gemma4:e4b)
showed Helix delivers a **+16.9pp absolute accuracy win** (7.9% → 24.9%)
but **regresses p95 latency** (159.5s → 173.5s, threshold-failing).

Stratifying by retrieval hit/miss:

| | n | mean Δ (ON − OFF) |
| --- | --- | --- |
| `found_in_context=True`  |  42 | +15s |
| `found_in_context=False` | 147 | **+34s** |
| Top-10 worst regressions |  10 | all `fic=False` |

When the genome doesn't contain the answer, today's confidence-tier still
falls into BROAD (~12K-token expressed_context) and the small e4b model
spends 2+ minutes digesting irrelevant noise. **Helix is paying full
latency cost on queries where it provides zero retrieval value.** This is
an architectural gap, not a tuning problem. The reframe of the open BM25
finding (research review 2026-04-22) follows from the same diagnosis:
helix isn't slow; helix is missing a gate it doesn't have yet.

This spec adds a fourth tier — **ABSTAIN** — slotted above BROAD. When
post-refinement scores show retrieval has nothing useful to contribute,
the tier returns a marker-only ContextWindow and the small model answers
from weights instead of digesting 12K of noise.

## 2. Non-Goals

- **No new threshold tuning.** ABSTAIN reuses the existing
  `FOCUSED_SCORE_FLOOR = 2.5` and the same ratio cutoff (1.8) the FOCUSED
  tier already uses. No new hyperparameter to defend.
- **No retrieval-pipeline changes.** ABSTAIN gates AFTER candidate
  refinement (`_apply_candidate_refiners`); refinement still runs and
  scores still update via cymatics / harmonic-bin / TCM / rerank.
- **No composite signal in v1.** Coordinate-confidence and PLR `prob_B`
  remain available as future second-stage signals (§9), not v1 inputs.
- **No new HTTP endpoint.** Observability ships through the existing
  `ContextHealth.status` field and `budget_tier_counter` metric.
- **No change to BROAD's budget today.** Tightening BROAD (12K → 6-8K) is
  a separate follow-up after ABSTAIN measures clean (§9).
- **No replacement of the empty-candidates branch.** Empty-candidates
  keeps its existing `status="denatured"` so the two cases stay
  observably distinct.

## 3. Trigger Condition

ABSTAIN fires when **all three** hold post-refinement:

1. `abstain_enabled` is true (config + env, see §6)
2. `top_score < FOCUSED_SCORE_FLOOR` (i.e. `< 2.5`)
3. `ratio < 1.8` (where `ratio = top_score / max(mean_score, 0.01)`,
   computed over candidate-set scores only — same definition the existing
   tier block uses at `context_manager.py:747-749`)

Both conditions are `<` strict. A query with `top_score == 2.5` does NOT
abstain (it can still earn FOCUSED). A query with `top_score == 1.0` and
`ratio == 1.85` does NOT abstain — strong-relative signal is preserved.

The trigger is the **negative space** of TIGHT/FOCUSED: any query that
fails BOTH the FOCUSED absolute floor AND the FOCUSED ratio floor lands in
ABSTAIN. Today such queries silently drop into BROAD. The whole change is
turning that fallthrough into an explicit abstain.

## 4. Output Shape

A new private helper `_build_abstain_window(query, candidates, *, reason)`
returns a `ContextWindow` with:

```python
expressed_context = _ABSTAIN_MARKER          # see §4.1
context_health = ContextHealth(
    ellipticity=0.0, coverage=0.0, density=0.0, freshness=0.0,
    genes_available=self.genome.stats().get("total_genes", 0),
    genes_expressed=0,
    status="abstain",                        # NEW enum value
)
metadata = {
    "query": query,
    "genes_expressed": 0,
    "budget_tier": "abstain",
    "abstain_reason": reason,                # forward-compat for v2 reasons
    "top_score": top_score,
    "ratio": ratio,
}
total_estimated_tokens = estimate_tokens(effective_decoder_prompt)
compression_ratio = 1.0
```

### 4.1 Marker string

A new module-level constant in `context_manager.py`:

```python
_ABSTAIN_MARKER = "(no relevant context found in genome)"
```

The empty-candidates branch (`context_manager.py:704-711`) is refactored
to reference this constant — but **only the marker string is shared**.
The empty-candidates branch keeps its existing `status="denatured"` and
its existing `ContextWindow` shape; only the literal `"(no relevant
context found in genome)"` is lifted to `_ABSTAIN_MARKER`. The new
abstain branch returns its own `ContextWindow` with `status="abstain"`
(see §4 above). Both branches ship the **same bytes** to the LLM, so
the small model's prompt-conditioning is identical regardless of which
short-circuit fired. The semantic difference is observable only in
`status` (`"denatured"` vs `"abstain"`) and in `metadata["budget_tier"]`.

### 4.2 Why a marker, not an empty string

The handoff phrase "skip injection entirely" was about not shipping the
12K of noise, not about omitting the marker. Keeping the 12-token marker
preserves the cue that *helix looked and didn't find* — useful for any
downstream consumer that inspects the system prompt. The latency
improvement comes from dropping ~12K → ~12 tokens, not from going to
zero.

## 5. Integration Point

The gate slots into `HelixContextManager.build_context` between
the existing score-gate floor (15%-of-top hard floor) and the TIGHT/FOCUSED
absolute-floor checks. Concretely, inside the `if len(candidates) > 3:`
block at `context_manager.py:739`, after `top_score`, `mean_score`, and
`ratio` are computed (lines 747-749) and after `gated`/`shadow_pool` are
materialized (lines 755-759):

```
... compute top_score, mean_score, ratio, gated, shadow_pool ...

# ── ABSTAIN gate ──────────────────────────────────────────────────
# When retrieval is weak on BOTH the absolute floor AND the ratio,
# inject a marker-only ContextWindow so the small model answers
# from weights instead of digesting 12K of irrelevant noise.
if (
    self._abstain_enabled
    and top_score < FOCUSED_SCORE_FLOOR
    and ratio < 1.8
):
    return self._build_abstain_window(
        query=query,
        effective_decoder_prompt=effective_decoder_prompt,
        top_score=top_score,
        ratio=ratio,
        reason="score_below_floor",
    )

# ── existing TIGHT_SCORE_FLOOR / FOCUSED_SCORE_FLOOR tiering ──────
TIGHT_SCORE_FLOOR = 5.0
FOCUSED_SCORE_FLOOR = 2.5
if ratio >= 3.0 and top_score >= TIGHT_SCORE_FLOOR ...
```

**`if len(candidates) > 3` placement is intentional.** The dominant 12K
dump is BROAD with 8-12 candidates. Small candidate sets (≤3) ship at
most ~3-6K and are not the regression driver, so leaving them outside
the gate is acceptable for v1 and avoids changing semantics for an
unrelated path. v2 can revisit if telemetry shows ≤3 weak-candidate
cases dominate residual p95.

**`_apply_candidate_refiners` runs unchanged.** Refinement (cymatics,
harmonic-bin, TCM, rerank) can rescue a weak-looking initial set; we
gate on **post-refinement** scores so the gate sees the system's best
attempt before deciding to abstain.

## 6. Config Surface

### 6.1 helix.toml

```toml
[budget]
# ... existing keys ...

# Confidence-gated context attachment. When true (default), build_context
# returns a marker-only ContextWindow when post-refinement retrieval is
# weak on BOTH absolute score (top_score < 2.5) AND ratio
# (top_score/mean < 1.8) — the negative space of the TIGHT and FOCUSED
# tiers. Goal: skip the 12K-token BROAD fallback on queries where helix
# can't help, so the small model answers from weights instead of
# digesting irrelevant noise. Set to false to restore the legacy
# always-inject behavior (BROAD takes the negative space). The
# HELIX_ABSTAIN_DISABLE=1 env var forces off without redeploy.
abstain_enabled = true
```

### 6.2 BudgetConfig

`helix_context/config.py`:

```python
@dataclass
class BudgetConfig:
    # ... existing fields ...
    abstain_enabled: bool = True
```

Loader in `load_config` reads `b.get("abstain_enabled",
cfg.budget.abstain_enabled)` like the other budget keys. (The loader is
named `load_config`, not `_load_helix_config` — it's public, not
underscore-prefixed.)

### 6.3 Env override

Resolved at gate-evaluation time (not at config-load) so an operator can
flip without restart:

```python
self._abstain_enabled = (
    self.config.budget.abstain_enabled
    and not _env_truthy("HELIX_ABSTAIN_DISABLE")
)
```

`_env_truthy("HELIX_ABSTAIN_DISABLE")` is a small helper that reads the
env var fresh on each call and treats `"1"`, `"true"`, `"yes"` (case-
insensitive) as true. Defined alongside the existing env utilities; if
none exist, add it inline in `context_manager.py`.

## 7. Telemetry

Extend the existing `budget_tier_counter` (already incremented for
`broad` / `focused` / `tight` at `context_manager.py:802-806`) with a
new label value:

```python
budget_tier_counter().add(1, attributes={"tier": "abstain"})
```

No new metric. The `abstain_reason` is **not** a metric label (would
explode cardinality with future reasons); it lives on the response
metadata only. Operators reading `/health` see ABSTAIN trip-rate as a
Prometheus query: `rate(budget_tier_total{tier="abstain"}[5m])`.

A separate `genes_expressed` already-emitted gauge stays at 0 on
ABSTAIN, so existing dashboards continue to work without modification.

## 8. Tests

New file `tests/test_abstain_tier.py` covering eight cases. All tests
use the existing `tests/conftest.py` fixtures (genome with planted
genes, score injection via `genome.last_query_scores`).

| # | Case | top_score | ratio | abstain_enabled | Expected `metadata["budget_tier"]` | Expected `status` |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Weak retrieval         | 1.5 | 1.2 | True  | `"abstain"`  | `"abstain"`   |
| 2 | Strong + dominant       | 8.0 | 4.0 | True  | `"tight"`    | (existing)    |
| 3 | FOCUSED-eligible        | 3.5 | 2.0 | True  | `"focused"`  | (existing)    |
| 4 | Boundary at floor       | 2.5 | 1.2 | True  | not abstain  | (existing)    |
| 5 | Boundary at ratio       | 1.5 | 1.8 | True  | not abstain  | (existing)    |
| 6 | Flag off, weak          | 1.5 | 1.2 | False | `"broad"`    | (existing)    |
| 7 | Env override (disable)  | 1.5 | 1.2 | True  | `"broad"`    | (existing)    |
| 8 | Telemetry               | 1.5 | 1.2 | True  | `budget_tier_counter` increments with `{tier: "abstain"}` |

Test 7 sets `HELIX_ABSTAIN_DISABLE=1` via `monkeypatch.setenv` and
asserts the env override beats the config flag.

Test 1 also asserts:
- `expressed_context == _ABSTAIN_MARKER`
- `metadata["abstain_reason"] == "score_below_floor"`
- `metadata["top_score"] == 1.5` and `metadata["ratio"] == 1.2`
- `context_health.genes_expressed == 0`

## 9. Bench Plan

Before merging, run `benchmarks/bench_aa_suite.py` on the n=147
`fic=False` GPQA Diamond subset with abstain enabled and compare to the
2026-05-01 baseline:

```bash
python benchmarks/bench_aa_suite.py \
    --bench gpqa_diamond \
    --ids "$(comm -23 <(sort baseline_fic_false_ids.txt) /dev/null)" \
    --timeout 240
```

**Pass criteria** (both required):

- p95 latency on the fic=False subset drops by ≥ 15s (target: claw back
  the bulk of the +34s mean Δ)
- Accuracy on the fic=False subset is ≥ baseline (no regression — these
  are queries helix wasn't helping on, so abstaining shouldn't lose any
  wins)

Commit the report to `overnight_logs/diamond_abstain_<date>_report.md`
with `git add -f` (overnight_logs/ is gitignored). Cross-link from the
implementing PR's body.

**Stratified secondary check:** also report fic=True latency to confirm
ABSTAIN doesn't fire on hits (it shouldn't — strong retrieval lands in
TIGHT/FOCUSED). If fic=True p95 also drops, we accidentally abstained on
useful queries and need to revisit thresholds.

## 10. Risk Register

| Risk | Severity | Mitigation |
| --- | --- | --- |
| ABSTAIN false-positives (gates a query helix could have helped) | Medium | Thresholds reuse FOCUSED's already-calibrated floor (KV-harvest 2026-04-12). Bench gate (§9) requires no fic=True regression before merge. Kill-switch (§6.3) for fast rollback. |
| The +16.9pp accuracy win partly came from BROAD-tier wins on weak retrieval | Low-Medium | Stratifying bench by fic=True/False isolates this. fic=False accuracy is the abstain target — should be unchanged or up (we're cutting the noise that was hurting them). fic=True is unaffected (stays in TIGHT/FOCUSED). |
| Thresholds drift as the genome grows / corpus changes | Low | Telemetry on `budget_tier_counter{tier="abstain"}` trip-rate flags drift early. Re-run bench quarterly or after large genome ingest. |
| Empty system message confuses some downstream client | None | We keep the marker string (§4.2). System prompt shape unchanged. |
| Off-axis interaction with classifier `assembly_max_genes_cap` | Low | Classifier cap applies AFTER tiering today. ABSTAIN slots into the tier stage and short-circuits before cap evaluation, so the cap is a no-op on abstained queries. Documented in test #1 (no cap effect observable). |

## 10.1 Reframing for paper §7

The 2026-04-22 research-review finding "BM25 8/8 @ 151ms beats helix_rag
4/8 @ 1793ms" reads, post-this-spec, as *helix needs the confidence
gate it doesn't have yet*, not *helix is too slow*. BM25 wins by
answering small or not at all; helix loses by paying full retrieval +
full injection cost on queries with no signal. ABSTAIN closes that gap.
Worth a light prose refresh in §7 once ABSTAIN ships and benches clean.

## 11. Out of Scope (Deferred)

- **Composite gate.** Add `coordinate_confidence < 0.30` as an OR
  trigger when score-only false-negatives surface in production
  telemetry. Code already exists at `context_packet.py:275-319`; lift
  the helper to a shared module, then OR into the abstain trigger.
- **PLR second-stage gate.** When the stacked PLR head reaches AUC
  ≥ 0.7 (current 0.631 is too weak), promote `prob_B > 0.5` from
  packet annotation to OR-gate input. Spec for this lives with the
  next PLR re-train.
- **BROAD budget tighten.** Drop `[budget] expression_tokens 12000 →
  6000-8000` and `max_genes_per_turn 12 → 8` in a follow-up PR after
  ABSTAIN measures clean. Handoff §"Open Finding" item #2.
- **Mid-confidence "single-anchor" tier between FOCUSED and ABSTAIN.**
  When `top_score ∈ [2.0, 2.5]` and ratio is moderate, ship the top
  gene only (3K) instead of abstaining. Hold until trip-rate
  telemetry shows ABSTAIN is over-firing.

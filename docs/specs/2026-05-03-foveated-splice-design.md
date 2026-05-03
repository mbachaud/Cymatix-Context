# Foveated-Splice — Rank-Scaled Compression Schedule for BROAD Tier — Design Spec

**Date:** 2026-05-03
**Status:** Approved for implementation planning (pending bench validation)
**Scope:** `helix_context/context_manager.py` (BROAD branch only), `helix_context/config.py`,
`helix.toml`, new `tests/test_foveated_splice.py`. **Depends on the ABSTAIN tier
(2026-05-02 spec) being merged first** — foveated-splice only modifies the BROAD
tier behavior; ABSTAIN owns the lower-bound cliff.

## 1. Motivation

The 2026-05-02 ABSTAIN tier closes the latency regression on `fic=False` (retrieval-
missed) queries by skipping injection entirely. It does **not** address the residual
ceiling on `fic=True` accuracy: when retrieval succeeds, helix today ships up to 12
candidate genes at uniform compression fidelity into the small e4b model's system
prompt. Two known small-model effects mean uniform-fidelity injection
under-utilizes the retrieval signal:

1. **Effective attention bandwidth.** gemma4:e4b on 12K tokens of mixed-relevance
   content spreads attention thin across all genes regardless of their score —
   the top-1 gene gets the same per-token attention budget as rank-12.
2. **Lost in the middle** (Liu et al. 2023). Small models attend disproportionately
   to the start and end of context. Helix's current rank-order injection (top-1
   first) buries the most-relevant content far from the user query, where attention
   weight is lowest.

This spec adds **foveated-splice**: rank-scaled per-gene compression for the BROAD
tier paired with reverse-rank placement. Top-1 gets full content nearest the query;
lower-ranked genes get progressively compressed and pushed away from the cursor.
The retrieval signal is preserved with higher fidelity where the model can use it,
and noise is shed where the model would spread attention thin anyway.

The framing analogy: foveated rendering in real-time graphics allocates GPU
budget to the foveal cone of the eye and lets resolution decay outward.
Foveated-splice does the same for the LLM's "attention pointer" (the user query):
top-rank content stays high-fidelity near the cursor; rank decays both in
compression and in position toward the periphery of the system prompt.

## 2. Non-Goals

- **No change to ABSTAIN, FOCUSED, or TIGHT tiers.** Foveated-splice only modifies
  the BROAD branch of the budget-tier block in `_build_context_internal`. ABSTAIN
  owns the lower-bound cliff; TIGHT/FOCUSED stay on uniform compression because
  they retrieve fewer genes and the rank gradient is less informative there.
  Extending foveated to TIGHT/FOCUSED is a deferred v3 item (§11).
- **No change to total token budget for BROAD.** Today's BROAD ships ~12K tokens.
  Foveated-splice redistributes those bytes by rank but holds total constant.
  BROAD budget tighten (12K → 6-8K) is a separate spec that can compose with
  foveated after both validate independently.
- **No change to retrieval scoring or candidate selection.** The set of 12
  candidates and their scores come out of `_express` + `_apply_candidate_refiners`
  unchanged. Foveated only affects how those 12 are compressed and placed.
- **No new compression backend.** Per-gene byte caps feed into the existing
  splice infrastructure via `max_item_chars` — no new ribosome interface, no
  LLMLingua wrapper, no headroom changes.
- **No on-by-default ship.** Unlike ABSTAIN (which shipped on as a fix),
  foveated-splice ships off-by-default for a measurement period (§6.3).
- **No bundled-attribution claim.** Schedule shape and placement are benched
  independently in Phase 1 of the bench plan (§9). Shipping both without
  isolation would conflate the well-known position-bias effect with the novel
  schedule-shape contribution and weaken the publication framing.

## 3. Trigger / Scope

Foveated-splice activates when:

1. `foveated_enabled` is true (config flag, default false; see §6)
2. The dynamic-budget tier resolves to `"broad"` (the existing post-refinement
   path at `context_manager.py` budget-tier block, which fires when the query
   is not weak-enough for ABSTAIN AND not strong-enough for TIGHT/FOCUSED)

Inside the BROAD branch, foveated replaces today's uniform per-gene
`max_item_chars` with a rank-scaled vector and reverses the assembly order.
All other branches (ABSTAIN, FOCUSED, TIGHT, the empty-candidates short-circuit)
are unaffected.

## 4. Maths — Schedule Shape

### 4.1 Power-law per-gene compression ratio

For BROAD with N candidates ranked by score `s_1 ≥ s_2 ≥ ... ≥ s_N`:

```
c_i = max(c_min, c_max · i^(-α))    for i ∈ [1, N]
```

Where:
- `i` is the gene's rank (1 = top, N = bottom of BROAD set)
- `α` (default `1.0`) is the power-law exponent, the only tunable knob
- `c_max` (default `1.0`) is the rank-1 ceiling — full content
- `c_min` (default `0.15`) is the rank-N floor — heavily compressed

Each gene's effective byte cap = `c_i · _DEFAULT_MAX_ITEM_CHARS`, passed to
`_build_item(...)` which already accepts a `max_item_chars` parameter.

### 4.2 Schedule shape comparison

Behavior at `α ∈ {0.5, 1.0, 2.0}` for N = 12:

| rank | α=0.5 | α=1.0 | α=2.0 |
| --- | --- | --- | --- |
|  1 | 1.00 | 1.00 | 1.00 |
|  2 | 0.71 | 0.50 | 0.25 |
|  3 | 0.58 | 0.33 | 0.15 |
|  6 | 0.41 | 0.17 | 0.15 |
| 12 | 0.29 | 0.15 | 0.15 |

`α=0.5` is gentle decay; `α=2.0` collapses to floor by rank 3-4 and is
effectively a "top-3 dominate" schedule. `α=1.0` (harmonic-ish) is the
balanced default.

### 4.3 Why power-law

Three families were considered:

- **Linear-in-rank** — single tunable [c_min, c_max], no curve choice. Too
  rigid: doesn't capture aggressive top-1-bias scenarios.
- **Power-law** (chosen) — single knob `α`, captures linear-ish (small α),
  harmonic (α=1), aggressive falloff (α=2). Easiest implementation that
  spans the relevant design space.
- **Lagrangian-optimal log-utility** — derived from `maximize Σ s_i · log(1+c_i)`
  subject to `Σ c_i · |g_i| ≤ B`. Theoretically grounded but requires per-call
  λ-binary-search and assumes log-utility as the relevance model. Deferred
  to v2 if power-law underperforms (§11).

### 4.4 Why this is novel

Per the 2026-05-03 arXiv verification (medium-high confidence, ~15-25% missed-
paper risk):

- **AdaComp** (arXiv:2409.01579) does per-query top-k selection — global cutoff,
  not per-doc continuous fidelity rate.
- **EXIT** (arXiv:2412.12559) does sentence-level binary keep/drop within docs —
  binary, not continuous compression rate.
- **PyramidKV / PyramidInfer** (2024) has the math (non-uniform budget along an
  axis with information-theoretic justification) but applies it to layer depth,
  not retrieval rank.
- **LongLLMLingua** (arXiv:2310.06839) does per-doc allocation but heuristic, not
  a derived schedule, and at token-drop granularity rather than continuous
  fidelity.
- **Ms-PoE** (NeurIPS 2024) addresses position bias but doesn't compress.

The exact fusion (rank-axis × continuous-fidelity × derived-optimal schedule)
remains unclaimed. The novelty is the schedule shape and its derivation, not
"rank-aware RAG" broadly. The placement intervention is acknowledged prior art
(Liu 2023, Ms-PoE 2024) and benched separately to keep attribution clean.

The work also engages with **Spectrum Projection Score** (arXiv:2508.05909),
which argues the *selection metric* matters more than the *rate*. Foveated-
splice implicitly assumes the retrieval score is well-calibrated; helix's
KV-harvest 2026-04-12 calibration of `FOCUSED_SCORE_FLOOR = 2.5` and the
shadow-pool / 15%-of-top score-gate floor satisfy that assumption.

## 5. Placement — Reverse Rank Order

For BROAD only, reverse the assembly order so the top-ranked gene lands
**immediately before the user query** rather than at the start of the system
prompt. This exploits the lost-in-the-middle effect (Liu 2023): small models
attend most to tokens nearest the cursor.

```
[system prompt]
  decoder_prompt
  ----
  gene 12 (heavily compressed, c_12 ≈ 0.15)
  gene 6  (moderate, c_6 ≈ 0.17)
  gene 3  (lighter, c_3 ≈ 0.33)
  gene 1  (full content, c_1 = 1.00)
  ----
[user]
  <query>
```

TIGHT and FOCUSED keep their current rank-order placement; only BROAD reverses.
Implementation: a single `list.reverse()` call at the assembly boundary,
guarded by the `foveated_enabled` flag.

**Attribution boundary.** Schedule shape (§4) and placement (§5) are
**conceptually independent** interventions. Schedule is the novel contribution;
placement is well-known prior art. Phase 1 of the bench plan (§9) isolates
placement so the headline paper claim — "the schedule shape contributes X
beyond well-known position-bias effects" — is defensible.

## 6. Config Surface

### 6.1 helix.toml

```toml
[budget]
# ... existing keys ...

# Foveated-splice (BROAD tier only). When true, the BROAD branch of the
# dynamic-budget tier replaces uniform per-gene compression with a rank-scaled
# power-law schedule and reverses the assembly order so the top-ranked gene
# lands immediately before the user query (lost-in-the-middle exploit). Off
# by default for the measurement period — flip to true after the α-sweep
# bench (spec §9) identifies a winner. See docs/specs/2026-05-03-foveated-
# splice-design.md.
foveated_enabled = false

# Power-law exponent for the per-gene compression schedule:
#   c_i = max(c_min, c_max · i^(-α))
# α=0.5 = gentle decay; α=1.0 (default) = harmonic-ish; α=2.0 = aggressive
# top-bias. Bench sweeps {0.5, 1.0, 2.0} during Phase 2 validation; ship the
# winner as the new default.
foveated_alpha = 1.0

# Rank-N (bottom of BROAD set) minimum compression ratio. Each gene's byte
# cap = max(foveated_c_min, foveated_alpha-driven c_i) × _DEFAULT_MAX_ITEM_CHARS.
foveated_c_min = 0.15
```

### 6.2 BudgetConfig

`helix_context/config.py`:

```python
@dataclass
class BudgetConfig:
    # ... existing fields ...
    abstain_enabled: bool = True
    foveated_enabled: bool = False        # NEW
    foveated_alpha: float = 1.0           # NEW
    foveated_c_min: float = 0.15          # NEW
```

Loader in `load_config` reads each new key with the standard
`b.get("foveated_*", cfg.budget.foveated_*)` pattern.

### 6.3 Rollout posture

Off by default for the measurement period. Different from ABSTAIN's on-by-default
ship — ABSTAIN was a fix for a known regression with calibrated thresholds;
foveated-splice has a 3-cell α-sweep ahead of it and ships into a less-instrumented
part of the pipeline. Flipping to on requires bench evidence (§9 pass criteria).

No env override (`HELIX_FOVEATED_DISABLE` is overkill for an off-by-default
feature). When/if foveated flips on by default in a future release, an env
override can be added then with the same `_env_truthy` helper ABSTAIN uses.

## 7. Integration Point

The change lives entirely inside the BROAD branch of the budget-tier block in
`HelixContextManager.build_context` (the section currently around
`context_manager.py:817-840`, the post-FOCUSED `# else: broad` fallthrough).

```python
# Existing: BROAD is the default fallthrough — no per-gene compression today.
# else: broad — keep current up-to-max_genes set
#   (weak absolute scores or weak ratio → widen the net)

# NEW: foveated-splice (BROAD only)
foveated_enabled = (
    self.config.budget.foveated_enabled
    and not _env_truthy("HELIX_FOVEATED_DISABLE")  # consistency with ABSTAIN
)
if budget_tier == "broad" and foveated_enabled and len(candidates) > 1:
    α = self.config.budget.foveated_alpha
    c_min = self.config.budget.foveated_c_min
    c_max = 1.0
    foveated_caps = [
        max(c_min, c_max * (i ** -α))
        for i in range(1, len(candidates) + 1)
    ]
    # Reverse for placement: top-rank lands nearest user query
    candidates = list(reversed(candidates))
    foveated_caps = list(reversed(foveated_caps))
    # foveated_caps[i] now corresponds to candidates[i] in their new order
    # Stash for the assembly path to consume via _build_item(max_item_chars=...)
    self._last_foveated_caps = foveated_caps
else:
    self._last_foveated_caps = None
```

The assembly path (`_build_item`-style construction during expression) reads
`self._last_foveated_caps[i] · _DEFAULT_MAX_ITEM_CHARS` per gene when the
attribute is non-None. When None, today's uniform behavior is preserved.

**Why pre-splice byte-cap, not splice_aggressiveness vector.** The
`splice_aggressiveness` knob is a scalar shared across the codons / splice /
ribosome layers. Plumbing it as a per-gene array touches multiple modules.
Per-gene `max_item_chars` is already a parameter on `_build_item` (used by
the `/context` packet path with `_RAW_MAX_ITEM_CHARS`), so this is a single-
module change with a clean existing seam.

## 8. Telemetry

Per-call metadata addition on the BROAD path when foveated fires:

```python
metadata["foveated_caps"] = foveated_caps   # list[float], len == len(candidates)
metadata["foveated_alpha"] = α
```

This lets us measure post-hoc whether a particular `α` value's curve shape
correlates with accuracy/latency on real production queries — without needing
to re-run the bench. No new metric counter; BROAD trip-rate is already on
`budget_tier_counter`.

When `foveated_enabled = false`, `metadata["foveated_caps"]` is absent.
Dashboards can detect the rollout state via metric presence/absence.

## 9. Bench Plan — Phased Attribution

### 9.1 Phase 0: Establish post-ABSTAIN baseline

Already running 2026-05-02 → 2026-05-03 overnight. All foveated bench cells
compare against this baseline, NOT against the pre-ABSTAIN 2026-05-01 run.

Output: `overnight_logs/diamond_2026-05-03_report.md`. Headline number to beat:
the post-ABSTAIN p95 on `fic=True` and `fic=False` subsets respectively.

### 9.2 Phase 1: Placement isolation

Two cells, ~10-14h overnight:

```
cell A:  forward-rank + α=1.0    (schedule alone, no placement flip)
cell B:  reverse-rank + α=1.0    (schedule + placement)
```

**Decide:** which placement wins on `fic=True` accuracy at α=1. Hold winner
for Phase 2.

**Pass criterion to advance:** the winning placement matches or exceeds the
Phase 0 baseline on `fic=True` accuracy. If both regress accuracy, the schedule
shape itself is broken at α=1 — pause before Phase 2 and revisit the design.

### 9.3 Phase 2: α-sweep on placement winner

Two cells, ~10-14h overnight:

```
cell C:  winner-placement + α=0.5
cell D:  winner-placement + α=2.0
(cell B from Phase 1 is the α=1.0 mid-point)
```

**Decide:** α winner. Ship that triple `(placement, α, c_min=0.15)` as
`foveated_alpha` default with `foveated_enabled = true`.

### 9.4 Pass criteria to merge / flip on by default

For the winning configuration on `fic=True` queries (where retrieval succeeded
— foveated's target population):

- Latency: p95 drops by ≥ 5s OR accuracy lifts by ≥ 2pp
- `fic=False` subset: no regression on latency or accuracy (foveated doesn't
  fire there — ABSTAIN owns it — but worth verifying as a sanity check)

Commit each phase's report to `overnight_logs/diamond_foveated_<DATE>_phaseN_report.md`
with `git add -f` (overnight_logs/ is gitignored). Cross-link from the
implementing PR's body.

### 9.5 Total cost

4 bench cells × ~5-7h each = ~20-28h. Two overnight cycles.

## 10. Tests

New file `tests/test_foveated_splice.py` covering eight cases. Test fixtures
mirror the `tests/test_abstain_tier.py` pattern (in-memory genome, mock
backend, controllable scores via `_stub_express`).

| # | Case | foveated_enabled | tier expected | Pinned behavior |
| --- | --- | --- | --- | --- |
| 1 | Disabled, BROAD | False | broad | `metadata["foveated_caps"]` absent; uniform compression preserved |
| 2 | Enabled, BROAD | True | broad | `metadata["foveated_caps"]` present, len = len(candidates), values match power-law formula |
| 3 | Enabled, TIGHT | True | tight | `metadata["foveated_caps"]` absent (foveated only fires on BROAD) |
| 4 | Enabled, FOCUSED | True | focused | `metadata["foveated_caps"]` absent |
| 5 | Enabled, ABSTAIN | True | abstain | `metadata["foveated_caps"]` absent (gate fires before BROAD branch) |
| 6 | α formula | True | broad | for N=12, α=1: caps[0] == 1.0, caps[5] == 0.5, caps[11] == max(0.15, 1/12) |
| 7 | Reverse-rank order | True | broad | the gene with the highest score is the LAST in the assembled candidates list |
| 8 | c_min floor | True | broad | for α=2 and N=12, caps[5..11] all == c_min (0.15) |

Run: `py -3 -m pytest tests/test_foveated_splice.py -v` — expected 8 passes.
Plus regression check: `py -3 -m pytest tests/test_abstain_tier.py
tests/test_pipeline.py tests/test_context_manager_classifier.py
tests/test_config.py -q` — all pass (no regression in ABSTAIN or surrounding
suites).

## 11. Out of Scope (Deferred)

- **Lagrangian-optimal log-utility schedule.** v2 if power-law underperforms
  on the bench, OR for the publication path where a derived schedule is a
  stronger contribution than a parametric one.
- **Score-driven (vs rank-driven) schedule.** v2 if rank-based shows a
  "score gradient sensitivity" failure mode (e.g., flat-score corpus where
  rank doesn't carry information).
- **Foveated extending to TIGHT/FOCUSED.** v3 once BROAD-only validates.
  TIGHT has 3 candidates and FOCUSED has 6 — the rank gradient is less
  informative, so the win is smaller and the bench cost-per-cell is higher.
- **Sandwich placement** (top-1 at start AND end, primacy + recency). v3
  orthogonal to schedule; would need its own attribution work.
- **BROAD budget tighten** (12K → 6-8K). Separate spec; can compose with
  foveated after both validate independently. The 2026-05-02 ABSTAIN-tier
  spec §11 already lists this as a deferred follow-up.
- **HELIX_FOVEATED_DISABLE env override.** Skipped while foveated is
  off-by-default. Add when foveated flips on by default in a future release.

## 12. Risk Register

| Risk | Severity | Mitigation |
| --- | --- | --- |
| Wrong attribution if interventions bundled (schedule + placement shipped together without isolation) | High | Phase 1 of bench (§9.2) isolates placement at α=1; schedule sweep happens in Phase 2 on the placement winner. Cost: 1 extra bench cell, ~5-7h. |
| α=1 is wrong default — needs per-corpus tuning | Medium | Phase 2 sweeps {0.5, 1.0, 2.0} pre-merge; ship winner as default; off-by-default until winner identified. |
| Reverse-rank placement breaks classifier integration | Medium | Phase 1 cells A/B include classifier-tagged queries; bench reports stratified by classifier class. |
| Per-gene byte-cap interacts badly with splice's content-summary boundary (e.g., truncation mid-sentence for c_min=0.15 genes) | Medium | Test fixture uses real splice path, not mock. Test 8 verifies the c_min floor is respected. If 0.15 truncates mid-token meaningfully, raise c_min to 0.20 in Phase 2 sweep. |
| 15-25% missed-paper risk from arXiv check | Low-Medium | Spec frames novelty as "schedule shape + derivation"; placement is acknowledged prior art (Liu 2023, Ms-PoE 2024). AdaComp and EXIT cited as closest competitors with explicit differentiation. |
| Bench cells take longer than expected | Low | Each cell ~5-7h. Total ~20-28h split across 2 overnights. If 3rd overnight needed, slip release by 1 day. |
| Foveated flips on by default before validation | Low | `foveated_enabled = false` default; PR body's pre-merge checklist requires §9.4 pass criteria as a checkbox. |

## 13. Spec Positioning vs Prior Work

For the implementing PR's body and the eventual paper draft:

- **AdaComp** ([arXiv:2409.01579](https://arxiv.org/abs/2409.01579)) — closest
  competitor on retrieval-quality-aware compression. Differs: per-query global
  top-k cutoff, not per-doc continuous fidelity rate.
- **EXIT** ([arXiv:2412.12559](https://arxiv.org/abs/2412.12559)) — closest
  competitor on context-aware extractive compression. Differs: per-sentence
  binary keep/drop, not continuous compression rate per doc.
- **LongLLMLingua** ([arXiv:2310.06839](https://arxiv.org/abs/2310.06839)) —
  per-doc heuristic compression with question-conditioned importance score.
  Cite as prior art for the relevance-scoring infrastructure we're wrapping.
- **PyramidKV / PyramidInfer** (2024) — non-uniform KV budget across layers.
  Cite as prior art for the math (cross-axis transplant: layer-depth →
  retrieval-rank). The geometric/linear decay framing is theirs; we apply it
  to a different axis.
- **Ms-PoE** (NeurIPS 2024) — position-bias mitigation. Cite as prior art for
  the placement-order rationale.
- **Spectrum Projection Score** ([arXiv:2508.05909](https://arxiv.org/abs/2508.05909))
  — argues selection metric matters more than rate. Engage with one sentence:
  foveated-splice's value depends on a calibrated relevance score, which
  helix's prior work (FOCUSED_SCORE_FLOOR calibration on KV-harvest 2026-04-12)
  has established.

## 14. Reframing for Substack Paper Series

The 2026-05-02 Substack #2 ("The Same Move at Every Layer") committed publicly to
a head-to-head benchmark fight as the next post, gated on three §7 blockers
(BM25 vs helix_rag, PKI tier broken, helix_only 4555-char ceiling). ABSTAIN
addressed the BM25 reframe (helix needed a confidence gate, not speed).
Foveated-splice addresses a different §7 blocker indirectly: the helix_only
4555-char assembly ceiling is partially a uniform-compression artifact —
ranking 12 genes equally at the same compression rate hits a global token
ceiling earlier than rank-scaled compression would.

Foveated-splice is paper-shape work in its own right. Two viable paths:

1. **Foveated as paper #3** (its own post) — if Phase 1+2 benches show clean
   schedule-shape attribution, the rank-axis × continuous-fidelity × power-law
   contribution is publication-grade with the citations above.
2. **Foveated as the §7 unblocker** — the head-to-head paper writes itself if
   foveated closes the helix_only ceiling AND the BM25 gap simultaneously.

Decision deferred until bench results are in.

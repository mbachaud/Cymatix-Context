# Scoring combination: the combinator, not the constant

**Status:** Design exploration for the post-fusion rerank layer, successor to the
scoring-invariance audit (`docs/research/2026-07-08-scoring-invariance-audit.md`, merged
in #257). Produced 2026-07-09. Docs-only — no code changes here; every implementation
path below is **bench-gated** (nothing ships without beating RRF on the 50-needle beds),
and the whole thing is **desk-testable on the existing `genomes/bench/matrix/xl.db`**
with no rig contention. Addresses #255 (rerank-additive scale mismatch) and touches #256
(fusion-mode layer split). The characterization test
`tests/test_retrieval_invariance.py::test_defect1_authority_bonus_dominates_rrf_ordering`
is the regression any fix must consciously flip.

---

## 1. The reframe

The invariance audit filed #255 as *the constant is wrong for the scale*: authority's
`+2.0` (`knowledge_store.py:1562`) dwarfs an O(0.05) fused RRF score, so the bonus becomes
the ranking. That reading is true but shallow, and its implied fix — re-tune `2.0` down to
~`0.04` — is exactly the anti-pattern the audit exists to kill: a magnitude hand-fitted to
one fusion scale, which silently re-breaks the next time the fusion scale moves.

The deeper defect is the **operator**, not the operand. The finalization is:

```python
# knowledge_store.py:2949-2952 (fusion_mode == "rrf")
final_scores[gid] = (
    fused_scores.get(gid, 0.0)      # rank-derived, O(Σweight/(k+rank)) ≈ 0.05–0.5
    + rerank_additive.get(gid, 0.0) # authority/party/access/sema, O(0.5–2.0)
)
```

`+` presupposes a shared unit. Under the legacy additive fusion, there *was* one: both
operands lived on the BM25-comparable scale (O(1–10)), so `fused_additive + authority` was
a fair add and `2.0` was a sensible constant. RRF changed the **left** operand's scale to
rank-reciprocals and left the **right** operand untouched — and `+` accepted the mismatch
without complaint, because addition never complains. The bonuses didn't get miscalibrated;
the *combinator* stopped being valid the moment the two sides stopped sharing a scale.

This matters because the two quantities have **no principled exchange rate to re-tune
toward**. "Rank-fusion quality" (how many tiers ranked this doc highly) and "provenance
authority" (this doc is the source of truth for its path) are not measured in the same
unit — there is no correct number of rank-reciprocals that one authority hit is worth.
Picking any constant is picking an arbitrary conversion. The fix is to stop needing one.

## 2. What the quantities actually are

Sort the scoring signals by how they *should* combine, and a pattern falls out — the
signals combined by `+` are precisely the ones that don't belong in an additive pool.

| Signal | Where | True type | Correct combinator |
|---|---|---|---|
| Rank-fusion tiers (fts5, tag, splade, dense, …) | RRF `fuser.add_tier` | rank lists | **add — after** mapping to a shared rank unit (this is why RRF works) |
| Freshness decay | freshness gate (audit class-c) | multiplier ∈ [0,1] | **multiply** (already correct) |
| Chromatin / density gate | `upsert_doc`, SPLADE tier (#258) | filter | **filter** (already correct) |
| Authority | `_apply_authority_boosts` `knowledge_store.py:1513`, `+2.0/1.5/0.5` at `:1562/1570/1580` → `rerank_additive` `:1590` | categorical fact ("is the source", "domain-primary", "fresh<48h") | **not `+`** — see §4 |
| Party attribution | `:2810` (into `gene_scores`) **and** `:2813` (into `rerank_additive`) | boolean scope match | **filter or tie-break** |
| Access-rate | `:2841` → `rerank_additive` | hotness prior | **tie-break only** |
| sema_boost | `:2457` → `rerank_additive` | similarity, but damped by an additive-scale constant (`1 − top/40`, audit §2d) | its own scale problem |

The signals helix already combines *correctly* are the ones expressed as **filters**
(chromatin, freshness gate) or **multipliers** (freshness decay). The ones it gets wrong
are the categorical facts — booleans dressed as floats — that get `+`'d onto a rank-fused
score. Adding a boolean-as-float to a rank-reciprocal is a category error that no choice of
constant repairs.

## 3. The combinator design space

Lay the options on one axis, from "one shared scale" to "no shared scale":

- **Pure additive** `Σ wᵢ sᵢ` (legacy fusion). Requires every `sᵢ` commensurable. Correct
  *only* while that holds; DEFECT-1 is what happens when it stops. Scale-dependent by
  construction (the audit's central finding).
- **Rank fusion (RRF)** (current default). Maps each tier to ranks *first* — deleting the
  per-tier scale — then adds rank-reciprocals. "Commensurate-then-add." This is exactly why
  it beat additive (+12pp): it manufactures a shared unit before using `+`. But it (a)
  flattens magnitude signal (the xl rank-squeeze the audit flagged) and (b) **still adds the
  rerank bonuses raw, after fusion, on no shared scale** — the unfixed hole, = #255.
- **Lexicographic / leximin** (the scale-free extreme). Order by tier priority; each lower
  tier is *infinitesimal* relative to the one above and only ever breaks ties. No exchange
  rate needed, perfectly scale-invariant. But rigid: a razor-thin primary win beats an
  overwhelming secondary signal, which is its own failure mode (the mirror of additive's).
- **The bounded middle** (what's worth exploring). Two concrete candidates:
  - **(a) ε-band lex** — compare on the fused score, but when the top candidates fall within
    a *relative* tolerance band δ of each other (a fused near-tie), consult the next tier to
    order within the band. This is the finite realization of "infinitesimal *unless the
    primary is a near-tie*": authority can only act inside a fused-score tie, never override
    a clear fused win. δ is a *ratio* (scale-free, audit class-b), not an additive constant.
  - **(b) provenance-as-a-tier, not a term** — feed authority into the rank fusion as its own
    ranked list (it becomes commensurate by construction, like every other tier), or as a
    pre-filter (party scoping), or as an ε-band lex layer. In no case does it remain a raw
    additive bonus on the fused score.

Both (a) and (b) share one move: **replace `+` across non-commensurable scales with either
a shared-unit mapping (rank it, then add) or nesting (order it, break ties).**

## 4. Why "nest, don't add" is the honest principle

The design rule this points at — *if two signals have no principled exchange rate, don't add
them; nest them* — is the finite shadow of a clean piece of mathematics. In a number system
that lets quantities of genuinely different scale coexist (an infinitesimal ε and a real r),
the defining property is that `ε + r = r`: the small quantity refines without ever perturbing
the large one. That is exactly the contract a tiebreaker should honor and the one authority
currently violates: a well-behaved tiebreaker is *infinitesimal relative to the tier above*,
so it decides ties and nothing else. Lexicographic ordering is that contract made finite —
nested scales that don't interfere. The `+` in `final = fused + rerank_additive` is the exact
violation: it lets a "tiebreaker" sit at 40× the thing it's supposed to be refining.

This is a design principle, not an argument from analogy — the mathematics only names the
structure; the beds decide whether it retrieves better.

## 5. The desk-test (bench-gated, no rig)

All of this runs offline against the existing `xl.db` — retrieval-only, zero rig, safe to run
while the ERB blob bench is out. Build a held-out query set, then compare four combinators for
the **rerank layer only** (fusion core untouched):

1. **current** — additive-on-fused (baseline; reproduces DEFECT-1).
2. **authority-as-tier** — authority emitted as a ranked list, RRF-fused with the rest.
3. **ε-band lex** — authority breaks ties only within a relative δ fused-score band.
4. **authority-as-prefilter / off** — authority removed from scoring entirely (floor).

Metrics: `gold_delivered`, gold rank-displacement, and specifically the **DEFECT-1 signature**
— how often a strictly-worse-fused doc outranks a strictly-better one via a rerank bonus
(the exact inversion `test_defect1_authority_bonus_dominates_rrf_ordering` pins). Success = a
combinator that drives the DEFECT-1 inversion count to ~0 **without** losing `gold_delivered`
versus RRF. Only a winner there earns a bench-gated PR on the 50-needle beds.

## 6. Scope, non-goals, relations

- **In scope:** the post-fusion rerank combination — the `+ rerank_additive` step at
  `knowledge_store.py:2949-2952` and the four writers feeding it.
- **Out of scope:** the RRF fusion core (it stays; it's the part that combines *correctly*);
  re-tuning any additive constant (the shallow fix this doc rejects); the sema_boost `1−top/40`
  damping (its own audit §2d item, related but separable).
- **#255** — this is the design behind its fix; the fix flips the pinned characterization test.
- **#256** (fusion-mode layer split) — orthogonal but same family: both are "combination
  happening at the wrong layer / on the wrong assumption." Worth fixing in the same pass since
  the direct-construction default silently runs the additive combinator.

## 7. Effort + sequencing

- **(S)** the four-way desk-test on `xl.db` — idle-bench desk work, no rig, no dependency.
- **(M)** implement the winning combinator (likely ε-band lex or authority-as-tier) + a
  bench-gated A/B on the beds; flip the DEFECT-1 characterization test with intent.

Sequencing: this is idle-desk research, not a rig job — it does not contend with the ERB blob
or the #239 critical path. The desk-test can start now; the implementation waits behind the
bench queue like every other scoring change.

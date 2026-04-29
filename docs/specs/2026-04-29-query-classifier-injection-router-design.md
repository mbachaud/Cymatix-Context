# Upstream Query Classifier / Injection Router — Design Spec

**Date:** 2026-04-29
**Status:** Approved for implementation planning
**Scope:** `helix_context/context_manager.py` (primary), small touch points in metadata payload and tests.

## 1. Motivation

Today the pipeline runs:

1. Step 0 — LLM intent expansion (optional, gated by `query_expansion_enabled`)
2. Step 1 — heuristic signal extraction
3. Step 2 — express (genome query)
4. Refiners — cymatics / harmonic-bin / TCM / rerank
5. Post-retrieval **score-ratio tier** (TIGHT / FOCUSED / BROAD) sizing the
   assembled gene count from retrieval confidence
6. Splice + assemble

The score-ratio tier is good at "how confident am I in the top candidate
*after* I've retrieved" but blind to "what shape of question is this."
A 12-gene BROAD bundle for a critical-path arithmetic question dilutes
the small model's attention and was the failure mode behind the CritPt
regression.

This spec adds an **upstream query classifier** that runs before
retrieval, infers a coarse query class from regex/keyword signals, and
contributes an **assembly-stage** cap on gene count plus a decoder-mode
hint. It does **not** alter retrieval depth, and it **never** raises
the gene budget — only ever lowers it.

## 2. Non-Goals

- No model call for classification (rule-based v1 only).
- No new HTTP endpoint. Observability ships through `metadata`.
- No replacement of the score-ratio tier. The two layers stack.
- No change to retrieval/candidate depth — classifier acts only at
  assembly.
- No support for "how many" / "how long" arithmetic detection in v1
  (deferred to v2 after rule-miss data is collected).

## 3. Taxonomy

Five classes, **strict priority order** (first match wins). Priority is
non-negotiable; it is the tiebreaker for queries that match multiple
classes.

| Priority | Class        | Trigger signals                                                                                            | Min-signal threshold                                                | Decoder mode | Assembly cap |
| -------- | ------------ | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- | ------------ | ------------ |
| 1        | `arithmetic` | Operators (`+`, `-`, `*`, `/`, `%`); keywords: `calculate`, `total`, `sum`, `critical path`                | **≥2 trigger matches**, OR **1 operator + 1 numeric/quantity keyword** | `minimal`    | 2            |
| 2        | `factual`    | Leading wh-word (`who`, `what`, `where`, `when`, `which`) **AND** query length `< 15` words (both required) | wh-word match AND length condition (length is **AND**, not short-circuit) | `condensed`  | 5            |
| 3*       | `procedural` | `how do I`, `how to`, `steps`, `walk me through`                                                            | 1 match                                                             | `full`       | 6            |
| 4*       | `multi_hop`  | Connectives: `and then`, `because`, `after that`, `compare`, ` vs `, `between X and Y`; OR length > 25 words | 1 match                                                             | `full`       | 8            |
| 5        | `default`    | —                                                                                                          | —                                                                   | unchanged    | unchanged    |

\* The relative ordering of `procedural` vs `multi_hop` is **provisional**.
The current ordering puts `procedural` first because step-sequenced
recall benefits from sequence preservation, but this is asserted, not
benchmarked. Revisit after the first procedural benchmark run; flip if
data warrants.

### 3.1 Backlog (deferred to v2)

- "how many" / "how long" + numeric conjunction → `arithmetic`
- Procedural vs multi_hop ordering pending benchmark data
- Embedding-NN fallback if rule miss rate exceeds target

## 4. Integration Point

The classifier slots into `HelixContextManager.build_context()` as a new
private method `_classify_query(query: str) -> ClassifierResult`.

**No new public API; no new HTTP endpoint.** The existing
`decoder_override` parameter remains the explicit caller-wins escape
hatch.

### 4.1 Order of operations inside `build_context()`

1. Run `_classify_query(query)` **always** (cheap, no I/O — runs even
   when `decoder_override` is set, for audit trail).
2. If `decoder_override` is set → caller wins for decoder selection;
   still record classifier metadata with `override_applied=True`.
   Else → use classifier's `decoder_mode`.
3. Retrieve/rerank using **existing candidate depth** (classifier does
   not limit retrieval).
4. Compute score-ratio tier from the **full candidate set** (TIGHT /
   FOCUSED / BROAD as today).
5. Apply final assembly cap as the **minimum** of all bounds:

   ```python
   max_genes_effective = min(
       score_ratio_budget,
       classifier_assembly_max_genes_cap,
       caller_max_genes_cap_if_any,
   )
   ```
6. Splice + assemble + return.

### 4.2 Core invariant

> The classifier can only **lower** the assembled gene count. It cannot
> raise it, and it cannot reduce retrieval depth. The score-ratio tier
> always sees the full candidate set.

## 5. Data Shapes

### 5.1 `ClassifierResult` (internal)

```python
@dataclass(frozen=True)
class ClassifierResult:
    cls: str                       # one of: arithmetic|factual|procedural|multi_hop|default
    signals_matched: list[str]     # e.g. ["operator:+", "keyword:total"]
    signal_count: int
    threshold_required: int
    assembly_max_genes_cap: int | None   # None for default (no cap)
    decoder_mode: str | None             # None for default (no override)
    reason: str | None             # filled on classifier_error fallback
```

### 5.2 Metadata payload

`metadata["classifier"]` shape (lands in `ContextWindow.metadata`):

```python
{
    "class": "arithmetic",
    "signals_matched": ["operator:+", "keyword:total"],
    "signal_count": 2,
    "threshold_required": 2,
    "assembly_max_genes_cap": 2,
    "max_genes_effective": 1,           # post score-ratio + caller clamps
    "decoder_selected": "minimal",
    "override_applied": False,          # True if decoder_override won
    "candidate_pool_size": 14,          # retrieval_k — distinguishes
                                        # "retrieved N" from "assembled M"
}
```

The `candidate_pool_size` field disambiguates retrieval depth from
assembled count for downstream debugging.

## 6. Failure Contract

The classifier is **infallible by construction** — pure-function
regex/keyword scan over the query string, no I/O.

- Empty/None query → return `default` immediately, no scan.
- Query truncated to **first 2,000 chars** before scanning. Long pasted
  code blocks must not make the classifier do work proportional to the
  paste.
- The whole call is wrapped in `try/except`; on any exception, log and
  return:
  ```python
  ClassifierResult(cls="default", signals_matched=[], signal_count=0,
                   threshold_required=0, assembly_max_genes_cap=None,
                   decoder_mode=None, reason="classifier_error")
  ```
- A `default` result is a true **no-op**: no decoder change, no cap,
  preserves today's behavior exactly.

## 7. Test Surface

1. **Per-class unit tests** — 3-5 representative queries per class;
   assert correct classification and `signals_matched` payload.
2. **Arithmetic threshold boundary** — 1 weak signal → falls through;
   2 weak → fires; 1 operator + 1 numeric keyword (strong) → fires.
3. **Priority test** — query matching multiple classes (e.g. "Calculate
   the critical path and then explain why") asserts `arithmetic` wins
   over `multi_hop`.
4. **Factual length-guard** — wh-word query at 14 words classifies as
   `factual`; same query padded to 16 words does not.
5. **Negative priority** — long factual question containing a stray `%`
   character does **not** become `arithmetic` unless the threshold is
   satisfied.
6. **Code-paste robustness** — query containing a pasted code block
   with operators/symbols does not trigger `arithmetic` purely from the
   paste; tests both the 2,000-char truncation and threshold guard.
7. **Override audit** — when `decoder_override` is passed, metadata
   still contains the inferred class and `override_applied=True`.
8. **No-op equivalence** — for queries that classify as `default`,
   `max_genes_effective` and decoder selection are identical to a
   baseline run with the classifier disabled.
9. **Failure contract** — synthetic exception in classifier returns
   `default` with `reason="classifier_error"` and the request still
   succeeds end-to-end.

## 8. Observability

- Per-call: `metadata["classifier"]` as specified in §5.2.
- Aggregate (Prometheus, optional v1.1): counter
  `helix_classifier_class_total{class="..."}` for class distribution
  and `helix_classifier_override_total` for caller-override audit.
  Aggregate metrics are **not** required for v1 ship; the per-call
  payload is sufficient for the first round of tuning.

## 9. Rollout

1. Land classifier behind a config flag `[classifier] enabled = true`
   in `helix.toml` (default on).
2. Ship to dev; run existing benchmark suite — confirm:
   - CritPt arithmetic queries now route to `minimal` + cap 2.
   - Factual benchmark unchanged or improved.
   - No regression on `default`-classified queries (must be exact no-op).
3. Capture one week of `metadata["classifier"]` payloads from real
   sessions. Diagnose `default`-rate: if > expected, draft v2 rules.
4. v2 candidates (separate spec): "how many/how long" conjunction;
   procedural vs multi_hop ordering revisit; embedding-NN fallback for
   high `default`-rate buckets.

## 10. Open Items / Provisional Decisions

- **`procedural` vs `multi_hop` priority ordering** — provisional. Flag
  in code comment; revisit after first procedural benchmark.
- **Aggregate Prometheus counters** — deferred to v1.1 unless an
  immediate need surfaces during initial rollout.

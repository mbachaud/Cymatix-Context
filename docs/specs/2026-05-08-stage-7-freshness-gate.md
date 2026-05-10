# Stage 7 — Freshness Gate on `know`: Stale / Cold / Superseded Demotion

Plan: helix-context retrieval-fix, Stage 7 of 6+1 (council 2026-05-08, two-reviewer convergence add-on). Layered on Stage 6's `know`/`miss` contract; closes the "high-confidence retrieval of stale source" gap. Stage 6 already pre-declared the β5 logistic slot, the three new `MissBlock.reason` values, and `agent.recommendation="refresh"` — Stage 7 fills them in.

## 1. Goals + non-goals

**Goals.**
- A `know` block can only be emitted when the top-1 gene's underlying source is fresh enough to act on.
- Stale, cold-tier-only, and superseded top-1 results downgrade to `MissBlock(reason ∈ {stale, cold, superseded})` carrying `refresh_targets`.
- Replace `mean(decay_score)` health math with three signals so a stale needle is no longer masked by fresh padding.
- New `agent.recommendation="refresh"` distinguishes "re-fetch then come back" from `"escalate"`.
- Surface heterochromatin SEMA hits as `MissBlock(reason="cold")` instead of dropping them silently.

**Non-goals.**
- No LLM in the freshness path (ribosome stays disabled).
- No KnowBlock/MissBlock redesign — extend Stage 6 additively.
- No hash-based revalidation; MVP is mtime + URL delegation to `/consolidate`.
- No auto-consolidation triggered by stale detection.
- No multi-version / point-in-time genome queries.

## 2. Surface area

| File | Lines | Change |
|---|---|---|
| `helix_context/context_manager.py` | 2255–2349 | Rewrite `_compute_health`: add freshness_min/top1/weighted; new `status="stale"` rule (§3) |
| `helix_context/context_manager.py` | 2446–2470 | Populate three new ContextHealth fields; back-compat shim sets `freshness=freshness_weighted` |
| `helix_context/context_manager.py` | `build_context` near know/miss decide | Call freshness pipeline before `decide_know_or_miss` |
| `helix_context/schemas.py` | 165–195 (`ContextHealth`) | Add `freshness_min`, `freshness_top1`, `freshness_weighted` (Optional[float]) |
| `helix_context/schemas.py` | Stage-6 `MissBlock` | Extend `reason` to add `stale|cold|superseded`; add `refresh_targets: list[str]` |
| `helix_context/schemas.py` | Stage-6 `KnowBlock` | Add `soft_stale: bool = False` |
| `helix_context/genome.py` | 471, 499 | `last_verified_at REAL` already in DDL — confirm; no new column |
| `helix_context/genome.py` | 1465–1500 (insert path) | Set `last_verified_at = now` on ingest if caller passed None |
| `helix_context/genome.py` | new method | `mark_verified(gene_ids, ts, *, read_only)` — no-op when read_only=True |
| `helix_context/genome.py` | 490+ migration block | `CREATE INDEX IF NOT EXISTS idx_genes_supersedes ON genes(supersedes) WHERE supersedes IS NOT NULL` |
| `helix_context/freshness.py` | new | `revalidate_source`, `revalidate_and_mark`, `check_superseded`; in-memory mtime cache (60s TTL) |
| `helix_context/know_decision.py` | Stage 6 helper | Three new gates (§7, §8); β5 plumbed in (§10) |
| `helix_context/know_calibration.py` | Stage 6 helper | Logistic extended to 5 features; default `β5 = +1.5` |
| `helix_context/server.py` | 974–1001 | Route MissBlock(stale/cold/superseded) → `recommendation="refresh"`; soft-stale on KnowBlock → `"refresh"` |
| `helix_context/legibility.py` | Stage-6 fragment | Append `HELIX_REFRESH_FRAGMENT` (§12) |
| `tests/test_freshness_gate.py` | new | §13 cases |

## 3. `_compute_health` rewrite

Replace the `mean(decay_score)` block at `context_manager.py:2313–2317` with three signals computed in one pass over score-desc-ordered candidates:

```python
if candidates:
    decays = [float(g.epigenetics.decay_score or 0.0) for g in candidates]
    freshness_min = min(decays)
    freshness_top1 = decays[0]
    raw_scores = [max(scores_map.get(g.gene_id, 0.0), 0.0) for g in candidates]
    s_total = sum(raw_scores) or 1.0
    weights = [s / s_total for s in raw_scores]
    freshness_weighted = sum(w * d for w, d in zip(weights, decays))
else:
    freshness_min = freshness_top1 = freshness_weighted = 0.0
```

**Status rule** (replaces the `freshness < 0.4` line at 2342):

```
if genes_expressed > 0 and (freshness_top1 < 0.4 or freshness_weighted < 0.5):
    status = "stale"
elif ellipticity >= 0.7: status = "aligned"
elif ellipticity >= 0.3: status = "sparse"
else:                    status = "denatured"
```

`freshness_top1 < 0.4` catches "the gene we'd answer from is stale". `freshness_weighted < 0.5` catches "score-weighted neighborhood is stale" — padding genes only contribute proportional to their score share, so 11 cap-fillers can no longer mathematically mask a stale needle.

**Back-compat shim.** `ContextHealth.freshness` (the only field external callers read) becomes `freshness_weighted` so it stays meaningful (a stale top-1 will pull it down) without renaming. New three fields are additive. The `status` string vocabulary (`aligned|sparse|stale|denatured`) is unchanged so OTel dashboards do not break.

## 4. `genes.last_verified_at` — already exists, wire writers

DDL inspection: `last_verified_at REAL` is already at `genome.py:471` and ALTER-block `:499`. **No column add required.** Stage 7 only ensures it is populated:

- **On ingest** (`genome.py:1465–1500`): if `gene.last_verified_at is None`, set to `now()` before INSERT. `provenance.py:394` already does this on the provenance path; mirror into bare-insert.
- **On `/consolidate`** (`server.py:2300+`): the existing UPDATE adds `last_verified_at = now()`.
- **On successful retrieval-time mtime check** (§5): `genome.mark_verified([gene_id], now_ts, read_only=read_only)` — gated; no-op under read_only.
- **Semantics.** `NULL` = "freshness unknown" (legacy rows). `decide_know_or_miss` treats NULL as **neutral**: confidence reduced via β5, but `know` still emittable on a strong score gap. Test `test_unknown_freshness_treated_as_neither_fresh_nor_stale` enforces.

No migration needed; existing snapshots already have the column. Stage-1 read_only contract preserved because `mark_verified` gates on it.

## 5. Per-gene source revalidation contract

New module `helix_context/freshness.py`:

```python
def revalidate_source(
    gene: Gene,
    *,
    mtime_cache: dict[str, tuple[float, float]],   # source_path -> (mtime, cached_at)
    now_ts: float,
    cache_ttl_s: float = 60.0,
) -> Literal["fresh", "stale", "missing", "unknown"]:
    """Compare on-disk mtime to gene.last_verified_at.

    Decision matrix:
      source_path is None                         -> "unknown"
      source_path is URL (scheme://...)           -> "unknown"  (URL flow delegated to /consolidate)
      file does not exist                         -> "missing"
      gene.last_verified_at is None               -> "unknown"
      mtime <= last_verified_at                   -> "fresh"
      mtime  > last_verified_at                   -> "stale"
    """
```

**Cache.** Keyed on `source_path`, value `(mtime, cached_at)`. Skip `os.stat` when `now_ts - cached_at < cache_ttl_s`. TTL=60s. Lives on `HelixContextManager` (per-batch state, not Genome). Evicted on `/admin/refresh`.

**read_only contract (Stage 1 boundary).** mtime cache is in-memory — populating it is NOT a genome mutation, so read_only callers MAY update the cache. They MUST NOT call `genome.mark_verified` (writes the column):

```python
def revalidate_and_mark(genome, gene, *, mtime_cache, now_ts, read_only):
    status = revalidate_source(gene, mtime_cache=mtime_cache, now_ts=now_ts)
    if status == "fresh" and not read_only:
        genome.mark_verified([gene.gene_id], now_ts, read_only=False)
    return status
```

Under `read_only=True`, "fresh" is reported for the turn but the column is not bumped; the next non-read-only call re-stats (within 60s, served from cache) and writes through.

**Where called.** In `build_context` after candidates are ranked but before `decide_know_or_miss` — only the top-K shown to the agent (default top-3, configurable as `freshness.revalidation_topk`). Bulk revalidation of all 500 pool candidates is out of scope. Latency: 3 stat calls × ~100µs warm = sub-millisecond.

## 6. Cold-tier surfacing

`query_cold_tier` already exists (`genome.py:1248`) but is not wired into the hot path. New helper `_cold_tier_peek(query, k=3)` in `context_manager.py`:

**Trigger condition.**
```
genes_expressed < 3
AND health.status != "abstain"
AND not classifier.disable_cold_tier
```

If trigger fires, call `genome.query_cold_tier(query, k=3, min_cosine=0.4)`. Threshold 0.4 is tighter than the function default (0.15): cold peek competes with `MissBlock(reason="sparse")` and we want it only on real archived hits.

**Translation to `refresh_targets`.** For each cold gene returned, take `gene.epigenetics.source_path or gene.source_id`. Emit:

```python
MissBlock(
    reason="cold",
    refresh_targets=[g.epigenetics.source_path or g.source_id for g in cold_hits],
    top_score=top_cold_cosine, ratio=1.0,
    escalate_to=[],
)
```

Cold matches are NOT auto-promoted to hot tier — the agent re-ingests by re-reading the source. Preserves the LLM-free design pillar.

## 7. Supersession check

`check_superseded(genome, gene) -> Optional[str]`. Two source paths exist:

**Path A — gene-level pointer.** `genes.supersedes TEXT` at `genome.py:473` is "this gene replaces ...". Reverse lookup ("who replaces me?"):

```sql
SELECT gene_id, source_id FROM genes WHERE supersedes = ? LIMIT 1
```

If a row exists, return `successor.source_id`.

**Path B — claim-edge chain.** `claims_graph.latest_in_chain` walks `claim_edges` for `supersedes` edges. For each top-1's claim_id, if `latest_in_chain != claim_id`, the head's gene's `source_id` is the successor.

**Stage 7 ships Path A only.** Path A is one indexed query; Path B is recursive walk plus claim→gene mapping. Add `idx_genes_supersedes` index in §2's migration. Path B is flagged Stage 7+1 if the bench surfaces gene rows whose `supersedes IS NULL` but a claim chain exists.

**Wiring** — called from `decide_know_or_miss` against top-1 only:

```python
successor_source = check_superseded(genome, top1)
if successor_source:
    return MissBlock(
        reason="superseded",
        refresh_targets=[successor_source],
        top_score=top_score, ratio=ratio, escalate_to=[],
    )
```

## 8. `MissBlock.reason` extension + `refresh_targets`

Final Stage 7 schema:

```python
class MissBlock(BaseModel):
    miss: Literal[True] = True
    reason: Literal[
        "abstain", "denatured", "sparse", "no_promoter_match",   # Stage 6
        "stale", "cold", "superseded",                           # Stage 7
    ]
    top_score: float
    ratio: float
    escalate_to: list[Literal["grep", "rag", "web", "ask_human"]] = Field(default_factory=list)
    refresh_targets: list[str] = Field(default_factory=list)     # NEW
    do_not_answer_from_genome: Literal[True] = True
```

**Field invariants** (pydantic `model_validator`):
- `reason in {"stale","cold","superseded"}` ⇒ `len(refresh_targets) >= 1` AND `escalate_to == []`.
- `reason in {"abstain","denatured","sparse","no_promoter_match"}` ⇒ `refresh_targets == []` AND `len(escalate_to) >= 1`.
- The two lists are mutually exclusive (one populated, one empty).

**JSON shape** (sibling of Stage 6 `know` key):

```json
{ "miss": {
    "miss": true, "reason": "stale",
    "top_score": 0.83, "ratio": 1.42,
    "escalate_to": [],
    "refresh_targets": ["F:/Projects/helix-context/helix_context/config.py"],
    "do_not_answer_from_genome": true
} }
```

`refresh_targets` are absolute file paths or fully-qualified URLs — same shape `RefreshTarget.source_id` carries (§11).

## 9. `agent.recommendation = "refresh"`

Sixth value, distinct from `"escalate"`. Rules append to Stage 6 at `server.py:974–1001`:

| Block | Condition | recommendation |
|---|---|---|
| `MissBlock(stale)` | top1 mtime > last_verified_at | `"refresh"` |
| `MissBlock(cold)` | cold-peek hit, no hot match | `"refresh"` |
| `MissBlock(superseded)` | top1 has successor gene | `"refresh"` |
| `MissBlock(abstain/denatured/sparse/no_promoter_match)` | (Stage 6) | `"escalate"` |
| `KnowBlock` AND `freshness_min < 0.5` AND `freshness_top1 >= 0.5` | soft-stale | `"refresh"` + `KnowBlock.soft_stale=true` |
| `KnowBlock` otherwise | normal | `"trust"`/`"verify"` |

Soft-stale-on-know is the one place a `know` block requests refresh: top-1 fresh (safe to act), but supporting context stale. `KnowBlock.soft_stale: bool = False` carries the signal; legacy parsers ignore it.

## 10. KnowBlock confidence formula extension (β5)

Stage 6 4-feature logistic becomes 5-feature:

```
z = β0
  + β1 * tanh(top_score / s_ref)
  + β2 * tanh(score_gap / g_ref)
  + β3 * (1.0 if lexical_dense_agree else 0.0)
  + β4 * coordinate_confidence
  + β5 * freshness_min                       # NEW
confidence = 1.0 / (1.0 + exp(-z))
```

`freshness_min` from §3, in `[0,1]`. **Default `β5 = +1.5`**. When all candidates' `last_verified_at IS NULL`, `freshness_min` falls back to `decay_score` (non-zero for legacy rows). Calibration via existing `scripts/calibrate_know_confidence.py` — gains a fifth coefficient column; CLI unchanged. Pre-calibration on `located_n1000`: planted-stale needles (`freshness_min ≈ 0.1`) drive `confidence < emit_floor` → fall through to `MissBlock(reason="stale")`.

## 11. Integration with `/context/refresh-plan`

Stage 7 `MissBlock(stale)` carries `refresh_targets: list[str]`; existing `/context/refresh-plan` returns `list[RefreshTarget]` where `source_id` is the equivalent string. Round-trip:

| `MissBlock` | `RefreshTarget` | Notes |
|---|---|---|
| `refresh_targets[i]` | `source_id` | Direct equivalence |
| (inferred from scheme) | `target_kind` | `"file"` if no scheme, else `"url"` |
| `reason` | `reason` | `stale`→`stale_mtime`, `cold`→`cold_tier`, `superseded`→`superseded_by_successor` |
| `1.0 - freshness_min` | `priority` | Higher = more urgent |

Adapter `MissBlock.to_refresh_targets() -> list[RefreshTarget]` lives in `schemas.py`. `/context/refresh-plan` wire format at `server.py:1174` is unchanged.

## 12. Frontier agent prompt-template extension

Append `HELIX_REFRESH_FRAGMENT` to Stage 6's `HELIX_NO_MATCH_FRAGMENT`:

```
If `recommendation` is "refresh" (NOT "escalate"):
  Stage 7 contract — the genome found a candidate but the underlying
  source is stale, archived, or superseded.

  1. DO NOT answer from the genome content (still binding).
  2. Read each path in `refresh_targets` from disk, OR fetch each URL.
     - "stale": file mtime moved past last verification — re-read.
     - "cold":  archived match — re-read to re-warm hot tier.
     - "superseded": a newer source replaces the queried one.
  3. After fetching, re-call /context with the same query.
  4. The next response will reflect the refreshed state.

"refresh" means "the answer is here, just out of date — fetch and retry."
"escalate" means "the answer is NOT here — go ask elsewhere."
These are distinct branches. Do not conflate them.
```

Identical imperative-numbered tone to Stage 6's fragment. Concatenated into the system-prompt prefix when `caller_model_class != "small_moe"`; small_moe gets a 1-line summary to preserve tokens.

## 13. Test plan

`tests/test_freshness_gate.py` (mock-only):

- `test_freshness_top1_dominates_padding` — top-1 decay=0.1, 11 padding decay=1.0 → `status="stale"` (regression vs current `mean → "aligned"`).
- `test_compute_health_back_compat_freshness_field` — `health.freshness == freshness_weighted`, reflects stale top-1 (< 0.5).
- `test_cold_tier_peek_emits_miss_cold` — heterochromatin cosine > 0.4, zero hot matches → `MissBlock(cold)` with correct `refresh_targets`.
- `test_supersession_downgrades_top1` — plant G2 with `supersedes=G1`, query retrieves G1 → `MissBlock(superseded)`, `refresh_targets=[G2.source_id]`.
- `test_revalidate_caches_mtime_60s_ttl` — two calls within 60s = one `os.stat`; +61s = two.
- `test_read_only_does_not_write_last_verified_at` — `read_only=True` mtime-pass call leaves DB column unchanged; `read_only=False` advances it.
- `test_unknown_freshness_treated_as_neither_fresh_nor_stale` — `last_verified_at IS NULL`, decay=0.9, strong gap → KnowBlock emitted (not downgraded), confidence reduced vs known-fresh.
- `test_soft_stale_know_block_recommends_refresh` — top-1 decay=0.95, ranks 2–5 decay=0.2 → KnowBlock with `soft_stale=true`, `recommendation="refresh"`.
- `test_refresh_targets_required_for_stale_cold_superseded` — pydantic validator blocks `reason="stale"` + `refresh_targets=[]` with `ValidationError`.

Reuses Stage 6 fixtures.

## 14. Acceptance criteria

- On `located_n1000` with planted-stale needles (mtime artificially aged so `freshness_min < 0.4` for planted top-1): **≥ 95%** emit `MissBlock(reason="stale")`. Bench flag `--plant-stale 0.05` ages 5% of needles.
- 11 fresh padding genes around a stale needle does **NOT** flip status from `"stale"` to `"aligned"` (regression vs `mean(decay)` math).
- Cold-tier visibility: corpus with 5% heterochromatin matches → **≥ 90%** of heterochromatin-only-match queries emit `MissBlock(reason="cold")` with non-empty `refresh_targets`, instead of `MissBlock(reason="sparse")`.
- Generic branch byte-compat: `health.freshness` shifts to `freshness_weighted`, equals `mean(decay)` exactly when all weights are equal — verify on the 100-query golden set.
- All Stage 1–6 tests stay green; `pytest tests/ -m "not live" -v` exits 0.

## 15. Out of scope

- Hash-based revalidation (`content_hash` exists; mtime is cheaper and sufficient for MVP).
- URL revalidation logic itself (delegated to `/consolidate`).
- Auto-consolidation triggered by stale detection (would write in the read flow — violates Stage 1's read_only contract).
- Per-tenant freshness policies (party-aware mtime tolerances).
- Multi-version / point-in-time genome queries.
- Path B supersession via `claim_edges` chains.
- Auto-retuning of `β5` outside the existing Stage 6 calibration script.

---

**Surface-area summary:** 1 health-math rewrite, 1 new freshness module, 2 schema extensions (additive), 3 know/miss decision branches, 1 prompt fragment appended, 1 test file. Zero new columns, zero new endpoints, zero LLM calls. Headline behavioral change: a stale top-1 needle can no longer produce a `KnowBlock`.

# Retrieval Stack Upgrade — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four sequenced improvements to helix retrieval: BM25 pre-filter, sub-query decomposition, D8 completion (intent taxonomy + SR + entity graph), and BGE-M3 dense vectors with ANN threshold-based dynamic gene counts.

**Architecture:** Each step is independently shippable and flag-gated. Steps 1 and 2 share the same retrieval path and compose naturally (sub-queries hit the pre-filter). Step 3 (D8) unlocks LLM-free decomposition and makes Step 2's LLM dependency optional. Step 4 is a separate architectural addition to ΣĒMA cold-tier and the confidence-tiering system.

**Tech Stack:** Python 3.14, SQLite FTS5 (BM25 native), sentence-transformers, spaCy, pytest. No new runtime dependencies until Step 4 (adds `sentence-transformers` BGE-M3 model).

---

## Background for implementers

helix-context retrieves "genes" (document chunks, ~18,500 in production) from a SQLite genome and compresses them into LLM context. The `/context` endpoint calls `HelixContextManager.build_context()` which runs a 5-step pipeline:

- **Step 0a** — query classifier (regex, identifies query class + gene count cap)
- **Step 0** — `_expand_query_intent()` (optional LLM keyword expansion)
- **Step 1** — `genome.query_genes()` (9-tier scorer over the full corpus)
- **Step 2** — `_apply_candidate_refiners()` (confidence tiers: TIGHT=3 genes, FOCUSED=6, BROAD=12)
- **Steps 3-5** — splice, assemble, replicate

**The two observed failure modes from benchmarks:**
1. Broad queries ("how does density gate work") → flat score distribution → BROAD tier → 12 diluted genes → 2.7x token savings vs RAG's 8000 tokens
2. Point-fact queries ("what port does helix use") → factual classifier cap clips BROAD(12) → 5 clean genes → 20x token savings

**Key files:**
- `helix_context/genome.py` — `query_genes()` at line 1684; BM25 post-filter at lines 2289–2330; FTS5 schema at line 614
- `helix_context/context_manager.py` — `build_context()` at line 664; `_expand_query_intent()` at line 1482; confidence tiers at lines 818–908; classifier cap at lines 974–990
- `helix_context/config.py` — `RetrievalConfig` at ~line 254; `bm25_shortlist_enabled=False` at line 260
- `helix_context/query_classifier.py` — `classify_query()` at line 116; caps per class at lines 132–175
- `helix_context/schemas.py` — `PromoterTags` at line 45; `intent: str = ""` at line 49
- `helix_context/tagger.py` — `_extract_intent()` at line 565; gene tagging pipeline at line 252
- `helix_context/sema.py` — ΣĒMA codec; `SentenceTransformer("all-MiniLM-L6-v2")` at line 123
- `helix.toml` — `bm25_shortlist_enabled = true` at line 241 (note: config.py default is `False`; toml overrides)

---

## Step 1: BM25 pre-filter (post → pre, N=200)

**What:** The BM25 shortlist currently fires as a post-filter — all 18,500 genes are scored by all 9 tiers, then the result is intersected with FTS5 top-50. Moving it to a pre-filter means only the FTS5 top-200 genes enter the scoring pipeline, reducing the SEMA cosine scan from 17,000×20 to 200×20 (~85× cheaper) and eliminating noise candidates before any tier touches them.

**Files:**
- Modify: `helix_context/genome.py` — add `_bm25_prefilter()` helper; thread `candidate_set` into tiers 1–3.5
- Modify: `helix_context/config.py` — add `bm25_prefilter_enabled: bool = False` and `bm25_prefilter_size: int = 200`
- Modify: `helix.toml` — add `bm25_prefilter_enabled = false` (dark ship, A/B against existing post-filter)
- Test: `tests/test_genome.py` (add cases) or `tests/test_bm25_prefilter.py` (new)

---

- [ ] **1.1 — Write failing test: pre-filter reduces candidate set**

```python
# tests/test_bm25_prefilter.py
import pytest
from helix_context.genome import Genome

@pytest.fixture
def genome_with_noise(genome):
    """Genome with one exact-match gene and 50 noisy genes sharing common terms."""
    # Insert the signal gene
    genome.upsert_gene(make_gene(
        content="The helix proxy port is 11437.",
        source_id="helix.toml",
        key_values=["port=11437"],
    ))
    # Insert noise: 50 genes that mention "port" but for different services
    for i in range(50):
        genome.upsert_gene(make_gene(
            content=f"Service {i} listens on port {8000+i}. General networking config.",
            source_id=f"service_{i}.conf",
        ))
    return genome

def test_prefilter_keeps_signal_gene(genome_with_noise):
    """BM25 pre-filter with a tight query should surface the exact-match gene."""
    genome_with_noise._bm25_prefilter_enabled = True
    genome_with_noise._bm25_prefilter_size = 10  # tight — only top-10 BM25 hits
    genes = genome_with_noise.query_genes(
        domains=["helix"], entities=["port", "11437"], max_genes=5
    )
    gene_sources = [g.source_id for g in genes]
    assert "helix.toml" in gene_sources, (
        "Signal gene should survive pre-filter; got sources: " + str(gene_sources)
    )

def test_prefilter_reduces_candidates_scored(genome_with_noise, monkeypatch):
    """Pre-filter should mean far fewer genes pass through tier scoring."""
    scored_ids = []
    original_tier1 = genome_with_noise._score_tier1  # hypothetical
    # (Instrument via monkeypatch or check via genome.last_query_scores size)
    genome_with_noise._bm25_prefilter_enabled = True
    genome_with_noise._bm25_prefilter_size = 10
    genome_with_noise.query_genes(domains=["helix"], entities=["port"], max_genes=5)
    assert len(genome_with_noise.last_query_scores) <= 12  # pre-filter capped input

def test_prefilter_empty_shortlist_fallback(genome):
    """If FTS5 returns nothing, pre-filter must fall through to full scoring."""
    genome._bm25_prefilter_enabled = True
    genome._bm25_prefilter_size = 5
    # Query with nonsense terms that won't match FTS5
    genes = genome.query_genes(domains=["xyzzy_nonexistent_abc"], entities=[], max_genes=3)
    # Should not raise, should return something (or empty if genome is tiny)
    assert isinstance(genes, list)
```

- [ ] **1.2 — Run tests to confirm they fail**

```bash
cd F:/Projects/helix-context
python -m pytest tests/test_bm25_prefilter.py -v 2>&1 | head -30
```
Expected: `FAILED` — `_bm25_prefilter_enabled` attribute doesn't exist yet.

- [ ] **1.3 — Add config fields to `config.py`**

In `RetrievalConfig` (around line 260, after the existing `bm25_shortlist_size`):

```python
# BM25 pre-filter (2026-05-08 retrieval stack upgrade).
# When enabled, query_genes fires a BM25 FTS5 pass BEFORE tier scoring,
# restricting the candidate pool to the top-N BM25 hits. This is the
# inverse of bm25_shortlist (post-filter). Dark ship — compare A/B
# against bm25_shortlist before enabling. Requires bm25_shortlist to be
# OFF to avoid double-filtering.
bm25_prefilter_enabled: bool = False
bm25_prefilter_size: int = 200     # must be >= max_genes * 2 (= 24)
```

- [ ] **1.4 — Wire config into `Genome.__init__`**

In `genome.py`, find where `_bm25_shortlist_enabled` is assigned from config (search for `_bm25_shortlist_enabled`). Add alongside it:

```python
self._bm25_prefilter_enabled: bool = getattr(retrieval_cfg, "bm25_prefilter_enabled", False)
self._bm25_prefilter_size: int = getattr(retrieval_cfg, "bm25_prefilter_size", 200)
```

- [ ] **1.5 — Implement `_bm25_candidate_set()` helper in `genome.py`**

Add as a method of `Genome`, before `query_genes`:

```python
def _bm25_candidate_set(
    self,
    query_terms: list[str],
    size: int,
) -> set[str] | None:
    """Return FTS5 BM25 top-N gene_ids, or None if FTS unavailable/empty.

    Returns None (not empty set) when the shortlist should be ignored,
    so callers can distinguish "BM25 found nothing" from "BM25 disabled".
    Soft-fails to None on any exception.
    """
    if not self._fts_available:
        return None
    bm25_terms = [t for t in query_terms if len(t) > 2]
    if not bm25_terms:
        return None
    try:
        bm25_match = " OR ".join(f'"{t}"' for t in bm25_terms)
        cur = self.read_conn.cursor()
        rows = cur.execute(
            "SELECT gene_id FROM genes_fts "
            "WHERE genes_fts MATCH ? ORDER BY rank LIMIT ?",
            (bm25_match, size),
        ).fetchall()
        if not rows:
            return None  # Empty → fall through; don't filter
        return {r["gene_id"] for r in rows}
    except Exception:
        log.warning("BM25 pre-filter failed — falling back to full corpus", exc_info=True)
        return None
```

- [ ] **1.6 — Insert pre-filter call at the top of `query_genes()`**

In `query_genes()` at `genome.py:1727`, immediately after `limit = max_genes * 2`:

```python
# ── BM25 pre-filter (2026-05-08) ─────────────────────────────────────
# When enabled, fire BM25 FIRST to build a candidate_set. All tier
# queries below are restricted to this set via an extra gene_id filter.
# Falls back to None (full-corpus) if FTS returns nothing or errors.
_prefilter_set: set[str] | None = None
if self._bm25_prefilter_enabled:
    _prefilter_set = self._bm25_candidate_set(query_terms, self._bm25_prefilter_size)
    log.debug(
        "bm25 prefilter: size=%d result=%s",
        self._bm25_prefilter_size,
        len(_prefilter_set) if _prefilter_set is not None else "fallback",
    )
```

- [ ] **1.7 — Add `_prefilter_set` guard to Tier 1, 2, and 3 SQL queries**

For each tier's SQL in `query_genes()`, add a `gene_id IN (...)` sub-clause when `_prefilter_set` is not None. **Important:** Tiers 1 and 2 use the table alias `g` (e.g. `g.gene_id`); Tiers 3 and 3.5 do not use an alias. Build two variants:

```python
# Build the IN clauses — two forms for aliased vs non-aliased tiers
_prefilter_aliased_clause = ""    # for Tier 1, 2 (use g.gene_id)
_prefilter_bare_clause = ""       # for Tier 3, 3.5 (no alias)
_prefilter_params: list = []
if _prefilter_set is not None:
    ph = ",".join("?" * len(_prefilter_set))
    _prefilter_aliased_clause = f" AND g.gene_id IN ({ph})"
    _prefilter_bare_clause = f" AND gene_id IN ({ph})"
    _prefilter_params = list(_prefilter_set)
```

Then in each tier's SQL string, append the appropriate clause before `ORDER BY` or `LIMIT`, and append `_prefilter_params` to the parameter list. Apply:
- Tier 1 exact tag match (search `"SELECT gene_id FROM promoter_index"`) → uses `g` alias → `_prefilter_aliased_clause`
- Tier 2 prefix match (search `"LIKE ?"`) → uses `g` alias → `_prefilter_aliased_clause`
- Tier 3 FTS5 (`genes_fts MATCH` tier query — NOT the BM25 post-filter block) → no alias → `_prefilter_bare_clause`
- Tier 3.5 SPLADE lookup → no alias → `_prefilter_bare_clause`
- Tier 4 SEMA Mode A (`WHERE g.gene_id IN (...)` already present — intersect with `_prefilter_set` in Python after the query, not in SQL)

**Important:** Leave SEMA Mode B new-candidate injection unfiltered — it's the fallback when hot tiers produce nothing. Pre-filter only applies to hot-tier scoring.

- [ ] **1.8 — Remove or demote the BM25 post-filter block**

The existing post-filter at `genome.py:2289–2330` should be disabled when `_bm25_prefilter_enabled` is True (to avoid double-filtering). Add a guard:

```python
if (
    getattr(self, "_bm25_shortlist_enabled", False)
    and not getattr(self, "_bm25_prefilter_enabled", False)  # add this guard
    and self._fts_available
    and gene_scores
):
```

- [ ] **1.9 — Add `bm25_prefilter_enabled = false` to `helix.toml`**

Under the existing BM25 shortlist config block (around line 241):

```toml
# BM25 pre-filter — fires BEFORE tier scoring (inverse of shortlist).
# Enable for A/B against bm25_shortlist. Disable shortlist when using this.
bm25_prefilter_enabled = false
bm25_prefilter_size = 200
```

- [ ] **1.10 — Run tests**

```bash
python -m pytest tests/test_bm25_prefilter.py tests/test_genome.py -v
```
Expected: all pass.

- [ ] **1.11 — Smoke test against live Helix**

Enable the pre-filter by temporarily editing `helix.toml` (the env var approach does NOT work — the flag is read at startup from config, not env):

```toml
# helix.toml — temporary change for smoke test
bm25_prefilter_enabled = true
bm25_shortlist_enabled = false   # disable post-filter to avoid double-filtering
```

Restart Helix, then:

```bash
python -c "
import httpx
r = httpx.post('http://127.0.0.1:11437/context', json={'query': 'what port does helix use'}, timeout=30)
print(r.json()[0]['content'][:200])
"
```

Revert `helix.toml` after the test (both flags back to their prior values: `bm25_shortlist_enabled = true`, `bm25_prefilter_enabled = false`).

- [ ] **1.12 — Commit**

```bash
git add helix_context/genome.py helix_context/config.py helix.toml tests/test_bm25_prefilter.py
git commit -m "feat: BM25 pre-filter tier-0 (dark ship, bm25_prefilter_enabled=false)"
```

---

## Step 2: Sub-query decomposition

**What:** For broad queries (`multi_hop` and `default` classifier classes), decompose the query into 3 point-fact sub-queries using a single LLM call, run each sub-query through `_prepare_query_signals → _express` in parallel, union the gene sets (weighting genes that appear in multiple sub-queries higher), and feed the union to the existing `_apply_candidate_refiners`. Each sub-query independently triggers the TIGHT/FOCUSED confidence tier, yielding 3–6 targeted genes per sub-query instead of 12 diluted genes for the whole broad query. Flag-gated; LRU-cached at 256 entries.

**Files:**
- Modify: `helix_context/context_manager.py` — add `_decompose_query()`, modify `build_context()` dispatch
- Modify: `helix_context/config.py` — add `query_decomposition_enabled: bool = False` to `RibosomeConfig`
- Modify: `helix.toml` — add `query_decomposition_enabled = false`
- Test: `tests/test_sub_query.py` (new)

---

- [ ] **2.1 — Write failing tests**

```python
# tests/test_sub_query.py
import pytest
from unittest.mock import patch, MagicMock
from helix_context.context_manager import HelixContextManager

def test_decompose_returns_list_of_strings(ctx_manager):
    """_decompose_query must return a list of 2-4 strings for a broad query."""
    result = ctx_manager._decompose_query("how does the density gate work")
    assert isinstance(result, list)
    assert 2 <= len(result) <= 4
    for q in result:
        assert isinstance(q, str) and len(q) > 5

def test_decompose_is_cached(ctx_manager):
    """Calling _decompose_query twice with the same input must not call LLM twice."""
    with patch.object(ctx_manager.ribosome.backend, "complete", return_value=
        "1. what is the density gate score threshold?\n"
        "2. what triggers density gate demotion?\n"
        "3. which chromatin tier does density gate assign?") as mock_complete:
        ctx_manager._decompose_query("how does the density gate work")
        ctx_manager._decompose_query("how does the density gate work")
        assert mock_complete.call_count == 1, "LLM must only be called once (cached)"

def test_decompose_disabled_by_default(ctx_manager):
    """When query_decomposition_enabled=False, _decompose_query returns [original]."""
    ctx_manager.config.ribosome.query_decomposition_enabled = False
    result = ctx_manager._decompose_query("how does the density gate work")
    assert result == ["how does the density gate work"]

def test_build_context_uses_decomposition_for_broad(ctx_manager, genome_with_density_gate_genes):
    """build_context on a multi_hop query should retrieve more targeted genes."""
    ctx_manager.config.ribosome.query_decomposition_enabled = True
    window = ctx_manager.build_context("how does the density gate work and what thresholds does it use")
    # With decomposition, should get genes from multiple sub-topics
    content = window.expressed_context
    assert "density" in content.lower()

def test_gene_appearing_in_multiple_subqueries_ranks_higher(ctx_manager):
    """A gene matching 2/3 sub-queries should outrank genes matching 1/3."""
    # This is a contract test — verify the union weighting logic
    from helix_context.context_manager import _merge_subquery_candidates
    from helix_context.schemas import Gene
    gene_a = MagicMock(spec=Gene); gene_a.gene_id = "aaa"
    gene_b = MagicMock(spec=Gene); gene_b.gene_id = "bbb"
    # gene_a appears in 2 sub-results, gene_b in 1
    sub_results = [[gene_a, gene_b], [gene_a], [gene_b]]
    merged = _merge_subquery_candidates(sub_results, base_scores={
        "aaa": 3.0, "bbb": 4.0,  # gene_b has higher base score
    })
    merged_ids = [g.gene_id for g in merged]
    assert merged_ids.index("aaa") < merged_ids.index("bbb"), (
        "gene_a (2 sub-query hits) should rank above gene_b (1 hit, higher base score)"
    )
```

- [ ] **2.2 — Run tests, confirm failure**

```bash
python -m pytest tests/test_sub_query.py -v 2>&1 | head -20
```
Expected: `FAILED` — `_decompose_query` and `_merge_subquery_candidates` don't exist.

- [ ] **2.3 — Add config flag to `RibosomeConfig` in `config.py`**

Find `RibosomeConfig` (search for `query_expansion_enabled`). Add:

```python
# Sub-query decomposition (2026-05-08 retrieval stack upgrade).
# When enabled, build_context decomposes multi_hop/default queries into
# 3 point-fact sub-queries via one LLM call. Each sub-query is run
# independently through the retrieval pipeline; results are union-merged
# with a cross-query hit multiplier. LRU-cached at 256 entries.
# Dark ship — requires query_expansion_enabled=true (same LLM backend).
query_decomposition_enabled: bool = False
```

- [ ] **2.4 — Implement `_merge_subquery_candidates()` as a module-level function in `context_manager.py`**

Add **before** the `HelixContextManager` class definition (NOT inside it — the test imports it as a bare function: `from helix_context.context_manager import _merge_subquery_candidates`):

```python
def _merge_subquery_candidates(
    sub_results: list[list["Gene"]],
    base_scores: dict[str, float],
) -> list["Gene"]:
    """Merge gene lists from multiple sub-queries.

    Genes appearing in more sub-queries are ranked higher, regardless of
    base score. Within the same appearance count, base_score is the
    tiebreaker. Returns a deduplicated list ordered by (hit_count DESC,
    base_score DESC).
    """
    from collections import Counter
    seen: dict[str, "Gene"] = {}  # gene_id → Gene object (first encountered)
    hit_counts: Counter = Counter()
    for sub_list in sub_results:
        for gene in sub_list:
            hit_counts[gene.gene_id] += 1
            if gene.gene_id not in seen:
                seen[gene.gene_id] = gene
    return sorted(
        seen.values(),
        key=lambda g: (hit_counts[g.gene_id], base_scores.get(g.gene_id, 0.0)),
        reverse=True,
    )
```

- [ ] **2.5 — Implement `_decompose_query()` in `HelixContextManager`**

Add after `_expand_query_intent()` (around line 1545):

```python
def _decompose_query(self, query: str) -> list[str]:
    """Decompose a broad query into 2-4 point-fact sub-queries.

    Returns [query] unchanged when:
    - query_decomposition_enabled is False
    - no LLM backend is available
    - the LLM call fails

    Results are LRU-cached at 256 entries (same pattern as _expand_query_intent).
    Only called for multi_hop and default classifier classes.
    """
    if not getattr(self, "_decompose_cache", None):
        self._decompose_cache: dict[str, list[str]] = {}
    if query in self._decompose_cache:
        return self._decompose_cache[query]

    if not getattr(
        getattr(self.config, "ribosome", None), "query_decomposition_enabled", False
    ):
        self._decompose_cache[query] = [query]
        return [query]

    if not hasattr(self.ribosome, "backend") or getattr(
        self.ribosome.backend, "is_disabled_backend", False
    ):
        self._decompose_cache[query] = [query]
        return [query]

    system = (
        "You are a retrieval query decomposer. Given a broad question, output "
        "2 to 4 SHORT, SPECIFIC sub-questions that together answer it. Each "
        "sub-question must be answerable from a single fact or rule. "
        "Format: one sub-question per line, numbered. No prose, no headings."
    )
    prompt = f"Broad question: {query}\n\nSub-questions:"

    try:
        raw = self.ribosome.backend.complete(prompt, system=system, temperature=0.0)
        import re as _re
        # Parse numbered lines: "1. ...", "2. ...", etc.
        sub_qs = [
            _re.sub(r"^\d+\.\s*", "", line).strip()
            for line in raw.strip().splitlines()
            if _re.match(r"^\d+\.", line.strip()) and len(line.strip()) > 10
        ]
        if not 2 <= len(sub_qs) <= 4:
            sub_qs = [query]  # Malformed output → passthrough
    except Exception:
        log.debug("Query decomposition failed, using raw query", exc_info=True)
        sub_qs = [query]

    if len(self._decompose_cache) > 256:
        self._decompose_cache.clear()
    self._decompose_cache[query] = sub_qs
    return sub_qs
```

- [ ] **2.6 — Wire decomposition into `build_context()`**

In `build_context()`, after the classifier runs (after line ~720) and before `_prepare_query_signals` is called (around line ~770), add:

```python
# Step 0b: Sub-query decomposition for broad/multi_hop queries.
# Only fires when decomposition is enabled AND the classifier identifies
# a broad query (multi_hop or default class = no cap or high cap).
# Factual/arithmetic/procedural queries already hit TIGHT/FOCUSED
# naturally and should NOT be decomposed.
_use_decomposition = (
    classifier_result is not None
    and classifier_result.cls in ("multi_hop", "default")
    and getattr(
        getattr(self.config, "ribosome", None),
        "query_decomposition_enabled", False,
    )
)
_sub_queries: list[str] = (
    self._decompose_query(query) if _use_decomposition else [query]
)
```

Then replace the single `_prepare_query_signals` + `_express` call with a dispatch over `_sub_queries`:

```python
if len(_sub_queries) == 1:
    # Normal path — unchanged behaviour
    expanded_q, domains, entities = self._prepare_query_signals(
        _sub_queries[0], session_context
    )
    candidates = self._express(expanded_q, domains, entities, ...)
else:
    # Multi-sub-query path
    import concurrent.futures
    def _run_sub(sq: str) -> list:
        eq, d, e = self._prepare_query_signals(sq, session_context)
        return self._express(eq, d, e, max_genes=self.config.max_genes_per_turn)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(_sub_queries)) as pool:
        sub_results = list(pool.map(_run_sub, _sub_queries))
    base_scores = {
        gid: s
        for sub in sub_results
        for gid, s in (self.genome.last_query_scores or {}).items()
    }
    candidates = _merge_subquery_candidates(sub_results, base_scores)
    # Clip to max_genes * 2 (same as single-path returns)
    candidates = candidates[: self.config.max_genes_per_turn * 2]
```

- [ ] **2.7 — Add `query_decomposition_enabled = false` to `helix.toml`**

Under the `[ribosome]` section:

```toml
# Sub-query decomposition — decomposes broad queries into 3 point-fact sub-queries.
# Requires query_expansion_enabled = true (same LLM backend).
query_decomposition_enabled = false
```

- [ ] **2.8 — Run all tests**

```bash
python -m pytest tests/test_sub_query.py tests/test_pipeline.py tests/test_integration.py -v
```
Expected: all pass.

- [ ] **2.9 — Validate end-to-end with a broad query**

```python
# Run from the helix-context directory with decomposition enabled in helix.toml
import httpx
r = httpx.post('http://127.0.0.1:11437/context', json={
    'query': 'how does the density gate work and what thresholds does it use'
}, timeout=60)
data = r.json()
print(f"Genes: {data[0].get('genes_expressed')}")
print(data[0]['content'][:400])
```

- [ ] **2.10 — Commit**

```bash
git add helix_context/context_manager.py helix_context/config.py helix.toml tests/test_sub_query.py
git commit -m "feat: sub-query decomposition for broad queries (dark ship, query_decomposition_enabled=false)"
```

---

## Step 3: D8 completion — intent taxonomy + SR + entity graph

**What D8 currently is:** The co-activation graph dimension (D8) is marked `[▓▓▓░]` wired-to-retrieval in `DIMENSIONS.md`. Harmonic links (227k edges) and ray-trace are live; SR (Successor Representation) is dark-shipped; `entity_graph` is populated but not a first-class retrieval path.

**What completion means — three sub-tasks:**

**3A — SR enablement:** `seeded_edges_enabled` is already in config, `sr_boost` exists in `query_genes()`. Enable, bench, confirm ≥2pp improvement (the D8 gate criterion from DIMENSIONS.md line 206). If it regresses, it was not production-ready — document why and leave dark.

**3B — Intent taxonomy:** `PromoterTags.intent` is populated at ingest (first sentence / docstring) but is free-text, not categorised. Add a structured `intent_class` field with a fixed taxonomy so sub-query routing (Step 2) can use templates instead of LLM decomposition — making it LLM-free.

**3C — Entity graph as first-class retrieval signal:** `entity_graph` is populated but `query_genes()` does not read it. Wire it as Tier 5b (alongside harmonic) to boost genes that share entity nodes with the query.

**Files:**
- Modify: `helix_context/schemas.py` — add `IntentClass` enum and `intent_class` field to `PromoterTags`
- Modify: `helix_context/tagger.py` — add `_classify_intent()` to assign `IntentClass` at ingest
- New: `helix_context/intent_router.py` — sub-query templates keyed by `IntentClass`
- Modify: `helix_context/context_manager.py` — wire `IntentClass`-based routing as fallback in `_decompose_query()` when LLM is off
- Modify: `helix_context/genome.py` — add Tier 5b entity graph boost; SR flag enable
- Modify: `helix_context/config.py` — add `intent_taxonomy_enabled`, `entity_graph_retrieval_enabled`, `sr_enabled` (rename from dark ship)
- Modify: `helix.toml` — expose new flags
- Modify: `docs/architecture/DIMENSIONS.md` — update D8 status to `[████]`
- Test: `tests/test_intent_taxonomy.py`, `tests/test_d8_entity_graph.py`

---

### 3A — SR enablement

- [ ] **3A.1 — Check current SR config knobs**

```bash
grep -n "sr_enabled\|sr_boost\|seeded_edges" helix_context/config.py helix.toml
```

Note the exact field names and current defaults before proceeding.

- [ ] **3A.2 — Enable SR in a bench-only helix.toml override**

Do NOT flip the production default yet. Run the dimensional lock bench with SR on:

```bash
N=50 SEED=42 HELIX_MODEL=gemma4:e4b \
  OUTPUT=benchmarks/results/sr_on_n50.json \
  python benchmarks/bench_dimensional_lock.py
```

Compare `sr_on_n50.json` axis-1 through axis-4 recall against the overnight `dimensional_lock_n50_e4b_2026-05-08_0012.json` baseline (axis recall curve: 8→6→12→34%).

- [ ] **3A.3 — Gate decision**

If SR adds ≥2pp on any axis without regressing others → flip `sr_enabled = true` in `helix.toml` and update DIMENSIONS.md D8 status.
If neutral or regressive → document in `docs/architecture/DIMENSIONS.md` D8 notes. Leave dark.

- [ ] **3A.4 — Commit SR result**

```bash
git add helix.toml docs/architecture/DIMENSIONS.md benchmarks/results/sr_on_n50.json
git commit -m "feat(d8): enable SR after bench gate — Xpp improvement on axis-N recall"
# or:
git commit -m "doc(d8): SR bench result — gate not met, leaving dark-shipped"
```

---

### 3B — Intent taxonomy

- [ ] **3B.1 — Write failing tests**

```python
# tests/test_intent_taxonomy.py
from helix_context.schemas import PromoterTags, IntentClass
from helix_context.tagger import GeneTagExtractor

def test_promoter_tags_has_intent_class():
    """PromoterTags must have an intent_class field with IntentClass type."""
    tags = PromoterTags()
    assert hasattr(tags, "intent_class")
    assert tags.intent_class == IntentClass.UNKNOWN

def test_tagger_assigns_mechanism_class():
    """Content describing how something works → IntentClass.MECHANISM."""
    tagger = GeneTagExtractor.__new__(GeneTagExtractor)
    cls = tagger._classify_intent(
        "The density gate computes a score and assigns chromatin state."
    )
    assert cls == IntentClass.MECHANISM

def test_tagger_assigns_config_knob_class():
    """Content with key=value facts → IntentClass.CONFIG_KNOB."""
    tagger = GeneTagExtractor.__new__(GeneTagExtractor)
    cls = tagger._classify_intent("bm25_shortlist_size = 50")
    assert cls == IntentClass.CONFIG_KNOB

def test_tagger_assigns_data_structure_class():
    """Content defining a table/schema → IntentClass.DATA_STRUCTURE."""
    tagger = GeneTagExtractor.__new__(GeneTagExtractor)
    cls = tagger._classify_intent(
        "CREATE TABLE genes (gene_id TEXT, content TEXT, chromatin INTEGER)"
    )
    assert cls == IntentClass.DATA_STRUCTURE
```

- [ ] **3B.2 — Run tests, confirm failure**

```bash
python -m pytest tests/test_intent_taxonomy.py -v 2>&1 | head -15
```

- [ ] **3B.3 — Add `IntentClass` enum to `schemas.py`**

After `ChromatinState` (around line 43):

```python
class IntentClass(str, Enum):
    """Structured intent taxonomy for sub-query routing (D8 completion).

    Assigned at ingest time by GeneTagExtractor._classify_intent().
    Used by intent_router.py to select sub-query templates for LLM-free
    query decomposition (the zero-LLM path for Step 2).
    """
    UNKNOWN = "unknown"
    MECHANISM = "mechanism"        # How something works ("how does X work?")
    CONFIG_KNOB = "config_knob"    # Configuration values, thresholds, flags
    DATA_STRUCTURE = "data_structure"  # Tables, schemas, data layouts
    PROCESS_STEP = "process_step"  # Sequential procedures, pipelines
    TRIGGER_CONDITION = "trigger_condition"  # If/when conditions, gates
    FACT = "fact"                  # Point facts, measurements, counts
    RELATIONSHIP = "relationship"  # How A relates to B
```

- [ ] **3B.4 — Add `intent_class` to `PromoterTags`**

Include ALL existing fields verbatim — only append `intent_class`. Do NOT remove or reorder `intent: str = ""` (tagger.py at line 267 writes to it):

```python
class PromoterTags(BaseModel):
    """Retrieval metadata — how the genome finds this gene."""
    domains: List[str] = Field(default_factory=list)
    entities: List[str] = Field(default_factory=list)
    intent: str = ""
    intent_class: IntentClass = IntentClass.UNKNOWN  # NEW — append here
    summary: str = ""
    sequence_index: Optional[int] = None
    metadata: dict = Field(default_factory=dict)
```

Existing serialized genes have no `intent_class` field in their JSON blob. Pydantic v2 populates the default (`UNKNOWN`) on deserialization — no migration needed for reads. The backfill script in Step 3B.10 upgrades the stored values in place.

- [ ] **3B.5 — Implement `_classify_intent()` in `tagger.py`**

Add after `_extract_intent()` (around line 587):

```python
def _classify_intent(self, text: str) -> "IntentClass":
    """Classify a gene's intent text into the IntentClass taxonomy.

    Pure heuristic — no model calls. Checks pattern priority order.
    Fast enough to run on every gene at ingest time.
    """
    from .schemas import IntentClass
    import re
    t = text.lower()
    # CONFIG_KNOB: key=value patterns or common config terms
    if re.search(r'\b\w+\s*[=:]\s*[\w\d.]+', text) or any(
        kw in t for kw in ("threshold", "timeout", "limit", "enabled", "config", "setting", "flag")
    ):
        return IntentClass.CONFIG_KNOB
    # DATA_STRUCTURE: SQL DDL or schema terms
    if re.search(r'\bcreate\s+table\b|\bschema\b|\bcolumn\b|\bindex\b', t):
        return IntentClass.DATA_STRUCTURE
    # TRIGGER_CONDITION: conditional logic
    if any(kw in t for kw in ("when ", "if ", "trigger", "gate", "condition", "fires when")):
        return IntentClass.TRIGGER_CONDITION
    # PROCESS_STEP: sequential language
    if any(kw in t for kw in ("step ", "pipeline", "then ", "followed by", "sequence")):
        return IntentClass.PROCESS_STEP
    # MECHANISM: explanatory language
    if any(kw in t for kw in ("how ", "computes", "calculates", "works by", "operates")):
        return IntentClass.MECHANISM
    # FACT: measurement/count language
    if re.search(r'\b\d+\b', text) and any(kw in t for kw in ("is ", "are ", "has ", "= ")):
        return IntentClass.FACT
    return IntentClass.UNKNOWN
```

- [ ] **3B.6 — Wire `_classify_intent()` into `tag_gene()` in `tagger.py`**

In `tag_gene()` (around line 252), after `intent = self._extract_intent(...)`:

```python
from .schemas import IntentClass as _IntentClass
intent_class = self._classify_intent(intent)
```

Then in the `PromoterTags(...)` constructor call (around line 264):

```python
promoter=PromoterTags(
    domains=domains[:10],
    entities=entities[:15],
    intent=intent,
    intent_class=intent_class,   # add this line
    summary=summary,
    sequence_index=sequence_index,
),
```

- [ ] **3B.7 — Create `helix_context/intent_router.py`**

```python
"""Intent-based sub-query router — LLM-free decomposition path (D8).

Maps IntentClass values to sub-query template functions. Used by
HelixContextManager._decompose_query() when the LLM backend is
unavailable or query_decomposition_enabled=False.

Templates produce 3 point-fact sub-queries for each broad intent class.
"""
from __future__ import annotations
from .schemas import IntentClass


_TEMPLATES: dict[IntentClass, list[str]] = {
    IntentClass.MECHANISM: [
        "what triggers {subject}?",
        "what does {subject} compute or produce?",
        "what are the inputs and outputs of {subject}?",
    ],
    IntentClass.CONFIG_KNOB: [
        "what is the default value of {subject}?",
        "what does changing {subject} affect?",
        "where is {subject} configured?",
    ],
    IntentClass.DATA_STRUCTURE: [
        "what columns or fields does {subject} have?",
        "what is stored in {subject}?",
        "how is {subject} populated?",
    ],
    IntentClass.TRIGGER_CONDITION: [
        "what condition activates {subject}?",
        "what happens when {subject} fires?",
        "what prevents {subject} from triggering?",
    ],
    IntentClass.PROCESS_STEP: [
        "what is the first step of {subject}?",
        "what does each stage of {subject} produce?",
        "what are the inputs to {subject}?",
    ],
    IntentClass.FACT: [
        "what is the exact value of {subject}?",
        "where is {subject} defined?",
        "what uses {subject}?",
    ],
    IntentClass.RELATIONSHIP: [
        "how does {a} depend on {b}?",
        "what does {a} provide to {b}?",
        "can {a} exist without {b}?",
    ],
}


def sub_queries_for(query: str, intent_class: IntentClass) -> list[str]:
    """Return 3 point-fact sub-queries for a broad query given its intent class.

    Falls back to [query] if the class has no template or subject extraction
    fails (e.g., IntentClass.UNKNOWN).
    """
    templates = _TEMPLATES.get(intent_class)
    if not templates:
        return [query]
    # Extract subject: everything after the first verb-like word
    import re
    m = re.search(r'\b(how|what|why|where|when|which)\b\s+(does|is|are|do|did)?\s*(.+)',
                  query, re.IGNORECASE)
    subject = m.group(3).rstrip("?. ") if m else query
    return [t.format(subject=subject, a=subject, b=subject) for t in templates[:3]]
```

- [ ] **3B.8 — Wire `intent_router` as LLM-free fallback in `_decompose_query()`**

In the method from Step 2.5, before the LLM call block, add:

```python
# LLM-free path: if a candidate gene's intent_class is known for this query,
# use template-based decomposition instead of an LLM call.
# Only available when the genome has intent_class data on candidate genes.
# (Populated at ingest after Step 3B ships.)
```

And in the `if not query_decomposition_enabled` branch:

```python
# Attempt template-based decomposition using intent router (no LLM)
from .intent_router import sub_queries_for, IntentClass
# Use the classifier's best-guess class to select templates
if classifier_result and classifier_result.cls in ("multi_hop", "default"):
    # Without a gene to read intent_class from, use query text heuristics
    from .tagger import GeneTagExtractor
    _tagger = GeneTagExtractor.__new__(GeneTagExtractor)
    guessed_class = _tagger._classify_intent(query)
    if guessed_class != IntentClass.UNKNOWN:
        sub_qs = sub_queries_for(query, guessed_class)
        self._decompose_cache[query] = sub_qs
        return sub_qs
# else: true passthrough
self._decompose_cache[query] = [query]
return [query]
```

- [ ] **3B.9 — Run tests**

```bash
python -m pytest tests/test_intent_taxonomy.py tests/test_sub_query.py -v
```

- [ ] **3B.10 — Backfill intent_class on existing genes**

```bash
python -c "
from helix_context.genome import Genome
from helix_context.tagger import GeneTagExtractor
from helix_context.schemas import IntentClass
import json

g = Genome('F:/Projects/helix-context/genomes/main/genome.db')
tagger = GeneTagExtractor.__new__(GeneTagExtractor)
conn = g.conn
cur = conn.cursor()
rows = cur.execute('SELECT gene_id, promoter FROM genes WHERE promoter IS NOT NULL').fetchall()
updated = 0
for row in rows:
    p = json.loads(row['promoter'])
    if p.get('intent_class', 'unknown') != 'unknown':
        continue
    intent_text = p.get('intent', '')
    cls = tagger._classify_intent(intent_text).value
    p['intent_class'] = cls
    cur.execute('UPDATE genes SET promoter = ? WHERE gene_id = ?',
                (json.dumps(p), row['gene_id']))
    updated += 1
conn.commit()
print(f'Backfilled {updated} genes')
"
```

- [ ] **3B.11 — Commit 3B**

```bash
git add helix_context/schemas.py helix_context/tagger.py helix_context/intent_router.py \
        helix_context/context_manager.py tests/test_intent_taxonomy.py
git commit -m "feat(d8): intent taxonomy — IntentClass on PromoterTags, template-based sub-query routing"
```

---

### 3C — Entity graph as first-class retrieval signal

- [ ] **3C.1 — Audit entity_graph table**

```bash
python -c "
import sqlite3
c = sqlite3.connect('F:/Projects/helix-context/genomes/main/genome.db')
c.row_factory = sqlite3.Row
cols = [r[1] for r in c.execute('PRAGMA table_info(entity_graph)')]
print('columns:', cols)
n = c.execute('SELECT COUNT(*) FROM entity_graph').fetchone()[0]
sample = c.execute('SELECT * FROM entity_graph LIMIT 3').fetchall()
print(f'{n} rows')
for r in sample:
    print(dict(r))
"
```

Note the schema before proceeding — the Tier 5b implementation depends on column names.

- [ ] **3C.2 — Write failing test**

```python
# tests/test_d8_entity_graph.py
def test_entity_graph_tier_boosts_shared_entity_gene(genome):
    """Gene sharing an entity with query terms should score higher with entity graph on."""
    from helix_context.schemas import Gene, PromoterTags, EpigeneticMarkers
    from helix_context.genome import Genome as _Genome

    def _make(content, source):
        g = Gene(
            gene_id=_Genome.make_gene_id(content),
            content=content,
            complement="",
            codons=[],
            promoter=PromoterTags(domains=["helix"], entities=["port", "11437"]),
            epigenetics=EpigeneticMarkers(),
            source_id=source,
        )
        return g

    genome.upsert_gene(_make("The helix proxy runs on port 11437.", "helix.toml"))
    genome.upsert_gene(_make("This is an unrelated gene about steam.", "steam.json"))
    # Insert entity_graph edge: entity "11437" → helix.toml gene
    # (would be populated at ingest in production)

    genes_with = genome.query_genes(
        domains=["helix"], entities=["11437"], max_genes=5,
        use_entity_graph=True,
    )
    genes_without = genome.query_genes(
        domains=["helix"], entities=["11437"], max_genes=5,
        use_entity_graph=False,
    )
    # With entity graph, helix.toml gene should rank higher
    ids_with = [g.source_id for g in genes_with]
    assert ids_with[0] == "helix.toml", f"Expected helix.toml first, got {ids_with}"
```

- [ ] **3C.3 — Add `use_entity_graph` parameter to `query_genes()`**

In `genome.py:1684`, extend the signature:

```python
def query_genes(
    self,
    domains: List[str],
    entities: List[str],
    max_genes: int = 8,
    party_id: Optional[str] = None,
    use_harmonic: bool = True,
    use_sr: Optional[bool] = None,
    use_entity_graph: bool = False,  # add this
    read_only: bool = False,
) -> List[Gene]:
```

- [ ] **3C.4 — Implement Tier 5b entity graph boost in `query_genes()`**

After the existing Tier 5 harmonic boost block, add:

```python
# ── Tier 5b: entity graph co-occurrence boost ─────────────────────
# Genes that share entity nodes with the query terms get a score
# multiplier proportional to the edge weight in entity_graph.
# This fires after harmonic so it's additive, not competitive.
if use_entity_graph and entities:
    try:
        _eg_t0 = time.monotonic()
        # entity_graph: entity_text → gene_id with weight
        # Exact schema depends on table — see audit in 3C.1
        eq_ph = ",".join("?" * len(entities))
        eg_rows = cur.execute(
            f"SELECT gene_id, weight FROM entity_graph "
            f"WHERE entity IN ({eq_ph})",
            entities,
        ).fetchall()
        for row in eg_rows:
            gid, w = row["gene_id"], row.get("weight", 1.0)
            if gid in gene_scores:
                bonus = min(w * 0.5, 2.0)  # cap boost at +2.0
                gene_scores[gid] += bonus
                tier_contrib.setdefault(gid, {})["entity_graph"] = bonus
        log.debug("tier 5b entity_graph: %.1fms", (time.monotonic() - _eg_t0) * 1000)
    except Exception:
        log.warning("entity_graph tier failed", exc_info=True)
```

- [ ] **3C.5 — Add config flag and wire into `build_context`**

In `config.py RetrievalConfig`:
```python
entity_graph_retrieval_enabled: bool = False
```

In `genome.py __init__`, wire from config (same pattern as `_bm25_prefilter_enabled`).

In `context_manager.py`, pass `use_entity_graph=self.genome._entity_graph_enabled` in the `query_genes()` call within `_express()`.

- [ ] **3C.6 — Run tests**

```bash
python -m pytest tests/test_d8_entity_graph.py tests/test_genome.py -v
```

- [ ] **3C.7 — Update DIMENSIONS.md D8 status**

Change `[▓▓▓░]` to `[████]` for "Wired to retrieval" once SR gate is decided and entity graph is live.

- [ ] **3C.8 — Commit 3C**

```bash
git add helix_context/genome.py helix_context/config.py helix.toml \
        tests/test_d8_entity_graph.py docs/architecture/DIMENSIONS.md
git commit -m "feat(d8): entity graph as Tier 5b retrieval signal — D8 complete"
```

---

## Step 4: BGE-M3 dense vectors + ANN threshold → dynamic gene counts

**What:** Replace the fixed TIGHT(3)/FOCUSED(6)/BROAD(12) step function with a similarity-threshold gate. Store full 768D BGE-M3 embeddings per gene (using Matryoshka truncation to 256D for space efficiency). At query time, embed the query with the `retrieval.query`-equivalent task, compute cosine similarity against all candidates, and include genes until similarity drops below a calibrated threshold `T`. This makes gene count dynamic per query rather than pre-determined by score-ratio buckets.

BGE-M3 is preferred over Jina v3 because its sparse output integrates naturally with helix's existing SPLADE tier (Tier 3.5), reducing it to one model serving both dense and sparse paths.

**Files:**
- New: `helix_context/bgem3_codec.py` — BGE-M3 encode/decode, replaces `sema.py` for the dense path
- Modify: `helix_context/genome.py` — add `gene_embedding_dense` column; populate at ingest; ANN similarity query
- Modify: `helix_context/context_manager.py` — replace ratio-based confidence tier with threshold gate (flag-gated)
- Modify: `helix_context/config.py` — add `dense_embedding_enabled`, `ann_threshold`, `ann_threshold_model`
- Modify: `helix.toml` — expose new flags
- New: `scripts/backfill_bgem3.py` — one-shot re-embed of all existing genes
- Test: `tests/test_bgem3_codec.py`, `tests/test_ann_threshold.py`

---

- [ ] **4.1 — Install BGE-M3**

```bash
pip install FlagEmbedding sentence-transformers
python -c "from FlagEmbedding import BGEM3FlagModel; print('ok')"
```

If `FlagEmbedding` is unavailable, fall back to `sentence-transformers` with `BAAI/bge-m3`.

- [ ] **4.2 — Write failing tests**

```python
# tests/test_bgem3_codec.py
from helix_context.bgem3_codec import BGEM3Codec

def test_encode_returns_256d_vector():
    codec = BGEM3Codec(dim=256)
    vec = codec.encode("what port does helix use?", task="query")
    assert len(vec) == 256
    assert all(isinstance(v, float) for v in vec)

def test_passage_and_query_vectors_are_compatible():
    """Query and passage vectors in the same space (dot product > 0 for relevant pair)."""
    codec = BGEM3Codec(dim=256)
    q_vec = codec.encode("what is the helix proxy port?", task="query")
    p_vec = codec.encode("The helix proxy listens on port 11437.", task="passage")
    unrelated = codec.encode("Steam game price history.", task="passage")
    import numpy as np
    score_relevant = np.dot(q_vec, p_vec)
    score_noise = np.dot(q_vec, unrelated)
    assert score_relevant > score_noise, "Relevant passage must score higher than noise"

# tests/test_ann_threshold.py
def test_threshold_returns_fewer_genes_for_precise_query(genome_with_bgem3):
    """Precise query should return fewer genes than broad query."""
    narrow = genome_with_bgem3.query_genes_ann(
        "what port does helix use", threshold=0.4, max_genes=12
    )
    broad = genome_with_bgem3.query_genes_ann(
        "how does helix work", threshold=0.4, max_genes=12
    )
    assert len(narrow) <= len(broad), "Narrow query should yield fewer or equal genes"

def test_threshold_zero_returns_max_genes(genome_with_bgem3):
    """threshold=0.0 should return up to max_genes (no filtering)."""
    genes = genome_with_bgem3.query_genes_ann("helix", threshold=0.0, max_genes=5)
    assert len(genes) == 5
```

- [ ] **4.3 — Implement `helix_context/bgem3_codec.py`**

```python
"""BGE-M3 dense encoder for helix gene embedding (Step 4, 2026-05-08).

Wraps BAAI/bge-m3 via sentence-transformers (or FlagEmbedding if available).
Supports Matryoshka dimension truncation: default 256D for storage efficiency.
Asymmetric: query and passage tasks use different internal instruction prefixes.
"""
from __future__ import annotations
import numpy as np
import logging

log = logging.getLogger("helix.bgem3")

_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
_PASSAGE_INSTRUCTION = ""  # BGE-M3 passage side is instruction-free


class BGEM3Codec:
    def __init__(self, dim: int = 256, device: str = "cpu"):
        self.dim = dim
        self._model = None
        self._device = device

    def _load(self):
        if self._model is not None:
            return
        try:
            from FlagEmbedding import BGEM3FlagModel
            self._model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)
            self._backend = "flagembedding"
        except ImportError:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer("BAAI/bge-m3", device=self._device)
            self._backend = "sentence_transformers"
        log.info("BGE-M3 loaded via %s, dim=%d", self._backend, self.dim)

    def encode(self, text: str, task: str = "passage") -> list[float]:
        """Encode text to a self.dim-dimensional float vector.

        task: "query" prepends the query instruction; "passage" is bare.
        """
        self._load()
        if task == "query":
            text = _QUERY_INSTRUCTION + text
        if self._backend == "flagembedding":
            out = self._model.encode([text], batch_size=1,
                                     max_length=512)["dense_vecs"]
            vec = out[0]
        else:
            vec = self._model.encode(text, normalize_embeddings=True)
        # Matryoshka truncation + re-normalize
        vec = np.array(vec[:self.dim], dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tolist()

    def similarity(self, vec_a: list[float], vec_b: list[float]) -> float:
        a = np.array(vec_a, dtype=np.float32)
        b = np.array(vec_b, dtype=np.float32)
        return float(np.dot(a, b))
```

- [ ] **4.4 — Add `gene_embedding_dense` column migration**

```python
# scripts/backfill_bgem3.py
"""One-shot BGE-M3 re-embedding of all genes. Run once after Step 4 ships."""
import sqlite3, json, time
from helix_context.bgem3_codec import BGEM3Codec

DB = "F:/Projects/helix-context/genomes/main/genome.db"
DIM = 256

codec = BGEM3Codec(dim=DIM)
conn = sqlite3.connect(DB)
cur = conn.cursor()

# Add column if missing
try:
    cur.execute("ALTER TABLE genes ADD COLUMN embedding_dense TEXT")
    conn.commit()
    print("Added embedding_dense column")
except Exception:
    print("Column already exists")

rows = cur.execute(
    "SELECT gene_id, content FROM genes WHERE embedding_dense IS NULL"
).fetchall()
print(f"Re-embedding {len(rows)} genes at dim={DIM}...")
for i, (gene_id, content) in enumerate(rows):
    vec = codec.encode(content[:2000], task="passage")  # truncate at 2000 chars
    cur.execute("UPDATE genes SET embedding_dense = ? WHERE gene_id = ?",
                (json.dumps(vec), gene_id))
    if i % 100 == 0:
        conn.commit()
        print(f"  {i}/{len(rows)}")
conn.commit()
print("Done.")
```

- [ ] **4.5 — Run backfill (takes ~20-60 min on CPU; run in background)**

```bash
python scripts/backfill_bgem3.py 2>&1 | tee benchmarks/logs/bgem3_backfill.log &
```

While it runs, continue with the config and context_manager changes.

- [ ] **4.6 — Add config flags**

In `config.py RetrievalConfig`:

```python
# BGE-M3 dense ANN retrieval + dynamic gene counts (Step 4, 2026-05-08).
dense_embedding_enabled: bool = False      # Dark ship until backfill complete
dense_embedding_dim: int = 256             # Matryoshka truncation dimension
ann_similarity_threshold: float = 0.35    # Include genes until sim drops below this
ann_threshold_min_genes: int = 1           # Never return 0 genes from threshold gate
ann_threshold_max_genes: int = 12          # Hard ceiling (same as max_genes_per_turn)
```

- [ ] **4.7 — Add `query_genes_ann()` method to `Genome`**

This is a NEW method alongside `query_genes()`, not a replacement. It fetches the query's dense vector, loads `embedding_dense` for all hot-tier candidates (from the existing 9-tier result), then reorders and filters by cosine similarity threshold:

```python
def query_genes_ann(
    self,
    query: str,
    threshold: float = 0.35,
    max_genes: int = 12,
    min_genes: int = 1,
    domains: list[str] | None = None,
    entities: list[str] | None = None,
) -> list[Gene]:
    """Threshold-based ANN retrieval using BGE-M3 dense vectors.

    1. Run standard query_genes() to get up to max_genes * 2 candidates.
    2. Embed the query with BGE-M3 (query task).
    3. Load embedding_dense for each candidate from DB.
    4. Sort candidates by similarity to query vector.
    5. Include candidates until similarity drops below threshold.
    6. Always return at least min_genes (the top-1 gene).
    """
    domains = domains or []
    entities = entities or []
    candidates = self.query_genes(domains, entities, max_genes=max_genes)
    if not candidates or not self._dense_codec:
        return candidates

    query_vec = self._dense_codec.encode(query, task="query")

    # Load dense vectors from DB
    gene_ids = [g.gene_id for g in candidates]
    ph = ",".join("?" * len(gene_ids))
    rows = self.read_conn.cursor().execute(
        f"SELECT gene_id, embedding_dense FROM genes WHERE gene_id IN ({ph})",
        gene_ids,
    ).fetchall()
    dense_map = {}
    for r in rows:
        if r["embedding_dense"]:
            import json as _json
            dense_map[r["gene_id"]] = _json.loads(r["embedding_dense"])

    # Score and filter
    # Un-embedded genes (backfill incomplete) fall back to their original
    # query_genes rank position with sim=0.0 so min_genes is always honoured.
    base_rank = {g.gene_id: i for i, g in enumerate(candidates)}
    scored = []
    for gene in candidates:
        vec = dense_map.get(gene.gene_id)
        if vec is not None:
            sim = self._dense_codec.similarity(query_vec, vec)
        else:
            # No dense vector yet — assign a score just below threshold so it
            # can still be pulled in by the min_genes floor, but won't crowd out
            # properly-embedded genes that cleared the threshold.
            sim = threshold - 0.01
        scored.append((gene, sim))
    scored.sort(key=lambda x: x[1], reverse=True)

    result = []
    for gene, sim in scored:
        if sim >= threshold or len(result) < min_genes:
            result.append(gene)
        else:
            break
    return result[:max_genes]
```

- [ ] **4.8 — Wire `query_genes_ann` as optional path in `build_context()`**

In `context_manager.py`, in the `_express()` call block, add:

```python
if self.genome._dense_embedding_enabled:
    candidates = self.genome.query_genes_ann(
        query=expanded_q,
        threshold=self.config.retrieval.ann_similarity_threshold,
        max_genes=self.config.max_genes_per_turn,
        domains=domains,
        entities=entities,
    )
else:
    candidates = self._express(expanded_q, domains, entities, ...)
```

When ANN path is active, the existing TIGHT/FOCUSED/BROAD confidence tier is bypassed — the threshold gate IS the count decision. The classifier cap (Step 3.6, line 974) still applies afterward as a hard ceiling.

- [ ] **4.9 — Calibrate threshold on benchmark**

```bash
# Run needle bench at multiple thresholds to find the sweet spot
for T in 0.25 0.30 0.35 0.40 0.45; do
    N=50 SEED=42 HELIX_MODEL=gemma4:e4b \
    ANN_THRESHOLD=$T \
    OUTPUT="benchmarks/results/ann_threshold_${T}_n50.json" \
    python benchmarks/bench_needle_1000.py
    echo "T=$T done"
done
```

Pick the threshold where answer_accuracy is highest. Update `ann_similarity_threshold` in `helix.toml`.

- [ ] **4.10 — Run all tests**

```bash
python -m pytest tests/test_bgem3_codec.py tests/test_ann_threshold.py \
    tests/test_pipeline.py tests/test_integration.py -v
```

- [ ] **4.11 — Commit**

```bash
git add helix_context/bgem3_codec.py helix_context/genome.py helix_context/context_manager.py \
        helix_context/config.py helix.toml scripts/backfill_bgem3.py \
        tests/test_bgem3_codec.py tests/test_ann_threshold.py
git commit -m "feat: BGE-M3 dense vectors + ANN threshold dynamic gene counts (dark ship)"
```

---

## Sequence and dependencies

```
Step 1 (BM25 pre-filter)          ──► ship independently, A/B vs post-filter
    ↓
Step 2 (sub-query decomposition)   ──► uses pre-filter automatically if enabled
    ↓
Step 3A (SR enablement)            ──► bench-gated, independent of 1/2
Step 3B (intent taxonomy)          ──► enables LLM-free path in Step 2
Step 3C (entity graph Tier 5b)     ──► adds retrieval signal, independent
    ↓
Step 4 (BGE-M3 + ANN threshold)   ──► replaces confidence tiers; requires backfill first
```

Each step is independently flag-gated. The whole system remains on the current retrieval path until each flag is flipped. Steps 1–3 are additive and non-breaking. Step 4 is a more significant architectural change but is isolated behind `dense_embedding_enabled = false`.

## Key test commands

```bash
# Run all tests
python -m pytest tests/ -v --ignore=tests/test_hardware_cuda_real.py

# Run just retrieval stack tests
python -m pytest tests/test_bm25_prefilter.py tests/test_sub_query.py \
    tests/test_intent_taxonomy.py tests/test_d8_entity_graph.py \
    tests/test_bgem3_codec.py tests/test_ann_threshold.py -v

# Quick smoke test against live Helix
python -c "
import httpx
for q in ['what port does helix use', 'how does the density gate work']:
    r = httpx.post('http://127.0.0.1:11437/context', json={'query': q}, timeout=30)
    d = r.json()[0]
    print(f'{q[:40]}: genes={d.get(\"genes_expressed\",\"?\")} tokens={len(d.get(\"content\",\"\").split())}')
"
```

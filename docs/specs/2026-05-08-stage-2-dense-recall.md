# Stage 2 — Dense as First-Class Recall

Plan: helix-context retrieval-fix, Stage 2 of 6 (council 2026-05-08). Depends on Stage 1 for measurement; independently mergeable code-side.

## 1. Goals + non-goals

**Goals.** Promote BGE-M3 dense retrieval from a 12-candidate re-ranker to a parallel first-class recall source returning top-K=500 over the full 18.9k-document corpus. Restore full 1024-dim BGE-M3 vectors. Decouple `pool_size` (recall breadth) from `max_genes` (final cut). Keep retrieval LLM-free, flat-numpy, and back-compat with `query_genes_ann` callers.

**Non-goals.** RRF / score fusion (Stage 3). Threshold recalibration (Stage 4). HNSW / sqlite-vec / USearch (later, if scan latency exceeds budget at >100k documents).

## 2. Surface area

| File:line | Change |
|---|---|
| `helix_context/genome.py:451-477` (documents DDL) | Add `embedding_dense_v2 BLOB` column (alter-block at lines 490-503 pattern). |
| `helix_context/genome.py:369-374` (KnowledgeStore `__init__` config wires) | Read new `dense_pool_size` config; keep existing keys; emit deprecation warn. |
| `helix_context/genome.py:2516-2521` (`_get_dense_codec`) | Pass `dim=1024` (new default); reuse instance. |
| `helix_context/genome.py:2523-2600` (`query_genes_ann`) | Refactor: split pool/cut, fan out lexical + dense in parallel, union, body-fetch once. |
| `helix_context/genome.py` (new method) | `query_genes_dense_recall(query, *, k=500, party_id, read_only) -> List[tuple[str, float]]`. |
| `helix_context/genome.py` (new attr in `__init__`) | `self._dense_matrix: np.ndarray | None`, `self._dense_matrix_ids: list[str] | None`, `self._dense_matrix_lock: threading.Lock`. |
| `helix_context/genome.py` (new method) | `_load_dense_matrix(force=False)` — bulk-read v2 BLOBs into fp32 (n,1024). |
| `helix_context/genome.py` (existing inserts) | Hook `_invalidate_dense_matrix()` after every document insert/update path. |
| `helix_context/bgem3_codec.py:17` | Default `dim=1024`. |
| `helix_context/bgem3_codec.py:53-58` | Drop `vec[:self.dim]` truncation when `dim==raw_dim`; keep renormalize. |
| `scripts/backfill_bgem3_v2.py` (new) | Re-encode all documents at 1024-dim, write BLOB to `embedding_dense_v2`. |
| `scripts/bench_dense_recall_latency.py` (new) | p50/p95 micro-bench for matmul scan. |
| `helix.toml:250-257` | Add `dense_pool_size = 500`, `dense_embedding_dim = 1024`; comment-mark `ann_similarity_threshold` as needing Stage-4 recalibration. |
| `tests/test_dense_recall.py` (new) | Tests enumerated in §9. |

## 3. Storage migration

**Strategy: parallel column, not in-place.** Keep `embedding_dense TEXT` (JSON, 256-dim) intact during the transition. Add `embedding_dense_v2 BLOB` (raw little-endian fp32, `dim*4` bytes). When v2 is fully populated, retrieval code reads v2 only. The legacy column is dropped in a follow-up release once a snapshot rotation has confirmed no rollback need.

**Why BLOB.** 18.9k × 1024 × 4 = 77.6 MiB raw vs ~600 MiB JSON-encoded text. Parsing 600 MiB of JSON on cold start is the dominant cost we are removing — BLOB → `np.frombuffer` is zero-copy.

**SQL migration (executed by `_init_db` + idempotent `ALTER` block, mirroring lines 490-503):**

```sql
ALTER TABLE genes ADD COLUMN embedding_dense_v2 BLOB;
CREATE INDEX IF NOT EXISTS idx_genes_dense_v2_hot
    ON genes(gene_id) WHERE embedding_dense_v2 IS NOT NULL AND chromatin < 2;
```

The partial index streams hot-tier (lifecycle tier < HETEROCHROMATIN) populated rows during the partial-rollout window without a table scan. Heterochromatin (lifecycle tier=2) documents are intentionally excluded — they remain reachable via the separate `query_cold_tier` path.

**Backfill (`scripts/backfill_bgem3_v2.py`)** mirrors `backfill_bgem3.py` but:
- Initialises `BGEM3Codec(dim=1024)`.
- Encodes `(content or "")[:2000]` as `task="passage"`.
- Writes `vec.astype('<f4').tobytes()` into `embedding_dense_v2`.
- Idempotent: `WHERE embedding_dense_v2 IS NULL`. Re-running is a no-op once complete.
- Batched commit every 100 rows, identical to existing script.

The May-8 snapshot is invalidated by design — operators run the backfill once after pulling Stage 2.

## 4. In-memory vector matrix

**Layout.** `self._dense_matrix: np.ndarray` of shape `(n_genes, 1024)`, dtype `float32`, C-contiguous. Sister array `self._dense_matrix_ids: list[str]` parallel to row index. Both held on the `Genome` instance.

**Lazy-load contract.** Built on first call to `query_genes_dense_recall`. Loader pseudocode:

```python
with self._dense_matrix_lock:
    if self._dense_matrix is not None: return
    rows = read_conn.execute(
        "SELECT gene_id, embedding_dense_v2 FROM genes "
        "WHERE embedding_dense_v2 IS NOT NULL "
        "AND chromatin < ?",
        (HETEROCHROMATIN,)  # = 2
    ).fetchall()
    ids, blobs = zip(*[(r["gene_id"], r["embedding_dense_v2"]) for r in rows])
    buf = b"".join(blobs)
    mat = np.frombuffer(buf, dtype="<f4").reshape(len(ids), self._dense_embedding_dim)
    self._dense_matrix = np.ascontiguousarray(mat)  # decouple from buffer lifetime
    self._dense_matrix_ids = list(ids)
```

**Hot-tier only.** Dense recall scans hot-tier (lifecycle tier < HETEROCHROMATIN) documents only — matches the existing hot-tier convention at [genome.py:1135](../../helix_context/genome.py#L1135) and [:1182-1184](../../helix_context/genome.py#L1182-L1184). Cold-tier documents are reachable via the separate `query_cold_tier()` path. Stage 7 surfaces cold-tier matches as `MissBlock(reason="cold", refresh_targets=[...])` rather than silently filtering them — see Stage 7 §6.

**Invalidation.** Coverage-based + count-based. After every insert/update path (ingest, persist, consolidate) call `self._invalidate_dense_matrix()` which sets `_dense_matrix=None`. Rebuild is full, not incremental — at 78 MiB and 18.9k rows, full rebuild is ≤ 200 ms and triggered only on first query after an ingest batch. (Incremental append is a future optimisation if ingest QPS rises.) An explicit refresh tick on `/admin/refresh` also calls `_invalidate_dense_matrix(force=True)`.

**Fallback.** If v2 coverage is partial (`count(v2) < 0.95 * count(genes)`), `query_genes_dense_recall` logs a one-time warn and returns `[]`; callers degrade to lexical-only.

## 5. New recall function signature

```python
def query_genes_dense_recall(
    self,
    query: str,
    *,
    k: int = 500,
    party_id: Optional[str] = None,
    read_only: bool = False,
) -> List[tuple[str, float]]:
    """Top-K dense recall over the full corpus. ID + cosine only.

    Does NOT load gene bodies. Body load happens in query_genes_ann via
    a single batched `_load_genes_by_ids`.
    """
```

Body. Encode query (BGE-M3 query task, full 1024-dim). Ensure matrix loaded. Compute `sims = self._dense_matrix @ query_vec` (vectors are L2-normalised, so dot = cosine). `top_idx = np.argpartition(-sims, k)[:k]`; sort that slice descending; map indices to ids. `party_id` filtering applies post-rank against the per-document `party_id` column (cheap dict lookup against an in-memory id→party map cached alongside the matrix).

## 6. Pool/cut separation in `query_genes_ann`

**New signature** (additive — old positional `max_genes` preserved):

```python
def query_genes_ann(
    self,
    query: str,
    threshold: float | None = None,
    max_genes: int | None = None,
    min_genes: int | None = None,
    domains: list[str] | None = None,
    entities: list[str] | None = None,
    party_id: Optional[str] = None,
    use_harmonic: bool = True,
    use_sr: Optional[bool] = None,
    use_entity_graph: Optional[bool] = None,
    read_only: bool = False,
    *,
    pool_size: int | None = None,   # NEW
) -> List[Gene]: ...
```

**Body sketch.**

1. Resolve `pool_size = pool_size or self._dense_pool_size` (default 500 when dense enabled, else falls back to `max_genes` for back-compat).
2. **Parallel recall** (sequential calls; "parallel" = independent sources, both feed pool):
   - `lex_candidates = self.query_genes(domains, entities, max_genes=pool_size, ...)` — returns up to `pool_size` Documents by lexical/promoter/harmonic/SR scoring.
   - `dense_pairs = self.query_genes_dense_recall(query, k=pool_size, party_id=party_id, read_only=read_only)` — returns up to `pool_size` `(gene_id, cosine)` pairs. Returns `[]` when `dense_embedding_enabled=False`.
3. **Union** by gene_id. For ranking inside this stage: keep dense cosine where present; lexical-only candidates get `sim = threshold - 0.01` (preserves existing min_genes-fill behavior for un-embedded documents). **Stage 3 will replace this with RRF — out of scope here.**
4. Sort by sim descending, `result_ids = [...][:max_genes]` after applying min_genes / threshold logic identical to current lines 2594-2599.
5. Single batched body load via `_load_genes_by_ids(result_ids)` preserving rank order. Lexical Documents from step 2a are reused (no re-fetch) for ids already materialised.

`min_genes` / `threshold` semantics unchanged. Dense candidates that didn't make the lexical cut are still subject to the same threshold gate.

## 7. Codec change

`bgem3_codec.py`:

- Line 17: `def __init__(self, dim: int = 1024, ...)` — flip default.
- Lines 53-54: drop the `vec = vec[:self.dim]` line **when `dim == raw_dim`**. Keep a guard `if self.dim < vec.shape[0]: vec = vec[:self.dim]` so future Matryoshka-sanctioned dims (1024/768/512) still work, but 256 is no longer the path. Renormalize unchanged.
- `sentence_transformers` branch (line 50): `normalize_embeddings=True` is already correct for 1024-d.
- Add a one-time warn in `_load` when `self.dim not in (1024, 768, 512)`: `"Non-Matryoshka dim=%d may produce degenerate cosine geometry"`.

## 8. Threshold deferral

Existing `ann_similarity_threshold = 0.35` was calibrated against 256-dim collapsed geometry. At 1024-dim, random-pair cosine drops materially; the absolute threshold becomes invalid. Stage 2 does **not** recalibrate.

Stage 2 emits a **single warn** at KnowledgeStore init when `dense_embedding_enabled=True` AND v2 coverage > 0:

```python
log.warning(
    "ann_similarity_threshold=%.3f is calibrated for dim=256; "
    "v2 vectors are dim=%d. Threshold recalibration is Stage 4. "
    "Recall pool is independent of threshold.",
    self._ann_threshold, self._dense_embedding_dim,
)
```

Threshold still gates the final cut in `query_genes_ann` to preserve current behavior — under-tuned threshold may over-include, but `max_genes` is the hard cap, so blast radius is bounded.

## 9. Test plan

`tests/test_dense_recall.py`:

- `test_dense_recall_finds_needle_outside_top12_lexical` — seed corpus with 1 needle document whose content shares only synonyms (not surface tokens) with query; verify `query_genes_dense_recall(k=500)` returns the needle, AND that `query_genes(max_genes=12)` does NOT.
- `test_query_genes_ann_pool_size_independent_of_max_genes` — assert `len(union of lex+dense)` reaches ≥ 100 with `pool_size=500` even when `max_genes=12`. Final return ≤ 12.
- `test_codec_full_1024_dim_on_encode` — `BGEM3Codec().encode("hello", "passage")` returns len 1024; norm ≈ 1.0; random-pair cosine on 1k pairs has mean < 0.10 (regression guard against the dim=256 collapse).
- `test_v2_blob_roundtrip_matches_json` — encode same passage, write JSON via legacy path and BLOB via v2 path, decode both, assert `np.allclose` within fp32 epsilon.
- `test_backfill_v2_idempotent` — run `backfill_bgem3_v2.py` twice; second run touches 0 rows; matrix shape unchanged.
- `test_dense_matrix_invalidation_after_insert` — call recall, insert document, call recall again, assert new document appears in pool.

## 10. Back-compat

`query_genes_ann` keeps positional `(query, threshold, max_genes, min_genes, ...)` order. `pool_size` is **kw-only**. When `dense_embedding_enabled=False`, `pool_size` defaults to `max_genes` (current behavior — lexical-only, no dense pass). When `True`, default 500. Existing callers in `context_manager.py`, `server.py`, and tests work unmodified. Legacy `embedding_dense` (TEXT JSON) column is read by no code path after this stage but stays in the schema for one release.

## 11. Latency budget + measurement

**Target.** Flat `np.dot(matrix, q)` at (18934, 1024) fp32, contiguous: ~78 MiB streamed. Numpy with OpenBLAS / MKL: 3-7 ms on a modern CPU; 10-15 ms on older laptops without BLAS. Encode cost (BGE-M3 query, single text, CPU) dominates at 30-80 ms — already present in current pipeline, so net Δ vs current is the matmul + argpartition + body-fetch difference.

**Bench.** `scripts/bench_dense_recall_latency.py`:
- 50-query warm-up, 200-query measured.
- Reports p50 / p95 / p99 for: (a) matmul scan only, (b) full `query_genes_dense_recall`, (c) full `query_genes_ann` with `pool_size=500`.
- Compares against `query_genes_ann` baseline at `dense_pool_size=12` (current behavior).
- Pass criterion: full `query_genes_ann` p95 ≤ baseline + 20 ms.

## 12. Acceptance criteria

1. `bench_n1000` reports `located_n1000 ≥ 60%` with Stage 2 alone (i.e., no RRF / no threshold recalib). Current 13.8% at 12-candidate dense re-rank.
2. `/context` endpoint p95 latency at corpus=18.9k does not exceed current p95 by more than 20 ms.
3. `tests/` pass (mock + live).
4. `backfill_bgem3_v2.py` completes on a fresh `genomes/main/genome.db` clone and reports 100% v2 coverage.
5. No new compressor / LLM calls on the `/context` path (auditable via existing OTel `tier_fired_counter`).

## 13. Out of scope

- **Stage 3:** RRF (Reciprocal Rank Fusion) over `(lex_score, dense_cosine)` replacing the `threshold - 0.01` placeholder.
- **Stage 4:** Margin-over-random threshold recalibration at 1024-dim. Likely lands at ~0.55-0.65 cosine but will be measured.
- **Stage 5+:** sqlite-vec / USearch / HNSW. Triggered when corpus exceeds ~100k or scan p95 exceeds 25 ms.
- Dropping legacy `embedding_dense TEXT` column (deferred one release).
- Per-document party-aware matrix sharding (single-tenant assumption holds at current scale).

---

**Key files referenced:** `helix_context/genome.py` lines 369-374, 451-477, 490-503, 2516-2600; `helix_context/bgem3_codec.py` lines 17, 49-58; `scripts/backfill_bgem3.py` (template for v2); `helix.toml` lines 250-257.

# Parallel + Sharded-Multi-Writer Ingest for `build_fixture_matrix.py` — Issue #92

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut fixture-matrix rebuild time from hours to tens of minutes by adding opt-in (a) file-level parallel ingest for monolithic profiles and (b) shard-level parallel ingest for sharded profiles. SPLADE encoding moves to batched calls in both new paths.

**Architecture:** Three independent phases. Phase 1 plumbs an optional `splade_sparse` kwarg through `upsert_doc` / `sync_splade_index` so callers can precompute SPLADE sparse vectors and skip the inline single-gene encode. Phase 2 adds an `--parallel` mode that chunks+tags files in an `mp.Pool` of workers and drains them into a main-process batched-SPLADE writer. Phase 3 adds a `--shard-workers N` mode that runs M independent shard-builds concurrently (each shard-build uses the Phase 2 writer logic internally without nested file pools).

**Tech Stack:** Python `multiprocessing`, existing `helix_context.backends.splade_backend.encode_batch`, existing `Genome`/`KnowledgeStore` ingest path, SQLite `busy_timeout` for cross-process writes.

**Scope of THIS PR:** All three phases. Independent commits per phase so we can revert any layer in isolation.

---

## File Inventory

**Create:**
- `helix_context/parallel.py` — auto-sizer helpers (`auto_workers`, `auto_shard_workers`)
- `tests/test_parallel_sizers.py` — unit tests for the sizers
- `tests/test_splade_precompute.py` — Phase 1 plumbing tests
- `tests/test_build_fixture_matrix_parallel.py` — Phase 2 + Phase 3 parity tests

**Modify:**
- `helix_context/storage/indexes.py` — `sync_splade_index` accepts optional precomputed sparse dict
- `helix_context/knowledge_store.py` — `upsert_doc` accepts and forwards `splade_sparse`
- `scripts/build_fixture_matrix.py` — add `--parallel`, `--workers`, `--shard-workers` CLI; new ingest paths

---

## Phase 1 — SPLADE precompute plumbing (prerequisite)

### Task 1: `sync_splade_index` accepts precomputed sparse dict

**Files:**
- Modify: `helix_context/storage/indexes.py:156-175`
- Test: `tests/test_splade_precompute.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_splade_precompute.py`:

```python
"""SPLADE precompute plumbing — issue #92, Phase 1.

Verifies that callers can pass a precomputed SPLADE sparse vector to
``sync_splade_index`` and ``upsert_doc`` instead of letting them call
``splade_backend.encode`` inline. Used by the parallel/shard-pool ingest
paths to batch SPLADE encoding outside the per-document upsert.
"""

from __future__ import annotations

import sqlite3

import pytest

from helix_context.backends import splade_backend
from helix_context.storage.indexes import sync_splade_index


def _fresh_splade_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    splade_backend.create_splade_table(conn)
    return conn


def test_sync_splade_index_uses_provided_sparse():
    """When splade_sparse is provided, no inline encode happens."""
    conn = _fresh_splade_db()
    provided = {"alpha": 1.5, "beta": 0.75}

    sync_splade_index(
        conn.cursor(),
        gene_id="g1",
        content="this content should be ignored",
        splade_enabled=True,
        splade_sparse=provided,
    )
    conn.commit()

    rows = conn.execute(
        "SELECT term, weight FROM splade_terms WHERE gene_id = ? ORDER BY term",
        ("g1",),
    ).fetchall()
    assert rows == [("alpha", 1.5), ("beta", 0.75)]


def test_sync_splade_index_disabled_is_noop_even_with_sparse():
    conn = _fresh_splade_db()
    sync_splade_index(
        conn.cursor(),
        gene_id="g1",
        content="x",
        splade_enabled=False,
        splade_sparse={"alpha": 1.0},
    )
    conn.commit()
    rows = conn.execute("SELECT COUNT(*) FROM splade_terms").fetchone()
    assert rows[0] == 0


def test_sync_splade_index_empty_sparse_dict_clears_existing_rows():
    """Pre-existing rows for gene_id get DELETE'd even when sparse is empty."""
    conn = _fresh_splade_db()
    conn.execute(
        "INSERT INTO splade_terms (gene_id, term, weight) VALUES (?, ?, ?)",
        ("g1", "stale", 1.0),
    )
    conn.commit()

    sync_splade_index(
        conn.cursor(),
        gene_id="g1",
        content="x",
        splade_enabled=True,
        splade_sparse={},
    )
    conn.commit()

    rows = conn.execute(
        "SELECT COUNT(*) FROM splade_terms WHERE gene_id = ?", ("g1",)
    ).fetchone()
    assert rows[0] == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_splade_precompute.py::test_sync_splade_index_uses_provided_sparse -v
```

Expected: FAIL — `sync_splade_index` does not accept `splade_sparse` yet.

- [ ] **Step 3: Add the kwarg**

In `helix_context/storage/indexes.py`, replace `sync_splade_index` (lines 156-175):

```python
def sync_splade_index(
    cur: sqlite3.Cursor,
    gene_id: str,
    content: str,
    splade_enabled: bool,
    splade_sparse: Dict[str, float] | None = None,
) -> None:
    """Populate the SPLADE sparse-term index.  No-op when disabled.

    When ``splade_sparse`` is None (default), encode ``content[:1000]``
    inline via :mod:`helix_context.backends.splade_backend`. When provided,
    use the supplied sparse dict as-is and skip the inline encode — used
    by the parallel ingest paths (issue #92) so SPLADE can be batched
    outside the per-document upsert.
    """
    if not splade_enabled:
        return
    try:
        if splade_sparse is None:
            from ..backends import splade_backend
            splade_sparse = splade_backend.encode(content[:1000])
        cur.execute("DELETE FROM splade_terms WHERE gene_id = ?", (gene_id,))
        if splade_sparse:
            cur.executemany(
                "INSERT INTO splade_terms (gene_id, term, weight) VALUES (?, ?, ?)",
                [(gene_id, term, weight) for term, weight in splade_sparse.items()],
            )
    except Exception:
        log.debug("SPLADE indexing failed for gene %s", gene_id, exc_info=True)
```

Also add to the top-of-file imports if not already present:

```python
from typing import Dict
```

- [ ] **Step 4: Run tests to verify pass**

```bash
python -m pytest tests/test_splade_precompute.py -v
```

Expected: all three Phase 1 tests pass.

- [ ] **Step 5: Commit**

```bash
git add helix_context/storage/indexes.py tests/test_splade_precompute.py
git commit -m "feat(ingest): sync_splade_index accepts precomputed sparse dict (#92)"
```

### Task 2: `upsert_doc` forwards `splade_sparse`

**Files:**
- Modify: `helix_context/knowledge_store.py:1002` (signature) and `:1126` (call site)
- Test: `tests/test_splade_precompute.py` (extend)

- [ ] **Step 1: Add the integration test**

Append to `tests/test_splade_precompute.py`:

```python
from helix_context.knowledge_store import KnowledgeStore
from helix_context.schemas import Gene


def _make_test_gene(content: str = "hello world parallel ingest") -> Gene:
    return Gene(
        content=content,
        content_type="text",
        source_id="test://splade-precompute",
        sequence_index=0,
    )


def test_upsert_doc_forwards_splade_sparse(tmp_path):
    """Pre-computed SPLADE sparse dict ends up in the splade_terms table."""
    db = tmp_path / "g.db"
    ks = KnowledgeStore(path=str(db), synonym_map={}, splade_enabled=True)
    gene = _make_test_gene()

    provided = {"semantic": 2.5, "expansion": 1.1}
    gene_id = ks.upsert_doc(gene, apply_gate=False, splade_sparse=provided)

    rows = ks.conn.execute(
        "SELECT term, weight FROM splade_terms WHERE gene_id = ? ORDER BY term",
        (gene_id,),
    ).fetchall()
    ks.close()

    assert sorted(rows) == [("expansion", 1.1), ("semantic", 2.5)]


def test_upsert_doc_inline_encode_when_sparse_not_provided(tmp_path, monkeypatch):
    """No splade_sparse → falls back to splade_backend.encode."""
    db = tmp_path / "g.db"
    ks = KnowledgeStore(path=str(db), synonym_map={}, splade_enabled=True)

    sentinel = {"sentinel": 9.99}
    calls: list[str] = []

    def fake_encode(text: str, top_k: int = 128, **kw):
        calls.append(text)
        return sentinel

    monkeypatch.setattr(splade_backend, "encode", fake_encode)
    gene_id = ks.upsert_doc(_make_test_gene(), apply_gate=False)

    rows = ks.conn.execute(
        "SELECT term, weight FROM splade_terms WHERE gene_id = ?", (gene_id,)
    ).fetchall()
    ks.close()

    assert calls, "splade_backend.encode should have been called once"
    assert rows == [("sentinel", 9.99)]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_splade_precompute.py::test_upsert_doc_forwards_splade_sparse -v
```

Expected: FAIL — `upsert_doc` does not accept `splade_sparse`.

- [ ] **Step 3: Add the kwarg to upsert_doc**

In `helix_context/knowledge_store.py`, change the signature at line 1002:

```python
    def upsert_doc(
        self,
        gene: Gene,
        apply_gate: bool = True,
        splade_sparse: Optional[Dict[str, float]] = None,
    ) -> str:
```

And update the `sync_splade_index` call at line 1126:

```python
        sync_splade_index(
            cur, gene_id, gene.content, self._splade_enabled,
            splade_sparse=splade_sparse,
        )
```

Confirm `Optional`, `Dict` are already imported at top of file (they are — used by `__init__` signature). If `Dict` import missing, add `from typing import Dict, Optional` (already imported per the existing `Optional[sqlite3.Connection]` usage).

- [ ] **Step 4: Run all Phase 1 tests**

```bash
python -m pytest tests/test_splade_precompute.py -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add helix_context/knowledge_store.py tests/test_splade_precompute.py
git commit -m "feat(ingest): upsert_doc forwards splade_sparse to indexer (#92)"
```

---

## Auto-sizer module

### Task 3: `helix_context/parallel.py` with `auto_workers`

**Files:**
- Create: `helix_context/parallel.py`
- Test: `tests/test_parallel_sizers.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_parallel_sizers.py`:

```python
"""Auto-sizer helpers for parallel ingest — issue #92."""

from __future__ import annotations

from unittest.mock import patch

from helix_context.parallel import auto_workers, auto_shard_workers


@patch("helix_context.parallel.os.cpu_count", return_value=8)
def test_auto_workers_8_core_default_buffer(_):
    """5800X-class box: 8 cores → 6 workers (12.5% headroom)."""
    assert auto_workers() == 6


@patch("helix_context.parallel.os.cpu_count", return_value=16)
def test_auto_workers_16_core(_):
    """16-core box: reserves max(2, ceil(16*0.125)+1) = 3 → 13 workers."""
    assert auto_workers() == 13


@patch("helix_context.parallel.os.cpu_count", return_value=4)
def test_auto_workers_4_core(_):
    """4-core box: reserves max(2, ceil(4*0.125)+1) = 2 → 2 workers."""
    assert auto_workers() == 2


@patch("helix_context.parallel.os.cpu_count", return_value=2)
def test_auto_workers_2_core_floor(_):
    """2-core box: reserves 2, returns at least 1."""
    assert auto_workers() == 1


@patch("helix_context.parallel.os.cpu_count", return_value=None)
def test_auto_workers_handles_unknown_cpu(_):
    """os.cpu_count() can return None — fall back to 4-core assumption."""
    assert auto_workers() >= 1
```

- [ ] **Step 2: Run test to verify it fails (module missing)**

```bash
python -m pytest tests/test_parallel_sizers.py::test_auto_workers_8_core_default_buffer -v
```

Expected: FAIL — `ModuleNotFoundError: helix_context.parallel`.

- [ ] **Step 3: Create the module with `auto_workers`**

Create `helix_context/parallel.py`:

```python
"""Auto-sizers for parallel ingest pools (issue #92).

Two helpers:

- :func:`auto_workers` picks a count for the file-level worker pool
  used by ``build_fixture_matrix.py --parallel``. CPU-bound; leaves
  ~12.5% headroom and reserves a core for the writer process.
- :func:`auto_shard_workers` picks a count for the shard-level worker
  pool used by ``build_fixture_matrix.py --mode sharded --shard-workers``.
  VRAM-bound because each shard-worker holds its own SPLADE model on
  the GPU (~4 GB per worker).

Both honour an explicit override at call time; these helpers are only
consulted when the user does not pass ``--workers`` / ``--shard-workers``.
"""

from __future__ import annotations

import math
import os


def auto_workers(buffer_pct: float = 0.125) -> int:
    """Worker count for the monolithic ``--parallel`` ingest pool.

    Leaves ``buffer_pct`` CPU headroom and reserves one extra core for
    the writer process. Always returns >= 1.

    On an 8-core 5800X (the reference dev box) the default returns 6.
    """
    physical = max(1, os.cpu_count() or 4)
    reserved = max(2, math.ceil(physical * buffer_pct) + 1)
    return max(1, physical - reserved)


def auto_shard_workers(buffer_pct: float = 0.125) -> int:
    """Shard-worker count for ``--mode sharded --shard-workers``.

    Each shard-worker holds an independent SPLADE model on the GPU
    (~4 GB). The cap is ``min(vram_gb // 4, auto_workers())`` so we never
    exceed CPU headroom or VRAM. Falls back to 1 when no GPU is reported.

    On a 3080 Ti (12 GB) + 5800X this returns 3.
    """
    try:
        from helix_context.hardware import get_hardware
        vram = get_hardware().vram_total_gb
    except Exception:
        vram = None

    vram_cap = max(1, int((vram or 4) // 4))  # 4 GB per SPLADE worker
    cpu_cap = max(1, auto_workers(buffer_pct))
    return max(1, min(vram_cap, cpu_cap))
```

- [ ] **Step 4: Run the auto_workers tests**

```bash
python -m pytest tests/test_parallel_sizers.py -v -k auto_workers
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add helix_context/parallel.py tests/test_parallel_sizers.py
git commit -m "feat(ingest): auto_workers helper for parallel ingest (#92)"
```

### Task 4: `auto_shard_workers`

**Files:**
- Already created in Task 3, only add tests.

- [ ] **Step 1: Add the shard-worker tests**

Append to `tests/test_parallel_sizers.py`:

```python
class _FakeHardware:
    def __init__(self, vram: float | None):
        self.vram_total_gb = vram


@patch("helix_context.parallel.os.cpu_count", return_value=8)
def test_auto_shard_workers_3080ti(_):
    """12 GB VRAM + 8-core CPU: min(12//4, auto_workers) = min(3, 6) = 3."""
    with patch("helix_context.hardware.get_hardware",
               return_value=_FakeHardware(vram=12.0)):
        assert auto_shard_workers() == 3


@patch("helix_context.parallel.os.cpu_count", return_value=16)
def test_auto_shard_workers_24gb(_):
    """24 GB + 16 core: VRAM allows 6, CPU allows 13 → min = 6."""
    with patch("helix_context.hardware.get_hardware",
               return_value=_FakeHardware(vram=24.0)):
        assert auto_shard_workers() == 6


@patch("helix_context.parallel.os.cpu_count", return_value=8)
def test_auto_shard_workers_8gb(_):
    """8 GB: VRAM caps to 2."""
    with patch("helix_context.hardware.get_hardware",
               return_value=_FakeHardware(vram=8.0)):
        assert auto_shard_workers() == 2


@patch("helix_context.parallel.os.cpu_count", return_value=8)
def test_auto_shard_workers_no_gpu(_):
    """No VRAM reported → fallback floor of 1."""
    with patch("helix_context.hardware.get_hardware",
               return_value=_FakeHardware(vram=None)):
        assert auto_shard_workers() == 1


@patch("helix_context.parallel.os.cpu_count", return_value=8)
def test_auto_shard_workers_hardware_import_error(_):
    """Hardware probing raises → still returns >= 1 (not a crash)."""
    with patch("helix_context.hardware.get_hardware",
               side_effect=RuntimeError("no torch")):
        assert auto_shard_workers() >= 1
```

- [ ] **Step 2: Run the full sizer suite**

```bash
python -m pytest tests/test_parallel_sizers.py -v
```

Expected: 10 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_parallel_sizers.py
git commit -m "test(ingest): auto_shard_workers VRAM-bound sizing (#92)"
```

---

## Phase 2 — `--parallel` mode for monolithic profiles

### Task 5: Extract pure chunk+tag function for worker reuse

The current `ingest_tree` in `scripts/build_fixture_matrix.py:202-276` opens each file, chunks it, tags each strand, and upserts inline. We need a pure helper that does the chunk+tag step only and returns plain dicts — usable both by the sequential path (unchanged behaviour) and by `mp.Pool` workers (which serialise return values across the process boundary).

**Files:**
- Modify: `scripts/build_fixture_matrix.py`

- [ ] **Step 1: Add the helper near the top of the script (after the SKIP/SIZE constants)**

Insert below the existing `MAX_FILE_SIZE` / `MIN_FILE_SIZE` constants in `scripts/build_fixture_matrix.py`:

```python
# ── File → gene-dict helper (shared by sequential + parallel paths) ──

_worker_chunker = None
_worker_tagger = None


def _init_worker():
    """Per-worker init for mp.Pool — loads tagger + chunker once."""
    global _worker_chunker, _worker_tagger
    from helix_context.codons import CodonChunker
    from helix_context.tagger import CpuTagger
    _worker_chunker = CodonChunker()
    _worker_tagger = CpuTagger()


def _chunk_and_tag_file(args: tuple[str, str]) -> list[dict]:
    """Read a single file, return list of Gene dicts.

    Runs in either the main process (sequential path) or in an
    ``mp.Pool`` worker (parallel path). Workers must have called
    :func:`_init_worker` first; the sequential path also needs to
    initialise the module-level chunker/tagger before calling this.

    Returns ``model_dump()`` dicts (not Gene instances) so the mp.Pool
    can hand results back to the parent process across its IPC boundary.
    """
    fpath, ext = args
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return []

    ct = "code" if ext in CODE_EXTS else "text"
    strands = _worker_chunker.chunk(content, content_type=ct)
    genes: list[dict] = []
    for i, strand in enumerate(strands):
        try:
            gene = _worker_tagger.pack(
                strand.content,
                content_type=ct,
                source_id=fpath,
                sequence_index=i,
            )
            gene.is_fragment = strand.is_fragment
            genes.append(gene.model_dump())
        except Exception:
            pass
    return genes
```

This step does not change behaviour — it adds dead code that subsequent tasks call.

- [ ] **Step 2: Run existing tests to confirm no regression**

```bash
python -m pytest tests/test_splade_precompute.py tests/test_parallel_sizers.py -v
```

Expected: previous-task tests still pass.

- [ ] **Step 3: Commit**

```bash
git add scripts/build_fixture_matrix.py
git commit -m "refactor(bench): extract _chunk_and_tag_file helper (#92)"
```

### Task 6: Batched-SPLADE writer + `_parallel_ingest_to_genome`

**Files:**
- Modify: `scripts/build_fixture_matrix.py`

- [ ] **Step 1: Add the iterator + parallel ingest function**

Insert below `_chunk_and_tag_file`:

```python
# ── File discovery iterator (drop-in for ingest_tree's walk) ────────


def _iter_ingestable_files(
    roots: list[str],
    skip_dirs: set[str],
    extra_filename_filters: list,
    stats: dict,
) -> list[tuple[str, str]]:
    """Walk ``roots`` and return [(fpath, ext)] passing all filters.

    Updates ``stats['missing_roots']`` and ``stats['skipped']`` in place.
    """
    files: list[tuple[str, str]] = []
    for root in roots:
        if not os.path.exists(root):
            log.warning("root %s does not exist, skipping", root)
            stats["missing_roots"].append(root)
            continue
        log.info("=== Discovering %s ===", root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in INGEST_EXTS:
                    stats["skipped"] += 1
                    continue
                fpath = os.path.join(dirpath, fname)
                if any(f(fpath) for f in extra_filename_filters):
                    stats["skipped"] += 1
                    continue
                try:
                    size = os.path.getsize(fpath)
                except OSError:
                    continue
                if size < MIN_FILE_SIZE or size > MAX_FILE_SIZE:
                    stats["skipped"] += 1
                    continue
                files.append((fpath, ext))
    return files


# ── Batched-SPLADE writer (drains gene dicts → genome) ──────────────


def _drain_with_batched_splade(
    gene_dict_iter,
    genome,
    stats: dict,
    batch_size: int = 64,
) -> None:
    """Drain ``gene_dict_iter`` (yielding lists of gene dicts per file)
    into ``genome``. SPLADE encoding is batched across ``batch_size`` genes
    instead of per-gene. Stats are updated in place.
    """
    from helix_context.backends import splade_backend
    from helix_context.schemas import Gene

    buf: list[Gene] = []

    def _flush(batch: list[Gene]) -> None:
        if not batch:
            return
        sparses = splade_backend.encode_batch(
            [g.content[:1000] for g in batch]
        )
        for g, sp in zip(batch, sparses):
            try:
                genome.upsert_doc(g, apply_gate=True, splade_sparse=sp)
                stats["genes"] += 1
            except Exception:
                stats["errors"] += 1
        if stats["genes"] % 500 < batch_size and stats["genes"] > 0:
            elapsed = time.perf_counter() - stats["t0"]
            log.info(
                "[%d files, %d genes] %.1f genes/s",
                stats["files"], stats["genes"],
                stats["genes"] / max(elapsed, 0.001),
            )

    for gene_dicts in gene_dict_iter:
        if not gene_dicts:
            stats["errors"] += 1
            continue
        for gd in gene_dicts:
            try:
                buf.append(Gene(**gd))
            except Exception:
                stats["errors"] += 1
        stats["files"] += 1
        while len(buf) >= batch_size:
            _flush(buf[:batch_size])
            del buf[:batch_size]

    if buf:
        _flush(buf)


# ── Parallel mode: file-level mp.Pool + main-process writer ─────────


def _parallel_ingest_to_genome(
    files: list[tuple[str, str]],
    genome,
    stats: dict,
    n_workers: int,
    batch_size: int = 64,
    chunksize: int = 4,
) -> None:
    """Chunk+tag files in parallel via ``mp.Pool``; drain into ``genome``
    via the batched-SPLADE writer in the main process.

    Caller is responsible for opening / closing ``genome``.
    """
    import multiprocessing as mp

    log.info(
        "parallel ingest: %d files, %d workers, batch_size=%d",
        len(files), n_workers, batch_size,
    )

    with mp.Pool(n_workers, initializer=_init_worker) as pool:
        gene_dict_iter = pool.imap_unordered(
            _chunk_and_tag_file, files, chunksize=chunksize,
        )
        _drain_with_batched_splade(
            gene_dict_iter, genome, stats, batch_size=batch_size,
        )
```

- [ ] **Step 2: Quick smoke test of the helpers**

Write a one-off check (no commit needed — just confirm imports resolve):

```bash
python -c "import sys, os; sys.path.insert(0, 'scripts'); import build_fixture_matrix as m; print(m._drain_with_batched_splade.__name__, m._parallel_ingest_to_genome.__name__, m._iter_ingestable_files.__name__)"
```

Expected: prints all three function names. (If an import or syntax error surfaces, fix it before continuing.)

- [ ] **Step 3: Commit**

```bash
git add scripts/build_fixture_matrix.py
git commit -m "feat(bench): batched-SPLADE writer + parallel ingest helpers (#92)"
```

### Task 7: Wire `--parallel` / `--workers` into `build_profile` + CLI

**Files:**
- Modify: `scripts/build_fixture_matrix.py` (`build_profile`, `main`)

- [ ] **Step 1: Update `build_profile` to support both paths**

Replace the body of `build_profile` (lines 282-370). The opening / setup is unchanged through `genome = Genome(...)`. Replace the body that walks roots and calls `ingest_tree(...)` for each root with:

```python
def build_profile(
    name: str,
    db_path: str,
    parallel: bool = False,
    n_workers: int = 0,
    batch_size: int = 64,
    chunksize: int = 4,
) -> dict:
    """Build the profile named ``name`` into a fresh ``.db`` at ``db_path``.

    When ``parallel=True`` use the worker-pool + batched-SPLADE path. When
    False (default), preserve the original sequential :func:`ingest_tree`
    behaviour byte-for-byte.
    """
    profile = PROFILES[name]

    out_dir = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(out_dir, exist_ok=True)

    if os.path.exists(db_path):
        log.info("removing existing %s", db_path)
        os.remove(db_path)
        for suffix in ("-wal", "-shm"):
            sidecar = db_path + suffix
            if os.path.exists(sidecar):
                os.remove(sidecar)

    log.info("opening fresh genome at %s", db_path)
    genome = Genome(
        path=db_path,
        synonym_map={},
        splade_enabled=True,
        entity_graph=True,
    )

    skip_dirs = SKIP_DIRS_COMMON | profile["extra_skip_dirs"]
    extra_filename_filters = profile["extra_filename_filters"]

    stats = {
        "profile": name,
        "label": profile["label"],
        "active_roots": profile["active_roots"],
        "roots": profile["roots"],
        "db_path": db_path,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "files": 0,
        "genes": 0,
        "skipped": 0,
        "errors": 0,
        "missing_roots": [],
        "t0": time.perf_counter(),
        "mode": "parallel" if parallel else "sequential",
    }

    if parallel:
        from helix_context.parallel import auto_workers
        if n_workers <= 0:
            n_workers = auto_workers()
        files = _iter_ingestable_files(
            profile["roots"], skip_dirs, extra_filename_filters, stats,
        )
        stats["discovered_files"] = len(files)
        _parallel_ingest_to_genome(
            files=files,
            genome=genome,
            stats=stats,
            n_workers=n_workers,
            batch_size=batch_size,
            chunksize=chunksize,
        )
        stats["workers"] = n_workers
    else:
        tagger = CpuTagger()
        chunker = CodonChunker()
        for root in profile["roots"]:
            ingest_tree(
                root=root,
                genome=genome,
                tagger=tagger,
                chunker=chunker,
                stats=stats,
                skip_dirs=skip_dirs,
                extra_filename_filters=extra_filename_filters,
            )

    elapsed = time.perf_counter() - stats["t0"]
    stats["elapsed_s"] = round(elapsed, 1)
    stats["finished_at"] = datetime.now(timezone.utc).isoformat()

    genome_stats = genome.stats()
    stats["total_genes"] = genome_stats.get("total_genes", 0)
    stats["compression_ratio"] = round(genome_stats.get("compression_ratio", 0.0), 4)

    try:
        hl_row = genome.conn.execute(
            "SELECT COUNT(*) AS n FROM harmonic_links"
        ).fetchone()
        stats["harmonic_links"] = int(hl_row["n"]) if hl_row else 0
    except Exception:
        stats["harmonic_links"] = 0

    try:
        stats["bytes"] = os.path.getsize(db_path)
    except OSError:
        stats["bytes"] = -1

    log.info("=" * 60)
    log.info("DONE %s (%s) in %.1fs", name, stats["mode"], elapsed)
    log.info("  files=%d genes=%d skipped=%d errors=%d",
             stats["files"], stats["genes"], stats["skipped"], stats["errors"])
    log.info("  total_genes=%d harmonic_links=%d bytes=%d",
             stats["total_genes"], stats["harmonic_links"], stats["bytes"])
    if stats["missing_roots"]:
        log.warning("  missing roots: %s", stats["missing_roots"])

    genome.close()
    stats.pop("t0", None)
    return stats
```

- [ ] **Step 2: Add CLI flags**

In the `main()` function (around line 622), add the new flags after `--shard-category`:

```python
    parser.add_argument(
        "--parallel", action="store_true",
        help="Use worker-pool + batched-SPLADE ingest (blob mode only). "
             "Default: sequential.",
    )
    parser.add_argument(
        "--workers", type=int, default=0,
        help="Worker count for --parallel (0 = auto via helix_context.parallel.auto_workers).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=64,
        help="SPLADE batch size in the writer (default: 64).",
    )
    parser.add_argument(
        "--chunksize", type=int, default=4,
        help="mp.Pool chunksize for --parallel (default: 4).",
    )
```

Then in the blob branch of `main()`, change the call:

```python
            stats = build_profile(
                name, db_path,
                parallel=args.parallel,
                n_workers=args.workers,
                batch_size=args.batch_size,
                chunksize=args.chunksize,
            )
```

Replace the existing trailing block at the end of the file:

```python
if __name__ == "__main__":
    import multiprocessing as mp
    mp.freeze_support()  # required on Windows for --parallel
    sys.exit(main())
```

- [ ] **Step 3: Smoke test the CLI**

```bash
python scripts/build_fixture_matrix.py --help | grep -E "(--parallel|--workers|--batch-size)"
```

Expected: the new flags appear in the help output.

- [ ] **Step 4: Commit**

```bash
git add scripts/build_fixture_matrix.py
git commit -m "feat(bench): --parallel / --workers flags for build_fixture_matrix (#92)"
```

### Task 8: Sequential-vs-parallel parity integration test

**Files:**
- Create: `tests/test_build_fixture_matrix_parallel.py`

- [ ] **Step 1: Write the parity test**

Create `tests/test_build_fixture_matrix_parallel.py`:

```python
"""Parity test for issue #92 parallel ingest.

Builds the same small synthetic corpus twice — once sequentially, once
with the new ``--parallel`` writer + ``mp.Pool`` workers — and asserts
the resulting gene_ids and content hashes are identical.
"""

from __future__ import annotations

import os
import sys
import sqlite3
from pathlib import Path

import pytest

# Make scripts/ importable.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "scripts")
)


def _populate_tree(root: Path, n_files: int = 6) -> None:
    """Write a tiny deterministic corpus."""
    root.mkdir(parents=True, exist_ok=True)
    bodies = [
        "def alpha():\n    return 'A' * 200\n" * 4,
        "# header\nbeta = 12345\n" * 8,
        "class Gamma:\n    def m(self):\n        pass\n" * 5,
        "// js\nconst delta = () => 7;\n" * 6,
        "{\"epsilon\": [1, 2, 3, 4, 5, 6, 7, 8]}\n" * 4,
        "phi: zeta\nrho: theta\n" * 10,
    ]
    suffixes = [".py", ".py", ".py", ".js", ".json", ".yaml"]
    for i in range(n_files):
        (root / f"f{i}{suffixes[i % len(suffixes)]}").write_text(
            bodies[i % len(bodies)], encoding="utf-8",
        )


def _collect_gene_summary(db_path: Path) -> set[tuple[str, str]]:
    """Return {(gene_id, content_hash)} for every gene in db."""
    import hashlib
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT gene_id, content FROM genes").fetchall()
    conn.close()
    return {
        (gid, hashlib.sha256(content.encode("utf-8")).hexdigest())
        for gid, content in rows
    }


@pytest.mark.slow
def test_parallel_matches_sequential(tmp_path, monkeypatch):
    """build_profile(parallel=False) and (parallel=True) should produce
    identical gene_ids + content hashes for the same corpus."""
    import build_fixture_matrix as bfm

    corpus = tmp_path / "corpus"
    _populate_tree(corpus, n_files=6)

    monkeypatch.setattr(bfm, "PROFILES", {
        "tiny92": {
            "label": "issue #92 parity test corpus",
            "active_roots": 1,
            "roots": [str(corpus)],
            "extra_skip_dirs": set(),
            "extra_filename_filters": [],
        }
    })

    seq_db = tmp_path / "seq.db"
    par_db = tmp_path / "par.db"

    bfm.build_profile("tiny92", str(seq_db), parallel=False)
    bfm.build_profile(
        "tiny92", str(par_db),
        parallel=True, n_workers=2, batch_size=8, chunksize=1,
    )

    seq = _collect_gene_summary(seq_db)
    par = _collect_gene_summary(par_db)

    assert seq == par, (
        f"gene-id/content mismatch\n"
        f"  only in seq: {sorted(seq - par)[:5]}...\n"
        f"  only in par: {sorted(par - seq)[:5]}..."
    )
```

- [ ] **Step 2: Run the parity test**

```bash
python -m pytest tests/test_build_fixture_matrix_parallel.py::test_parallel_matches_sequential -v -s
```

Expected: PASS. SPLADE will load on first encode (slow first run), then both builds complete and gene_ids match.

If the test reveals an actual mismatch (rather than infrastructure noise), investigate before continuing — the parity guarantee is the core acceptance criterion for the PR.

- [ ] **Step 3: Commit**

```bash
git add tests/test_build_fixture_matrix_parallel.py
git commit -m "test(bench): parity test for --parallel ingest (#92)"
```

---

## Phase 3 — `--shard-workers` mode for sharded profiles

### Task 9: Extract single-shard build into `_build_one_shard`

The current `build_profile_sharded` interleaves the shard loop with the main.db `register_shard` + `_copy_fingerprint_indexes` calls. We need to split it so the per-shard work (which can run in a subprocess) is separable from the main.db writes (which always happen in the parent).

**Files:**
- Modify: `scripts/build_fixture_matrix.py`

- [ ] **Step 1: Add `_build_one_shard` as a pure function**

Insert above `build_profile_sharded`:

```python
def _build_one_shard(
    label: str,
    root: str,
    shard_db_path: str,
    skip_dirs: set[str],
    extra_filename_filters: list,
    use_batched_splade: bool = True,
    batch_size: int = 64,
) -> dict:
    """Build a single shard ``.db`` for ``root``. Returns the shard's
    fingerprint payload + stats — caller is responsible for writing
    the rows into main.db.

    Runs end-to-end in one process: discover files, chunk+tag, batched
    SPLADE upsert. Used by both the serial sharded build (called from
    the parent process) and the parallel pool (called inside subprocesses
    via :func:`_shard_worker_entry`).
    """
    p = Path(shard_db_path)
    if p.exists():
        p.unlink()
        for s in (str(p) + "-wal", str(p) + "-shm"):
            if os.path.exists(s):
                os.remove(s)
    p.parent.mkdir(parents=True, exist_ok=True)

    shard = Genome(
        path=str(p), synonym_map={},
        splade_enabled=True, entity_graph=True,
    )
    s_stats = {
        "files": 0, "genes": 0, "skipped": 0, "errors": 0,
        "missing_roots": [],
        "t0": time.perf_counter(),
    }
    try:
        if use_batched_splade:
            files = _iter_ingestable_files(
                [root], skip_dirs, extra_filename_filters, s_stats,
            )
            _init_worker()  # fill module-level chunker/tagger
            gen = (_chunk_and_tag_file(f) for f in files)
            _drain_with_batched_splade(
                gen, shard, s_stats, batch_size=batch_size,
            )
        else:
            tagger = CpuTagger()
            chunker = CodonChunker()
            ingest_tree(
                root=root,
                genome=shard,
                tagger=tagger,
                chunker=chunker,
                stats=s_stats,
                skip_dirs=skip_dirs,
                extra_filename_filters=extra_filename_filters,
            )

        gene_count = shard.stats().get("total_genes", 0)
        try:
            byte_size = p.stat().st_size if p.is_file() else 0
        except OSError:
            byte_size = 0
        elapsed = round(time.perf_counter() - s_stats["t0"], 1)

        # Build fingerprint payload here (with the shard still open) so the
        # parent process can write to main.db without re-opening the shard.
        fp_rows = shard.conn.execute(
            "SELECT gene_id, source_id, promoter, key_values, is_fragment "
            "FROM genes"
        ).fetchall()
        now = time.time()
        fp_payload = []
        for r in fp_rows:
            promoter_blob = r["promoter"]
            domains_json = None
            entities_json = None
            if promoter_blob:
                try:
                    pm = json.loads(promoter_blob)
                    domains_json = json.dumps(pm.get("domains") or [])
                    entities_json = json.dumps(pm.get("entities") or [])
                except Exception:
                    pass
            fp_payload.append((
                r["gene_id"], label, r["source_id"],
                domains_json, entities_json, r["key_values"],
                0 if r["is_fragment"] else 1, None, now,
            ))

        return {
            "label": label,
            "root": root,
            "shard_db_path": str(p),
            "gene_count": gene_count,
            "byte_size": byte_size,
            "elapsed_s": elapsed,
            "files": s_stats["files"],
            "genes": s_stats["genes"],
            "skipped": s_stats["skipped"],
            "errors": s_stats["errors"],
            "missing_roots": s_stats["missing_roots"],
            "fingerprint_payload": fp_payload,
        }
    finally:
        shard.close()


def _shard_worker_entry(task: dict) -> dict:
    """``mp.Pool`` entry point — accepts a task dict, returns shard result."""
    return _build_one_shard(
        label=task["label"],
        root=task["root"],
        shard_db_path=task["shard_db_path"],
        skip_dirs=task["skip_dirs"],
        extra_filename_filters=task["extra_filename_filters"],
        use_batched_splade=True,
        batch_size=task.get("batch_size", 64),
    )
```

Note: `_build_one_shard` uses `Path` from `pathlib`. Add `from pathlib import Path` near the top of the file if not already imported (it is — used by `corpus_shard_db`).

- [ ] **Step 2: Confirm imports resolve**

```bash
python -c "import sys; sys.path.insert(0, 'scripts'); import build_fixture_matrix as m; print(m._build_one_shard.__name__, m._shard_worker_entry.__name__)"
```

Expected: prints both function names.

- [ ] **Step 3: Commit**

```bash
git add scripts/build_fixture_matrix.py
git commit -m "refactor(bench): extract _build_one_shard helper (#92)"
```

### Task 10: Multi-process shard pool + `build_profile_sharded` rewire

**Files:**
- Modify: `scripts/build_fixture_matrix.py` (`build_profile_sharded`)

- [ ] **Step 1: Rewire `build_profile_sharded` to dispatch via `_build_one_shard`**

Replace the body of `build_profile_sharded` with the function below. Also delete the now-unused `_copy_fingerprint_indexes` helper:

```python
def build_profile_sharded(
    name: str,
    profile_out_dir: str,
    shard_category: str = "reference",
    shard_workers: int = 1,
    batch_size: int = 64,
) -> dict:
    """Build the profile as a sharded layout under ``profile_out_dir``.

    When ``shard_workers > 1`` the per-shard builds run in an ``mp.Pool``;
    main.db writes happen in the parent process after each shard returns,
    serialized through SQLite's ``busy_timeout``. ``shard_workers == 1``
    (default) preserves the serial behaviour byte-for-byte.
    """
    import multiprocessing as mp

    profile = PROFILES[name]
    os.makedirs(profile_out_dir, exist_ok=True)

    main_path = main_db_path(profile_out_dir)
    if main_path.exists():
        log.info("removing existing %s", main_path)
        main_path.unlink()
        for sidecar in (str(main_path) + "-wal", str(main_path) + "-shm"):
            if os.path.exists(sidecar):
                os.remove(sidecar)
    main_conn = open_main_db(str(main_path))
    init_main_db(main_conn)
    try:
        main_conn.execute("PRAGMA busy_timeout = 30000")
    except Exception:
        log.debug("busy_timeout pragma failed", exc_info=True)
    log.info("sharded main.db at %s (shard_workers=%d)",
             main_path, shard_workers)

    skip_dirs = SKIP_DIRS_COMMON | profile["extra_skip_dirs"]
    extra_filename_filters = profile["extra_filename_filters"]

    totals = {
        "profile": name,
        "label": profile["label"],
        "active_roots": profile["active_roots"],
        "roots": profile["roots"],
        "out_dir": profile_out_dir,
        "main_db": str(main_path),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "files": 0,
        "genes": 0,
        "skipped": 0,
        "errors": 0,
        "missing_roots": [],
        "shards": [],
        "shard_workers": shard_workers,
        "t0": time.perf_counter(),
    }

    # Build the task list (filter out missing roots up front).
    tasks: list[dict] = []
    for root in profile["roots"]:
        if not os.path.exists(root):
            log.warning("root %s does not exist, skipping", root)
            totals["missing_roots"].append(root)
            continue
        label = _slug_for_root(root)
        shard_db = corpus_shard_db(root, label, profile_out_dir)
        tasks.append({
            "label": label,
            "root": root,
            "shard_db_path": str(shard_db),
            "skip_dirs": skip_dirs,
            "extra_filename_filters": extra_filename_filters,
            "batch_size": batch_size,
        })

    # Per-shard execution — serial or pool.
    def _commit_shard_result(res: dict) -> None:
        register_shard(
            main_conn,
            shard_name=res["label"],
            category=shard_category,
            path=res["shard_db_path"],
            gene_count=res["gene_count"],
            byte_size=res["byte_size"],
        )
        if res["fingerprint_payload"]:
            main_conn.executemany(
                "INSERT OR REPLACE INTO fingerprint_index "
                "(gene_id, shard_name, source_id, domains, entities, key_values, "
                "is_parent, sequence_idx, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                res["fingerprint_payload"],
            )
        main_conn.commit()
        log.info(
            "  %s: %d genes, %d fingerprint rows, %.1f MB (%.1fs)",
            res["label"], res["gene_count"], len(res["fingerprint_payload"]),
            res["byte_size"] / 1_048_576, res["elapsed_s"],
        )
        totals["shards"].append({
            "name": res["label"],
            "root": res["root"],
            "path": res["shard_db_path"],
            "genes": res["gene_count"],
            "fingerprint_rows": len(res["fingerprint_payload"]),
            "bytes": res["byte_size"],
            "elapsed_s": res["elapsed_s"],
        })
        for k in ("files", "genes", "skipped", "errors"):
            totals[k] += res[k]
        totals["missing_roots"].extend(res["missing_roots"])

    if shard_workers <= 1:
        for task in tasks:
            log.info("=== Shard %s @ %s -> %s ===",
                     task["label"], task["root"], task["shard_db_path"])
            _commit_shard_result(_shard_worker_entry(task))
    else:
        log.info("dispatching %d shards across %d workers",
                 len(tasks), shard_workers)
        with mp.Pool(shard_workers) as pool:
            for res in pool.imap_unordered(_shard_worker_entry, tasks):
                _commit_shard_result(res)

    elapsed = time.perf_counter() - totals["t0"]
    totals["elapsed_s"] = round(elapsed, 1)
    totals["finished_at"] = datetime.now(timezone.utc).isoformat()

    try:
        main_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        log.debug("wal_checkpoint on main.db failed", exc_info=True)
    main_conn.close()

    try:
        totals["main_db_bytes"] = os.path.getsize(main_path)
    except OSError:
        totals["main_db_bytes"] = -1

    total_shard_bytes = sum(s["bytes"] for s in totals["shards"])
    totals["total_bytes"] = total_shard_bytes + max(totals["main_db_bytes"], 0)
    totals["total_genes"] = sum(s["genes"] for s in totals["shards"])
    totals["shard_count"] = len(totals["shards"])

    log.info("=" * 60)
    log.info("DONE %s-sharded in %.1fs (shard_workers=%d)",
             name, elapsed, shard_workers)
    log.info(
        "  shards=%d genes=%d bytes=%d (main_db=%d)",
        totals["shard_count"], totals["total_genes"],
        totals["total_bytes"], totals["main_db_bytes"],
    )
    if totals["missing_roots"]:
        log.warning("  missing roots: %s", totals["missing_roots"])

    totals.pop("t0", None)
    return totals
```

Remove the existing `_copy_fingerprint_indexes` function (lines 386-428 in the original file) — no longer referenced.

- [ ] **Step 2: Confirm imports + signatures**

```bash
python -c "import sys; sys.path.insert(0, 'scripts'); import build_fixture_matrix as m; import inspect; print(inspect.signature(m.build_profile_sharded))"
```

Expected: signature includes `shard_workers` and `batch_size`.

- [ ] **Step 3: Commit**

```bash
git add scripts/build_fixture_matrix.py
git commit -m "feat(bench): multi-process shard pool for sharded builds (#92)"
```

### Task 11: Wire `--shard-workers` CLI flag

**Files:**
- Modify: `scripts/build_fixture_matrix.py` (`main`)

- [ ] **Step 1: Add the flag**

In the `main()` argparse block, add after `--shard-category`:

```python
    parser.add_argument(
        "--shard-workers", type=int, default=0,
        help="Number of parallel shard-builders (sharded mode only). "
             "0 = auto via helix_context.parallel.auto_shard_workers; 1 = serial.",
    )
```

In the sharded branch of `main()`, before the loop:

```python
    if args.shard_workers <= 0:
        from helix_context.parallel import auto_shard_workers
        shard_workers = auto_shard_workers()
    else:
        shard_workers = args.shard_workers
```

And change the call inside the loop:

```python
        stats = build_profile_sharded(
            name=name,
            profile_out_dir=profile_dir,
            shard_category=args.shard_category,
            shard_workers=shard_workers,
            batch_size=args.batch_size,
        )
```

Update the `log.info` line in the loop to mention worker count:

```python
        log.info("### Profile: %s (sharded, %d workers) ###", name, shard_workers)
```

- [ ] **Step 2: CLI smoke test**

```bash
python scripts/build_fixture_matrix.py --help | grep -E "(--shard-workers|--parallel|--workers)"
```

Expected: all four flags appear.

- [ ] **Step 3: Commit**

```bash
git add scripts/build_fixture_matrix.py
git commit -m "feat(bench): --shard-workers CLI flag (#92)"
```

### Task 12: Sharded parity test

**Files:**
- Modify: `tests/test_build_fixture_matrix_parallel.py`

- [ ] **Step 1: Append the sharded parity test**

```python
def _collect_main_db_summary(main_db_path: Path) -> dict[str, set[tuple]]:
    """Return {shards, fingerprint_rows} for parity checks on a main.db."""
    conn = sqlite3.connect(str(main_db_path))
    shards = {
        (row[0], row[1])
        for row in conn.execute("SELECT shard_name, category FROM shards")
    }
    fps = {
        (row[0], row[1], row[2])
        for row in conn.execute(
            "SELECT gene_id, shard_name, source_id FROM fingerprint_index"
        )
    }
    conn.close()
    return {"shards": shards, "fingerprint_rows": fps}


@pytest.mark.slow
def test_sharded_pool_matches_serial(tmp_path, monkeypatch):
    """build_profile_sharded(shard_workers=1) and (shard_workers=2) should
    produce identical main.db fingerprint_index + per-shard gene_ids."""
    import build_fixture_matrix as bfm

    a = tmp_path / "rootA"
    b = tmp_path / "rootB"
    _populate_tree(a, n_files=4)
    _populate_tree(b, n_files=4)

    monkeypatch.setattr(bfm, "PROFILES", {
        "shardtest92": {
            "label": "issue #92 sharded parity",
            "active_roots": 2,
            "roots": [str(a), str(b)],
            "extra_skip_dirs": set(),
            "extra_filename_filters": [],
        }
    })

    out_serial = tmp_path / "ser"
    out_pool = tmp_path / "pool"

    bfm.build_profile_sharded(
        "shardtest92", str(out_serial),
        shard_workers=1, batch_size=8,
    )
    bfm.build_profile_sharded(
        "shardtest92", str(out_pool),
        shard_workers=2, batch_size=8,
    )

    ser_summary = _collect_main_db_summary(out_serial / "main.genome.db")
    pool_summary = _collect_main_db_summary(out_pool / "main.genome.db")

    assert ser_summary["shards"] == pool_summary["shards"]
    assert ser_summary["fingerprint_rows"] == pool_summary["fingerprint_rows"]
```

- [ ] **Step 2: Run sharded parity test**

```bash
python -m pytest tests/test_build_fixture_matrix_parallel.py::test_sharded_pool_matches_serial -v -s
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_build_fixture_matrix_parallel.py
git commit -m "test(bench): sharded-pool parity test (#92)"
```

---

## Finalize

### Task 13: Update docstring

**Files:**
- Modify: `scripts/build_fixture_matrix.py` (module docstring)

- [ ] **Step 1: Refresh the top-of-file docstring**

Update the existing usage block in `scripts/build_fixture_matrix.py` to mention the new flags. The exact lines to replace are in the module docstring under the `Usage` heading. Add a new section after the existing `Usage` section:

```
Parallel modes (issue #92)
--------------------------
    --parallel              File-level mp.Pool + batched-SPLADE writer
                            (monolithic blob mode only).
    --workers N             Override worker count for --parallel (0 = auto).
    --shard-workers N       Run sharded builds with N concurrent shard
                            processes. 0 = auto from VRAM + CPU.
    --batch-size N          SPLADE batch size in the writer (default 64).

Examples:
    python scripts/build_fixture_matrix.py --profile medium --parallel
    python scripts/build_fixture_matrix.py --profile xl --parallel --workers 6
    python scripts/build_fixture_matrix.py --profile xl --mode sharded --shard-workers 3
```

- [ ] **Step 2: Commit**

```bash
git add scripts/build_fixture_matrix.py
git commit -m "docs(bench): refresh build_fixture_matrix usage block (#92)"
```

### Task 14: Run the full test baseline

- [ ] **Step 1: Run the non-live, non-slow test suite**

```bash
python -m pytest tests/ -m "not live and not slow" -q
```

Expected: same pass/fail set as master (no regressions). The new slow parity tests are excluded from this run.

- [ ] **Step 2: Run the new tests explicitly**

```bash
python -m pytest tests/test_splade_precompute.py tests/test_parallel_sizers.py tests/test_build_fixture_matrix_parallel.py -v
```

Expected: all new tests pass.

If any pre-existing test on master fails on this branch (regression), stop and diagnose before opening the PR.

### Task 15: Push branch, open PR

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/92-parallel-ingest
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "feat(bench): parallel + sharded-pool ingest for build_fixture_matrix (#92)" --body "$(cat <<'EOF'
Closes #92.

## Summary
- Phase 1: optional ``splade_sparse`` kwarg on ``sync_splade_index`` and ``upsert_doc`` so callers can precompute SPLADE outside the per-document upsert.
- Phase 2: ``--parallel`` ingest mode for monolithic profiles — ``mp.Pool`` chunks+tags in parallel; main-process writer batches SPLADE in groups of 64 and commits.
- Phase 3: ``--shard-workers N`` mode for sharded profiles — M concurrent shard-builds, main.db writes serialised through SQLite ``busy_timeout``.
- Auto-sizers (``helix_context.parallel.auto_workers`` / ``auto_shard_workers``) so the CLI picks a sane default without manual tuning.

## Test plan
- [ ] ``pytest tests/test_splade_precompute.py``
- [ ] ``pytest tests/test_parallel_sizers.py``
- [ ] ``pytest tests/test_build_fixture_matrix_parallel.py`` (slow; both parity tests)
- [ ] ``pytest tests/ -m "not live and not slow"`` — no regressions vs master
- [ ] Manual: ``python scripts/build_fixture_matrix.py --profile small --parallel`` end-to-end
- [ ] Manual: ``python scripts/build_fixture_matrix.py --profile small --mode sharded --shard-workers 2`` end-to-end

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Verify the PR exists**

```bash
gh pr view --json url,state,number
```

Expected: state OPEN, URL returned, number printed.

---

## Out of Scope (deferred follow-ups)

- Refactoring `scripts/ingest_parallel.py` to use the new precompute path (separate cleanup, possibly its own issue)
- Internal-to-`sync_splade_index` batching (the function still receives one gene at a time; batching happens above)
- Multi-GPU sharding
- Nested parallelism within a shard-worker (no file-pool inside each shard)
- New SPLADE backfill script (not needed if batched encoding works inline)

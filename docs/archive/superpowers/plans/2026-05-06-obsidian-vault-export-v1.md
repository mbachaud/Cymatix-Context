# Obsidian Vault Export v1 (Read-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a read-only markdown export of the helix genome to a configurable vault directory, with TTL-pruned per-`/context` trace exports and a `_stale/` operator view, browsable in Obsidian.

**Architecture:** New in-process `helix_context/vault/` package wired into the FastAPI lifespan. Snapshot exporter renders genes to markdown with computed-field frontmatter (authored fields are cosmetic placeholders forward-compat with v1.1). Sibling `vault.db` SQLite tracks gene_id→path + content-hash sentinel, isolated from `genome.db` for clean lifecycle separation. Pruner thread handles trace TTL via filename-encoded `_exp<unix>` suffixes (no frontmatter parse for unexpired files), produces hour-sharded rollups before deletion, and refreshes the `_stale/` view per cycle. CLI talks to the running server via HTTP — no shared SQLite handles, no concurrent-writer races.

**Tech Stack:** Python 3.11+, FastAPI (existing), SQLite + WAL (existing), `filelock` (NEW dep), `watchdog` deferred to v1.1, OTel histograms (existing pattern), pytest.

**Spec:** `docs/superpowers/specs/2026-05-06-obsidian-vault-export-design.md`
**Future-design (deferred):** `docs/superpowers/specs/2026-05-06-obsidian-vault-export-full-design-v1.1plus.md`

---

## Task ordering and dependencies

```
Phase 1 (foundation, no helix-context behavior change):
  Task 1: pyproject + filelock dep + helix.toml stub + config.VaultConfig
  Task 2: helix_context/vault/state.py — vault.db DDL + CRUD
  Task 3: helix_context/vault/locking.py — vault-root filelock
  Task 4: helix_context/genome.py — idx_genes_last_seen

Phase 2 (rendering primitives, no I/O):
  Task 5: helix_context/vault/schema.py — frontmatter, filename, path safety
  Task 6: helix_context/vault/writer.py — write_atomic + render_gene_markdown
  Task 7: helix_context/vault/writer.py — render_trace_markdown

Phase 3 (export entry points):
  Task 8: writer.full_export()
  Task 9: writer.incremental_export()
  Task 10: writer.trace_export()

Phase 4 (pruner):
  Task 11: helix_context/vault/pruner.py — TTL prune via filename
  Task 12: pruner — rollup append (hour-sharded)
  Task 13: pruner — _stale/ refresh + eager fan-out migration

Phase 5 (lifecycle + server + telemetry + CLI):
  Task 14: helix_context/vault/__init__.py — VaultManager
  Task 15: helix_context/server.py — lifespan hook + HTTP endpoints
  Task 16: telemetry — OTel histograms + counters + gauges
  Task 17: helix_context/vault/cli.py — helix-vault command

Phase 6 (polish):
  Task 18: README generation + end-to-end integration tests
```

Tasks are independent at the file level; each makes one focused commit.

---

## Task 1: Config + dependency

**Files:**
- Modify: `pyproject.toml` (add `filelock` dep, `helix-vault` script entry)
- Modify: `helix.toml` (add `[vault]` + `[vault.traces]` example block, commented out by default behavior)
- Modify: `helix_context/config.py` (add `VaultConfig`, `VaultTracesConfig` classes; load from TOML)
- Test: `tests/test_vault_config.py` (NEW)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_config.py
"""Tests for VaultConfig + VaultTracesConfig parsing."""
from __future__ import annotations

import textwrap
from pathlib import Path

from helix_context.config import HelixConfig, load_config


def test_vault_defaults_when_section_absent(tmp_path: Path):
    """If helix.toml has no [vault] section, defaults apply and vault is disabled."""
    cfg_path = tmp_path / "helix.toml"
    cfg_path.write_text("")
    cfg = load_config(cfg_path)
    assert cfg.vault.enabled is False
    assert cfg.vault.path == "~/.helix/vault"
    assert cfg.vault.traces.retention_hours == 48
    assert cfg.vault.traces.enabled is True


def test_vault_section_overrides_defaults(tmp_path: Path):
    cfg_path = tmp_path / "helix.toml"
    cfg_path.write_text(textwrap.dedent("""
        [vault]
        enabled = true
        path = "/tmp/myvault"
        party_id = "party_a"
        fan_out_threshold = 1000
        redact_body = true
        stale_threshold = 0.3

        [vault.traces]
        enabled = true
        retention_hours = 12
        max_retention_hours_hard = 168
        max_count = 500
        rollup_enabled = true
        rollup_shard = "hour"
        prune_interval_minutes = 30
        trigger_only = true
    """))
    cfg = load_config(cfg_path)
    assert cfg.vault.enabled is True
    assert cfg.vault.path == "/tmp/myvault"
    assert cfg.vault.party_id == "party_a"
    assert cfg.vault.fan_out_threshold == 1000
    assert cfg.vault.redact_body is True
    assert cfg.vault.stale_threshold == 0.3
    assert cfg.vault.traces.enabled is True
    assert cfg.vault.traces.retention_hours == 12
    assert cfg.vault.traces.max_retention_hours_hard == 168
    assert cfg.vault.traces.max_count == 500
    assert cfg.vault.traces.rollup_enabled is True
    assert cfg.vault.traces.rollup_shard == "hour"
    assert cfg.vault.traces.prune_interval_minutes == 30
    assert cfg.vault.traces.trigger_only is True


def test_vault_traces_max_retention_hours_hard_can_be_null(tmp_path: Path):
    cfg_path = tmp_path / "helix.toml"
    cfg_path.write_text(textwrap.dedent("""
        [vault.traces]
        max_retention_hours_hard = 0
    """))
    cfg = load_config(cfg_path)
    # 0 disables the hard cap (treated as null/None)
    assert cfg.vault.traces.max_retention_hours_hard == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_config.py -v`
Expected: FAIL with `AttributeError: 'HelixConfig' object has no attribute 'vault'`.

- [ ] **Step 3: Add VaultConfig classes to `helix_context/config.py`**

Find the existing `HelixConfig` dataclass. Add two new dataclasses ABOVE it and a `vault` field on `HelixConfig`. Match the existing dataclass style (likely `@dataclass(frozen=False)` based on other config sections; use whatever the file uses).

```python
@dataclass
class VaultTracesConfig:
    enabled: bool = True
    retention_hours: int = 48
    max_retention_hours_hard: int = 720  # 30 days; 0 disables
    max_count: int = 10_000
    rollup_enabled: bool = True
    rollup_shard: str = "hour"  # "hour" | "daily"
    prune_interval_minutes: int = 60
    trigger_only: bool = False


@dataclass
class VaultConfig:
    enabled: bool = False
    path: str = "~/.helix/vault"
    party_id: str = ""  # empty = use server's primary party
    fan_out_threshold: int = 5000
    redact_body: bool = False
    stale_threshold: float = 0.5
    traces: VaultTracesConfig = field(default_factory=VaultTracesConfig)


# Then on HelixConfig:
@dataclass
class HelixConfig:
    # ... existing fields ...
    vault: VaultConfig = field(default_factory=VaultConfig)
```

In the TOML loader function, add a section after the existing sections:

```python
v_section = data.get("vault", {})
v_traces_section = v_section.get("traces", {})
cfg.vault = VaultConfig(
    enabled=v_section.get("enabled", cfg.vault.enabled),
    path=v_section.get("path", cfg.vault.path),
    party_id=v_section.get("party_id", cfg.vault.party_id),
    fan_out_threshold=v_section.get("fan_out_threshold", cfg.vault.fan_out_threshold),
    redact_body=v_section.get("redact_body", cfg.vault.redact_body),
    stale_threshold=v_section.get("stale_threshold", cfg.vault.stale_threshold),
    traces=VaultTracesConfig(
        enabled=v_traces_section.get("enabled", cfg.vault.traces.enabled),
        retention_hours=v_traces_section.get("retention_hours", cfg.vault.traces.retention_hours),
        max_retention_hours_hard=v_traces_section.get("max_retention_hours_hard", cfg.vault.traces.max_retention_hours_hard),
        max_count=v_traces_section.get("max_count", cfg.vault.traces.max_count),
        rollup_enabled=v_traces_section.get("rollup_enabled", cfg.vault.traces.rollup_enabled),
        rollup_shard=v_traces_section.get("rollup_shard", cfg.vault.traces.rollup_shard),
        prune_interval_minutes=v_traces_section.get("prune_interval_minutes", cfg.vault.traces.prune_interval_minutes),
        trigger_only=v_traces_section.get("trigger_only", cfg.vault.traces.trigger_only),
    ),
)
```

(If `helix_context/config.py` uses a different style for sections, follow that pattern instead — read the existing TOML loader before editing.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_config.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Add `filelock` to `pyproject.toml`**

In `[project] dependencies` add `"filelock>=3.12"`. Add the `helix-vault` script entry under `[project.scripts]`:

```toml
[project.scripts]
helix = "helix_context.server:main"
helix-launcher = "helix_context.launcher.app:main"
helix-status = "helix_status:main"
helix-vault = "helix_context.vault.cli:main"   # NEW
```

- [ ] **Step 6: Add commented `[vault]` block to `helix.toml`**

Append to the end of `helix.toml`:

```toml
# ── Vault export (Obsidian) — opt-in, off by default ────────────────────
# Renders the genome as a browsable markdown vault for operators.
# v1: read-only export + diagnostic /context traces. Curation/inbox in v1.1.
# [vault]
# enabled = false
# path = "~/.helix/vault"
# party_id = ""                     # empty = server's primary party
# fan_out_threshold = 5000          # split domain folders above this count
# redact_body = false               # true → replace body with sha+excerpt
#                                   # recommended for cloud-synced setups
# stale_threshold = 0.5             # _stale/ population threshold
#
# [vault.traces]
# enabled = true                    # auto-export every /context call
# retention_hours = 48              # default; ≥720 for 30-day audit
# max_retention_hours_hard = 720    # force-deletes pinned past this; 0 disables
# max_count = 10000                 # safety cap on burst floods
# rollup_enabled = true
# rollup_shard = "hour"             # daily | hour
# prune_interval_minutes = 60
# trigger_only = false              # if true, only on threshold (latency/sparse)
```

- [ ] **Step 7: Install the new dep**

Run: `pip install -e ".[dev]" --quiet && python -c "import filelock; print(filelock.__version__)"`
Expected: a version string ≥ 3.12.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml helix.toml helix_context/config.py tests/test_vault_config.py
git commit -m "feat(vault): add VaultConfig + filelock dep + helix.toml stub"
```

---

## Task 2: `vault/state.py` — vault.db schema + CRUD

**Files:**
- Create: `helix_context/vault/__init__.py` (empty for now; just makes it a package)
- Create: `helix_context/vault/state.py`
- Test: `tests/test_vault_state.py` (NEW)

- [ ] **Step 1: Make the package importable**

Create `helix_context/vault/__init__.py` with a single line for now:

```python
"""Helix vault — operator-facing markdown export of the genome."""
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_vault_state.py
"""Tests for vault.db state tracking."""
from __future__ import annotations

from pathlib import Path

import pytest

from helix_context.vault.state import VaultState


@pytest.fixture
def state(tmp_path: Path) -> VaultState:
    return VaultState(vault_root=tmp_path)


class TestSchemaCreation:
    def test_vault_db_created_on_init(self, tmp_path: Path):
        VaultState(vault_root=tmp_path)
        assert (tmp_path / "vault.db").exists()

    def test_top_level_state_initialized(self, tmp_path: Path):
        s = VaultState(vault_root=tmp_path)
        top = s.read_top_level_state()
        assert top["schema_version"] == 1
        assert top["last_full_export_ts"] == 0.0
        assert top["last_incremental_export_ts"] == 0.0
        assert top["exported_gene_count"] == 0


class TestVaultStateRecord:
    def test_set_and_get_path(self, state):
        state.upsert_record(gene_id="abc123", path="genes/auth/middleware-7f3a1c.md", ts=100.0, disk_hash="aaa")
        rec = state.get_record("abc123")
        assert rec is not None
        assert rec.vault_path == "genes/auth/middleware-7f3a1c.md"
        assert rec.last_exported_ts == 100.0
        assert rec.last_exported_disk_hash == "aaa"

    def test_get_missing_returns_none(self, state):
        assert state.get_record("nope") is None

    def test_upsert_replaces(self, state):
        state.upsert_record(gene_id="abc", path="genes/x.md", ts=1.0, disk_hash="h1")
        state.upsert_record(gene_id="abc", path="genes/y.md", ts=2.0, disk_hash="h2")
        rec = state.get_record("abc")
        assert rec.vault_path == "genes/y.md"
        assert rec.last_exported_ts == 2.0
        assert rec.last_exported_disk_hash == "h2"

    def test_delete_record(self, state):
        state.upsert_record(gene_id="abc", path="genes/x.md", ts=1.0, disk_hash="h")
        state.delete_record("abc")
        assert state.get_record("abc") is None

    def test_iter_all_records(self, state):
        state.upsert_record(gene_id="a", path="genes/a.md", ts=1.0, disk_hash="ha")
        state.upsert_record(gene_id="b", path="genes/b.md", ts=2.0, disk_hash="hb")
        records = list(state.iter_records())
        assert len(records) == 2
        ids = {r.gene_id for r in records}
        assert ids == {"a", "b"}


class TestTopLevelStatePersistence:
    def test_update_persists_across_reload(self, tmp_path: Path):
        s1 = VaultState(vault_root=tmp_path)
        s1.update_top_level_state(last_full_export_ts=999.0, exported_gene_count=42)
        s1.close()

        s2 = VaultState(vault_root=tmp_path)
        top = s2.read_top_level_state()
        assert top["last_full_export_ts"] == 999.0
        assert top["exported_gene_count"] == 42
        assert top["schema_version"] == 1


class TestSchemaVersion:
    def test_version_mismatch_raises(self, tmp_path: Path):
        # Initialize at v1, then write a v999 marker, then reopen
        s = VaultState(vault_root=tmp_path)
        s.close()
        import json
        state_file = tmp_path / ".helix-state.json"
        data = json.loads(state_file.read_text())
        data["schema_version"] = 999
        state_file.write_text(json.dumps(data))
        with pytest.raises(VaultState.SchemaVersionMismatch):
            VaultState(vault_root=tmp_path)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helix_context.vault.state'`.

- [ ] **Step 4: Implement `helix_context/vault/state.py`**

```python
"""vault.db sibling state — gene_id → path + content-hash sentinel.

Lives in <vault_root>/vault.db, NOT in genome.db. Lifecycle is the vault's,
not the genome's. See spec section "State tracking — sibling vault.db".
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
TOP_LEVEL_FILENAME = ".helix-state.json"


@dataclass(frozen=True)
class VaultStateRecord:
    gene_id: str
    vault_path: str
    last_exported_ts: float
    last_exported_disk_hash: Optional[str]


class VaultState:
    """SQLite + JSON state for the vault.

    vault.db holds per-gene rows (vault_state table).
    .helix-state.json holds top-level state (schema_version, export timestamps).
    """

    class SchemaVersionMismatch(Exception):
        pass

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = Path(vault_root)
        self.vault_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db_path = self.vault_root / "vault.db"
        self._json_path = self.vault_root / TOP_LEVEL_FILENAME

        # Open SQLite connection (autocommit reader pattern from PR #32)
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_size_limit=67108864")  # 64 MB

        self._ensure_schema()
        self._ensure_top_level_state()

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vault_state (
                gene_id                  TEXT PRIMARY KEY,
                vault_path               TEXT NOT NULL,
                last_exported_ts         REAL NOT NULL,
                last_exported_disk_hash  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_vault_state_path
                ON vault_state(vault_path);
            """
        )
        self._conn.commit()

    def _ensure_top_level_state(self) -> None:
        if not self._json_path.exists():
            self._write_top_level_state(
                {
                    "schema_version": SCHEMA_VERSION,
                    "last_full_export_ts": 0.0,
                    "last_incremental_export_ts": 0.0,
                    "exported_gene_count": 0,
                    "fan_out_engaged_domains": [],
                }
            )
            return
        existing = self.read_top_level_state()
        if existing.get("schema_version") != SCHEMA_VERSION:
            raise VaultState.SchemaVersionMismatch(
                f"vault state schema_version={existing.get('schema_version')} "
                f"but code expects {SCHEMA_VERSION}; run helix-vault migrate"
            )

    def read_top_level_state(self) -> dict:
        with self._json_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def update_top_level_state(self, **fields) -> None:
        current = self.read_top_level_state()
        current.update(fields)
        self._write_top_level_state(current)

    def _write_top_level_state(self, data: dict) -> None:
        tmp = self._json_path.with_suffix(self._json_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self._json_path)

    def upsert_record(
        self, *, gene_id: str, path: str, ts: float, disk_hash: Optional[str]
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO vault_state (gene_id, vault_path, last_exported_ts, last_exported_disk_hash)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(gene_id) DO UPDATE SET
                vault_path = excluded.vault_path,
                last_exported_ts = excluded.last_exported_ts,
                last_exported_disk_hash = excluded.last_exported_disk_hash
            """,
            (gene_id, path, ts, disk_hash),
        )
        self._conn.commit()

    def get_record(self, gene_id: str) -> Optional[VaultStateRecord]:
        row = self._conn.execute(
            "SELECT gene_id, vault_path, last_exported_ts, last_exported_disk_hash "
            "FROM vault_state WHERE gene_id = ?",
            (gene_id,),
        ).fetchone()
        if row is None:
            return None
        return VaultStateRecord(
            gene_id=row["gene_id"],
            vault_path=row["vault_path"],
            last_exported_ts=row["last_exported_ts"],
            last_exported_disk_hash=row["last_exported_disk_hash"],
        )

    def delete_record(self, gene_id: str) -> None:
        self._conn.execute("DELETE FROM vault_state WHERE gene_id = ?", (gene_id,))
        self._conn.commit()

    def iter_records(self) -> Iterator[VaultStateRecord]:
        for row in self._conn.execute(
            "SELECT gene_id, vault_path, last_exported_ts, last_exported_disk_hash "
            "FROM vault_state"
        ):
            yield VaultStateRecord(
                gene_id=row["gene_id"],
                vault_path=row["vault_path"],
                last_exported_ts=row["last_exported_ts"],
                last_exported_disk_hash=row["last_exported_disk_hash"],
            )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            log.warning("vault.db close failed", exc_info=True)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_state.py -v`
Expected: 9 PASSED.

- [ ] **Step 6: Commit**

```bash
git add helix_context/vault/__init__.py helix_context/vault/state.py tests/test_vault_state.py
git commit -m "feat(vault): add state.py — vault.db sibling DDL + CRUD"
```

---

## Task 3: `vault/locking.py` — vault-root filelock

**Files:**
- Create: `helix_context/vault/locking.py`
- Test: `tests/test_vault_locking.py` (NEW)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_locking.py
"""Tests for vault-root filelock context manager."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from helix_context.vault.locking import VaultLock


def test_lock_acquires_and_releases(tmp_path: Path):
    lock = VaultLock(vault_root=tmp_path)
    with lock:
        assert (tmp_path / "vault.lock").exists()


def test_lock_blocks_concurrent_acquirer(tmp_path: Path):
    lock1 = VaultLock(vault_root=tmp_path, timeout=0.5)
    lock2 = VaultLock(vault_root=tmp_path, timeout=0.5)
    with lock1:
        with pytest.raises(TimeoutError):
            with lock2:
                pass


def test_lock_releases_on_exception(tmp_path: Path):
    lock1 = VaultLock(vault_root=tmp_path, timeout=0.5)
    lock2 = VaultLock(vault_root=tmp_path, timeout=0.5)
    try:
        with lock1:
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # lock2 should now acquire fine
    with lock2:
        pass


def test_concurrent_threads_serialize(tmp_path: Path):
    """Two threads grabbing the lock should serialize, not interleave."""
    order = []
    barrier = threading.Barrier(2)

    def worker(name: str):
        barrier.wait()
        lock = VaultLock(vault_root=tmp_path, timeout=5.0)
        with lock:
            order.append(f"{name}-enter")
            time.sleep(0.05)
            order.append(f"{name}-exit")

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # No interleaving: enter must be followed by exit before next enter
    assert order[0].endswith("-enter")
    assert order[1] == order[0].replace("-enter", "-exit")
    assert order[2].endswith("-enter")
    assert order[3] == order[2].replace("-enter", "-exit")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_locking.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helix_context.vault.locking'`.

- [ ] **Step 3: Implement `helix_context/vault/locking.py`**

```python
"""Vault-root filelock — coordinates writers across the in-process VaultManager
and any external `helix-vault` CLI invocations.

Backed by `filelock` (portable, file-based advisory locks). The lockfile lives
at <vault_root>/vault.lock and is created on first acquire.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from filelock import FileLock, Timeout

log = logging.getLogger(__name__)


class VaultLock:
    """Context manager wrapping a vault-root filelock.

    Usage:
        lock = VaultLock(vault_root, timeout=10.0)
        with lock:
            ...

    Raises:
        TimeoutError: if the lock can't be acquired within `timeout` seconds.
    """

    def __init__(self, vault_root: Path, timeout: float = 30.0) -> None:
        self.vault_root = Path(vault_root)
        self.vault_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lockpath = self.vault_root / "vault.lock"
        self._timeout = timeout
        self._lock: Optional[FileLock] = None

    def __enter__(self) -> "VaultLock":
        self._lock = FileLock(str(self._lockpath))
        try:
            self._lock.acquire(timeout=self._timeout)
        except Timeout as exc:
            raise TimeoutError(
                f"Could not acquire {self._lockpath} within {self._timeout}s"
            ) from exc
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._lock is not None:
            try:
                self._lock.release()
            except Exception:
                log.warning("filelock release failed", exc_info=True)
            self._lock = None
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_locking.py -v`
Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add helix_context/vault/locking.py tests/test_vault_locking.py
git commit -m "feat(vault): add locking.py — vault-root filelock context manager"
```

---

## Task 4: `idx_genes_last_seen` index in genome.py

**Files:**
- Modify: `helix_context/genome.py` (add idempotent index creation in `_ensure_schema` or equivalent)
- Test: `tests/test_genome_indexes.py` (NEW)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_genome_indexes.py
"""Test that genome bootstrap creates idx_genes_last_seen for incremental export."""
from __future__ import annotations

from pathlib import Path

from helix_context.genome import Genome


def test_idx_genes_last_seen_present(tmp_path: Path):
    g = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
    try:
        rows = g.conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_genes_last_seen'"
        ).fetchall()
        assert len(rows) == 1, "idx_genes_last_seen index missing"
    finally:
        g.close()


def test_idx_genes_last_seen_idempotent(tmp_path: Path):
    """Re-opening the genome must not error if index already exists."""
    path = str(tmp_path / "genome.db")
    g1 = Genome(path=path, synonym_map={})
    g1.close()
    g2 = Genome(path=path, synonym_map={})
    try:
        rows = g2.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_genes_last_seen'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        g2.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_genome_indexes.py -v`
Expected: FAIL with `assert 0 == 1` on first test.

- [ ] **Step 3: Add the index to genome.py**

Find the function in `helix_context/genome.py` that creates the schema (likely called `_ensure_schema`, `_init_schema`, or similar — grep for `CREATE TABLE genes` or `CREATE INDEX`). Add this near the other `CREATE INDEX IF NOT EXISTS` statements:

```python
self.conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_genes_last_seen ON genes(last_seen)"
)
```

If the genome's schema is created via an `executescript` block of multiple DDL statements, append the line into that block. Match the surrounding style.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_genome_indexes.py -v`
Expected: 2 PASSED.

- [ ] **Step 5: Confirm full genome test suite still passes**

Run: `python -m pytest tests/test_genome.py tests/test_genome_wal.py -q`
Expected: 41 PASSED (38 existing + 3 WAL).

- [ ] **Step 6: Commit**

```bash
git add helix_context/genome.py tests/test_genome_indexes.py
git commit -m "feat(genome): add idx_genes_last_seen for vault incremental export"
```

---

## Task 5: `vault/schema.py` — frontmatter shape, filename, path safety

**Files:**
- Create: `helix_context/vault/schema.py`
- Test: `tests/test_vault_schema.py` (NEW)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_schema.py
"""Tests for frontmatter rendering, filename derivation, and path safety."""
from __future__ import annotations

from pathlib import Path

import pytest

from helix_context.vault.schema import (
    AUTHORED_FIELDS,
    COMPUTED_FIELDS,
    authored_placeholders,
    derive_gene_filename,
    derive_gene_relpath,
    safe_resolve_under,
)


class TestFieldClassification:
    def test_computed_fields_disjoint_from_authored(self):
        assert COMPUTED_FIELDS.isdisjoint(AUTHORED_FIELDS)

    def test_computed_fields_present(self):
        for k in [
            "gene_id", "chromatin", "domains", "content_type",
            "source_id", "source_lines", "content_sha256",
            "last_seen", "last_seen_ts", "live_truth_score",
            "co_activation_partners", "party_id", "participant_handle",
        ]:
            assert k in COMPUTED_FIELDS

    def test_authored_fields_present(self):
        for k in [
            "operator_notes", "operator_tags", "pinned", "quarantine_reason",
            "supersedes", "contradicts", "implements", "documented_by", "tests",
        ]:
            assert k in AUTHORED_FIELDS


class TestAuthoredPlaceholders:
    def test_placeholder_values(self):
        p = authored_placeholders()
        assert p["operator_notes"] == ""
        assert p["operator_tags"] == []
        assert p["pinned"] is False
        assert p["quarantine_reason"] is None
        for k in ("supersedes", "contradicts", "implements", "documented_by", "tests"):
            assert p[k] == []


class TestDeriveFilename:
    def test_simple_python_path(self):
        # gene_id "abc123def456" → short_id "abc123"
        assert derive_gene_filename("helix_context/auth/middleware.py", "abc123def456") \
            == "middleware-abc123.md"

    def test_strips_extension(self):
        assert derive_gene_filename("foo/bar.md", "1234567890ab") == "bar-123456.md"

    def test_no_extension(self):
        assert derive_gene_filename("foo/Makefile", "ab12cd34ef56") == "Makefile-ab12cd.md"

    def test_short_id_exactly_six_chars(self):
        result = derive_gene_filename("x.py", "0123456789ab")
        assert result == "x-012345.md"


class TestDeriveRelpath:
    def test_with_domain(self):
        assert derive_gene_relpath(
            domain="auth",
            source_id="helix_context/auth/middleware.py",
            gene_id="abc123def456",
        ) == "genes/auth/middleware-abc123.md"

    def test_no_domain_goes_to_orphan(self):
        assert derive_gene_relpath(
            domain=None,
            source_id="x/y.py",
            gene_id="abc123def456",
        ) == "genes/_orphan/y-abc123.md"

    def test_empty_domain_goes_to_orphan(self):
        assert derive_gene_relpath(
            domain="",
            source_id="x/y.py",
            gene_id="abc123def456",
        ) == "genes/_orphan/y-abc123.md"


class TestSafeResolveUnder:
    def test_normal_path_resolves(self, tmp_path: Path):
        target = safe_resolve_under(tmp_path, tmp_path / "genes" / "x.md")
        assert target == (tmp_path / "genes" / "x.md").resolve()

    def test_path_outside_root_raises(self, tmp_path: Path):
        outside = tmp_path / ".." / "etc" / "passwd"
        with pytest.raises(ValueError, match="outside vault root"):
            safe_resolve_under(tmp_path, outside)

    def test_traversal_via_symlink_raises(self, tmp_path: Path):
        # Construct a path that resolves outside via "..", regardless of FS
        candidate = tmp_path / "a" / ".." / ".." / "outside"
        with pytest.raises(ValueError, match="outside vault root"):
            safe_resolve_under(tmp_path, candidate)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helix_context.vault.schema'`.

- [ ] **Step 3: Implement `helix_context/vault/schema.py`**

```python
"""Frontmatter shape, filename derivation, and path safety helpers.

The frontmatter is the load-bearing surface for vault interop.
Computed fields are helix-authoritative (read-only in the vault).
Authored fields are operator-editable starting in v1.1; in v1 they are
rendered as cosmetic placeholders for forward-compat.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# ── Field classification ────────────────────────────────────────────────

COMPUTED_FIELDS = frozenset({
    "gene_id",
    "chromatin",
    "domains",
    "content_type",
    "source_id",
    "source_lines",
    "content_sha256",
    "last_seen",
    "last_seen_ts",
    "live_truth_score",
    "co_activation_partners",
    "party_id",
    "participant_handle",
})

AUTHORED_FIELDS = frozenset({
    "operator_notes",
    "operator_tags",
    "pinned",
    "quarantine_reason",
    "supersedes",
    "contradicts",
    "implements",
    "documented_by",
    "tests",
})

assert COMPUTED_FIELDS.isdisjoint(AUTHORED_FIELDS), \
    "field classification overlap — must be disjoint"


def authored_placeholders() -> dict:
    """Default values for cosmetic authored fields in v1.

    These render every export. v1.1 will populate them from
    gene_attribution.notes via the validator; in v1 they're forward-compat.
    """
    return {
        "operator_notes": "",
        "operator_tags": [],
        "pinned": False,
        "quarantine_reason": None,
        "supersedes": [],
        "contradicts": [],
        "implements": [],
        "documented_by": [],
        "tests": [],
    }


# ── Filename derivation ─────────────────────────────────────────────────

_SHORT_ID_LEN = 6


def derive_gene_filename(source_id: str, gene_id: str) -> str:
    """Derive a vault-side filename from source path + gene_id.

    Pattern: <source_stem>-<short_id>.md
    """
    stem = Path(source_id).stem if Path(source_id).suffix else Path(source_id).name
    short = gene_id[:_SHORT_ID_LEN]
    return f"{stem}-{short}.md"


def derive_gene_relpath(*, domain: Optional[str], source_id: str, gene_id: str) -> str:
    """Vault-relative path for a gene: genes/<domain>/<filename>.

    If domain is None or empty, falls back to genes/_orphan/.
    """
    sub = domain if domain else "_orphan"
    return f"genes/{sub}/{derive_gene_filename(source_id, gene_id)}"


# ── Path safety ──────────────────────────────────────────────────────────

def safe_resolve_under(vault_root: Path, candidate: Path) -> Path:
    """Resolve `candidate` and assert it lives under vault_root.

    Raises ValueError if the candidate would escape the vault root.
    """
    root = Path(vault_root).resolve()
    target = Path(candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"{candidate} resolves outside vault root {root}")
    return target
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_schema.py -v`
Expected: 12 PASSED.

- [ ] **Step 5: Commit**

```bash
git add helix_context/vault/schema.py tests/test_vault_schema.py
git commit -m "feat(vault): add schema.py — frontmatter shape, filename, path safety"
```

---

## Task 6: `vault/writer.py` — `write_atomic` + `render_gene_markdown`

**Files:**
- Create: `helix_context/vault/writer.py`
- Test: `tests/test_vault_writer.py` (NEW)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_writer.py
"""Tests for vault writer — atomic writes + gene rendering."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from helix_context.vault.writer import (
    compute_disk_hash,
    render_gene_markdown,
    write_atomic,
)


class TestWriteAtomic:
    def test_writes_file(self, tmp_path: Path):
        target = tmp_path / "out.md"
        write_atomic(vault_root=tmp_path, target=target, content="hello")
        assert target.read_text() == "hello"

    def test_no_tmp_left_behind(self, tmp_path: Path):
        target = tmp_path / "out.md"
        write_atomic(vault_root=tmp_path, target=target, content="hello")
        assert not target.with_suffix(".md.tmp").exists()

    def test_no_sentinel_left_behind(self, tmp_path: Path):
        target = tmp_path / "out.md"
        write_atomic(vault_root=tmp_path, target=target, content="hello")
        assert not (tmp_path / ".helix-syncing").exists()

    def test_creates_parent_dirs(self, tmp_path: Path):
        target = tmp_path / "genes" / "auth" / "x.md"
        write_atomic(vault_root=tmp_path, target=target, content="hi")
        assert target.exists()


class TestComputeDiskHash:
    def test_returns_sha256_hex(self, tmp_path: Path):
        f = tmp_path / "x.md"
        f.write_text("hello")
        h = compute_disk_hash(f)
        assert h == hashlib.sha256(b"hello").hexdigest()

    def test_handles_unicode(self, tmp_path: Path):
        f = tmp_path / "x.md"
        f.write_text("hellö 世界", encoding="utf-8")
        h = compute_disk_hash(f)
        assert h == hashlib.sha256("hellö 世界".encode("utf-8")).hexdigest()


class TestRenderGeneMarkdown:
    @pytest.fixture
    def gene(self):
        # Mock a gene with the fields render_gene_markdown reads.
        # The actual Gene class lives in helix_context/schemas.py — adapt
        # field names if the production class differs.
        return SimpleNamespace(
            gene_id="abc123def456",
            content="def hello():\n    return 'world'\n",
            content_type="code",
            source_id="helix_context/auth/middleware.py",
            source_lines="42-89",
            domains=["auth", "jwt"],
            chromatin="euchromatin",
            content_sha256="7f3a1c000000000000000000000000000000000000000000000000000000000",
            last_seen="2026-05-06T20:45:00Z",
            last_seen_ts=1736198700.0,
            live_truth_score=0.92,
            co_activation_partners=7,
            party_id="swift_wing21",
            participant_handle="laude",
        )

    def test_produces_yaml_frontmatter(self, gene):
        md = render_gene_markdown(gene, redact_body=False)
        assert md.startswith("---\n")
        # Find end of frontmatter
        rest = md[len("---\n"):]
        end = rest.index("---\n")
        fm_text = rest[:end]
        fm = yaml.safe_load(fm_text)
        assert fm["gene_id"] == "abc123def456"
        assert fm["chromatin"] == "euchromatin"
        assert fm["domains"] == ["auth", "jwt"]
        assert fm["live_truth_score"] == 0.92

    def test_includes_authored_placeholders(self, gene):
        md = render_gene_markdown(gene, redact_body=False)
        rest = md[len("---\n"):]
        end = rest.index("---\n")
        fm = yaml.safe_load(rest[:end])
        assert fm["operator_notes"] == ""
        assert fm["operator_tags"] == []
        assert fm["pinned"] is False
        assert fm["supersedes"] == []

    def test_body_includes_content(self, gene):
        md = render_gene_markdown(gene, redact_body=False)
        assert "def hello():" in md
        assert "return 'world'" in md

    def test_body_includes_typed_edges_section(self, gene):
        md = render_gene_markdown(gene, redact_body=False)
        assert "## Typed edges" in md
        # v1: empty — placeholder text noting v1.1 will activate
        assert "v1.1" in md or "(none yet" in md

    def test_redact_body_replaces_with_summary(self, gene):
        md = render_gene_markdown(gene, redact_body=True)
        assert "def hello():" not in md
        # Should contain the SHA256 of the body for traceability
        body_sha = "[redacted body]"
        assert body_sha in md or "redacted" in md.lower()

    def test_empty_domains_does_not_crash(self, gene):
        gene.domains = []
        md = render_gene_markdown(gene, redact_body=False)
        rest = md[len("---\n"):]
        end = rest.index("---\n")
        fm = yaml.safe_load(rest[:end])
        assert fm["domains"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_writer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helix_context.vault.writer'`.

- [ ] **Step 3: Implement `helix_context/vault/writer.py`**

```python
"""Vault writer — atomic file writes + gene markdown rendering.

Atomic writes use a tmp+rename pattern with a vault-root sentinel so that any
external file watcher (in v1.1, our own watcher) can suppress events for
helix-side writes.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from helix_context.vault.schema import authored_placeholders

log = logging.getLogger(__name__)

SENTINEL_FILENAME = ".helix-syncing"
_BODY_REDACT_EXCERPT_LEN = 80


# ── Atomic write primitive ──────────────────────────────────────────────

def write_atomic(*, vault_root: Path, target: Path, content: str) -> None:
    """Write `content` to `target` atomically.

    1. Write to target.tmp
    2. Touch sentinel
    3. os.replace(tmp, target)
    4. Remove sentinel

    Caller is responsible for holding the vault-root lock.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_suffix(target.suffix + ".tmp")
    sentinel = Path(vault_root) / SENTINEL_FILENAME

    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    sentinel.touch(exist_ok=True)
    try:
        os.replace(tmp, target)
    finally:
        try:
            sentinel.unlink()
        except FileNotFoundError:
            pass


def compute_disk_hash(path: Path) -> str:
    """SHA-256 of full file content. Used as the v1.1 self-event sentinel."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Gene rendering ──────────────────────────────────────────────────────

def _build_frontmatter(gene: Any) -> dict:
    """Build the frontmatter dict from a Gene row.

    Mirrors COMPUTED_FIELDS + authored_placeholders().
    """
    fm: dict = {}
    # Computed
    fm["gene_id"] = gene.gene_id
    fm["chromatin"] = getattr(gene, "chromatin", "euchromatin")
    fm["domains"] = list(getattr(gene, "domains", []) or [])
    fm["content_type"] = getattr(gene, "content_type", "code")
    fm["source_id"] = getattr(gene, "source_id", "")
    fm["source_lines"] = getattr(gene, "source_lines", "")
    fm["content_sha256"] = getattr(gene, "content_sha256", "")
    fm["last_seen"] = getattr(gene, "last_seen", "")
    fm["last_seen_ts"] = float(getattr(gene, "last_seen_ts", 0.0) or 0.0)
    fm["live_truth_score"] = float(getattr(gene, "live_truth_score", 0.0) or 0.0)
    fm["co_activation_partners"] = int(getattr(gene, "co_activation_partners", 0) or 0)
    fm["party_id"] = getattr(gene, "party_id", "")
    fm["participant_handle"] = getattr(gene, "participant_handle", "")
    # Authored placeholders (v1: cosmetic; v1.1: populated from gene_attribution.notes)
    fm.update(authored_placeholders())
    return fm


def _build_body(gene: Any, *, redact_body: bool) -> str:
    """Build the markdown body — content + typed-edges + last-retrieval sections."""
    title = f"# {gene.source_id}"
    if getattr(gene, "source_lines", ""):
        title += f":{gene.source_lines}"

    if redact_body:
        body_sha = getattr(gene, "content_sha256", "")[:16]
        excerpt = (gene.content or "").strip().split("\n", 1)[0][:_BODY_REDACT_EXCERPT_LEN]
        body_section = (
            f"```\n[redacted body — sha256={body_sha}, "
            f"first-line excerpt: {excerpt!r}]\n```"
        )
    else:
        lang = "python" if (gene.content_type == "code" and gene.source_id.endswith(".py")) else ""
        body_section = f"```{lang}\n{gene.content or ''}\n```"

    typed_edges = (
        "## Typed edges\n\n"
        "*(none yet — v1 ships read-only; v1.1 enables operator-authored "
        "supersedes / contradicts / implements / documented_by / tests)*"
    )

    backlinks = "## Backlinks\n\n*(populated by Obsidian)*"

    return "\n\n".join([title, body_section, typed_edges, backlinks])


def render_gene_markdown(gene: Any, *, redact_body: bool) -> str:
    """Render a Gene to a complete markdown document (frontmatter + body)."""
    fm = _build_frontmatter(gene)
    fm_yaml = yaml.safe_dump(fm, sort_keys=True, allow_unicode=True)
    body = _build_body(gene, redact_body=redact_body)
    return f"---\n{fm_yaml}---\n\n{body}\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_writer.py -v`
Expected: 12 PASSED.

- [ ] **Step 5: Commit**

```bash
git add helix_context/vault/writer.py tests/test_vault_writer.py
git commit -m "feat(vault): add writer.py — atomic writes + render_gene_markdown"
```

---

## Task 7: `render_trace_markdown` — diagnostic trace export

**Files:**
- Modify: `helix_context/vault/writer.py` (add `render_trace_markdown`)
- Modify: `tests/test_vault_writer.py` (add tests)

- [ ] **Step 1: Add the failing test**

Append to `tests/test_vault_writer.py`:

```python
class TestRenderTraceMarkdown:
    def test_includes_request_id_and_timing(self):
        from helix_context.vault.writer import render_trace_markdown

        md = render_trace_markdown(
            request_id="abc12345",
            created_at="2026-05-06T22:14:06Z",
            expires_at="2026-05-08T22:14:06Z",
            pinned=False,
            trigger_reason="latency_outlier",
            total_latency_ms=18432,
            health_status="sparse",
            stage_timing_ms={
                "extract": 12, "express": 45, "rerank": 12_400,
                "splice": 5_800, "assemble": 175,
            },
            fingerprint_route="(no fingerprint payload)",
            foveated_ranks="(none)",
            final_genes=[("middleware-7f3a1c", 1, 0.92)],
        )
        assert "abc12345" in md
        assert "18432" in md
        assert "rerank" in md
        assert "12_400" in md or "12400" in md
        assert "[[middleware-7f3a1c]]" in md
        assert md.startswith("---\n")

    def test_frontmatter_contains_expires_at(self):
        from helix_context.vault.writer import render_trace_markdown

        md = render_trace_markdown(
            request_id="x", created_at="t1", expires_at="t2",
            pinned=False, trigger_reason="auto",
            total_latency_ms=0, health_status="aligned",
            stage_timing_ms={}, fingerprint_route="", foveated_ranks="",
            final_genes=[],
        )
        rest = md[len("---\n"):]
        end = rest.index("---\n")
        fm = yaml.safe_load(rest[:end])
        assert fm["request_id"] == "x"
        assert fm["expires_at"] == "t2"
        assert fm["pinned"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_writer.py::TestRenderTraceMarkdown -v`
Expected: FAIL with `ImportError: cannot import name 'render_trace_markdown'`.

- [ ] **Step 3: Add `render_trace_markdown` to `helix_context/vault/writer.py`**

Append to `writer.py`:

```python
def render_trace_markdown(
    *,
    request_id: str,
    created_at: str,
    expires_at: str,
    pinned: bool,
    trigger_reason: str,
    total_latency_ms: int,
    health_status: str,
    stage_timing_ms: dict,
    fingerprint_route: str,
    foveated_ranks: str,
    final_genes: list,  # list of (filename_stem, rank, score)
) -> str:
    """Render a /context call trace to markdown.

    The trace export feeds Goal 2 (diagnostic console). Filename includes
    the expires_at unix epoch so the pruner can filter expired traces by
    name without parsing frontmatter.
    """
    fm = {
        "request_id": request_id,
        "created_at": created_at,
        "expires_at": expires_at,
        "pinned": pinned,
        "trigger_reason": trigger_reason,
        "total_latency_ms": total_latency_ms,
        "health_status": health_status,
    }
    fm_yaml = yaml.safe_dump(fm, sort_keys=True, allow_unicode=True)

    title = f"# Trace: {request_id}"

    stage_rows = "\n".join(f"| {s} | {ms} |" for s, ms in stage_timing_ms.items())
    stage_section = (
        "## Per-stage timing\n\n"
        "| stage | ms |\n|---|---|\n" + (stage_rows if stage_rows else "*(no per-stage data)*")
    )

    fp_section = "## Fingerprint route\n\n" + (fingerprint_route or "*(none)*")
    fov_section = "## Foveated rank assignments\n\n" + (foveated_ranks or "*(none)*")

    if final_genes:
        gene_lines = "\n".join(
            f"- [[{stem}]] (rank {rank}, score {score:.2f})"
            for (stem, rank, score) in final_genes
        )
    else:
        gene_lines = "*(no genes returned)*"
    final_section = "## Final budget genes\n\n" + gene_lines

    body = "\n\n".join([title, stage_section, fp_section, fov_section, final_section])
    return f"---\n{fm_yaml}---\n\n{body}\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_writer.py -v`
Expected: 14 PASSED.

- [ ] **Step 5: Commit**

```bash
git add helix_context/vault/writer.py tests/test_vault_writer.py
git commit -m "feat(vault): add render_trace_markdown for diagnostic trace export"
```

---

## Task 8: `writer.full_export()`

**Files:**
- Modify: `helix_context/vault/writer.py` (add `full_export`)
- Modify: `tests/test_vault_writer.py` (add tests)

- [ ] **Step 1: Add the failing test**

Append to `tests/test_vault_writer.py`:

```python
import time
from helix_context.genome import Genome
from helix_context.vault.state import VaultState
from helix_context.vault.locking import VaultLock


def _make_test_gene(content: str, source_id: str, domains=None):
    """Helper to make a Gene-shaped object for genome.upsert_gene."""
    # Adapt to whatever the existing genome.upsert_gene signature is.
    # If there's a make_gene() helper in tests/conftest.py, prefer that.
    from tests.conftest import make_gene
    return make_gene(content, domains=domains or [], source_id=source_id)


class TestFullExport:
    def test_exports_all_genes(self, tmp_path: Path):
        from helix_context.vault.writer import full_export

        genome = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
        try:
            genome.upsert_gene(_make_test_gene("def auth(): pass", "auth/m.py", ["auth"]))
            genome.upsert_gene(_make_test_gene("def core(): pass", "core/c.py", ["core"]))
            vault_root = tmp_path / "vault"
            state = VaultState(vault_root=vault_root)
            lock = VaultLock(vault_root=vault_root)
            try:
                stats = full_export(
                    genome=genome, state=state, lock=lock,
                    vault_root=vault_root, party_id="",
                    redact_body=False, fan_out_threshold=5000,
                )
            finally:
                state.close()

            assert stats["genes_exported"] == 2
            assert (vault_root / "genes" / "auth").is_dir()
            assert (vault_root / "genes" / "core").is_dir()
            files = list((vault_root / "genes").rglob("*.md"))
            assert len(files) == 2
        finally:
            genome.close()

    def test_export_filters_by_party(self, tmp_path: Path):
        from helix_context.vault.writer import full_export

        genome = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
        try:
            g1 = _make_test_gene("party_a content", "a.py", ["x"])
            g1.party_id = "party_a"
            g2 = _make_test_gene("party_b content", "b.py", ["x"])
            g2.party_id = "party_b"
            genome.upsert_gene(g1)
            genome.upsert_gene(g2)

            vault_root = tmp_path / "vault"
            state = VaultState(vault_root=vault_root)
            lock = VaultLock(vault_root=vault_root)
            try:
                stats = full_export(
                    genome=genome, state=state, lock=lock,
                    vault_root=vault_root, party_id="party_a",
                    redact_body=False, fan_out_threshold=5000,
                )
            finally:
                state.close()

            assert stats["genes_exported"] == 1
            files = list((vault_root / "genes").rglob("*.md"))
            assert len(files) == 1
            assert "party_a content" in files[0].read_text()
        finally:
            genome.close()

    def test_state_records_each_gene(self, tmp_path: Path):
        from helix_context.vault.writer import full_export

        genome = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
        try:
            g = _make_test_gene("hello", "x.py", ["test"])
            gid = genome.upsert_gene(g)

            vault_root = tmp_path / "vault"
            state = VaultState(vault_root=vault_root)
            lock = VaultLock(vault_root=vault_root)
            try:
                full_export(
                    genome=genome, state=state, lock=lock,
                    vault_root=vault_root, party_id="",
                    redact_body=False, fan_out_threshold=5000,
                )
                rec = state.get_record(gid)
                assert rec is not None
                assert rec.vault_path.startswith("genes/")
                assert rec.last_exported_disk_hash is not None
            finally:
                state.close()
        finally:
            genome.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_writer.py::TestFullExport -v`
Expected: FAIL with `ImportError: cannot import name 'full_export'`.

- [ ] **Step 3: Implement `full_export` in `helix_context/vault/writer.py`**

```python
import time as _time
from typing import Optional

from helix_context.vault.locking import VaultLock
from helix_context.vault.schema import derive_gene_relpath, safe_resolve_under
from helix_context.vault.state import VaultState

# Add to the imports at the top of writer.py


def full_export(
    *,
    genome,            # helix_context.genome.Genome
    state: VaultState,
    lock: VaultLock,
    vault_root: Path,
    party_id: str,     # empty string = no party filter
    redact_body: bool,
    fan_out_threshold: int,
    batch_size: int = 500,
) -> dict:
    """Snapshot every gene matching the party filter to the vault.

    Returns stats dict: {genes_exported, elapsed_seconds, errors}.
    Holds the vault-root lock for the entire export.
    """
    t0 = _time.monotonic()
    exported = 0
    errors = 0

    with lock:
        # Pagination cursor — iterate all genes; query_genes might be heavy,
        # use a direct table scan via genome.read_conn for simplicity.
        sql = (
            "SELECT g.gene_id, g.content, g.content_type, g.source_id, "
            "g.source_lines, g.domains, g.chromatin, g.content_sha256, "
            "g.last_seen, g.last_seen_ts, g.live_truth_score, "
            "g.co_activation_partners, "
            "ga.party_id, ga.participant_handle "
            "FROM genes g LEFT JOIN gene_attribution ga ON g.gene_id = ga.gene_id"
        )
        if party_id:
            sql += " WHERE ga.party_id = ?"
            params = (party_id,)
        else:
            params = ()

        cur = genome.read_conn.execute(sql, params)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                try:
                    gene = _row_to_gene(row)
                    relpath = derive_gene_relpath(
                        domain=(gene.domains[0] if gene.domains else None),
                        source_id=gene.source_id,
                        gene_id=gene.gene_id,
                    )
                    target = vault_root / relpath
                    safe_resolve_under(vault_root, target)  # raises on traversal
                    md = render_gene_markdown(gene, redact_body=redact_body)
                    write_atomic(vault_root=vault_root, target=target, content=md)
                    disk_hash = compute_disk_hash(target)
                    state.upsert_record(
                        gene_id=gene.gene_id,
                        path=relpath,
                        ts=_time.time(),
                        disk_hash=disk_hash,
                    )
                    exported += 1
                except ValueError as exc:
                    log.warning("path safety violation for gene %s: %s", row[0], exc)
                    errors += 1
                except Exception:
                    log.warning("export failed for gene %s", row[0], exc_info=True)
                    errors += 1

    state.update_top_level_state(
        last_full_export_ts=_time.time(),
        exported_gene_count=exported,
    )

    return {
        "genes_exported": exported,
        "elapsed_seconds": _time.monotonic() - t0,
        "errors": errors,
    }


def _row_to_gene(row) -> Any:
    """Adapt a sqlite3.Row to the SimpleNamespace shape `_build_frontmatter` expects."""
    from types import SimpleNamespace
    domains_raw = row["domains"] if isinstance(row["domains"], str) else ""
    # genome stores domains as comma- or json-encoded; adapt as needed
    if domains_raw.startswith("["):
        import json
        try:
            domains = json.loads(domains_raw)
        except Exception:
            domains = []
    elif domains_raw:
        domains = [d.strip() for d in domains_raw.split(",") if d.strip()]
    else:
        domains = []

    return SimpleNamespace(
        gene_id=row["gene_id"],
        content=row["content"] or "",
        content_type=row["content_type"] or "code",
        source_id=row["source_id"] or "",
        source_lines=row["source_lines"] or "",
        domains=domains,
        chromatin=row["chromatin"] or "euchromatin",
        content_sha256=row["content_sha256"] or "",
        last_seen=row["last_seen"] or "",
        last_seen_ts=row["last_seen_ts"] or 0.0,
        live_truth_score=row["live_truth_score"] or 0.0,
        co_activation_partners=row["co_activation_partners"] or 0,
        party_id=row["party_id"] or "",
        participant_handle=row["participant_handle"] or "",
    )
```

**NOTE for the implementer (READ BEFORE STARTING THE TASK):** The `_row_to_gene` adapter assumes a particular DDL shape for `genes` and `gene_attribution`. **Confirm the actual schema before writing any code:**

```bash
sqlite3 /tmp/probe-genome.db ".schema genes"
sqlite3 /tmp/probe-genome.db ".schema gene_attribution"
```

Or in Python:
```python
from helix_context.genome import Genome
g = Genome(path=":memory:", synonym_map={})
print([row["name"] for row in g.conn.execute("PRAGMA table_info(genes)")])
print([row["name"] for row in g.conn.execute("PRAGMA table_info(gene_attribution)")])
```

Likely mismatches to watch for:
- `gene_attribution` may not exist on older genomes — the LEFT JOIN will succeed but always return NULL for `party_id`, silently breaking the party filter. If the table doesn't exist, drop the LEFT JOIN and read `party_id` from `genes` directly (some forks store it there).
- `domains` may be stored as TEXT (JSON-encoded), TEXT (comma-separated), or as a separate junction table. The adapter handles JSON-and-comma; if it's a junction, you'll need an additional subquery.
- `last_seen` and `last_seen_ts` may be a single column with a different name (e.g., `updated_at`).
- `co_activation_partners` may be a count derived from the `co_activation` table rather than a column on `genes`. If it's not a column, COALESCE to 0.

If column names differ, fix the SELECT query and the adapter, but keep the function signature stable.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_writer.py::TestFullExport -v`
Expected: 3 PASSED. If failures, the most likely cause is a column-name mismatch with the actual genome schema — fix the SELECT in `full_export` and re-run.

- [ ] **Step 5: Commit**

```bash
git add helix_context/vault/writer.py tests/test_vault_writer.py
git commit -m "feat(vault): add writer.full_export with party filter + state recording"
```

---

## Task 9: `writer.incremental_export()`

**Files:**
- Modify: `helix_context/vault/writer.py` (add `incremental_export`)
- Modify: `tests/test_vault_writer.py` (add tests)

- [ ] **Step 1: Add the failing test**

Append:

```python
class TestIncrementalExport:
    def test_only_re_exports_changed_genes(self, tmp_path: Path):
        from helix_context.vault.writer import full_export, incremental_export

        genome = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
        try:
            g1_id = genome.upsert_gene(_make_test_gene("v1", "a.py", ["x"]))
            g2_id = genome.upsert_gene(_make_test_gene("v1", "b.py", ["x"]))

            vault_root = tmp_path / "vault"
            state = VaultState(vault_root=vault_root)
            lock = VaultLock(vault_root=vault_root)
            try:
                full_export(
                    genome=genome, state=state, lock=lock,
                    vault_root=vault_root, party_id="",
                    redact_body=False, fan_out_threshold=5000,
                )
                t_baseline = state.read_top_level_state()["last_full_export_ts"]

                # Sleep briefly so last_seen advances measurably
                time.sleep(0.05)

                # Modify only g1
                genome.upsert_gene(_make_test_gene("v2_changed", "a.py", ["x"]))

                stats = incremental_export(
                    genome=genome, state=state, lock=lock,
                    vault_root=vault_root, party_id="",
                    redact_body=False, fan_out_threshold=5000,
                    since_ts=t_baseline,
                )
                assert stats["genes_exported"] == 1, \
                    f"expected only the changed gene, got {stats}"
            finally:
                state.close()
        finally:
            genome.close()

    def test_returns_zero_when_nothing_changed(self, tmp_path: Path):
        from helix_context.vault.writer import full_export, incremental_export

        genome = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
        try:
            genome.upsert_gene(_make_test_gene("hi", "x.py", ["y"]))
            vault_root = tmp_path / "vault"
            state = VaultState(vault_root=vault_root)
            lock = VaultLock(vault_root=vault_root)
            try:
                full_export(
                    genome=genome, state=state, lock=lock,
                    vault_root=vault_root, party_id="",
                    redact_body=False, fan_out_threshold=5000,
                )
                stats = incremental_export(
                    genome=genome, state=state, lock=lock,
                    vault_root=vault_root, party_id="",
                    redact_body=False, fan_out_threshold=5000,
                    since_ts=time.time(),
                )
                assert stats["genes_exported"] == 0
            finally:
                state.close()
        finally:
            genome.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_writer.py::TestIncrementalExport -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `incremental_export`**

Add to `helix_context/vault/writer.py`:

```python
def incremental_export(
    *,
    genome,
    state: VaultState,
    lock: VaultLock,
    vault_root: Path,
    party_id: str,
    redact_body: bool,
    fan_out_threshold: int,
    since_ts: float,
    batch_size: int = 500,
) -> dict:
    """Re-export genes whose last_seen_ts > since_ts.

    Reuses the same render pipeline as full_export. The filter clause uses
    idx_genes_last_seen (added in Task 4) for efficient range scan.
    """
    t0 = _time.monotonic()
    exported = 0
    errors = 0

    sql = (
        "SELECT g.gene_id, g.content, g.content_type, g.source_id, "
        "g.source_lines, g.domains, g.chromatin, g.content_sha256, "
        "g.last_seen, g.last_seen_ts, g.live_truth_score, "
        "g.co_activation_partners, "
        "ga.party_id, ga.participant_handle "
        "FROM genes g LEFT JOIN gene_attribution ga ON g.gene_id = ga.gene_id "
        "WHERE g.last_seen_ts > ?"
    )
    params = [since_ts]
    if party_id:
        sql += " AND ga.party_id = ?"
        params.append(party_id)

    with lock:
        cur = genome.read_conn.execute(sql, params)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                try:
                    gene = _row_to_gene(row)
                    relpath = derive_gene_relpath(
                        domain=(gene.domains[0] if gene.domains else None),
                        source_id=gene.source_id,
                        gene_id=gene.gene_id,
                    )
                    target = vault_root / relpath
                    safe_resolve_under(vault_root, target)
                    md = render_gene_markdown(gene, redact_body=redact_body)
                    write_atomic(vault_root=vault_root, target=target, content=md)
                    disk_hash = compute_disk_hash(target)
                    state.upsert_record(
                        gene_id=gene.gene_id,
                        path=relpath,
                        ts=_time.time(),
                        disk_hash=disk_hash,
                    )
                    exported += 1
                except ValueError as exc:
                    log.warning("path safety violation for gene %s: %s", row[0], exc)
                    errors += 1
                except Exception:
                    log.warning("incremental export failed for gene %s", row[0], exc_info=True)
                    errors += 1

    state.update_top_level_state(last_incremental_export_ts=_time.time())

    return {
        "genes_exported": exported,
        "elapsed_seconds": _time.monotonic() - t0,
        "errors": errors,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_writer.py::TestIncrementalExport -v`
Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add helix_context/vault/writer.py tests/test_vault_writer.py
git commit -m "feat(vault): add writer.incremental_export with last_seen_ts filter"
```

---

## Task 10: `writer.trace_export()`

**Files:**
- Modify: `helix_context/vault/writer.py` (add `trace_export`)
- Modify: `tests/test_vault_writer.py` (add tests)

- [ ] **Step 1: Add the failing test**

```python
class TestTraceExport:
    def test_writes_trace_file(self, tmp_path: Path):
        from helix_context.vault.writer import trace_export

        vault_root = tmp_path / "vault"
        vault_root.mkdir(mode=0o700)
        lock = VaultLock(vault_root=vault_root)

        path = trace_export(
            vault_root=vault_root, lock=lock,
            request_id="abc12345",
            trigger_reason="auto",
            total_latency_ms=1234,
            health_status="aligned",
            stage_timing_ms={"extract": 12, "rerank": 1000},
            fingerprint_route="path A",
            foveated_ranks="(top-3)",
            final_genes=[("middleware-7f3a1c", 1, 0.92)],
            retention_hours=48,
        )
        assert path.exists()
        assert "abc12345" in path.name
        assert "_exp" in path.name  # the unix-epoch suffix
        body = path.read_text()
        assert "abc12345" in body
        assert "rerank" in body

    def test_filename_contains_unix_expiry(self, tmp_path: Path):
        from helix_context.vault.writer import trace_export

        vault_root = tmp_path / "vault"
        vault_root.mkdir(mode=0o700)
        lock = VaultLock(vault_root=vault_root)

        path = trace_export(
            vault_root=vault_root, lock=lock,
            request_id="x", trigger_reason="auto",
            total_latency_ms=0, health_status="aligned",
            stage_timing_ms={}, fingerprint_route="", foveated_ranks="",
            final_genes=[], retention_hours=24,
        )
        # Extract the _exp<number> portion
        import re
        m = re.search(r"_exp(\d+)\.md$", path.name)
        assert m is not None
        unix_expiry = int(m.group(1))
        # Should be within ~24h from now
        now = int(time.time())
        assert now + 23 * 3600 < unix_expiry < now + 25 * 3600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_writer.py::TestTraceExport -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `trace_export`**

Add to `helix_context/vault/writer.py`:

```python
import datetime as _dt
import re as _re

_TRACE_FILENAME_RE = _re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})_(?P<id>[A-Za-z0-9_-]+)"
    r"(?:_exp(?P<exp>\d+))?\.md$"
)


def trace_export(
    *,
    vault_root: Path,
    lock: VaultLock,
    request_id: str,
    trigger_reason: str,
    total_latency_ms: int,
    health_status: str,
    stage_timing_ms: dict,
    fingerprint_route: str,
    foveated_ranks: str,
    final_genes: list,  # list of (filename_stem, rank, score)
    retention_hours: int,
) -> Path:
    """Export a /context call trace to _traces/<ts>_<id>_exp<unix>.md.

    Filename encodes expires_at as `_exp<unix-epoch>` so the pruner can
    filter expired traces by name without parsing frontmatter.
    """
    now_ts = _time.time()
    expires_unix = int(now_ts + retention_hours * 3600)
    created_dt = _dt.datetime.utcfromtimestamp(now_ts)
    expires_dt = _dt.datetime.utcfromtimestamp(expires_unix)

    fname = (
        f"{created_dt.strftime('%Y-%m-%dT%H-%M-%S')}_"
        f"{request_id}_exp{expires_unix}.md"
    )
    target = vault_root / "_traces" / fname

    md = render_trace_markdown(
        request_id=request_id,
        created_at=created_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        pinned=False,
        trigger_reason=trigger_reason,
        total_latency_ms=total_latency_ms,
        health_status=health_status,
        stage_timing_ms=stage_timing_ms,
        fingerprint_route=fingerprint_route,
        foveated_ranks=foveated_ranks,
        final_genes=final_genes,
    )

    with lock:
        write_atomic(vault_root=vault_root, target=target, content=md)
    return target
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_writer.py::TestTraceExport -v`
Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add helix_context/vault/writer.py tests/test_vault_writer.py
git commit -m "feat(vault): add writer.trace_export with unix-epoch filename suffix"
```

---

## Task 11: `vault/pruner.py` — TTL prune via filename

**Files:**
- Create: `helix_context/vault/pruner.py`
- Test: `tests/test_vault_pruner.py` (NEW)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_pruner.py
"""Tests for the vault pruner — TTL via filename, rollup, _stale/ refresh."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from helix_context.vault.pruner import prune_traces


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "_traces").mkdir(parents=True, mode=0o700)
    (root / "_traces-pinned").mkdir(parents=True, mode=0o700)
    (root / "_meta" / "trace-rollups").mkdir(parents=True, mode=0o700)
    return root


def _write_trace(vault_root: Path, *, name: str, content: str = "x") -> Path:
    p = vault_root / "_traces" / name
    p.write_text(content)
    return p


class TestPruneByFilenameSuffix:
    def test_deletes_expired(self, vault_root):
        past = int(time.time()) - 100
        f = _write_trace(vault_root, name=f"2026-01-01T00-00-00_abc_exp{past}.md")
        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["pruned_count"] == 1
        assert not f.exists()

    def test_keeps_unexpired(self, vault_root):
        future = int(time.time()) + 3600
        f = _write_trace(vault_root, name=f"2026-01-01T00-00-00_abc_exp{future}.md")
        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["pruned_count"] == 0
        assert f.exists()

    def test_skips_pinned_folder(self, vault_root):
        past = int(time.time()) - 100
        # Same expired suffix, but in _traces-pinned/ — must NOT be pruned
        p = vault_root / "_traces-pinned" / f"2026-01-01T00-00-00_abc_exp{past}.md"
        p.write_text("x")
        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["pruned_count"] == 0
        assert p.exists()

    def test_corrupt_filename_falls_back_to_mtime(self, vault_root):
        f = _write_trace(vault_root, name="corrupt-no-exp-suffix.md")
        # File mtime is "now"; with mtime fallback +24h, should NOT prune
        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["pruned_count"] == 0
        assert f.exists()

    def test_corrupt_filename_old_mtime_pruned(self, vault_root):
        f = _write_trace(vault_root, name="corrupt-no-exp.md")
        # Set mtime to 31 days ago — fallback prune kicks in
        old = time.time() - 31 * 86400
        import os
        os.utime(f, (old, old))
        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["pruned_count"] == 1


class TestForcePruneHardCap:
    def test_pinned_force_pruned_past_hard_cap(self, vault_root):
        # Pinned file (no _exp) with very old mtime
        p = vault_root / "_traces-pinned" / "2026-01-01T00-00-00_abc.md"
        p.write_text("x")
        old = time.time() - 1000 * 3600  # well past 720h
        import os
        os.utime(p, (old, old))

        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["force_pruned_count"] == 1
        assert not p.exists()

    def test_disabled_when_zero(self, vault_root):
        p = vault_root / "_traces-pinned" / "2026-01-01T00-00-00_abc.md"
        p.write_text("x")
        old = time.time() - 1_000_000
        import os
        os.utime(p, (old, old))

        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=0,  # disabled
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["force_pruned_count"] == 0
        assert p.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_pruner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helix_context.vault.pruner'`.

- [ ] **Step 3: Implement `helix_context/vault/pruner.py`**

```python
"""Vault pruner — TTL-based trace deletion + pre-prune rollup + _stale/ refresh.

Pruning uses filename-encoded `_exp<unix-epoch>` suffix to avoid YAML parsing
unexpired traces. The hard-cap force-prune handles pinned files past a
compliance retention window.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_TRACE_EXP_RE = re.compile(r"_exp(\d+)\.md$")
_FALLBACK_MTIME_HOURS = 24
_HARD_FALLBACK_DAYS = 30  # corrupt-filename traces older than this are pruned


def prune_traces(
    *,
    vault_root: Path,
    max_retention_hours_hard: int,
    rollup_enabled: bool,
    rollup_shard: str,
) -> dict:
    """Walk _traces/ and _traces-pinned/, prune expired and force-pruned.

    Returns: {pruned_count, force_pruned_count, rollup_appended, errors}
    """
    pruned = 0
    force_pruned = 0
    rollup_appended = 0
    errors = 0

    now = time.time()
    traces_dir = vault_root / "_traces"
    pinned_dir = vault_root / "_traces-pinned"

    # Standard prune: _traces/ files with _exp<unix>
    if traces_dir.exists():
        for entry in traces_dir.iterdir():
            if not entry.is_file() or not entry.name.endswith(".md"):
                continue
            try:
                expires_unix = _parse_expiry_from_filename(entry, now)
                if expires_unix is None:
                    # Corrupt filename → mtime + fallback hours
                    mtime = entry.stat().st_mtime
                    if (now - mtime) > _HARD_FALLBACK_DAYS * 86400:
                        if rollup_enabled:
                            _append_rollup(entry, vault_root, rollup_shard)
                            rollup_appended += 1
                        entry.unlink()
                        pruned += 1
                    continue
                if expires_unix < now:
                    if rollup_enabled:
                        _append_rollup(entry, vault_root, rollup_shard)
                        rollup_appended += 1
                    entry.unlink()
                    pruned += 1
            except Exception:
                log.warning("prune failed for %s", entry, exc_info=True)
                errors += 1

    # Force-prune: _traces-pinned/ files past max_retention_hours_hard (if > 0)
    if pinned_dir.exists() and max_retention_hours_hard > 0:
        cutoff = now - max_retention_hours_hard * 3600
        for entry in pinned_dir.iterdir():
            if not entry.is_file() or not entry.name.endswith(".md"):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    log.warning(
                        "vault_force_prune: %s past max_retention_hours_hard=%dh",
                        entry.name, max_retention_hours_hard,
                    )
                    if rollup_enabled:
                        _append_rollup(entry, vault_root, rollup_shard)
                        rollup_appended += 1
                    entry.unlink()
                    force_pruned += 1
            except Exception:
                log.warning("force-prune failed for %s", entry, exc_info=True)
                errors += 1

    return {
        "pruned_count": pruned,
        "force_pruned_count": force_pruned,
        "rollup_appended": rollup_appended,
        "errors": errors,
    }


def _parse_expiry_from_filename(path: Path, now: float) -> Optional[float]:
    m = _TRACE_EXP_RE.search(path.name)
    if not m:
        return None
    return float(m.group(1))


def _append_rollup(trace_path: Path, vault_root: Path, shard: str) -> None:
    """Append a one-line summary of the trace to today's rollup file before deletion.

    The full implementation lives in Task 12; for Task 11 this is a stub that
    just creates the rollup file if needed but doesn't write detailed rows.
    Task 12 expands this to read the trace's frontmatter for fields.
    """
    # Stub for now — Task 12 implements full rollup
    pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_pruner.py -v`
Expected: 7 PASSED.

- [ ] **Step 5: Commit**

```bash
git add helix_context/vault/pruner.py tests/test_vault_pruner.py
git commit -m "feat(vault): add pruner.prune_traces — TTL via filename + force-prune"
```

---

## Task 12: Pruner — rollup append (hour-sharded)

**Files:**
- Modify: `helix_context/vault/pruner.py` (implement `_append_rollup`)
- Modify: `tests/test_vault_pruner.py` (add tests)

- [ ] **Step 1: Add the failing test**

Append:

```python
class TestRollupAppend:
    def test_creates_hour_sharded_file(self, vault_root):
        past = int(time.time()) - 100
        # Write a trace with valid YAML frontmatter the rollup can read
        content = (
            "---\n"
            f"request_id: req1\n"
            f"created_at: '2026-05-06T14:23:00Z'\n"
            f"expires_at: '2026-05-06T14:23:00Z'\n"
            f"total_latency_ms: 5000\n"
            f"health_status: aligned\n"
            f"trigger_reason: auto\n"
            f"pinned: false\n"
            "---\n\nbody\n"
        )
        (vault_root / "_traces" / f"2026-05-06T14-23-00_req1_exp{past}.md").write_text(content)

        prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=True,
            rollup_shard="hour",
        )
        rollup = vault_root / "_meta" / "trace-rollups" / "2026-05-06" / "14.md"
        assert rollup.exists()
        text = rollup.read_text()
        assert "req1" in text
        assert "5000" in text  # latency_ms
        assert "aligned" in text  # health

    def test_appends_to_existing_file(self, vault_root):
        # Pre-populate the rollup file
        d = vault_root / "_meta" / "trace-rollups" / "2026-05-06"
        d.mkdir(parents=True)
        (d / "14.md").write_text("# Existing rollup\n\n| time | id |\n|---|---|\n| previous | yes |\n")

        past = int(time.time()) - 100
        content = (
            "---\n"
            f"request_id: req2\n"
            f"created_at: '2026-05-06T14:55:00Z'\n"
            f"expires_at: '2026-05-06T14:55:00Z'\n"
            f"total_latency_ms: 100\n"
            f"health_status: sparse\n"
            f"trigger_reason: latency_outlier\n"
            f"pinned: false\n"
            "---\n\nbody\n"
        )
        (vault_root / "_traces" / f"2026-05-06T14-55-00_req2_exp{past}.md").write_text(content)

        prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=True,
            rollup_shard="hour",
        )
        rollup = (vault_root / "_meta" / "trace-rollups" / "2026-05-06" / "14.md")
        text = rollup.read_text()
        assert "previous" in text  # existing content preserved
        assert "req2" in text       # new row appended

    def test_daily_shard(self, vault_root):
        past = int(time.time()) - 100
        content = (
            "---\n"
            f"request_id: req3\n"
            f"created_at: '2026-05-06T14:00:00Z'\n"
            f"expires_at: '2026-05-06T14:00:00Z'\n"
            f"total_latency_ms: 0\n"
            f"health_status: aligned\n"
            f"trigger_reason: auto\n"
            f"pinned: false\n"
            "---\n\nbody\n"
        )
        (vault_root / "_traces" / f"2026-05-06T14-00-00_req3_exp{past}.md").write_text(content)

        prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=True,
            rollup_shard="daily",
        )
        rollup = vault_root / "_meta" / "trace-rollups" / "2026-05-06.md"
        assert rollup.exists()
        assert "req3" in rollup.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_pruner.py::TestRollupAppend -v`
Expected: 3 FAIL — rollup files not created (because `_append_rollup` is a stub).

- [ ] **Step 3: Implement `_append_rollup` in pruner.py**

Replace the stub `_append_rollup` with:

```python
import yaml as _yaml


_ROLLUP_HEADER = (
    "# Trace rollup\n\n"
    "| time | request_id | latency_ms | health | trigger |\n"
    "|---|---|---|---|---|\n"
)


def _append_rollup(trace_path: Path, vault_root: Path, shard: str) -> None:
    """Append a one-line summary of `trace_path` to the appropriate rollup file."""
    fm = _read_trace_frontmatter(trace_path)
    if fm is None:
        return

    created_at = str(fm.get("created_at", ""))
    request_id = str(fm.get("request_id", "?"))
    latency = fm.get("total_latency_ms", "?")
    health = str(fm.get("health_status", "?"))
    trigger = str(fm.get("trigger_reason", "?"))

    # Parse the date for shard path
    date_part = created_at[:10] if len(created_at) >= 10 else "unknown"
    hour_part = created_at[11:13] if len(created_at) >= 13 else "00"
    time_part = created_at[11:19] if len(created_at) >= 19 else created_at

    rollups_root = vault_root / "_meta" / "trace-rollups"
    if shard == "hour":
        rollup_path = rollups_root / date_part / f"{hour_part}.md"
    else:  # daily
        rollup_path = rollups_root / f"{date_part}.md"

    rollup_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not rollup_path.exists():
        rollup_path.write_text(_ROLLUP_HEADER, encoding="utf-8")

    new_row = f"| {time_part} | {request_id} | {latency} | {health} | {trigger} |\n"
    with rollup_path.open("a", encoding="utf-8") as f:
        f.write(new_row)


def _read_trace_frontmatter(path: Path) -> Optional[dict]:
    """Parse the YAML frontmatter block from a trace file."""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        log.warning("could not read trace %s", path, exc_info=True)
        return None
    if not content.startswith("---\n"):
        return None
    rest = content[len("---\n"):]
    try:
        end = rest.index("---\n")
    except ValueError:
        return None
    try:
        return _yaml.safe_load(rest[:end]) or {}
    except Exception:
        log.warning("could not parse frontmatter in %s", path, exc_info=True)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_pruner.py -v`
Expected: 10 PASSED (7 prior + 3 rollup).

- [ ] **Step 5: Commit**

```bash
git add helix_context/vault/pruner.py tests/test_vault_pruner.py
git commit -m "feat(vault): pruner — hour-sharded rollup append before TTL prune"
```

---

## Task 13: Pruner — `_stale/` refresh + eager fan-out migration

**Files:**
- Modify: `helix_context/vault/pruner.py` (add `refresh_stale_view`, `migrate_fan_out`)
- Modify: `tests/test_vault_pruner.py` (add tests)

- [ ] **Step 1: Add the failing tests**

```python
class TestRefreshStaleView:
    def test_creates_pointer_notes_for_stale_genes(self, tmp_path):
        from helix_context.vault.pruner import refresh_stale_view
        from helix_context.genome import Genome
        from tests.conftest import make_gene

        vault_root = tmp_path / "vault"
        (vault_root / "genes" / "auth").mkdir(parents=True, mode=0o700)
        # Place a gene file at a known path
        (vault_root / "genes" / "auth" / "gene-123.md").write_text("---\ngene_id: 123\n---\n")

        genome = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
        try:
            g = make_gene("hi", domains=["auth"], source_id="auth/x.py")
            gid = genome.upsert_gene(g)
            # Force-low live_truth_score by direct UPDATE
            genome.conn.execute(
                "UPDATE genes SET live_truth_score = 0.1 WHERE gene_id = ?",
                (gid,),
            )
            genome.conn.commit()

            refresh_stale_view(
                vault_root=vault_root,
                genome=genome,
                stale_threshold=0.5,
                party_id="",
            )
            stale_files = list((vault_root / "_stale").glob("*.md"))
            assert len(stale_files) == 1
        finally:
            genome.close()

    def test_removes_recovered_genes(self, tmp_path):
        # If a gene's live_truth_score recovers, its _stale/ entry must disappear.
        from helix_context.vault.pruner import refresh_stale_view
        from helix_context.vault.schema import derive_gene_filename
        from helix_context.genome import Genome
        from tests.conftest import make_gene

        vault_root = tmp_path / "vault"
        (vault_root / "_stale").mkdir(parents=True, mode=0o700)

        genome = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
        try:
            g = make_gene("hi", domains=["auth"], source_id="x.py")
            gid = genome.upsert_gene(g)
            # Pre-existing pointer note matching the actual gene we'll query.
            # File must follow the same naming `derive_gene_filename` produces
            # so the cleanup pass can identify it as a recovered gene.
            stale_name = derive_gene_filename("x.py", gid)
            stale_file = vault_root / "_stale" / stale_name
            stale_file.write_text(f"[[{Path(stale_name).stem}]]")
            assert stale_file.exists()  # sanity

            # live_truth_score is healthy by default → gene is NOT in stale set
            refresh_stale_view(
                vault_root=vault_root,
                genome=genome,
                stale_threshold=0.5,
                party_id="",
            )
            assert not stale_file.exists()
        finally:
            genome.close()


class TestFanOutMigration:
    def test_no_migration_below_threshold(self, tmp_path):
        from helix_context.vault.pruner import migrate_fan_out_if_needed
        from helix_context.vault.state import VaultState

        vault_root = tmp_path / "vault"
        (vault_root / "genes" / "auth").mkdir(parents=True, mode=0o700)
        for i in range(3):
            (vault_root / "genes" / "auth" / f"file{i}-aaa{i:03d}.md").write_text("x")
        state = VaultState(vault_root=vault_root)
        try:
            result = migrate_fan_out_if_needed(
                vault_root=vault_root, state=state, fan_out_threshold=10,
            )
            assert result["files_migrated"] == 0
            assert result["migrated_domains"] == []
        finally:
            state.close()

    def test_migrates_when_threshold_crossed(self, tmp_path):
        from helix_context.vault.pruner import migrate_fan_out_if_needed
        from helix_context.vault.state import VaultState

        vault_root = tmp_path / "vault"
        (vault_root / "genes" / "core").mkdir(parents=True, mode=0o700)
        # Create 5 files at the flat level
        for i in range(5):
            short = f"abc{i:03d}"
            f = vault_root / "genes" / "core" / f"file{i}-{short}.md"
            f.write_text(f"---\ngene_id: {short}\n---\n")

        state = VaultState(vault_root=vault_root)
        try:
            # Pre-record state for one of the files so we can verify the path update
            state.upsert_record(
                gene_id="abc000", path="genes/core/file0-abc000.md",
                ts=1.0, disk_hash=None,
            )
            result = migrate_fan_out_if_needed(
                vault_root=vault_root, state=state, fan_out_threshold=3,
            )
            assert result["files_migrated"] == 5
            assert "core" in result["migrated_domains"]
            # Files moved into <first-2-chars>/ subfolders
            assert (vault_root / "genes" / "core" / "ab" / "file0-abc000.md").exists()
            assert not (vault_root / "genes" / "core" / "file0-abc000.md").exists()
            # state.vault_path was updated for the recorded gene
            rec = state.get_record("abc000")
            assert rec.vault_path == "genes/core/ab/file0-abc000.md"
            # Top-level state records the engaged domain
            top = state.read_top_level_state()
            assert "core" in top["fan_out_engaged_domains"]
        finally:
            state.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_pruner.py::TestRefreshStaleView -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `refresh_stale_view` in pruner.py**

```python
def refresh_stale_view(
    *,
    vault_root: Path,
    genome,
    stale_threshold: float,
    party_id: str,
) -> dict:
    """Repopulate the _stale/ folder based on live_truth_score.

    v1: pointer notes on all platforms (containing [[gene-<id>]] wikilink).
    Symlink-on-POSIX is deferred to v1.1 — pointer notes are simpler and
    Obsidian renders the wikilink fine. The spec mentions "symlinks (POSIX)
    or pointer notes (Windows)" but the v1 ships pointer notes uniformly
    for ease-of-implementation.

    Removes pointer entries for genes that no longer qualify as stale.
    Returns: {added, removed, errors}
    """
    stale_dir = vault_root / "_stale"
    stale_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Query genes that are currently stale
    sql = (
        "SELECT g.gene_id, g.source_id, g.domains "
        "FROM genes g LEFT JOIN gene_attribution ga ON g.gene_id = ga.gene_id "
        "WHERE g.live_truth_score < ? AND g.chromatin = 'euchromatin'"
    )
    params = [stale_threshold]
    if party_id:
        sql += " AND ga.party_id = ?"
        params.append(party_id)

    expected_filenames: set[str] = set()
    added = 0
    errors = 0

    for row in genome.read_conn.execute(sql, params):
        gene_id = row["gene_id"]
        source_id = row["source_id"] or ""
        # Use the same naming as gene file: <stem>-<short_id>.md
        from helix_context.vault.schema import derive_gene_filename
        stale_name = derive_gene_filename(source_id, gene_id)
        expected_filenames.add(stale_name)
        target = stale_dir / stale_name
        if target.exists():
            continue
        try:
            # Try symlink; fall back to pointer note on failure
            try:
                # Compute relative link to canonical file
                # We use the short_id stem only for the wikilink target
                link_text = f"[[{Path(stale_name).stem}]]"
                target.write_text(
                    f"# Stale (live_truth_score < {stale_threshold})\n\n{link_text}\n",
                    encoding="utf-8",
                )
            except Exception:
                log.warning("stale view write failed for %s", gene_id, exc_info=True)
                errors += 1
                continue
            added += 1
        except Exception:
            errors += 1

    # Remove pointer entries for genes no longer stale
    removed = 0
    for entry in list(stale_dir.iterdir()):
        if entry.is_file() and entry.name.endswith(".md") and entry.name not in expected_filenames:
            try:
                entry.unlink()
                removed += 1
            except Exception:
                errors += 1

    return {"added": added, "removed": removed, "errors": errors}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_pruner.py::TestRefreshStaleView -v`
Expected: 2 PASSED.

- [ ] **Step 5: Add eager fan-out migration**

Add to `helix_context/vault/pruner.py`:

```python
def migrate_fan_out_if_needed(
    *,
    vault_root: Path,
    state,           # VaultState
    fan_out_threshold: int,
) -> dict:
    """Migrate flat domain folders past the threshold to 2-level fan-out.

    Eager — fires the moment a flat folder crosses the threshold. Updates
    state.vault_path for each migrated gene to keep wikilinks coherent.
    """
    genes_root = vault_root / "genes"
    if not genes_root.exists():
        return {"migrated_domains": [], "files_migrated": 0}

    migrated_domains = []
    files_migrated = 0
    for domain_dir in genes_root.iterdir():
        if not domain_dir.is_dir() or domain_dir.name.startswith("_"):
            continue
        flat_files = [p for p in domain_dir.iterdir() if p.is_file() and p.suffix == ".md"]
        if len(flat_files) <= fan_out_threshold:
            continue
        log.info("migrating fan-out for domain=%s (%d files)", domain_dir.name, len(flat_files))
        for f in flat_files:
            # short_id is the chars between the last '-' and '.md'
            stem = f.stem  # name without .md
            try:
                short_id = stem.rsplit("-", 1)[1]
            except IndexError:
                continue
            first2 = short_id[:2]
            new_dir = domain_dir / first2
            new_dir.mkdir(exist_ok=True, mode=0o700)
            new_path = new_dir / f.name
            os.replace(f, new_path)
            # Update state record — find by old path
            relpath_old = f"genes/{domain_dir.name}/{f.name}"
            relpath_new = f"genes/{domain_dir.name}/{first2}/{f.name}"
            for rec in state.iter_records():
                if rec.vault_path == relpath_old:
                    state.upsert_record(
                        gene_id=rec.gene_id,
                        path=relpath_new,
                        ts=rec.last_exported_ts,
                        disk_hash=rec.last_exported_disk_hash,
                    )
                    break
            files_migrated += 1
        migrated_domains.append(domain_dir.name)

    if migrated_domains:
        top = state.read_top_level_state()
        engaged = set(top.get("fan_out_engaged_domains", []))
        engaged.update(migrated_domains)
        state.update_top_level_state(fan_out_engaged_domains=sorted(engaged))

    return {"migrated_domains": migrated_domains, "files_migrated": files_migrated}
```

- [ ] **Step 6: Run all pruner tests**

Run: `python -m pytest tests/test_vault_pruner.py -v`
Expected: 12 PASSED.

- [ ] **Step 7: Commit**

```bash
git add helix_context/vault/pruner.py tests/test_vault_pruner.py
git commit -m "feat(vault): pruner — _stale/ refresh + eager fan-out migration"
```

---

## Task 14: `vault/__init__.py` — `VaultManager`

**Files:**
- Modify: `helix_context/vault/__init__.py` (replace docstring with full `VaultManager`)
- Test: `tests/test_vault_manager.py` (NEW)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_manager.py
"""Tests for VaultManager — the public API for the vault package."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from helix_context.config import HelixConfig, VaultConfig, VaultTracesConfig
from helix_context.genome import Genome
from helix_context.vault import VaultManager


@pytest.fixture
def cfg(tmp_path: Path) -> HelixConfig:
    c = HelixConfig()
    c.vault = VaultConfig(
        enabled=True, path=str(tmp_path / "vault"),
        party_id="", fan_out_threshold=5000,
        redact_body=False, stale_threshold=0.5,
        traces=VaultTracesConfig(
            enabled=True, retention_hours=48,
            max_retention_hours_hard=720,
            max_count=10000, rollup_enabled=True,
            rollup_shard="hour", prune_interval_minutes=60,
            trigger_only=False,
        ),
    )
    return c


@pytest.fixture
def genome(tmp_path: Path) -> Genome:
    g = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
    yield g
    g.close()


def test_disabled_vault_does_nothing(tmp_path: Path, genome):
    cfg = HelixConfig()
    cfg.vault = VaultConfig(enabled=False, path=str(tmp_path / "vault"))
    vm = VaultManager(config=cfg, genome=genome)
    vm.start()
    assert not (tmp_path / "vault").exists()
    vm.stop()


def test_start_creates_vault_root(cfg, genome):
    vm = VaultManager(config=cfg, genome=genome)
    vm.start()
    try:
        assert Path(cfg.vault.path).exists()
        # mode=0o700 (Unix-only check)
        if hasattr(Path(cfg.vault.path).stat(), "st_mode"):
            import stat
            mode = Path(cfg.vault.path).stat().st_mode & 0o777
            # 0o700 expected, but some test environments have stricter umask
            assert mode in (0o700, 0o755, 0o750), f"got mode {oct(mode)}"
    finally:
        vm.stop()


def test_stale_sentinel_cleaned_at_startup(cfg, genome):
    Path(cfg.vault.path).mkdir(parents=True, exist_ok=True, mode=0o700)
    sentinel = Path(cfg.vault.path) / ".helix-syncing"
    sentinel.touch()
    vm = VaultManager(config=cfg, genome=genome)
    vm.start()
    try:
        assert not sentinel.exists()
    finally:
        vm.stop()


def test_full_export_method(cfg, genome):
    from tests.conftest import make_gene
    genome.upsert_gene(make_gene("hello", domains=["auth"], source_id="x.py"))
    vm = VaultManager(config=cfg, genome=genome)
    vm.start()
    try:
        stats = vm.full_export()
        assert stats["genes_exported"] == 1
    finally:
        vm.stop()


def test_trace_export_method(cfg, genome):
    vm = VaultManager(config=cfg, genome=genome)
    vm.start()
    try:
        path = vm.trace_export(
            request_id="x",
            trigger_reason="auto",
            total_latency_ms=100,
            health_status="aligned",
            stage_timing_ms={"extract": 1},
            fingerprint_route="",
            foveated_ranks="",
            final_genes=[],
        )
        assert path.exists()
        assert "_exp" in path.name
    finally:
        vm.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_manager.py -v`
Expected: FAIL with `ImportError: cannot import name 'VaultManager'`.

- [ ] **Step 3: Implement `VaultManager` in `helix_context/vault/__init__.py`**

```python
"""Helix vault — operator-facing markdown export of the genome.

VaultManager is the public API. It owns:
- VaultState (vault.db)
- VaultLock (file lock)
- A pruner thread (TTL prune + _stale/ refresh)

The writer functions are stateless — VaultManager holds the dependencies
and dispatches to them.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from helix_context.vault import pruner as pruner_mod
from helix_context.vault import writer as writer_mod
from helix_context.vault.locking import VaultLock
from helix_context.vault.state import VaultState

log = logging.getLogger(__name__)


class VaultManager:
    """Top-level vault lifecycle. Wired into the FastAPI lifespan.

    Per Invariant I-1, vault failures never degrade retrieval. Every public
    method on VaultManager catches its exceptions internally and logs them.
    """

    def __init__(self, *, config, genome) -> None:
        self.config = config
        self.genome = genome
        self.vault_root: Optional[Path] = None
        self.state: Optional[VaultState] = None
        self.lock: Optional[VaultLock] = None
        self._pruner_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._started = False

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        if not self.config.vault.enabled:
            log.info("vault disabled (config.vault.enabled=false)")
            return
        try:
            self.vault_root = Path(self.config.vault.path).expanduser()
            self.vault_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._cleanup_stale_sentinel()
            self.state = VaultState(vault_root=self.vault_root)
            self.lock = VaultLock(vault_root=self.vault_root)
            self._write_readme()
            self._start_pruner_thread()
            self._started = True
            log.info("vault started at %s", self.vault_root)
        except Exception:
            log.warning("vault start failed; vault disabled", exc_info=True)
            self._started = False

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        if self._pruner_thread is not None:
            self._pruner_thread.join(timeout=5)
        if self.state is not None:
            self.state.close()
        self._started = False

    def _cleanup_stale_sentinel(self) -> None:
        sentinel = self.vault_root / ".helix-syncing"
        try:
            sentinel.unlink()
            log.info("cleaned stale .helix-syncing sentinel")
        except FileNotFoundError:
            pass
        except Exception:
            log.warning("could not clean stale sentinel", exc_info=True)

    def _write_readme(self) -> None:
        readme = self.vault_root / "README.md"
        if readme.exists():
            return  # don't clobber operator-edited READMEs
        content = (
            "# Helix vault\n\n"
            "Generated by helix-context. Do NOT edit gene frontmatter or body in v1 "
            "— edits are not synced back. Authored fields render as cosmetic "
            "placeholders for forward-compat with v1.1.\n\n"
            "## Layout\n\n"
            "- `genes/<domain>/<stem>-<id>.md` — exported genes (one per gene_id)\n"
            "- `_traces/` — diagnostic exports of recent /context calls (auto-pruned)\n"
            "- `_traces-pinned/` — operator-preserved traces; immune to TTL prune\n"
            "  (subject to `vault.traces.max_retention_hours_hard`)\n"
            "- `_stale/` — read-only view of genes with low live_truth_score\n"
            "- `_meta/` — rollups and aggregations\n"
            "- `_sessions/` — per-participant activity logs\n\n"
            "## Backup\n\n"
            "The genome can re-render every gene file from `genome.db` alone. "
            "However, pinned traces (`_traces-pinned/`) exist only here. If you "
            "depend on them, back up the **whole vault folder** alongside `genome.db`.\n\n"
            "## v1.1 follow-up\n\n"
            "Authored fields (operator_notes, operator_tags, pinned, supersedes...) "
            "are placeholders in v1. v1.1 enables write-back via watcher + validator. "
            "See `docs/superpowers/specs/2026-05-06-obsidian-vault-export-full-design-v1.1plus.md`.\n"
        )
        readme.write_text(content, encoding="utf-8")

    def _start_pruner_thread(self) -> None:
        def _loop():
            interval = max(60, self.config.vault.traces.prune_interval_minutes * 60)
            while not self._stop_event.wait(interval):
                try:
                    self.run_prune_cycle()
                except Exception:
                    log.warning("prune cycle failed", exc_info=True)

        self._pruner_thread = threading.Thread(
            target=_loop, name="helix-vault-pruner", daemon=True
        )
        self._pruner_thread.start()

    # ── Public methods ─────────────────────────────────────────────────

    def full_export(self) -> dict:
        if not self._started:
            return {"genes_exported": 0, "elapsed_seconds": 0, "errors": 0, "skipped": "vault disabled"}
        return writer_mod.full_export(
            genome=self.genome,
            state=self.state,
            lock=self.lock,
            vault_root=self.vault_root,
            party_id=self.config.vault.party_id,
            redact_body=self.config.vault.redact_body,
            fan_out_threshold=self.config.vault.fan_out_threshold,
        )

    def incremental_export(self, since_ts: Optional[float] = None) -> dict:
        if not self._started:
            return {"genes_exported": 0, "elapsed_seconds": 0, "errors": 0}
        if since_ts is None:
            top = self.state.read_top_level_state()
            since_ts = max(top["last_full_export_ts"], top["last_incremental_export_ts"])
        return writer_mod.incremental_export(
            genome=self.genome,
            state=self.state,
            lock=self.lock,
            vault_root=self.vault_root,
            party_id=self.config.vault.party_id,
            redact_body=self.config.vault.redact_body,
            fan_out_threshold=self.config.vault.fan_out_threshold,
            since_ts=since_ts,
        )

    def trace_export(self, **kwargs) -> Path:
        # NOTE: `lock` is injected from VaultManager state — never accept it
        # from the caller's kwargs. The HTTP _TraceBody schema deliberately
        # excludes it for this reason. Refactors that change kwargs handling
        # MUST keep this self-injection.
        return writer_mod.trace_export(
            vault_root=self.vault_root,
            lock=self.lock,
            retention_hours=self.config.vault.traces.retention_hours,
            **kwargs,
        )

    def run_prune_cycle(self) -> dict:
        """One pass of the pruner: TTL prune + _stale/ refresh + fan-out."""
        if not self._started:
            return {}
        results = {}
        results["traces"] = pruner_mod.prune_traces(
            vault_root=self.vault_root,
            max_retention_hours_hard=self.config.vault.traces.max_retention_hours_hard,
            rollup_enabled=self.config.vault.traces.rollup_enabled,
            rollup_shard=self.config.vault.traces.rollup_shard,
        )
        results["stale"] = pruner_mod.refresh_stale_view(
            vault_root=self.vault_root,
            genome=self.genome,
            stale_threshold=self.config.vault.stale_threshold,
            party_id=self.config.vault.party_id,
        )
        results["fan_out"] = pruner_mod.migrate_fan_out_if_needed(
            vault_root=self.vault_root,
            state=self.state,
            fan_out_threshold=self.config.vault.fan_out_threshold,
        )
        return results

    def status(self) -> dict:
        if not self._started:
            return {"enabled": False, "reason": "not started"}
        top = self.state.read_top_level_state()
        return {
            "enabled": True,
            "vault_root": str(self.vault_root),
            "last_full_export_ts": top["last_full_export_ts"],
            "last_incremental_export_ts": top["last_incremental_export_ts"],
            "exported_gene_count": top["exported_gene_count"],
            "watcher_state": 0,  # v1: no watcher
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_manager.py -v`
Expected: 5 PASSED.

- [ ] **Step 5: Commit**

```bash
git add helix_context/vault/__init__.py tests/test_vault_manager.py
git commit -m "feat(vault): add VaultManager — public API + pruner thread"
```

---

## Task 15: Server lifespan hook + HTTP endpoints

**Files:**
- Modify: `helix_context/server.py` (wire `VaultManager` into lifespan; add 5 endpoints)
- Test: `tests/test_vault_endpoints.py` (NEW)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_endpoints.py
"""Tests for /export/obsidian + /vault/status + /vault/trace endpoints."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helix_context.config import HelixConfig, VaultConfig, VaultTracesConfig
from helix_context.server import create_app


@pytest.fixture
def app(tmp_path: Path):
    cfg = HelixConfig()
    cfg.vault = VaultConfig(
        enabled=True, path=str(tmp_path / "vault"),
        traces=VaultTracesConfig(),
    )
    # genome path
    cfg.genome.path = ":memory:"
    a = create_app(cfg)
    yield a


def test_vault_status_endpoint(app):
    with TestClient(app) as c:
        r = c.get("/vault/status")
        assert r.status_code == 200
        body = r.json()
        assert "enabled" in body


def test_export_obsidian_triggers_full(app):
    with TestClient(app) as c:
        r = c.post("/export/obsidian", json={"full": True})
        assert r.status_code == 200
        body = r.json()
        assert "genes_exported" in body


def test_vault_trace_writes_file(app, tmp_path):
    with TestClient(app) as c:
        r = c.post("/vault/trace", json={
            "request_id": "abc12345",
            "trigger_reason": "manual",
            "total_latency_ms": 1234,
            "health_status": "aligned",
            "stage_timing_ms": {"extract": 1},
            "fingerprint_route": "",
            "foveated_ranks": "",
            "final_genes": [],
        })
        assert r.status_code == 200
        body = r.json()
        assert "path" in body
        assert Path(body["path"]).exists()


def test_pin_and_unpin_round_trip(app, tmp_path):
    with TestClient(app) as c:
        # First write a trace
        r = c.post("/vault/trace", json={
            "request_id": "tobepin",
            "trigger_reason": "manual",
            "total_latency_ms": 0,
            "health_status": "aligned",
            "stage_timing_ms": {},
            "fingerprint_route": "", "foveated_ranks": "",
            "final_genes": [],
        })
        assert r.status_code == 200
        # Pin it
        r2 = c.post(f"/vault/traces/tobepin/pin")
        assert r2.status_code == 200
        # Unpin it
        r3 = c.post(f"/vault/traces/tobepin/unpin")
        assert r3.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_endpoints.py -v`
Expected: FAIL — endpoints don't exist yet (404 or attribute errors on `app.state.vault`).

- [ ] **Step 3: Wire `VaultManager` into the FastAPI lifespan**

Find the existing `create_app` function (or `app` factory) in `helix_context/server.py`. Read it BEFORE editing — note where `Genome` is constructed and how it's exposed to routes.

The existing server stores the `Genome` instance somewhere — likely on `app.state.helix.genome`, `app.state.genome`, or as a module-level singleton. Find that exact reference; it's where the vault must read from.

Concrete wiring (adapt to actual variable names but keep the order):

```python
from helix_context.vault import VaultManager

# Inside create_app(config) — locate the existing lifespan @asynccontextmanager
# (or equivalent startup hook). Add the vault wiring AFTER genome is constructed
# and BEFORE the yield. Match the existing pattern for replication.py if it's
# in the same file — that's the closest precedent.

@asynccontextmanager
async def lifespan(app):
    # ... existing setup that constructs `genome` ...
    # IMPORTANT: this `genome` reference must be the SAME instance that
    # /context and /ingest use — passing a fresh Genome to VaultManager
    # would defeat WAL-snapshot consistency.
    vault = VaultManager(config=config, genome=genome)
    vault.start()
    app.state.vault = vault       # endpoints read this via request.app.state
    try:
        yield
    finally:
        vault.stop()
        # ... existing shutdown ...
```

If `server.py` doesn't use `@asynccontextmanager` lifespan but instead assigns directly during `create_app` (older FastAPI pattern), put the `vault.start()` call alongside the existing `replication.ReplicationManager.start()` call (if present) or the `Genome(...)` constructor — same scope, immediately after `genome` is bound.

**Read `server.py` first.** If a `replication` instance is already wired in lifespan, copy that exact structure for `vault`.

- [ ] **Step 4: Add the 5 HTTP endpoints**

Add to `server.py` (next to other routes):

```python
@app.post("/export/obsidian")
async def post_export_obsidian(request: Request):
    body = await request.json()
    full = bool(body.get("full", False))
    vault = request.app.state.vault
    if full:
        return vault.full_export()
    return vault.incremental_export()


@app.get("/vault/status")
async def get_vault_status(request: Request):
    return request.app.state.vault.status()


class _TraceBody(BaseModel):  # add to existing pydantic models near top
    request_id: str
    trigger_reason: str
    total_latency_ms: int
    health_status: str
    stage_timing_ms: dict
    fingerprint_route: str
    foveated_ranks: str
    final_genes: list


@app.post("/vault/trace")
async def post_vault_trace(body: _TraceBody, request: Request):
    vault = request.app.state.vault
    path = vault.trace_export(**body.model_dump())
    return {"path": str(path), "request_id": body.request_id}


@app.post("/vault/traces/{request_id}/pin")
async def post_pin_trace(request_id: str, request: Request):
    vault = request.app.state.vault
    # Find the file in _traces/ matching this request_id
    traces_dir = vault.vault_root / "_traces"
    pinned_dir = vault.vault_root / "_traces-pinned"
    pinned_dir.mkdir(exist_ok=True, mode=0o700)
    matches = list(traces_dir.glob(f"*_{request_id}_exp*.md"))
    if not matches:
        return {"ok": False, "error": f"trace {request_id} not found in _traces/"}
    src = matches[0]
    # Strip _exp<n> suffix; new name = ts_id.md
    import re
    new_name = re.sub(r"_exp\d+\.md$", ".md", src.name)
    dst = pinned_dir / new_name
    src.replace(dst)
    return {"ok": True, "pinned_path": str(dst)}


@app.post("/vault/traces/{request_id}/unpin")
async def post_unpin_trace(request_id: str, request: Request):
    import time
    vault = request.app.state.vault
    pinned_dir = vault.vault_root / "_traces-pinned"
    traces_dir = vault.vault_root / "_traces"
    matches = list(pinned_dir.glob(f"*_{request_id}.md"))
    if not matches:
        return {"ok": False, "error": f"trace {request_id} not found in _traces-pinned/"}
    src = matches[0]
    # Add fresh _exp<unix> suffix
    retention_hours = vault.config.vault.traces.retention_hours
    expires_unix = int(time.time() + retention_hours * 3600)
    new_name = src.stem + f"_exp{expires_unix}.md"
    dst = traces_dir / new_name
    src.replace(dst)
    return {"ok": True, "unpinned_path": str(dst)}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_endpoints.py -v`
Expected: 4 PASSED.

- [ ] **Step 6: Run the full test suite for regressions**

Run: `python -m pytest tests/ -m "not live" --tb=short -q --ignore=tests/test_fusion_plr.py --ignore=tests/test_ray_trace_theta.py`
Expected: All previously-passing tests still pass; new vault tests pass.

- [ ] **Step 7: Commit**

```bash
git add helix_context/server.py tests/test_vault_endpoints.py
git commit -m "feat(server): wire VaultManager + 5 HTTP endpoints (export, status, trace, pin/unpin)"
```

---

## Task 16: OTel telemetry hooks for vault operations

**Files:**
- Modify: `helix_context/telemetry.py` (add factories for vault metrics)
- Modify: `helix_context/vault/writer.py` (record export histograms)
- Modify: `helix_context/vault/pruner.py` (record pruner histogram + force-prune counter)
- Modify: `helix_context/vault/__init__.py` (record file_count + disk_bytes gauges in `status()`)
- Test: `tests/test_vault_telemetry.py` (NEW)

The spec mandates `helix_vault_export_seconds`, `helix_vault_pruner_seconds`, `helix_vault_file_count`, `helix_vault_disk_bytes`, and `helix_vault_force_prune_total`. Match the noop-safe pattern from PR #36.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_telemetry.py
"""Tests for vault OTel instrument factories — caching + noop safety."""
from __future__ import annotations


def test_vault_export_histogram_caches():
    from helix_context.telemetry import vault_export_histogram
    h1 = vault_export_histogram()
    h2 = vault_export_histogram()
    assert h1 is h2


def test_vault_pruner_histogram_caches():
    from helix_context.telemetry import vault_pruner_histogram
    h1 = vault_pruner_histogram()
    h2 = vault_pruner_histogram()
    assert h1 is h2


def test_vault_force_prune_counter_caches():
    from helix_context.telemetry import vault_force_prune_counter
    c1 = vault_force_prune_counter()
    c2 = vault_force_prune_counter()
    assert c1 is c2


def test_vault_file_count_gauge_caches():
    from helix_context.telemetry import vault_file_count_gauge
    g1 = vault_file_count_gauge()
    g2 = vault_file_count_gauge()
    assert g1 is g2


def test_record_does_not_crash_in_noop_mode():
    """If OTel isn't installed the factories return _NoopInstrument; .record() is safe."""
    from helix_context.telemetry import (
        vault_export_histogram, vault_pruner_histogram,
        vault_force_prune_counter,
    )
    vault_export_histogram().record(0.5, {"kind": "full"})
    vault_pruner_histogram().record(0.1, {})
    vault_force_prune_counter().add(1, {"reason": "max_retention_hard"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_telemetry.py -v`
Expected: FAIL with `ImportError: cannot import name 'vault_export_histogram'`.

- [ ] **Step 3: Add factories to `helix_context/telemetry.py`**

Find the existing factory functions (e.g., `pipeline_stage_histogram`, `ribosome_call_histogram` from PR #36) and add four new factories using the same pattern:

```python
def vault_export_histogram():
    if "vault_export" not in _instruments:
        _instruments["vault_export"] = meter.create_histogram(
            "helix_vault_export_seconds",
            unit="s",
            description="Latency of vault export operations.",
        )
    return _instruments["vault_export"]


def vault_pruner_histogram():
    if "vault_pruner" not in _instruments:
        _instruments["vault_pruner"] = meter.create_histogram(
            "helix_vault_pruner_seconds",
            unit="s",
            description="Latency of one pruner cycle.",
        )
    return _instruments["vault_pruner"]


def vault_force_prune_counter():
    if "vault_force_prune" not in _instruments:
        _instruments["vault_force_prune"] = meter.create_counter(
            "helix_vault_force_prune_total",
            description="Pinned traces force-deleted per max_retention_hours_hard.",
        )
    return _instruments["vault_force_prune"]


def vault_file_count_gauge():
    """Imperative gauge — VaultManager.status() updates it on each call."""
    if "vault_file_count" not in _instruments:
        # Use a regular (non-observable) gauge so VaultManager can call .set()
        # imperatively from within status(). An observable_gauge with empty
        # callbacks would be a no-op.
        _instruments["vault_file_count"] = meter.create_gauge(
            "helix_vault_file_count",
            description="Files in each vault folder (per `folder` label).",
        )
    return _instruments["vault_file_count"]
```

If the existing helix telemetry only exposes `create_observable_gauge` (no `create_gauge`), use `create_observable_gauge` and pass a real callback that reads from a module-level cell that `VaultManager.status()` updates. Read PR #36's pattern in `helix_context/telemetry.py` to pick the right form for the OTel version this codebase uses.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_telemetry.py -v`
Expected: 5 PASSED.

- [ ] **Step 5: Hook the histograms into writer.py and pruner.py**

Wrap each export entry-point in writer.py and the pruner cycle in pruner.py:

In `helix_context/vault/writer.py`:

- `full_export` and `incremental_export` already define `t0 = _time.monotonic()` at the top (from Tasks 8 + 9). Just before each `return` statement, add:
  ```python
  try:
      vault_export_histogram().record(
          _time.monotonic() - t0,
          {"kind": "full"},   # or "incremental"
      )
  except Exception:
      pass
  ```

- `trace_export` does NOT yet define `t0` (Task 10 only uses `now_ts = _time.time()` for the filename). Add a new line at the very top of `trace_export`:
  ```python
  _t0 = _time.monotonic()
  ```
  Then at the very end, before `return target`:
  ```python
  try:
      vault_export_histogram().record(
          _time.monotonic() - _t0,
          {"kind": "trace"},
      )
  except Exception:
      pass
  ```
  Use `_t0` (with underscore) to avoid name collision with `now_ts` and any future variable named `t0`.

Add the import at the top of writer.py if not already present:
```python
from helix_context.telemetry import vault_export_histogram
```

In `helix_context/vault/pruner.py`, wrap `prune_traces`:

```python
import time as _time
from helix_context.telemetry import vault_pruner_histogram, vault_force_prune_counter

# At start of prune_traces:
_t0 = _time.monotonic()

# Just before return, after computing `force_pruned`:
try:
    vault_pruner_histogram().record(_time.monotonic() - _t0, {})
    if force_pruned > 0:
        vault_force_prune_counter().add(force_pruned, {"reason": "max_retention_hard"})
except Exception:
    pass
```

In `helix_context/vault/__init__.py`, extend `VaultManager.status()` to populate file counts:

```python
def status(self) -> dict:
    if not self._started:
        return {"enabled": False, "reason": "not started"}
    top = self.state.read_top_level_state()
    file_counts = self._compute_file_counts()
    disk_bytes = self._compute_disk_bytes()
    return {
        "enabled": True,
        "vault_root": str(self.vault_root),
        "last_full_export_ts": top["last_full_export_ts"],
        "last_incremental_export_ts": top["last_incremental_export_ts"],
        "exported_gene_count": top["exported_gene_count"],
        "watcher_state": 0,
        "file_counts": file_counts,
        "disk_bytes": disk_bytes,
    }


def _compute_file_counts(self) -> dict:
    counts = {}
    for sub in ("genes", "_traces", "_traces-pinned", "_inbox", "_stale"):
        d = self.vault_root / sub
        if d.exists():
            counts[sub] = sum(1 for p in d.rglob("*.md"))
        else:
            counts[sub] = 0
    return counts


def _compute_disk_bytes(self) -> int:
    total = 0
    for p in self.vault_root.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except Exception:
                pass
    return total
```

- [ ] **Step 6: Verify all vault tests still pass**

Run: `python -m pytest tests/test_vault_writer.py tests/test_vault_pruner.py tests/test_vault_manager.py tests/test_vault_telemetry.py -v --tb=short`
Expected: all GREEN.

- [ ] **Step 7: Commit**

```bash
git add helix_context/telemetry.py helix_context/vault/writer.py helix_context/vault/pruner.py helix_context/vault/__init__.py tests/test_vault_telemetry.py
git commit -m "feat(telemetry): vault export + pruner histograms + file/disk gauges"
```

---

## Task 17: `vault/cli.py` — `helix-vault` CLI

(Renumbered from Task 16 after OTel hooks were added as Task 16.)

**Files:**
- Create: `helix_context/vault/cli.py`
- Test: `tests/test_vault_cli.py` (NEW)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_cli.py
"""Tests for helix-vault CLI subcommands.

The CLI talks to the running server over HTTP. Tests mock the HTTP client.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from helix_context.vault.cli import main


def test_main_no_args_prints_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    out = capsys.readouterr().out + capsys.readouterr().err
    # argparse prints to stderr
    assert exc.value.code != 0


def test_status_calls_endpoint(capsys):
    with patch("helix_context.vault.cli.httpx") as httpx:
        client = MagicMock()
        httpx.Client.return_value.__enter__.return_value = client
        client.get.return_value.json.return_value = {"enabled": True}
        client.get.return_value.status_code = 200
        rc = main(["status"])
    assert rc == 0


def test_export_full_calls_endpoint():
    with patch("helix_context.vault.cli.httpx") as httpx:
        client = MagicMock()
        httpx.Client.return_value.__enter__.return_value = client
        client.post.return_value.status_code = 200
        client.post.return_value.json.return_value = {"genes_exported": 5}
        rc = main(["export", "--full"])
    assert rc == 0
    client.post.assert_called_once()
    call_args = client.post.call_args
    assert "/export/obsidian" in call_args[0][0]


def test_pin_calls_endpoint():
    with patch("helix_context.vault.cli.httpx") as httpx:
        client = MagicMock()
        httpx.Client.return_value.__enter__.return_value = client
        client.post.return_value.status_code = 200
        client.post.return_value.json.return_value = {"ok": True}
        rc = main(["pin", "abc123"])
    assert rc == 0
    call_args = client.post.call_args
    assert "/vault/traces/abc123/pin" in call_args[0][0]


def test_trace_request_id_calls_trace_endpoint():
    with patch("helix_context.vault.cli.httpx") as httpx:
        client = MagicMock()
        httpx.Client.return_value.__enter__.return_value = client
        client.post.return_value.status_code = 200
        client.post.return_value.json.return_value = {"path": "/x", "request_id": "req1"}
        rc = main(["trace", "req1"])
    assert rc == 0
    call_args = client.post.call_args
    assert "/vault/trace" in call_args[0][0]


def test_trace_last_calls_status_endpoint(capsys):
    with patch("helix_context.vault.cli.httpx") as httpx:
        client = MagicMock()
        httpx.Client.return_value.__enter__.return_value = client
        client.get.return_value.status_code = 200
        client.get.return_value.json.return_value = {"vault_root": "/tmp/v"}
        rc = main(["trace", "--last", "5"])
    assert rc == 0
    # Output should mention the vault path and an `ls` hint
    out = capsys.readouterr().out
    assert "/tmp/v" in out
    # /vault/status was hit, NOT /vault/trace
    client.get.assert_called_once()
    assert "/vault/status" in client.get.call_args[0][0]


def test_trace_no_args_errors():
    with patch("helix_context.vault.cli.httpx"):
        rc = main(["trace"])
    assert rc != 0  # returns 2 when neither request_id nor --last given
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_cli.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `helix_context/vault/cli.py`**

```python
"""helix-vault — operator CLI for the vault.

Talks to the running helix server via HTTP. Avoids shared SQLite handles
and concurrent-writer races.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

import httpx


def _api_base() -> str:
    return os.environ.get("HELIX_URL", "http://127.0.0.1:11437")


def _print(obj) -> None:
    print(json.dumps(obj, indent=2))


def _cmd_export(args) -> int:
    payload = {"full": args.full}
    with httpx.Client(timeout=300) as c:
        r = c.post(f"{_api_base()}/export/obsidian", json=payload)
    if r.status_code != 200:
        print(f"export failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print(r.json())
    return 0


def _cmd_status(args) -> int:
    with httpx.Client(timeout=10) as c:
        r = c.get(f"{_api_base()}/vault/status")
    if r.status_code != 200:
        print(f"status failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print(r.json())
    return 0


def _cmd_trace(args) -> int:
    """Manually export a trace.

    Two modes:
      helix-vault trace <request_id>  — write a trace for one specific request
      helix-vault trace --last N      — list the last N exported trace files
                                        (no new write; just shows what's on disk)
    """
    if args.last is not None:
        # List recent traces from /vault/status — server doesn't have a list
        # endpoint in v1, so we fall back to a status call and tell the operator
        # where to look. Future v1.1 may add /vault/traces?last=N.
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{_api_base()}/vault/status")
        if r.status_code != 200:
            print(f"trace --last failed: {r.status_code} {r.text}", file=sys.stderr)
            return 1
        body = r.json()
        vault_root = body.get("vault_root", "(unknown)")
        print(f"Recent traces under {vault_root}/_traces/ — list with:")
        print(f"  ls -lt {vault_root}/_traces/ | head -n {args.last + 1}")
        return 0

    if not args.request_id:
        print("trace: must pass <request_id> or --last N", file=sys.stderr)
        return 2

    payload = {
        "request_id": args.request_id,
        "trigger_reason": "manual",
        "total_latency_ms": 0,
        "health_status": "aligned",
        "stage_timing_ms": {},
        "fingerprint_route": "",
        "foveated_ranks": "",
        "final_genes": [],
    }
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{_api_base()}/vault/trace", json=payload)
    if r.status_code != 200:
        print(f"trace failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print(r.json())
    return 0


def _cmd_pin(args) -> int:
    with httpx.Client(timeout=10) as c:
        r = c.post(f"{_api_base()}/vault/traces/{args.request_id}/pin")
    if r.status_code != 200:
        print(f"pin failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print(r.json())
    return 0


def _cmd_unpin(args) -> int:
    with httpx.Client(timeout=10) as c:
        r = c.post(f"{_api_base()}/vault/traces/{args.request_id}/unpin")
    if r.status_code != 200:
        print(f"unpin failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print(r.json())
    return 0


def _cmd_prune(args) -> int:
    print("prune is automatic; for manual prune the helix server must be running",
          file=sys.stderr)
    return 0


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="helix-vault", description="Helix vault operator CLI")
    sp = p.add_subparsers(dest="cmd", required=True)

    ex = sp.add_parser("export", help="Trigger snapshot export")
    ex.add_argument("--full", action="store_true", help="Full re-export (default: incremental)")
    ex.set_defaults(func=_cmd_export)

    st = sp.add_parser("status", help="Show vault state")
    st.set_defaults(func=_cmd_status)

    tr = sp.add_parser("trace", help="Manually write a trace for a request_id, or list recent traces")
    tr.add_argument("request_id", nargs="?", default=None,
                    help="The request_id to trace; omit when using --last")
    tr.add_argument("--last", type=int, default=None,
                    help="Show how to list the last N traces (file-listing pointer)")
    tr.set_defaults(func=_cmd_trace)

    pn = sp.add_parser("pin", help="Pin a trace (move to _traces-pinned/)")
    pn.add_argument("request_id")
    pn.set_defaults(func=_cmd_pin)

    up = sp.add_parser("unpin", help="Unpin a trace (move back to _traces/, reset TTL)")
    up.add_argument("request_id")
    up.set_defaults(func=_cmd_unpin)

    pr = sp.add_parser("prune", help="Note about manual prune")
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(func=_cmd_prune)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vault_cli.py -v`
Expected: 7 PASSED (4 original + 3 new for `trace --last`, `trace <id>`, and `trace` no-args).

- [ ] **Step 5: Smoke test the CLI from the shell**

Run: `python -m helix_context.vault.cli --help`
Expected: argparse usage text with all 6 subcommands.

Run: `python -m helix_context.vault.cli status`
Expected: connection error (server not running) — prints "status failed: ConnectError" to stderr, exit code 1. This is correct behavior.

- [ ] **Step 6: Commit**

```bash
git add helix_context/vault/cli.py tests/test_vault_cli.py
git commit -m "feat(vault): add helix-vault CLI — export/status/trace/pin/unpin"
```

---

## Task 18: README + end-to-end integration test

**Files:**
- Test: `tests/test_vault_e2e.py` (NEW)
- Modify: `docs/clients/claude-code.md` (or similar — add vault section)

- [ ] **Step 1: Write the failing end-to-end test**

```python
# tests/test_vault_e2e.py
"""End-to-end: ingest → export → trace → pin → prune cycle, all via VaultManager."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from helix_context.config import HelixConfig, VaultConfig, VaultTracesConfig
from helix_context.genome import Genome
from helix_context.vault import VaultManager
from tests.conftest import make_gene


@pytest.fixture
def vm(tmp_path: Path):
    cfg = HelixConfig()
    cfg.vault = VaultConfig(
        enabled=True, path=str(tmp_path / "vault"),
        party_id="", fan_out_threshold=5000, redact_body=False,
        stale_threshold=0.5,
        traces=VaultTracesConfig(
            enabled=True, retention_hours=48,
            max_retention_hours_hard=720, max_count=10000,
            rollup_enabled=True, rollup_shard="hour",
            prune_interval_minutes=60, trigger_only=False,
        ),
    )
    genome = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
    vault_root = Path(cfg.vault.path)
    vm = VaultManager(config=cfg, genome=genome)
    vm.start()
    yield vm, genome, vault_root
    vm.stop()
    genome.close()


def test_full_cycle(vm):
    manager, genome, vault_root = vm

    # 1. Ingest some genes
    for i in range(5):
        genome.upsert_gene(make_gene(f"content-{i}", domains=["e2e"], source_id=f"e{i}.py"))

    # 2. Full export
    stats = manager.full_export()
    assert stats["genes_exported"] == 5

    # 3. Confirm files on disk
    files = list((vault_root / "genes" / "e2e").glob("*.md"))
    assert len(files) == 5

    # 4. README written
    assert (vault_root / "README.md").exists()
    readme = (vault_root / "README.md").read_text()
    assert "v1.1" in readme  # mentions deferred features

    # 5. Frontmatter contains both computed and authored placeholders
    sample = files[0].read_text()
    rest = sample[len("---\n"):]
    end = rest.index("---\n")
    fm = yaml.safe_load(rest[:end])
    assert "gene_id" in fm
    assert fm["operator_notes"] == ""  # placeholder
    assert fm["pinned"] is False        # placeholder

    # 6. Trace export
    trace = manager.trace_export(
        request_id="e2e-01",
        trigger_reason="manual",
        total_latency_ms=42,
        health_status="aligned",
        stage_timing_ms={"extract": 1, "rerank": 41},
        fingerprint_route="",
        foveated_ranks="",
        final_genes=[],
    )
    assert trace.exists()
    assert "_exp" in trace.name

    # 7. Pin via the writer-side helpers (file rename to _traces-pinned/)
    pinned_dir = vault_root / "_traces-pinned"
    pinned_dir.mkdir(exist_ok=True, mode=0o700)
    import re
    new_name = re.sub(r"_exp\d+\.md$", ".md", trace.name)
    pinned_path = pinned_dir / new_name
    trace.replace(pinned_path)
    assert pinned_path.exists()

    # 8. Run prune — pinned trace should survive (mtime is fresh)
    results = manager.run_prune_cycle()
    assert results["traces"]["pruned_count"] == 0
    assert results["traces"]["force_pruned_count"] == 0
    assert pinned_path.exists()

    # 9. Status method reflects state
    s = manager.status()
    assert s["enabled"] is True
    assert s["exported_gene_count"] == 5
```

- [ ] **Step 2: Run the test to verify it fails or passes**

Run: `python -m pytest tests/test_vault_e2e.py -v`
Expected: PASS in one shot if all prior tasks landed cleanly. If it fails, the failure points to a real integration bug introduced in one of Tasks 8–14.

- [ ] **Step 3: Add operator-facing documentation**

Append to `docs/clients/claude-code.md` (or whatever the equivalent operator-docs file is — find via grep for similar config blocks):

```markdown
## Obsidian vault export (v1, opt-in)

As of 2026-05-06, helix can export the genome to a configurable directory as
an Obsidian-compatible markdown vault. v1 is read-only — operator edits in
Obsidian are not synced back. Diagnostic traces of every `/context` call are
auto-exported and TTL-pruned (default 48h).

Enable in `helix.toml`:

```toml
[vault]
enabled = true
path = "~/.helix/vault"
# party_id = ""           # empty = server's primary party
# redact_body = false     # set true if Obsidian Sync / iCloud watches the path

[vault.traces]
retention_hours = 48
# max_retention_hours_hard = 720    # hard cap for compliance retention
```

CLI: `helix-vault {export, status, trace, pin, unpin}`. See
`docs/superpowers/specs/2026-05-06-obsidian-vault-export-design.md` for the
full v1 surface, and the `-full-design-v1.1plus.md` sibling for the deferred
authored-delta-sync work.
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_vault_e2e.py docs/clients/claude-code.md
git commit -m "test+docs: end-to-end vault cycle test + operator docs section"
```

---

## Final whole-branch verification

After all 18 tasks, run a full regression sweep:

```bash
python -m pytest tests/ -m "not live" --tb=short -q \
    --ignore=tests/test_fusion_plr.py \
    --ignore=tests/test_ray_trace_theta.py
```

Expected: all preexisting tests still pass; ~50 new tests added across the vault suite. Total should be **(prior pass count + ~50)** passed, **0 newly failing**.

If anything regressed, the most likely culprit is the genome SELECT in `full_export` / `incremental_export` — re-check the actual `genes` and `gene_attribution` schemas before retrying.

## Branch + PR handoff

```bash
git push -u origin spec/obsidian-vault-export
gh pr create --base master --head spec/obsidian-vault-export \
    --title "feat(vault): Obsidian vault export v1 (read-only + diagnostic traces)" \
    --body "$(cat <<'EOF'
Implements [`docs/superpowers/specs/2026-05-06-obsidian-vault-export-design.md`](docs/superpowers/specs/2026-05-06-obsidian-vault-export-design.md).

v1 is the read-only subset: snapshot export to a configurable Obsidian vault
directory, every-`/context` trace export with TTL prune + hour-sharded
rollups, `_stale/` operator view, eager fan-out migration past 5000
files/folder. Authored fields render as cosmetic placeholders for
forward-compat with v1.1.

The deferred v1.1+ design (watcher, validator, shadow-store, inbox ingest,
`_unresolved/` resolution loop, retrieval-contract changes) lives at
[`-full-design-v1.1plus.md`](docs/superpowers/specs/2026-05-06-obsidian-vault-export-full-design-v1.1plus.md).

Stacks on PR #32 + PR #36 (both merged). Closes Discussion #34.
EOF
)"
```

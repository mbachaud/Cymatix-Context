"""Writer-connection reopen paths keep the boot-path PRAGMAs.

Tech-debt audit 2026-10-22, finding 9: vacuum() and the refresh() fallback
reopened ``self.conn`` with only WAL / busy_timeout / journal_size_limit, so
after ``/admin/vacuum`` the writer silently reverted to SQLite's defaults —
``synchronous=FULL`` (undoing #372's NORMAL), the default page cache, and no
mmap (undoing the dynamic-ram-scaling plan).

The plan below is deliberately non-default so a dropped PRAGMA is visible on
any host regardless of what ``sqlite_memory_budget`` would resolve to.
"""

import pytest

from cymatix_context.hardware import SqliteMemPlan
from cymatix_context.knowledge_store import KnowledgeStore

_PLAN = SqliteMemPlan(
    mmap_size=8 * 1024 * 1024,
    writer_cache_size=-8192,
    reader_cache_size=-4096,
)

# What a bare sqlite3.connect() reports, i.e. what a dropped PRAGMA reverts to.
_SQLITE_DEFAULTS = {"synchronous": 2, "cache_size": -2000, "mmap_size": 0}


def _pragmas(store) -> dict:
    return {
        name: store.conn.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("synchronous", "cache_size", "mmap_size", "journal_mode")
    }


@pytest.fixture
def store(tmp_path):
    g = KnowledgeStore(path=str(tmp_path / "g.db"), mem_plan=_PLAN, synchronous="NORMAL")
    yield g
    g.close()


def test_boot_pragmas_differ_from_sqlite_defaults(store):
    """Precondition: the reopen tests below can only fail if boot values
    differ from what a bare connection would report."""
    boot = _pragmas(store)
    assert boot["synchronous"] == 1  # NORMAL
    assert boot["cache_size"] == _PLAN.writer_cache_size
    assert boot["mmap_size"] == _PLAN.mmap_size
    for name, default in _SQLITE_DEFAULTS.items():
        assert boot[name] != default, name


def test_vacuum_reopen_keeps_writer_pragmas(store):
    boot = _pragmas(store)
    store.vacuum()
    assert _pragmas(store) == boot


def test_refresh_fallback_reopen_keeps_writer_pragmas(store, monkeypatch):
    boot = _pragmas(store)

    def _broken_snapshot():
        raise RuntimeError("simulated bad connection state")

    monkeypatch.setattr(store, "_refresh_snapshot", _broken_snapshot)
    store.refresh()
    assert _pragmas(store) == boot

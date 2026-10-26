"""idx_genes_id_chromatin is an OPT-IN migration, never built on open.

Building it blocks the first writable open of a 947k store for ~38 s, so
``init_db`` must not create it.  ``scripts/migrate_genes_cover_index.py``
builds it on request; a large writable store opened without it logs one
info line naming that command.  Query results are identical either way.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.storage import ddl

from tests.test_tier_sql_rank_identical import CONFIGS, GOLDEN, QUERIES, _make

IDX = "idx_genes_id_chromatin"
MARK = "migrate_genes_cover_index"


def _has_index(db: Path) -> bool:
    c = sqlite3.connect(str(db))
    try:
        return c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (IDX,)
        ).fetchone() is not None
    finally:
        c.close()


def _hints(caplog):
    return [r for r in caplog.records if MARK in r.getMessage()]


def test_init_db_does_not_create_cover_index(tmp_path):
    store = _make(tmp_path)
    store.conn.commit()
    assert not _has_index(tmp_path / "g.db")


def test_migration_creates_index_and_is_idempotent(tmp_path):
    from scripts.migrate_genes_cover_index import migrate

    _make(tmp_path).conn.commit()
    db = tmp_path / "g.db"
    dry = migrate(db, dry_run=True)
    assert dry["status"] == "dry_run" and not _has_index(db)
    first = migrate(db)
    assert first["status"] == "created" and _has_index(db)
    assert first["elapsed_s"] >= 0 and first["holds_write_lock"] is True
    second = migrate(db)
    assert second["status"] == "already_present"
    assert _has_index(db)


def test_migration_missing_db_raises(tmp_path):
    from scripts.migrate_genes_cover_index import migrate

    with pytest.raises(FileNotFoundError):
        migrate(tmp_path / "nope.db")


def test_no_old_hint_and_autobuild_skipped_below_default_threshold(
        tmp_path, caplog):
    # The "run the migration" hint was replaced by the background build
    # (tests/test_cover_index_autobuild.py); a small store builds nothing.
    caplog.set_level(logging.INFO)
    _make(tmp_path).conn.commit()
    caplog.clear()
    KnowledgeStore(str(tmp_path / "g.db"))
    assert not _hints(caplog)
    assert not _has_index(tmp_path / "g.db")


@pytest.mark.parametrize("with_index", [False, True])
@pytest.mark.parametrize("cfg", list(CONFIGS))
@pytest.mark.parametrize("q", list(QUERIES))
def test_rank_identical_with_and_without_index(tmp_path, with_index, cfg, q):
    from scripts.migrate_genes_cover_index import migrate

    _make(tmp_path).conn.commit()
    if with_index:
        migrate(tmp_path / "g.db")
    store = KnowledgeStore(str(tmp_path / "g.db"), **CONFIGS[cfg])
    assert _has_index(tmp_path / "g.db") is with_index
    docs = store.query_docs(domains=QUERIES[q], entities=[], max_genes=8)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))[f"{cfg}/{q}"]
    assert [d.gene_id for d in docs] == golden["ids"]
    contrib = [
        [gid, [[k, repr(v)] for k, v in c.items()]]
        for gid, c in store.last_tier_contributions.items()
    ]
    assert contrib == golden["contrib"]

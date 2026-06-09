"""Issue #165 Option-B: path_key_index compaction + new-DDL shape.

Covers the three probe-backed moves: drop idx_pki_lookup, prune
dead pairs above PKI_NOISE_CUTOFF, rebuild WITHOUT ROWID — plus the
invariants that make them safe (scorer-visible rows unchanged, delete
path intact, constants shared with the Tier-0 scorer).
"""

from __future__ import annotations

import sqlite3

import pytest

from helix_context.storage.indexes import (
    PKI_NOISE_CUTOFF,
    compact_path_key_index,
)


# ── fixtures ───────────────────────────────────────────────────────────


def _legacy_db(path, live_pairs=3, live_genes=5, dead_genes=12,
               cutoff=8):
    """Build a pre-#165 (rowid + idx_pki_lookup) path_key_index.

    ``live_pairs`` pairs with ``live_genes`` rows each (scoreable) and
    one dead pair with ``dead_genes`` rows (> cutoff → scorer-skipped).
    """
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE path_key_index (
        path_token TEXT NOT NULL,
        kv_key     TEXT NOT NULL,
        gene_id    TEXT NOT NULL,
        PRIMARY KEY (path_token, kv_key, gene_id)
    )
    """)
    cur.execute(
        "CREATE INDEX idx_pki_lookup ON path_key_index(path_token, kv_key)"
    )
    cur.execute("CREATE INDEX idx_pki_gene ON path_key_index(gene_id)")
    for p in range(live_pairs):
        for g in range(live_genes):
            cur.execute(
                "INSERT INTO path_key_index VALUES (?,?,?)",
                (f"proj{p}", f"key{p}", f"g{p}_{g}"),
            )
    for g in range(dead_genes):
        cur.execute(
            "INSERT INTO path_key_index VALUES (?,?,?)",
            ("sources", "url", f"dead_{g}"),
        )
    conn.commit()
    return conn


def _names(conn, kind):
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type=?", (kind,)
        ).fetchall()
    }


# ── compaction on a legacy DB ──────────────────────────────────────────


def test_compaction_drops_lookup_index_and_autoindex(tmp_path):
    conn = _legacy_db(tmp_path / "g.db")
    assert "idx_pki_lookup" in _names(conn, "index")
    assert "sqlite_autoindex_path_key_index_1" in _names(conn, "index")

    report = compact_path_key_index(conn, noise_cutoff=8)

    idx = _names(conn, "index")
    assert "idx_pki_lookup" not in idx
    # WITHOUT ROWID tables have no PK autoindex
    assert "sqlite_autoindex_path_key_index_1" not in idx
    assert "idx_pki_gene" in idx
    assert report["had_lookup_index"] is True
    assert report["was_rowid_table"] is True


def test_compaction_prunes_only_dead_pairs(tmp_path):
    conn = _legacy_db(tmp_path / "g.db", live_pairs=3, live_genes=5,
                      dead_genes=12)
    report = compact_path_key_index(conn, noise_cutoff=8)
    rows = conn.execute(
        "SELECT path_token, kv_key, gene_id FROM path_key_index"
    ).fetchall()
    # all 15 live rows survive, all 12 dead rows pruned
    assert len(rows) == 15
    assert report["rows_before"] == 27
    assert report["rows_after"] == 15
    assert report["rows_pruned"] == 12
    assert report["dead_pairs"] == 1
    assert all(r[0] != "sources" for r in rows)


def test_compaction_is_scorer_visible_row_invariant(tmp_path):
    """Rows the Tier-0 scorer can credit are byte-identical pre/post.

    The scorer hard-skips pairs with cardinality > cutoff, so the set of
    rows in pairs <= cutoff IS the scorer-visible surface.
    """
    conn = _legacy_db(tmp_path / "g.db")
    cutoff = 8
    visible_before = set(conn.execute(
        "SELECT path_token, kv_key, gene_id FROM path_key_index "
        "WHERE (path_token, kv_key) IN ("
        "  SELECT path_token, kv_key FROM path_key_index "
        "  GROUP BY 1,2 HAVING COUNT(*) <= ?)", (cutoff,)
    ).fetchall())
    compact_path_key_index(conn, noise_cutoff=cutoff)
    visible_after = set(conn.execute(
        "SELECT path_token, kv_key, gene_id FROM path_key_index"
    ).fetchall())
    assert visible_before == visible_after


def test_compaction_lookup_plan_stays_covering(tmp_path):
    conn = _legacy_db(tmp_path / "g.db")
    compact_path_key_index(conn, noise_cutoff=8)
    plan = " ".join(
        r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT path_token, kv_key, gene_id "
            "FROM path_key_index WHERE path_token IN (?,?) "
            "AND kv_key IN (?,?)", ("a", "b", "c", "d")
        ).fetchall()
    )
    # WITHOUT ROWID: the table btree IS the PK — same covering shape
    assert "PRIMARY KEY" in plan
    assert "idx_pki_lookup" not in plan


def test_compaction_delete_path_intact(tmp_path):
    conn = _legacy_db(tmp_path / "g.db")
    compact_path_key_index(conn, noise_cutoff=8)
    plan = " ".join(
        r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM path_key_index "
            "WHERE gene_id = ?", ("g0_0",)
        ).fetchall()
    )
    assert "idx_pki_gene" in plan
    conn.execute("DELETE FROM path_key_index WHERE gene_id = 'g0_0'")
    n = conn.execute(
        "SELECT COUNT(*) FROM path_key_index WHERE gene_id='g0_0'"
    ).fetchone()[0]
    assert n == 0


def test_dry_run_reports_without_modifying(tmp_path):
    conn = _legacy_db(tmp_path / "g.db")
    report = compact_path_key_index(conn, noise_cutoff=8, dry_run=True)
    assert report["dry_run"] is True
    assert report["needs_rebuild"] is True
    assert report["dead_rows"] == 12
    assert "rows_after" not in report
    assert "idx_pki_lookup" in _names(conn, "index")
    assert conn.execute(
        "SELECT COUNT(*) FROM path_key_index").fetchone()[0] == 27


def test_compaction_idempotent(tmp_path):
    conn = _legacy_db(tmp_path / "g.db")
    compact_path_key_index(conn, noise_cutoff=8)
    report2 = compact_path_key_index(conn, noise_cutoff=8)
    assert report2["was_rowid_table"] is False
    assert report2["had_lookup_index"] is False
    assert report2["needs_rebuild"] is False
    assert report2["rows_after"] == report2["rows_before"]


def test_compaction_no_table_is_graceful(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    report = compact_path_key_index(conn)
    assert report["skipped"] == "no path_key_index table"


# ── new-DB DDL shape ───────────────────────────────────────────────────


def test_new_ddl_is_without_rowid_and_lookup_free(tmp_path):
    from helix_context.storage.ddl import _create_path_key_index
    conn = sqlite3.connect(str(tmp_path / "new.db"))
    _create_path_key_index(conn.cursor())
    conn.commit()
    idx = _names(conn, "index")
    assert "idx_pki_lookup" not in idx
    assert "sqlite_autoindex_path_key_index_1" not in idx
    assert "idx_pki_gene" in idx
    # round-trip a row through the PK
    conn.execute(
        "INSERT OR IGNORE INTO path_key_index VALUES ('p','k','g')")
    conn.execute(
        "INSERT OR IGNORE INTO path_key_index VALUES ('p','k','g')")
    assert conn.execute(
        "SELECT COUNT(*) FROM path_key_index").fetchone()[0] == 1


def test_scorer_and_compactor_share_cutoff():
    """Guard against drift: the Tier-0 scorer must import the canonical
    constants from storage.indexes (the compactor's defaults)."""
    import inspect
    from helix_context import knowledge_store
    src = inspect.getsource(knowledge_store)
    assert "from .storage.indexes import" in src
    assert "PKI_NOISE_CUTOFF = 200" not in src  # no local shadow
    assert PKI_NOISE_CUTOFF == 200

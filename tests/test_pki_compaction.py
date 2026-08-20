"""Issue #165 Option-B: path_key_index compaction + new-DDL shape.

Covers the three probe-backed moves: drop idx_pki_lookup, prune
dead pairs above PKI_NOISE_CUTOFF, rebuild WITHOUT ROWID — plus the
invariants that make them safe (scorer-visible rows unchanged, delete
path intact, constants shared with the Tier-0 scorer).
"""

from __future__ import annotations

import sqlite3

import pytest

from cymatix_context.storage.indexes import (
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
    from cymatix_context.storage.ddl import _create_path_key_index
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
    from cymatix_context import knowledge_store
    src = inspect.getsource(knowledge_store)
    assert "from .storage.indexes import" in src
    assert "PKI_NOISE_CUTOFF = 200" not in src  # no local shadow
    assert PKI_NOISE_CUTOFF == 200


# ── issue #370: drop/reclaim mode on pki-off stores ────────────────────


def _pki_gene(content: str, gene_id: str):
    """Gene with source_id + key_values (i.e. PKI-row-eligible)."""
    from cymatix_context.schemas import (
        ChromatinState, EpigeneticMarkers, Gene, PromoterTags,
    )
    return Gene(
        gene_id=gene_id,
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=["alpha"], entities=[]),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
        source_id="F:/Projects/demoproj/settings/config.py",
        key_values=["port=8080", "model=llava"],
    )


def test_drop_refused_when_pki_enabled(tmp_path):
    """The guard: drop=True + pki_enabled=True raises, names the knob,
    and leaves the table byte-identical."""
    conn = _legacy_db(tmp_path / "g.db")
    with pytest.raises(ValueError, match=r"pki_enabled"):
        compact_path_key_index(conn, drop=True, pki_enabled=True)
    assert "path_key_index" in _names(conn, "table")
    assert conn.execute(
        "SELECT COUNT(*) FROM path_key_index").fetchone()[0] == 27


def test_drop_refused_by_default(tmp_path):
    """pki_enabled defaults to True — a bare drop=True must refuse (a
    caller that forgets to thread the store flag cannot drop by accident)."""
    conn = _legacy_db(tmp_path / "g.db")
    with pytest.raises(ValueError, match=r"pki_enabled"):
        compact_path_key_index(conn, drop=True)
    assert "path_key_index" in _names(conn, "table")


def test_drop_succeeds_when_pki_disabled(tmp_path):
    conn = _legacy_db(tmp_path / "g.db")
    report = compact_path_key_index(conn, drop=True, pki_enabled=False)
    assert report["dropped"] is True
    assert report["rows_before"] == 27
    tables = _names(conn, "table")
    idx = _names(conn, "index")
    assert "path_key_index" not in tables
    # DROP TABLE takes the indexes down with it
    assert "idx_pki_gene" not in idx
    assert "idx_pki_lookup" not in idx
    assert "sqlite_autoindex_path_key_index_1" not in idx
    # honest reclaim wording: the drop alone frees nothing on disk
    assert "vacuum" in report["hint"].lower()
    assert "backfill_path_key_index" in report["hint"]


def test_drop_dry_run_reports_without_dropping(tmp_path):
    conn = _legacy_db(tmp_path / "g.db")
    report = compact_path_key_index(
        conn, drop=True, pki_enabled=False, dry_run=True,
    )
    assert report["would_drop"] is True
    assert report["rows_before"] == 27
    assert "dropped" not in report
    assert "path_key_index" in _names(conn, "table")


def test_drop_no_table_is_graceful(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    report = compact_path_key_index(conn, drop=True, pki_enabled=False)
    assert report["skipped"] == "no path_key_index table"


def test_sync_noops_after_drop(tmp_path):
    """Post-drop upserts must not abort: sync_path_key_index degrades to
    a no-op when the table is gone (same optional-table treatment as
    delete_gene)."""
    from cymatix_context.storage.indexes import sync_path_key_index
    conn = sqlite3.connect(str(tmp_path / "g.db"))
    gene = _pki_gene("post-drop upsert body", "pd-0")
    # no path_key_index table at all — must not raise
    sync_path_key_index(conn.cursor(), "pd-0", gene)
    assert "path_key_index" not in _names(conn, "table")


# ── #370 server seam: /admin/compact-pki?drop=true ─────────────────────


def _pki_client(tmp_path, pki_enabled: bool):
    from cymatix_context.config import GenomeConfig, RetrievalConfig
    from tests.conftest import make_client, make_cymatix_config
    config = make_cymatix_config(
        genome=GenomeConfig(
            path=str(tmp_path / "pki-drop.db"), cold_start_threshold=5,
        ),
        retrieval=RetrievalConfig(pki_enabled=pki_enabled),
    )
    return make_client(config=config)


def test_admin_drop_refused_when_pki_on(tmp_path):
    client = _pki_client(tmp_path, pki_enabled=True)
    genome = client.app.state.cymatix.genome
    genome.upsert_gene(_pki_gene("guard fixture", "guard-0"), apply_gate=False)

    resp = client.post("/admin/compact-pki?drop=true")
    assert resp.status_code == 409
    body = resp.json()
    assert body["ok"] is False
    assert "pki_enabled" in body["error"]
    # table untouched
    row = genome.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='path_key_index'").fetchone()
    assert row is not None


def test_admin_drop_pki_off_then_query_green(tmp_path):
    """Drop succeeds on a pki-off store; /context stays green post-drop
    (scorer-neutral: the pki-off read path never touches the table) and
    subsequent ingest upserts don't abort."""
    client = _pki_client(tmp_path, pki_enabled=False)
    genome = client.app.state.cymatix.genome
    genome.upsert_gene(
        _pki_gene("the splice stage compresses candidates", "drop-0"),
        apply_gate=False,
    )

    resp = client.post("/admin/compact-pki?drop=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["dropped"] is True
    row = genome.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='path_key_index'").fetchone()
    assert row is None

    # retrieval still green post-drop
    q = client.post("/context", json={"query": "splice stage candidates"})
    assert q.status_code == 200

    # the write path degrades to a no-op instead of aborting the upsert
    genome.upsert_gene(
        _pki_gene("ingest after the drop", "drop-1"), apply_gate=False,
    )
    assert genome.conn.execute(
        "SELECT COUNT(*) FROM genes WHERE gene_id='drop-1'"
    ).fetchone()[0] == 1


def test_admin_drop_dry_run_via_route(tmp_path):
    client = _pki_client(tmp_path, pki_enabled=False)
    resp = client.post("/admin/compact-pki?drop=true&dry_run=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["would_drop"] is True
    row = client.app.state.cymatix.genome.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='path_key_index'").fetchone()
    assert row is not None


# ── #370 rebuild story: backfill script recreates after a drop ──────────


def test_backfill_script_recreates_after_drop(tmp_path):
    """scripts/backfill_path_key_index.py rebuilds the index from the
    genes table if pki is ever re-enabled."""
    import importlib.util
    from pathlib import Path

    from cymatix_context.genome import Genome

    db_path = str(tmp_path / "rebuild.db")
    genome = Genome(db_path, pki_enabled=False)
    genome.upsert_gene(
        _pki_gene("rebuild fixture body", "rb-0"), apply_gate=False,
    )
    pre = genome.conn.execute(
        "SELECT COUNT(*) FROM path_key_index").fetchone()[0]
    assert pre > 0  # write path still populated it
    report = compact_path_key_index(
        genome.conn, drop=True, pki_enabled=False,
    )
    assert report["dropped"] is True
    genome.conn.close()

    spec = importlib.util.spec_from_file_location(
        "backfill_path_key_index",
        Path(__file__).resolve().parent.parent
        / "scripts" / "backfill_path_key_index.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    stats = mod.backfill(db_path, dry_run=False)
    assert stats["rows_inserted"] > 0
    assert stats["post_rows"] == pre  # byte-for-byte row recovery

    conn = sqlite3.connect(db_path)
    assert "path_key_index" in _names(conn, "table")
    assert conn.execute(
        "SELECT COUNT(*) FROM path_key_index").fetchone()[0] == pre
    conn.close()

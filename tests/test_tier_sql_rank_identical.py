"""Rank-identity + cost-driver pins for the tag_exact / lex_anchor SQL path.

perf/tier-sql: tag_exact (Tier 1) and lex_anchor each used to JOIN genes once
per posting ROW (random PK probe per row; 82k-116k rows per query at 947k).
They now share ONE posting fetch and probe genes once per DISTINCT gene.

The ranked output of every tier must be unchanged -- ties and insertion order
included, because the fuser breaks ties by list position.  The golden file was
captured from the pre-change code (``python tests/test_tier_sql_rank_identical.py
--write`` on the base commit); these tests compare the new code against it.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.schemas import ChromatinState

GOLDEN = Path(__file__).parent / "data" / "tier_sql_rank_golden.json"

CONFIGS = {
    "default": {},
    "idf": {"tag_idf_enabled": True},
    "dfcap": {"tag_df_cap": 0.3},
}
QUERIES = {
    "plain": ["alpha", "beta", "gamma", "delta"],
    "dupes": ["alpha", "alpha", "beta", "epsilon"],
    "rare": ["zeta", "eta", "theta"],
}


def _make(tmp_path, **kw) -> KnowledgeStore:
    from tests.conftest import make_gene

    store = KnowledgeStore(str(tmp_path / "g.db"), **kw)
    spec = []
    # Many genes with identical tag sets => exact score ties everywhere.
    for i in range(12):
        spec.append((f"t{i:02d}", ["alpha", "beta"], []))
    for i in range(6):
        spec.append((f"u{i:02d}", ["alpha", "gamma", "alphabet"], ["delta"]))
    spec.append(("v00", ["zeta", "eta"], []))
    spec.append(("v01", ["zeta"], []))
    spec.append(("w00", ["theta", "epsilon", "epsilon"], []))
    for gid, doms, ents in spec:
        store.upsert_doc(make_gene(content=f"doc {gid} body", domains=doms,
                                   entities=ents, gene_id=gid))
    store.upsert_doc(make_gene(content="cold doc", domains=["alpha", "zeta"],
                               gene_id="x_cold",
                               chromatin=ChromatinState.HETEROCHROMATIN))
    # orphan promoter rows: tag rows whose gene no longer exists
    store.conn.execute(
        "INSERT INTO promoter_index(gene_id, tag_type, tag_value) VALUES "
        "('orphan1','domain','alpha'),('orphan1','domain','zeta')")
    store.conn.commit()
    return store


def snapshot(tmp_path, cfg: str, q: str):
    store = _make(tmp_path, **CONFIGS[cfg])
    docs = store.query_docs(domains=QUERIES[q], entities=[], max_genes=8)
    ids = [d.gene_id for d in docs]
    contrib = [
        [gid, [[k, repr(v)] for k, v in c.items()]]
        for gid, c in store.last_tier_contributions.items()
    ]
    return {"ids": ids, "contrib": contrib}


def _all(tmp_path_factory):
    out = {}
    for cfg in CONFIGS:
        for q in QUERIES:
            out[f"{cfg}/{q}"] = snapshot(tmp_path_factory.mktemp("s"), cfg, q)
    return out


@pytest.mark.parametrize("cfg", list(CONFIGS))
@pytest.mark.parametrize("q", list(QUERIES))
def test_ranked_output_identical_to_pre_change_golden(tmp_path, cfg, q):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert snapshot(tmp_path, cfg, q) == golden[f"{cfg}/{q}"]


def _statements(store, fn):
    seen = []
    store.read_conn.set_trace_callback(seen.append)
    try:
        fn()
    finally:
        store.read_conn.set_trace_callback(None)
    return seen


def test_no_per_posting_row_genes_join(tmp_path):
    """Cost driver: no statement may probe genes once per posting row."""
    store = _make(tmp_path)
    stmts = _statements(
        store,
        lambda: store.query_docs(domains=QUERIES["plain"], entities=[], max_genes=8),
    )
    per_row = [
        s for s in stmts
        if "promoter_index pi" in s and "genes g" in s and "JOIN" in s
    ]
    assert per_row == [], per_row[:2]


def test_posting_fetch_is_index_only_and_genes_probed_per_distinct_gene(tmp_path):
    store = _make(tmp_path)
    sql, params = store._tag_postings_sql(["alpha", "beta"])
    plan = " | ".join(
        r[3] for r in store.conn.execute("EXPLAIN QUERY PLAN " + sql[0], params[0])
    )
    assert "SCAN genes" not in plan and "SCAN g" not in plan, plan
    assert "idx_promoter_cover" in plan, plan
    vplan = " | ".join(
        r[3] for r in store.conn.execute("EXPLAIN QUERY PLAN " + sql[1], params[1])
    )
    assert "SCAN genes" not in vplan and "SCAN g " not in vplan, vplan
    assert "DISTINCT" in sql[1]


def test_genes_probe_uses_covering_index_and_migration_is_idempotent(tmp_path):
    store = _make(tmp_path)
    names = {r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_genes_id_chromatin" in names
    # existing store: drop it, reopen => recreated; reopen again => no error
    store.conn.execute("DROP INDEX idx_genes_id_chromatin")
    store.conn.commit()
    store.close() if hasattr(store, "close") else None
    for _ in range(2):
        s2 = KnowledgeStore(str(tmp_path / "g.db"))
        assert s2.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='idx_genes_id_chromatin'"
        ).fetchone()
    (_, _), (_, vparams) = s2._tag_postings_sql(["alpha"])
    vsql = s2._tag_postings_sql(["alpha"])[0][1]
    plan = " | ".join(
        r[3] for r in s2.conn.execute("EXPLAIN QUERY PLAN " + vsql, vparams))
    assert "COVERING INDEX idx_genes_id_chromatin" in plan, plan


def test_postings_helper_matches_legacy_per_term_join(tmp_path):
    store = _make(tmp_path)
    terms = ["alpha", "zeta", "nosuchtag"]
    got = store._tag_postings(store.conn.cursor(), terms)
    for t in terms:
        legacy = [
            r[0] for r in store.conn.execute(
                "SELECT pi.gene_id FROM promoter_index pi JOIN genes g "
                "ON pi.gene_id = g.gene_id WHERE pi.tag_value = ? AND g.chromatin < ?",
                (t, int(ChromatinState.HETEROCHROMATIN)))
        ]
        assert got.get(t, []) == legacy, t


if __name__ == "__main__":
    if "--write" not in sys.argv:
        raise SystemExit("pass --write to (re)capture the golden on the BASE commit")
    import tempfile

    out = {}
    for cfg in CONFIGS:
        for q in QUERIES:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
                out[f"{cfg}/{q}"] = snapshot(Path(d), cfg, q)
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    print("wrote", GOLDEN)

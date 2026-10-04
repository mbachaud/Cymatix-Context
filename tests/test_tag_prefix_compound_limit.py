"""Gate: Tier-2 tag_prefix must survive queries with more than
SQLITE_LIMIT_COMPOUND_SELECT (500) distinct terms.

``_tag_prefix_sql`` emits one ``UNION ALL`` branch per query term; past
500 terms SQLite raised ``OperationalError: too many terms in compound
SELECT`` and the whole ``build_context`` call failed (6/1,401 BEIR
ArguAna queries, 1/200 RepoBench-R cff-easy — see
docs/benchmarks/2026-09-04-beta-witness-sweep.md). Present since
6c2aac6.

The fix must not move ordinary queries: at or under the limit the SQL
is byte-identical to the pre-fix builder (pinned below by an inline
copy of it), so tier output is unchanged by construction.
"""

from __future__ import annotations

from typing import List, Tuple

import pytest

from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.schemas import ChromatinState

from tests.conftest import make_gene

SQLITE_COMPOUND_LIMIT = 500


def _terms(n: int) -> List[str]:
    # Fixed-width so no term is a prefix of another: each term's range
    # matches only tags that start with exactly that term.
    return [f"zterm{i:05d}" for i in range(n)]


def _legacy_tag_prefix_sql(
    query_terms: List[str],
    party_filter: str = "",
    party_params: tuple = (),
    prefilter_clause: str = "",
    prefilter_params: tuple = (),
) -> Tuple[str, tuple]:
    """Verbatim copy of the pre-fix builder (master e3825e4b)."""
    sub = " UNION ALL ".join(
        "SELECT gene_id, tag_value FROM promoter_index "
        "WHERE tag_value >= ? AND tag_value < ?"
        for _ in query_terms
    )
    sql = (
        f"SELECT g.gene_id, p.match_count AS match_count "
        f"FROM (SELECT gene_id, COUNT(tag_value) AS match_count "
        f"      FROM ({sub}) GROUP BY gene_id) p "
        f"CROSS JOIN genes g ON g.gene_id = p.gene_id "
        f"WHERE g.chromatin < ? {party_filter} {prefilter_clause}"
    )
    bounds: List[str] = []
    for t in query_terms:
        lo = t.lower()
        hi = lo[:-1] + chr(ord(lo[-1]) + 1) if lo else "￿"
        bounds.extend((lo, hi))
    params = (
        *bounds,
        int(ChromatinState.HETEROCHROMATIN),
        *party_params,
        *prefilter_params,
    )
    return sql, params


def _seed(store: KnowledgeStore, terms: List[str]):
    """Genes whose tags hit terms at both ends of the list, so a chunked
    implementation that drops or double-counts a chunk is caught."""
    a = make_gene(
        content="a",
        domains=[terms[0] + "x", terms[1] + "y", terms[-1] + "z"],
    )
    b = make_gene(content="b", domains=[terms[-2], terms[-1]])
    c = make_gene(content="c", domains=["unrelated"])
    d = make_gene(
        content="d",
        domains=[terms[-1] + "q"],
        chromatin=ChromatinState.HETEROCHROMATIN,
    )
    for g in (a, b, c, d):
        store.upsert_doc(g)
    return a, b, c, d


@pytest.mark.parametrize("n_terms", [501, 1200])
def test_tag_prefix_sql_over_compound_limit_executes(tmp_path, n_terms):
    store = KnowledgeStore(str(tmp_path / "g.db"))
    terms = _terms(n_terms)
    a, b, c, d = _seed(store, terms)

    sql, params = store._tag_prefix_sql(terms)
    rows = {r["gene_id"]: r["match_count"] for r in store.conn.execute(sql, params)}

    assert rows == {a.gene_id: 3, b.gene_id: 2}


def test_query_docs_over_compound_limit_no_operational_error(tmp_path):
    store = KnowledgeStore(str(tmp_path / "g.db"))
    terms = _terms(SQLITE_COMPOUND_LIMIT + 101)
    a, b, c, d = _seed(store, terms)

    store.query_docs(domains=terms, entities=[], max_genes=8)
    contrib = store.last_tier_contributions

    assert contrib[a.gene_id]["tag_prefix"] == pytest.approx(3 * 1.5)
    assert contrib[b.gene_id]["tag_prefix"] == pytest.approx(2 * 1.5)
    assert "tag_prefix" not in contrib.get(c.gene_id, {})
    assert "tag_prefix" not in contrib.get(d.gene_id, {})


@pytest.mark.parametrize("n_terms", [1, 2, 37, SQLITE_COMPOUND_LIMIT])
def test_tag_prefix_sql_byte_identical_at_or_under_limit(tmp_path, n_terms):
    """Ordinary queries must not move: same SQL string, same params."""
    store = KnowledgeStore(str(tmp_path / "g.db"))
    terms = _terms(n_terms)
    args = (" AND g.party_id = ?", ("p1",), " AND g.gene_id IN (?)", ("x",))
    assert store._tag_prefix_sql(terms, *args) == _legacy_tag_prefix_sql(terms, *args)
    assert store._tag_prefix_sql(terms) == _legacy_tag_prefix_sql(terms)


def test_tag_prefix_over_limit_keeps_index_range_plan(tmp_path):
    """The chunked form must keep the covering-index range SEARCHes and
    never fall back to scanning genes (test_tag_prefix_drives_promoter_index
    pins the same plan for ordinary queries)."""
    store = KnowledgeStore(str(tmp_path / "g.db"))
    store.upsert_doc(make_gene(content="a", domains=["serverconfig"]))
    sql, params = store._tag_prefix_sql(_terms(SQLITE_COMPOUND_LIMIT + 1))
    plan = " | ".join(
        r[3] for r in store.conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
    )
    assert "SCAN g" not in plan and "SCAN genes" not in plan, plan
    assert "SCAN promoter_index" not in plan, plan
    assert "SEARCH promoter_index" in plan, plan

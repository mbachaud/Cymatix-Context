"""Tier 5 harmonic candidate query must be linear in the candidate pool.

``gene_id_a IN (<pool>) AND gene_id_b IN (<pool>)`` makes SQLite probe the
(a, b) primary key once per pair in the cross product of the two IN lists
(``SEARCH ... (gene_id_a=? AND gene_id_b=?)``): a 12,000-candidate pool took
~13 s on a 198-row table. The rows returned must stay identical (same pairs,
same order, same weights), only the cost changes.
"""
from __future__ import annotations

import random

from cymatix_context.genome import Genome

OLD_SQL = (
    "SELECT gene_id_a, gene_id_b, weight FROM harmonic_links "
    "WHERE gene_id_a IN ({ph}) AND gene_id_b IN ({ph})"
)


def _store(n_pool: int, n_links: int, seed: int = 3):
    g = Genome(path=":memory:")
    rng = random.Random(seed)
    pool = [f"g{i:08d}" for i in range(n_pool)]
    outside = [f"x{i:08d}" for i in range(50)]
    rows = set()
    while len(rows) < n_links:
        a = rng.choice(pool + outside)
        b = rng.choice(pool + outside)
        if a != b:
            rows.add((a, b))
    # Few distinct weights -> many ties in the downstream ranking.
    g.conn.executemany(
        "INSERT INTO harmonic_links (gene_id_a, gene_id_b, weight, updated_at, source) "
        "VALUES (?, ?, ?, 0.0, 'co_retrieved')",
        [(a, b, rng.choice([0.5, 1.0])) for a, b in sorted(rows)],
    )
    g.conn.commit()
    return g, pool


def _rows(g, pool):
    with g._harmonic_candidate_rows(g.conn.cursor(), pool) as rs:
        return [(r["gene_id_a"], r["gene_id_b"], r["weight"]) for r in rs]


def test_rows_identical_to_cross_product_query():
    g, pool = _store(400, 300)
    ph = ",".join("?" * len(pool))
    old = [
        tuple(r)
        for r in g.conn.execute(OLD_SQL.format(ph=ph), (*pool, *pool))
    ]
    new = _rows(g, pool)
    assert len(old) > 20  # fixture is non-trivial
    assert new == old  # same pairs, same order, same weights


def _vm_steps(g, pool):
    steps = [0]

    def tick():
        steps[0] += 1
        return 0

    g.conn.set_progress_handler(tick, 100)
    try:
        _rows(g, pool)
    finally:
        g.conn.set_progress_handler(None, 0)
    return steps[0]


def test_cost_is_linear_in_pool():
    g1, p1 = _store(2000, 200)
    g2, p2 = _store(4000, 200)
    s1, s2 = _vm_steps(g1, p1), _vm_steps(g2, p2)
    # Doubling the pool: linear ~2x, cross product ~4x.
    assert s2 < 3 * max(s1, 1), (s1, s2)

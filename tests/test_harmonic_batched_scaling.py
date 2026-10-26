"""#431/#454: the opt-in batched Tier 5 query must scale with the links it
returns, not with the square of the candidate pool.

``a IN (SELECT pool) AND b IN (SELECT pool)`` is planned by SQLite as a
(gene_id_a, gene_id_b) primary-key probe for every pair in pool x pool. On a
c580 bed (171k-candidate pool, 2.5M seeded links) that is ~15 min per query;
the same rows come back in ~5 s by joining the pool to harmonic_links on
gene_id_a and filtering gene_id_b by membership.

Work is measured in SQLite VM steps (progress handler), not wall time, so
the test is deterministic.
"""
from __future__ import annotations

from cymatix_context.genome import Genome

LINKS_PER_DOC = 4


def _store(n_docs: int) -> Genome:
    g = Genome(path=":memory:")
    ids = [f"g{i:05d}" for i in range(n_docs)]
    g.conn.executemany(
        "INSERT INTO harmonic_links (gene_id_a, gene_id_b, weight, updated_at, source) "
        "VALUES (?, ?, 1.0, 0.0, 'seeded')",
        [(ids[i], ids[(i + k) % n_docs]) for i in range(n_docs) for k in range(1, LINKS_PER_DOC + 1)],
    )
    g.conn.commit()
    return g


def _batched_rows_and_steps(g: Genome, pool: list[str]) -> tuple[set, int]:
    conn = g.conn
    steps = [0]

    def tick():
        steps[0] += 1
        return 0

    conn.set_progress_handler(tick, 100)
    try:
        with g._harmonic_candidate_rows(conn.cursor(), pool, batched=True) as rows:
            got = {(r["gene_id_a"], r["gene_id_b"]) for r in rows}
    finally:
        conn.set_progress_handler(None, 0)
    return got, steps[0]


def _expected(g: Genome, pool: list[str]) -> set:
    members = set(pool)
    return {(a, b) for a, b in g.conn.execute("SELECT gene_id_a, gene_id_b FROM harmonic_links")
            if a in members and b in members}


def test_batched_rows_match_pool_filter():
    g = _store(2000)
    try:
        pool = [f"g{i:05d}" for i in range(0, 2000, 3)] + ["not-a-doc"]
        got, _ = _batched_rows_and_steps(g, pool)
        assert got == _expected(g, pool)
        assert got  # links inside the pool exist
    finally:
        g.close()


def test_batched_work_is_not_quadratic_in_pool():
    g = _store(4000)
    try:
        small = [f"g{i:05d}" for i in range(0, 4000, 4)]   # 1000
        large = [f"g{i:05d}" for i in range(4000)]         # 4000
        _, steps_s = _batched_rows_and_steps(g, small)
        got_l, steps_l = _batched_rows_and_steps(g, large)
        assert got_l == _expected(g, large)
        ratio = steps_l / max(steps_s, 1)
        # Linear in pool + returned links: ~4x. Pairwise probing: ~16x.
        assert ratio < 8, f"4x pool cost {ratio:.1f}x the VM steps ({steps_s} -> {steps_l})"
    finally:
        g.close()

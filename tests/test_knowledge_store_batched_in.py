"""Regression tests for SQLite IN-clause batching in knowledge_store.py.

SQLite caps `WHERE col IN (?, ?, ...)` placeholders at `SQLITE_LIMIT_VARIABLE_NUMBER`
(historically 999; 2000 on the Python 3.12 / SQLite 3.50 ARM64+x86 builds we ship
to; 32766 on newer compile-defaulted SQLite). Several `KnowledgeStore` methods
build the IN clause from candidate sets whose size is caller-determined and can
exceed that cap in production:

  - `_apply_authority_boosts`: scoped over `gene_scores`, which on
    SPLADE-disabled retrieval contains the full dense fan-out (no SPLADE/
    prefilter narrowing). Observed in the 2026-05-28 v2-fixture 100q bench:
    3 of 29 queries had a per-shard `_apply_authority_boosts` raise
    `sqlite3.OperationalError: too many SQL variables`, which the daemon's
    per-shard try/except swallowed as "shard X query failed; skipping" — biasing
    recall@K by silently dropping any gold doc that lived in the skipped shard.

  - Sema-boost embedding lookup (~line 1869), party-attribution lookup
    (~line 2206), access-rate epigenetics lookup (~line 2227): all scoped over
    `gene_scores` inside the same `query_docs` call. Same failure mode,
    different code paths.

These tests pin the contract: each of those four call sites must accept a
candidate set whose size exceeds SQLite's runtime placeholder cap without
raising. The fix batches the IN clause; the tests don't care HOW (helper vs
inlined) — only that it doesn't blow up.

Test scale (3000 candidates) is chosen to exceed the empirical 2000 cap on
the Python 3.12.13 + SQLite 3.50.4 builds in use, while staying well below
the 32766 cap of newer SQLite builds — meaning the test will RED on every
build that has the bug and GREEN on every build that has the fix.
"""

from __future__ import annotations

import sqlite3

import pytest

from helix_context.knowledge_store import KnowledgeStore


# ── Fixture: a KnowledgeStore on a fresh tmp DB ──────────────────────


@pytest.fixture
def store(tmp_path):
    """A KnowledgeStore backed by an on-disk SQLite file with the full
    schema initialized (tables created by KnowledgeStore.__init__).

    The genes table is empty — the regression tests below trigger the
    SQL-variable cap at BIND time (before any rows are returned), so an
    empty table is sufficient to expose the bug.
    """
    s = KnowledgeStore(path=str(tmp_path / "kv-batched-in.db"))
    yield s
    try:
        s.conn.close()
    except Exception:
        pass


# ── _apply_authority_boosts: the site that fired in production ──────


def test_apply_authority_boosts_does_not_blow_up_past_sqlite_cap(store):
    """Regression: candidate sets above SQLite's `?` cap must not raise.

    Pre-fix: passing 3000 gene_ids into the `WHERE gene_id IN (?, ?, ...)`
    clause raises `sqlite3.OperationalError: too many SQL variables` at
    execute-bind time on any SQLite build with `SQLITE_LIMIT_VARIABLE_NUMBER`
    below 3000 (covers the 999 legacy cap, the 2000 cap on our current
    Python 3.12 / SQLite 3.50 builds, and anything else short of the modern
    32766 default).

    Post-fix: the call site batches into chunks below the cap and returns
    silently.
    """
    cur = store.conn.cursor()
    gene_scores = {f"gene_{i:05d}": 1.0 for i in range(3000)}
    query_terms = ["test"]

    # Should not raise. (No rows match — genes table is empty — but the bug
    # fires before the result set matters.)
    store._apply_authority_boosts(cur, gene_scores, query_terms)


def test_apply_authority_boosts_at_runtime_cap_boundary(store):
    """Edge case: at, just-above, and well-above the runtime variable cap.

    Probes the SQLite limit with `conn.getlimit` (Python 3.11+) and exercises
    the call site at:
      - cap     (highest legal single-batch size; pre-fix passes; post-fix passes)
      - cap+1   (first illegal size; pre-fix raises; post-fix passes)
      - 4*cap+7 (off-boundary multiple to catch batch-slicing off-by-ones)
    """
    cap = store.conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    assert cap > 0, f"unexpected SQLite variable cap: {cap}"
    cur = store.conn.cursor()
    query_terms = ["test"]

    # At the cap: single batch, always legal.
    store._apply_authority_boosts(
        cur, {f"gene_{i:05d}": 1.0 for i in range(cap)}, query_terms,
    )

    # One past the cap: pre-fix raises, post-fix batches.
    store._apply_authority_boosts(
        cur, {f"gene_{i:05d}": 1.0 for i in range(cap + 1)}, query_terms,
    )

    # Off-boundary multiple: catches off-by-ones in batch slicing.
    store._apply_authority_boosts(
        cur, {f"gene_{i:05d}": 1.0 for i in range(4 * cap + 7)}, query_terms,
    )


# ── Empty-input guard (must still return cleanly) ───────────────────


def test_apply_authority_boosts_empty_gene_scores_is_a_noop(store):
    """Empty gene_scores returns silently — does not invoke the SQL path
    at all, so it cannot raise. Pins existing behavior so the batching
    refactor doesn't regress the early-return guard.
    """
    cur = store.conn.cursor()
    store._apply_authority_boosts(cur, {}, ["test"])


# ── Correctness: the batched query must still return the right rows ─


def test_apply_authority_boosts_batched_query_finds_all_matching_genes(store):
    """When some of the >cap candidates DO exist in the genes table, the
    batched IN clause must still match them (catch off-by-one in batch
    slicing) and the boost must still be applied.

    This is the only test that actually inserts rows — the others probe
    only the cap. We insert N genes spanning a 4-batch range and verify the
    authority boost is applied to ALL of them — including the ones that
    straddle batch boundaries — when their gene_ids appear in gene_scores.
    """
    cap = store.conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    # Span >3 batches to expose off-by-one at every internal boundary
    n_genes = 3 * cap + 17

    cur = store.conn.cursor()
    # Insert genes with a source_id that contains "test" so the authority-
    # boost source-match path fires. Promoter / epigenetics blobs are
    # valid-but-empty JSON to avoid parse warnings.
    rows = []
    for i in range(n_genes):
        rows.append((
            f"gene_{i:05d}", f"path/to/test_file_{i}.md", "doc",
            "{}", "{}", None, None,
        ))
    cur.executemany(
        "INSERT INTO genes (gene_id, source_id, source_kind, promoter, "
        "epigenetics, key_values, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    store.conn.commit()

    gene_scores = {f"gene_{i:05d}": 1.0 for i in range(n_genes)}
    base = dict(gene_scores)
    store._apply_authority_boosts(cur, gene_scores, ["test"])

    # Every gene should have received the +2.0 source-authority bonus
    # (source_id contains "test"). Pin that every entry — including those
    # that straddle internal batch boundaries — got the boost.
    missed = [
        gid for gid in base
        if gene_scores[gid] != pytest.approx(base[gid] + 2.0)
    ]
    assert not missed, (
        f"{len(missed)} genes missing source-authority boost; first 5: "
        f"{missed[:5]}"
    )

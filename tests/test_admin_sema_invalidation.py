"""Admin swap/refresh ops must invalidate BOTH ΣĒMA caches (#339 rider).

``routes_admin.py`` historically called ``invalidate_sema_cache()`` (hot
tier) at three sites — ``/admin/sema/rebuild``, ``/admin/reload``, and
``/admin/swap-db`` — but never ``invalidate_cold_sema_cache()``. The hot
and cold builds scan disjoint chromatin partitions (``< HETEROCHROMATIN``
vs ``= HETEROCHROMATIN``), and the cold cache is lazily built by
``query_cold_tier`` and held until explicitly invalidated, so stale
cold-tier vectors survived admin swap/refresh until process restart.

Fix (#339 rider):
  - ``/admin/sema/rebuild`` and ``/admin/reload`` invalidate the cold
    cache (and its ``_cold_sema_vectorless`` gate) beside the hot one.
  - ``/admin/swap-db`` deliberately does NOT: the swapped-in store is
    freshly constructed by ``open_read_source()``, so its cold cache is
    unbuilt — there is no stale cold state to drop, and the old store's
    caches die with ``old_store.close()``. The test here pins that
    fresh-store invariant so the skip stays provably safe.
"""

from __future__ import annotations

from cymatix_context.config import GenomeConfig
from cymatix_context.knowledge_store import KnowledgeStore

from tests.conftest import make_client, make_cymatix_config


# -- Helpers ---------------------------------------------------------------


_STALE_COLD_CACHE = {"gene_ids": ["stale-cold-gene"], "matrix": None}


def _make_app_and_client(genome_path: str):
    """FastAPI app + TestClient over a file-backed store at *genome_path*.

    File-backed (not :memory:) so ``/admin/reload``'s ``genome.refresh()``
    exercises the real WAL snapshot-reopen path.
    """
    config = make_cymatix_config(
        genome=GenomeConfig(path=genome_path, cold_start_threshold=5),
    )
    client = make_client(config=config)
    return client.app, client


def _poison_cold_cache(genome) -> None:
    """Plant a stale cold cache + a stale vectorless verdict.

    Both fields matter: ``_cold_sema_cache`` holds the stale vectors, and
    ``_cold_sema_vectorless=True`` would permanently gate the next rebuild
    in ``query_cold_tier`` if the invalidation forgot to clear it.
    """
    genome._cold_sema_cache = dict(_STALE_COLD_CACHE)
    genome._cold_sema_vectorless = True


def _assert_cold_cache_dropped(genome, endpoint: str) -> None:
    assert genome._cold_sema_cache is None, (
        f"#339 rider: {endpoint} must invalidate the cold-tier ΣĒMA cache "
        f"(stale cache survived)"
    )
    assert genome._cold_sema_vectorless is False, (
        f"#339 rider: {endpoint} must clear _cold_sema_vectorless so the "
        f"next query_cold_tier re-checks instead of staying gated"
    )


# -- /admin/sema/rebuild ---------------------------------------------------


def test_sema_rebuild_invalidates_both_caches(tmp_path):
    """A forced rebuild drops the cold cache, not just the hot one."""
    db_path = str(tmp_path / "rebuild.db")
    KnowledgeStore(path=db_path).close()

    app, client = _make_app_and_client(db_path)
    genome = app.state.cymatix.genome
    _poison_cold_cache(genome)
    # Poison the hot vectorless gate too, so the same request also pins
    # the pre-existing hot-tier invalidation behavior.
    genome._sema_vectorless = True

    resp = client.post("/admin/sema/rebuild")
    assert resp.status_code == 200
    assert resp.json()["rebuilt"] is True

    _assert_cold_cache_dropped(genome, "/admin/sema/rebuild")
    # Hot tier: an empty store rebuilds to a vectorless verdict — the
    # invalidation ran (flag was cleared, then the rebuild re-derived it).
    assert genome._sema_cache is None


# -- /admin/reload ---------------------------------------------------------


def test_admin_reload_invalidates_both_caches(tmp_path):
    """Reload refreshes the WAL snapshot — cold vectors may have changed
    out-of-process, so the cold cache must drop alongside the hot one."""
    db_path = str(tmp_path / "reload.db")
    KnowledgeStore(path=db_path).close()

    app, client = _make_app_and_client(db_path)
    genome = app.state.cymatix.genome
    _poison_cold_cache(genome)

    resp = client.post("/admin/reload")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reloaded"] is True
    # The sema step must not have errored out before the invalidation.
    assert "sema_error" not in body["changes"], body["changes"]

    _assert_cold_cache_dropped(genome, "/admin/reload")


# -- /admin/swap-db --------------------------------------------------------


def test_swap_db_new_store_has_clean_cold_state(tmp_path):
    """Pins the invariant the swap-db site's deliberate skip relies on.

    ``/admin/swap-db`` does not call ``invalidate_cold_sema_cache()``
    because the swapped-in store is freshly constructed — this test fails
    if that ever stops being true (e.g. ``open_read_source`` starts
    returning pooled/reused stores with prebuilt cold state).
    """
    db_a = str(tmp_path / "swap_a.db")
    db_b = str(tmp_path / "swap_b.db")
    KnowledgeStore(path=db_a).close()
    KnowledgeStore(path=db_b).close()

    app, client = _make_app_and_client(db_a)
    # Stale cold state on the OLD store must not leak across the swap.
    _poison_cold_cache(app.state.cymatix.genome)

    resp = client.post("/admin/swap-db", json={"path": db_b})
    assert resp.status_code == 200
    assert resp.json()["swapped"] is True

    genome = app.state.cymatix.genome
    assert genome.path == db_b
    _assert_cold_cache_dropped(genome, "/admin/swap-db (fresh store)")

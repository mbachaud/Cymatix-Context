"""End-to-end smoke: real HelixSession against an :memory: genome.

These tests are slow-ish (cold-start the manager once) but catch
wiring bugs the per-subcommand unit tests can't.
"""
from __future__ import annotations

import json
import os

import pytest

from tests.conftest import run_cli as _run


@pytest.fixture(autouse=True)
def _isolate_genome(monkeypatch, tmp_path):
    """Force the integration test to use an in-memory genome with CPU backend.

    Without this, the cold-start CLI would try to open genome.db from
    cwd and either fail or pollute the developer's working genome.

    The CPU backend (spaCy + regex, no LLM calls) is used so that
    ingest works without a running Ollama instance. The ribosome is
    intentionally disabled in this project (LLM-free retrieval is a
    design pillar), so we must configure backend="cpu" explicitly.
    """
    monkeypatch.setenv("HELIX_GENOME_PATH", ":memory:")
    monkeypatch.setenv("HELIX_CONFIG", str(tmp_path / "no-such-file.toml"))

    # Reset and pre-warm the cached module-level manager with a CPU-backend
    # config so all CLI commands that call open_session() get a manager that
    # can actually ingest (ribosome is disabled; cpu tagger is the only path).
    from helix_context import api
    from helix_context.config import HelixConfig, IngestionConfig
    api.close_manager()
    cfg = HelixConfig()
    cfg.genome.path = ":memory:"
    cfg.ingestion = IngestionConfig(backend="cpu")
    # Prime the module-level manager directly so open_session() reuses it.
    from helix_context.context_manager import HelixContextManager
    api._DEFAULT_MANAGER = HelixContextManager(config=cfg)

    yield

    api.close_manager()


@pytest.mark.live
def test_end_to_end_ingest_then_query():
    """Ingest a small fact, query for it, verify the bytes round-trip."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("The flux capacitor requires 1.21 gigawatts of electricity.\n")
        fname = f.name

    try:
        rc, out, err = _run(["ingest", fname, "--json"])
        assert rc == 0, err
        ingest_payload = json.loads(out)
        assert ingest_payload["ok"] is True
        assert ingest_payload["files_processed"] == 1

        rc, out, err = _run(["query", "flux capacitor", "--json"])
        assert rc == 0, err
        query_payload = json.loads(out)
        # Don't over-assert on retrieval quality — the pipeline can route
        # to "miss" with a small genome. Just verify the wire format.
        assert "verdict" in query_payload
        assert "expressed_context" in query_payload
        assert isinstance(query_payload["evidence"], list)
    finally:
        os.unlink(fname)


@pytest.mark.live
def test_end_to_end_cold_start_constructs_manager_lazily(monkeypatch, tmp_path):
    """Regression for review #11: the integration suite was pre-warming
    ``api._DEFAULT_MANAGER`` in its autouse fixture, so the cold-start
    branch in ``open_session()`` (``if _DEFAULT_MANAGER is None: ...
    HelixContextManager(config=cfg)``) was never exercised.

    This test explicitly clears the cached manager AFTER the autouse
    fixture has run, points the construction at an in-memory genome via
    a monkeypatched ``HelixConfig`` factory, and verifies that ingest +
    query both succeed and return well-shaped JSON — proving the lazy
    construction path works end-to-end.
    """
    from helix_context import api
    from helix_context.config import HelixConfig, IngestionConfig

    # 1. Wipe the manager the autouse fixture pre-warmed. This is the
    #    whole point: we want open_session() to hit `_DEFAULT_MANAGER is None`.
    api._DEFAULT_MANAGER = None

    # 2. Monkeypatch the HelixConfig used inside api.open_session so that
    #    when it calls `HelixConfig()` (no args), we get a CPU/:memory:
    #    config instead of the disk-backed default. open_session() does
    #    NOT call load_config — it constructs HelixConfig() directly —
    #    so an env-var-only approach won't redirect the genome path.
    def _cpu_memory_config():
        cfg = HelixConfig()
        cfg.genome.path = ":memory:"
        cfg.ingestion = IngestionConfig(backend="cpu")
        return cfg

    monkeypatch.setattr(api, "HelixConfig", _cpu_memory_config)

    # Track whether the cold-start branch actually fires by wrapping
    # HelixContextManager construction. This makes the regression
    # explicit: if a future change re-introduces pre-warming, this
    # assertion will catch it.
    from helix_context import context_manager as cm_mod
    construction_count = {"n": 0}
    real_ctor = cm_mod.HelixContextManager

    def _counting_ctor(*args, **kwargs):
        construction_count["n"] += 1
        return real_ctor(*args, **kwargs)

    monkeypatch.setattr(cm_mod, "HelixContextManager", _counting_ctor)

    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("Cold-start regression: the manager must be constructed lazily.\n")
        fname = f.name

    try:
        # 3. Run ingest — this triggers open_session() → cold-start branch.
        rc, out, err = _run(["ingest", fname, "--json"])
        assert rc == 0, err
        ingest_payload = json.loads(out)
        assert ingest_payload["ok"] is True
        assert ingest_payload["files_processed"] == 1

        # 4. Run query — should reuse the now-cached manager (no second ctor).
        rc, out, err = _run(["query", "cold-start", "--json"])
        assert rc == 0, err
        query_payload = json.loads(out)
        assert "verdict" in query_payload
        assert "expressed_context" in query_payload
        assert isinstance(query_payload["evidence"], list)

        # 5. The cold-start branch fired exactly once (on first ingest);
        #    the query reused the cached manager.
        assert construction_count["n"] == 1, (
            f"expected lazy ctor to fire once, got {construction_count['n']}"
        )
    finally:
        os.unlink(fname)
        # Finalizer: keep test isolation by resetting the cached manager.
        api.close_manager()


@pytest.mark.live
def test_end_to_end_diag_corpus_returns_well_shaped_payload():
    """`helix diag corpus --json` returns a well-shaped JSON payload
    against a fresh :memory: genome. Verifies pipeline wiring, not
    retrieval quality — total_genes may legitimately be 0."""
    rc, out, err = _run(["diag", "corpus", "--json"])
    assert rc == 0, err
    payload = json.loads(out)
    assert isinstance(payload["total_genes"], int)
    assert payload["total_genes"] >= 0
    # Wire-format smoke: tier_distribution + compression_ratio must be present.
    assert "tier_distribution" in payload
    assert "compression_ratio" in payload

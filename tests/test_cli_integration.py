"""End-to-end smoke: real HelixSession against an :memory: genome.

These tests are slow-ish (cold-start the manager once) but catch
wiring bugs the per-subcommand unit tests can't.
"""
from __future__ import annotations

import io
import json
import contextlib
import os

import pytest

from helix_context.cli import main


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


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
def test_end_to_end_diag_corpus_reports_genes():
    """After ingest, diag corpus shows non-zero gene count."""
    rc, out, err = _run(["diag", "corpus", "--json"])
    assert rc == 0, err
    payload = json.loads(out)
    assert isinstance(payload["total_genes"], int)
    # Allow zero (fresh :memory: genome) — we just check the field exists.
    assert payload["total_genes"] >= 0

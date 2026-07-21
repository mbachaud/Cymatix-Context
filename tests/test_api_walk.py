"""Tests for the walk-aware methods on ``cymatix_context.api.HelixSession``.

These methods (``gene_get``, ``packet``, ``refresh_targets``,
``neighbors``) were added 2026-05-12 to back the CLI agent surface. The
council review of the v1 CLI flagged that the in-process API
deliberately deferred them to v1.1; making them v1 unblocks `helix
packet`, `helix gene get`, etc. without standing up an HTTP server.

The tests stub the underlying ``HelixContextManager`` (and the
``build_context_packet`` helper that the packet methods delegate to) so
they verify the API boundary in isolation from the retrieval stack.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cymatix_context.api import HelixSession
from cymatix_context.schemas import (
    ChromatinState,
    ContextItem,
    ContextPacket,
    Gene,
    PromoterTags,
    RefreshTarget,
)


def _make_session(manager=None):
    """Build a HelixSession bound to a MagicMock manager — bypasses
    open_session() so we don't accidentally spin up a real
    HelixContextManager (and a real SQLite genome)."""
    manager = manager or MagicMock()
    return HelixSession(manager=manager, session_id="sess-test-walk")


def _make_gene(gene_id="gene-001", content="splice trims fragments"):
    return Gene(
        gene_id=gene_id,
        content=content,
        complement=content[:40],
        codons=["splice"],
        promoter=PromoterTags(metadata={"path": "cymatix_context/splice.py"}),
        chromatin=ChromatinState.OPEN,
    )


# ── gene_get ─────────────────────────────────────────────────────────


def test_gene_get_delegates_to_genome():
    mgr = MagicMock()
    # R3 Stage C renamed KnowledgeStore.get_gene -> get_doc with a legacy
    # alias. api.py:gene_get calls the canonical name, so configure that.
    mgr.genome.get_doc.return_value = _make_gene()
    sess = _make_session(mgr)
    gene = sess.gene_get("gene-001")
    assert gene.gene_id == "gene-001"
    mgr.genome.get_doc.assert_called_once_with("gene-001")


def test_gene_get_returns_none_on_unknown_id():
    mgr = MagicMock()
    mgr.genome.get_doc.return_value = None
    sess = _make_session(mgr)
    assert sess.gene_get("nope") is None


# ── packet ───────────────────────────────────────────────────────────


def test_packet_calls_builder_with_genome_and_read_only_true():
    mgr = MagicMock()
    expected = ContextPacket(
        task_type="explain",
        query="splice",
        verified=[],
        stale_risk=[],
        refresh_targets=[],
    )
    with patch(
        "cymatix_context.context_packet.build_context_packet",
        return_value=expected,
    ) as builder:
        sess = _make_session(mgr)
        out = sess.packet("splice", task_type="explain", max_genes=4)
    assert out is expected
    _, kwargs = builder.call_args
    assert kwargs["genome"] is mgr.genome
    assert kwargs["task_type"] == "explain"
    assert kwargs["max_genes"] == 4
    assert kwargs["read_only"] is True
    assert kwargs["include_raw"] is False


def test_packet_include_raw_passes_through():
    mgr = MagicMock()
    with patch(
        "cymatix_context.context_packet.build_context_packet",
        return_value=ContextPacket(task_type="edit", query="x"),
    ) as builder:
        sess = _make_session(mgr)
        sess.packet("x", task_type="edit", include_raw=True)
    _, kwargs = builder.call_args
    assert kwargs["include_raw"] is True


# ── refresh_targets ──────────────────────────────────────────────────


def test_refresh_targets_returns_only_the_reread_plan():
    mgr = MagicMock()
    packet = ContextPacket(
        task_type="edit",
        query="splice",
        verified=[ContextItem(title="t", content="c")],  # would be in packet, not in refresh
        stale_risk=[],
        refresh_targets=[
            RefreshTarget(
                target_kind="file", source_id="a.py", reason="stale", priority=0.5,
            ),
            RefreshTarget(
                target_kind="file", source_id="b.py", reason="cold", priority=0.2,
            ),
        ],
    )
    with patch(
        "cymatix_context.context_packet.build_context_packet",
        return_value=packet,
    ):
        sess = _make_session(mgr)
        targets = sess.refresh_targets("splice", task_type="edit")
    assert [t.source_id for t in targets] == ["a.py", "b.py"]


def test_refresh_targets_default_task_type_is_edit():
    """High-risk default — the CLI subcommand mirrors this."""
    mgr = MagicMock()
    with patch(
        "cymatix_context.context_packet.build_context_packet",
        return_value=ContextPacket(task_type="edit", query="x"),
    ) as builder:
        sess = _make_session(mgr)
        sess.refresh_targets("x")
    _, kwargs = builder.call_args
    assert kwargs["task_type"] == "edit"


# ── neighbors ────────────────────────────────────────────────────────


def test_neighbors_empty_when_codec_missing():
    """If the SEMA codec didn't initialize (e.g. `embeddings` extra not
    installed), we return [] rather than raise — the CLI surface relies
    on this so a fresh install doesn't crash on `helix neighbors`."""
    mgr = MagicMock()
    mgr._sema_codec = None
    sess = _make_session(mgr)
    assert sess.neighbors("test") == []


def test_neighbors_empty_when_no_embeddings_in_genome():
    mgr = MagicMock()
    mgr._sema_codec = MagicMock()
    mgr.genome.read_conn.execute.return_value.fetchall.return_value = []
    sess = _make_session(mgr)
    assert sess.neighbors("test", k=5) == []


def test_neighbors_sorts_by_similarity_desc():
    """The cosine-similarity tuple is sorted descending; top-k truncates."""
    mgr = MagicMock()
    codec = MagicMock()
    codec.encode.return_value = [1.0, 0.0]
    # Three genes — manager's similarity returns a different score per call.
    codec.similarity.side_effect = [0.3, 0.9, 0.6]
    mgr._sema_codec = codec

    # sqlite rows are dict-indexable; emulate with a list of dicts.
    class Row(dict):
        def __getitem__(self, k):
            return super().__getitem__(k)

    rows = [
        Row(gene_id="gene-A", embedding="[1.0, 0.1]"),
        Row(gene_id="gene-B", embedding="[1.0, 0.2]"),
        Row(gene_id="gene-C", embedding="[1.0, 0.3]"),
    ]
    mgr.genome.read_conn.execute.return_value.fetchall.return_value = rows

    def _get_gene(gid):
        return _make_gene(gene_id=gid, content=f"content of {gid}")

    mgr.genome.get_doc.side_effect = _get_gene  # R3 Stage C canonical

    sess = _make_session(mgr)
    out = sess.neighbors("query", k=2)
    assert [n["gene_id"] for n in out] == ["gene-B", "gene-C"]
    assert out[0]["sema_cos_sim"] == pytest.approx(0.9)
    assert out[0]["path"] == "cymatix_context/splice.py"


def test_open_session_honors_helix_config_env(monkeypatch, tmp_path):
    """`open_session()` must route through ``load_config()`` so that
    ``HELIX_CONFIG`` and ``HELIX_GENOME_PATH`` are honored exactly like
    ``helix status`` already honors them. Pre-fix this used the
    ``HelixConfig()`` default and silently fell back to ``./genome.db``
    regardless of operator config — making ``helix status`` and
    ``helix query`` look at different genomes."""
    import cymatix_context.api as api

    # Reset the cached manager so the test exercises the cold-start path.
    api._DEFAULT_MANAGER = None

    cfg_path = tmp_path / "helix.toml"
    cfg_path.write_text(
        "[genome]\npath = \"" + str(tmp_path / "configured.db").replace("\\", "/") + "\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HELIX_CONFIG", str(cfg_path))
    # The test environment sets HELIX_GENOME_PATH=:memory: to keep tests
    # off-disk; that env var wins over helix.toml at load_config time
    # (see config.py:611). Delete it for this test so we exercise the
    # TOML-discovery path. Real operators usually don't set both.
    monkeypatch.delenv("HELIX_GENOME_PATH", raising=False)

    seen: dict = {}

    class _FakeMgr:
        def __init__(self, config):
            seen["config"] = config

    monkeypatch.setattr(
        "cymatix_context.context_manager.HelixContextManager", _FakeMgr,
    )

    api.open_session()
    assert seen["config"].genome.path.endswith("configured.db"), (
        "open_session() did not propagate HELIX_CONFIG → load_config(); got: "
        f"{seen['config'].genome.path!r}"
    )

    # Cleanup so subsequent tests start cold.
    api._DEFAULT_MANAGER = None


def test_neighbors_skips_malformed_embedding_row():
    """A row whose embedding column is non-JSON gets logged + skipped;
    the rest of the result still comes back."""
    mgr = MagicMock()
    codec = MagicMock()
    codec.encode.return_value = [1.0]
    codec.similarity.return_value = 0.5
    mgr._sema_codec = codec

    class Row(dict):
        def __getitem__(self, k):
            return super().__getitem__(k)

    rows = [
        Row(gene_id="gene-A", embedding="this is not json"),
        Row(gene_id="gene-B", embedding="[1.0, 0.0]"),
    ]
    mgr.genome.read_conn.execute.return_value.fetchall.return_value = rows
    mgr.genome.get_doc.side_effect = lambda gid: _make_gene(gene_id=gid)  # R3 Stage C canonical

    sess = _make_session(mgr)
    out = sess.neighbors("q", k=10)
    assert [n["gene_id"] for n in out] == ["gene-B"]

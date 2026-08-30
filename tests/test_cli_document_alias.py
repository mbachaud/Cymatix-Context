"""Tests for the `cymatix document` alias of `cymatix gene`, and the
`--max-docs` alias of `--max-genes` on `packet` / `refresh-targets`.

Tier 2 rosetta work: `document` is the canonical spelling going forward;
`gene` must keep working identically (additive only). Parser-level +
mocked-session tests only — no server, no genome on disk.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cymatix_context.cli import dispatcher
from cymatix_context.schemas import (
    ChromatinState,
    ContextItem,
    ContextPacket,
    Gene,
    PromoterTags,
    RefreshTarget,
)
from tests.conftest import run_cli as _run


def _parse(argv: list[str]):
    """Thin helper over the dispatcher's real top-level parser: parse only
    the first token (subcommand name), exactly as ``dispatcher.main`` does."""
    return dispatcher._build_parser().parse_args(argv[:1])


def _handler(ns):
    """Thin helper over the dispatcher's real resolution step."""
    return dispatcher._resolve(ns.subcommand)


# ── same handler, both spellings ────────────────────────────────────────


def test_document_get_resolves_to_gene_handler():
    ns_legacy = _parse(["gene", "get", "abc123"])
    ns_alias = _parse(["document", "get", "abc123"])
    assert _handler(ns_alias) is _handler(ns_legacy)


def test_document_resolver_is_cmd_gene_run():
    from cymatix_context.cli import cmd_gene

    assert dispatcher._resolve("document") is cmd_gene.run


def test_gene_resolver_unchanged():
    from cymatix_context.cli import cmd_gene

    assert dispatcher._resolve("gene") is cmd_gene.run


# ── `document` is a first-class command in --help ──────────────────────


def test_document_listed_in_help_output():
    from cymatix_context.cli import main
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
    assert exc.value.code == 0
    assert "document" in out.getvalue()


# ── full dispatch parity: `document get|preview` behaves like `gene` ───


def _make_gene(gene_id="gene-001", content="The splice step trims low-value fragments. " * 20):
    return Gene(
        gene_id=gene_id,
        content=content,
        complement=content[:80],
        codons=["splice", "fragment"],
        promoter=PromoterTags(
            domains=["retrieval"],
            entities=["splice"],
            intent="explain",
            summary="splice step",
            metadata={"path": "cymatix_context/splice.py"},
        ),
        chromatin=ChromatinState.OPEN,
    )


@pytest.fixture
def fake_gene_session():
    sess = MagicMock()
    sess.gene_get.return_value = _make_gene()
    return sess


def test_document_get_matches_gene_get_payload(fake_gene_session):
    with patch("cymatix_context.cli.cmd_gene.open_session", return_value=fake_gene_session):
        rc_legacy, out_legacy, err_legacy = _run(["gene", "get", "gene-001", "--json"])
        rc_alias, out_alias, err_alias = _run(["document", "get", "gene-001", "--json"])
    assert rc_legacy == 0, err_legacy
    assert rc_alias == 0, err_alias
    assert out_alias == out_legacy


def test_document_preview_matches_gene_preview_payload(fake_gene_session):
    with patch("cymatix_context.cli.cmd_gene.open_session", return_value=fake_gene_session):
        rc_legacy, out_legacy, err_legacy = _run(
            ["gene", "preview", "gene-001", "--chars", "40", "--json"]
        )
        rc_alias, out_alias, err_alias = _run(
            ["document", "preview", "gene-001", "--chars", "40", "--json"]
        )
    assert rc_legacy == 0, err_legacy
    assert rc_alias == 0, err_alias
    assert out_alias == out_legacy


def test_gene_still_works_after_document_alias_added(fake_gene_session):
    """Regression: `gene` must keep working identically (additive only)."""
    with patch("cymatix_context.cli.cmd_gene.open_session", return_value=fake_gene_session):
        rc, out, err = _run(["gene", "get", "gene-001", "--json"])
    assert rc == 0, err
    assert "gene-001" in out


# ── --max-docs flag alias, shares dest with --max-genes ────────────────


@pytest.fixture
def fake_packet_session():
    sess = MagicMock()
    sess.packet.return_value = ContextPacket(
        task_type="explain",
        query="q",
        verified=[
            ContextItem(
                kind="gene",
                gene_id="gene-001",
                title="t",
                content="c",
                relevance_score=0.5,
                live_truth_score=0.5,
                status="verified",
            ),
        ],
        stale_risk=[],
        refresh_targets=[],
        notes=[],
        coordinate_confidence=0.9,
        file_coverage=0.9,
    )
    return sess


def test_max_docs_flag_alias_on_packet(fake_packet_session):
    with patch("cymatix_context.cli.cmd_packet.open_session", return_value=fake_packet_session):
        rc, _, err = _run(["packet", "q", "--max-docs", "7"])
    assert rc == 0, err
    _, kwargs = fake_packet_session.packet.call_args
    assert kwargs["max_genes"] == 7


def test_max_genes_and_max_docs_share_dest_on_packet(fake_packet_session):
    """Both spellings must populate the same argparse dest (`max_genes`)."""
    with patch("cymatix_context.cli.cmd_packet.open_session", return_value=fake_packet_session):
        _run(["packet", "q", "--max-genes", "9"])
        _, kwargs_legacy = fake_packet_session.packet.call_args
        _run(["packet", "q", "--max-docs", "9"])
        _, kwargs_alias = fake_packet_session.packet.call_args
    assert kwargs_legacy["max_genes"] == kwargs_alias["max_genes"] == 9


@pytest.fixture
def fake_refresh_session():
    sess = MagicMock()
    sess.refresh_targets.return_value = [
        RefreshTarget(
            target_kind="file",
            source_id="cymatix_context/splice.py",
            reason="stale",
            priority=0.7,
        ),
    ]
    return sess


def test_max_docs_flag_alias_on_refresh_targets(fake_refresh_session):
    with patch(
        "cymatix_context.cli.cmd_refresh_targets.open_session",
        return_value=fake_refresh_session,
    ):
        rc, _, err = _run(["refresh-targets", "q", "--max-docs", "5"])
    assert rc == 0, err
    _, kwargs = fake_refresh_session.refresh_targets.call_args
    assert kwargs["max_genes"] == 5


def test_max_genes_still_works_on_refresh_targets(fake_refresh_session):
    """Regression: legacy flag spelling keeps working identically."""
    with patch(
        "cymatix_context.cli.cmd_refresh_targets.open_session",
        return_value=fake_refresh_session,
    ):
        rc, _, err = _run(["refresh-targets", "q", "--max-genes", "5"])
    assert rc == 0, err
    _, kwargs = fake_refresh_session.refresh_targets.call_args
    assert kwargs["max_genes"] == 5

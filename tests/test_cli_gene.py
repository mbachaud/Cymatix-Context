"""Tests for `helix gene get` and `helix gene preview`."""
from __future__ import annotations

import contextlib
import io
import json
from unittest.mock import MagicMock, patch

import pytest

from cymatix_context.cli import main
from cymatix_context.schemas import (
    ChromatinState,
    Gene,
    PromoterTags,
)
from tests.conftest import run_cli as _run


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
def fake_session():
    sess = MagicMock()
    sess.gene_get.return_value = _make_gene()
    return sess


def test_gene_get_json_dumps_full_model(fake_session):
    with patch("cymatix_context.cli.cmd_gene.open_session", return_value=fake_session):
        rc, out, err = _run(["gene", "get", "gene-001", "--json"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["gene_id"] == "gene-001"
    assert "content" in payload
    assert payload["promoter"]["domains"] == ["retrieval"]


def test_gene_preview_truncates_to_chars(fake_session):
    with patch("cymatix_context.cli.cmd_gene.open_session", return_value=fake_session):
        rc, out, err = _run(["gene", "preview", "gene-001", "--chars", "40", "--json"])
    assert rc == 0, err
    payload = json.loads(out)
    assert len(payload["preview"]) == 40
    assert payload["truncated"] is True
    assert payload["path"] == "cymatix_context/splice.py"


def test_gene_get_unknown_id_returns_one():
    sess = MagicMock()
    sess.gene_get.return_value = None
    with patch("cymatix_context.cli.cmd_gene.open_session", return_value=sess):
        rc, out, err = _run(["gene", "get", "gene-nope", "--json"])
    assert rc == 1
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "gene-nope" in payload["error"]


def test_gene_requires_action():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        with pytest.raises(SystemExit) as exc:
            main(["gene"])
    assert exc.value.code == 2


def test_gene_preview_text_mode_shows_truncation_marker(fake_session):
    with patch("cymatix_context.cli.cmd_gene.open_session", return_value=fake_session):
        rc, out, err = _run(["gene", "preview", "gene-001", "--chars", "20"])
    assert rc == 0, err
    assert "truncated" in out

"""Tests for `helix neighbors`."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import run_cli as _run


@pytest.fixture
def fake_session():
    sess = MagicMock()
    sess.neighbors.return_value = [
        {
            "gene_id": "gene-001",
            "sema_cos_sim": 0.91,
            "preview": "splice trims fragments",
            "path": "helix_context/splice.py",
        },
        {
            "gene_id": "gene-002",
            "sema_cos_sim": 0.84,
            "preview": "codon chunker splits text",
            "path": "helix_context/codons.py",
        },
    ]
    return sess


def test_neighbors_json_shape(fake_session):
    with patch(
        "helix_context.cli.cmd_neighbors.open_session", return_value=fake_session
    ):
        rc, out, err = _run(["neighbors", "splice step", "--json"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["query"] == "splice step"
    assert payload["count"] == 2
    assert payload["neighbors"][0]["gene_id"] == "gene-001"


def test_neighbors_passes_k(fake_session):
    with patch(
        "helix_context.cli.cmd_neighbors.open_session", return_value=fake_session
    ):
        rc, _, _ = _run(["neighbors", "test", "--k", "5"])
    assert rc == 0
    _, kwargs = fake_session.neighbors.call_args
    assert kwargs["k"] == 5


def test_neighbors_empty_text_mode_explains_why():
    sess = MagicMock()
    sess.neighbors.return_value = []
    with patch("helix_context.cli.cmd_neighbors.open_session", return_value=sess):
        rc, out, _ = _run(["neighbors", "test"])
    assert rc == 0
    assert "SEMA codec missing" in out or "no embeddings" in out

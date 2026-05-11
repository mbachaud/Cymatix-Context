"""Tests for `helix query`. Mocks helix_context.api.open_session so the
CLI surface is tested in isolation from the retrieval stack."""
from __future__ import annotations

import io
import json
import contextlib
from unittest.mock import MagicMock, patch

import pytest

from helix_context.api import QueryResult
from helix_context.cli import main


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def fake_session():
    sess = MagicMock()
    sess.session_id = "sess-test-0000000000000000"
    sess.query.return_value = QueryResult(
        expressed_context="<helix>example bytes</helix>",
        document_ids=["gene-001", "gene-002"],
        know=None,
        miss=None,
        estimated_tokens=42,
        decision_reason="test fixture verdict",
        next_action="answer_from_evidence",
    )
    return sess


def test_query_text_mode_prints_expressed_context(fake_session):
    with patch("helix_context.cli.cmd_query.open_session", return_value=fake_session):
        rc, out, err = _run(["query", "hello world"])
    assert rc == 0, err
    assert "<helix>example bytes</helix>" in out


def test_query_json_emits_agent_payload(fake_session):
    with patch("helix_context.cli.cmd_query.open_session", return_value=fake_session):
        rc, out, err = _run(["query", "hello world", "--json"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["expressed_context"] == "<helix>example bytes</helix>"
    assert payload["evidence"] == ["gene-001", "gene-002"]
    assert payload["estimated_tokens"] == 42
    assert payload["decision_reason"] == "test fixture verdict"
    assert payload["next_action"] == "answer_from_evidence"


def test_query_passes_k_through(fake_session):
    with patch("helix_context.cli.cmd_query.open_session", return_value=fake_session):
        rc, out, err = _run(["query", "test", "--k", "8"])
    assert rc == 0
    _, kwargs = fake_session.query.call_args
    assert kwargs["k"] == 8


def test_query_tier_broad_maps_to_broad_decoder(fake_session):
    with patch("helix_context.cli.cmd_query.open_session", return_value=fake_session):
        rc, _, _ = _run(["query", "test", "--tier", "broad"])
    assert rc == 0
    _, kwargs = fake_session.query.call_args
    assert kwargs["decoder_mode"] == "broad"


def test_query_tier_focused_maps_to_condensed_decoder(fake_session):
    with patch("helix_context.cli.cmd_query.open_session", return_value=fake_session):
        rc, _, _ = _run(["query", "test", "--tier", "focused"])
    assert rc == 0
    _, kwargs = fake_session.query.call_args
    assert kwargs["decoder_mode"] == "condensed"


def test_query_learn_flag_passes_through(fake_session):
    with patch("helix_context.cli.cmd_query.open_session", return_value=fake_session):
        rc, _, _ = _run(["query", "test", "--learn"])
    assert rc == 0
    _, kwargs = fake_session.query.call_args
    assert kwargs["learn"] is True


def test_query_returns_one_when_session_raises():
    sess = MagicMock()
    sess.query.side_effect = RuntimeError("genome unreachable")
    with patch("helix_context.cli.cmd_query.open_session", return_value=sess):
        rc, out, err = _run(["query", "test", "--json"])
    assert rc == 1
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "genome unreachable" in payload["error"]


def test_query_text_mode_includes_verdict(fake_session):
    """Text mode must surface the verdict from to_agent_json()."""
    with patch("helix_context.cli.cmd_query.open_session", return_value=fake_session):
        rc, out, err = _run(["query", "hello world"])
    assert rc == 0
    assert "verdict:" in out


def test_query_text_mode_error_goes_to_stderr():
    """Plain-text error path writes the error string to stderr (not stdout)
    and returns EXIT_ERROR."""
    sess = MagicMock()
    sess.query.side_effect = RuntimeError("genome unreachable")
    with patch("helix_context.cli.cmd_query.open_session", return_value=sess):
        rc, out, err = _run(["query", "test"])  # no --json
    assert rc == 1
    assert "genome unreachable" in err
    # Stdout should NOT contain the error message in text mode.
    assert "genome unreachable" not in out

"""Tests for `helix packet`. Mocks ``cymatix_context.api.open_session`` so
the CLI surface is tested without standing up the retrieval stack."""
from __future__ import annotations

import contextlib
import io
import json
from unittest.mock import MagicMock, patch

import pytest

from cymatix_context.cli import main
from cymatix_context.schemas import (
    ContextItem,
    ContextPacket,
    RefreshTarget,
)
from tests.conftest import run_cli as _run


@pytest.fixture
def fake_session():
    sess = MagicMock()
    sess.session_id = "sess-test-0000000000000000"
    sess.packet.return_value = ContextPacket(
        task_type="explain",
        query="what does the splice step do?",
        verified=[
            ContextItem(
                kind="gene",
                gene_id="gene-001",
                title="splice.py docstring",
                content="The splice step trims low-value fragments.",
                relevance_score=0.82,
                live_truth_score=0.91,
                status="verified",
            ),
        ],
        stale_risk=[],
        refresh_targets=[
            RefreshTarget(
                target_kind="file",
                source_id="cymatix_context/splice.py",
                reason="last_seen_outside_freshness_window",
                priority=0.5,
            ),
        ],
        notes=["coordinate_confidence=0.71 below 0.80 floor"],
        coordinate_confidence=0.71,
        file_coverage=0.80,
    )
    return sess


def test_packet_json_emits_full_packet(fake_session):
    with patch("cymatix_context.cli.cmd_packet.open_session", return_value=fake_session):
        rc, out, err = _run(["packet", "what does the splice step do?", "--json"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["task_type"] == "explain"
    assert payload["query"] == "what does the splice step do?"
    assert len(payload["verified"]) == 1
    assert payload["verified"][0]["gene_id"] == "gene-001"
    assert len(payload["refresh_targets"]) == 1
    assert payload["refresh_targets"][0]["source_id"] == "cymatix_context/splice.py"


def test_packet_text_mode_surfaces_counts(fake_session):
    with patch("cymatix_context.cli.cmd_packet.open_session", return_value=fake_session):
        rc, out, err = _run(["packet", "test"])
    assert rc == 0, err
    assert "verified: 1" in out
    assert "stale_risk: 0" in out
    assert "refresh_targets: 1" in out


def test_packet_passes_task_type_through(fake_session):
    with patch("cymatix_context.cli.cmd_packet.open_session", return_value=fake_session):
        rc, _, _ = _run(["packet", "test", "--task-type", "edit"])
    assert rc == 0
    _, kwargs = fake_session.packet.call_args
    assert kwargs["task_type"] == "edit"


def test_packet_rejects_unknown_task_type(fake_session):
    out, err = io.StringIO(), io.StringIO()
    with patch("cymatix_context.cli.cmd_packet.open_session", return_value=fake_session):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with pytest.raises(SystemExit) as exc:
                main(["packet", "test", "--task-type", "bogus"])
    assert exc.value.code == 2
    assert "bogus" in err.getvalue()
    assert fake_session.packet.call_count == 0


def test_packet_include_raw_passes_through(fake_session):
    with patch("cymatix_context.cli.cmd_packet.open_session", return_value=fake_session):
        rc, _, _ = _run(["packet", "test", "--include-raw"])
    assert rc == 0
    _, kwargs = fake_session.packet.call_args
    assert kwargs["include_raw"] is True


def test_packet_error_path_returns_one():
    sess = MagicMock()
    sess.packet.side_effect = RuntimeError("genome unreachable")
    with patch("cymatix_context.cli.cmd_packet.open_session", return_value=sess):
        rc, out, err = _run(["packet", "test", "--json"])
    assert rc == 1
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "genome unreachable" in payload["error"]

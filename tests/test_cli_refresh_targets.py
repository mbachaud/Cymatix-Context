"""Tests for `helix refresh-targets`."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from cymatix_context.schemas import RefreshTarget
from tests.conftest import run_cli as _run


@pytest.fixture
def fake_session():
    sess = MagicMock()
    sess.refresh_targets.return_value = [
        RefreshTarget(
            target_kind="file",
            source_id="cymatix_context/splice.py",
            reason="stale",
            priority=0.7,
        ),
        RefreshTarget(
            target_kind="file",
            source_id="cymatix_context/codons.py",
            reason="weakly_grounded",
            priority=0.3,
        ),
    ]
    return sess


def test_refresh_targets_json_emits_list(fake_session):
    with patch(
        "cymatix_context.cli.cmd_refresh_targets.open_session",
        return_value=fake_session,
    ):
        rc, out, err = _run(["refresh-targets", "edit splice", "--json"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["count"] == 2
    assert payload["refresh_targets"][0]["source_id"] == "cymatix_context/splice.py"


def test_refresh_targets_default_task_type_is_edit(fake_session):
    with patch(
        "cymatix_context.cli.cmd_refresh_targets.open_session",
        return_value=fake_session,
    ):
        rc, _, _ = _run(["refresh-targets", "test"])
    assert rc == 0
    _, kwargs = fake_session.refresh_targets.call_args
    assert kwargs["task_type"] == "edit"


def test_refresh_targets_empty_list_text_mode():
    sess = MagicMock()
    sess.refresh_targets.return_value = []
    with patch(
        "cymatix_context.cli.cmd_refresh_targets.open_session",
        return_value=sess,
    ):
        rc, out, _ = _run(["refresh-targets", "test"])
    assert rc == 0
    assert "(none" in out


def test_refresh_targets_passes_max_genes(fake_session):
    with patch(
        "cymatix_context.cli.cmd_refresh_targets.open_session",
        return_value=fake_session,
    ):
        rc, _, _ = _run(["refresh-targets", "test", "--max-genes", "16"])
    assert rc == 0
    _, kwargs = fake_session.refresh_targets.call_args
    assert kwargs["max_genes"] == 16

"""Tests for `helix diag corpus`."""
from __future__ import annotations

import io
import json
import contextlib
from unittest.mock import MagicMock, patch

import pytest

from helix_context.api import StatsResult
from helix_context.cli import main


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def fake_session():
    sess = MagicMock()
    sess.stats.return_value = StatsResult(
        total_genes=125,
        total_codons=500,
        chromatin_open=80,
        chromatin_eu=35,
        chromatin_hetero=10,
        compression_ratio=4.2,
        metadata={"health": {"stale_genes": 3}},
    )
    return sess


def test_diag_corpus_json(fake_session):
    with patch("helix_context.cli.cmd_diag.open_session", return_value=fake_session):
        rc, out, err = _run(["diag", "corpus", "--json"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["total_genes"] == 125
    assert payload["tier_distribution"]["open"] == 80
    assert payload["tier_distribution"]["euchromatin"] == 35
    assert payload["tier_distribution"]["heterochromatin"] == 10
    assert payload["compression_ratio"] == 4.2
    assert payload["staleness"]["stale_genes"] == 3


def test_diag_corpus_text(fake_session):
    with patch("helix_context.cli.cmd_diag.open_session", return_value=fake_session):
        rc, out, err = _run(["diag", "corpus"])
    assert rc == 0, err
    assert "total_genes: 125" in out
    assert "open: 80" in out
    assert "heterochromatin: 10" in out


def test_diag_unknown_target_returns_two(fake_session):
    with pytest.raises(SystemExit) as exc:
        main(["diag", "nope"])
    assert exc.value.code == 2


def test_diag_corpus_json_remains_valid_when_session_raises(monkeypatch):
    """Regression: `helix diag corpus --json` must emit a parseable JSON
    payload on the error path, not a half-printed traceback.

    cmd_diag.run wraps open_session() + sess.stats() in a single try/except;
    if either raises, the error payload must still serialize as valid JSON
    so machine consumers (CI, MCP bridges) don't choke.
    """
    from helix_context.cli import cmd_diag
    from helix_context.cli.output import EXIT_ERROR

    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(cmd_diag, "open_session", _boom)

    rc, out, err = _run(["diag", "corpus", "--json"])
    # The assertion is "this parses" — if json.loads raises, the regression
    # is back.
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "error" in payload
    assert "RuntimeError" in payload["error"]
    assert "boom" in payload["error"]
    assert rc == EXIT_ERROR

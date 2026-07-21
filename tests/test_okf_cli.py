"""`helix ingest --okf` — CLI surface for OKF bundle ingestion."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import make_helix_config, run_cli as _run

pytest.importorskip("spacy")

OKF_FIXTURES = Path(__file__).parent / "fixtures" / "okf"


@pytest.fixture
def real_session():
    """A session-shaped object over a real in-memory manager."""
    from cymatix_context.context_manager import HelixContextManager

    return SimpleNamespace(_manager=HelixContextManager(make_helix_config()))


def test_okf_ingest_happy_path(real_session):
    with patch(
        "cymatix_context.cli.cmd_ingest.open_session", return_value=real_session
    ):
        rc, out, err = _run(
            ["ingest", str(OKF_FIXTURES / "type_only"), "--okf", "--json"]
        )
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["bundle_id"] == "type_only"
    assert payload["concepts_ingested"] == 1
    assert len(payload["digest"]) == 64
    assert payload["links"] == {"captured": 0, "resolved": 0, "dangling": 0}
    assert payload["deterministic_profile"] is False


def test_okf_reports_warnings_and_links(real_session):
    with patch(
        "cymatix_context.cli.cmd_ingest.open_session", return_value=real_session
    ):
        rc, out, err = _run(
            ["ingest", str(OKF_FIXTURES / "degraded"), "--okf", "--json"]
        )
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["links"]["captured"] == 2
    assert payload["links"]["dangling"] == 1
    assert payload["links"]["resolved"] == 1
    assert len(payload["warnings"]) == 3


def test_okf_bundle_id_override(real_session):
    with patch(
        "cymatix_context.cli.cmd_ingest.open_session", return_value=real_session
    ):
        rc, out, err = _run(
            [
                "ingest", str(OKF_FIXTURES / "type_only"),
                "--okf", "--bundle-id", "renamed", "--json",
            ]
        )
    assert rc == 0, err
    assert json.loads(out)["bundle_id"] == "renamed"


def test_okf_requires_directory(tmp_path):
    f = tmp_path / "file.md"
    f.write_text("---\ntype: X\n---\nbody", encoding="utf-8")
    rc, out, err = _run(["ingest", str(f), "--okf", "--json"])
    assert rc == 1
    assert "directory" in json.loads(out)["error"]


def test_okf_flags_require_okf(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    for extra in (["--bundle-id", "b"], ["--deterministic"]):
        rc, out, err = _run(["ingest", str(tmp_path), "--json"] + extra)
        assert rc == 1
        assert "--okf" in json.loads(out)["error"]


def test_okf_deterministic_profile_flips_ingest_flags(real_session):
    """--deterministic loads config with SEMA/dense/SPLADE ingest off."""
    captured = {}

    def fake_open_session(*, config=None, **kwargs):
        captured["config"] = config
        return real_session

    with patch(
        "cymatix_context.cli.cmd_ingest.open_session", side_effect=fake_open_session
    ), patch(
        "cymatix_context.config.load_config", return_value=make_helix_config()
    ):
        rc, out, err = _run(
            [
                "ingest", str(OKF_FIXTURES / "type_only"),
                "--okf", "--deterministic", "--json",
            ]
        )
    assert rc == 0, err
    cfg = captured["config"]
    assert cfg is not None
    assert cfg.ingestion.sema_embed_on_ingest is False
    assert cfg.ingestion.dense_embed_on_ingest is False
    assert cfg.ingestion.splade_enabled is False
    assert json.loads(out)["deterministic_profile"] is True


def test_okf_standard_config_is_default(real_session):
    """Without --deterministic, open_session gets no config override —
    the public determinism claim is digest-scoped, not byte-scoped."""
    captured = {}

    def fake_open_session(*, config=None, **kwargs):
        captured["config"] = config
        return real_session

    with patch(
        "cymatix_context.cli.cmd_ingest.open_session", side_effect=fake_open_session
    ):
        rc, _out, err = _run(
            ["ingest", str(OKF_FIXTURES / "type_only"), "--okf", "--json"]
        )
    assert rc == 0, err
    assert captured["config"] is None

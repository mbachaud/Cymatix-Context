"""Tests for `helix config show`."""
from __future__ import annotations

import io
import json
import contextlib

from helix_context.cli import main


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_config_show_emits_json_by_default(monkeypatch, tmp_path):
    cfg = tmp_path / "helix.toml"
    cfg.write_text(
        "[budget]\nribosome_tokens = 3500\n[server]\nport = 11437\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HELIX_CONFIG", str(cfg))

    rc, out, err = _run(["config", "show"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["budget"]["ribosome_tokens"] == 3500
    assert payload["server"]["port"] == 11437


def test_config_show_text_mode(monkeypatch, tmp_path):
    cfg = tmp_path / "helix.toml"
    cfg.write_text("[budget]\nribosome_tokens = 4000\n", encoding="utf-8")
    monkeypatch.setenv("HELIX_CONFIG", str(cfg))

    rc, out, err = _run(["config", "show", "--text"])
    assert rc == 0, err
    assert "ribosome_tokens" in out
    assert "4000" in out


def test_config_show_falls_back_to_defaults_when_no_toml(monkeypatch, tmp_path):
    monkeypatch.setenv("HELIX_CONFIG", str(tmp_path / "missing.toml"))

    rc, out, err = _run(["config", "show"])
    assert rc == 0, err
    payload = json.loads(out)
    assert isinstance(payload.get("budget"), dict)
    assert isinstance(payload.get("server"), dict)

"""Tests for `helix status`."""
from __future__ import annotations

import io
import json
import contextlib
import sqlite3

from helix_context.cli import main


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def _make_genome_file(path):
    """Create a minimal SQLite file so cold-start probe can open it RO."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS genes (id TEXT PRIMARY KEY, content TEXT)")
    conn.commit()
    conn.close()


def test_status_exit_3_when_genome_missing(monkeypatch, tmp_path):
    missing_db = (tmp_path / 'does-not-exist.db').as_posix()
    cfg = tmp_path / "helix.toml"
    cfg.write_text(
        f"[genome]\npath = \"{missing_db}\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HELIX_CONFIG", str(cfg))

    rc, out, err = _run(["status", "--json", "--no-network"])
    assert rc == 3, err
    payload = json.loads(out)
    assert payload["genome"]["reachable"] is False
    assert "next_action" in payload


def test_status_exit_0_when_genome_present(monkeypatch, tmp_path):
    genome = tmp_path / "genome.db"
    _make_genome_file(genome)
    cfg = tmp_path / "helix.toml"
    cfg.write_text(
        f"[genome]\npath = \"{genome.as_posix()}\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HELIX_CONFIG", str(cfg))
    # Unset HELIX_GENOME_PATH so load_config reads from the TOML file
    monkeypatch.delenv("HELIX_GENOME_PATH", raising=False)

    rc, out, err = _run(["status", "--json", "--no-network"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["genome"]["reachable"] is True
    assert payload["config"]["valid"] is True


def test_status_text_mode_human_readable(monkeypatch, tmp_path):
    genome = tmp_path / "genome.db"
    _make_genome_file(genome)
    cfg = tmp_path / "helix.toml"
    cfg.write_text(f"[genome]\npath = \"{genome.as_posix()}\"\n", encoding="utf-8")
    monkeypatch.setenv("HELIX_CONFIG", str(cfg))
    # Unset HELIX_GENOME_PATH so load_config reads from the TOML file
    monkeypatch.delenv("HELIX_GENOME_PATH", raising=False)

    rc, out, err = _run(["status", "--no-network"])
    assert rc == 0, err
    assert "Genome:" in out
    assert "Config:" in out


def test_status_json_remains_valid_when_load_config_raises(monkeypatch, tmp_path):
    """Regression for production fix #6: when load_config() raises (e.g.
    type-coerced field rejects a value), `helix status --json` must still
    emit valid JSON, log a warning, and annotate the genome section so
    consumers know the path is a CWD-relative fallback, not authoritative.

    NOTE on TOML parse errors: tomllib.TOMLDecodeError is swallowed inside
    load_config (config.py:531-533 falls back to defaults silently). To
    exercise the fix #6 branch we use a TOML that *parses* but fails type
    coercion (server.port = string → int() raises ValueError). This is the
    realistic trigger for the fallback path.
    """
    cfg = tmp_path / "helix.toml"
    # Parses as TOML but server.port=int(...) will raise ValueError.
    cfg.write_text('[server]\nport = "not-a-number"\n', encoding="utf-8")

    # Ensure no env override masks the malformed file's effect.
    monkeypatch.delenv("HELIX_GENOME_PATH", raising=False)
    monkeypatch.delenv("HELIX_CONFIG", raising=False)

    rc, out, err = _run(["status", "--json", "--no-network", "--config", str(cfg)])

    # The regression assertion: stdout payload must parse as JSON.
    payload = json.loads(out)

    # config_report should reflect the load_config failure.
    assert payload["config"]["valid"] is False
    assert "error" in payload["config"]

    # production fix #6: genome section is annotated as a CWD-relative
    # fallback when load_config raised, so --json consumers can tell the
    # path isn't authoritative.
    assert payload["genome"]["path_source"] == "fallback_default"


def test_status_probes_genome_on_absolute_windows_path(monkeypatch, tmp_path):
    """Regression: as_uri() must produce a SQLite-parseable URI on Windows paths."""
    monkeypatch.delenv("HELIX_GENOME_PATH", raising=False)
    genome = tmp_path / "genome.db"
    _make_genome_file(genome)
    cfg = tmp_path / "helix.toml"
    # TOML literal string (single quotes) preserves backslashes verbatim,
    # so this works for native Windows paths and POSIX paths alike.
    cfg.write_text(f"[genome]\npath = '{genome.resolve()}'\n", encoding="utf-8")
    monkeypatch.setenv("HELIX_CONFIG", str(cfg))

    rc, out, err = _run(["status", "--json", "--no-network"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["genome"]["reachable"] is True

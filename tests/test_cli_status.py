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

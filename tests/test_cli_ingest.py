"""Tests for `helix ingest`."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from cymatix_context.api import IngestResult
from tests.conftest import run_cli as _run


@pytest.fixture
def fake_session():
    sess = MagicMock()
    sess.ingest.return_value = IngestResult(
        gene_ids=["gene-aaaa", "gene-bbbb"], chunks=2, bytes_written=128
    )
    return sess


def test_ingest_single_file(fake_session, tmp_path):
    src = tmp_path / "doc.txt"
    src.write_text("hello helix\n", encoding="utf-8")

    with patch("cymatix_context.cli.cmd_ingest.open_session", return_value=fake_session):
        rc, out, err = _run(["ingest", str(src), "--json"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["files_processed"] == 1
    assert payload["gene_ids"] == ["gene-aaaa", "gene-bbbb"]
    assert payload["bytes_written"] == 128

    # The session.ingest was called once with the file contents.
    assert fake_session.ingest.call_count == 1
    args, kwargs = fake_session.ingest.call_args
    assert args == ("hello helix\n",) or kwargs.get("content") == "hello helix\n"


def test_ingest_directory_walks_top_level_files(fake_session, tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.md").write_text("b", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.txt").write_text("c", encoding="utf-8")

    with patch("cymatix_context.cli.cmd_ingest.open_session", return_value=fake_session):
        rc, out, err = _run(["ingest", str(tmp_path), "--json"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["files_processed"] == 2   # a.txt + b.md, not sub/c.txt


def test_ingest_returns_one_on_missing_path(tmp_path):
    rc, out, err = _run(["ingest", str(tmp_path / "missing.txt"), "--json"])
    assert rc == 1
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "not found" in payload["error"].lower() or "no such" in payload["error"].lower()


def test_ingest_recursive_flag_walks_subdirs(fake_session, tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.txt").write_text("c", encoding="utf-8")

    with patch("cymatix_context.cli.cmd_ingest.open_session", return_value=fake_session):
        rc, out, err = _run(["ingest", str(tmp_path), "--recursive", "--json"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["files_processed"] == 2  # a.txt + sub/c.txt


def test_ingest_rejects_single_file_with_unsupported_extension(tmp_path):
    """Single-file ingest must honor the extension filter — otherwise a
    user pointing at a binary file would silently ingest replacement chars."""
    bin_file = tmp_path / "blob.exe"
    bin_file.write_bytes(b"\x00\x01MZ\x90\x00\x03")

    rc, out, err = _run(["ingest", str(bin_file), "--json"])
    assert rc == 1
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "no matching files" in payload["error"].lower()

"""Tests for helix-vault CLI subcommands.

The CLI talks to the running server over HTTP. Tests mock the HTTP client.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from helix_context.vault.cli import main


def test_main_no_args_prints_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    out = capsys.readouterr().out + capsys.readouterr().err
    assert exc.value.code != 0


def test_status_calls_endpoint(capsys):
    with patch("helix_context.vault.cli.httpx") as httpx:
        client = MagicMock()
        httpx.Client.return_value.__enter__.return_value = client
        client.get.return_value.json.return_value = {"enabled": True}
        client.get.return_value.status_code = 200
        rc = main(["status"])
    assert rc == 0


def test_export_full_calls_endpoint():
    with patch("helix_context.vault.cli.httpx") as httpx:
        client = MagicMock()
        httpx.Client.return_value.__enter__.return_value = client
        client.post.return_value.status_code = 200
        client.post.return_value.json.return_value = {"genes_exported": 5}
        rc = main(["export", "--full"])
    assert rc == 0
    client.post.assert_called_once()
    call_args = client.post.call_args
    assert "/export/obsidian" in call_args[0][0]


def test_pin_calls_endpoint():
    with patch("helix_context.vault.cli.httpx") as httpx:
        client = MagicMock()
        httpx.Client.return_value.__enter__.return_value = client
        client.post.return_value.status_code = 200
        client.post.return_value.json.return_value = {"ok": True}
        rc = main(["pin", "abc123"])
    assert rc == 0
    call_args = client.post.call_args
    assert "/vault/traces/abc123/pin" in call_args[0][0]


def test_trace_request_id_calls_trace_endpoint():
    with patch("helix_context.vault.cli.httpx") as httpx:
        client = MagicMock()
        httpx.Client.return_value.__enter__.return_value = client
        client.post.return_value.status_code = 200
        client.post.return_value.json.return_value = {"path": "/x", "request_id": "req1"}
        rc = main(["trace", "req1"])
    assert rc == 0
    call_args = client.post.call_args
    assert "/vault/trace" in call_args[0][0]


def test_trace_last_calls_status_endpoint(capsys):
    with patch("helix_context.vault.cli.httpx") as httpx:
        client = MagicMock()
        httpx.Client.return_value.__enter__.return_value = client
        client.get.return_value.status_code = 200
        client.get.return_value.json.return_value = {"vault_root": "/tmp/v"}
        rc = main(["trace", "--last", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "/tmp/v" in out
    client.get.assert_called_once()
    assert "/vault/status" in client.get.call_args[0][0]


def test_trace_no_args_errors():
    with patch("helix_context.vault.cli.httpx"):
        rc = main(["trace"])
    assert rc != 0  # returns 2 when neither request_id nor --last given

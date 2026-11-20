"""`cymatix plugins list` and plugin-provided CLI commands."""
from __future__ import annotations

import logging

import pytest

from cymatix_context.cli import main
from cymatix_context.plugins import loader
from cymatix_context.plugins.loader import LoadedPlugin
from tests.conftest import run_cli as _run


class _P:
    name = "pro"
    version = "2.0"
    requires_core = ">=0"

    def __init__(self, cmds):
        self._cmds = cmds

    def cli_commands(self):
        return self._cmds


def _lp(name="pro", status="loaded", obj=None, signed=False, reason=None):
    return LoadedPlugin(name, "1.0", "entry_point", signed, status, reason, obj)


@pytest.fixture(autouse=True)
def _reset():
    loader.reset_plugins_cache()
    yield
    loader.reset_plugins_cache()


def test_plugins_list_prints_rows(monkeypatch):
    monkeypatch.setattr(
        loader, "get_plugins",
        lambda: [_lp("alpha"), _lp("beta", status="disabled", reason="ImportError('x')")],
    )
    rc, out, _ = _run(["plugins", "list"])
    assert rc == 0
    header = out.splitlines()[0].split()
    assert header[:5] == ["name", "version", "source", "signed", "status"]
    assert "alpha" in out and "beta" in out
    assert "disabled" in out and "loaded" in out


def test_plugin_cli_command_dispatched(monkeypatch):
    calls = []

    def fn(args):
        calls.append(args)
        return 7

    monkeypatch.setattr(loader, "get_plugins", lambda: [_lp(obj=_P({"pro": fn}))])
    assert main(["pro", "x"]) == 7
    assert calls == [["x"]]


def test_builtin_name_not_overridable(monkeypatch, caplog):
    monkeypatch.setattr(loader, "get_plugins", lambda: pytest.fail("builtin must not discover"))
    from cymatix_context.cli import cmd_query

    ran = []
    monkeypatch.setattr(cmd_query, "run", lambda a: ran.append(a) or 0)
    assert main(["query", "q"]) == 0
    assert ran == [["q"]]


def test_builtin_override_attempt_warns_once(monkeypatch, caplog):
    def fn(args):
        raise AssertionError("must not run")

    monkeypatch.setattr(loader, "get_plugins", lambda: [_lp(obj=_P({"query": fn}))])
    with caplog.at_level(logging.WARNING):
        rc, out, _ = _run(["plugins", "list"])
        _run(["plugins", "list"])
    assert rc == 0
    msgs = [r for r in caplog.records if "query" in r.getMessage() and "builtin" in r.getMessage()]
    assert len(msgs) == 1


def test_unknown_subcommand_still_errors(monkeypatch):
    monkeypatch.setattr(loader, "get_plugins", lambda: [_lp(obj=_P({"pro": lambda a: 0}))])
    with pytest.raises(SystemExit) as exc:
        main(["nope"])
    assert exc.value.code == 2

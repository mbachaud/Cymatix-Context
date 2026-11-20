"""Plugin discovery (entry-point source), hook isolation, [plugins] config."""
from __future__ import annotations

import logging

import cymatix_context
from cymatix_context.config import PluginsConfig, load_config
from cymatix_context.plugins.loader import LoadedPlugin, call_hook, discover_plugins


class _FakeEP:
    def __init__(self, name, factory=None, exc=None):
        self.name = name
        self._factory = factory
        self._exc = exc

    def load(self):
        if self._exc is not None:
            raise self._exc
        return self._factory


class _Plugin:
    name = "demo"
    version = "1.0"

    def __init__(self, requires_core=None):
        self.requires_core = requires_core or f"=={cymatix_context.__version__}"

    def menu(self):
        raise RuntimeError("boom")


def _discover(cfg=None, *eps):
    return discover_plugins(cfg or PluginsConfig(), entry_points=list(eps))


def test_loads_entry_point_plugin():
    out = _discover(None, _FakeEP("demo", lambda: _Plugin()))
    assert len(out) == 1
    assert out[0].status == "loaded"
    assert out[0].source == "entry_point"
    assert out[0].name == "demo"
    assert out[0].obj is not None


def test_import_error_isolated():
    out = _discover(None, _FakeEP("demo", exc=ImportError("nope")))
    assert len(out) == 1
    assert out[0].status == "disabled"
    assert "ImportError" in out[0].reason


def test_requires_core_mismatch_skipped():
    out = _discover(None, _FakeEP("demo", lambda: _Plugin(requires_core=">=99")))
    assert out[0].status == "skipped"
    assert "requires_core" in out[0].reason


def test_disabled_by_config():
    out = _discover(PluginsConfig(disabled=["demo"]), _FakeEP("demo", lambda: _Plugin()))
    assert out[0].status == "skipped"
    assert out[0].reason == "disabled in [plugins]"


def test_identity_is_entry_point_name(caplog):
    class Liar(_Plugin):
        name = "other"

    with caplog.at_level(logging.WARNING):
        out = _discover(PluginsConfig(disabled=["other"]), _FakeEP("demo", lambda: Liar()))
    assert out[0].name == "demo"
    assert out[0].status == "loaded"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("demo" in m and "other" in m for m in msgs)


def test_disabled_ep_name_never_calls_factory():
    def factory():
        raise AssertionError("factory must not run")

    class EP(_FakeEP):
        def load(self):
            return factory

    out = _discover(PluginsConfig(disabled=["demo"]), EP("demo"))
    assert out[0].status == "skipped"


def test_call_hook_systemexit_disables(caplog):
    class P(_Plugin):
        def menu(self):
            raise SystemExit(3)

    lp = _discover(None, _FakeEP("demo", lambda: P()))[0]
    with caplog.at_level(logging.WARNING):
        assert call_hook(lp, "menu", default=[]) == []
    assert lp.status == "disabled"


def test_call_hook_keyboardinterrupt_propagates():
    import pytest

    class P(_Plugin):
        def menu(self):
            raise KeyboardInterrupt

    lp = _discover(None, _FakeEP("demo", lambda: P()))[0]
    with pytest.raises(KeyboardInterrupt):
        call_hook(lp, "menu", default=[])


def test_plugins_off():
    assert _discover(PluginsConfig(enabled=False), _FakeEP("demo", lambda: _Plugin())) == []


def test_call_hook_exception_disables(caplog):
    lp = _discover(None, _FakeEP("demo", lambda: _Plugin()))[0]
    with caplog.at_level(logging.WARNING):
        assert call_hook(lp, "menu", default=[]) == []
    assert lp.status == "disabled"
    assert "RuntimeError" in lp.reason
    assert any(r.levelno == logging.WARNING and "demo" in r.getMessage() for r in caplog.records)
    # Disabled plugins are no longer called.
    assert call_hook(lp, "menu", default=["d"]) == ["d"]


def test_call_hook_missing_hook_returns_default():
    lp = _discover(None, _FakeEP("demo", lambda: _Plugin()))[0]
    assert call_hook(lp, "panels", default="dflt") == "dflt"
    assert lp.status == "loaded"


def test_call_hook_non_loaded_returns_default():
    lp = LoadedPlugin("x", "1", "entry_point", False, "skipped", "r", None)
    assert call_hook(lp, "menu", default=5) == 5


def test_plugins_section_parsed(tmp_path, caplog):
    p = tmp_path / "cymatix.toml"
    p.write_text('[plugins]\nenabled=false\ndisabled=["a"]\ndir="X"\nbogus=1\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        cfg = load_config(str(p))
    assert cfg.plugins.enabled is False
    assert cfg.plugins.disabled == ["a"]
    assert cfg.plugins.dir == "X"
    assert any("Unknown keys in [plugins]" in r.getMessage() for r in caplog.records)

"""Directory plugins: layout, Ed25519 signatures, per-file hashes, ordering."""
from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

import pytest

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from cymatix_context.config import PluginsConfig  # noqa: E402
from cymatix_context.plugins.loader import discover_plugins  # noqa: E402
from cymatix_context.plugins.signing import verify_plugin_dir  # noqa: E402

PLUGIN_SRC = """
class _P:
    name = {name!r}
    version = "1.2.3"
    requires_core = ">=0"

def make():
    return _P()
"""


@pytest.fixture
def key():
    return Ed25519PrivateKey.generate()


def _pub(key) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


@pytest.fixture(autouse=True)
def _clean_import_state(monkeypatch):
    monkeypatch.delenv("CYMATIX_ALLOW_UNSIGNED_PLUGINS", raising=False)
    path_before = list(sys.path)
    mods_before = set(sys.modules)
    yield
    sys.path[:] = path_before
    for m in set(sys.modules) - mods_before:
        sys.modules.pop(m, None)


def make_plugin(
    root: Path,
    key=None,
    *,
    name: str = "demo",
    module: str | None = None,
    manifest_extra: dict | None = None,
    files_extra: dict | None = None,
    sign: bool = True,
    dirname: str | None = None,
) -> Path:
    """Build <root>/<dirname>/{plugin.json,plugin.sig,src/<module>.py}."""
    module = module or f"dirplug_{uuid.uuid4().hex}"
    pdir = root / (dirname or name)
    (pdir / "src").mkdir(parents=True)
    body = PLUGIN_SRC.format(name=name).encode()
    (pdir / "src" / f"{module}.py").write_bytes(body)
    files = {f"{module}.py": hashlib.sha256(body).hexdigest()}
    files.update(files_extra or {})
    manifest = {
        "name": name,
        "version": "1.2.3",
        "requires_core": ">=0",
        "entry": f"{module}:make",
        "capabilities": ["status"],
        "publisher": "tests",
        "requires": [],
        "files": files,
    }
    manifest.update(manifest_extra or {})
    raw = json.dumps(manifest, sort_keys=True).encode()
    (pdir / "plugin.json").write_bytes(raw)
    if sign and key is not None:
        (pdir / "plugin.sig").write_bytes(key.sign(raw))
    return pdir


def run(root: Path, *, keys=(), frozen=False, entry_points=(), cfg=None):
    return discover_plugins(
        cfg or PluginsConfig(),
        entry_points=list(entry_points),
        plugin_dir=root,
        frozen=frozen,
        trusted_keys=tuple(keys),
    )


def test_signed_dir_plugin_loads(tmp_path, key):
    make_plugin(tmp_path, key)
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "loaded", lp.reason
    assert lp.source == "directory"
    assert lp.signed is True
    assert lp.name == "demo"
    assert lp.version == "1.2.3"
    assert lp.obj is not None


def test_unsigned_dir_plugin_refused_when_frozen(tmp_path):
    make_plugin(tmp_path, None, sign=False)
    [lp] = run(tmp_path, frozen=True)
    assert lp.status == "skipped"
    assert lp.reason == "unsigned"
    assert lp.obj is None


def test_unsigned_dir_plugin_refused_by_default_not_frozen(tmp_path):
    make_plugin(tmp_path, None, sign=False)
    [lp] = run(tmp_path, frozen=False)
    assert lp.status == "skipped"
    assert lp.reason == "unsigned"


def test_unsigned_allowed_with_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CYMATIX_ALLOW_UNSIGNED_PLUGINS", "1")
    make_plugin(tmp_path, None, sign=False)
    [lp] = run(tmp_path)
    assert lp.status == "loaded", lp.reason
    assert lp.signed is False
    assert lp.source == "directory"


def test_tampered_file_refused(tmp_path, key):
    pdir = make_plugin(tmp_path, key, module="tamper_mod")
    f = pdir / "src" / "tamper_mod.py"
    f.write_bytes(f.read_bytes() + b"# x\n")
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "skipped"
    assert "tamper_mod.py" in lp.reason
    assert lp.obj is None


def test_extra_file_refused(tmp_path, key):
    pdir = make_plugin(tmp_path, key)
    (pdir / "src" / "evil.py").write_text("raise SystemExit\n")
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "skipped"
    assert "evil.py" in lp.reason


def test_missing_requires_refused(tmp_path, key):
    make_plugin(tmp_path, key, manifest_extra={"requires": ["no_such_mod"]})
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "skipped"
    assert "no_such_mod" in lp.reason


def test_present_requires_ok(tmp_path, key):
    make_plugin(tmp_path, key, manifest_extra={"requires": ["json"]})
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "loaded", lp.reason


class _FakeEP:
    def __init__(self, name, factory):
        self.name = name
        self._factory = factory

    def load(self):
        return self._factory


class _EPPlugin:
    name = "demo"
    version = "9.9"
    requires_core = ">=0"


def test_duplicate_name_entry_point_wins(tmp_path, key):
    make_plugin(tmp_path, key)
    out = run(tmp_path, keys=[_pub(key)], entry_points=[_FakeEP("demo", lambda: _EPPlugin())])
    by_source = {lp.source: lp for lp in out}
    assert by_source["entry_point"].status == "loaded"
    assert by_source["directory"].status == "skipped"
    assert by_source["directory"].reason == "duplicate of entry point"


def test_cryptography_missing_refuses_dir_plugins(tmp_path, key, monkeypatch):
    make_plugin(tmp_path, key)
    monkeypatch.setitem(sys.modules, "cryptography.exceptions", None)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.asymmetric.ed25519", None)
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "skipped"
    assert lp.reason == "cryptography not installed"


def test_signed_but_untrusted_key_refused(tmp_path, key):
    make_plugin(tmp_path, key)
    other = Ed25519PrivateKey.generate()
    [lp] = run(tmp_path, keys=[_pub(other)])
    assert lp.status == "skipped"
    assert "trusted key" in lp.reason


def test_no_trusted_keys_refuses_signed(tmp_path, key):
    make_plugin(tmp_path, key)
    [lp] = run(tmp_path, keys=[])
    assert lp.status == "skipped"


def test_refused_plugin_leaves_sys_path_unchanged(tmp_path, key):
    pdir = make_plugin(tmp_path, key, module="path_mod")
    (pdir / "src" / "path_mod.py").write_bytes(b"raise RuntimeError('ran')\n")
    before = list(sys.path)
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "skipped"
    assert sys.path == before
    assert "path_mod" not in sys.modules


def test_unsigned_refused_leaves_sys_path_unchanged(tmp_path):
    make_plugin(tmp_path, None, sign=False)
    before = list(sys.path)
    run(tmp_path)
    assert sys.path == before


def test_disabled_checked_before_src_on_path_or_import(tmp_path, key):
    pdir = make_plugin(tmp_path, key, module="disabled_mod")
    (pdir / "src" / "disabled_mod.py").write_bytes(b"raise RuntimeError('ran')\n")
    before = list(sys.path)
    [lp] = run(tmp_path, keys=[_pub(key)], cfg=PluginsConfig(disabled=["demo"]))
    assert lp.status == "skipped"
    assert "disabled" in lp.reason
    assert sys.path == before
    assert "disabled_mod" not in sys.modules


def test_dirname_must_equal_manifest_name(tmp_path, key):
    make_plugin(tmp_path, key, name="demo", dirname="other")
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "skipped"
    assert "other" in lp.reason and "demo" in lp.reason


def test_reported_name_mismatch_keeps_identity_and_warns(tmp_path, key, caplog):
    pdir = make_plugin(tmp_path, key, module="liar_mod")
    body = PLUGIN_SRC.format(name="liar").encode()
    (pdir / "src" / "liar_mod.py").write_bytes(body)
    manifest = json.loads((pdir / "plugin.json").read_bytes())
    manifest["files"]["liar_mod.py"] = hashlib.sha256(body).hexdigest()
    raw = json.dumps(manifest).encode()
    (pdir / "plugin.json").write_bytes(raw)
    (pdir / "plugin.sig").write_bytes(key.sign(raw))
    with caplog.at_level("WARNING"):
        [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "loaded", lp.reason
    assert lp.name == "demo"
    assert any("demo" in r.getMessage() and "liar" in r.getMessage() for r in caplog.records)


def test_symlink_in_src_refused(tmp_path, key):
    pdir = make_plugin(tmp_path, key)
    target = tmp_path / "outside.py"
    target.write_text("x = 1\n")
    try:
        (pdir / "src" / "link.py").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this host")
    ok, reason = verify_plugin_dir(pdir, [_pub(key)])
    assert ok is False
    assert "link.py" in reason
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "skipped"


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions are Windows-only")
def test_junction_in_src_refused(tmp_path, key):
    import subprocess

    pdir = make_plugin(tmp_path, key)
    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    outside.mkdir()
    (outside / "x.py").write_text("x = 1\n")
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(pdir / "src" / "jn"), str(outside)],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    ok, reason = verify_plugin_dir(pdir, [_pub(key)])
    assert ok is False
    assert "jn" in reason
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "skipped"


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions are Windows-only")
def test_junction_target_pycache_not_deleted(tmp_path, key):
    import subprocess

    pdir = make_plugin(tmp_path, key)
    outside = tmp_path.parent / f"{tmp_path.name}_outside2"
    victim = outside / "__pycache__"
    victim.mkdir(parents=True)
    (victim / "keep.pyc").write_bytes(b"keep")
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(pdir / "src" / "jn"), str(outside)],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "skipped"
    assert (victim / "keep.pyc").exists()


def test_unlistable_subdir_refused(tmp_path, key, monkeypatch):
    import os

    pdir = make_plugin(tmp_path, key)
    (pdir / "src" / "sub").mkdir()
    (pdir / "src" / "sub" / "planted.py").write_text("x = 1\n")
    real = os.scandir

    def deny(p="."):
        if Path(os.fspath(p)).name == "sub":
            raise PermissionError("denied")
        return real(p)

    monkeypatch.setattr(os, "scandir", deny)
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "skipped"
    assert "cannot list" in lp.reason


@pytest.mark.parametrize("bad", ["../escape.py", "/abs.py", "a/../../b.py", "C:evil.py", "a\\b.py"])
def test_path_traversal_in_files_refused(tmp_path, key, bad):
    # Manifest is validly signed: the refusal must come from the key check.
    make_plugin(tmp_path, key, files_extra={bad: "0" * 64})
    before = list(sys.path)
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "skipped"
    assert "unsafe path" in lp.reason
    assert sys.path == before


def test_stale_pycache_purged_and_plugin_loads(tmp_path, key):
    pdir = make_plugin(tmp_path, key)
    cache = pdir / "src" / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "x.cpython-312.pyc").write_bytes(b"junk")
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "loaded", lp.reason
    assert not cache.exists()


def test_second_discovery_after_load_still_loads(tmp_path, key):
    make_plugin(tmp_path, key)
    assert run(tmp_path, keys=[_pub(key)])[0].status == "loaded"
    assert run(tmp_path, keys=[_pub(key)])[0].status == "loaded"


def test_pyc_outside_pycache_still_refused(tmp_path, key):
    pdir = make_plugin(tmp_path, key)
    (pdir / "src" / "evil.pyc").write_bytes(b"junk")
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "skipped"
    assert "evil.pyc" in lp.reason


def test_pycache_removal_failure_refuses(tmp_path, key, monkeypatch):
    import shutil

    pdir = make_plugin(tmp_path, key)
    (pdir / "src" / "__pycache__").mkdir()

    def boom(*a, **k):
        raise PermissionError("locked")

    monkeypatch.setattr(shutil, "rmtree", boom)
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "skipped"
    assert "__pycache__" in lp.reason


def test_verify_plugin_dir_ok_and_unsigned(tmp_path, key):
    pdir = make_plugin(tmp_path, key)
    assert verify_plugin_dir(pdir, [_pub(key)]) == (True, None)
    (pdir / "plugin.sig").unlink()
    ok, reason = verify_plugin_dir(pdir, [_pub(key)])
    assert ok is False and reason == "unsigned"


def test_import_failure_isolated_and_path_restored(tmp_path, key):
    pdir = make_plugin(tmp_path, key, module="boom_mod")
    body = b"raise RuntimeError('boom')\n"
    (pdir / "src" / "boom_mod.py").write_bytes(body)
    manifest = json.loads((pdir / "plugin.json").read_bytes())
    manifest["files"]["boom_mod.py"] = hashlib.sha256(body).hexdigest()
    raw = json.dumps(manifest).encode()
    (pdir / "plugin.json").write_bytes(raw)
    (pdir / "plugin.sig").write_bytes(key.sign(raw))
    before = list(sys.path)
    [lp] = run(tmp_path, keys=[_pub(key)])
    assert lp.status == "disabled"
    assert "boom" in lp.reason
    assert sys.path == before


def test_missing_dir_is_empty(tmp_path):
    assert run(tmp_path / "nope") == []

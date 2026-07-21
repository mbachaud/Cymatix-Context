"""Rename back-compat contract: old helix_context paths alias, not copy."""
import os
import subprocess
import sys
import warnings


def _purge_old_modules():
    for mod in [m for m in list(sys.modules) if m.split(".")[0] == "helix_context"]:
        del sys.modules[mod]


def test_old_package_import_warns():
    _purge_old_modules()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        import helix_context  # noqa: F401
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_old_import_is_same_module_object():
    import cymatix_context.config as new_cfg
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import helix_context.config as old_cfg
    assert old_cfg is new_cfg


def test_deep_from_import_is_same_module_object():
    from cymatix_context.retrieval import freshness as new_mod
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from helix_context.retrieval import freshness as old_mod
    assert old_mod is new_mod


def test_existing_module_shims_still_work():
    # genome.py / ribosome.py / server.py re-export shims from the pre-rename
    # era must survive the second rename layer.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import helix_context.genome  # noqa: F401
        import helix_context.mcp_server as old_mcp
    import cymatix_context.mcp.mcp_server as new_mcp
    assert old_mcp is new_mcp


def test_mcp_dash_m_entry_importable_via_old_path():
    proc = subprocess.run(
        [sys.executable, "-c",
         "import helix_context.mcp_server as m; print(m.__name__)"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


def test_alias_preserves_canonical_metadata():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import helix_context.retrieval.expand as old_mod
    import cymatix_context.retrieval.expand as new_mod
    assert old_mod is new_mod
    assert new_mod.__name__ == "cymatix_context.retrieval.expand"
    assert new_mod.__spec__ is not None
    assert new_mod.__spec__.name == "cymatix_context.retrieval.expand"
    assert new_mod.__package__ == "cymatix_context.retrieval"


def test_env_mirror_copies_cymatix_to_helix(monkeypatch):
    monkeypatch.setenv("CYMATIX_RENAME_TEST_XYZ", "1")
    monkeypatch.delenv("HELIX_RENAME_TEST_XYZ", raising=False)
    from cymatix_context import _mirror_env
    _mirror_env()
    assert os.environ["HELIX_RENAME_TEST_XYZ"] == "1"


def test_env_mirror_never_overrides_explicit_helix_value(monkeypatch):
    monkeypatch.setenv("CYMATIX_RENAME_TEST_ABC", "new")
    monkeypatch.setenv("HELIX_RENAME_TEST_ABC", "old")
    from cymatix_context import _mirror_env
    _mirror_env()
    assert os.environ["HELIX_RENAME_TEST_ABC"] == "old"


def test_config_prefers_cymatix_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HELIX_CONFIG", raising=False)
    monkeypatch.delenv("CYMATIX_CONFIG", raising=False)
    (tmp_path / "cymatix.toml").write_text("[server]\nport = 12345\n", encoding="utf-8")
    (tmp_path / "helix.toml").write_text("[server]\nport = 54321\n", encoding="utf-8")
    from cymatix_context.config import load_config
    cfg = load_config()
    assert cfg.server.port == 12345


def test_config_falls_back_to_helix_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HELIX_CONFIG", raising=False)
    monkeypatch.delenv("CYMATIX_CONFIG", raising=False)
    (tmp_path / "helix.toml").write_text("[server]\nport = 54321\n", encoding="utf-8")
    from cymatix_context.config import load_config
    cfg = load_config()
    assert cfg.server.port == 54321


def test_new_and_old_console_scripts_registered():
    from importlib.metadata import entry_points
    names = {ep.name for ep in entry_points(group="console_scripts")}
    expected = {
        "cymatix", "cymatix-server", "cymatix-launcher", "cymatix-status", "cymatix-vault",
        "helix", "helix-server", "helix-launcher", "helix-status", "helix-vault",
    }
    missing = expected - names
    assert not missing, f"missing console scripts: {missing}"


def test_mcp_server_identifies_as_cymatix():
    from cymatix_context.mcp.mcp_server import mcp
    assert mcp.name == "cymatix"

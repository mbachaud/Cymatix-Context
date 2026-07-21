"""Rename back-compat contract: old helix_context paths alias, not copy."""
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

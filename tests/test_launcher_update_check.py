from unittest.mock import MagicMock, patch

from cymatix_context.launcher.update_check import UpdateChecker, is_newer_version


def test_is_newer_version_compares_numeric_prefixes():
    assert is_newer_version("0.14.0", "0.13.4") is True
    assert is_newer_version("0.14.0", "0.14.0") is False
    assert is_newer_version("0.14.0b1", "0.14.0") is False


def test_update_checker_reports_new_pypi_version(monkeypatch):
    monkeypatch.setattr(
        "cymatix_context.launcher.update_check.installed_version",
        lambda package_name="helix-context": "0.13.4",
    )
    resp = MagicMock()
    resp.json.return_value = {"info": {"version": "0.14.0"}}
    with patch("cymatix_context.launcher.update_check.httpx.get", return_value=resp):
        info = UpdateChecker(ttl_s=60).check()
    assert info.current_version == "0.13.4"
    assert info.latest_version == "0.14.0"
    assert info.update_available is True


def test_update_checker_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HELIX_LAUNCHER_UPDATE_CHECK", "0")
    info = UpdateChecker().check()
    assert info.update_available is False
    assert info.latest_version is None

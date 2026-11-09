from unittest.mock import MagicMock, patch

import pytest

from cymatix_context.launcher.update_check import UpdateChecker, is_newer_version


def test_is_newer_version_compares_numeric_prefixes():
    assert is_newer_version("0.14.0", "0.13.4") is True
    assert is_newer_version("0.14.0", "0.14.0") is False
    assert is_newer_version("0.14.0b1", "0.14.0") is False


def _no_network(*args, **kwargs):
    raise AssertionError("update check made an outbound request")


def test_update_checker_is_off_by_default_and_makes_no_request(monkeypatch):
    # Issue #481: nothing leaves the machine unless the operator opts in.
    monkeypatch.delenv("CYMATIX_LAUNCHER_UPDATE_CHECK", raising=False)
    monkeypatch.setattr("cymatix_context.launcher.update_check.httpx.get", _no_network)
    checker = UpdateChecker()
    assert checker.enabled is False
    info = checker.check()
    assert info.update_available is False
    assert info.latest_version is None
    assert checker.check(force=True).latest_version is None


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_update_checker_opt_in_values_enable_it(monkeypatch, value):
    monkeypatch.setenv("CYMATIX_LAUNCHER_UPDATE_CHECK", value)
    assert UpdateChecker().enabled is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
def test_update_checker_other_values_leave_it_off(monkeypatch, value):
    monkeypatch.setenv("CYMATIX_LAUNCHER_UPDATE_CHECK", value)
    monkeypatch.setattr("cymatix_context.launcher.update_check.httpx.get", _no_network)
    checker = UpdateChecker()
    assert checker.enabled is False
    assert checker.check().latest_version is None


def test_update_checker_explicit_argument_wins_over_env(monkeypatch):
    monkeypatch.setenv("CYMATIX_LAUNCHER_UPDATE_CHECK", "1")
    assert UpdateChecker(enabled=False).enabled is False
    monkeypatch.setenv("CYMATIX_LAUNCHER_UPDATE_CHECK", "0")
    assert UpdateChecker(enabled=True).enabled is True


def test_update_checker_reports_new_pypi_version(monkeypatch):
    monkeypatch.setenv("CYMATIX_LAUNCHER_UPDATE_CHECK", "1")
    monkeypatch.setattr(
        "cymatix_context.launcher.update_check.installed_version",
        lambda package_name="cymatix-context": "0.13.4",
    )
    resp = MagicMock()
    resp.json.return_value = {"info": {"version": "0.14.0"}}
    with patch("cymatix_context.launcher.update_check.httpx.get", return_value=resp):
        info = UpdateChecker(ttl_s=60).check()
    assert info.current_version == "0.13.4"
    assert info.latest_version == "0.14.0"
    assert info.update_available is True

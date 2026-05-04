"""
Tests for helix_context.launcher.tray — tray menu action handlers and
CLI integration. pystray is fully mocked so tests run headless.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from helix_context.launcher import tray as tray_mod
from helix_context.launcher.tray import HelixTrayIcon, _build_icon_image, is_tray_available
from helix_context.launcher.supervisor import (
    AlreadyRunning,
    NotRunning,
    SupervisorError,
)


@pytest.fixture
def fake_supervisor():
    sup = MagicMock()
    sup.is_running.return_value = True
    sup.get_pid.return_value = 12345
    sup.start.return_value = 12345
    sup.restart.return_value = 67890
    return sup


@pytest.fixture
def tray_icon(fake_supervisor):
    return HelixTrayIcon(
        supervisor=fake_supervisor,
        dashboard_url="http://127.0.0.1:11438/",
    )


class TestIsTrayAvailable:
    def test_returns_bool(self):
        assert isinstance(is_tray_available(), bool)


class TestBuildIconImage:
    def test_default_size(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            pytest.skip("PIL not installed — build_icon_image requires Pillow")
        img = _build_icon_image()
        assert img.size == (64, 64)

    def test_custom_size(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            pytest.skip("PIL not installed")
        img = _build_icon_image(size=32)
        assert img.size == (32, 32)


class TestMenuActions:
    def test_open_dashboard_calls_webbrowser(self, tray_icon):
        with patch("helix_context.launcher.tray.webbrowser.open") as mock_open:
            tray_icon._open_dashboard(None, None)
            mock_open.assert_called_once_with("http://127.0.0.1:11438/")

    def test_open_dashboard_swallows_errors(self, tray_icon):
        with patch(
            "helix_context.launcher.tray.webbrowser.open",
            side_effect=Exception("no browser"),
        ):
            # Should not raise
            tray_icon._open_dashboard(None, None)

    def test_start_calls_supervisor_start(self, tray_icon, fake_supervisor):
        fake_supervisor.is_running.return_value = False
        tray_icon._start_helix(None, None)
        fake_supervisor.start.assert_called_once()

    def test_start_handles_already_running(self, tray_icon, fake_supervisor):
        fake_supervisor.start.side_effect = AlreadyRunning("already up")
        # Should not raise
        tray_icon._start_helix(None, None)

    def test_start_handles_supervisor_error(self, tray_icon, fake_supervisor):
        fake_supervisor.start.side_effect = SupervisorError("port in use")
        # Should not raise
        tray_icon._start_helix(None, None)

    def test_restart_calls_supervisor_restart(self, tray_icon, fake_supervisor):
        tray_icon._restart_helix(None, None)
        fake_supervisor.restart.assert_called_once()

    def test_stop_calls_supervisor_stop(self, tray_icon, fake_supervisor):
        tray_icon._stop_helix(None, None)
        fake_supervisor.stop.assert_called_once()

    def test_stop_handles_not_running(self, tray_icon, fake_supervisor):
        fake_supervisor.stop.side_effect = NotRunning("not running")
        # Should not raise
        tray_icon._stop_helix(None, None)


class TestQuitAction:
    def test_quit_stops_helix_when_running(self, tray_icon, fake_supervisor):
        fake_supervisor.is_running.return_value = True
        tray_icon._icon = MagicMock()
        with patch("helix_context.launcher.tray.os.kill"):
            tray_icon._quit(None, None)
        fake_supervisor.stop.assert_called_once()
        tray_icon._icon.stop.assert_called_once()
        assert tray_icon._quit_event.is_set()

    def test_quit_skips_helix_stop_when_already_stopped(self, tray_icon, fake_supervisor):
        fake_supervisor.is_running.return_value = False
        tray_icon._icon = MagicMock()
        with patch("helix_context.launcher.tray.os.kill"):
            tray_icon._quit(None, None)
        fake_supervisor.stop.assert_not_called()
        tray_icon._icon.stop.assert_called_once()

    def test_quit_calls_on_quit_extra(self, fake_supervisor):
        on_quit_mock = MagicMock()
        tray = HelixTrayIcon(
            supervisor=fake_supervisor,
            dashboard_url="http://127.0.0.1:11438/",
            on_quit=on_quit_mock,
        )
        tray._icon = MagicMock()
        fake_supervisor.is_running.return_value = False
        with patch("helix_context.launcher.tray.os.kill"):
            tray._quit(None, None)
        on_quit_mock.assert_called_once()

    def test_quit_survives_on_quit_hook_exception(self, fake_supervisor):
        on_quit_mock = MagicMock(side_effect=Exception("boom"))
        tray = HelixTrayIcon(
            supervisor=fake_supervisor,
            dashboard_url="http://127.0.0.1:11438/",
            on_quit=on_quit_mock,
        )
        tray._icon = MagicMock()
        fake_supervisor.is_running.return_value = False
        with patch("helix_context.launcher.tray.os.kill"):
            # Should not raise
            tray._quit(None, None)
        assert tray._quit_event.is_set()


class TestCLIIntegration:
    def test_tray_and_native_rejected_on_non_windows(self, monkeypatch):
        """--tray --native combined returns exit 2 on non-Windows platforms."""
        from helix_context.launcher import app as app_mod
        monkeypatch.setattr(app_mod.sys, "platform", "darwin")
        rc = app_mod.main(["--tray", "--native", "--no-browser", "--no-autostart"])
        assert rc == 2

    def test_tray_and_native_rejected_on_linux(self, monkeypatch):
        from helix_context.launcher import app as app_mod
        monkeypatch.setattr(app_mod.sys, "platform", "linux"),
        rc = app_mod.main(["--tray", "--native", "--no-browser", "--no-autostart"])
        assert rc == 2

    def test_tray_without_extras_fails_fast(self, monkeypatch):
        """--tray with pystray unavailable returns exit code 1, not silent exit."""
        from helix_context.launcher import app as app_mod
        monkeypatch.setattr(app_mod, "_check_tray_available", lambda: False)
        rc = app_mod.main(["--tray", "--no-autostart"])
        assert rc == 1

    def test_check_tray_available_returns_bool(self):
        from helix_context.launcher.app import _check_tray_available
        assert isinstance(_check_tray_available(), bool)


def _menu_titles(menu) -> list:
    """Robust extraction of item.text strings from a pystray.Menu.

    pystray.Menu exposes its items via .items in 0.19+; older versions
    expose ._items. Either way we want the list of MenuItem.text values
    (or None for separators). This helper exists so tests don't break
    when pystray bumps minor versions.
    """
    raw = getattr(menu, "items", None)
    if raw is None:
        raw = getattr(menu, "_items", [])
    out = []
    for it in raw:
        out.append(getattr(it, "text", None))
    return out


def test_tray_observability_submenu_built_when_supervisor_present(tmp_path):
    """When an ObservabilitySupervisor is wired, the tray menu gains an
    Observability submenu with per-service status entries."""
    pytest.importorskip("pystray")  # only meaningful if [launcher-tray] installed
    from helix_context.launcher.tray import HelixTrayIcon
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )
    from helix_context.launcher.state import StateStore
    from helix_context.launcher.supervisor import HelixSupervisor

    store = StateStore(path=tmp_path / "state.json")
    helix_sup = HelixSupervisor(
        store=store, helix_host="127.0.0.1", helix_port=11999,
        helix_log_path=tmp_path / "h.log",
    )
    obs_sup = ObservabilitySupervisor()
    icon = HelixTrayIcon(
        supervisor=helix_sup,
        dashboard_url="http://127.0.0.1:11438",
        observability_supervisor=obs_sup,
    )
    # The Observability submenu lives as a single item titled "Observability";
    # the per-service status content is rendered when the submenu is opened.
    titles = _menu_titles(icon._build_menu())
    assert "Observability" in titles


def test_tray_observability_submenu_omitted_without_supervisor(tmp_path):
    """No supervisor wired → no Observability submenu (clean menu for
    users who opted out)."""
    pytest.importorskip("pystray")
    from helix_context.launcher.tray import HelixTrayIcon
    from helix_context.launcher.state import StateStore
    from helix_context.launcher.supervisor import HelixSupervisor

    store = StateStore(path=tmp_path / "state.json")
    helix_sup = HelixSupervisor(
        store=store, helix_host="127.0.0.1", helix_port=11999,
        helix_log_path=tmp_path / "h.log",
    )
    icon = HelixTrayIcon(
        supervisor=helix_sup,
        dashboard_url="http://127.0.0.1:11438",
        observability_supervisor=None,
    )
    titles = _menu_titles(icon._build_menu())
    assert "Observability" not in titles


# ── Task 8.5: install-pulse on the Observability submenu ───────────────


def _resolve_text(item):
    """Resolve a pystray MenuItem.text — could be a str or a callable
    that takes the item and returns a str."""
    raw = getattr(item, "_text", None)
    if callable(raw):
        return raw(item)
    return getattr(item, "text", None)


def _find_observability_item(menu):
    """Locate the top-level "Observability" MenuItem (or its pulsed
    variant). Returns None if not present."""
    raw = getattr(menu, "items", None)
    if raw is None:
        raw = getattr(menu, "_items", [])
    for it in raw:
        text = _resolve_text(it)
        if text and text.startswith("Observability"):
            return it
    return None


def _build_pulsing_tray(tmp_path):
    pytest.importorskip("pystray")
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )
    from helix_context.launcher.state import StateStore
    from helix_context.launcher.supervisor import HelixSupervisor

    store = StateStore(path=tmp_path / "state.json")
    helix_sup = HelixSupervisor(
        store=store, helix_host="127.0.0.1", helix_port=11999,
        helix_log_path=tmp_path / "h.log",
    )
    obs_sup = ObservabilitySupervisor()
    icon = HelixTrayIcon(
        supervisor=helix_sup,
        dashboard_url="http://127.0.0.1:11438",
        observability_supervisor=obs_sup,
    )
    icon._icon = MagicMock()
    return icon


class TestInstallPulse:
    def test_pulse_inactive_by_default(self, tmp_path):
        icon = _build_pulsing_tray(tmp_path)
        assert icon._install_pulse_active is False
        assert icon._install_pulse_timer is None

    def test_notify_install_needed_starts_pulse(self, tmp_path):
        icon = _build_pulsing_tray(tmp_path)
        # Patch threading.Timer so the timer never actually schedules a
        # callback during the test (we check the start path, not the loop).
        with patch("helix_context.launcher.tray.threading.Timer") as mock_timer:
            mock_timer.return_value = MagicMock()
            icon.notify_install_needed()
        assert icon._install_pulse_active is True
        # Timer was scheduled with a 1-second cadence.
        mock_timer.assert_called()
        args, _kwargs = mock_timer.call_args
        assert args[0] == 1.0

    def test_pulse_timer_is_daemon(self, tmp_path):
        icon = _build_pulsing_tray(tmp_path)
        icon.start_install_pulse()
        try:
            assert icon._install_pulse_timer is not None
            assert icon._install_pulse_timer.daemon is True
        finally:
            icon.stop_install_pulse()

    def test_stop_install_pulse_clears_state(self, tmp_path):
        icon = _build_pulsing_tray(tmp_path)
        icon.start_install_pulse()
        icon.stop_install_pulse()
        assert icon._install_pulse_active is False
        assert icon._install_pulse_timer is None

    def test_stop_install_pulse_idempotent(self, tmp_path):
        icon = _build_pulsing_tray(tmp_path)
        # Calling without start should not crash
        icon.stop_install_pulse()
        # Double-stop after start should not crash
        icon.start_install_pulse()
        icon.stop_install_pulse()
        icon.stop_install_pulse()
        assert icon._install_pulse_active is False

    def test_observability_label_plain_when_idle(self, tmp_path):
        icon = _build_pulsing_tray(tmp_path)
        menu = icon._build_menu()
        item = _find_observability_item(menu)
        assert item is not None
        assert _resolve_text(item) == "Observability"

    def test_observability_label_alternates_during_pulse(self, tmp_path):
        icon = _build_pulsing_tray(tmp_path)
        icon.start_install_pulse()
        try:
            menu = icon._build_menu()
            item = _find_observability_item(menu)
            assert item is not None
            # Tick state 0 → filled dot
            icon._install_pulse_state = 0
            label_a = _resolve_text(item)
            icon._install_pulse_state = 1
            label_b = _resolve_text(item)
            assert label_a != label_b
            assert "●" in label_a or "○" in label_a   # ● or ○
            assert "●" in label_b or "○" in label_b
            assert label_a.startswith("Observability ")
            assert label_b.startswith("Observability ")
        finally:
            icon.stop_install_pulse()

    def test_dismiss_item_present_only_while_pulsing(self, tmp_path):
        icon = _build_pulsing_tray(tmp_path)

        def _submenu_titles(item):
            sm = getattr(item, "submenu", None)
            if sm is None:
                return []
            raw = getattr(sm, "items", None)
            if raw is None:
                raw = getattr(sm, "_items", [])
            # Filter out items that pystray would suppress via visible=False.
            return [_resolve_text(x) for x in raw if getattr(x, "visible", True)]

        # Idle → no Dismiss
        idle_menu = icon._build_menu()
        idle_item = _find_observability_item(idle_menu)
        sub_titles_idle = _submenu_titles(idle_item)
        assert not any(t and t.startswith("Dismiss") for t in sub_titles_idle)

        icon.start_install_pulse()
        try:
            pulsing_menu = icon._build_menu()
            pulsing_item = _find_observability_item(pulsing_menu)
            sub_titles = _submenu_titles(pulsing_item)
            assert any(t and t.startswith("Dismiss") for t in sub_titles)
        finally:
            icon.stop_install_pulse()

    def test_dismiss_handler_stops_pulse(self, tmp_path):
        icon = _build_pulsing_tray(tmp_path)
        icon.start_install_pulse()
        assert icon._install_pulse_active is True
        icon._dismiss_install_pulse(None, None)
        assert icon._install_pulse_active is False

    def test_restart_obs_service_acknowledges_and_stops_pulse(self, tmp_path):
        """Spec §11.4: clicking any observability submenu item is treated
        as acknowledgment and stops the pulse."""
        icon = _build_pulsing_tray(tmp_path)
        icon.start_install_pulse()
        assert icon._install_pulse_active is True
        # Stub the observability supervisor's restart_service so we don't
        # actually try to restart anything during the test.
        icon.observability.restart_service = MagicMock()
        handler = icon._restart_obs_service("collector")
        handler(None, None)
        assert icon._install_pulse_active is False

    def test_open_obs_log_dir_acknowledges_and_stops_pulse(self, tmp_path, monkeypatch):
        icon = _build_pulsing_tray(tmp_path)
        icon.start_install_pulse()
        # Stub the file-explorer side effect.
        if sys.platform == "win32":
            monkeypatch.setattr("helix_context.launcher.tray.os.startfile",
                                lambda _p: None, raising=False)
        else:
            monkeypatch.setattr(
                "helix_context.launcher.tray.subprocess.Popen",
                lambda *a, **k: None,
                raising=False,
            )
        icon._open_obs_log_dir(None, None)
        assert icon._install_pulse_active is False

    def test_pulse_dismiss_persists_within_process(self, tmp_path):
        """Spec §11.4: dismissal persists for the process lifetime — pulse
        cannot be re-armed by notify_install_needed in the same process."""
        icon = _build_pulsing_tray(tmp_path)
        icon.start_install_pulse()
        icon._dismiss_install_pulse(None, None)
        assert icon._install_pulse_dismissed is True
        # Subsequent notify_install_needed should NOT restart the pulse.
        with patch("helix_context.launcher.tray.threading.Timer"):
            icon.notify_install_needed()
        assert icon._install_pulse_active is False

    def test_tick_pulse_toggles_state_and_refreshes(self, tmp_path):
        icon = _build_pulsing_tray(tmp_path)
        # Bypass the actual timer scheduling by patching threading.Timer.
        with patch("helix_context.launcher.tray.threading.Timer") as mock_timer:
            mock_timer.return_value = MagicMock()
            icon.start_install_pulse()
            initial = icon._install_pulse_state
            icon._tick_pulse()
            assert icon._install_pulse_state != initial
            # update_menu should have been called on the icon.
            assert icon._icon.update_menu.called

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
from helix_context.launcher.update_check import UpdateInfo
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

    def test_notify_update_available_is_one_shot(self, fake_supervisor):
        checker = MagicMock()
        checker.check.return_value = UpdateInfo(
            current_version="0.13.4",
            latest_version="0.14.0",
            update_available=True,
        )
        icon = HelixTrayIcon(
            supervisor=fake_supervisor,
            dashboard_url="http://127.0.0.1:11438/",
            update_checker=checker,
        )
        icon._icon = MagicMock()

        icon.notify_update_available()
        icon.notify_update_available()

        icon._icon.notify.assert_called_once()


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
    """No supervisor wired AND install not pending → no Observability submenu
    (clean menu for users who opted out via HELIX_OBSERVABILITY=0)."""
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
        install_pending=False,
    )
    titles = _menu_titles(icon._build_menu())
    assert "Observability" not in titles


# ── Install-pending submenu (Task 13 fix) ──────────────────────────────


def _build_install_pending_tray(tmp_path):
    """Build a tray with no supervisor but install_pending=True.

    Mirrors the actual app.py wiring path on a fresh checkout where
    tools/native-otel/ is missing — _maybe_build_observability returns
    (None, install_pending=True), and the tray must still surface a
    submenu with an Install action.
    """
    pytest.importorskip("pystray")
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
        install_pending=True,
    )
    icon._icon = MagicMock()
    return icon


def _submenu_items_for_observability(menu):
    """Return the visible submenu items (text-resolved) under the
    top-level Observability entry."""
    item = _find_observability_item(menu)
    if item is None:
        return []
    sm = getattr(item, "submenu", None)
    if sm is None:
        return []
    raw = getattr(sm, "items", None)
    if raw is None:
        raw = getattr(sm, "_items", [])
    return [
        _resolve_text(x) for x in raw
        if getattr(x, "visible", True)
    ]


class TestInstallPendingSubmenu:
    def test_submenu_rendered_when_install_pending_without_supervisor(
        self, tmp_path,
    ):
        """Task 13 fix: when binaries are missing, the tray still shows
        an Observability submenu so the user has a clickable Install
        action — not just a balloon notification."""
        icon = _build_install_pending_tray(tmp_path)
        titles = _menu_titles(icon._build_menu())
        # Top-level Observability entry must be present (label may carry
        # pulse suffix when active, so use a startswith check).
        assert any(
            t and t.startswith("Observability") for t in titles
        ), f"Observability label missing from top-level titles: {titles}"

    def test_install_action_present_in_install_pending_submenu(
        self, tmp_path,
    ):
        """The install-pending submenu must contain an "Install
        Observability..." item that the user can click."""
        icon = _build_install_pending_tray(tmp_path)
        sub_titles = _submenu_items_for_observability(icon._build_menu())
        assert any(
            t and t.startswith("Install Observability") for t in sub_titles
        ), f"Install action missing from submenu: {sub_titles}"

    def test_install_action_invokes_powershell_visibly(
        self, tmp_path,
    ):
        """Clicking Install Observability spawns the bundled
        scripts/install-native-observability.ps1 via subprocess.Popen
        with creationflags=0 — the install console is intentionally
        VISIBLE so the user can see download progress during the
        ~5-10 minute install (UX gap fix). Fire-and-forget (no
        .wait/.communicate)."""
        icon = _build_install_pending_tray(tmp_path)
        with patch(
            "helix_context.launcher.tray.subprocess.Popen"
        ) as mock_popen, patch(
            "helix_context.launcher.tray.threading.Thread"
        ):
            mock_popen.return_value = MagicMock()
            icon._run_install_observability(None, None)
        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert cmd[0].lower().endswith("powershell.exe") or cmd[0] == "powershell.exe"
        assert "-NoProfile" in cmd
        assert "-ExecutionPolicy" in cmd
        assert "Bypass" in cmd
        assert "-File" in cmd
        # The path passed to -File must end in the install script name.
        file_idx = cmd.index("-File")
        script_path = cmd[file_idx + 1]
        assert script_path.endswith("install-native-observability.ps1")
        # creationflags MUST be 0 — install console is intentionally visible
        # so the user sees download progress during the multi-minute install.
        # CREATE_NO_WINDOW is 0x08000000 on Windows; we want it OFF here.
        assert kwargs.get("creationflags", 0) == 0, (
            "Install spawn must NOT use CREATE_NO_WINDOW — console is "
            "intentionally visible for progress feedback during the long install."
        )

    def test_install_action_path_resolves_to_repo_script(
        self, tmp_path,
    ):
        """The script path must be the repo-relative
        scripts/install-native-observability.ps1, computed from the
        tray.py module location (not cwd-dependent)."""
        icon = _build_install_pending_tray(tmp_path)
        with patch(
            "helix_context.launcher.tray.subprocess.Popen"
        ) as mock_popen, patch(
            "helix_context.launcher.tray.threading.Thread"
        ):
            mock_popen.return_value = MagicMock()
            icon._run_install_observability(None, None)
        cmd = mock_popen.call_args[0][0]
        file_idx = cmd.index("-File")
        from pathlib import Path
        script_path = Path(cmd[file_idx + 1])
        # The script must actually exist at the resolved path —
        # otherwise the user click will fail at runtime.
        assert script_path.exists(), (
            f"Install script not found at resolved path: {script_path}"
        )

    def test_install_action_does_not_block_tray_thread(
        self, tmp_path,
    ):
        """Spec-critical: subprocess must NOT call .wait() or
        .communicate() — that would freeze the tray UI thread for the
        ~minutes-long install."""
        icon = _build_install_pending_tray(tmp_path)
        proc_mock = MagicMock()
        with patch(
            "helix_context.launcher.tray.subprocess.Popen",
            return_value=proc_mock,
        ), patch("helix_context.launcher.tray.threading.Thread"):
            icon._run_install_observability(None, None)
        proc_mock.wait.assert_not_called()
        proc_mock.communicate.assert_not_called()

    def test_dismiss_item_present_in_install_pending_submenu(
        self, tmp_path,
    ):
        """The install-pending submenu must include the existing
        Dismiss action so users have a non-install opt-out path. Pulse
        is started by notify_install_needed, which controls Dismiss
        visibility — so we activate the pulse before sampling."""
        icon = _build_install_pending_tray(tmp_path)
        with patch("helix_context.launcher.tray.threading.Timer"):
            icon.start_install_pulse()
        try:
            sub_titles = _submenu_items_for_observability(icon._build_menu())
            assert any(
                t and t.startswith("Dismiss") for t in sub_titles
            ), f"Dismiss item missing from submenu: {sub_titles}"
        finally:
            icon.stop_install_pulse()

    def test_pulse_label_alternates_in_install_pending_state(
        self, tmp_path,
    ):
        """Pulse animation must work in install-pending state — the
        pulse only depends on _install_pulse_active, not on supervisor
        presence (Task 8.5 contract)."""
        icon = _build_install_pending_tray(tmp_path)
        with patch("helix_context.launcher.tray.threading.Timer"):
            icon.start_install_pulse()
        try:
            menu = icon._build_menu()
            item = _find_observability_item(menu)
            assert item is not None
            icon._install_pulse_state = 0
            label_a = _resolve_text(item)
            icon._install_pulse_state = 1
            label_b = _resolve_text(item)
            assert label_a != label_b
            assert label_a.startswith("Observability ")
            assert label_b.startswith("Observability ")
            # ●/○ alternation
            assert ("●" in label_a and "○" in label_b) or (
                "○" in label_a and "●" in label_b
            )
        finally:
            icon.stop_install_pulse()

    def test_install_pending_default_false_preserves_existing_callers(
        self, tmp_path, fake_supervisor,
    ):
        """Constructor's install_pending kwarg must default to False so
        existing call sites keep working without modification."""
        icon = HelixTrayIcon(
            supervisor=fake_supervisor,
            dashboard_url="http://127.0.0.1:11438/",
        )
        assert icon._install_pending is False

    def test_install_action_swallows_subprocess_errors(
        self, tmp_path,
    ):
        """Spawn failures must not crash the tray thread — log + return
        per the global error-handling rule."""
        icon = _build_install_pending_tray(tmp_path)
        with patch(
            "helix_context.launcher.tray.subprocess.Popen",
            side_effect=OSError("powershell missing"),
        ), patch("helix_context.launcher.tray.threading.Thread"):
            # Should not raise
            icon._run_install_observability(None, None)


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


# ── Install completion watcher + auto-restart (UX gap fix) ─────────────


class TestInstallCompletionWatcher:
    """When the user clicks Install Observability, the tray spawns a
    daemon thread that polls for tools/native-otel/.install-complete
    every 2 s. When the sentinel appears, the watcher fires the
    auto-restart path: balloon → remove sentinel → spawn fresh launcher
    detached → icon.stop()."""

    def test_run_install_starts_watcher_thread(self, tmp_path):
        """_run_install_observability must spawn a watcher daemon thread."""
        icon = _build_install_pending_tray(tmp_path)
        with patch(
            "helix_context.launcher.tray.subprocess.Popen"
        ), patch(
            "helix_context.launcher.tray.threading.Thread"
        ) as mock_thread:
            mock_thread.return_value = MagicMock()
            icon._run_install_observability(None, None)
        mock_thread.assert_called_once()
        # Thread must be daemon=True so it doesn't block process exit.
        _args, kwargs = mock_thread.call_args
        assert kwargs.get("daemon", False) is True
        # The thread target must be the sentinel watcher.
        target = kwargs.get("target")
        assert target == icon._install_completion_watcher

    def test_watcher_does_not_start_if_spawn_fails(self, tmp_path):
        """If Popen raises (e.g. powershell missing), no watcher is
        scheduled — there's no install to wait for."""
        icon = _build_install_pending_tray(tmp_path)
        with patch(
            "helix_context.launcher.tray.subprocess.Popen",
            side_effect=OSError("missing"),
        ), patch(
            "helix_context.launcher.tray.threading.Thread"
        ) as mock_thread:
            icon._run_install_observability(None, None)
        mock_thread.assert_not_called()

    def test_watcher_detects_sentinel_and_calls_auto_restart(self, tmp_path):
        """When the sentinel file appears, the watcher loop calls
        _auto_restart_launcher and removes the sentinel before exiting."""
        icon = _build_install_pending_tray(tmp_path)
        # Build a fake repo root with the sentinel pre-staged.
        repo_root = tmp_path / "repo"
        (repo_root / "tools" / "native-otel").mkdir(parents=True)
        sentinel = repo_root / "tools" / "native-otel" / ".install-complete"
        sentinel.write_text("done")
        # Stub _repo_root() and _auto_restart_launcher.
        icon._repo_root = lambda: repo_root  # type: ignore[method-assign]
        icon._auto_restart_launcher = MagicMock()  # type: ignore[method-assign]
        # Patch sleep so the loop returns immediately.
        with patch("helix_context.launcher.tray.time.sleep"):
            icon._install_completion_watcher()
        icon._auto_restart_launcher.assert_called_once()
        assert not sentinel.exists(), (
            "Sentinel must be removed after detection so re-launches "
            "don't re-trigger the auto-restart."
        )

    def test_watcher_exits_when_dismissed(self, tmp_path):
        """If the user explicitly dismissed the install pulse, the
        watcher must stop polling (no auto-restart)."""
        icon = _build_install_pending_tray(tmp_path)
        repo_root = tmp_path / "repo"
        (repo_root / "tools" / "native-otel").mkdir(parents=True)
        # No sentinel — would loop forever; dismiss flag should break out.
        icon._repo_root = lambda: repo_root  # type: ignore[method-assign]
        icon._auto_restart_launcher = MagicMock()  # type: ignore[method-assign]
        icon._install_pulse_dismissed = True
        with patch("helix_context.launcher.tray.time.sleep"):
            icon._install_completion_watcher()
        icon._auto_restart_launcher.assert_not_called()

    def test_watcher_caps_at_30_minutes(self, tmp_path):
        """If the sentinel never appears, the watcher must cap at 30 min
        (900 iterations at 2 s) and exit without auto-restarting."""
        icon = _build_install_pending_tray(tmp_path)
        repo_root = tmp_path / "repo"
        (repo_root / "tools" / "native-otel").mkdir(parents=True)
        icon._repo_root = lambda: repo_root  # type: ignore[method-assign]
        icon._auto_restart_launcher = MagicMock()  # type: ignore[method-assign]
        # Count sleeps — must cap, not infinite loop.
        sleep_count = {"n": 0}

        def fake_sleep(secs):  # noqa: ARG001
            sleep_count["n"] += 1
            if sleep_count["n"] > 1000:
                raise RuntimeError("Watcher did not cap — would loop forever")

        with patch(
            "helix_context.launcher.tray.time.sleep", side_effect=fake_sleep
        ):
            icon._install_completion_watcher()
        icon._auto_restart_launcher.assert_not_called()
        # Should have run roughly 900 iterations (30 min / 2 s) — not infinite.
        assert 100 < sleep_count["n"] <= 1000

    def test_watcher_handles_oserror_on_stat(self, tmp_path):
        """If exists() raises (e.g. transient FS error), the watcher
        catches OSError and continues polling rather than crashing."""
        icon = _build_install_pending_tray(tmp_path)
        repo_root = tmp_path / "repo"
        (repo_root / "tools" / "native-otel").mkdir(parents=True)
        icon._repo_root = lambda: repo_root  # type: ignore[method-assign]
        icon._auto_restart_launcher = MagicMock()  # type: ignore[method-assign]

        # First two checks raise OSError; third returns False so the loop
        # eventually exits via the dismiss flag.
        call_count = {"n": 0}

        def fake_exists(_self):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise OSError("transient")
            # Trigger graceful exit on third call.
            icon._install_pulse_dismissed = True
            return False

        with patch("pathlib.Path.exists", new=fake_exists), patch(
            "helix_context.launcher.tray.time.sleep"
        ):
            # Should not raise.
            icon._install_completion_watcher()
        icon._auto_restart_launcher.assert_not_called()


class TestAutoRestart:
    """The auto-restart path spawns Start-helix-tray.bat in a fully
    detached process (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP) so
    the new tray survives the dying one, then calls icon.stop() to wind
    down the current tray cleanly."""

    def test_auto_restart_spawns_detached_launcher_and_stops_icon(
        self, tmp_path,
    ):
        icon = _build_install_pending_tray(tmp_path)
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        bat = repo_root / "Start-helix-tray.bat"
        bat.write_text("@echo on\n")
        icon._repo_root = lambda: repo_root  # type: ignore[method-assign]

        with patch(
            "helix_context.launcher.tray.subprocess.Popen"
        ) as mock_popen:
            icon._auto_restart_launcher()
        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        # First element is the bat path.
        from pathlib import Path
        assert Path(cmd[0]).name == "Start-helix-tray.bat"
        # Detach flags: DETACHED_PROCESS (0x08) | CREATE_NEW_PROCESS_GROUP (0x200) = 0x208.
        cf = kwargs.get("creationflags", 0)
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        assert cf & DETACHED_PROCESS, (
            f"creationflags missing DETACHED_PROCESS: 0x{cf:x}"
        )
        assert cf & CREATE_NEW_PROCESS_GROUP, (
            f"creationflags missing CREATE_NEW_PROCESS_GROUP: 0x{cf:x}"
        )
        # cwd must be repo_root so relative paths in the bat resolve.
        assert Path(str(kwargs.get("cwd"))) == repo_root
        # close_fds=True for fully detached child.
        assert kwargs.get("close_fds") is True
        # icon.stop() called after spawning the new launcher.
        icon._icon.stop.assert_called_once()

    def test_auto_restart_skips_if_bat_missing(self, tmp_path):
        """If Start-helix-tray.bat doesn't exist, auto-restart logs a
        warning and skips — icon.stop is NOT called (don't kill the
        current tray when we can't bring up a replacement)."""
        icon = _build_install_pending_tray(tmp_path)
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        # No .bat file written.
        icon._repo_root = lambda: repo_root  # type: ignore[method-assign]

        with patch(
            "helix_context.launcher.tray.subprocess.Popen"
        ) as mock_popen:
            icon._auto_restart_launcher()
        mock_popen.assert_not_called()
        icon._icon.stop.assert_not_called()

    def test_auto_restart_swallows_spawn_errors(self, tmp_path):
        """A failed Popen for the new launcher must not crash the
        watcher thread."""
        icon = _build_install_pending_tray(tmp_path)
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        bat = repo_root / "Start-helix-tray.bat"
        bat.write_text("@echo on\n")
        icon._repo_root = lambda: repo_root  # type: ignore[method-assign]

        with patch(
            "helix_context.launcher.tray.subprocess.Popen",
            side_effect=OSError("spawn failed"),
        ):
            # Should not raise.
            icon._auto_restart_launcher()
        # icon.stop is best-effort skipped when spawn fails — keep current
        # tray alive rather than dying with no replacement.
        icon._icon.stop.assert_not_called()


class TestRepoRootHelper:
    def test_repo_root_resolves_from_module_path(self, tmp_path, fake_supervisor):
        """_repo_root() returns the repo containing helix_context/, computed
        from the tray.py module location (not cwd-dependent)."""
        icon = HelixTrayIcon(
            supervisor=fake_supervisor,
            dashboard_url="http://127.0.0.1:11438/",
        )
        repo = icon._repo_root()
        from pathlib import Path
        assert isinstance(repo, Path)
        # The resolved repo must contain helix_context/ as a subdirectory.
        assert (repo / "helix_context").is_dir()
        # And scripts/install-native-observability.ps1 must live under it.
        assert (repo / "scripts" / "install-native-observability.ps1").exists()


# ── Hardware-fallback balloon (Task 13 — feat/hardware-detection) ─────


class TestHardwareFallbackBalloon:
    """Spec §6 third surface: tray balloon when the active device differs
    from the requested device. Sentinel encodes (requested, active) so a
    state-change re-fires; same-state re-launches stay quiet."""

    def test_hardware_fallback_balloon_fires_first_launch(self, tmp_path, monkeypatch):
        """Balloon fires once when fallback_active=True and no sentinel exists."""
        from helix_context import hardware

        monkeypatch.setattr("helix_context.launcher.observability_paths.state_dir",
                            lambda create=False: tmp_path)
        hardware.reset_for_test()
        fake = hardware.HardwareInfo(
            device="cpu", device_type="cpu", device_name="CPU",
            vram_total_gb=None, vram_free_gb=None,
            cpu_arch="x86_64", cpu_brand="CPU",
            system_ram_gb=16.0, requested_device="cuda",
            fallback_reason="cuda not available",
            batch_size_overrides={},
        )
        monkeypatch.setattr(hardware, "_detect", lambda: fake)

        from helix_context.launcher.tray import _should_fire_hardware_fallback_balloon
        assert _should_fire_hardware_fallback_balloon() is True

    def test_hardware_fallback_balloon_dedups_via_sentinel(self, tmp_path, monkeypatch):
        """Sentinel exists -> balloon suppressed."""
        monkeypatch.setattr("helix_context.launcher.observability_paths.state_dir",
                            lambda create=False: tmp_path)
        sentinel = tmp_path / ".hardware-fallback-acknowledged-cuda-cpu"
        sentinel.touch()

        from helix_context import hardware
        hardware.reset_for_test()
        monkeypatch.setattr(hardware, "_detect", lambda: hardware.HardwareInfo(
            device="cpu", device_type="cpu", device_name="CPU",
            vram_total_gb=None, vram_free_gb=None,
            cpu_arch="x86_64", cpu_brand="CPU", system_ram_gb=16.0,
            requested_device="cuda", fallback_reason="cuda not available",
            batch_size_overrides={},
        ))

        from helix_context.launcher.tray import _should_fire_hardware_fallback_balloon
        assert _should_fire_hardware_fallback_balloon() is False

    def test_hardware_fallback_balloon_refires_on_different_state(self, tmp_path, monkeypatch):
        """Different requested/active combo => different sentinel => balloon fires."""
        monkeypatch.setattr("helix_context.launcher.observability_paths.state_dir",
                            lambda create=False: tmp_path)
        (tmp_path / ".hardware-fallback-acknowledged-cuda-cpu").touch()

        from helix_context import hardware
        hardware.reset_for_test()
        monkeypatch.setattr(hardware, "_detect", lambda: hardware.HardwareInfo(
            device="cpu", device_type="cpu", device_name="CPU",
            vram_total_gb=None, vram_free_gb=None,
            cpu_arch="x86_64", cpu_brand="CPU", system_ram_gb=16.0,
            requested_device="mps", fallback_reason="mps not available",
            batch_size_overrides={},
        ))

        from helix_context.launcher.tray import _should_fire_hardware_fallback_balloon
        # Sentinel is for cuda->cpu; current state is mps->cpu.
        assert _should_fire_hardware_fallback_balloon() is True

    def test_hardware_fallback_balloon_skipped_when_no_fallback(self, tmp_path, monkeypatch):
        """fallback_reason is None => no balloon ever."""
        monkeypatch.setattr("helix_context.launcher.observability_paths.state_dir",
                            lambda create=False: tmp_path)
        from helix_context import hardware
        hardware.reset_for_test()
        monkeypatch.setattr(hardware, "_detect", lambda: hardware.HardwareInfo(
            device="cuda:0", device_type="cuda", device_name="RTX 4090",
            vram_total_gb=24.0, vram_free_gb=22.0,
            cpu_arch="x86_64", cpu_brand="CPU", system_ram_gb=64.0,
            requested_device="auto", fallback_reason=None,
            batch_size_overrides={},
        ))

        from helix_context.launcher.tray import _should_fire_hardware_fallback_balloon
        assert _should_fire_hardware_fallback_balloon() is False

"""
Tests for the tray genome-switch fix (issue #286):

  1. Durable selection persistence in `genome_registry` — the tray's genome
     choice must survive a launcher Quit + relaunch (previously it lived
     only in the launcher process env).
  2. Off-pump dispatch in the tray handler — the pystray menu callback must
     return immediately and run the blocking confirm dialog on a worker
     thread, never on the message-pump thread (that wedge is the bug).
  3. Confirm-dialog flag values + fail-safe (decline, not consent).

All GUI is mocked; these run headless in CI.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from helix_context.launcher import genome_registry as gr
from helix_context.launcher import tray as tray_mod
from helix_context.launcher.tray import HelixTrayIcon


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Redirect the durable selection file into a tmp dir and clear the
    HELIX_GENOME_PATH env for every test in this module."""
    monkeypatch.setenv("HELIX_LAUNCHER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HELIX_GENOME_PATH", raising=False)
    gr.clear_cache()
    yield
    gr.clear_cache()


def _make_genome(tmp_path: Path, name: str = "g.db") -> Path:
    p = tmp_path / name
    p.write_bytes(b"SQLite format 3\x00")  # header is enough; we never open it
    return p


# ── persistence (issue #286 root cause #2) ──────────────────────────────


class TestDurableSelection:
    def test_select_genome_sets_env_and_persists(self, tmp_path):
        g = _make_genome(tmp_path)
        resolved = gr.select_genome(g)
        assert os.environ["HELIX_GENOME_PATH"] == str(resolved)
        # File written co-located under the redirected state dir.
        assert gr._selection_state_path().exists()
        assert gr._read_selected_genome() == g.resolve()

    def test_active_path_reads_persisted_when_env_absent(self, tmp_path, monkeypatch):
        g = _make_genome(tmp_path)
        gr.select_genome(g)
        # Simulate a relaunch: env is gone, only the file remains.
        monkeypatch.delenv("HELIX_GENOME_PATH", raising=False)
        assert gr.active_genome_path() == g.resolve()

    def test_env_var_wins_over_persisted(self, tmp_path, monkeypatch):
        persisted = _make_genome(tmp_path, "persisted.db")
        gr.select_genome(persisted)
        override = _make_genome(tmp_path, "override.db")
        monkeypatch.setenv("HELIX_GENOME_PATH", str(override))
        assert gr.active_genome_path() == override.resolve()

    def test_missing_persisted_file_is_ignored(self, tmp_path):
        g = _make_genome(tmp_path)
        gr.select_genome(g)
        g.unlink()  # genome deleted after selection
        assert gr._read_selected_genome() is None

    def test_apply_persisted_selection_sets_env(self, tmp_path, monkeypatch):
        g = _make_genome(tmp_path)
        gr.select_genome(g)
        monkeypatch.delenv("HELIX_GENOME_PATH", raising=False)
        applied = gr.apply_persisted_selection()
        assert applied == g.resolve()
        assert os.environ["HELIX_GENOME_PATH"] == str(g.resolve())

    def test_apply_does_not_override_explicit_env(self, tmp_path, monkeypatch):
        persisted = _make_genome(tmp_path, "persisted.db")
        gr.select_genome(persisted)
        override = _make_genome(tmp_path, "override.db")
        monkeypatch.setenv("HELIX_GENOME_PATH", str(override))
        assert gr.apply_persisted_selection() is None
        assert os.environ["HELIX_GENOME_PATH"] == str(override)

    def test_apply_noop_without_selection(self, monkeypatch):
        monkeypatch.delenv("HELIX_GENOME_PATH", raising=False)
        assert gr.apply_persisted_selection() is None
        assert "HELIX_GENOME_PATH" not in os.environ

    def test_clear_selection(self, tmp_path):
        g = _make_genome(tmp_path)
        gr.select_genome(g)
        gr.clear_selection()
        assert not gr._selection_state_path().exists()
        assert gr._read_selected_genome() is None


# ── off-pump dispatch (issue #286 root cause #1) ────────────────────────


@pytest.fixture
def tray_icon():
    sup = MagicMock()
    sup.is_running.return_value = True
    return HelixTrayIcon(supervisor=sup, dashboard_url="http://127.0.0.1:11438/")


class TestOffPumpDispatch:
    def test_click_returns_before_confirm_completes(self, tray_icon, tmp_path):
        """The pystray menu callback must NOT block on the confirm dialog.
        We make confirm block on an event and assert the handler returns
        while confirm is still in-flight (i.e. it ran on another thread)."""
        started = threading.Event()
        release = threading.Event()

        def _blocking_confirm(target):
            started.set()
            release.wait(timeout=5)
            return False  # decline, so no supervisor calls after release

        handler = tray_icon._switch_genome(_make_genome(tmp_path))
        with patch.object(tray_mod, "_confirm_genome_switch", _blocking_confirm):
            handler(None, None)  # must return immediately
            assert started.wait(timeout=2), "flow thread never started"
            # Confirm is still blocked → the callback clearly did not wait.
            assert not release.is_set()
            release.set()
        # Let the daemon flow thread finish.
        for t in threading.enumerate():
            if t.name == "helix-genome-switch":
                t.join(timeout=5)

    def test_flow_declined_does_not_switch(self, tray_icon, tmp_path):
        g = _make_genome(tmp_path)
        with patch.object(tray_mod, "_confirm_genome_switch", return_value=False), \
             patch.object(tray_mod.genome_registry, "select_genome") as sel:
            tray_icon._genome_switch_flow(g)
            sel.assert_not_called()

    def test_flow_confirmed_selects_and_restarts(self, tray_icon, tmp_path):
        g = _make_genome(tmp_path)
        with patch.object(tray_mod, "_confirm_genome_switch", return_value=True):
            tray_icon._genome_switch_flow(g)
        tray_icon.supervisor.restart.assert_called_once()
        # Selection persisted as a side effect of the real select_genome.
        assert gr._read_selected_genome() == g.resolve()


# ── confirm-dialog flags + fail-safe ────────────────────────────────────


class TestConfirmDialog:
    @pytest.mark.skipif(sys.platform != "win32", reason="Win32 MessageBoxW path")
    def test_messagebox_sets_foreground_and_topmost(self, tmp_path):
        MB_SETFOREGROUND = 0x10000
        MB_TOPMOST = 0x40000
        captured = {}

        def _fake_mbox(hwnd, body, title, flags):
            captured["flags"] = flags
            return 6  # IDYES

        with patch("ctypes.windll.user32.MessageBoxW", _fake_mbox):
            assert tray_mod._confirm_genome_switch(_make_genome(tmp_path)) is True
        assert captured["flags"] & MB_SETFOREGROUND
        assert captured["flags"] & MB_TOPMOST

    def test_dialog_failure_defaults_to_decline(self, tmp_path, monkeypatch):
        """If every dialog backend fails, the switch must be declined —
        a restart-the-server action is not implied by a broken dialog."""
        # Force the win32 path (if present) to raise, and tkinter to fail.
        monkeypatch.setattr(sys, "platform", "linux", raising=False)
        with patch.dict(sys.modules, {"tkinter": None}):
            # importing tkinter raises ImportError when the module entry is None
            assert tray_mod._confirm_genome_switch(_make_genome(tmp_path)) is False

"""
Tray — system tray icon for the helix launcher.

Runs on the main thread on Windows (pystray needs the Win32 message
pump), with uvicorn running in a daemon thread. The tray icon is the
persistent surface in `--tray` mode — you can close browser tabs and
the launcher keeps running; only clicking "Quit" from the tray menu
actually stops things.

Menu:
    - Open Dashboard        opens the browser at the launcher URL
    - ---                   (separator)
    - Start helix           supervisor.start()   (disabled if running)
    - Restart helix         supervisor.restart() (disabled if stopped)
    - Stop helix            supervisor.stop()    (disabled if stopped)
    - ---
    - Quit                  stops launcher AND helix, exits the process

License note: pystray is LGPL-3. It is NOT bundled with helix-context
— users install it explicitly via the optional extra:

    pip install helix-context[launcher-tray]

This keeps the helix-context wheel itself Apache-2.0-clean.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable, Optional

# Windows process-creation flags used for auto-restart detach. Defined as
# module-level constants so tests can introspect them and so non-Windows
# platforms (where these would be 0) still see the names cleanly.
# Values per MSDN CreateProcess flags:
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

from .supervisor import (
    AlreadyRunning,
    HelixSupervisor,
    NotRunning,
    SupervisorError,
)

log = logging.getLogger("helix.launcher.tray")


def is_tray_available() -> bool:
    """Probe pystray + PIL imports without raising. Used for --tray fail-fast."""
    try:
        import pystray  # noqa: F401  # type: ignore
        from PIL import Image  # noqa: F401  # type: ignore
        return True
    except ImportError:
        return False


def _build_icon_image(size: int = 64):
    """Build a small square icon programmatically via PIL.

    Intentionally simple — a dark panel background with a blue accent
    ring, matching the launcher CSS theme tokens. A designer-friendly
    path can ship a real PNG in a follow-up.
    """
    from PIL import Image, ImageDraw

    bg = (11, 14, 19)       # --color-bg
    accent = (124, 196, 255)  # --color-accent

    img = Image.new("RGB", (size, size), color=bg)
    draw = ImageDraw.Draw(img)

    margin = size // 8
    draw.ellipse(
        (margin, margin, size - margin, size - margin),
        outline=accent,
        width=max(2, size // 16),
    )

    # Tiny inner dot for visual weight
    inner = size // 3
    draw.ellipse(
        (inner, inner, size - inner, size - inner),
        fill=accent,
    )

    return img


class HelixTrayIcon:
    """Wraps a pystray.Icon with launcher-aware menu actions.

    Instantiate after pystray is verified available (call is_tray_available
    from the caller). ``run()`` blocks the current thread — run it on the
    main thread per pystray's platform requirements.
    """

    def __init__(
        self,
        supervisor: HelixSupervisor,
        dashboard_url: str,
        name: str = "helix-launcher",
        tooltip: str = "Helix Launcher",
        on_quit: Optional[Callable[[], None]] = None,
        grafana_url: Optional[str] = None,
        prometheus_url: Optional[str] = None,
        headroom_supervisor=None,
        headroom_dashboard_url: Optional[str] = None,
        observability_supervisor=None,
        install_pending: bool = False,
    ) -> None:
        self.supervisor = supervisor
        self.dashboard_url = dashboard_url
        self.name = name
        self.tooltip = tooltip
        self._on_quit_extra = on_quit
        self.grafana_url = grafana_url
        self.prometheus_url = prometheus_url
        # Optional — provided when helix-context[codec] is installed and
        # the launcher either adopted a running Headroom proxy or is
        # allowed to manage one from config.
        self.headroom = headroom_supervisor
        self.headroom_dashboard_url = headroom_dashboard_url
        # Optional — wired when the native observability sidecar stack is
        # enabled (HELIX_OBSERVABILITY != 0 and install bootstrap is
        # complete). Drives the Observability submenu (spec §7.5, §11.4).
        self.observability = observability_supervisor
        # Task 13 fix: when binaries are missing, _maybe_build_observability
        # returns (None, install_pending=True). The tray must still surface
        # the Observability submenu in that state so the user has a
        # clickable Install action — not just an ephemeral balloon.
        self._install_pending = bool(install_pending)
        self._icon = None  # type: ignore[assignment]
        self._quit_event = threading.Event()

        # Install-prompt pulse state (spec §11.4 — balloon + tray-menu
        # pulse). The Observability submenu label alternates between
        # "Observability ●" and "Observability ○" on a 1 s cadence to draw
        # the user's eye until they either click an observability menu
        # item (acknowledgment) or explicitly Dismiss. Dismissal persists
        # for THIS process lifetime; pulse returns next launch if still
        # not installed.
        self._install_pulse_lock = threading.Lock()
        self._install_pulse_active: bool = False
        self._install_pulse_state: int = 0  # 0 → ●, 1 → ○
        self._install_pulse_timer: Optional[threading.Timer] = None
        self._install_pulse_dismissed: bool = False

    # ── menu action handlers ───────────────────────────────────────

    def _open_dashboard(self, icon, item) -> None:  # noqa: ARG002 — pystray API
        log.info("Tray: opening dashboard at %s", self.dashboard_url)
        try:
            webbrowser.open(self.dashboard_url)
        except Exception:
            log.warning("Tray: failed to open browser", exc_info=True)

    def _open_grafana(self, icon, item) -> None:  # noqa: ARG002
        if not self.grafana_url:
            return
        log.info("Tray: opening Grafana at %s", self.grafana_url)
        try:
            webbrowser.open(self.grafana_url)
        except Exception:
            log.warning("Tray: failed to open Grafana", exc_info=True)

    def _open_prometheus(self, icon, item) -> None:  # noqa: ARG002
        if not self.prometheus_url:
            return
        log.info("Tray: opening Prometheus at %s", self.prometheus_url)
        try:
            webbrowser.open(self.prometheus_url)
        except Exception:
            log.warning("Tray: failed to open Prometheus", exc_info=True)

    # ── Headroom handlers ─────────────────────────────────────────

    def _open_headroom_dashboard(self, icon, item) -> None:  # noqa: ARG002
        if not self.headroom_dashboard_url:
            return
        log.info("Tray: opening Headroom dashboard at %s",
                 self.headroom_dashboard_url)
        try:
            webbrowser.open(self.headroom_dashboard_url)
        except Exception:
            log.warning("Tray: failed to open Headroom dashboard", exc_info=True)

    def _start_headroom(self, icon, item) -> None:  # noqa: ARG002
        if self.headroom is None:
            return
        log.info("Tray: starting Headroom")
        try:
            pid = self.headroom.start()
            log.info("Tray: Headroom started (pid=%d)", pid)
        except Exception as exc:
            log.error("Tray Headroom start failed: %s", exc, exc_info=True)
        finally:
            self._refresh_menu()

    def _restart_headroom(self, icon, item) -> None:  # noqa: ARG002
        if self.headroom is None:
            return
        log.info("Tray: restarting Headroom")
        try:
            self.headroom.restart(reason="manual restart from tray menu")
        except Exception as exc:
            log.error("Tray Headroom restart failed: %s", exc, exc_info=True)
        finally:
            self._refresh_menu()

    def _stop_headroom(self, icon, item) -> None:  # noqa: ARG002
        if self.headroom is None:
            return
        log.info("Tray: stopping Headroom (force=True — user-initiated)")
        try:
            # User clicked the menu item — override ownership gate.
            self.headroom.stop(reason="manual stop from tray menu", force=True)
        except Exception as exc:
            log.error("Tray Headroom stop failed: %s", exc, exc_info=True)
        finally:
            self._refresh_menu()

    # ── Observability handlers ────────────────────────────────────

    def _restart_obs_service(self, service: str):
        def _h(icon, item):  # noqa: ARG001 — pystray API
            if self.observability is None:
                return
            # Treat any observability submenu click as acknowledgment of
            # the install-prompt (spec §11.4).
            self.stop_install_pulse()
            log.info("Tray: restart observability/%s", service)
            try:
                self.observability.restart_service(service)
            except Exception:
                log.warning("Tray restart obs/%s failed", service, exc_info=True)
            finally:
                self._refresh_menu()
        return _h

    def _open_obs_log_dir(self, icon, item):  # noqa: ARG002
        # Treat as acknowledgment of the install-prompt (spec §11.4).
        self.stop_install_pulse()
        from .observability_paths import logs_dir
        p = logs_dir(create=True)
        log.info("Tray: open log dir %s", p)
        try:
            if os.name == "nt":
                os.startfile(str(p))  # type: ignore[attr-defined]
            else:
                import subprocess
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen(
                    [opener, str(p)],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    start_new_session=(sys.platform != "win32"),
                )
        except Exception:
            log.warning("Tray: failed to open log dir", exc_info=True)

    def _dismiss_install_pulse(self, icon, item) -> None:  # noqa: ARG002
        """User explicitly dismissed the install-prompt pulse from the
        Observability submenu. Persists for this process lifetime."""
        log.info("Tray: install-prompt pulse dismissed by user")
        self.stop_install_pulse()
        self._install_pulse_dismissed = True
        self._refresh_menu()

    def _run_install_observability(self, icon, item) -> None:  # noqa: ARG002
        """Spawn the bundled install script as a fire-and-forget
        subprocess (Task 13 fix). Invoked when the user clicks
        "Install Observability..." from the install-pending submenu.

        The install runs to completion in its own PowerShell process —
        we never .wait() or .communicate() because that would freeze the
        tray UI thread for the multi-minute download/extract pass. If
        the user closes the tray before install completes, the spawned
        subprocess keeps running.
        """
        script_path = self._repo_root() / "scripts" / "install-native-observability.ps1"
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", str(script_path),
        ]
        log.info("Tray: launching native-observability installer (%s)",
                 script_path)
        # Treat clicking Install as acknowledgment — stop the pulse so
        # the user gets visual feedback even if the install takes a while.
        self.stop_install_pulse()
        try:
            # Fire-and-forget: no .wait/.communicate. creationflags=0
            # INTENTIONALLY shows the PowerShell console — the install
            # downloads ~400MB of binaries over 5-10 minutes, and a hidden
            # console gives the user no progress feedback ("is it stuck?").
            # Every other Popen in this module still uses CREATE_NO_WINDOW
            # (per global CLAUDE.md subprocess-safety); only the user-
            # facing install console gets visibility.
            subprocess.Popen(cmd, creationflags=0)
        except Exception:
            log.warning(
                "Tray: failed to launch install-native-observability.ps1",
                exc_info=True,
            )
            return
        # Spawn was successful — kick off the daemon watcher that polls
        # for the completion sentinel and triggers the auto-restart so
        # the freshly-installed binaries get picked up without the user
        # having to manually quit + re-launch.
        try:
            watcher = threading.Thread(
                target=self._install_completion_watcher,
                name="helix-install-watcher",
                daemon=True,
            )
            watcher.start()
        except Exception:
            log.warning(
                "Tray: failed to start install-completion watcher thread",
                exc_info=True,
            )

    # ── repo-root + install completion watcher + auto-restart ────

    def _repo_root(self) -> Path:
        """Resolve the repo root from this module's filesystem location.

        tray.py lives at <repo>/helix_context/launcher/tray.py, so three
        parents up from __file__ is the repo. Computed lazily so tests
        can monkeypatch this method to redirect to a tmp_path tree.
        """
        return Path(__file__).resolve().parent.parent.parent

    def _install_completion_watcher(self) -> None:
        """Daemon thread: poll for tools/native-otel/.install-complete
        every 2 s. When the sentinel appears, the install script wrote
        it just before exiting — fire balloon, remove sentinel, and
        auto-restart the launcher so the new tray picks up the freshly-
        installed binaries.

        Caps at 30 minutes total (900 iterations × 2 s) so a failed
        install doesn't leave a watcher thread polling forever. Exits
        early if the user explicitly dismissed the install pulse —
        treats dismissal as "I changed my mind, don't auto-restart."
        """
        sentinel = self._repo_root() / "tools" / "native-otel" / ".install-complete"
        max_iterations = 900  # 30 minutes at 2 s cadence
        for _ in range(max_iterations):
            if self._install_pulse_dismissed:
                log.info(
                    "Tray: install-completion watcher exiting "
                    "(user dismissed install pulse)"
                )
                return
            try:
                found = sentinel.exists()
            except OSError:
                # Transient FS error (network drive hiccup, AV scan, ...).
                # Don't crash the watcher — keep polling.
                log.debug(
                    "Tray: sentinel exists() raised OSError, will retry",
                    exc_info=True,
                )
                found = False
            if found:
                log.info(
                    "Tray: install-completion sentinel detected at %s",
                    sentinel,
                )
                # Notify the user, then remove the sentinel BEFORE the
                # restart so a subsequent re-launch with binaries already
                # present doesn't re-trigger this code path. Best-effort —
                # if removal fails the next run will still proceed.
                if self._icon is not None:
                    try:
                        self._icon.notify(
                            "Native observability installed — "
                            "restarting helix launcher...",
                            title="Helix Launcher",
                        )
                    except Exception:
                        log.debug("Tray: notify on install complete failed",
                                  exc_info=True)
                try:
                    sentinel.unlink()
                except OSError:
                    log.warning(
                        "Tray: failed to remove install sentinel %s",
                        sentinel, exc_info=True,
                    )
                self._auto_restart_launcher()
                return
            time.sleep(2.0)
        log.warning(
            "Tray: install-completion watcher timed out after 30 min "
            "without seeing sentinel — install may have failed"
        )

    def _auto_restart_launcher(self) -> None:
        """Spawn a fresh tray launcher in a fully-detached process and
        stop the current tray icon. The detach flags
        (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP) on Windows ensure
        the new launcher survives even when the current process exits.
        """
        repo_root = self._repo_root()
        bat_path = repo_root / "Start-helix-tray.bat"
        if not bat_path.exists():
            log.warning(
                "Tray: auto-restart skipped — Start-helix-tray.bat not "
                "found at %s. User must manually relaunch.",
                bat_path,
            )
            return
        # Windows detach flags. On non-Windows the named constants are
        # still numerically valid (0x208) but Popen ignores them — the
        # auto-restart path is Windows-only in practice (the .bat file is
        # not portable), but keeping the constants module-level rather
        # than os.name-gated keeps the code testable across platforms.
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(
                [str(bat_path)],
                creationflags=flags,
                cwd=str(repo_root),
                close_fds=True,
            )
        except OSError:
            log.warning(
                "Tray: auto-restart spawn failed — current tray will "
                "stay alive rather than dying with no replacement",
                exc_info=True,
            )
            return
        except Exception:
            log.warning(
                "Tray: auto-restart spawn failed (unexpected exception)",
                exc_info=True,
            )
            return
        # New launcher is up — stop the current tray icon. pystray's
        # run() returns once stop() is called, app.py's main() proceeds
        # to its tray-exit handler which shuts the supervisor down
        # cleanly. The new tray we just spawned has its own supervisor.
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                log.warning(
                    "Tray: icon.stop after auto-restart failed",
                    exc_info=True,
                )

    def _start_helix(self, icon, item) -> None:  # noqa: ARG002
        log.info("Tray: starting helix")
        try:
            pid = self.supervisor.start()
            log.info("Tray: helix started (pid=%d)", pid)
        except AlreadyRunning as exc:
            log.warning("Tray start: %s", exc)
        except (SupervisorError, Exception) as exc:
            log.error("Tray start failed: %s", exc, exc_info=True)
        finally:
            self._refresh_menu()

    def _restart_helix(self, icon, item) -> None:  # noqa: ARG002
        log.info("Tray: restarting helix")
        try:
            self.supervisor.restart(reason="manual restart from tray menu")
        except Exception as exc:
            log.error("Tray restart failed: %s", exc, exc_info=True)
        finally:
            self._refresh_menu()

    def _stop_helix(self, icon, item) -> None:  # noqa: ARG002
        log.info("Tray: stopping helix")
        try:
            self.supervisor.stop(reason="manual stop from tray menu")
        except NotRunning as exc:
            log.warning("Tray stop: %s", exc)
        except Exception as exc:
            log.error("Tray stop failed: %s", exc, exc_info=True)
        finally:
            self._refresh_menu()

    def _quit(self, icon, item) -> None:  # noqa: ARG002
        """Stop helix then tear down the tray icon.

        After icon.stop(), pystray's run() returns, main() exits, and
        the process terminates. The uvicorn daemon thread dies with the
        process. If the supervisor is still holding helix, try to stop
        it cleanly first — best-effort, never blocks the quit path.
        """
        log.info("Tray: quit")
        try:
            if self.supervisor.is_running():
                self.supervisor.stop(reason="launcher quit from tray menu")
        except Exception:
            log.warning("Tray quit: helix stop failed (continuing)", exc_info=True)

        # Headroom Quit policy: only stop if we spawned it. Adopted
        # headroom stays alive — the user (or another tool) launched it
        # outside the launcher and Quit shouldn't surprise-kill it.
        if self.headroom is not None:
            try:
                if self.headroom.is_running() and self.headroom.owns_process():
                    log.info("Tray quit: stopping owned Headroom")
                    self.headroom.stop(
                        reason="launcher quit from tray menu",
                        force=False,  # still enforces ownership gate
                    )
                elif self.headroom.is_running():
                    log.info(
                        "Tray quit: leaving adopted Headroom running "
                        "(it was started outside the launcher)"
                    )
            except Exception:
                log.warning(
                    "Tray quit: headroom stop failed (continuing)",
                    exc_info=True,
                )

        if self._on_quit_extra is not None:
            try:
                self._on_quit_extra()
            except Exception:
                log.warning("Tray on_quit hook failed", exc_info=True)

        self._quit_event.set()
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                log.warning("Tray icon.stop failed", exc_info=True)

        # Belt and suspenders: some platforms leave the message pump
        # blocked even after icon.stop(). Send SIGINT as a final nudge
        # so the uvicorn daemon thread and main loop wind down.
        try:
            os.kill(os.getpid(), signal.SIGINT)
        except Exception:
            pass

    # ── menu construction ──────────────────────────────────────────

    def _build_menu(self):
        """Build a fresh pystray.Menu reflecting current helix state.

        pystray reads the menu dynamically from the Icon.menu property
        so re-entering the menu picks up enable/disable state without
        needing an explicit refresh call — but some backends do cache,
        so we also call icon.update_menu() from _refresh_menu.
        """
        import pystray

        running = self.supervisor.is_running()

        items = [
            pystray.MenuItem(
                "Open Dashboard",
                self._open_dashboard,
                default=True,  # click on the tray icon itself triggers this
            ),
        ]
        # Observability links — only shown if URLs were configured. Keeps
        # the menu clean for users who aren't running the OTel stack.
        if self.grafana_url:
            items.append(pystray.MenuItem("Open Grafana", self._open_grafana))
        if self.prometheus_url:
            items.append(pystray.MenuItem("Open Prometheus", self._open_prometheus))
        # Headroom dashboard link — only when wired. Dashboard is reachable
        # whenever headroom is running (whether we spawned it or adopted it).
        if self.headroom is not None and self.headroom_dashboard_url:
            items.append(pystray.MenuItem(
                "Open Headroom Dashboard",
                self._open_headroom_dashboard,
                enabled=lambda item: self.headroom.is_running(),  # noqa: ARG005
            ))
        items.extend([
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start helix",
                self._start_helix,
                enabled=lambda item: not self.supervisor.is_running(),  # noqa: ARG005
            ),
            pystray.MenuItem(
                "Restart helix",
                self._restart_helix,
                enabled=lambda item: self.supervisor.is_running(),  # noqa: ARG005
            ),
            pystray.MenuItem(
                "Stop helix",
                self._stop_helix,
                enabled=lambda item: self.supervisor.is_running(),  # noqa: ARG005
            ),
        ])
        # Headroom lifecycle controls — only surfaced when a supervisor is wired.
        if self.headroom is not None:
            items.extend([
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Start Headroom",
                    self._start_headroom,
                    enabled=lambda item: not self.headroom.is_running(),  # noqa: ARG005
                ),
                pystray.MenuItem(
                    "Restart Headroom",
                    self._restart_headroom,
                    enabled=lambda item: self.headroom.is_running(),  # noqa: ARG005
                ),
                pystray.MenuItem(
                    "Stop Headroom",
                    self._stop_headroom,
                    enabled=lambda item: self.headroom.is_running(),  # noqa: ARG005
                ),
            ])
        # Observability submenu — rendered in two cases (spec §7.5, §11.4
        # + Task 13 fix):
        #   1. Supervisor wired (install complete, services running): per-
        #      service status + restart actions + Open log directory.
        #   2. Supervisor None but install_pending=True (Task 13 — fresh
        #      checkout, binaries missing): Install Observability action +
        #      Dismiss reminder. Without this branch the user sees ONLY
        #      the balloon and has no clickable surface.
        # When supervisor is None AND install_pending is False (e.g. user
        # opted out via HELIX_OBSERVABILITY=0), the submenu is omitted
        # entirely so the menu stays clean.
        if self.observability is not None:
            obs_services = ["collector", "prometheus", "tempo", "loki", "grafana"]

            def _status_label(svc: str):
                return lambda item: f"{svc.capitalize()}: {self.observability.status(svc)}"  # noqa: ARG005

            obs_items = [
                pystray.MenuItem(
                    _status_label(svc), None, enabled=False,
                )
                for svc in obs_services
            ]
            obs_items.append(pystray.Menu.SEPARATOR)
            for svc in obs_services:
                obs_items.append(pystray.MenuItem(
                    f"Restart {svc}", self._restart_obs_service(svc),
                ))
            obs_items.append(pystray.Menu.SEPARATOR)
            obs_items.append(pystray.MenuItem(
                "Open log directory", self._open_obs_log_dir,
            ))
            # Conditional "Dismiss install reminder" item — only visible
            # while the install-prompt pulse is active (spec §11.4).
            obs_items.append(pystray.MenuItem(
                "Dismiss install reminder",
                self._dismiss_install_pulse,
                visible=lambda item: self._install_pulse_active,  # noqa: ARG005
            ))
            # Top-level "Observability" label is callable so it can render
            # the pulse suffix (● / ○) without rebuilding the whole menu.
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem(
                self._observability_label,
                pystray.Menu(*obs_items),
            ))
        elif self._install_pending:
            # Task 13 fix path: no supervisor (install incomplete) but
            # the caller flagged install_pending. Surface a minimal
            # submenu with the Install action + Dismiss reminder.
            obs_items = [
                pystray.MenuItem(
                    "Install Observability...",
                    self._run_install_observability,
                ),
                pystray.MenuItem(
                    "Dismiss install reminder",
                    self._dismiss_install_pulse,
                    visible=lambda item: self._install_pulse_active,  # noqa: ARG005
                ),
            ]
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem(
                self._observability_label,
                pystray.Menu(*obs_items),
            ))

        items.extend([
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        ])
        return pystray.Menu(*items)

    def notify_install_needed(self) -> None:
        """Show a Windows balloon prompting the user to install native
        observability binaries. Called by app.py after detecting the
        bootstrap is missing or incomplete (spec §11.4).

        Also starts the Observability submenu label pulse (spec §11.4 —
        "balloon notification + tray-menu pulse"). Pulse runs until the
        user clicks an observability submenu item (acknowledgment) or
        explicitly dismisses it. Dismissal persists for this process
        lifetime — pulse will only re-appear on a future launch if the
        bootstrap is still incomplete.
        """
        if self._icon is None:
            return
        try:
            # pystray's notify is a no-op on backends that don't support it.
            self._icon.notify(
                "Native observability not installed — "
                "right-click the tray icon, choose Observability ▸ "
                "Install, or run scripts/install-native-observability.ps1",
                title="Helix Launcher",
            )
        except Exception:
            log.warning("notify_install_needed failed", exc_info=True)
        # Pulse the Observability submenu label until acknowledged.
        self.start_install_pulse()

    # ── install-prompt pulse (spec §11.4) ──────────────────────────

    def _observability_label(self, item) -> str:  # noqa: ARG002 — pystray API
        """Compute the Observability submenu's top-level label.

        When idle: "Observability". When pulsing: alternates between
        "Observability ●" (filled) and "Observability ○" (open) on each
        timer tick. The label is rendered lazily by pystray on every
        menu re-display, so update_menu() after a state toggle is enough
        to refresh the visible label.
        """
        with self._install_pulse_lock:
            active = self._install_pulse_active
            state = self._install_pulse_state
        if not active:
            return "Observability"
        return "Observability ●" if state == 0 else "Observability ○"

    def start_install_pulse(self) -> None:
        """Begin the Observability submenu label pulse. Idempotent — a
        second call while already pulsing is a no-op. No-op if the user
        has already dismissed the pulse during this process lifetime."""
        with self._install_pulse_lock:
            if self._install_pulse_dismissed:
                log.debug("Tray: install-pulse suppressed (already dismissed)")
                return
            if self._install_pulse_active:
                return
            self._install_pulse_active = True
            self._install_pulse_state = 0
        self._schedule_pulse_tick()
        self._refresh_menu()

    def stop_install_pulse(self) -> None:
        """Stop the pulse and cancel the active timer. Idempotent — safe
        to call repeatedly and from any thread."""
        with self._install_pulse_lock:
            self._install_pulse_active = False
            timer = self._install_pulse_timer
            self._install_pulse_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                log.debug("Tray: pulse timer cancel failed", exc_info=True)
        self._refresh_menu()

    def _schedule_pulse_tick(self) -> None:
        """Arm the next 1 s pulse tick. The timer is daemon=True so it
        never blocks process exit."""
        timer = threading.Timer(1.0, self._tick_pulse)
        timer.daemon = True
        with self._install_pulse_lock:
            # If a stop raced us, abandon this scheduled timer.
            if not self._install_pulse_active:
                return
            self._install_pulse_timer = timer
        try:
            timer.start()
        except Exception:
            log.warning("Tray: failed to start pulse timer", exc_info=True)

    def _tick_pulse(self) -> None:
        """Toggle the pulse state, refresh the menu, and reschedule.

        Runs on the timer thread. Aborts cleanly if the pulse was stopped
        between the previous tick scheduling and this callback firing.
        """
        with self._install_pulse_lock:
            if not self._install_pulse_active:
                return
            self._install_pulse_state = 1 - self._install_pulse_state
        # Refresh the menu so pystray re-evaluates the callable label.
        self._refresh_menu()
        # Reschedule the next tick.
        self._schedule_pulse_tick()

    def _refresh_menu(self) -> None:
        if self._icon is not None:
            try:
                self._icon.update_menu()
            except Exception:
                log.debug("Tray menu refresh failed", exc_info=True)

    # ── lifecycle ──────────────────────────────────────────────────

    def run(self) -> None:
        """Blocking — runs the tray on the current thread until Quit.

        Call this from the main thread. It returns when the user clicks
        Quit from the tray menu.
        """
        import pystray

        image = _build_icon_image()
        self._icon = pystray.Icon(
            name=self.name,
            icon=image,
            title=self.tooltip,
            menu=self._build_menu(),
        )
        log.info("Tray icon running (dashboard=%s)", self.dashboard_url)
        self._icon.run()

    def quit_event(self) -> threading.Event:
        """Event set when Quit is clicked — other threads can wait on it."""
        return self._quit_event

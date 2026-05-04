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
import sys
import threading
import webbrowser
from typing import Callable, Optional

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
        self._icon = None  # type: ignore[assignment]
        self._quit_event = threading.Event()

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
            log.info("Tray: restart observability/%s", service)
            try:
                self.observability.restart_service(service)
            except Exception:
                log.warning("Tray restart obs/%s failed", service, exc_info=True)
            finally:
                self._refresh_menu()
        return _h

    def _open_obs_log_dir(self, icon, item):  # noqa: ARG002
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
        # Observability submenu — only when an ObservabilitySupervisor is
        # wired (spec §7.5). Per-service status entries are disabled menu
        # items whose labels are computed lazily so the green/red dots
        # reflect the latest health-loop snapshot every time the user
        # opens the menu.
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
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem(
                "Observability", pystray.Menu(*obs_items),
            ))

        items.extend([
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        ])
        return pystray.Menu(*items)

    def notify_install_needed(self) -> None:
        """Show a Windows balloon prompting the user to install native
        observability binaries. Called by app.py after detecting the
        bootstrap is missing or incomplete (spec §11.4)."""
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

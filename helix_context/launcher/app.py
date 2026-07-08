"""
Launcher FastAPI app + CLI entry point.

Run via the ``helix-launcher`` console script. See ``docs/LAUNCHER.md``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .collector import StateCollector
from .state import StateStore
from .supervisor import (
    AlreadyRunning,
    HelixSupervisor,
    NotRunning,
    ShutdownTimeout,
    SupervisorError,
)
from .update_check import UpdateChecker
from .headroom_supervisor import (
    HeadroomSupervisor,
    HeadroomNotInstalled,
    is_headroom_installed,
)
from .observability_paths import (
    ALL_CONFIG_FILES,
    ALL_SERVICES,
    binary_path,
    configs_dir,
)

log = logging.getLogger("helix.launcher.app")

DEFAULT_GRAFANA_URL = "http://127.0.0.1:3000/d/helix-overview/helix-overview"
DEFAULT_PROMETHEUS_URL = "http://127.0.0.1:9090/graph"

if TYPE_CHECKING:
    from .observability_supervisor import ObservabilitySupervisor

LAUNCHER_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = LAUNCHER_DIR / "templates"
STATIC_DIR = LAUNCHER_DIR / "static"


def _get_templates():
    """Lazy-import Jinja2 so the module loads without the [launcher] extra."""
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except ImportError as e:
        raise SupervisorError(
            "jinja2 is required. Install with: pip install helix-context[launcher]"
        ) from e
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env


def create_app(
    store: StateStore,
    supervisor: HelixSupervisor,
    collector: StateCollector,
    observability: Optional["ObservabilitySupervisor"] = None,
    observability_install_pending: bool = False,
    grafana_url: str = DEFAULT_GRAFANA_URL,
    prometheus_url: str = DEFAULT_PROMETHEUS_URL,
    bench_supervisor: Optional[HelixSupervisor] = None,
    bench_genome_path: str = "",
    needs_db_selection: bool = False,
) -> FastAPI:
    """Build the launcher FastAPI app."""
    templates = _get_templates()

    # --grafana-url / --prometheus-url default to None outside tray mode;
    # normalize so the Monitoring links always have a destination.
    grafana_url = grafana_url or DEFAULT_GRAFANA_URL
    prometheus_url = prometheus_url or DEFAULT_PROMETHEUS_URL

    # Known provisioned dashboards (deploy/otel/grafana/dashboards/*.json).
    # uid -> human title; rendered as direct links on the Monitoring tab.
    _grafana_base = grafana_url.split("/d/")[0].rstrip("/")
    _telem_dashboards = [
        ("helix-overview", "Overview"),
        ("helix-a27094-pipeline-observatory", "Pipeline Observatory"),
        ("helix-retrieval-hitl", "Retrieval + HITL"),
        ("helix-agent-usage", "Agent Usage"),
        ("helix-genai", "GenAI (gen_ai.*)"),
        ("helix-internals", "Internals & Research"),
    ]

    def _observability_state() -> Optional[dict]:
        """Launcher-side observability snapshot injected into the page
        state — service health from the supervisor plus the telemetry
        links the Monitoring tab renders. None when the operator opted
        out via HELIX_OBSERVABILITY=0 (panel hidden entirely)."""
        if observability is None and not observability_install_pending:
            return None
        statuses = (
            observability.all_statuses() if observability is not None else {}
        )
        return {
            "install_pending": observability_install_pending,
            "services": [
                {"name": name, "status": status}
                for name, status in sorted(statuses.items())
            ],
            "links": {
                "grafana": grafana_url,
                "grafana_base": _grafana_base,
                "prometheus": prometheus_url,
                "dashboards": [
                    {"title": title, "url": f"{_grafana_base}/d/{uid}"}
                    for uid, title in _telem_dashboards
                ],
            },
        }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # On startup, try to adopt an already-running helix.
        try:
            supervisor.adopt()
        except Exception:
            log.warning("Adoption check failed", exc_info=True)
        yield
        # On shutdown, stop only processes this launcher spawned itself.
        # Adopted Helix instances should keep running when the launcher exits.
        if supervisor.is_running() and supervisor.owns_process():
            try:
                log.info("Launcher shutting down — stopping helix")
                supervisor.stop(reason="launcher shutdown")
            except Exception:
                log.warning("Graceful helix stop failed during launcher shutdown", exc_info=True)
        elif supervisor.is_running():
            log.info("Launcher shutting down — leaving adopted helix running")

    app = FastAPI(title="Helix Launcher", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.supervisor = supervisor
    app.state.collector = collector

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ── dashboard HTML ─────────────────────────────────────────────

    def _enrich(state: dict) -> dict:
        state["observability"] = _observability_state()
        # Startup-UX (v0.7.0): surface "alive but not answering yet" so the
        # dashboard can render a loading state instead of "stopped".
        try:
            # Strict identity: test doubles (MagicMock) must not render
            # the starting panel by accident.
            state.setdefault("helix", {})["start_pending"] = (
                supervisor.last_start_pending is True
            )
        except Exception:
            state.setdefault("helix", {})["start_pending"] = False
        # First-boot db selection: stays true until a genome is selected
        # (the select/create endpoints start helix, which flips running).
        state["needs_db_selection"] = bool(
            needs_db_selection and not state.get("helix", {}).get("running"),
        )
        if bench_supervisor is not None:
            state["bench"] = {
                "running": bench_supervisor.is_running(),
                "port": bench_supervisor.helix_port,
                "genome": bench_genome_path,
            }
        else:
            state["bench"] = None
        return state

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_root(request: Request) -> HTMLResponse:
        state = _enrich(collector.collect())
        template = templates.get_template("dashboard.html")
        html = template.render(state=state, launcher_port=_launcher_port(request))
        return HTMLResponse(html)

    @app.get("/api/state/panels", response_class=HTMLResponse)
    async def panels_partial(request: Request):
        """Server-rendered HTML partial — just the panels, for polling."""
        if request.headers.get("sec-fetch-mode") == "navigate":
            return RedirectResponse("/", status_code=303)
        state = _enrich(collector.collect())
        template = templates.get_template("components/panels.html")
        html = template.render(state=state)
        return HTMLResponse(html)

    # ── JSON state API ─────────────────────────────────────────────

    @app.get("/api/state")
    async def api_state():
        return _enrich(collector.collect())

    # ── bench instance controls (dev/configuration mode) ──────────

    @app.post("/api/control/bench/start")
    async def api_bench_start():
        if bench_supervisor is None:
            return JSONResponse(
                {"ok": False, "error": "bench mode is disabled "
                 "([server] bench_enabled = false)"},
                status_code=409,
            )
        try:
            pid = bench_supervisor.start()
            return {"ok": True, "pid": pid}
        except AlreadyRunning as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except SupervisorError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.post("/api/control/bench/stop")
    async def api_bench_stop():
        if bench_supervisor is None:
            return JSONResponse(
                {"ok": False, "error": "bench mode is disabled"},
                status_code=409,
            )
        try:
            bench_supervisor.stop(reason="manual bench stop from launcher UI")
            return {"ok": True}
        except NotRunning as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except SupervisorError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # ── genome management (v0.7.0: dashboard parity with the tray's
    #    "Manage Database" submenu — select or create from the web UI) ──

    @app.get("/api/genomes")
    async def api_genomes():
        from . import genome_registry
        try:
            infos = genome_registry.discover_genomes()
            return {
                "ok": True,
                "active": str(genome_registry.active_genome_path()),
                "genomes": [i.as_dict() for i in infos],
            }
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)}, status_code=500,
            )

    def _select_and_restart(target: Path) -> JSONResponse:
        from . import genome_registry
        try:
            resolved = genome_registry.select_genome(target)
        except FileNotFoundError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        genome_registry.clear_cache()

        def _worker() -> None:
            try:
                if supervisor.is_running():
                    supervisor.restart(
                        reason=f"switch genome to {resolved.name} (dashboard)",
                    )
                else:
                    supervisor.start()
            except Exception:
                log.error(
                    "Genome switch restart failed (%s)", resolved, exc_info=True,
                )

        threading.Thread(
            target=_worker, name="helix-genome-switch-web", daemon=True,
        ).start()
        return JSONResponse(
            {"ok": True, "selected": str(resolved), "restarting": True},
            status_code=202,
        )

    @app.post("/api/genome/select")
    async def api_genome_select(request: Request):
        body = await request.json()
        raw = (body or {}).get("path", "")
        if not raw:
            return JSONResponse(
                {"ok": False, "error": "missing 'path'"}, status_code=400,
            )
        return _select_and_restart(Path(raw))

    @app.post("/api/genome/create")
    async def api_genome_create(request: Request):
        body = await request.json()
        raw = (body or {}).get("path", "")
        if not raw:
            return JSONResponse(
                {"ok": False, "error": "missing 'path'"}, status_code=400,
            )
        target = Path(raw).expanduser()
        if target.suffix != ".db":
            return JSONResponse(
                {"ok": False, "error": "genome path must end in .db"},
                status_code=400,
            )
        if target.exists():
            return JSONResponse(
                {"ok": False, "error": f"already exists: {target}"},
                status_code=409,
            )
        # Schema construction imports the full knowledge-store stack
        # (hardware probe and friends) — far too heavy for the event
        # loop. Validate cheaply here, do the build + select + restart in
        # a worker thread, and answer 202 immediately; the dashboard's
        # poll shows the switch when it lands. (v0.7.0 review fix: the
        # first cut blocked every launcher request mid-create.)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return JSONResponse(
                {"ok": False, "error": f"cannot create parent dir: {exc}"},
                status_code=500,
            )

        def _create_worker() -> None:
            from . import genome_registry
            try:
                from ..knowledge_store import Genome
                g = Genome(path=str(target), synonym_map={})
                g.close()
                resolved = genome_registry.select_genome(target)
                genome_registry.clear_cache()
                if supervisor.is_running():
                    supervisor.restart(
                        reason=f"create+switch genome {resolved.name} (dashboard)",
                    )
                else:
                    supervisor.start()
            except Exception:
                log.error(
                    "Genome create+switch failed (%s)", target, exc_info=True,
                )

        threading.Thread(
            target=_create_worker, name="helix-genome-create-web", daemon=True,
        ).start()
        return JSONResponse(
            {"ok": True, "creating": str(target), "restarting": True},
            status_code=202,
        )

    # ── control endpoints ──────────────────────────────────────────

    @app.post("/api/control/start")
    async def api_control_start():
        try:
            pid = supervisor.start()
        except AlreadyRunning as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except SupervisorError as exc:
            # NB: StartupTimeout is no longer raised by start() (PR #68); it is
            # surfaced via ``supervisor.last_start_pending`` below. SupervisorError
            # still covers port-collisions and non-timeout failures.
            return JSONResponse({"error": str(exc)}, status_code=500)
        # PR #68 made cold-start /stats timeout non-fatal — proc is left running
        # for the tray's next poll. REST callers need to distinguish "ready"
        # from "alive-but-not-ready" so an external automation doesn't treat
        # a hung backend as success (closes #72).
        if supervisor.last_start_pending:
            return JSONResponse(
                {
                    "ok": True,
                    "pid": pid,
                    "started_pending": True,
                    "message": (
                        "Backend pid was spawned but /stats did not answer "
                        "within the wait timeout. The process is still alive; "
                        "poll /api/state or GET /stats to confirm readiness."
                    ),
                },
                status_code=202,
            )
        return {"ok": True, "pid": pid, "started_pending": False}

    @app.post("/api/control/stop")
    async def api_control_stop():
        try:
            supervisor.stop(reason="manual stop from launcher UI")
            return {"ok": True}
        except NotRunning as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ShutdownTimeout as exc:
            return JSONResponse({"error": str(exc)}, status_code=408)
        except SupervisorError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.post("/api/control/restart")
    async def api_control_restart():
        try:
            pid = supervisor.restart(reason="manual restart from launcher UI")
        except (ShutdownTimeout, SupervisorError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        # Same alive-but-not-ready surface as /api/control/start (closes #72).
        if supervisor.last_start_pending:
            return JSONResponse(
                {
                    "ok": True,
                    "pid": pid,
                    "started_pending": True,
                    "message": (
                        "Backend pid was respawned but /stats did not answer "
                        "within the wait timeout. The process is still alive; "
                        "poll /api/state or GET /stats to confirm readiness."
                    ),
                },
                status_code=202,
            )
        return {"ok": True, "pid": pid, "started_pending": False}

    return app


def _launcher_port(request: Request) -> int:
    """Extract the port the launcher is running on from the request."""
    try:
        return request.url.port or 11438
    except Exception:
        return 11438


# ── CLI entry ──────────────────────────────────────────────────────


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="helix-launcher",
        description="Supervisor + dashboard for a helix-context server.",
    )
    p.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "install-service", "uninstall-service"],
        help="Subcommand. 'run' (default) starts the launcher. "
             "'install-service' writes a systemd/launchd service file for "
             "the current platform. 'uninstall-service' removes it.",
    )
    p.add_argument("--host", default="127.0.0.1", help="Launcher UI bind host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=11438, help="Launcher UI port (default: 11438)")
    p.add_argument(
        "--bench", action="store_true",
        help="Dev mode: also supervise a second helix on the bench port "
             "bound to the bench genome ([server] bench_* in helix.toml; "
             "overrides bench_enabled=false).",
    )
    p.add_argument(
        "--log-file", type=str, default=None,
        help="Append launcher logs to this file (enables fully headless "
             "pythonw starts where stderr has nowhere to go).",
    )
    p.add_argument("--helix-host", default="127.0.0.1", help="Host for supervised helix (default: 127.0.0.1)")
    p.add_argument("--helix-port", type=int, default=11437, help="Port for supervised helix (default: 11437)")
    p.add_argument("--no-autostart", action="store_true", help="Don't spawn helix on launcher start")
    p.add_argument("--no-browser", action="store_true", help="Don't open the dashboard in a browser")
    p.add_argument("--native", action="store_true", help="Use pywebview native window instead of browser")
    p.add_argument(
        "--tray", action="store_true",
        help="Run with a system tray icon (pystray required). "
             "Click the tray icon to open the dashboard, click Quit from "
             "its menu to stop the launcher. Mutually exclusive with --native.",
    )
    p.add_argument(
        "--ollama-base-url",
        default="http://127.0.0.1:11434",
        help="Ollama base URL for model discovery (default: http://127.0.0.1:11434)",
    )
    p.add_argument(
        "--grafana-url",
        default=None,
        help="If set, tray menu gains 'Open Grafana' item pointing here "
             "(e.g. http://localhost:3000/d/helix-overview/helix-overview)",
    )
    p.add_argument(
        "--prometheus-url",
        default=None,
        help="If set, tray menu gains 'Open Prometheus' item pointing here "
             "(e.g. http://localhost:9090/graph)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="For install-service / uninstall-service: show what would happen without making changes",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return p.parse_args(argv)


def _check_native_available() -> bool:
    """Return True if pywebview can be imported. Used to fail-fast in main()."""
    try:
        import webview  # noqa: F401  # type: ignore
        return True
    except ImportError:
        return False


def _check_tray_available() -> bool:
    """Return True if pystray + PIL can be imported. Used for --tray fail-fast."""
    from .tray import is_tray_available
    return is_tray_available()


def _open_ui(url: str, native: bool, window_title: str = "Helix Launcher") -> None:
    """Open the dashboard — browser tab (default) or native webview window.

    This is a blocking call when native=True (webview owns the main thread
    until the user closes the window). Browser mode is non-blocking — it
    just opens a tab and returns.

    Caller is responsible for verifying pywebview availability via
    _check_native_available() BEFORE starting any background work, so
    that --native with no pywebview installed fails fast.
    """
    if native:
        import webview  # noqa: F401  # type: ignore  -- caller already verified
        webview.create_window(
            window_title, url, width=1000, height=720, resizable=True,
        )
        webview.start()
    else:
        try:
            webbrowser.open(url)
        except Exception:
            log.warning("Failed to open browser — navigate manually to %s", url)


def _env_truthy(name: str) -> Optional[bool]:
    """Parse an env var as tristate: True/False/None (unset)."""
    v = os.environ.get(name)
    if v is None:
        return None
    return v.strip().lower() in ("1", "true", "yes", "on")


def _is_loopback_host(host: Optional[str]) -> bool:
    return (host or "").strip().lower() in {"127.0.0.1", "localhost", "::1"}


def _should_route_helix_upstream_via_headroom(cfg, auto_override: Optional[bool] = None) -> bool:
    """Decide whether the launcher should rewrite helix's chat upstream
    to dial the local Headroom proxy.

    Precedence (highest wins):

      1. ``auto_override`` (from ``HELIX_HEADROOM_ROUTE_UPSTREAM_AUTO``):
         truthy forces ON, falsy forces OFF, ``None`` defers to config.
      2. ``cfg.headroom.route_upstream``: must be ``True`` to route.
         Defaults False so a fresh install never silently rewrites the
         upstream to a proxy that isn't running. Separate from
         ``cfg.headroom.enabled`` (which is the proxy *lifecycle* switch)
         so an operator can run the proxy + dashboard without rerouting
         chat, or vice versa.
      3. Upstream must be a parseable non-loopback URL (local model
         servers like Ollama on localhost:11434 stay direct).
      4. Upstream must not already point at the configured Headroom
         (avoid double-route).
    """
    if auto_override is False:
        return False
    if auto_override is None and not getattr(cfg.headroom, "route_upstream", False):
        return False
    # auto_override is True OR config explicitly opted in.

    upstream = str(cfg.server.upstream or "").strip()
    if not upstream:
        return False

    parsed = urlparse(upstream)
    if not parsed.scheme or not parsed.hostname:
        return False

    if _is_loopback_host(parsed.hostname):
        return False

    headroom_host = getattr(cfg.headroom, "host", "127.0.0.1")
    headroom_port = int(getattr(cfg.headroom, "port", 8787))
    if parsed.hostname == headroom_host and parsed.port == headroom_port:
        return False

    return True


def _configure_helix_upstream_routing(cfg, auto_override: Optional[bool] = None) -> bool:
    """Set env overrides so Helix optionally routes chat upstream via Headroom.

    Returns True when Helix should point at the local Headroom proxy.
    """
    route_via_headroom = _should_route_helix_upstream_via_headroom(
        cfg, auto_override=auto_override,
    )
    if route_via_headroom:
        headroom_base = f"http://{cfg.headroom.host}:{cfg.headroom.port}"
        os.environ["OPENAI_TARGET_API_URL"] = str(cfg.server.upstream).rstrip("/")
        os.environ["HELIX_SERVER_UPSTREAM"] = headroom_base
        log.info(
            "Helix upstream auto-route ON: %s -> %s",
            cfg.server.upstream,
            headroom_base,
        )
        return True

    # Clear launcher-managed routing so local upstreams stay direct.
    os.environ.pop("HELIX_SERVER_UPSTREAM", None)
    os.environ.pop("OPENAI_TARGET_API_URL", None)
    log.info("Helix upstream auto-route OFF: using direct upstream %s", cfg.server.upstream)
    return False


def _maybe_build_headroom(
    store,
    autostart_override: Optional[bool] = None,
    enabled_override: Optional[bool] = None,
) -> tuple[Optional["HeadroomSupervisor"], Optional[str]]:
    """Build a HeadroomSupervisor + dashboard URL, or (None, None) if
    the feature isn't enabled for this environment.

    Resolution order:
        1. `helix-context[codec]` must be installed (headroom importable)
        2. Try to adopt an existing headroom proxy on the configured port.
        3. If no orphan found, require `[headroom] enabled = true` in
           helix.toml (or HELIX_HEADROOM_ENABLED=1).
        4. If enabled AND no orphan found AND
           (autostart=true OR HELIX_HEADROOM_AUTOSTART=1),
           spawn a new headroom child.

    Never raises — returns (None, None) on any failure. Headroom is an
    optional enhancement; a broken install should never block launcher start.
    """
    if not is_headroom_installed():
        log.debug("Headroom not installed — skipping headroom supervisor")
        return None, None

    try:
        from helix_context.config import load_config
        cfg = load_config()
        hcfg = cfg.headroom
    except Exception as exc:
        log.warning("Headroom: failed to load config, skipping (%s)", exc)
        return None, None

    headroom = HeadroomSupervisor(
        store=store,
        host=hcfg.host,
        port=hcfg.port,
        mode=hcfg.mode,
    )
    dashboard_url = f"http://{hcfg.host}:{hcfg.port}{hcfg.dashboard_path}"

    # Stage 1: adopt if a headroom is already running.
    if headroom.adopt():
        log.info(
            "Headroom: adopted existing process on %s:%d — will NOT stop on Quit",
            hcfg.host, hcfg.port,
        )
        return headroom, dashboard_url

    enabled = hcfg.enabled if enabled_override is None else enabled_override
    if not enabled:
        log.debug(
            "Headroom: [headroom] enabled=false and no running proxy found — skipping"
        )
        return None, None

    autostart = hcfg.autostart if autostart_override is None else autostart_override
    if autostart:
        # Stage 2: spawn a new one.
        try:
            log.info("Headroom: starting on %s:%d (mode=%s)",
                     hcfg.host, hcfg.port, hcfg.mode)
            headroom.start()
        except HeadroomNotInstalled:
            log.warning("Headroom: package not installed; disabling")
            return None, None
        except Exception as exc:
            log.warning("Headroom: autostart failed (%s); continuing without", exc)
            # Keep the supervisor so the tray still shows Start Headroom.

    return headroom, dashboard_url


def _observability_install_complete() -> bool:
    """True iff every binary AND every rendered config is present."""
    if not all(binary_path(s).exists() for s in ALL_SERVICES):
        return False
    cfg = configs_dir()
    return all((cfg / r).exists() for r in ALL_CONFIG_FILES)


def _should_skip_observability() -> bool:
    """True iff HELIX_OBSERVABILITY is explicitly set to an opt-out token.

    The default is opt-IN: unset or unrecognized strings yield False
    (run observability). The opt-out vocabulary is "0"/"false"/"no"/"off"
    (case-insensitive). Distinct from `_env_truthy` semantics (which
    matches OPT-IN tokens — the inverse vocabulary), so we keep this as
    a small named helper rather than forcing a negate-of-truthy fit.
    """
    return os.environ.get("HELIX_OBSERVABILITY", "1").strip().lower() in (
        "0", "false", "no", "off",
    )


def _maybe_build_observability() -> tuple[
    Optional["ObservabilitySupervisor"], bool,
]:
    """Return (supervisor, install_pending).

    supervisor is None when:
        - HELIX_OBSERVABILITY is opt-out (install_pending=False)
        - install is incomplete (install_pending=True — tray will balloon)
        - import error / extras not installed (install_pending=False)

    install_pending is True only when the bin/config layout is incomplete
    and the caller should schedule the install-needed balloon.
    """
    if _should_skip_observability():
        log.info("Observability skipped: HELIX_OBSERVABILITY=0")
        return None, False

    if not _observability_install_complete():
        log.info(
            "Observability install incomplete; tray will surface a balloon. "
            "Run scripts/install-native-observability.ps1 to enable."
        )
        return None, True

    try:
        from .observability_supervisor import ObservabilitySupervisor
        return ObservabilitySupervisor(), False
    except ImportError:
        log.warning(
            "Observability deps missing — install with "
            "pip install helix-context[launcher-tray]",
            exc_info=True,
        )
        return None, False


def _export_otel_env_for_backend() -> None:
    """Point the helix child's OTel exporter at the observability stack.

    Called after ObservabilitySupervisor.start_all() returns without
    raising (services spawned or adopted as external). Without this, the
    default boot ran a full Grafana/Tempo/Loki/Prometheus stack while the
    backend emitted nothing — HELIX_OTEL_ENABLED defaults off — so a
    fresh install got an empty helix-overview dashboard.

    Mutates the launcher's own os.environ BEFORE the helix child spawns;
    HelixSupervisor.start() passes the environment through (env=None
    inherits, extra_env copies os.environ first), so main + bench children
    both pick it up, as do tray-menu restarts.

    Guards:
    - An EXPLICIT user HELIX_OTEL_ENABLED — on or off — is never
      overridden. (Env-over-toml precedence is otherwise intentional:
      the export must beat the shipped ``[telemetry] enabled=false``
      default.)
    - The collector's OTLP port must actually accept connections.
      start_all() returning is NOT proof of that — a service that spawns
      but never becomes ready is logged STATUS_RED and start_all returns
      normally, and ``_maybe_external`` can be satisfied by a squatter on
      any collector port (e.g. :8889 alone). A backend dialing a dead
      :4317 wedges its gRPC channel and drops metrics even after the
      collector later binds, so no reachable port -> no export.

    The endpoint is deliberately NOT exported: the backend's own
    resolution chain (env > [telemetry] toml > default) already lands on
    localhost:4317, and a synthesized env endpoint would override an
    explicit user [telemetry] endpoint in helix.toml.
    """
    if os.environ.get("HELIX_OTEL_ENABLED", "").strip():
        return
    from .observability_health import SERVICE_PORTS, is_port_bound
    otlp_port = SERVICE_PORTS["collector"][0]
    if not is_port_bound("127.0.0.1", otlp_port):
        log.warning(
            "Observability start reported success but the collector OTLP "
            "port :%d is not accepting connections — NOT exporting "
            "HELIX_OTEL_ENABLED for the helix child (a backend dialing a "
            "dead collector wedges its gRPC channel)",
            otlp_port,
        )
        return
    os.environ["HELIX_OTEL_ENABLED"] = "1"
    log.info(
        "Observability stack up — exported HELIX_OTEL_ENABLED=1 for helix "
        "children spawned by this launcher (collector :%d)",
        otlp_port,
    )


def _start_observability_stack(
    observability_sup: Optional["ObservabilitySupervisor"],
) -> None:
    """start_all() + on success export the OTel env for the helix child.

    A failed start deliberately skips the export — pointing the backend
    at a dead collector wedges its gRPC channel (see the start-order
    comment at the call site).
    """
    if observability_sup is None:
        return
    try:
        observability_sup.start_all()
    except Exception:
        log.warning(
            "ObservabilitySupervisor.start_all failed; tray will "
            "indicate via per-service red status",
            exc_info=True,
        )
    else:
        _export_otel_env_for_backend()


def _handle_service_command(command: str, dry_run: bool, port: int = 11438) -> int:
    """Handle install-service / uninstall-service subcommands.

    Returns exit code 0 on success, non-zero on failure. Prints the
    result (which includes next-step instructions) to stdout.
    """
    from .installer import install_service, uninstall_service
    if command == "install-service":
        ok, msg = install_service(dry_run=dry_run, port=port)
    else:  # uninstall-service
        ok, msg = uninstall_service(dry_run=dry_run)
    print(msg)
    return 0 if ok else 1


def _configure_logging(verbose: bool) -> None:
    """Attach a console handler AND a rotating file handler at
    ``~/.helix/launcher/launcher.log``.

    Without the file handler, autostart failures are invisible — the
    ``start "..." /B python -m helix_context.launcher.app`` invocation in
    ``start-helix-tray.bat`` redirects stdout/stderr to the calling cmd
    window, which exits immediately, so any WARN/ERROR from
    ``_maybe_build_headroom`` or ``supervisor.start()`` is lost.
    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "[%(asctime)s] %(name)s %(levelname)s: %(message)s"
    root = logging.getLogger()
    root.setLevel(level)
    # Drop any handlers basicConfig may have left so we own configuration.
    for h in list(root.handlers):
        root.removeHandler(h)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(stream_handler)

    try:
        from logging.handlers import RotatingFileHandler

        log_dir = Path.home() / ".helix" / "launcher"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "launcher.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(fmt))
        root.addHandler(file_handler)
    except Exception as exc:
        # Console-only is acceptable; don't fail launcher boot for a log file.
        log.warning("launcher.log file handler unavailable: %s", exc)


def _ensure_streams(log_file: Optional[str]) -> None:
    """Give the process usable stdio under pythonw (v0.7.0 hotfix).

    A detached ``pythonw`` start has ``sys.stdout``/``sys.stderr`` set to
    None; uvicorn's default logging config writes to those streams, so
    the launcher's own uvicorn thread died before binding the UI port
    (observed: ":11438 did not bind within 3.0s" with no traceback).
    Route the streams into the --log-file (or os.devnull) so every
    print/StreamHandler in the stack stays harmless when headless.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    target = log_file or os.devnull
    try:
        sink = open(target, "a", encoding="utf-8", buffering=1)
    except OSError:
        sink = open(os.devnull, "a", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink


def main(argv: Optional[list] = None) -> int:
    args = _parse_args(argv)

    if args.log_file:
        _fh = logging.FileHandler(args.log_file, encoding="utf-8")
        _fh.setFormatter(logging.Formatter(
            "[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        ))
        logging.getLogger().addHandler(_fh)
        logging.getLogger().setLevel(logging.INFO)
    _ensure_streams(args.log_file)

    _configure_logging(verbose=args.verbose)

    # Service install/uninstall subcommands — do not start any server.
    if args.command in ("install-service", "uninstall-service"):
        return _handle_service_command(args.command, dry_run=args.dry_run, port=args.port)

    # --tray and --native combined is only supported on Windows today.
    #
    # Approach: pywebview on main thread, pystray on a detached background
    # thread. Windows allows pystray's Win32 message pump to run outside
    # the main thread when spawned via threading.Thread + icon.run(). On
    # macOS this is fundamentally impossible because pystray's Cocoa
    # backend requires main-thread + NSApplication event loop, which
    # conflicts with pywebview's WebKit main loop. On Linux it depends
    # on the pystray backend (AppIndicator works from a thread, Xlib
    # does not), so we reject it too — opt into it explicitly later
    # once the Linux-backend story is tested.
    if args.tray and args.native:
        if sys.platform != "win32":
            log.error(
                "--tray --native is only supported on Windows in this "
                "release. On macOS and Linux, pystray's backend needs "
                "the main thread and cannot be combined with pywebview. "
                "Pick --tray or --native, not both."
            )
            return 2

    # Fail fast if --native is requested but pywebview isn't installed.
    if args.native and not _check_native_available():
        log.error(
            "--native requires pywebview. Install with: "
            "pip install helix-context[launcher-native]"
        )
        return 1

    # Fail fast if --tray is requested but pystray/PIL aren't installed.
    if args.tray and not _check_tray_available():
        log.error(
            "--tray requires pystray + Pillow. Install with: "
            "pip install helix-context[launcher-tray]"
        )
        return 1

    store = StateStore()
    store.set_launcher(pid=_current_pid())

    from helix_context.config import load_config
    runtime_cfg = load_config()
    route_helix_via_headroom = _configure_helix_upstream_routing(
        runtime_cfg,
        auto_override=_env_truthy("HELIX_HEADROOM_ROUTE_UPSTREAM_AUTO"),
    )

    supervisor = HelixSupervisor(
        store=store,
        helix_host=args.helix_host,
        helix_port=args.helix_port,
    )

    # ── dev/configuration mode: optional second helix on the bench port ──
    # Chat stays attached to the MAIN genome on the main port; a subagent
    # can point the bench-harness at the bench port without the two
    # instances sharing a knowledge store. Final deployments leave
    # [server] bench_enabled = false (and pass no --bench) and get exactly
    # one server.
    bench_supervisor: Optional[HelixSupervisor] = None
    bench_genome_path = ""
    try:
        from ..config import load_config as _load_config
        _cfg = _load_config()
        _bench_on = bool(args.bench or _cfg.server.bench_enabled)
        if _bench_on:
            bench_genome_path = _cfg.server.bench_genome_path
            from .state import StateStore as _StateStore
            _bench_store = _StateStore(
                path=Path.home() / ".helix" / "launcher" / "bench-state.json",
            )
            bench_supervisor = HelixSupervisor(
                store=_bench_store,
                helix_host=args.helix_host,
                helix_port=_cfg.server.bench_port,
                helix_log_path=(
                    Path.home() / ".helix" / "launcher" / "helix-bench.log"
                ),
                extra_env={
                    "HELIX_GENOME_PATH": bench_genome_path,
                    "HELIX_SERVER_PORT": str(_cfg.server.bench_port),
                },
            )
            log.info(
                "Bench mode: second helix planned on :%d (genome=%s)",
                _cfg.server.bench_port, bench_genome_path,
            )
    except Exception:
        log.warning("Bench-mode config probe failed; continuing without",
                    exc_info=True)

    # First-boot db-selection gate: if the active genome file does not
    # exist yet, do NOT silently autostart onto an empty store — let the
    # dashboard pop the select-or-create dialog instead.
    needs_db_selection = False
    try:
        from . import genome_registry as _gr
        needs_db_selection = not _gr.active_genome_path().exists()
    except Exception:
        needs_db_selection = False

    update_checker = UpdateChecker()

    collector = StateCollector(
        supervisor=supervisor,
        ollama_base_url=args.ollama_base_url,
        update_checker=update_checker,
    )

    # Optional Headroom proxy — if a proxy is already running on the
    # configured port, adopt it and surface it in the tray even when
    # [headroom] enabled=false. The enabled flag still controls whether
    # the launcher may provision a new Headroom child.
    headroom_supervisor, headroom_dashboard_url = _maybe_build_headroom(
        store=store,
        autostart_override=_env_truthy("HELIX_HEADROOM_AUTOSTART"),
        enabled_override=_env_truthy("HELIX_HEADROOM_ENABLED"),
    )

    # Build + start the observability stack BEFORE helix in tray mode so
    # the collector is already bound to :4317 when helix's OTLP exporter
    # dials it. Otherwise helix dials a dead port at startup, the gRPC
    # channel wedges, and metrics drop with `StatusCode.UNIMPLEMENTED`
    # even after the collector eventually binds.
    observability_sup: Optional["ObservabilitySupervisor"] = None
    observability_install_pending = False
    if args.tray:
        observability_sup, observability_install_pending = (
            _maybe_build_observability()
        )
        # On success this also exports HELIX_OTEL_ENABLED=1 (+ endpoint)
        # into our env so the helix child spawned below actually emits
        # into the stack we just started. Explicit user env wins.
        _start_observability_stack(observability_sup)

    # Adopt or start helix before the UI comes up.
    if needs_db_selection:
        log.info(
            "No genome found at the active path — skipping autostart; "
            "the dashboard will prompt to select or create a database.",
        )
    if not supervisor.adopt() and not args.no_autostart and not needs_db_selection:
        try:
            if route_helix_via_headroom:
                log.info(
                    "Starting helix on %s:%d via Headroom upstream %s",
                    args.helix_host,
                    args.helix_port,
                    os.environ.get("HELIX_SERVER_UPSTREAM"),
                )
            else:
                log.info("Starting helix on %s:%d", args.helix_host, args.helix_port)
            supervisor.start()
        except AlreadyRunning:
            pass
        except Exception as exc:
            log.error("Failed to start helix: %s", exc)
            log.info("Launcher will continue; use the Start button once the issue is fixed")

    if bench_supervisor is not None and not args.no_autostart:
        try:
            if not bench_supervisor.adopt():
                bench_supervisor.start()
        except AlreadyRunning:
            pass
        except Exception as exc:
            log.error("Failed to start bench helix: %s", exc)

    app = create_app(
        store=store,
        supervisor=supervisor,
        collector=collector,
        observability=observability_sup,
        observability_install_pending=observability_install_pending,
        grafana_url=args.grafana_url,
        prometheus_url=args.prometheus_url,
        bench_supervisor=bench_supervisor,
        bench_genome_path=bench_genome_path,
        needs_db_selection=needs_db_selection,
    )

    url = f"http://{args.host}:{args.port}/"

    if args.tray and args.native:
        # Windows-only combined mode — see platform guard above.
        return _run_tray_native_combined(
            app, args.host, args.port, url, supervisor,
            grafana_url=args.grafana_url,
            prometheus_url=args.prometheus_url,
        )

    if args.tray:
        # Uvicorn in daemon thread, pystray on main.
        # pystray owns the process lifecycle — Quit from the tray menu
        # calls icon.stop() which unblocks this main thread; main() returns
        # and the daemon thread dies with the process.
        from .tray import HelixTrayIcon

        server_thread = threading.Thread(
            target=_run_uvicorn,
            args=(app, args.host, args.port),
            daemon=True,
            name="launcher-uvicorn",
        )
        server_thread.start()
        _wait_for_port_bound(args.host, args.port)  # replaces 0.4s race

        # observability_sup was built + started above (BEFORE helix), so the
        # collector is already bound to :4317 when helix's OTLP exporter
        # dials on first metric push. Only the menu URLs need wiring here.
        if observability_sup is not None:
            if args.grafana_url is None:
                args.grafana_url = DEFAULT_GRAFANA_URL
            if args.prometheus_url is None:
                args.prometheus_url = DEFAULT_PROMETHEUS_URL

        tray_icon = HelixTrayIcon(
            supervisor=supervisor,
            dashboard_url=url,
            grafana_url=args.grafana_url,
            prometheus_url=args.prometheus_url,
            headroom_supervisor=headroom_supervisor,
            headroom_dashboard_url=headroom_dashboard_url,
            observability_supervisor=observability_sup,
            install_pending=observability_install_pending,
            update_checker=update_checker,
        )

        # Surface the install-needed balloon if the build helper flagged it.
        if observability_install_pending:
            try:
                # Defer one tick so the icon is fully constructed.
                threading.Timer(1.0, tray_icon.notify_install_needed).start()
            except Exception:
                log.warning("install-needed balloon scheduling failed", exc_info=True)
        try:
            threading.Timer(2.0, tray_icon.notify_update_available).start()
        except Exception:
            log.warning("update balloon scheduling failed", exc_info=True)

        # Surface the hardware-fallback balloon (spec §6 third surface) —
        # fires once per (requested, active) state change. Mirrors the
        # install-pending balloon pattern above. Defer past notify_update
        # so balloons don't stack on top of each other on startup.
        try:
            threading.Timer(3.0, tray_icon.notify_hardware_fallback).start()
        except Exception:
            log.warning("hardware-fallback balloon scheduling failed", exc_info=True)

        log.info("Tray mode active — dashboard at %s", url)
        log.info("Click the tray icon to open the dashboard; Quit from its menu to exit.")
        tray_icon.run()  # blocks until Quit

        # Tray exited — shut down observability (Job Object would do this on
        # Windows even on hard exit, but the clean path is courteous).
        if observability_sup is not None:
            try:
                observability_sup.shutdown()
            except Exception:
                log.warning("ObservabilitySupervisor.shutdown failed", exc_info=True)
        return 0

    if args.native:
        # Start uvicorn in a background thread so pywebview can own the main thread.
        server_thread = threading.Thread(
            target=_run_uvicorn,
            args=(app, args.host, args.port),
            daemon=True,
            name="launcher-uvicorn",
        )
        server_thread.start()
        # Poll until uvicorn binds (replaces 0.4s race).
        _wait_for_port_bound(args.host, args.port)
        _open_ui(url, native=True)
    else:
        if not args.no_browser:
            # Browser tab opened just before uvicorn blocks the main thread.
            _schedule_open(url)
        _run_uvicorn(app, args.host, args.port)

    return 0


def _current_pid() -> int:
    import os
    return os.getpid()


def _run_tray_native_combined(
    app: FastAPI,
    host: str,
    port: int,
    url: str,
    supervisor: HelixSupervisor,
    grafana_url: Optional[str] = None,
    prometheus_url: Optional[str] = None,
) -> int:
    """Close-to-tray mode: pywebview window + persistent tray icon.

    Windows-only (see _run_args guard). Threading model:

      - Main thread:        pywebview (WebView2 message pump)
      - Background thread:  uvicorn (daemon)
      - Background thread:  pystray (daemon, owns tray icon message pump)

    Window close behavior:
      - User clicks X → closing event returns False, window.hide()
      - Tray "Show Window" → window.show()
      - Tray "Hide to Tray" → window.hide()
      - Tray "Quit" → set flag, window.destroy() → closing returns True
                     → webview.start() returns → main() exits

    On quit, the daemon threads (uvicorn + pystray) are cleaned up
    automatically when the process exits. The tray icon.stop() is also
    called explicitly for good measure.
    """
    import webview  # type: ignore
    import pystray  # type: ignore
    from .tray import _build_icon_image

    log.info("Tray + native mode — pywebview on main, pystray detached")

    # ── background uvicorn ────────────────────────────────────────
    server_thread = threading.Thread(
        target=_run_uvicorn,
        args=(app, host, port),
        daemon=True,
        name="launcher-uvicorn",
    )
    server_thread.start()
    time.sleep(0.4)  # let uvicorn bind

    # ── shared state for the close-to-tray dance ──────────────────
    quitting = threading.Event()
    window_holder: list = [None]  # mutable holder shared between threads
    tray_holder: list = [None]

    def on_window_closing():
        """Hook for window.events.closing — intercept close, hide instead."""
        if quitting.is_set():
            return True  # allow close
        w = window_holder[0]
        if w is not None:
            try:
                w.hide()
            except Exception:
                log.warning("Hide-on-close failed", exc_info=True)
        return False  # cancel the close

    # ── tray menu action handlers ─────────────────────────────────
    def _show_window(icon, item):  # noqa: ARG001
        w = window_holder[0]
        if w is not None:
            try:
                w.show()
            except Exception:
                log.warning("Show window failed", exc_info=True)

    def _hide_window(icon, item):  # noqa: ARG001
        w = window_holder[0]
        if w is not None:
            try:
                w.hide()
            except Exception:
                log.warning("Hide window failed", exc_info=True)

    def _open_browser(icon, item):  # noqa: ARG001
        try:
            webbrowser.open(url)
        except Exception:
            log.warning("Open browser failed", exc_info=True)

    def _start_helix(icon, item):  # noqa: ARG001
        try:
            supervisor.start()
        except Exception:
            log.warning("Tray start failed", exc_info=True)

    def _restart_helix(icon, item):  # noqa: ARG001
        try:
            supervisor.restart(reason="manual restart from tray menu")
        except Exception:
            log.warning("Tray restart failed", exc_info=True)

    def _stop_helix(icon, item):  # noqa: ARG001
        try:
            supervisor.stop(reason="manual stop from tray menu")
        except Exception:
            log.warning("Tray stop failed", exc_info=True)

    def _quit_all(icon, item):  # noqa: ARG001
        log.info("Tray Quit — destroying window")
        quitting.set()

        try:
            if supervisor.is_running():
                supervisor.stop(reason="launcher quit from tray menu (native)")
        except Exception:
            log.warning("Helix stop during quit failed", exc_info=True)

        w = window_holder[0]
        if w is not None:
            try:
                w.destroy()
            except Exception:
                log.warning("Window destroy failed", exc_info=True)

        tray = tray_holder[0]
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                log.warning("Tray stop failed", exc_info=True)

    def _open_grafana(icon, item):  # noqa: ARG001
        if grafana_url:
            try:
                webbrowser.open(grafana_url)
            except Exception:
                log.warning("Open Grafana failed", exc_info=True)

    def _open_prometheus(icon, item):  # noqa: ARG001
        if prometheus_url:
            try:
                webbrowser.open(prometheus_url)
            except Exception:
                log.warning("Open Prometheus failed", exc_info=True)

    # ── build the tray menu ───────────────────────────────────────
    menu_items = [
        pystray.MenuItem("Show Window", _show_window, default=True),
        pystray.MenuItem("Hide to Tray", _hide_window),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open in Browser", _open_browser),
    ]
    if grafana_url:
        menu_items.append(pystray.MenuItem("Open Grafana", _open_grafana))
    if prometheus_url:
        menu_items.append(pystray.MenuItem("Open Prometheus", _open_prometheus))
    menu_items.extend([
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Start helix", _start_helix,
            enabled=lambda item: not supervisor.is_running(),  # noqa: ARG005
        ),
        pystray.MenuItem(
            "Restart helix", _restart_helix,
            enabled=lambda item: supervisor.is_running(),  # noqa: ARG005
        ),
        pystray.MenuItem(
            "Stop helix", _stop_helix,
            enabled=lambda item: supervisor.is_running(),  # noqa: ARG005
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit_all),
    ])
    tray_menu = pystray.Menu(*menu_items)

    tray_icon = pystray.Icon(
        name="helix-launcher",
        icon=_build_icon_image(),
        title="Helix Launcher",
        menu=tray_menu,
    )
    tray_holder[0] = tray_icon

    # ── start pystray on a background thread ──────────────────────
    tray_thread = threading.Thread(
        target=tray_icon.run,
        daemon=True,
        name="launcher-tray",
    )
    tray_thread.start()

    # ── create the pywebview window on main thread ────────────────
    window = webview.create_window(
        "Helix Launcher",
        url,
        width=1000,
        height=720,
        resizable=True,
    )
    window_holder[0] = window

    # Hook closing BEFORE start so the first close attempt is intercepted.
    try:
        window.events.closing += on_window_closing
    except Exception:
        log.warning("Could not hook window.events.closing — close-to-tray disabled", exc_info=True)

    log.info("Combined mode active — dashboard at %s", url)
    log.info("Close the window to hide to tray; Quit from tray menu to exit.")

    webview.start()  # blocks until window.destroy() is called by _quit_all

    # Cleanup
    try:
        tray_icon.stop()
    except Exception:
        pass
    log.info("Combined mode: exited cleanly")
    return 0


def _schedule_open(url: str) -> None:
    """Fire a browser open after a short delay so uvicorn has time to bind."""
    def _worker() -> None:
        time.sleep(0.6)
        _open_ui(url, native=False)
    t = threading.Thread(target=_worker, daemon=True, name="launcher-browser-open")
    t.start()


def _wait_for_port_bound(host: str, port: int, timeout: float = 3.0) -> bool:
    """Poll until the uvicorn server binds the port, or timeout.

    Replaces `time.sleep(0.4)` (a race — the server may not yet have
    bound when the UI tries to connect). Returns True if the port is
    reachable, False on timeout. Keeps polling cheap (50ms gap).
    """
    import socket as _socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                s.connect((host, port))
            return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.05)
    log.warning("Port %s:%d did not bind within %.1fs — continuing anyway",
                host, port, timeout)
    return False


def _run_uvicorn(app: FastAPI, host: str, port: int) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    sys.exit(main())

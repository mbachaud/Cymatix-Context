"""Dashboard UX pass (PR 1): single Start/Stop toggle, Chunks wording,
Diagnostics open-folder, component tooltips, compact switchboard, and
agent labels that echo what the agent announced instead of a code map."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("jinja2", reason="launcher extra not installed")
from fastapi.testclient import TestClient

from cymatix_context.cli.cmd_mcp import supported_hosts
from cymatix_context.launcher.app import create_app
from cymatix_context.launcher.collector import StateCollector
from tests.test_launcher_dashboard_wiring import FakeSupervisor


def _client(state: dict, start_pending: bool = False) -> TestClient:
    collector = MagicMock()
    collector.collect.return_value = state
    sup = FakeSupervisor()
    sup.last_start_pending = start_pending   # the app derives start_pending from this
    app = create_app(store=SimpleNamespace(), supervisor=sup, collector=collector)
    return TestClient(app)


RUNNING = {"cymatix": {"running": True, "pid": 4242, "port": 11437}}
STOPPED = {"cymatix": {"running": False, "port": 11437}}


# ── single Start/Stop toggle ───────────────────────────────────────────


def test_running_state_offers_stop_not_start():
    with _client(RUNNING) as c:
        html = c.get("/").text
    assert 'data-action="toggle"' in html
    assert 'data-running="true"' in html
    toggle = html.split('data-action="toggle"', 1)[1].split("</button>", 1)[0]
    assert "Stop" in toggle and "Start" not in toggle
    assert 'data-action="start"' not in html
    assert 'data-action="stop"' not in html
    assert 'data-action="restart"' in html


def test_stopped_state_offers_start():
    with _client(STOPPED) as c:
        html = c.get("/").text
    assert 'data-running="false"' in html
    toggle = html.split('data-action="toggle"', 1)[1].split("</button>", 1)[0]
    assert "Start" in toggle and "Stop" not in toggle


@pytest.mark.parametrize("running", [True, False])
def test_stale_start_pending_never_disables_the_toggle(running):
    """last_start_pending is sticky on the supervisor; a dead-end disabled
    button must be impossible whatever it says."""
    state = {"cymatix": {"running": running, "pid": 1, "port": 11437}}
    with _client(state, start_pending=True) as c:
        html = c.get("/").text
    tag = html.split('data-action="toggle"', 1)[1].split(">", 1)[0]
    assert "disabled" not in tag
    label = html.split("data-toggle-label>", 1)[1].split("</span>", 1)[0]
    assert label == ("Starting…" if running else "Start")


def test_panel_swap_patches_changed_panels_and_keeps_focus():
    """The 2 s refresh must not replace unchanged panels (it dropped keyboard
    focus every tick) and must fall back to the full swap where the DOM lacks
    outerHTML/replaceWith."""
    with _client(RUNNING) as c:
        js = c.get("/static/launcher.js").text
    assert "function patchPanels" in js and "replaceWith" in js
    assert "panels.replaceChildren(...newNodes)" in js       # the fallback
    assert "restoreFocus(focus)" in js


def test_toggle_is_only_disabled_by_its_own_click():
    with _client(RUNNING) as c:
        js = c.get("/static/launcher.js").text
    assert "btn.disabled = pendingAction !== null;" in js
    # a start click is held until the POST resolves, not until the first poll
    assert "running === (pendingAction" not in js


# ── Chunks wording ──────────────────────────────────────────────────────


def test_genes_are_labelled_chunks():
    state = {
        **RUNNING,
        "genes": {"total": 6272, "raw_chars": 5_000_000,
                  "compressed_chars": 2_000_000, "compression_ratio": 2.5},
    }
    with _client(state) as c:
        page = c.get("/").text
        panels = c.get("/api/state/panels").text
    assert "chunks</span>" in page
    assert "Chunks" in panels
    assert "Genes" not in panels and "genes</span>" not in page


# ── Diagnostics open-folder ─────────────────────────────────────────────


def test_diagnostics_has_bridge_only_open_folder():
    state = {**RUNNING, "cymatix": {**RUNNING["cymatix"], "paths": {
        "state_file": "C:/x/state.json", "cymatix_log": "C:/x/cymatix.log"}}}
    with _client(state) as c:
        html = c.get("/api/state/panels").text
    assert 'data-action="desktop-open-logs"' in html
    assert "needs-bridge" in html
    assert 'data-action="copy-path"' in html


def test_open_folder_wired_outside_the_rail():
    with _client(RUNNING) as c:
        js = c.get("/static/launcher.js").text
        css = c.get("/static/launcher.css").text
    assert "has-bridge" in js
    assert ".needs-bridge" in css


# ── component tooltips ──────────────────────────────────────────────────


def test_component_rows_carry_a_description_tooltip():
    state = {**RUNNING, "tools": {
        "count": 2, "source_count": 2, "last_activity_s_ago": 3,
        "entries": [
            {"name": "retriever", "kind": "tier", "status": "active", "backend": "fts5",
             "description": "Finds candidate chunks."},
            {"name": "mystery", "kind": "stage", "status": "active", "backend": None},
        ]}}
    with _client(state) as c:
        html = c.get("/api/state/panels").text
    assert "Finds candidate chunks." in html
    # A component with no description still renders, without an empty tooltip.
    assert 'title=""' not in html


def test_glossary_fills_in_a_missing_description():
    c = StateCollector(supervisor=MagicMock())
    filled = c._with_description({"name": "splade", "kind": "encoder"})
    assert "Opt-in" in filled["description"]
    assert "description" not in c._with_description({"name": "nope", "kind": "x"})
    # the server's own text wins
    assert c._with_description({"name": "splade", "description": "mine"})["description"] == "mine"


# ── compact switchboard ─────────────────────────────────────────────────


def test_switchboard_marks_off_flags_inactive():
    s = StateCollector(supervisor=MagicMock())._switchboard_panel()
    by = {e["label"]: e for e in s["settings"]}
    assert by["fusion_mode"]["active"] is True          # enums always show
    assert by["expression_tokens"]["active"] is True    # numbers always show
    assert by["pki_enabled"]["active"] is False         # default-off flag
    assert by["session_delivery_enabled"]["active"] is True
    for empty in ("", "—"):
        assert StateCollector._is_active_value(empty) is False


def test_switchboard_template_folds_inactive_settings():
    state = {**STOPPED, "switchboard": {"summary": "s", "settings": [
        {"label": "fusion_mode", "value": "rrf", "description": "d1", "active": True},
        {"label": "pki_enabled", "value": "off", "description": "d2", "active": False},
        {"label": "splade_enabled", "value": "off", "description": "d3", "active": False},
    ]}}
    with _client(state) as c:
        html = c.get("/api/state/panels").text
    assert "fusion_mode" in html
    assert "2 switched off" in html
    # Folded, not removed: still in the DOM so the disclosure can reveal it.
    assert "<details" in html and "pki_enabled" in html


# ── agent labels echo the announcement ──────────────────────────────────


def test_agent_labels_echo_announced_values():
    from cymatix_context.launcher import host_labels
    assert host_labels.compose_label("claude-code", "vscode") == "claude-code + vscode"
    assert host_labels.compose_label("codex", "codex") == "codex"
    assert host_labels.compose_label(None, "unknown") is None
    assert not hasattr(host_labels, "_VENDOR_MAP")
    assert not hasattr(host_labels, "_HOST_MAP")


def test_rail_hosts_come_from_the_cli_table_not_the_template():
    with _client(RUNNING) as c:
        html = c.get("/?embedded=1").text
    rail = html.split("data-desktop-mcp-host", 1)[1].split("</select>", 1)[0]
    for host in supported_hosts():
        assert f'value="{host}"' in rail
    # No pretty vendor strings baked into the template.
    assert "Claude Code" not in rail and "Gemini CLI" not in rail

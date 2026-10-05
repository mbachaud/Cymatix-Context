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


def _client(state: dict) -> TestClient:
    collector = MagicMock()
    collector.collect.return_value = state
    app = create_app(
        store=SimpleNamespace(), supervisor=FakeSupervisor(), collector=collector,
    )
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


def test_toggle_script_picks_the_next_action():
    with _client(RUNNING) as c:
        js = c.get("/static/launcher.js").text
    assert '"toggle"' in js
    assert 'data-action="toggle"' in js or "[data-action=\"toggle\"]" in js


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


# ── compact switchboard ─────────────────────────────────────────────────


def test_switchboard_marks_off_flags_inactive():
    s = StateCollector(supervisor=MagicMock())._switchboard_panel()
    by = {e["label"]: e for e in s["settings"]}
    assert by["fusion_mode"]["active"] is True          # enums always show
    assert by["expression_tokens"]["active"] is True    # numbers always show
    for e in s["settings"]:
        assert e["active"] is (e["value"] not in {"off", "false", "disabled", "none"})


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

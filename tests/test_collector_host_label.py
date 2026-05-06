"""Collector populates host_label on agent panel entries.

Exercises the wire from a participant dict (with agent_kind / mcp_host
fields) through StateCollector's panel builders to the rendered entry
shape that the Jinja templates consume.
"""
from unittest.mock import MagicMock

from helix_context.launcher.collector import StateCollector


def _make_supervisor():
    sup = MagicMock()
    sup.helix_host = "127.0.0.1"
    sup.helix_port = 11437
    return sup


def _make_participant(**overrides):
    base = {
        "participant_id": "abc12345",
        "handle": "laude",
        "party_id": "party_x",
        "workspace": "F:\\Projects",
        "status": "active",
        "last_seen_s_ago": 1.0,
        "agent_kind": None,
        "mcp_host": None,
        "ide_detected": None,
        "ide_detection_via": None,
        "model_id": None,
    }
    base.update(overrides)
    return base


def test_all_agents_panel_emits_host_label_when_both_set():
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant(agent_kind="claude-code", mcp_host="vscode")
    panel = collector._all_agents_panel([p])
    assert panel["entries"][0]["host_label"] == "Claude Code + VS Code"


def test_all_agents_panel_emits_host_label_vendor_only():
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant(agent_kind="codex", mcp_host=None)
    panel = collector._all_agents_panel([p])
    assert panel["entries"][0]["host_label"] == "Codex"


def test_all_agents_panel_omits_host_label_when_neither_set():
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant()
    panel = collector._all_agents_panel([p])
    # Either absent or explicitly None — both let the {% if %} skip render.
    assert not panel["entries"][0].get("host_label")


def test_disconnected_agents_panel_emits_host_label():
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant(
        status="stale",
        agent_kind="claude-code",
        mcp_host="antigravity",
    )
    panel = collector._disconnected_agents_panel([p])
    assert panel is not None
    assert panel["entries"][0]["host_label"] == "Claude Code + Antigravity"


def test_all_agents_panel_emits_tooltip_fields_when_announced():
    """Entry has model_pretty/ide_pretty/agent_kind_pretty/ide_detection_via."""
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant(
        agent_kind="claude-code",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
        model_id="claude-opus-4-7",
    )
    panel = collector._all_agents_panel([p])
    entry = panel["entries"][0]
    tooltip = entry["tooltip"]
    assert tooltip["model_label"] == "Claude Opus 4.7"
    assert tooltip["ide_label"] == "VS Code"
    assert tooltip["agent_kind_label"] == "Claude Code"
    assert tooltip["ide_detection_via"] == "env:VSCODE_PID"


def test_all_agents_panel_emits_placeholders_when_missing():
    """Missing fields render as 'Not announced' / 'Not detected' / 'Not set'."""
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant()  # all announce fields None; agent_kind also None
    panel = collector._all_agents_panel([p])
    entry = panel["entries"][0]
    tooltip = entry["tooltip"]
    assert tooltip["model_label"] == "Not announced"
    assert tooltip["ide_label"] == "Not detected"
    assert tooltip["agent_kind_label"] == "Not set"


def test_all_agents_panel_emits_unknown_model_id_verbatim():
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant(model_id="acme-experimental-7b")
    panel = collector._all_agents_panel([p])
    assert panel["entries"][0]["tooltip"]["model_label"] == "acme-experimental-7b"


def test_disconnected_agents_panel_also_emits_tooltip():
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant(
        status="stale",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
        model_id="claude-opus-4-7",
    )
    panel = collector._disconnected_agents_panel([p])
    assert panel is not None
    tooltip = panel["entries"][0]["tooltip"]
    assert tooltip["model_label"] == "Claude Opus 4.7"
    assert tooltip["ide_label"] == "VS Code"


def test_participants_panel_also_emits_tooltip():
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant(
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
        model_id="gpt-5",
    )
    panel = collector._participants_panel([p])
    tooltip = panel["entries"][0]["tooltip"]
    assert tooltip["model_label"] == "GPT-5"
    assert tooltip["ide_label"] == "VS Code"

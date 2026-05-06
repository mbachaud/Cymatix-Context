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

"""
Tests for cymatix_context.launcher.collector — state aggregation with
mocked supervisor + mocked HTTP responses.

Also covers host_label/tooltip wiring on the agent panel builders
(absorbed from the former test_collector_host_label.py): the wire
from a participant dict (agent_kind / mcp_host / model_id / ide_*
fields) through StateCollector's panel builders to the rendered
entry shape the Jinja templates consume.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from cymatix_context.launcher.collector import StateCollector


@pytest.fixture
def fake_supervisor(tmp_path):
    sup = MagicMock()
    sup.helix_host = "127.0.0.1"
    sup.helix_port = 11437
    sup.is_running.return_value = True
    sup.get_pid.return_value = 12345
    sup.get_uptime_s.return_value = 42.5
    sup.store.state.last_restart_reason = "test"
    sup.store.state.last_restart_at = time.time()
    # Telemetry defaults — collector now reads these
    sup.find_orphan_helix.return_value = None
    sup.get_last_error.return_value = None
    sup.store.path = tmp_path / "state.json"
    sup.helix_log_path = tmp_path / "helix.log"
    return sup


@pytest.fixture
def collector(fake_supervisor):
    return StateCollector(supervisor=fake_supervisor)


def _mock_client(responses: dict):
    """Build a context-managed httpx.Client mock with prebaked responses.

    `responses` maps URL paths to JSON bodies. Missing paths return 404.
    """
    client = MagicMock()
    def fake_get(path, params=None):
        resp = MagicMock()
        if path in responses:
            resp.status_code = 200
            resp.json.return_value = responses[path]
        else:
            resp.status_code = 404
        return resp
    client.get.side_effect = fake_get
    client.close = MagicMock()
    return client


class TestCollectHelixDown:
    def test_returns_only_helix_field_when_stopped(self, collector, fake_supervisor):
        fake_supervisor.is_running.return_value = False
        fake_supervisor.find_orphan_helix.return_value = None
        fake_supervisor.get_last_error.return_value = None
        result = collector.collect()
        assert "helix" in result
        assert result["helix"]["running"] is False
        assert result["helix"]["availability"] == "unavailable"
        # No other panels should be present
        assert "genes" not in result
        assert "parties" not in result
        assert "tools" not in result

    def test_orphan_pid_surfaced_when_helix_down(self, collector, fake_supervisor):
        fake_supervisor.is_running.return_value = False
        fake_supervisor.find_orphan_helix.return_value = 45678
        fake_supervisor.get_last_error.return_value = None
        result = collector.collect()
        assert result["helix"]["orphan_pid"] == 45678

    def test_last_error_surfaced(self, collector, fake_supervisor):
        fake_supervisor.is_running.return_value = False
        fake_supervisor.find_orphan_helix.return_value = None
        fake_supervisor.get_last_error.return_value = {
            "operation": "start",
            "message": "port 11437 occupied",
            "at": 1775896000.0,
        }
        result = collector.collect()
        assert result["helix"]["last_error"]["operation"] == "start"
        assert "port 11437" in result["helix"]["last_error"]["message"]

    def test_paths_always_present(self, collector, fake_supervisor, tmp_path):
        fake_supervisor.is_running.return_value = False
        fake_supervisor.find_orphan_helix.return_value = None
        fake_supervisor.get_last_error.return_value = None
        fake_supervisor.store.path = tmp_path / "state.json"
        fake_supervisor.helix_log_path = tmp_path / "helix.log"
        result = collector.collect()
        assert "paths" in result["helix"]
        assert "state_file" in result["helix"]["paths"]
        assert "helix_log" in result["helix"]["paths"]


class TestGenesPanel:
    def test_genes_panel_built_from_stats(self, collector):
        responses = {
            "/stats": {
                "total_genes": 8000,
                "total_chars_raw": 47_000_000,
                "total_chars_compressed": 17_500_000,
                "compression_ratio": 2.69,
            },
            "/sessions": {"participants": []},
        }
        with patch("httpx.Client", return_value=_mock_client(responses)):
            with patch.object(collector, "_collect_models", return_value=None):
                state = collector.collect()
        assert state["genes"]["total"] == 8000
        assert state["genes"]["raw_chars"] == 47_000_000
        assert state["genes"]["compression_ratio"] == 2.69

    def test_health_ok_marks_available(self, collector):
        responses = {
            "/stats": {
                "total_genes": 8000,
                "total_chars_raw": 47_000_000,
                "total_chars_compressed": 17_500_000,
                "compression_ratio": 2.69,
            },
            "/sessions": {"participants": []},
            "/health": {"status": "ok", "ribosome": "mock"},
        }
        with patch("httpx.Client", return_value=_mock_client(responses)):
            with patch.object(collector, "_collect_models", return_value=None):
                state = collector.collect()
        assert state["helix"]["availability"] == "available"

    def test_missing_health_still_available_when_older_helix_answers_stats(self, collector):
        responses = {
            "/stats": {
                "total_genes": 8000,
                "total_chars_raw": 47_000_000,
                "total_chars_compressed": 17_500_000,
                "compression_ratio": 2.69,
                "version": "0.2.0",
            },
            "/sessions": {"participants": []},
        }
        with patch("httpx.Client", return_value=_mock_client(responses)):
            with patch.object(collector, "_collect_models", return_value=None):
                state = collector.collect()
        assert state["helix"]["availability"] == "available"
        assert state["helix"]["version"] == "0.2.0"


class TestPartiesAndParticipants:
    def test_parties_derived_from_unique_party_ids(self, collector):
        participants = [
            {"handle": "taude", "party_id": "max@local", "status": "active", "last_seen_s_ago": 1.0},
            {"handle": "laude", "party_id": "max@local", "status": "active", "last_seen_s_ago": 5.0},
            {"handle": "guest", "party_id": "other@remote", "status": "stale", "last_seen_s_ago": 9999.0},
        ]
        responses = {
            "/stats": {"total_genes": 0, "total_chars_raw": 0, "total_chars_compressed": 0, "compression_ratio": 1.0},
            "/sessions": {"participants": participants},
        }
        with patch("httpx.Client", return_value=_mock_client(responses)):
            with patch.object(collector, "_collect_models", return_value=None):
                state = collector.collect()

        assert state["parties"]["count"] == 2
        assert "max@local" in state["parties"]["party_ids"]
        assert "other@remote" in state["parties"]["party_ids"]

        # Main panel is active identities; total_count is raw session rows.
        assert state["participants"]["count"] == 2
        assert state["participants"]["identity_total_count"] == 3
        assert state["participants"]["total_count"] == 3
        handles = [p["handle"] for p in state["participants"]["entries"]]
        assert handles == ["taude", "laude"]  # ordered by last_seen_s_ago
        assert state["disconnected_agents"]["count"] == 1
        assert state["disconnected_agents"]["entries"][0]["handle"] == "guest"
        assert state["disconnected_agents"]["entries"][0]["status"] == "stale"
        assert state["all_agents"]["count"] == 3

    def test_duplicate_sessions_collapse_into_one_identity(self, collector):
        participants = [
            {
                "participant_id": "aaaaaaaa11111111",
                "handle": "laude",
                "party_id": "swift_wing21",
                "workspace": "f:\\Projects\\Education",
                "status": "active",
                "last_seen_s_ago": 2.0,
                "started_at": 100.0,
            },
            {
                "participant_id": "bbbbbbbb22222222",
                "handle": "laude",
                "party_id": "swift_wing21",
                "workspace": "f:\\Projects\\Education",
                "status": "active",
                "last_seen_s_ago": 4.0,
                "started_at": 101.0,
            },
        ]
        responses = {
            "/stats": {"total_genes": 0, "total_chars_raw": 0, "total_chars_compressed": 0, "compression_ratio": 1.0},
            "/sessions": {"participants": participants},
        }
        with patch("httpx.Client", return_value=_mock_client(responses)):
            with patch.object(collector, "_collect_models", return_value=None):
                state = collector.collect()

        assert state["participants"]["count"] == 1
        assert state["participants"]["identity_total_count"] == 1
        assert state["participants"]["total_count"] == 2
        assert state["participants"]["entries"][0]["session_count"] == 2
        assert "disconnected_agents" not in state
        assert state["all_agents"]["count"] == 2
        assert state["all_agents"]["entries"][0]["participant_id_short"] == "aaaaaaaa"

    def test_no_participants_omits_panel(self, collector):
        responses = {
            "/stats": {"total_genes": 0, "total_chars_raw": 0, "total_chars_compressed": 0, "compression_ratio": 1.0},
            "/sessions": {"participants": []},
        }
        with patch("httpx.Client", return_value=_mock_client(responses)):
            with patch.object(collector, "_collect_models", return_value=None):
                state = collector.collect()
        assert "parties" not in state
        assert "participants" not in state
        assert "disconnected_agents" not in state


class TestToolsPanel:
    def test_tools_built_from_components_endpoint(self, collector):
        components = {
            "components": [
                {"name": "ribosome", "kind": "decoder", "status": "running"},
                {"name": "splade", "kind": "encoder", "status": "idle"},
            ],
            "count": 2,
            "last_activity_s_ago": 12.4,
        }
        responses = {
            "/stats": {"total_genes": 0, "total_chars_raw": 0, "total_chars_compressed": 0, "compression_ratio": 1.0},
            "/sessions": {"participants": []},
            "/admin/components": components,
        }
        with patch("httpx.Client", return_value=_mock_client(responses)):
            with patch.object(collector, "_collect_models", return_value=None):
                state = collector.collect()

        assert state["tools"]["count"] == 1
        assert state["tools"]["source_count"] == 2
        assert state["tools"]["hidden_count"] == 1
        assert state["tools"]["last_activity_s_ago"] == 12.4
        assert len(state["tools"]["entries"]) == 1
        assert state["tools"]["entries"][0]["name"] == "splade"

    def test_no_components_omits_tools_panel(self, collector):
        responses = {
            "/stats": {"total_genes": 0, "total_chars_raw": 0, "total_chars_compressed": 0, "compression_ratio": 1.0},
            "/sessions": {"participants": []},
            "/admin/components": {"components": [], "count": 0},
        }
        with patch("httpx.Client", return_value=_mock_client(responses)):
            with patch.object(collector, "_collect_models", return_value=None):
                state = collector.collect()
        assert "tools" not in state

    def test_ribosome_only_omits_tools_panel(self, collector):
        responses = {
            "/stats": {"total_genes": 0, "total_chars_raw": 0, "total_chars_compressed": 0, "compression_ratio": 1.0},
            "/sessions": {"participants": []},
            "/admin/components": {
                "components": [
                    {"name": "ribosome", "kind": "decoder", "status": "running", "backend": "gemma4:e2b"},
                ],
                "count": 1,
                "last_activity_s_ago": 3.1,
            },
        }
        with patch("httpx.Client", return_value=_mock_client(responses)):
            with patch.object(collector, "_collect_models", return_value=None):
                state = collector.collect()
        assert "tools" not in state


class TestTokensPanel:
    def test_tokens_built_from_metrics_endpoint(self, collector):
        tokens = {
            "session": {
                "prompt_tokens": 100,
                "completion_tokens": 200,
                "total": 300,
                "estimated_prompt_tokens": 0,
                "estimated_completion_tokens": 0,
                "estimated_total": 0,
            },
            "lifetime": {
                "prompt_tokens": 5000,
                "completion_tokens": 8000,
                "total": 13000,
                "estimated_prompt_tokens": 200,
                "estimated_completion_tokens": 300,
                "estimated_total": 500,
            },
        }
        responses = {
            "/stats": {"total_genes": 0, "total_chars_raw": 0, "total_chars_compressed": 0, "compression_ratio": 1.0},
            "/sessions": {"participants": []},
            "/metrics/tokens": tokens,
        }
        with patch("httpx.Client", return_value=_mock_client(responses)):
            with patch.object(collector, "_collect_models", return_value=None):
                state = collector.collect()

        assert "tokens" in state
        assert state["tokens"]["session"]["total"] == 300
        assert state["tokens"]["session"]["exact"] == 300
        assert state["tokens"]["session"]["estimated"] == 0
        # Lifetime combines exact + estimated
        assert state["tokens"]["lifetime"]["total"] == 13500
        assert state["tokens"]["lifetime"]["exact"] == 13000
        assert state["tokens"]["lifetime"]["estimated"] == 500

    def test_zero_tokens_omits_panel(self, collector):
        tokens = {
            "session": {"prompt_tokens": 0, "completion_tokens": 0, "total": 0, "estimated_total": 0},
            "lifetime": {"prompt_tokens": 0, "completion_tokens": 0, "total": 0, "estimated_total": 0},
        }
        responses = {
            "/stats": {"total_genes": 0, "total_chars_raw": 0, "total_chars_compressed": 0, "compression_ratio": 1.0},
            "/sessions": {"participants": []},
            "/metrics/tokens": tokens,
        }
        with patch("httpx.Client", return_value=_mock_client(responses)):
            with patch.object(collector, "_collect_models", return_value=None):
                state = collector.collect()
        # Panel still rendered if buckets exist (even with 0 totals) — that's
        # fine; the empty-state check is "did we get a response at all".
        # The panel template handles the all-zeros case visually.
        assert "tokens" in state
        assert state["tokens"]["session"]["total"] == 0


class TestModelsPanel:
    def test_ollama_models_collected(self, collector):
        ollama_resp = MagicMock()
        ollama_resp.status_code = 200
        ollama_resp.json.return_value = {
            "models": [
                {"name": "gemma4:e4b", "size": 4_400_000_000},
            ]
        }
        with patch("httpx.get", return_value=ollama_resp):
            models = collector._collect_models()
        assert models is not None
        assert models["loaded"][0]["name"] == "gemma4:e4b"
        assert models["loaded"][0]["source"] == "ollama"

    def test_ollama_unreachable_returns_none(self, collector):
        with patch("httpx.get", side_effect=Exception("connection refused")):
            models = collector._collect_models()
        assert models is None

    def test_empty_models_list_returns_none(self, collector):
        ollama_resp = MagicMock()
        ollama_resp.status_code = 200
        ollama_resp.json.return_value = {"models": []}
        with patch("httpx.get", return_value=ollama_resp):
            models = collector._collect_models()
        assert models is None


# --- host_label / tooltip wiring (absorbed from test_collector_host_label.py) ---
#
# These tests exercise the panel-builder methods (`_all_agents_panel`,
# `_disconnected_agents_panel`, `_participants_panel`) directly against a
# minimal supervisor mock, rather than the full `.collect()` HTTP flow used
# above — the two supervisor doubles are genuinely different in shape
# (this one carries none of the store/log-path/telemetry attributes the
# `fake_supervisor` fixture above needs), so they are kept as separate
# local helpers rather than merged into one fixture.


def _make_label_supervisor():
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


class TestHostLabelWiring:
    """host_label composition on the agent panel builders.

    Trimmed vs. the original file: the vendor-only case (asserting
    host_label == "Codex") was dropped — it only re-verified the
    vendor_pretty("codex") -> "Codex" mapping, which is unit-tested
    directly in test_host_labels.py::test_vendor_pretty_known and the
    vendor-only compose_label branch in
    test_host_labels.py::test_compose_label_vendor_only. The both-set
    and neither-set cases below still exercise the collector's own
    wiring (calling compose_label and placing the result under
    "host_label").
    """

    def test_all_agents_panel_emits_host_label_when_both_set(self):
        collector = StateCollector(supervisor=_make_label_supervisor())
        p = _make_participant(agent_kind="claude-code", mcp_host="vscode")
        panel = collector._all_agents_panel([p])
        assert panel["entries"][0]["host_label"] == "Claude Code + VS Code"

    def test_all_agents_panel_omits_host_label_when_neither_set(self):
        collector = StateCollector(supervisor=_make_label_supervisor())
        p = _make_participant()
        panel = collector._all_agents_panel([p])
        # Either absent or explicitly None — both let the {% if %} skip render.
        assert not panel["entries"][0].get("host_label")

    def test_disconnected_agents_panel_emits_host_label(self):
        collector = StateCollector(supervisor=_make_label_supervisor())
        p = _make_participant(
            status="stale",
            agent_kind="claude-code",
            mcp_host="antigravity",
        )
        panel = collector._disconnected_agents_panel([p])
        assert panel is not None
        assert panel["entries"][0]["host_label"] == "Claude Code + Antigravity"


class TestTooltipWiring:
    """Tooltip field wiring (model_label/ide_label/agent_kind_label/
    ide_detection_via) on the agent panel builders.

    Trimmed vs. the original file:
    - `model_label == "Claude Opus 4.7"` was dropped from the
      "when_announced" test below — it only re-verified
      model_pretty("claude-opus-4-7") -> "Claude Opus 4.7", unit-tested
      directly in test_host_labels.py::TestModelLabels::test_known_anthropic_models.
      `ide_label` stays as the one wire-connected pretty-form canary for
      this panel, alongside `agent_kind_label` and the always-unique
      `ide_detection_via` passthrough.
    - `test_all_agents_panel_emits_unknown_model_id_verbatim` was dropped
      entirely — its sole assertion (`model_label ==
      "acme-experimental-7b"`) duplicates
      test_host_labels.py::TestModelLabels::test_unknown_model_id_echoes_verbatim
      (`model_pretty("acme-experimental-7b") == "acme-experimental-7b"`)
      verbatim, with no additional wiring value over the tests below.
    """

    def test_all_agents_panel_emits_tooltip_fields_when_announced(self):
        """Entry has ide_pretty/agent_kind_pretty/ide_detection_via."""
        collector = StateCollector(supervisor=_make_label_supervisor())
        p = _make_participant(
            agent_kind="claude-code",
            ide_detected="vscode",
            ide_detection_via="env:VSCODE_PID",
            model_id="claude-opus-4-7",
        )
        panel = collector._all_agents_panel([p])
        entry = panel["entries"][0]
        tooltip = entry["tooltip"]
        assert tooltip["ide_label"] == "VS Code"
        assert tooltip["agent_kind_label"] == "Claude Code"
        assert tooltip["ide_detection_via"] == "env:VSCODE_PID"

    def test_all_agents_panel_emits_placeholders_when_missing(self):
        """Missing fields render as 'Not announced' / 'Not detected' / 'Not set'."""
        collector = StateCollector(supervisor=_make_label_supervisor())
        p = _make_participant()  # all announce fields None; agent_kind also None
        panel = collector._all_agents_panel([p])
        entry = panel["entries"][0]
        tooltip = entry["tooltip"]
        assert tooltip["model_label"] == "Not announced"
        assert tooltip["ide_label"] == "Not detected"
        assert tooltip["agent_kind_label"] == "Not set"

    def test_disconnected_agents_panel_also_emits_tooltip(self):
        collector = StateCollector(supervisor=_make_label_supervisor())
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

    def test_participants_panel_also_emits_tooltip(self):
        collector = StateCollector(supervisor=_make_label_supervisor())
        p = _make_participant(
            ide_detected="vscode",
            ide_detection_via="env:VSCODE_PID",
            model_id="gpt-5",
        )
        panel = collector._participants_panel([p])
        tooltip = panel["entries"][0]["tooltip"]
        assert tooltip["model_label"] == "GPT-5"
        assert tooltip["ide_label"] == "VS Code"

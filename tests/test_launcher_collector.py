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

import contextlib
import importlib
import inspect
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cymatix_context.cli import cymatix_status
from cymatix_context.launcher.collector import StateCollector


@pytest.fixture
def fake_supervisor(tmp_path):
    sup = MagicMock()
    sup.cymatix_host = "127.0.0.1"
    sup.cymatix_port = 11437
    sup.is_running.return_value = True
    sup.get_pid.return_value = 12345
    sup.get_uptime_s.return_value = 42.5
    sup.store.state.last_restart_reason = "test"
    sup.store.state.last_restart_at = time.time()
    # Telemetry defaults — collector now reads these
    sup.find_orphan_cymatix.return_value = None
    sup.get_last_error.return_value = None
    sup.store.path = tmp_path / "state.json"
    sup.cymatix_log_path = tmp_path / "cymatix.log"
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


class TestCollectCymatixDown:
    def test_returns_only_cymatix_field_when_stopped(self, collector, fake_supervisor):
        fake_supervisor.is_running.return_value = False
        fake_supervisor.find_orphan_cymatix.return_value = None
        fake_supervisor.get_last_error.return_value = None
        result = collector.collect()
        assert "cymatix" in result
        assert result["cymatix"]["running"] is False
        assert result["cymatix"]["availability"] == "unavailable"
        # No other panels should be present
        assert "genes" not in result
        assert "parties" not in result
        assert "tools" not in result

    def test_orphan_pid_surfaced_when_cymatix_down(self, collector, fake_supervisor):
        fake_supervisor.is_running.return_value = False
        fake_supervisor.find_orphan_cymatix.return_value = 45678
        fake_supervisor.get_last_error.return_value = None
        result = collector.collect()
        assert result["cymatix"]["orphan_pid"] == 45678

    def test_last_error_surfaced(self, collector, fake_supervisor):
        fake_supervisor.is_running.return_value = False
        fake_supervisor.find_orphan_cymatix.return_value = None
        fake_supervisor.get_last_error.return_value = {
            "operation": "start",
            "message": "port 11437 occupied",
            "at": 1775896000.0,
        }
        result = collector.collect()
        assert result["cymatix"]["last_error"]["operation"] == "start"
        assert "port 11437" in result["cymatix"]["last_error"]["message"]

    def test_paths_always_present(self, collector, fake_supervisor, tmp_path):
        fake_supervisor.is_running.return_value = False
        fake_supervisor.find_orphan_cymatix.return_value = None
        fake_supervisor.get_last_error.return_value = None
        fake_supervisor.store.path = tmp_path / "state.json"
        fake_supervisor.cymatix_log_path = tmp_path / "cymatix.log"
        result = collector.collect()
        assert "paths" in result["cymatix"]
        assert "state_file" in result["cymatix"]["paths"]
        assert "cymatix_log" in result["cymatix"]["paths"]


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
        assert state["cymatix"]["availability"] == "available"

    def test_missing_health_still_available_when_older_cymatix_answers_stats(self, collector):
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
        assert state["cymatix"]["availability"] == "available"
        assert state["cymatix"]["version"] == "0.2.0"


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
    sup.cymatix_host = "127.0.0.1"
    sup.cymatix_port = 11437
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


# --- Slice A: the additive `host_status` key -------------------------------
#
# Covers three collector contracts: auto discovery with a fixed
# workspace/home and a null launcher URL (so no recursive state request),
# supervisor evidence that moves only the launcher field, and the key existing
# before the stopped-child return with the old aggregate untouched. The first
# two cases run against the shipped CLI alone; the cases marked
# requires_host_status need the collector's host status wiring and skip
# while it is absent.

HOST_STATUS_KEY = "host_status"

# The keywords StateCollector may take for the injected status reader or
# cache. The host status cases run when the collector takes one of them.
_READER_KWARGS = ("host_status_reader", "host_status_cache", "status_cache", "status_reader")

# Patching `collect_status` on each namespace that holds it covers both
# `import module` and `from module import collect_status`.
_STATUS_MODULES = (
    "cymatix_context.cli.cymatix_status",
    "cymatix_context.launcher.status_cache",
    "cymatix_context.launcher.collector",
)

# Projection keys allowed to move between two reads of one cached observation:
# they are computed at serialization time, not at observation time.
_VOLATILE = frozenset({"age_s", "fresh_for_s", "last_attempt_at", "refresh_state"})

_OLD_STOPPED_KEYS = frozenset({"cymatix", "switchboard", "database", "graph_summary", "update"})

_FAKE_REPORT = {
    "host": {"selection": "claude-code", "profile": "Claude Code", "inspected_paths": []},
    "server": {"url": "http://127.0.0.1:11437", "source": "configured",
               "configured_url_match": True, "transport": "reachable", "health": "healthy",
               "payload": {"status": "ok"}, "parse_error": None, "error": None},
    "launcher": {"url": None, "state": "not_configured"},
    "mcp": {"configuration": "canonical", "activation": "enabled", "live": "connected",
            "path": "/fixture/.mcp.json", "detail": ""},
    "skill": {"installation": "present", "activation": "enabled",
              "path": "/fixture/skill", "detail": ""},
    "configured_ready": True,
    "guided_ready": True,
    "next_action": "Cymatix is configured for this host.",
}


def _reader_kwarg():
    """The injection keyword the collector exposes, or None if unwired."""
    params = inspect.signature(StateCollector.__init__).parameters
    return next((name for name in _READER_KWARGS if name in params), None)


requires_host_status = pytest.mark.skipif(
    _reader_kwarg() is None,
    reason="host status wiring absent; expected one of " + ", ".join(_READER_KWARGS),
)


@contextlib.contextmanager
def _spy_collect_status(report=None):
    """Replace `collect_status` everywhere it is bound and record the calls."""
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return dict(report or _FAKE_REPORT)

    spy.calls = calls
    with contextlib.ExitStack() as stack:
        patched = 0
        for name in _STATUS_MODULES:
            try:
                module = importlib.import_module(name)
            except ImportError:
                continue
            if hasattr(module, "collect_status"):
                stack.enter_context(patch.object(module, "collect_status", spy))
                patched += 1
        assert patched, "no module exposed collect_status to patch"
        yield spy


def _observed(collector, deadline_s=5.0):
    """Collect until the panel carries an observation, then return the state.

    The refresh is single flight, so the first caller's finite wait can expire
    before the leader publishes. This polls the public entry point rather than
    reaching into the cache.
    """
    # The panel reports `freshness` as one of fresh, stale or unavailable.
    deadline = time.monotonic() + deadline_s
    state = collector.collect()
    while time.monotonic() < deadline:
        panel = state.get(HOST_STATUS_KEY)
        if isinstance(panel, dict) and panel.get("freshness") != "unavailable":
            break
        time.sleep(0.05)
        state = collector.collect()
    return state


def _probe_recorder(recorded):
    def fake_probe(url, timeout_s=1.0):
        recorded.append(url)
        return cymatix_status.ProbeResult("unreachable", None, None, "fixture")
    return fake_probe


def _stopped(fake_supervisor):
    fake_supervisor.is_running.return_value = False
    fake_supervisor.find_orphan_cymatix.return_value = None
    fake_supervisor.get_last_error.return_value = None
    return fake_supervisor


class TestSelfPollPrevention:
    def test_null_launcher_url_requests_no_state_endpoint(self, tmp_path):
        probed = []
        with patch.object(cymatix_status, "_probe_json", _probe_recorder(probed)):
            report = cymatix_status.collect_status(
                host="auto", server_url=None, launcher_url=None,
                start_dir=tmp_path, home_dir=tmp_path,
            )
        assert probed, "the health probe should still run"
        assert not [url for url in probed if "/api/state" in url]
        assert report["launcher"] == {"url": None, "state": "not_configured"}

    def test_default_launcher_url_would_request_it(self, tmp_path):
        """Tripwire: proves the case above is not vacuously green."""
        probed = []
        with patch.object(cymatix_status, "_probe_json", _probe_recorder(probed)):
            cymatix_status.collect_status(
                host="auto", server_url=None, launcher_url="http://127.0.0.1:11438",
                start_dir=tmp_path, home_dir=tmp_path,
            )
        assert [url for url in probed if url.endswith("/api/state")]

    def test_collector_never_requests_its_own_state_routes(self, collector):
        responses = {"/stats": {"total_genes": 0, "total_chars_raw": 0,
                                "total_chars_compressed": 0, "compression_ratio": 1.0},
                     "/sessions": {"participants": []}}
        client = _mock_client(responses)
        with _spy_collect_status() as spy:
            with patch("httpx.Client", return_value=client):
                with patch.object(collector, "_collect_models", return_value=None):
                    collector.collect()
        requested = [call.args[0] for call in client.get.call_args_list]
        assert "/api/state" not in requested and "/api/state/panels" not in requested
        assert all(kwargs.get("launcher_url") is None for _a, kwargs in spy.calls)

    @requires_host_status
    def test_refresh_uses_auto_discovery_and_a_fixed_local_context(self, fake_supervisor):
        collector = StateCollector(supervisor=_stopped(fake_supervisor))
        with _spy_collect_status() as spy:
            _observed(collector)
        assert spy.calls, "the refresh never called collect_status"
        for args, kwargs in spy.calls:
            assert args == (), "collect_status is keyword only"
            assert kwargs.get("host") == "auto"
            assert kwargs.get("server_url") is None
            assert kwargs.get("launcher_url") is None
            assert Path(kwargs["start_dir"]).is_absolute()
            assert Path(kwargs["home_dir"]).is_absolute()


class TestHostStatusKeyIsAdditive:
    def test_stopped_child_keeps_the_old_aggregate(self, collector, fake_supervisor):
        _stopped(fake_supervisor)
        with _spy_collect_status():
            result = collector.collect()
        assert set(result) - {HOST_STATUS_KEY} <= _OLD_STOPPED_KEYS
        assert result["cymatix"]["running"] is False
        assert result["cymatix"]["availability"] == "unavailable"
        assert result["cymatix"]["next_action"] == "Click Start to launch Cymatix."
        assert "state_file" in result["cymatix"]["paths"]
        for absent in ("genes", "parties", "participants", "tools", "tokens", "models"):
            assert absent not in result

    def test_skip_gate_cannot_hide_a_built_panel(self, collector, fake_supervisor):
        """Not gated: a renamed keyword must go red, not silently skip."""
        _stopped(fake_supervisor)
        with _spy_collect_status():
            result = collector.collect()
        if HOST_STATUS_KEY in result:
            assert _reader_kwarg() is not None, (
                "host_status ships but no keyword in %s; add the real one"
                % (_READER_KWARGS,)
            )

    @requires_host_status
    def test_key_present_before_the_stopped_child_return(self, collector, fake_supervisor):
        _stopped(fake_supervisor)
        with _spy_collect_status():
            state = _observed(collector)
        assert isinstance(state.get(HOST_STATUS_KEY), dict)
        assert state[HOST_STATUS_KEY]["freshness"] in {"fresh", "stale", "unavailable"}


class TestSupervisorOverlay:
    @requires_host_status
    def test_evidence_changes_only_the_launcher_field(self, collector, fake_supervisor):
        """One collector, one cached observation, the child flipped underneath.

        Reusing the same collector is deliberate: the host observation is
        served from the same cache entry both times, so any difference outside
        the launcher block is the overlay leaking into host evidence rather
        than two independent collections drifting.
        """
        # The overlay sets host_status["launcher"]["state"] and the fixed
        # host_status["launcher"]["source"] == "supervisor" label; everything
        # else, minus _VOLATILE, must match between the two reads.
        _stopped(fake_supervisor)
        with _spy_collect_status() as spy:
            stopped = _observed(collector)[HOST_STATUS_KEY]
            fake_supervisor.is_running.return_value = True
            running = collector.collect()[HOST_STATUS_KEY]

        assert len(spy.calls) == 1, "the overlay must not force a host re-read"
        assert stopped["launcher"]["state"] == "stopped"
        assert running["launcher"]["state"] == "running"
        assert running["launcher"]["source"] == "supervisor"
        assert stopped["launcher"]["source"] == "supervisor"

        def stable(panel):
            return {k: v for k, v in panel.items()
                    if k != "launcher" and k not in _VOLATILE}

        assert stable(stopped) == stable(running)
        assert stable(stopped), "the comparison must not be empty"

    @requires_host_status
    @pytest.mark.parametrize(
        "installation, activation, expected",
        [("present", "enabled", True), ("missing", "enabled", False),
         ("present", "disabled", False), ("present", "unknown", None)],
    )
    def test_readiness_survives_the_overlay_across_skill_states(
        self, fake_supervisor, installation, activation, expected
    ):
        # configured_ready / guided_ready keep the CLI's exact
        # true / false / null values in the projection.
        report = dict(_FAKE_REPORT)
        report["skill"] = {"installation": installation, "activation": activation,
                           "path": "/fixture/skill", "detail": ""}
        report["guided_ready"] = cymatix_status.guided_ready(True, installation, activation)
        assert report["guided_ready"] is expected

        _stopped(fake_supervisor)
        with _spy_collect_status(report):
            panel = _observed(StateCollector(supervisor=fake_supervisor))[HOST_STATUS_KEY]
        assert panel["configured_ready"] is True
        assert panel["guided_ready"] is expected
        assert panel["launcher"]["state"] == "stopped"

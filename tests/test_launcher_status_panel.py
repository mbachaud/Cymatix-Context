"""
Tests for the launcher host status panel: the approved projection in
`launcher/collector.py` and the bounded refresh cache in
`launcher/status_cache.py`.

Three properties matter more than the field list, and most of the file is
about them:

  * nothing but the allowlist reaches the snapshot. The report carries
    native config paths, a raw upstream payload, error text and free form
    detail strings; a sentinel is planted in each of them and must not
    appear in the projection, in the cache or in the log.
  * a caller waits a bounded time and is never lied to. A failed, invalid
    or overdue refresh keeps the previous evidence as `stale` with its
    original observation time, or reports `unavailable`, and it never
    repaints an old observation as current.
  * the worker bound holds. One refresh at a time, no replacement while
    one is stuck, no queue, late results fenced out, and every waiter
    resolved even when the reader raises a BaseException.

Handoff ordering is asserted outside the worker (a broken handoff inside
a callback that the cache swallows would otherwise pass silently).
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cymatix_context.cli import cymatix_status
from cymatix_context.cli.cymatix_status import (
    McpLiveReport,
    ProbeResult,
    SkillReport,
    _diagnostic_target_action,
    _next_action,
    _select_server_target,
)
from cymatix_context.launcher import collector as collector_module
from cymatix_context.launcher import status_cache as status_cache_module
from cymatix_context.launcher.collector import (
    APPROVED_NEXT_ACTIONS,
    StateCollector,
    approved_next_action,
    default_status_context,
    default_status_reader,
    project_host_status,
    supervisor_launcher_evidence,
)
from cymatix_context.launcher.status_cache import (
    GENERIC_NEXT_ACTION,
    OBSERVATION_SCOPE,
    SNAPSHOT_KEYS,
    StatusCache,
)

SENTINEL = "s3cr3t-do-not-render"


# -- fixtures and helpers ---------------------------------------------


class FakeClock:
    """One monotonic reading and one wall reading, both movable."""

    def __init__(self, monotonic: float = 1000.0, wall: float = 1_757_000_000.0) -> None:
        self.monotonic = monotonic
        self.wall = wall

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += seconds


def inline_spawn(work):
    """Run the refresh on the calling thread, for deterministic tests."""
    work()


def report(**overrides):
    """A valid `collect_status` report, secrets planted in every extra."""

    base = {
        "host": {
            "selection": "claude-code",
            "profile": "Claude Code",
            "inspected_paths": [f"/home/{SENTINEL}/.mcp.json"],
        },
        "server": {
            "url": f"http://{SENTINEL}:11437",
            "source": "configured",
            "configured_url_match": True,
            "transport": "reachable",
            "health": "healthy",
            "payload": {"status": "ok", "token": SENTINEL},
            "parse_error": SENTINEL,
            "error": SENTINEL,
        },
        "launcher": {"url": f"http://{SENTINEL}:11438", "state": "running"},
        "mcp": {
            "configuration": "canonical",
            "activation": "enabled",
            "live": "connected",
            "path": f"/home/{SENTINEL}/.claude.json",
            "detail": SENTINEL,
        },
        "skill": {
            "installation": "present",
            "activation": "enabled",
            "path": f"/home/{SENTINEL}/SKILL.md",
            "detail": SENTINEL,
        },
        "configured_ready": True,
        "guided_ready": True,
        "next_action": "Use cymatix_context for repo questions.",
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


def strings_in(value):
    """Every string anywhere in a nested structure, keys included."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings_in(key)
            yield from strings_in(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from strings_in(item)


def build_cache(reader, clock=None, **kwargs):
    clock = clock or FakeClock()
    kwargs.setdefault("spawn", inline_spawn)
    return StatusCache(
        reader=reader,
        projector=project_host_status,
        clock=lambda: clock.monotonic,
        wall_clock=lambda: clock.wall,
        **kwargs,
    )


@pytest.fixture
def clock():
    return FakeClock()


# -- the approved schema ----------------------------------------------


class TestApprovedSchema:
    def test_projection_publishes_exactly_the_approved_fields(self):
        projected = project_host_status(report())
        assert set(projected) == {
            "host",
            "server",
            "mcp",
            "skill",
            "configured_ready",
            "guided_ready",
            "next_action",
        }
        assert set(projected["host"]) == {"selection", "profile"}
        assert set(projected["server"]) == {
            "transport",
            "health",
            "source",
            "configured_url_match",
        }
        assert set(projected["mcp"]) == {"configuration", "activation", "live"}
        assert set(projected["skill"]) == {"installation", "activation"}

    def test_snapshot_keys_are_the_published_contract(self, clock):
        cache = build_cache(lambda: report(), clock)
        assert set(cache.get()) == set(SNAPSHOT_KEYS)

    def test_secrets_in_paths_payloads_and_details_never_reach_the_projection(self):
        projected = project_host_status(report())
        assert SENTINEL not in " ".join(strings_in(projected))

    def test_secrets_never_reach_the_cache_or_the_log(self, clock, caplog):
        cache = build_cache(lambda: report(), clock)
        with caplog.at_level(logging.DEBUG):
            snapshot = cache.get()
        assert SENTINEL not in " ".join(strings_in(snapshot))
        assert SENTINEL not in " ".join(strings_in(vars(cache)))
        assert SENTINEL not in caplog.text

    @pytest.mark.parametrize("value", [1, 0, "true", [], 1.0])
    def test_integer_truthiness_is_not_a_boolean_contract(self, value):
        assert project_host_status(report(configured_ready=value)) is None

    @pytest.mark.parametrize(
        "configured,installation,activation,guided",
        [
            (True, "present", "enabled", True),
            (False, "present", "enabled", False),
            (None, "present", "enabled", None),
            (True, "missing", "enabled", False),
            (True, "present", "unknown", None),
            (True, "present", "disabled", False),
        ],
    )
    def test_true_false_and_null_stay_distinct(
        self, configured, installation, activation, guided,
    ):
        projected = project_host_status(
            report(
                configured_ready=configured,
                guided_ready=guided,
                skill={"installation": installation, "activation": activation},
            )
        )
        assert projected["configured_ready"] is configured
        assert projected["guided_ready"] is guided

    def test_a_disagreeing_guided_value_is_an_invalid_report(self):
        # The collected value and the helper derived value must agree;
        # a report that says otherwise is not presentable evidence.
        assert project_host_status(report(guided_ready=False)) is None

    @pytest.mark.parametrize(
        "group,field,value",
        [
            ("host", "selection", "some-other-host"),
            ("host", "profile", "Totally New Host"),
            ("server", "transport", "degraded"),
            ("server", "health", "ok"),
            ("server", "source", "launcher"),
            ("server", "configured_url_match", "yes"),
            ("mcp", "configuration", "present"),
            ("mcp", "activation", "on"),
            ("mcp", "live", "active"),
            ("skill", "installation", "installed"),
            ("skill", "activation", "on"),
        ],
    )
    def test_values_outside_the_registry_vocabulary_are_rejected(self, group, field, value):
        assert project_host_status(report(**{group: {field: value}})) is None

    @pytest.mark.parametrize("bad", [None, [], "report", 7, {}, {"host": {}}])
    def test_a_malformed_report_is_rejected_whole(self, bad):
        assert project_host_status(bad) is None

    def test_every_approved_host_id_and_label_is_accepted(self):
        from cymatix_context.integrations.host_profiles import HOST_PROFILES

        for profile in HOST_PROFILES:
            projected = project_host_status(
                report(host={"selection": profile.id, "profile": profile.display_name})
            )
            assert projected["host"]["selection"] == profile.id
        for selection in ("unknown", "ambiguous"):
            projected = project_host_status(
                report(host={"selection": selection, "profile": None})
            )
            assert projected["host"]["profile"] is None

    def test_distinct_negative_evidence_survives_projection(self):
        projected = project_host_status(
            report(
                host={"selection": "ambiguous", "profile": None},
                server={
                    "transport": "unreachable",
                    "health": "unknown",
                    "source": "default",
                    "configured_url_match": None,
                },
                mcp={"configuration": "invalid", "activation": "unknown", "live": "unknown"},
                skill={"installation": "missing", "activation": "unknown"},
                configured_ready=False,
                guided_ready=False,
                next_action="Multiple host configs contain cymatix-context; pass --host explicitly.",
            )
        )
        assert projected["server"]["transport"] == "unreachable"
        assert projected["server"]["health"] == "unknown"
        assert projected["server"]["configured_url_match"] is None
        assert projected["mcp"]["live"] == "unknown"
        assert projected["host"]["selection"] == "ambiguous"


class TestApprovedGuidance:
    @pytest.mark.parametrize(
        "text",
        [
            "<script>alert(1)</script>",
            f"Use the token {SENTINEL} to connect.",
            "Run rm -rf / to repair the install.",
            "x" * 600,
            "",
            None,
            12,
        ],
    )
    def test_unapproved_guidance_is_replaced_not_merely_escaped(self, text):
        projected = project_host_status(report(next_action=text))
        assert projected["next_action"] == GENERIC_NEXT_ACTION
        assert SENTINEL not in projected["next_action"]
        assert "<" not in projected["next_action"]

    def test_approved_guidance_passes_through_unchanged(self):
        for text in APPROVED_NEXT_ACTIONS:
            assert approved_next_action(text) == text

    def test_every_guidance_string_the_cli_can_produce_is_approved(self):
        # Drift guard. If upstream rewords a next action, the panel would
        # silently fall back to the generic instruction; this fails first.
        targets = {None}
        for configured in (
            None,
            "http://127.0.0.1:11437",
            "http://10.1.2.3:11437",
            "ht!tp://broken",
            "http://[::1]:11437",
        ):
            targets.add(_select_server_target(explicit_url=None, configured_url=configured).action)
        diagnostics = {_diagnostic_target_action(value) for value in (True, False, None)}
        skills = [
            SkillReport(installation="present", activation="enabled", path=Path()),
            SkillReport(installation="present", activation="disabled", path=Path()),
            SkillReport(installation="present", activation="unknown", path=Path()),
            SkillReport(installation="missing", activation="unknown", path=Path()),
        ]
        produced = set()
        for selection in ("unknown", "ambiguous", "claude-code"):
            for configuration in ("canonical", "noncanonical", "missing", "invalid"):
                for activation in ("enabled", "disabled", "unknown"):
                    for health in ("healthy", "unhealthy", "unknown"):
                        for skill in skills:
                            for live in ("connected", "disconnected", "unknown"):
                                for target in targets:
                                    for diagnostic in diagnostics:
                                        produced.add(
                                            _next_action(
                                                selection=selection,
                                                configuration=configuration,
                                                mcp_activation=activation,
                                                health=health,
                                                skill=skill,
                                                live=McpLiveReport(live, "", None),
                                                target_action=target,
                                                diagnostic_action=diagnostic,
                                            )
                                        )
        assert produced <= APPROVED_NEXT_ACTIONS, produced - APPROVED_NEXT_ACTIONS


# -- the real collector path ------------------------------------------


class TestRealReportPath:
    """Sentinels planted in real files, read by the real collector."""

    @pytest.fixture
    def workspace(self, tmp_path):
        # The directory name carries the sentinel: host_profiles keeps no
        # environment mapping, so the paths are the leak vector the real
        # report actually has.
        config = tmp_path / f"ws-{SENTINEL}" / ".mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps({
                "mcpServers": {
                    "cymatix-context": {
                        "command": "python",
                        "args": ["-m", "cymatix_context.mcp_server"],
                        "env": {
                            "CYMATIX_MCP_URL": "http://127.0.0.1:11437",
                            "CYMATIX_MCP_HOST": "claude-code",
                            "CYMATIX_HANDLE": "operator",
                            "CYMATIX_API_TOKEN": SENTINEL,
                        },
                    }
                }
            }),
            encoding="utf-8",
        )
        skill = config.parent / ".claude/skills/cymatix-context/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"# skill {SENTINEL}\n", encoding="utf-8")
        return config.parent

    @pytest.fixture
    def offline(self, monkeypatch):
        """No socket is opened by any test in this class."""
        calls = []

        def refuse(url, timeout_s=None):
            calls.append(url)
            return ProbeResult("unreachable", None, None, "offline test")

        monkeypatch.setattr(cymatix_status, "_probe_json", refuse)
        return calls

    def test_real_report_projects_without_leaking_config_contents(
        self, workspace, tmp_path, offline, caplog,
    ):
        read = default_status_reader(workspace=workspace, home=tmp_path / "home")
        with caplog.at_level(logging.DEBUG):
            raw = read()
            projected = project_host_status(raw)
        planted = [text for text in strings_in(raw) if SENTINEL in text]
        assert planted, "the fixture must plant a real secret in the report"
        assert projected is not None
        assert projected["host"]["selection"] == "claude-code"
        assert SENTINEL not in " ".join(strings_in(projected))
        assert SENTINEL not in caplog.text

    def test_the_reader_uses_auto_discovery_and_never_probes_the_launcher(
        self, workspace, tmp_path, offline,
    ):
        read = default_status_reader(workspace=workspace, home=tmp_path / "home")
        raw = read()
        assert raw["launcher"]["url"] is None
        assert raw["launcher"]["state"] == "not_configured"
        assert not any("/api/state" in url for url in offline), offline

    def test_the_reader_passes_the_fixed_context_and_no_explicit_target(
        self, workspace, tmp_path, monkeypatch,
    ):
        seen = {}

        def capture(**kwargs):
            seen.update(kwargs)
            return report()

        monkeypatch.setattr(collector_module, "collect_status", capture)
        default_status_reader(workspace=workspace, home=tmp_path / "home")()
        assert seen == {
            "host": "auto",
            "server_url": None,
            "launcher_url": None,
            "mcp_config": None,
            "skill_dir": None,
            "start_dir": workspace,
            "home_dir": tmp_path / "home",
        }

    def test_a_status_read_changes_nothing_on_disk(self, workspace, tmp_path, offline):
        before = {
            path: path.read_bytes()
            for path in sorted(workspace.rglob("*"))
            if path.is_file()
        }
        default_status_reader(workspace=workspace, home=tmp_path / "home")()
        after = {
            path: path.read_bytes()
            for path in sorted(workspace.rglob("*"))
            if path.is_file()
        }
        assert before == after

    def test_the_context_identity_is_never_published(self, workspace, tmp_path, offline):
        context = default_status_context(workspace=workspace, home=tmp_path / "home")
        cache = StatusCache(
            reader=default_status_reader(workspace=workspace, home=tmp_path / "home"),
            projector=project_host_status,
            context_factory=lambda: context,
            spawn=inline_spawn,
        )
        snapshot = cache.get()
        published = " ".join(strings_in(snapshot)) + " ".join(strings_in(vars(cache)))
        assert str(workspace) not in published
        assert context not in published


# -- cache timing ------------------------------------------------------


class TestCacheTiming:
    def test_the_ttl_starts_when_the_observation_completes(self, clock):
        reads = []

        def read():
            reads.append(1)
            clock.advance(3.0)
            return report()

        cache = build_cache(read, clock)
        first = cache.get()
        assert first["freshness"] == "fresh"
        assert first["age_s"] == 0.0
        clock.advance(4.9)
        assert cache.get()["freshness"] == "fresh"
        assert len(reads) == 1
        clock.advance(0.2)
        assert cache.get()["freshness"] == "fresh"
        assert len(reads) == 2, "expiry must admit exactly one new refresh"

    def test_a_hit_does_not_advance_the_attempt_time(self, clock):
        cache = build_cache(lambda: report(), clock)
        first = cache.get()
        clock.advance(1.0)
        second = cache.get()
        assert second["last_attempt_at"] == first["last_attempt_at"]
        assert second["observed_at"] == first["observed_at"]
        assert second["age_s"] == 1.0
        assert second["fresh_for_s"] == 4.0

    def test_an_admitted_refresh_advances_the_attempt_time_only(self, clock):
        outcomes = [report(), RuntimeError("reader down")]

        def read():
            value = outcomes.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        cache = build_cache(read, clock)
        good = cache.get()
        clock.advance(6.0)
        stale = cache.get()
        assert stale["freshness"] == "stale"
        assert stale["refresh_state"] == "failed"
        assert stale["observed_at"] == good["observed_at"]
        assert stale["last_success_at"] == good["last_success_at"]
        assert stale["last_attempt_at"] != good["last_attempt_at"]
        assert stale["host"] == good["host"], "retained evidence, marked stale"

    def test_an_initial_failure_is_unavailable_not_stale(self, clock):
        def read():
            raise OSError("no config")

        snapshot = build_cache(read, clock).get()
        assert snapshot["freshness"] == "unavailable"
        assert snapshot["refresh_state"] == "failed"
        assert snapshot["observed_at"] is None
        assert snapshot["last_success_at"] is None
        assert snapshot["last_attempt_at"] is not None
        assert snapshot["host"] is None
        assert snapshot["configured_ready"] is None
        assert snapshot["guided_ready"] is None
        assert snapshot["next_action"] == GENERIC_NEXT_ACTION
        assert snapshot["observation_scope"] == OBSERVATION_SCOPE

    def test_an_invalid_report_is_a_failed_refresh_not_invented_evidence(self, clock):
        snapshot = build_cache(lambda: {"host": {"selection": "nope"}}, clock).get()
        assert snapshot["refresh_state"] == "invalid"
        assert snapshot["freshness"] == "unavailable"
        assert snapshot["host"] is None

    def test_a_failure_cooldown_holds_off_the_next_attempt(self, clock):
        reads = []

        def read():
            reads.append(1)
            raise RuntimeError("down")

        cache = build_cache(read, clock)
        cache.get()
        clock.advance(4.0)
        cache.get()
        assert len(reads) == 1, "a poll inside the cooldown must not retry"
        clock.advance(1.5)
        cache.get()
        assert len(reads) == 2

    def test_recovery_advances_the_success_time_and_restores_fresh(self, clock):
        state = {"fail": False}

        def read():
            if state["fail"]:
                raise RuntimeError("down")
            return report()

        cache = build_cache(read, clock)
        first = cache.get()
        state["fail"] = True
        clock.advance(6.0)
        assert cache.get()["freshness"] == "stale"
        state["fail"] = False
        clock.advance(6.0)
        recovered = cache.get()
        assert recovered["freshness"] == "fresh"
        assert recovered["refresh_state"] == "idle"
        assert recovered["last_success_at"] != first["last_success_at"]

    def test_negative_evidence_is_a_successful_observation(self, clock):
        """Unreachable and unknown are collected facts, not cache errors."""

        unreachable = report(
            server={
                "transport": "unreachable",
                "health": "unknown",
                "source": "default",
                "configured_url_match": None,
            },
            mcp={"live": "unknown", "activation": "unknown"},
            configured_ready=None,
            guided_ready=None,
        )
        snapshot = build_cache(lambda: unreachable, clock).get()
        assert snapshot["freshness"] == "fresh"
        assert snapshot["refresh_state"] == "idle"
        assert snapshot["last_success_at"] is not None
        assert snapshot["server"]["transport"] == "unreachable"
        assert snapshot["configured_ready"] is None

    def test_age_and_lifetime_stay_nonnegative_and_finite(self, clock):
        cache = build_cache(lambda: report(), clock)
        cache.get()
        clock.monotonic -= 50.0
        snapshot = cache.get()
        assert snapshot["age_s"] >= 0.0
        assert snapshot["fresh_for_s"] >= 0.0
        assert snapshot["fresh_for_s"] <= cache.ttl_s


# -- concurrency and the worker bound ---------------------------------


class TestSingleFlight:
    def test_concurrent_callers_share_one_refresh_and_get_separate_copies(self):
        started = threading.Event()
        release = threading.Event()
        reads = []

        def read():
            reads.append(1)
            started.set()
            assert release.wait(5), "reader was never released"
            return report()

        cache = build_cache(read, caller_wait_s=5.0, spawn=None)
        cache._spawn = status_cache_module._spawn_thread
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(cache.get)
            assert started.wait(5)
            second = pool.submit(cache.get)
            third = pool.submit(cache.get)
            release.set()
            results = [first.result(timeout=5), second.result(timeout=5), third.result(timeout=5)]
        assert len(reads) == 1, "one refresh, shared by every caller"
        assert all(result["host"] == results[0]["host"] for result in results)
        assert results[0]["host"] is not results[1]["host"], "nested copies must be independent"

    def test_a_caller_never_waits_longer_than_its_budget(self, clock):
        release = threading.Event()

        def read():
            assert release.wait(10)
            return report()

        cache = build_cache(read, clock, caller_wait_s=0.05, spawn=None)
        cache._spawn = status_cache_module._spawn_thread
        try:
            snapshot = cache.get()
            assert snapshot["freshness"] == "unavailable"
            assert snapshot["refresh_state"] == "refreshing"
        finally:
            release.set()

    def test_the_bookkeeping_lock_stays_available_while_a_reader_hangs(self, clock):
        entered = threading.Event()
        release = threading.Event()

        def read():
            entered.set()
            assert release.wait(10)
            return report()

        cache = build_cache(read, clock, caller_wait_s=0.05, spawn=None)
        cache._spawn = status_cache_module._spawn_thread
        try:
            cache.get()
            assert entered.wait(5)
            with ThreadPoolExecutor(max_workers=1) as pool:
                # A second poll must be served promptly rather than
                # queueing behind the lock the reader would otherwise hold.
                assert pool.submit(cache.get).result(timeout=2)["freshness"] == "unavailable"
        finally:
            release.set()

    def test_no_replacement_worker_is_admitted_while_one_is_stuck(self, clock):
        entered = threading.Event()
        release = threading.Event()
        reads = []

        def read():
            reads.append(1)
            entered.set()
            assert release.wait(10)
            return report()

        cache = build_cache(read, clock, caller_wait_s=0.05, spawn=None)
        cache._spawn = status_cache_module._spawn_thread
        try:
            cache.get()
            assert entered.wait(5)
            for _ in range(5):
                clock.advance(20.0)
                cache.get()
            assert len(reads) == 1, "the single slot must not grow a second worker"
        finally:
            release.set()

    def test_a_returned_snapshot_cannot_poison_the_next_poll(self, clock):
        cache = build_cache(lambda: report(), clock)
        first = cache.get()
        first["host"]["selection"] = "tampered"
        first["next_action"] = SENTINEL
        second = cache.get()
        assert second["host"]["selection"] == "claude-code"
        assert second["next_action"] != SENTINEL


class TestFencesAndCleanup:
    def test_a_base_exception_resolves_waiters_releases_the_slot_and_is_reraised(self, clock):
        order = []
        escaped = []

        class Fatal(BaseException):
            pass

        def read():
            order.append("read")
            raise Fatal("fatal")

        def spawn(work):
            def run():
                try:
                    work()
                except BaseException as exc:  # noqa: BLE001
                    escaped.append(exc)
                order.append("worker-exit")

            thread = threading.Thread(target=run)
            thread.start()
            thread.join(5)
            assert not thread.is_alive()

        cache = build_cache(read, clock, caller_wait_s=1.0, spawn=spawn)
        snapshot = cache.get()
        order.append("caller-returned")

        # Ordering is asserted here, outside the worker: an assertion
        # inside a swallowed callback would never fail the test.
        assert order == ["read", "worker-exit", "caller-returned"]
        assert escaped and isinstance(escaped[0], Fatal), "a fatal error must not be swallowed"
        assert cache._worker_busy is False
        assert cache._inflight is None
        assert snapshot["freshness"] == "unavailable"
        assert snapshot["refresh_state"] == "failed"

        # The slot is genuinely free: a later poll is admitted again.
        clock.advance(10.0)
        cache._reader = lambda: report()
        assert cache.get()["freshness"] == "fresh"

    def test_a_deliberately_broken_handoff_is_detected(self, clock):
        """The ordering guard itself must be able to fail."""

        order = []

        def spawn(work):
            order.append("spawned")  # and never run: the handoff is broken

        cache = build_cache(lambda: report(), clock, caller_wait_s=0.05, spawn=spawn)
        snapshot = cache.get()
        assert order == ["spawned"]
        assert snapshot["freshness"] == "unavailable"
        assert snapshot["refresh_state"] == "refreshing"

    def test_a_worker_that_cannot_start_releases_the_slot(self, clock):
        def refuse(work):
            raise RuntimeError("can't start new thread")

        cache = build_cache(lambda: report(), clock, spawn=refuse)
        with pytest.raises(RuntimeError):
            cache.get()
        assert cache._worker_busy is False
        assert cache._inflight is None

        # The panel must recover rather than sit on a slot nobody holds.
        clock.advance(10.0)
        cache._spawn = inline_spawn
        assert cache.get()["freshness"] == "fresh"

    def test_an_overdue_refresh_reads_timed_out_before_it_returns(self, clock):
        entered = threading.Event()
        release = threading.Event()

        def read():
            entered.set()
            assert release.wait(10)
            return report()

        cache = build_cache(read, clock, caller_wait_s=0.05, spawn=None)
        cache._spawn = status_cache_module._spawn_thread
        try:
            assert cache.get()["refresh_state"] == "refreshing"
            assert entered.wait(5)
            clock.advance(26.0)
            assert cache.get()["refresh_state"] == "timed_out"
        finally:
            release.set()

    def test_a_late_result_cannot_publish(self, clock):
        def read():
            clock.advance(30.0)
            return report()

        cache = build_cache(read, clock)
        snapshot = cache.get()
        assert snapshot["refresh_state"] == "timed_out"
        assert snapshot["freshness"] == "unavailable"
        assert snapshot["host"] is None
        assert snapshot["last_success_at"] is None

    def test_a_context_change_never_serves_another_context_evidence(self, clock):
        context = {"value": "workspace-a"}
        payloads = {
            "workspace-a": report(host={"selection": "claude-code", "profile": "Claude Code"}),
            "workspace-b": report(host={"selection": "codex", "profile": "Codex"}),
        }
        cache = build_cache(
            lambda: payloads[context["value"]],
            clock,
            context_factory=lambda: context["value"],
            spawn=lambda work: None,
        )
        cache._spawn = inline_spawn
        assert cache.get()["host"]["selection"] == "claude-code"

        context["value"] = "workspace-b"
        cache._spawn = lambda work: None  # the new observation has not arrived
        retired = cache.get()
        assert retired["freshness"] == "unavailable"
        assert retired["host"] is None
        assert retired["observed_at"] is None

    def test_a_same_context_change_appears_on_the_next_refresh(self, clock):
        current = {"value": report(mcp={"activation": "enabled"})}
        cache = build_cache(lambda: current["value"], clock)
        assert cache.get()["mcp"]["activation"] == "enabled"
        current["value"] = report(
            mcp={"activation": "disabled"}, configured_ready=False, guided_ready=False,
        )
        assert cache.get()["mcp"]["activation"] == "enabled", "inside the TTL"
        clock.advance(6.0)
        assert cache.get()["mcp"]["activation"] == "disabled"

    def test_clear_retires_the_snapshot_and_keeps_the_worker_bound(self, clock):
        cache = build_cache(lambda: report(), clock)
        assert cache.get()["freshness"] == "fresh"
        cache._worker_busy = True  # pretend a stuck worker still holds the slot
        cache.clear()
        snapshot = cache.get()
        assert snapshot["freshness"] == "unavailable"
        assert snapshot["observed_at"] is None
        assert cache._inflight is None


# -- collector composition --------------------------------------------


class FakeCache:
    """A StatusCache stand in: no reader, no thread, no clock."""

    def __init__(self, snapshot=None, raises=None, stamp="2026-09-11T00:00:00Z"):
        self.snapshot = snapshot if snapshot is not None else _fresh_snapshot()
        self.raises = raises
        self.stamp = stamp
        self.calls = 0

    def get(self):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return dict(self.snapshot)

    def wall_now(self):
        return self.stamp


def _fresh_snapshot():
    snapshot = status_cache_module.unavailable_snapshot()
    snapshot.update(project_host_status(report()))
    snapshot["freshness"] = "fresh"
    return snapshot


@pytest.fixture
def supervisor(tmp_path):
    sup = MagicMock()
    sup.cymatix_host = "127.0.0.1"
    sup.cymatix_port = 11437
    sup.is_running.return_value = False
    sup.get_pid.return_value = None
    sup.get_uptime_s.return_value = None
    sup.find_orphan_cymatix.return_value = None
    sup.get_last_error.return_value = None
    sup.store.path = tmp_path / "state.json"
    sup.cymatix_log_path = tmp_path / "cymatix.log"
    return sup


class TestCollectorComposition:
    def test_the_panel_is_present_while_the_child_is_stopped(self, supervisor):
        cache = FakeCache()
        state = StateCollector(supervisor=supervisor, status_cache=cache).collect()
        assert state["cymatix"]["running"] is False
        assert "genes" not in state, "the stopped child return must still happen"
        assert state["host_status"]["freshness"] == "fresh"
        assert state["host_status"]["host"]["selection"] == "claude-code"
        assert cache.calls == 1

    @pytest.mark.parametrize(
        "running,expected",
        [(True, "running"), (False, "stopped"), (None, "unreachable"), ("yes", "unreachable")],
    )
    def test_the_supervisor_overlay_maps_child_liveness(self, running, expected):
        evidence = supervisor_launcher_evidence(running, "2026-09-11T00:00:00Z")
        assert evidence["state"] == expected
        assert evidence["source"] == "supervisor"
        assert evidence["observed_at"] == "2026-09-11T00:00:00Z"

    def test_the_overlay_changes_only_the_launcher_dimension(self, supervisor):
        cache = FakeCache()
        baseline = dict(cache.snapshot)
        state = StateCollector(supervisor=supervisor, status_cache=cache).collect()
        panel = state["host_status"]
        assert panel["launcher"] == {
            "state": "stopped",
            "source": "supervisor",
            "observed_at": cache.stamp,
        }
        for key in baseline:
            assert panel[key] == baseline[key], key
        assert panel["configured_ready"] is True
        assert panel["guided_ready"] is True

    def test_stale_host_evidence_coexists_with_a_fresh_stopped_child(self, supervisor):
        snapshot = _fresh_snapshot()
        snapshot["freshness"] = "stale"
        snapshot["age_s"] = 42.0
        cache = FakeCache(snapshot=snapshot)
        panel = StateCollector(supervisor=supervisor, status_cache=cache).collect()["host_status"]
        assert panel["freshness"] == "stale"
        assert panel["launcher"]["state"] == "stopped"
        assert panel["launcher"]["observed_at"] == cache.stamp

    def test_a_failing_cache_never_breaks_the_state_payload(self, supervisor, caplog):
        cache = FakeCache(raises=RuntimeError(SENTINEL))
        with caplog.at_level(logging.WARNING):
            state = StateCollector(supervisor=supervisor, status_cache=cache).collect()
        panel = state["host_status"]
        assert panel["freshness"] == "unavailable"
        assert panel["launcher"]["state"] == "stopped"
        assert SENTINEL not in " ".join(strings_in(panel))

    def test_the_existing_state_keys_are_untouched(self, supervisor):
        collector = StateCollector(supervisor=supervisor, status_cache=FakeCache())
        state = collector.collect()
        assert set(state) - {"host_status"} <= {
            "cymatix",
            "switchboard",
            "database",
            "graph_summary",
            "update",
        }
        assert "next_action" in state["cymatix"]
        assert state["cymatix"]["availability"] == "unavailable"

    def test_a_status_read_triggers_no_control_action(self, supervisor):
        StateCollector(supervisor=supervisor, status_cache=FakeCache()).collect()
        for forbidden in ("start", "stop", "restart", "install", "repair", "ingest"):
            assert not getattr(supervisor, forbidden).called, forbidden


# =====================================================================
# Attack tests
# =====================================================================
#
# Each test below was written as a failing test against a hole found in
# the first build of the status cache and projection, and cites the
# source line that let it through. These tests change no production
# code: a test here that passes is the evidence that its hole is closed.
#
# Only attacks that landed are recorded here: a passing test for an
# attack that never landed would only pad the suite.
#
# Line references are `cymatix_context/...` as first built on this
# branch for the new files (status_cache.py, collector.py) and at
# upstream c05849b for files this branch had not yet changed
# (cli/cymatix_status.py, integrations/host_profiles.py).

import email
import http.client
import io
import urllib.request
import urllib.response


@pytest.fixture
def sandbox_env(tmp_path, monkeypatch):
    """Point every config discovery environment variable at scratch.

    Nothing in the status path is supposed to read the real user
    profile. This fixture makes a mistake in that direction fail into an
    empty temporary directory instead of into the operator's home.
    """

    sandbox = tmp_path / "sandbox-env"
    for name in ("home", "tmp", "config", "data", "state", "cache"):
        (sandbox / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(sandbox / "home"))
    monkeypatch.setenv("USERPROFILE", str(sandbox / "home"))
    monkeypatch.setenv("TMPDIR", str(sandbox / "tmp"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(sandbox / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(sandbox / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(sandbox / "cache"))
    monkeypatch.delenv("CYMATIX_STATUS_URL", raising=False)
    monkeypatch.delenv("CYMATIX_LAUNCHER_URL", raising=False)
    return sandbox


@pytest.fixture
def recorded_probes(monkeypatch):
    """Record every probe URL and open no socket."""

    urls = []

    def refuse(url, timeout_s=None):
        urls.append(url)
        return ProbeResult("unreachable", None, None, "offline test")

    monkeypatch.setattr(cymatix_status, "_probe_json", refuse)
    return urls


def _canonical_config(url):
    return json.dumps({
        "mcpServers": {
            "cymatix-context": {
                "command": "python",
                "args": ["-m", "cymatix_context.mcp_server"],
                "env": {"CYMATIX_MCP_URL": url, "CYMATIX_MCP_HOST": "claude-code"},
            }
        }
    })


class TestCacheHoles:
    """Attacks on the timing and publication fences in status_cache.py."""

    def test_hole_a_worker_started_late_publishes_after_the_panel_said_timed_out(self, clock):
        """The deadline is measured twice, from two different instants.

        The caller facing deadline runs from admission (`_inflight_started`
        is stamped in `get`, status_cache.py:228, and read by
        `_display_state_locked`, :371). The publication fence runs from
        the worker's own start (`started = self._clock()`, :264, compared
        at :277). Anything between admission and the worker's first
        instruction, thread start latency included, is inside the fence
        and outside the display. A refresh the panel has already reported
        as `timed_out` therefore publishes as a fresh observation.
        """

        pending = []
        cache = build_cache(lambda: report(), clock, caller_wait_s=0.0, spawn=pending.append)

        assert cache.get()["refresh_state"] == "refreshing"
        clock.advance(26.0)
        assert cache.get()["refresh_state"] == "timed_out", "the panel published timed_out"

        pending[0]()  # the worker finally starts, and its read returns at once
        published = cache.get()
        assert published["freshness"] != "fresh", "a fenced refresh must not publish"
        assert published["last_success_at"] is None, "and must not record a success"

    def test_hole_the_cache_publishes_every_key_the_projector_returns(self, clock):
        """`SNAPSHOT_KEYS` is documentation, not a boundary check.

        status_cache.py:69 calls `SNAPSHOT_KEYS` "every key the cache
        publishes" and the module docstring promises "approved snapshots
        only". `_render_locked` then does an unchecked
        `snapshot.update(copy.deepcopy(self._snapshot))` (:352), so the
        published mapping is whatever the projector handed over. The
        allowlist exists once, in the projector; the publication boundary
        re-checks nothing, so a projector that grows a field, or is
        replaced, carries it into the snapshot, the JSON route and the
        template in one step.
        """

        def leaky(raw):
            projected = project_host_status(raw)
            projected["inspected_paths"] = [f"/home/{SENTINEL}/.mcp.json"]
            projected["observation_scope"] = f"/home/{SENTINEL}/workspace"
            return projected

        cache = StatusCache(
            reader=lambda: report(),
            projector=leaky,
            clock=lambda: clock.monotonic,
            wall_clock=lambda: clock.wall,
            spawn=inline_spawn,
        )
        snapshot = cache.get()
        assert set(snapshot) == set(SNAPSHOT_KEYS), "unapproved keys were published"
        assert SENTINEL not in " ".join(strings_in(snapshot))

    def test_hole_clear_during_a_refresh_reports_idle_while_a_worker_runs(self, clock):
        """`clear` hides the running refresh instead of reporting it.

        `_retire_locked` drops the in flight future and resets
        `_refresh_state` to "idle" (status_cache.py:329 to 340) but
        leaves `_worker_busy` set, which is correct for the worker bound
        and wrong for the display: `_display_state_locked` (:368) only
        looks at `_inflight`, so the panel reads `idle` while a refresh is
        running and while no other refresh can be admitted. The operator
        is shown a cache at rest and an empty card, and nothing on the
        page says a collection is still out.
        """

        pending = []
        cache = build_cache(lambda: report(), clock, caller_wait_s=0.0, spawn=pending.append)
        cache.get()
        cache.clear()

        snapshot = cache.get()
        assert len(pending) == 1, "the worker bound itself holds"
        assert snapshot["refresh_state"] == "refreshing", "a running refresh must be visible"

    def test_hole_a_context_change_reports_idle_while_the_retired_worker_holds_the_slot(
        self, clock,
    ):
        """The same blind spot on the context retirement path.

        A changed execution context retires through the same
        `_retire_locked` (status_cache.py:208 to 209), so the new context
        is served `unavailable` plus `idle` even though the only refresh
        slot is still occupied by the retired observation and no refresh
        for the new context can start until it exits.
        """

        context = {"value": "workspace-a"}
        pending = []
        cache = build_cache(
            lambda: report(),
            clock,
            caller_wait_s=0.0,
            context_factory=lambda: context["value"],
            spawn=pending.append,
        )
        cache.get()
        context["value"] = "workspace-b"

        snapshot = cache.get()
        assert len(pending) == 1
        assert snapshot["freshness"] == "unavailable"
        assert snapshot["refresh_state"] == "refreshing", "a running refresh must be visible"


class TestLogHoles:
    """Sentinels that reach a log line rather than the page."""

    def test_hole_the_reader_exception_text_reaches_a_log_line(self, clock, caplog):
        """status_cache.py:269 logs the reader failure with `exc_info`.

        The projection never renders an error string, and that is the
        half of the promise this path keeps. The other half, "do not log
        raw values", is not kept: the whole traceback of whatever the
        reader raised goes to the launcher log at WARNING, and the reader
        is the real `cymatix-status` collector reading native host
        configuration files.
        """

        def read():
            raise RuntimeError(f"could not read native config token {SENTINEL}")

        cache = build_cache(read, clock)
        with caplog.at_level(logging.DEBUG):
            snapshot = cache.get()

        assert snapshot["freshness"] == "unavailable", "the page itself stays clean"
        assert SENTINEL not in caplog.text, "the log line is not clean"

    def test_hole_the_collector_logs_the_cache_exception_text(self, supervisor, caplog):
        """The same leak one layer up.

        `StateCollector._host_status_panel` logs its fallback with
        `exc_info=True` (collector.py, `log.warning("Host status snapshot
        unavailable", exc_info=True)`), so an exception that carries a
        secret is written out in full while the panel it protects shows
        nothing. `test_a_failing_cache_never_breaks_the_state_payload`
        already raises `RuntimeError(SENTINEL)` here and checks only the
        payload, which is why this went unnoticed.
        """

        cache = FakeCache(raises=RuntimeError(f"token {SENTINEL}"))
        with caplog.at_level(logging.WARNING):
            state = StateCollector(supervisor=supervisor, status_cache=cache).collect()

        assert state["host_status"]["freshness"] == "unavailable"
        assert SENTINEL not in caplog.text

    def test_hole_a_malformed_native_config_logs_its_path(
        self, tmp_path, sandbox_env, recorded_probes, caplog,
    ):
        """A real read failure logs the config path at WARNING.

        `host_profiles._read_native_config` logs `path` and the exception
        with `exc_info=True` (host_profiles.py:214 at c05849b). The
        projection drops `inspected_paths` precisely because a path is
        not approved for output; the log keeps it anyway, and the status
        refresh is what triggers the read. The sentinel is in the
        directory name here for the same reason the existing fixture puts
        it there: the path is the leak vector this report really has.
        """

        workspace = tmp_path / f"ws-{SENTINEL}"
        workspace.mkdir()
        (workspace / ".mcp.json").write_text("{ not json at all", encoding="utf-8")

        read = default_status_reader(workspace=workspace, home=tmp_path / "home")
        with caplog.at_level(logging.WARNING):
            projected = project_host_status(read())

        assert SENTINEL not in " ".join(strings_in(projected or {})), "the page stays clean"
        assert SENTINEL not in caplog.text

    def test_hole_an_upstream_probe_error_logs_the_url_and_the_error_text(
        self, tmp_path, sandbox_env, monkeypatch, caplog,
    ):
        """cymatix_status.py:352 logs the probe URL and the exception.

        Two approved-for-nothing values reach the log in one line: the
        configured endpoint URL, which the projection drops by design,
        and the text of whatever the transport raised, which the
        projection also drops by design. The URL comes out of the native
        host config, so it is attacker shaped in exactly the way the
        sentinel tests assume. `_read_bounded_status_body` does the same
        at :321.
        """

        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / ".mcp.json").write_text(
            _canonical_config(f"http://127.0.0.1:11437/{SENTINEL}"), encoding="utf-8",
        )

        def explode(url, timeout=None):
            raise ValueError(f"transport refused the {SENTINEL} handshake")

        monkeypatch.setattr(urllib.request, "urlopen", explode)

        read = default_status_reader(workspace=workspace, home=tmp_path / "home")
        with caplog.at_level(logging.WARNING):
            projected = project_host_status(read())

        assert SENTINEL not in " ".join(strings_in(projected or {})), "the page stays clean"
        assert SENTINEL not in caplog.text


class TestProbeHoles:
    """Where the bounded local probe stops being bounded or local."""

    def test_hole_the_refresh_probes_the_launcher_own_origin_from_the_environment(
        self, tmp_path, sandbox_env, recorded_probes, monkeypatch,
    ):
        """Self poll prevention covers one keyword, not the target.

        `default_status_reader` passes `launcher_url=None`, which stops
        the `/api/state` probe. Nothing stops the server probe from
        resolving to the launcher's own origin: with no host config the
        implicit target is `DEFAULT_SERVER_URL`
        (cymatix_status.py:222), and that is read from
        `CYMATIX_STATUS_URL` at import (:36). A launcher started with
        that variable pointed at itself, or at any other port it also
        serves, probes itself from inside the request it is serving, and
        `_is_loopback_hostname` approves it because it is loopback.
        """

        monkeypatch.setattr(cymatix_status, "DEFAULT_SERVER_URL", "http://127.0.0.1:11438")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        default_status_reader(workspace=workspace, home=tmp_path / "home")()

        assert not [url for url in recorded_probes if ":11438" in url], recorded_probes

    def test_hole_the_refresh_probes_the_launcher_own_origin_from_a_host_config(
        self, tmp_path, sandbox_env, recorded_probes,
    ):
        """The same hole through the other input.

        A native host config that names the launcher's own origin as the
        cymatix-context MCP URL is a configuration mistake, not an
        attack, and it turns every dashboard poll into a request the
        launcher sends to itself while it is answering one.
        """

        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / ".mcp.json").write_text(
            _canonical_config("http://127.0.0.1:11438"), encoding="utf-8",
        )

        default_status_reader(workspace=workspace, home=tmp_path / "home")()

        assert not [url for url in recorded_probes if ":11438" in url], recorded_probes

    def test_hole_a_probe_follows_a_redirect_to_a_non_loopback_host(self, monkeypatch):
        """The loopback rule is enforced once, before the first request.

        `_select_server_target` refuses an implicit non loopback target,
        and then `_probe_json` hands the validated URL to
        `urllib.request.urlopen` with the ordinary opener
        (cymatix_status.py:335 at c05849b), whose handler chain includes
        `HTTPRedirectHandler`. A loopback endpoint that answers 302
        therefore sends the probe anywhere it likes, and the health
        evidence the panel shows is then collected from that other host.

        Only the socket layer is faked here. The opener, the redirect
        handler and the error processor are the real ones `urlopen`
        builds, so the redirect decision under test is production's.
        """

        remote = "http://169.254.169.254/health"

        class _Response(urllib.response.addinfourl):
            def __init__(self, url, code, header_text, body=b"{}"):
                headers = email.message_from_string(
                    header_text, _class=http.client.HTTPMessage,
                )
                super().__init__(io.BytesIO(body), headers, url, code)
                self.msg = "fake"

        class _Transport(urllib.request.HTTPHandler):
            def __init__(self):
                self.requested = []

            def http_open(self, req):
                self.requested.append(req.full_url)
                if len(self.requested) == 1:
                    return _Response(req.full_url, 302, f"Location: {remote}\n")
                return _Response(req.full_url, 200, "Content-Type: application/json\n",
                                 b'{"status": "healthy"}')

        transport = _Transport()
        monkeypatch.setattr(
            urllib.request, "_opener", urllib.request.build_opener(transport),
        )

        result = cymatix_status._probe_json("http://127.0.0.1:11437/health")

        assert transport.requested == ["http://127.0.0.1:11437/health"], transport.requested
        assert result.payload != {"status": "healthy"}, "remote evidence reached the report"

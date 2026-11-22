"""Plugin service supervisor: spawn, capped backoff, stop, hook isolation."""
from __future__ import annotations

import subprocess

from cymatix_context.plugins.api import ServiceSpec
from cymatix_context.plugins.loader import LoadedPlugin
from cymatix_context.plugins.services import PluginServiceSupervisor


class FakeProc:
    _next_pid = 1000

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.returncode = None
        self.events = []
        self.exits_on_terminate = True

    def poll(self):
        return self.returncode

    def die(self, code=1):
        self.returncode = code

    def terminate(self):
        self.events.append("terminate")
        if self.exits_on_terminate:
            self.returncode = 0

    def kill(self):
        self.events.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


class Env:
    def __init__(self, die_immediately=False):
        self.now = 0.0
        self.procs = []
        self.spawn_times = []
        self.die_immediately = die_immediately

    def clock(self):
        return self.now

    def popen(self, argv, **kwargs):
        p = FakeProc(argv, **kwargs)
        if self.die_immediately:
            p.returncode = 1
        self.procs.append(p)
        self.spawn_times.append(self.now)
        return p


class Plugin:
    version = "1"
    requires_core = ">=0"

    def __init__(self, name, specs):
        self.name = name
        self._specs = specs

    def services(self):
        if isinstance(self._specs, Exception):
            raise self._specs
        return self._specs


def lp(name, specs):
    return LoadedPlugin(name, "1", "entry_point", False, "loaded", None, Plugin(name, specs))


def make(env, plugins):
    return PluginServiceSupervisor(
        plugins, popen=env.popen, clock=env.clock, sleep=lambda s: None
    )


def test_start_all_spawns_each_service(monkeypatch):
    monkeypatch.setenv("CX_BASE", "base")
    env = Env()
    sup = make(
        env,
        [
            lp("a", [ServiceSpec("s1", ["x", "1"], {"CX_EXTRA": "e", "CX_BASE": "over"})]),
            lp("b", [ServiceSpec("s2", ["y"])]),
        ],
    )
    sup.start_all()
    assert [p.argv for p in env.procs] == [["x", "1"], ["y"]]
    first = env.procs[0].kwargs
    assert first["env"]["CX_EXTRA"] == "e"
    assert first["env"]["CX_BASE"] == "over"  # spec wins
    assert first.get("shell", False) is False
    assert first["stdout"] == subprocess.DEVNULL
    assert first["stderr"] == subprocess.DEVNULL
    assert first["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    snap = sup.snapshot()
    assert {(r["plugin"], r["service"]) for r in snap} == {("a", "s1"), ("b", "s2")}
    for r in snap:
        assert r["running"] is True and r["restarts"] == 0
        assert r["next_restart_in_s"] is None and isinstance(r["pid"], int)


def test_crash_loop_backs_off():
    env = Env(die_immediately=True)
    sup = make(env, [lp("a", [ServiceSpec("s", ["x"])])])
    sup.start_all()
    while env.now < 400:
        sup.tick()
        env.now += 0.5
    assert env.spawn_times[:9] == [0, 1, 3, 7, 15, 31, 63, 123, 183]
    row = sup.snapshot()[0]
    assert row["restarts"] == len(env.spawn_times) - 1
    assert row["running"] is False and row["pid"] is None
    assert row["next_restart_in_s"] is not None


def test_backoff_resets_after_stable_minute():
    env = Env()
    sup = make(env, [lp("a", [ServiceSpec("s", ["x"])])])
    sup.start_all()
    # crash twice quickly to advance backoff
    env.procs[-1].die()
    sup.tick()
    env.now = 1
    sup.tick()  # respawn #1
    env.procs[-1].die()
    sup.tick()  # schedules +2
    env.now = 3
    sup.tick()  # respawn #2
    assert len(env.procs) == 3
    # stays up for 61 s, then crashes: next delay is back to 1 s
    env.now = 64
    sup.tick()
    env.procs[-1].die()
    sup.tick()
    assert sup.snapshot()[0]["next_restart_in_s"] == 1
    env.now = 65
    sup.tick()
    assert len(env.procs) == 4


def test_stop_all_terminates_then_kills():
    env = Env()
    sup = make(
        env,
        [lp("a", [ServiceSpec("polite", ["x"]), ServiceSpec("stubborn", ["y"])])],
    )
    sup.start_all()
    polite, stubborn = env.procs
    stubborn.exits_on_terminate = False
    sup.stop_all(timeout=3.0)
    assert polite.events[0] == "terminate" and "kill" not in polite.events
    assert stubborn.events[0] == "terminate" and "kill" in stubborn.events
    n = len(env.procs)
    sup.stop_all()  # idempotent
    env.now = 1000
    sup.tick()  # nothing restarts after stop
    assert len(env.procs) == n
    assert all(r["running"] is False for r in sup.snapshot())


def test_services_hook_failure_isolated():
    env = Env()
    bad = lp("bad", RuntimeError("boom"))
    good = lp("good", [ServiceSpec("s", ["x"])])
    sup = make(env, [bad, good])
    sup.start_all()
    assert bad.status == "disabled"
    assert [p.argv for p in env.procs] == [["x"]]
    assert [r["plugin"] for r in sup.snapshot()] == ["good"]


def test_invalid_specs_disable_plugin_and_start_none():
    env = Env()
    cases = [
        "not-a-list",
        [("tuple", ["x"])],
        [ServiceSpec("", ["x"])],
        [ServiceSpec("s", [])],
        [ServiceSpec("s", "x y")],
        [ServiceSpec("s", ["x", 3])],
        [ServiceSpec("s", ["x"], {"A": 1})],
        [ServiceSpec("ok", ["x"]), ServiceSpec("s", [])],
    ]
    plugins = [lp(f"p{i}", c) for i, c in enumerate(cases)]
    sup = make(env, plugins)
    sup.start_all()
    assert env.procs == []
    assert all(p.status == "disabled" for p in plugins)
    assert sup.snapshot() == []


def test_popen_failure_does_not_break_others():
    env = Env()

    def popen(argv, **kw):
        if argv == ["bad"]:
            raise OSError("no such file")
        return env.popen(argv, **kw)

    sup = PluginServiceSupervisor(
        [lp("a", [ServiceSpec("s1", ["bad"]), ServiceSpec("s2", ["good"])])],
        popen=popen,
        clock=env.clock,
        sleep=lambda s: None,
    )
    sup.start_all()
    rows = {r["service"]: r for r in sup.snapshot()}
    assert rows["s1"]["running"] is False and rows["s1"]["next_restart_in_s"] is not None
    assert rows["s2"]["running"] is True


def test_run_in_thread_is_daemon_and_stops():
    env = Env()
    sup = make(env, [lp("a", [ServiceSpec("s", ["x"])])])
    sup.start_all()
    t = sup.run_in_thread()
    assert t.daemon and t.is_alive()
    sup.stop_all()
    t.join(timeout=5)
    assert not t.is_alive()

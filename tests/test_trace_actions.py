"""Phase 3 core of issue #493 -- ``action`` trace events.

Adapters POST raw tool-call args to the local server; the server hashes
them with the deployment key. Metadata level never stores raw args.
"""

from __future__ import annotations

import json
import time

import pytest

from cymatix_context.config import ServerConfig, TraceConfig
from cymatix_context.telemetry import trace as tr

from tests.conftest import make_client, make_cymatix_config

SECRET = "hunter2-very-secret-arg-value"
ARGS = {"path": SECRET, "mode": "r"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("CYMATIX_TRACE_ENABLED", "CYMATIX_TRACE_LEVEL", "CYMATIX_TRACE_PATH"):
        monkeypatch.delenv(k, raising=False)


def _tracer(tmp_path, **kw):
    s = dict(tr._TRACE_DEFAULTS)
    s.update(enabled=True, path=str(tmp_path / "traces"), **kw)
    return tr.PacketTracer(s)


def _lines(tmp_path, n, timeout=5.0):
    f = tmp_path / "traces" / "packets.jsonl"
    end = time.time() + timeout
    while time.time() < end:
        if f.exists():
            ls = [x for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
            if len(ls) >= n:
                return ls
        time.sleep(0.02)
    return []


def _packet(t, pid, session):
    t.writer.submit({"schema_version": "0", "event": "packet", "packet_id": pid,
                     "session_id": session, "ts": time.time()})
    t.note_packet(session, pid)


def test_action_schema_and_join_key(tmp_path):
    t = _tracer(tmp_path)
    _packet(t, "P1", "s1")
    rec = t.emit_action("s1", "Read", ARGS)
    assert rec["event"] == "action" and rec["schema_version"] == "0"
    assert rec["packet_id"] == "P1" and rec["orphan"] is False
    assert rec["tool_name"] == "Read" and rec["session_id"] == "s1"
    assert rec["args_keys"] == ["mode", "path"]
    assert len(rec["args_hash"]) == 64 and isinstance(rec["ts"], float)
    t.close()


def test_explicit_packet_id_wins(tmp_path):
    t = _tracer(tmp_path)
    _packet(t, "P1", "s1")
    assert t.emit_action("s1", "Read", {}, packet_id="PX")["packet_id"] == "PX"
    t.close()


def test_orphan_action_is_not_an_error(tmp_path):
    t = _tracer(tmp_path)
    rec = t.emit_action("nobody", "Bash", {"cmd": "ls"})
    assert rec["packet_id"] is None and rec["orphan"] is True
    t.close()


def test_latest_packet_id_isolated_per_session_and_bounded(tmp_path):
    t = _tracer(tmp_path)
    t.note_packet("a", "PA1")
    t.note_packet("b", "PB1")
    t.note_packet("a", "PA2")
    assert t.latest_packet_id("a") == "PA2"
    assert t.latest_packet_id("b") == "PB1"
    assert t.latest_packet_id("zzz") is None
    t._latest_max = 3
    for i in range(10):
        t.note_packet(f"s{i}", f"P{i}")
    assert len(t._latest) <= 3
    t.close()


def test_emit_packet_records_latest(tmp_path):
    class G:  # minimal genome stub for replay_info
        pass
    t = _tracer(tmp_path)
    t.emit(genome=G(), config=None, packet_id="PE", pipeline_request_id=None,
           session_id="sE", query="q", chunks=[], verdict={"kind": None},
           timing_ms={}, traceparent=None)
    assert t.latest_packet_id("sE") == "PE"
    t.close()


def test_metadata_hashes_args_and_never_writes_raw(tmp_path):
    t = _tracer(tmp_path)
    t.emit_action("s1", "Read", ARGS)
    lines = _lines(tmp_path, 1)
    t.close()
    assert lines
    assert SECRET not in "".join(lines)
    d = json.loads(lines[0])
    assert "args" not in d and d["args_hash"]


def test_args_hash_is_canonical_and_keyed(tmp_path):
    t = _tracer(tmp_path)
    a = t.emit_action("s", "T", {"a": 1, "b": 2})
    b = t.emit_action("s", "T", {"b": 2, "a": 1})
    c = t.emit_action("s", "T", {"a": 1, "b": 3})
    assert a["args_hash"] == b["args_hash"] != c["args_hash"]
    t.close()


def test_full_level_keeps_args(tmp_path):
    t = _tracer(tmp_path, level="full")
    t.emit_action("s1", "Read", ARGS)
    lines = _lines(tmp_path, 1)
    t.close()
    assert json.loads(lines[0])["args"] == ARGS


def test_disabled_tracer_is_noop(tmp_path):
    s = dict(tr._TRACE_DEFAULTS)
    s["enabled"] = False  # default is ON since 2026-10-18
    s["path"] = str(tmp_path / "traces")
    t = tr.PacketTracer(s)
    assert t.emit_action("s", "Read", ARGS) is None
    assert not (tmp_path / "traces").exists()


def _client(tmp_path, token="", **kw):
    cfg = make_cymatix_config(
        trace=TraceConfig(path=str(tmp_path / "traces"), **kw),
        server=ServerConfig(admin_token=token),
    )
    return make_client(cfg)


def test_endpoint_roundtrip_and_event_filter(tmp_path):
    c = _client(tmp_path, enabled=True)
    tracer = c.app.state.packet_tracer
    _packet(tracer, "PK1", "sess")
    r = c.post("/trace/action", json={"session_id": "sess", "tool_name": "Read", "args": ARGS})
    assert r.status_code == 200
    assert r.json() == {"packet_id": "PK1", "orphan": False}
    o = c.post("/trace/action", json={"session_id": "none", "tool_name": "Bash", "args": {}})
    assert o.status_code == 200 and o.json() == {"packet_id": None, "orphan": True}
    _lines(tmp_path, 3)
    acts = c.get("/trace/recent?event=action").json()
    assert len(acts) == 2 and all(x["event"] == "action" for x in acts)
    pk = c.get("/trace/recent?event=packet").json()
    assert len(pk) == 1 and pk[0]["event"] == "packet"
    assert len(c.get("/trace/recent").json()) == 3


def test_endpoint_requires_admin_token_when_set(tmp_path):
    c = _client(tmp_path, token="s3cret", enabled=True)
    body = {"session_id": "s", "tool_name": "Read", "args": {}}
    assert c.post("/trace/action", json=body).status_code == 401
    ok = c.post("/trace/action", json=body, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_endpoint_disabled_tracing(tmp_path):
    c = _client(tmp_path, enabled=False)
    r = c.post("/trace/action", json={"session_id": "s", "tool_name": "Read", "args": {}})
    assert r.status_code == 404
    assert not (tmp_path / "traces").exists()


def test_endpoint_rejects_bad_body(tmp_path):
    c = _client(tmp_path, enabled=True)
    assert c.post("/trace/action", json={"session_id": "s"}).status_code == 422

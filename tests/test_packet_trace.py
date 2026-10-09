"""Phase 1 of issue #493 -- opt-in packet trace.

Covers: ULID ids, [trace] config + env precedence, keyed hashing, the
metadata/full redaction contract, the JSONL writer (hash chain, rotation,
failure isolation), traceparent handling, packet_id on /context, and the
GET /trace read endpoints. No external services.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import pytest

from cymatix_context.config import ServerConfig, TraceConfig, load_config
from cymatix_context.telemetry import trace as tr

from tests.conftest import make_client, make_cymatix_config

QUERY = "zebracrossing authentication flamingo"
DOC_TEXT = "Zebracrossing authentication flamingo tokens are rotated nightly."

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


# -- ULID ---------------------------------------------------------------


def test_ulid_format_and_uniqueness():
    ids = [tr.new_packet_id() for _ in range(500)]
    assert all(ULID_RE.match(i) for i in ids)
    assert len(set(ids)) == 500


def test_ulid_timestamp_prefix_is_monotonic_ish():
    a = tr.new_packet_id()
    time.sleep(0.01)
    b = tr.new_packet_id()
    assert a[:10] <= b[:10]
    assert a[:10] != b[:10]


# -- config -------------------------------------------------------------


def test_trace_config_defaults():
    t = TraceConfig()
    assert t.enabled is False
    assert t.level == "metadata"
    assert t.hash_chain is False
    assert t.sampler_ratio == 1.0
    assert t.rotate_bytes == 64 * 1024 * 1024


def test_load_config_reads_trace_section(tmp_path):
    p = tmp_path / "cymatix.toml"
    p.write_text('[trace]\nenabled = true\nlevel = "full"\nhash_chain = true\n', encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.trace.enabled is True
    assert cfg.trace.level == "full"
    assert cfg.trace.hash_chain is True


def test_env_beats_toml_both_directions(monkeypatch, tmp_path):
    monkeypatch.delenv("CYMATIX_TRACE_ENABLED", raising=False)
    monkeypatch.delenv("CYMATIX_TRACE_LEVEL", raising=False)
    monkeypatch.delenv("CYMATIX_TRACE_PATH", raising=False)
    cfg = TraceConfig(enabled=False, level="metadata")
    assert tr.resolve_trace_settings(cfg)["enabled"] is False
    monkeypatch.setenv("CYMATIX_TRACE_ENABLED", "1")
    monkeypatch.setenv("CYMATIX_TRACE_LEVEL", "full")
    monkeypatch.setenv("CYMATIX_TRACE_PATH", str(tmp_path / "x"))
    s = tr.resolve_trace_settings(cfg)
    assert s["enabled"] is True and s["level"] == "full"
    assert s["path"] == str(tmp_path / "x")
    # env 0 silences toml enabled=true
    monkeypatch.setenv("CYMATIX_TRACE_ENABLED", "0")
    assert tr.resolve_trace_settings(TraceConfig(enabled=True))["enabled"] is False


def test_invalid_level_falls_back_to_metadata(monkeypatch):
    monkeypatch.delenv("CYMATIX_TRACE_ENABLED", raising=False)
    monkeypatch.setenv("CYMATIX_TRACE_LEVEL", "bogus")
    assert tr.resolve_trace_settings(TraceConfig())["level"] == "metadata"


# -- traceparent ---------------------------------------------------------


def test_parse_traceparent_valid():
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert tr.parse_traceparent(tp) == {
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "parent_span_id": "00f067aa0ba902b7",
        "trace_flags": "01",
    }


@pytest.mark.parametrize("bad", [
    "", None, "garbage",
    "00-00000000000000000000000000000000-00f067aa0ba902b7-01",   # zero trace id
    "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",   # zero span id
    "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",   # version ff
    "00-4bf92f3577b34da6a3ce929d0e0e473-00f067aa0ba902b7-01",    # short trace id
])
def test_parse_traceparent_invalid(bad):
    assert tr.parse_traceparent(bad) is None


# -- hashing & event builder --------------------------------------------


def test_keyed_hash_uses_secret_file_and_is_not_plain_sha(tmp_path):
    k = tr.KeyedHasher(tmp_path)
    h = k.hash("hello")
    assert h != hashlib.sha256(b"hello").hexdigest()
    assert h == tr.KeyedHasher(tmp_path).hash("hello")      # secret persisted
    other = tr.KeyedHasher(tmp_path / "other")
    assert other.hash("hello") != h                          # per-deployment


def _chunks():
    return [
        {"gene_id": "g1", "text": "SECRET-CHUNK-TEXT-ONE", "rank": 1, "score": 3.2,
         "source_kind": "code", "lane_contribs": {"fts5": 1.0}},
        {"gene_id": "g2", "text": "SECRET-CHUNK-TEXT-TWO", "rank": 2, "score": 1.1,
         "source_kind": None},
    ]


def _event(level, hasher, **kw):
    return tr.build_packet_event(
        packet_id=tr.new_packet_id(), pipeline_request_id="abc123", session_id="s1",
        query="my private query text", chunks=_chunks(), verdict={"kind": "know", "confidence": 0.8},
        timing_ms={"total": 12.5}, replay={"index_fingerprint": "f", "config_hash": "c",
                                           "cymatix_version": "0", "git_sha": None},
        level=level, hasher=hasher, **kw,
    )


def test_metadata_event_has_no_raw_content(tmp_path):
    ev = _event("metadata", tr.KeyedHasher(tmp_path))
    blob = json.dumps(ev)
    assert ev["schema_version"] == "0"
    assert "my private query" not in blob
    assert "SECRET-CHUNK-TEXT" not in blob
    assert "query" not in ev
    assert ev["query_len_terms"] == 4
    assert ev["chunks"][0]["content_hash"] and ev["chunks"][0]["chunk_id"] == "g1"
    assert "source_kind" not in ev["chunks"][1]            # unavailable -> omitted
    assert ev["verdict"]["kind"] == "know"


def test_full_event_includes_raw(tmp_path):
    ev = _event("full", tr.KeyedHasher(tmp_path))
    assert ev["query"] == "my private query text"
    assert ev["chunks"][0]["text"] == "SECRET-CHUNK-TEXT-ONE"


def test_traceparent_recorded_in_event(tmp_path):
    ev = _event("metadata", tr.KeyedHasher(tmp_path),
                traceparent={"trace_id": "a" * 32, "parent_span_id": "b" * 16, "trace_flags": "01"})
    assert ev["trace"]["trace_id"] == "a" * 32


# -- writer ---------------------------------------------------------------


def _wait_lines(path: Path, n: int, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists():
            lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l]
            if len(lines) >= n:
                return lines
        time.sleep(0.02)
    return []


def test_writer_hash_chain_links(tmp_path):
    w = tr.TraceWriter(tmp_path, hash_chain=True, rotate_bytes=10_000_000)
    for i in range(5):
        w.submit({"schema_version": "0", "event": "packet", "packet_id": f"p{i}"})
    lines = _wait_lines(tmp_path / "packets.jsonl", 5)
    w.close()
    assert len(lines) == 5
    prev = None
    for line in lines:
        rec = json.loads(line)
        assert rec["prev_hash"] == prev
        prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
    assert tr.verify_chain(tmp_path / "packets.jsonl") is True


def test_verify_chain_detects_tamper(tmp_path):
    w = tr.TraceWriter(tmp_path, hash_chain=True, rotate_bytes=10_000_000)
    for i in range(3):
        w.submit({"packet_id": f"p{i}"})
    lines = _wait_lines(tmp_path / "packets.jsonl", 3)
    w.close()
    p = tmp_path / "packets.jsonl"
    p.write_text("\n".join([lines[0], lines[1].replace("p1", "pX"), lines[2]]) + "\n", encoding="utf-8")
    assert tr.verify_chain(p) is False


def test_writer_rotates_by_size(tmp_path):
    w = tr.TraceWriter(tmp_path, hash_chain=False, rotate_bytes=300)
    for i in range(20):
        w.submit({"packet_id": f"p{i}", "pad": "x" * 100})
    time.sleep(0.5)
    w.close()
    assert len(list(tmp_path.glob("packets*.jsonl"))) >= 2


def test_writer_failure_never_raises(tmp_path, caplog):
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    w = tr.TraceWriter(blocker / "sub", hash_chain=False, rotate_bytes=1000)  # un-creatable dir
    w.submit({"packet_id": "p"})        # must not raise
    time.sleep(0.2)
    w.close()


def test_reader_get_and_recent(tmp_path):
    tracer = tr.PacketTracer({"enabled": True, "level": "metadata", "path": str(tmp_path),
                              "hash_chain": False, "sampler_ratio": 1.0,
                              "rotate_bytes": 10_000_000})
    for i in range(4):
        tracer.writer.submit({"packet_id": f"p{i}", "session_id": "a" if i % 2 else "b"})
    _wait_lines(tmp_path / "packets.jsonl", 4)
    assert tracer.get("p2")["packet_id"] == "p2"
    assert tracer.get("nope") is None
    assert [r["packet_id"] for r in tracer.recent(limit=2)] == ["p3", "p2"]
    assert {r["session_id"] for r in tracer.recent(session_id="a")} == {"a"}
    tracer.close()


# -- HTTP integration ---------------------------------------------------


def _client(tmp_path, **trace_kw):
    cfg = make_cymatix_config(trace=TraceConfig(path=str(tmp_path / "traces"), **trace_kw))
    c = make_client(cfg)
    c.post("/ingest", json={"content": DOC_TEXT, "content_type": "text"})
    return c


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("CYMATIX_TRACE_ENABLED", "CYMATIX_TRACE_LEVEL", "CYMATIX_TRACE_PATH"):
        monkeypatch.delenv(k, raising=False)


def test_trace_read_endpoints_require_admin_token_when_set(tmp_path):
    # full-level records carry raw query + chunk text, so the readers sit
    # behind the same [server] admin_token guard as /admin/* and /ingest.
    cfg = make_cymatix_config(
        trace=TraceConfig(path=str(tmp_path / "traces"), enabled=True),
        server=ServerConfig(admin_token="s3cret"),
    )
    c = make_client(cfg)
    assert c.get("/trace/recent").status_code == 401
    assert c.get("/trace/01ARZ3NDEKTSV4RRFFQ69G5FAV").status_code == 401
    ok = {"Authorization": "Bearer s3cret"}
    assert c.get("/trace/recent", headers=ok).status_code == 200
    assert c.get("/trace/01ARZ3NDEKTSV4RRFFQ69G5FAV", headers=ok).status_code == 404


def test_trace_off_writes_nothing_but_packet_id_present(tmp_path):
    c = _client(tmp_path)           # enabled defaults False
    r = c.post("/context", json={"query": QUERY})
    assert r.status_code == 200
    pid = r.headers["X-Cymatix-Packet-Id"]
    assert ULID_RE.match(pid)
    assert r.json()[0]["packet_id"] == pid
    assert not (tmp_path / "traces").exists() or not list((tmp_path / "traces").glob("*.jsonl"))
    assert c.get(f"/trace/{pid}").status_code == 404
    assert c.get("/trace/recent").json() == []


def test_packet_endpoint_carries_packet_id(tmp_path):
    c = _client(tmp_path)
    r = c.post("/context/packet", json={"query": QUERY})
    assert r.status_code == 200
    assert r.json()["packet_id"] == r.headers["X-Cymatix-Packet-Id"]


def test_metadata_level_records_without_raw_content(tmp_path):
    c = _client(tmp_path, enabled=True, level="metadata")
    r = c.post("/context", json={"query": QUERY, "session_id": "sess-1"})
    pid = r.headers["X-Cymatix-Packet-Id"]
    f = tmp_path / "traces" / "packets.jsonl"
    lines = _wait_lines(f, 1)
    assert lines
    raw = f.read_bytes()
    assert b"zebracrossing" not in raw.lower()
    assert b"flamingo" not in raw.lower()
    rec = json.loads(lines[0])
    assert rec["packet_id"] == pid and rec["session_id"] == "sess-1"
    assert rec["replay"]["config_hash"] and rec["replay"]["cymatix_version"]
    got = c.get(f"/trace/{pid}")
    assert got.status_code == 200 and got.json()["packet_id"] == pid
    rec_list = c.get("/trace/recent", params={"session_id": "sess-1", "limit": 5}).json()
    assert [x["packet_id"] for x in rec_list] == [pid]
    assert c.get("/trace/recent", params={"session_id": "other"}).json() == []


def _force_delivery(c, level_dir):
    """Make the pipeline 'deliver' the ingested doc so chunk rows exist
    regardless of the tiny test corpus tripping the abstain gate."""
    ing = c.post("/ingest", json={"content": DOC_TEXT + " second copy variant.",
                                  "content_type": "text"}).json()
    gid = (ing.get("gene_ids") or [None])[0]
    assert gid
    mgr = c.app.state.cymatix
    orig = mgr.build_context_async

    async def patched(*a, **k):
        w = await orig(*a, **k)
        w.expressed_gene_ids = [gid]
        w.retrieval_scores = {gid: 2.5}
        return w

    mgr.build_context_async = patched
    return gid


def test_chunks_recorded_hashed_not_raw_at_metadata(tmp_path):
    c = _client(tmp_path, enabled=True, level="metadata")
    gid = _force_delivery(c, tmp_path)
    c.post("/context", json={"query": QUERY})
    f = tmp_path / "traces" / "packets.jsonl"
    rec = json.loads(_wait_lines(f, 1)[0])
    ch = rec["chunks"][0]
    assert ch["chunk_id"] == gid and ch["rank"] == 1 and ch["score"] == 2.5
    assert len(ch["content_hash"]) == 64 and "text" not in ch
    assert b"flamingo" not in f.read_bytes().lower()


def test_chunks_carry_text_at_full(tmp_path):
    c = _client(tmp_path, enabled=True, level="full")
    _force_delivery(c, tmp_path)
    c.post("/context", json={"query": QUERY})
    rec = json.loads(_wait_lines(tmp_path / "traces" / "packets.jsonl", 1)[0])
    assert "flamingo" in rec["chunks"][0]["text"].lower()


def test_full_level_includes_query(tmp_path):
    c = _client(tmp_path, enabled=True, level="full")
    c.post("/context", json={"query": QUERY})
    f = tmp_path / "traces" / "packets.jsonl"
    assert _wait_lines(f, 1)
    assert QUERY.encode() in f.read_bytes()


def test_traceparent_stored_and_invalid_ignored(tmp_path):
    c = _client(tmp_path, enabled=True)
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    p1 = c.post("/context", json={"query": QUERY}, headers={"traceparent": tp}).headers["X-Cymatix-Packet-Id"]
    p2 = c.post("/context", json={"query": QUERY}, headers={"traceparent": "nonsense"}).headers["X-Cymatix-Packet-Id"]
    _wait_lines(tmp_path / "traces" / "packets.jsonl", 2)
    assert c.get(f"/trace/{p1}").json()["trace"]["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert "trace" not in c.get(f"/trace/{p2}").json()


def test_writer_exception_does_not_break_context(tmp_path, monkeypatch):
    c = _client(tmp_path, enabled=True)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(tr.TraceWriter, "submit", boom)
    r = c.post("/context", json={"query": QUERY})
    assert r.status_code == 200
    assert ULID_RE.match(r.headers["X-Cymatix-Packet-Id"])


def test_hash_chain_over_http(tmp_path):
    c = _client(tmp_path, enabled=True, hash_chain=True)
    for _ in range(3):
        c.post("/context", json={"query": QUERY})
    f = tmp_path / "traces" / "packets.jsonl"
    assert len(_wait_lines(f, 3)) == 3
    assert tr.verify_chain(f) is True

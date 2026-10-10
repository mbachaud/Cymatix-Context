"""Phase 5 of issue #493 -- shareable trace export (no replay)."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from cymatix_context.cli import dispatcher
from cymatix_context.telemetry import trace as tr
from cymatix_context.telemetry import trace_export as te

SCHEMA = Path(__file__).resolve().parent.parent / "docs/specs/packet-trace-v0.schema.json"
REPLAY = {"index_fingerprint": "a" * 32, "doc_count": 3, "config_hash": "b" * 64,
          "cymatix_version": "0.11.2", "git_sha": None}


def _event(hasher, query, level="metadata", pid="01M4ZZZZZZZZZZZZZZZZZZZZZZ",
           reason=None):
    verdict = {"kind": "miss", "reason": reason} if reason else {"kind": "know", "confidence": 0.8}
    return tr.build_packet_event(
        packet_id=pid, pipeline_request_id="ab12cd34ef56", session_id="sess-1",
        query=query,
        chunks=[{"gene_id": "g1", "text": "alpha body", "rank": 1, "score": 2.5,
                 "source_kind": "code", "lane_contribs": {"fts5": 1.0}},
                {"gene_id": "g2", "text": "beta body", "rank": 2, "score": 1.0}],
        verdict=verdict, timing_ms={"total": 1.5}, replay=REPLAY, level=level,
        hasher=hasher, traceparent=None, ts=1791582325.0)


@pytest.fixture
def src(tmp_path):
    d = tmp_path / "traces"
    d.mkdir()
    return d


@pytest.fixture
def hasher(src):
    return tr.KeyedHasher(src)


def _write(src, records, name="packets.jsonl", chain=False):
    lines = []
    prev = None
    for r in records:
        if chain:
            r = {**r, "prev_hash": prev}
        line = json.dumps(r, separators=(",", ":"))
        prev = hashlib.sha256(line.encode()).hexdigest()
        lines.append(line)
    (src / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read(out):
    return [json.loads(l) for l in (out / "packets.jsonl").read_text("utf-8").splitlines()]


def test_refuses_full_level(src, hasher, tmp_path):
    _write(src, [_event(hasher, "q one", level="full")])
    with pytest.raises(te.ExportRefused, match="full"):
        te.export_traces(src, tmp_path / "out")
    assert not (tmp_path / "out" / "packets.jsonl").exists()


def test_joins_preserved_within_export(src, hasher, tmp_path):
    _write(src, [_event(hasher, "same query", pid="01M4AAAAAAAAAAAAAAAAAAAAAA"),
                 _event(hasher, "same query", pid="01M4BBBBBBBBBBBBBBBBBBBBBB"),
                 _event(hasher, "other query", pid="01M4CCCCCCCCCCCCCCCCCCCCCC")])
    te.export_traces(src, tmp_path / "out")
    r = _read(tmp_path / "out")
    assert r[0]["query_hash"] == r[1]["query_hash"] != r[2]["query_hash"]
    assert r[0]["chunks"][0]["content_hash"] == r[1]["chunks"][0]["content_hash"]
    assert r[0]["chunks"][0]["content_hash"] != r[0]["chunks"][1]["content_hash"]


def test_two_exports_uncorrelatable(src, hasher, tmp_path):
    _write(src, [_event(hasher, "same query")])
    te.export_traces(src, tmp_path / "o1")
    te.export_traces(src, tmp_path / "o2")
    a, b = _read(tmp_path / "o1")[0], _read(tmp_path / "o2")[0]
    assert a["query_hash"] != b["query_hash"]
    assert a["chunks"][0]["content_hash"] != b["chunks"][0]["content_hash"]


def test_no_hash_equals_or_verifies_under_deployment_key(src, hasher, tmp_path):
    ev = _event(hasher, "same query")
    _write(src, [ev])
    te.export_traces(src, tmp_path / "out")
    rec = _read(tmp_path / "out")[0]
    key = (src / ".trace_key").read_bytes()
    assert rec["query_hash"] != ev["query_hash"]
    assert rec["query_hash"] != hmac.new(key, b"same query", hashlib.sha256).hexdigest()
    assert rec["chunks"][0]["content_hash"] != ev["chunks"][0]["content_hash"]
    assert rec["chunks"][0]["content_hash"] != hmac.new(
        key, b"alpha body", hashlib.sha256).hexdigest()
    assert not (tmp_path / "out" / ".trace_key").exists()


def test_chain_dropped_and_recorded(src, hasher, tmp_path):
    _write(src, [_event(hasher, "a"), _event(hasher, "b")], chain=True)
    m = te.export_traces(src, tmp_path / "out")
    assert all("prev_hash" not in r for r in _read(tmp_path / "out"))
    assert "prev_hash" in m["dropped_fields"]


def test_raw_text_stripped(src, hasher, tmp_path):
    ev = _event(hasher, "secretive question")
    ev["chunks"][0]["text"] = "leaky body"  # stray text despite metadata level
    ev["query"] = "secretive question"
    _write(src, [ev])
    te.export_traces(src, tmp_path / "out")
    blob = (tmp_path / "out" / "packets.jsonl").read_text("utf-8")
    assert "leaky body" not in blob and "secretive question" not in blob
    rec = _read(tmp_path / "out")[0]
    assert "query" not in rec and "text" not in rec["chunks"][0]


def test_canary_fails_export(src, hasher, tmp_path):
    _write(src, [_event(hasher, "q", reason="key AKIAIOSFODNN7EXAMPLE leaked")])
    with pytest.raises(te.SecretFound) as ei:
        te.export_traces(src, tmp_path / "out")
    assert ei.value.findings
    assert not (tmp_path / "out" / "packets.jsonl").exists()


@pytest.mark.parametrize("canary", [
    "ghp_" + "a" * 36, "api_key=abcd1234efgh5678", "-----BEGIN RSA PRIVATE KEY-----",
    "bob@example.com", "10.1.2.3", "http://internal.corp/x", "db01.internal.example.org",
])
def test_secret_scan_patterns(tmp_path, canary):
    p = tmp_path / "f.jsonl"
    p.write_text(json.dumps({"x": canary}) + "\n", encoding="utf-8")
    assert te.secret_scan(p)


def test_secret_scan_clean_on_real_record(src, hasher, tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text(json.dumps(_event(hasher, "q")) + "\n", encoding="utf-8")
    assert te.secret_scan(p) == []


def test_manifest(src, hasher, tmp_path):
    _write(src, [_event(hasher, "a"), _event(hasher, "b")])
    m = te.export_traces(src, tmp_path / "out")
    disk = json.loads((tmp_path / "out" / "manifest.json").read_text("utf-8"))
    assert disk == m
    assert m["consent"] == "not_shareable"
    assert m["record_count"] == 2
    assert m["schema_version"] == "0"
    assert {"query_hash", "chunks[].content_hash"} <= set(m["rehashed_fields"])
    assert {"query", "chunks[].text"} <= set(m["stripped_fields"])
    assert m["source_files"] == ["packets.jsonl"]
    assert "export_key" not in json.dumps(m)


def test_schema_validation(src, hasher, tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text("utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    v = jsonschema.Draft202012Validator(schema)
    full = _event(hasher, "q", level="full")
    v.validate(full)
    meta = _event(hasher, "q")
    v.validate(meta)
    bad = {**meta, "query": "leak"}
    with pytest.raises(jsonschema.ValidationError):
        v.validate(bad)
    _write(src, [meta])
    te.export_traces(src, tmp_path / "out")
    for r in _read(tmp_path / "out"):
        v.validate(r)


def test_content_and_path_derived_ids_not_in_output(src, hasher, tmp_path):
    text = "planted document text for gene id"
    gid = hashlib.sha256(text.encode()).hexdigest()[:16]
    pid = hashlib.sha256(b"/repo/src/a.py::parent").hexdigest()[:16]
    ev = tr.build_packet_event(
        packet_id="01M4ZZZZZZZZZZZZZZZZZZZZZZ", pipeline_request_id="ab12cd34ef56",
        session_id="s", query="q",
        chunks=[{"gene_id": gid, "text": text, "rank": 1, "score": 1.0},
                {"gene_id": gid, "rank": 2, "score": 0.5}],
        verdict={"kind": "know", "confidence": 0.5}, timing_ms={"total": 1.0},
        replay=REPLAY, level="metadata", hasher=hasher)
    ev["chunks"][0]["parent_id"] = pid
    ev["chunks"][1]["gene_id"] = pid
    _write(src, [ev])
    te.export_traces(src, tmp_path / "out")
    blob = (tmp_path / "out" / "packets.jsonl").read_text("utf-8")
    assert gid not in blob and pid not in blob
    c = _read(tmp_path / "out")[0]["chunks"]
    assert c[0]["chunk_id"] == c[0]["doc_id"] == c[1]["chunk_id"]  # joins survive
    assert c[0]["parent_id"] == c[1]["gene_id"]


def test_normal_verdict_reasons_do_not_trip_scan(tmp_path):
    from cymatix_context.schemas import MISS_REASONS
    p = tmp_path / "f.jsonl"
    p.write_text("\n".join(json.dumps({"verdict": {"kind": "miss", "reason": r}})
                           for r in MISS_REASONS) + "\n", encoding="utf-8")
    assert te.secret_scan(p) == []


def test_manifest_field_decisions(src, hasher, tmp_path):
    _write(src, [_event(hasher, "a")])
    m = te.export_traces(src, tmp_path / "out")
    d = m["field_decisions"]
    assert d["chunks[].chunk_id"] == "rekey" and d["chunks[].doc_id"] == "rekey"
    assert d["packet_id"] == "keep" and d["pipeline_request_id"] == "keep"
    assert d["prev_hash"] == "drop"


def test_replay_fingerprints_rekeyed(src, hasher, tmp_path):
    _write(src, [_event(hasher, "a"), _event(hasher, "b")])
    m = te.export_traces(src, tmp_path / "out")
    blob = (tmp_path / "out" / "packets.jsonl").read_text("utf-8")
    assert REPLAY["config_hash"] not in blob and REPLAY["index_fingerprint"] not in blob
    r = _read(tmp_path / "out")
    assert r[0]["replay"]["config_hash"] == r[1]["replay"]["config_hash"]
    assert r[0]["replay"]["index_fingerprint"] == r[1]["replay"]["index_fingerprint"]
    assert r[0]["replay"]["doc_count"] == 3
    assert m["field_decisions"]["replay.config_hash"] == "rekey"
    assert m["field_decisions"]["replay.index_fingerprint"] == "rekey"
    assert "replay.config_hash" in m["rehashed_fields"]


def test_cli_export(src, hasher, tmp_path, capsys):
    _write(src, [_event(hasher, "q")])
    out = tmp_path / "out"
    assert dispatcher.main(["trace", "export", "--in", str(src), "--out", str(out)]) == 0
    assert (out / "packets.jsonl").exists()
    _write(src, [_event(hasher, "q", level="full")])
    assert dispatcher.main(["trace", "export", "--in", str(src),
                            "--out", str(tmp_path / "o2")]) != 0

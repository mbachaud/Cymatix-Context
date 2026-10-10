"""Phase 4 of issue #493 -- taxonomy constants and the ``label`` event.

Labels are recorded only; no detector emits them here. Covers the label
vocabulary, the OWASP/ATLAS mapping table, and ``PacketTracer.emit_label``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cymatix_context.telemetry import taxonomy as tx
from cymatix_context.telemetry import trace as tr


def _settings(path: Path, enabled: bool = True) -> dict:
    return {
        "enabled": enabled,
        "level": "metadata",
        "path": str(path),
        "hash_chain": False,
        "sampler_ratio": 1.0,
        "rotate_bytes": 10_000_000,
    }


def _wait_lines(path: Path, n: int, timeout: float = 5.0) -> list:
    end = time.time() + timeout
    while time.time() < end:
        if path.exists():
            lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l]
            if len(lines) >= n:
                return lines
        time.sleep(0.02)
    return []


def test_valid_label_is_written(tmp_path):
    tracer = tr.PacketTracer(_settings(tmp_path))
    tracer.emit_label(
        "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        tx.INDIRECT_PROMPT_INJECTION,
        confidence=0.9,
        evidence={"reason": "instruction-like chunk"},
    )
    lines = _wait_lines(tmp_path / "packets.jsonl", 1)
    tracer.close()
    assert lines, "label event was not written"
    rec = json.loads(lines[0])
    assert rec["event"] == "label"
    assert rec["schema_version"] == "0"
    assert rec["packet_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert rec["labels"] == [tx.INDIRECT_PROMPT_INJECTION]
    assert rec["label_source"] == "trace_check"
    assert rec["confidence"] == 0.9
    assert rec["evidence"] == {"reason": "instruction-like chunk"}
    assert isinstance(rec["ts"], float)


def test_label_source_accepts_human_and_judge(tmp_path):
    tracer = tr.PacketTracer(_settings(tmp_path))
    tracer.emit_label("p1", tx.BENIGN, label_source="human")
    tracer.emit_label("p2", tx.RBAC_LEAK, label_source="judge")
    lines = _wait_lines(tmp_path / "packets.jsonl", 2)
    tracer.close()
    assert [json.loads(l)["label_source"] for l in lines] == ["human", "judge"]


def test_unknown_label_rejected(tmp_path):
    tracer = tr.PacketTracer(_settings(tmp_path))
    with pytest.raises(ValueError):
        tracer.emit_label("p1", "not_a_real_label")
    tracer.close()
    assert not (tmp_path / "packets.jsonl").exists()


@pytest.mark.parametrize("bad_source", ["robot", "", "TRACE_CHECK", None])
def test_bad_label_source_rejected(tmp_path, bad_source):
    tracer = tr.PacketTracer(_settings(tmp_path))
    with pytest.raises(ValueError):
        tracer.emit_label("p1", tx.BENIGN, label_source=bad_source)
    tracer.close()
    assert not (tmp_path / "packets.jsonl").exists()


def test_disabled_tracer_is_noop(tmp_path):
    tracer = tr.PacketTracer(_settings(tmp_path / "traces", enabled=False))
    assert tracer.emit_label("p1", tx.CORPUS_POISONING) is None
    assert not (tmp_path / "traces").exists()


def test_mapping_keys_are_labels_and_benign_unmapped():
    assert tx.LABELS == frozenset({
        tx.INDIRECT_PROMPT_INJECTION,
        tx.CONTROL_TAG_FORGERY,
        tx.RBAC_LEAK,
        tx.CORPUS_POISONING,
        tx.DATA_EXFILTRATION,
        tx.BENIGN,
    })
    assert set(tx.MAPPING) <= tx.LABELS
    assert tx.BENIGN not in tx.MAPPING
    assert set(tx.MAPPING) == tx.LABELS - {tx.BENIGN}
    for entry in tx.MAPPING.values():
        assert entry["verified"] is False

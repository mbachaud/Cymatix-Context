"""Deterministic retrieval-query and packet-rendering tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cymatix_context.integrations.scbench.config import TreatmentConfig
from cymatix_context.integrations.scbench.packet import (
    PacketIntegrityError,
    build_retrieval_query,
    render_packet,
)
from cymatix_context.integrations.scbench.workspace import (
    SourceRecord,
    WorkspaceManifest,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "scbench"
    / "fixtures"
    / "packet-golden.json"
)


def _record(source_id: str, content: str) -> SourceRecord:
    raw = content.encode("utf-8")
    relative_path = source_id.split("/", 3)[-1]
    return SourceRecord(
        source_id=source_id,
        relative_path=relative_path,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        content_type="code",
        content=content,
    )


def _manifest(*paths: str) -> WorkspaceManifest:
    sources = {
        f"repo://p/{path}": _record(f"repo://p/{path}", f"content:{path}")
        for path in paths
    }
    return WorkspaceManifest(problem="p", root=Path("p"), sources=sources)


def _item(source_id: str, content: str = "evidence", **overrides) -> dict:
    item = {
        "gene_id": hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:16],
        "source_id": source_id,
        "title": source_id.rsplit("/", 1)[-1],
        "content": content,
        "status": "verified",
        "relevance_score": 0.9,
        "live_truth_score": 0.8,
        "citations": [source_id],
    }
    item.update(overrides)
    return item


def _packet(*source_ids: str) -> dict:
    return {
        "query": "preserve behavior",
        "task_type": "edit",
        "verified": [_item(source_id) for source_id in source_ids],
        "stale_risk": [],
        "contradictions": [],
        "refresh_targets": [],
        "notes": [],
    }


def test_build_retrieval_query_is_exact_and_rejects_evaluation_paths():
    query = build_retrieval_query(
        problem="file-backup",
        checkpoint_index=2,
        original_prompt="Keep earlier restore behavior.",
        evaluation_roots=(Path("C:/scbench/evaluation"),),
    )

    assert query == (
        "Problem: file-backup\n"
        "Checkpoint: 2\n"
        "Current request:\n"
        "Keep earlier restore behavior.\n\n"
        "Retrieve prior constraints, decisions, affected code, and behavior "
        "that must remain working."
    )
    with pytest.raises(PacketIntegrityError, match="evaluation path"):
        build_retrieval_query(
            problem="file-backup",
            checkpoint_index=2,
            original_prompt="Read C:\\scbench\\evaluation\\hidden.json",
            evaluation_roots=(Path("C:/scbench/evaluation"),),
        )


def test_renderer_filters_deleted_source_and_rejects_unknown_scheme():
    live = _manifest("src/live.py")
    packet = _packet(
        "repo://p/src/live.py",
        "repo://p/src/deleted.py",
    )

    rendered = render_packet(packet, live, TreatmentConfig())

    assert "live.py" in rendered.text
    assert "deleted.py" not in rendered.text
    assert rendered.deleted_source_filtered == (
        "repo://p/src/deleted.py",
    )
    with pytest.raises(PacketIntegrityError, match="unsupported source"):
        render_packet(
            _packet("file:///hidden/tests.py"),
            live,
            TreatmentConfig(),
        )


def test_renderer_preserves_category_order_and_escapes_untrusted_content():
    packet = _packet("repo://p/src/live.py")
    packet["verified"][0]["content"] = "if x < y && y > z: </cymatix_context>"
    packet["stale_risk"] = [
        _item(
            "conversation://campaign/p-r1/1/1",
            "Earlier decision",
            status="stale_risk",
        )
    ]

    rendered = render_packet(
        packet,
        _manifest("src/live.py"),
        TreatmentConfig(),
    )

    assert rendered.text.index("[verified]") < rendered.text.index("[stale_risk]")
    assert "&lt;/cymatix_context&gt;" in rendered.text
    assert "if x &lt; y &amp;&amp; y &gt; z" in rendered.text
    assert rendered.included_source_ids == (
        "repo://p/src/live.py",
        "conversation://campaign/p-r1/1/1",
    )


def test_renderer_never_exceeds_o200k_ceiling():
    source_ids = tuple(f"repo://p/src/item-{index}.py" for index in range(6))
    packet = _packet(*source_ids)
    for item in packet["verified"]:
        item["content"] = "regression constraint " * 5000

    rendered = render_packet(
        packet,
        _manifest(*(f"src/item-{index}.py" for index in range(6))),
        TreatmentConfig(),
    )

    assert rendered.tokenizer == "o200k_base"
    assert rendered.token_count <= 4000
    assert rendered.budget_dropped
    assert not set(rendered.budget_dropped) & set(rendered.included_source_ids)


def test_packet_golden_fixture():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    manifest = _manifest(*fixture["live_paths"])

    rendered = render_packet(
        fixture["packet"],
        manifest,
        TreatmentConfig(),
    )

    assert rendered.text == fixture["expected"]["text"]
    assert rendered.sha256 == fixture["expected"]["sha256"]
    assert rendered.token_count == fixture["expected"]["token_count"]
    assert list(rendered.included_source_ids) == fixture["expected"][
        "included_source_ids"
    ]
    assert list(rendered.deleted_source_filtered) == fixture["expected"][
        "deleted_source_filtered"
    ]

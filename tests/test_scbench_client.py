"""Verified HTTP boundary tests for the SCBench Cymatix sidecar."""

from __future__ import annotations

import httpx
import pytest

from cymatix_context.integrations.scbench.client import (
    CymatixClient,
    CymatixProtocolError,
)
from cymatix_context.integrations.scbench.config import TreatmentConfig
from cymatix_context.integrations.scbench.workspace import SourceRecord


def _source() -> SourceRecord:
    return SourceRecord(
        source_id="repo://p/app.py",
        relative_path="app.py",
        sha256="a" * 64,
        size_bytes=9,
        content_type="code",
        content="VALUE = 1",
    )


def test_ingest_rejects_success_without_gene_ids():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"gene_ids": [], "count": 0},
        )
    )
    client = CymatixClient("http://x", timeout_s=20, transport=transport)
    try:
        with pytest.raises(CymatixProtocolError, match="no gene ids"):
            client.ingest_source(_source())
    finally:
        client.close()


def test_ingest_source_sends_provenance_and_verifies_count():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ingest"
        data = __import__("json").loads(request.content)
        assert data == {
            "content": "VALUE = 1",
            "content_type": "code",
            "metadata": {
                "path": "app.py",
                "source_id": "repo://p/app.py",
                "problem": "p",
                "content_sha256": "a" * 64,
                "source_kind": "code",
            },
            "local_federation": False,
        }
        return httpx.Response(200, json={"gene_ids": ["gene-1"], "count": 1})

    client = CymatixClient(
        "http://x",
        timeout_s=20,
        transport=httpx.MockTransport(respond),
    )
    try:
        verification = client.ingest_source(_source())
    finally:
        client.close()

    assert verification.gene_ids == ("gene-1",)
    assert verification.count == 1
    assert verification.source_id == "repo://p/app.py"


def test_wait_ready_requires_genome_ready():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "status": "ok",
                "checks": {"genome_ready": False},
                "genes": 0,
            },
        )
    )
    client = CymatixClient("http://x", timeout_s=10, transport=transport)
    try:
        with pytest.raises(CymatixProtocolError, match="genome_ready"):
            client.wait_ready(deadline_s=0.01, poll_interval_s=0.001)
    finally:
        client.close()


def test_context_packet_sends_frozen_treatment_and_validates_shape():
    def respond(request: httpx.Request) -> httpx.Response:
        data = __import__("json").loads(request.content)
        assert request.url.path == "/context/packet"
        assert data == {
            "query": "preserve behavior",
            "task_type": "edit",
            "session_id": "campaign:pair:r1",
            "max_genes": 8,
            "include_raw": True,
            "max_item_chars": 2000,
            "ignore_delivered": True,
            "read_only": False,
        }
        return httpx.Response(
            200,
            json={
                "query": "preserve behavior",
                "task_type": "edit",
                "verified": [],
                "stale_risk": [],
                "refresh_targets": [],
                "notes": [],
            },
        )

    client = CymatixClient(
        "http://x",
        timeout_s=20,
        transport=httpx.MockTransport(respond),
    )
    try:
        packet = client.context_packet(
            "preserve behavior",
            task_type="edit",
            session_id="campaign:pair:r1",
            treatment=TreatmentConfig(),
        )
    finally:
        client.close()

    assert packet.query == "preserve behavior"
    assert packet.payload["verified"] == []


def test_conversation_uses_stable_source_id_and_tombstone_validates_source():
    seen_paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        data = __import__("json").loads(request.content)
        if request.url.path == "/ingest":
            assert data["metadata"]["source_id"] == (
                "conversation://campaign/p-r1/1/2"
            )
            assert data["content_type"] == "conversation"
            return httpx.Response(
                200,
                json={"gene_ids": ["conversation-gene"], "count": 1},
            )
        assert data == {"source_id": "repo://p/deleted.py"}
        return httpx.Response(
            200,
            json={
                "source_id": "repo://p/deleted.py",
                "tombstoned": 1,
                "gene_ids": ["deleted-gene"],
            },
        )

    client = CymatixClient(
        "http://x",
        timeout_s=20,
        transport=httpx.MockTransport(respond),
    )
    try:
        conversation = client.ingest_conversation(
            campaign="campaign",
            pair="p-r1",
            replicate=1,
            checkpoint=2,
            content="User:\nrequest\n\nAssistant:\nresult",
        )
        tombstone = client.tombstone("repo://p/deleted.py")
    finally:
        client.close()

    assert conversation.count == 1
    assert tombstone == ("deleted-gene",)
    assert seen_paths == ["/ingest", "/admin/genes/tombstone"]

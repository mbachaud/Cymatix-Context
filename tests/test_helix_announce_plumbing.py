"""End-to-end plumbing for helix_announce + IDE auto-detect.

Exercises: VSCODE_PID env → adapter detect_ide → POST /sessions/register
→ POST /sessions/{participant_id}/announce → GET /sessions →
collector entry has all the right fields. Catches regressions where
any layer in the chain drops something.
"""
import json

import pytest
from fastapi.testclient import TestClient

from helix_context.config import (
    BudgetConfig,
    GenomeConfig,
    HelixConfig,
    RibosomeConfig,
    ServerConfig,
)
from helix_context.server import create_app


class _MinimalMockBackend:
    """Minimal ribosome mock — only needs to not crash for registry tests."""

    def complete(self, prompt: str, system: str = "", temperature: float = 0.0) -> str:
        if "compression engine" in system:
            return json.dumps({
                "codons": [{"meaning": "test_codon", "weight": 0.8, "is_exon": True}],
                "complement": "Compressed test content.",
                "promoter": {
                    "domains": ["test"],
                    "entities": ["TestEntity"],
                    "intent": "test",
                    "summary": "Test content for plumbing tests",
                },
            })
        return "{}"


@pytest.fixture
def client():
    """TestClient backed by a fresh in-memory genome for full isolation."""
    config = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=4),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        server=ServerConfig(upstream="http://localhost:11434"),
    )
    app = create_app(config)
    app.state.helix.ribosome.backend = _MinimalMockBackend()
    with TestClient(app) as c:
        yield c


def test_register_then_announce_round_trips_through_get_sessions(client):
    """Full plumbing: simulated MCP startup populates ide_detected via the
    body fields the adapter would have sent; agent then announces model_id;
    GET /sessions reflects both."""

    # Stage 1 — adapter posts the detected IDE at register time
    reg_resp = client.post(
        "/sessions/register",
        json={
            "party_id": "swift_wing21",
            "handle": "laude",
            "workspace": "F:\\Projects",
            "ide_detected": "vscode",
            "ide_detection_via": "env:VSCODE_PID",
            "agent_kind": "claude-code",  # backward-compat from PR #26
        },
    )
    assert reg_resp.status_code == 200, reg_resp.text
    pid = reg_resp.json()["participant_id"]

    # Stage 2 — agent announces its model
    ann_resp = client.post(
        f"/sessions/{pid}/announce",
        json={"model_id": "claude-opus-4-7"},
    )
    assert ann_resp.status_code == 200, ann_resp.text

    # Stage 3 — GET projection reflects both
    listing = client.get("/sessions", params={"party_id": "swift_wing21", "status": "all"}).json()
    rows = listing if isinstance(listing, list) else listing.get("participants", [])
    matching = [r for r in rows if r.get("participant_id") == pid]
    assert len(matching) == 1, f"expected one matching row, got: {rows}"
    row = matching[0]
    assert row["ide_detected"] == "vscode"
    assert row["ide_detection_via"] == "env:VSCODE_PID"
    assert row["model_id"] == "claude-opus-4-7"
    assert row["agent_kind"] == "claude-code"


def test_announce_with_ide_override_changes_via_to_agent_override(client):
    """Agent override path: register with one IDE, announce with override,
    confirm ide_detection_via flips to agent_override."""
    reg = client.post(
        "/sessions/register",
        json={
            "party_id": "party_override",
            "handle": "laude",
            "ide_detected": "vscode",
            "ide_detection_via": "env:VSCODE_PID",
        },
    )
    pid = reg.json()["participant_id"]

    client.post(
        f"/sessions/{pid}/announce",
        json={"model_id": "gpt-5", "ide_override": "cursor"},
    )

    listing = client.get("/sessions", params={"party_id": "party_override", "status": "all"}).json()
    rows = listing if isinstance(listing, list) else listing.get("participants", [])
    row = rows[0] if rows else {}
    assert row["ide_detected"] == "cursor"
    assert row["ide_detection_via"] == "agent_override"
    assert row["model_id"] == "gpt-5"

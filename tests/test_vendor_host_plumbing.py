"""End-to-end plumbing for vendor+host badges.

Exercises: POST /sessions/register with agent_kind+mcp_host →
registry write → GET /sessions projection → assert fields intact.

This single test guards against regressions where any layer in the
chain (request parsing, registry write, list_participants projection,
response serialisation) silently drops the new fields.

Runs entirely in-process via TestClient + in-memory SQLite.
No external services required.
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


def test_register_with_vendor_host_fields_survives_to_list(client):
    """Full plumbing: POST /sessions/register with agent_kind+mcp_host, then
    GET /sessions to confirm both fields and the composed host_label survive
    the entire endpoint→registry→projection→serialisation chain.
    """
    # 1. Register with vendor + host fields.
    resp = client.post(
        "/sessions/register",
        json={
            "party_id": "swift_wing21",
            "handle": "laude",
            "workspace": "F:\\Projects",
            "agent_kind": "claude-code",
            "mcp_host": "vscode",
        },
    )
    assert resp.status_code == 200, f"register failed: {resp.text}"
    reg = resp.json()
    assert reg["participant_id"], "registration must return a participant_id"

    # 2. Read back via the list endpoint (status="all" so the brand-new
    #    participant, which is "active", is included regardless of filter).
    resp = client.get("/sessions", params={"party_id": "swift_wing21", "status": "all"})
    assert resp.status_code == 200, f"list failed: {resp.text}"
    data = resp.json()
    assert data["count"] == 1, f"expected 1 participant, got {data['count']}"

    participant = data["participants"][0]

    # 3. Assert the vendor+host fields made it through every layer.
    assert participant["agent_kind"] == "claude-code", (
        f"agent_kind dropped or wrong: {participant.get('agent_kind')!r}"
    )
    assert participant["mcp_host"] == "vscode", (
        f"mcp_host dropped or wrong: {participant.get('mcp_host')!r}"
    )

    # 4. Verify the label composition directly (mirrors what StateCollector does).
    from helix_context.launcher.host_labels import compose_label
    label = compose_label(participant["agent_kind"], participant["mcp_host"])
    assert label == "Claude Code + VS Code", (
        f"compose_label produced unexpected result: {label!r}"
    )

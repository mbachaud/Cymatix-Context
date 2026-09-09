"""Canonical output is a boundary projection, never a storage migration."""
import importlib
import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient


def wire_module():
    return importlib.import_module("cymatix_context.wire")


def test_canonical_fields_preserve_content_and_user_metadata():
    payload = {
        "expressed_gene_ids": ["gene-1"],
        "know": {"gene_id_match": "gene-1"},
        "miss": {"do_not_answer_from_genome": True},
        "documents": [{"gene_id": "gene-1", "promoter": {"domains": ["bio"]},
                       "codons": ["gene"], "epigenetics": {}, "chromatin": 1,
                       "content": "<GENE> gene_id genome",
                       "metadata": {"gene_id": "user-owned"}}],
    }
    wire = wire_module()
    result = wire.to_wire(payload, "canonical")
    assert result["expressed_document_ids"] == ["gene-1"]
    assert result["know"] == {"document_id_match": "gene-1"}
    assert result["miss"] == {"do_not_answer_from_knowledge_store": True}
    doc = result["documents"][0]
    assert doc["document_id"] == "gene-1"
    assert doc["tags"] == {"domains": ["bio"]}
    assert doc["fragments"] == ["gene"]
    assert doc["tier"] == 1 and doc["signals"] == {}
    assert doc["content"] == payload["documents"][0]["content"]
    assert doc["metadata"] == {"gene_id": "user-owned"}
    assert "gene_id" in payload["documents"][0]
    assert wire.to_wire(payload, "legacy") is payload
    assert wire.to_wire(result, "canonical") == result


def test_alias_collision_cannot_silently_discard_data():
    wire = wire_module()
    assert wire.to_wire({"gene_id": "x", "document_id": "x"}, "canonical") == {"document_id": "x"}
    with pytest.raises(ValueError, match="collision"):
        wire.to_wire({"gene_id": "x", "document_id": "y"}, "canonical")


@pytest.mark.parametrize("mode", ["legacy", "canonical"])
def test_http_boundary_stats_explicit_responses_and_upstream_isolation(mode):
    app = FastAPI()
    app.router.route_class = wire_module().wire_route_class(mode)

    @app.get("/stats")
    def stats():
        return {"total_genes": 3, "total_codons": 7, "open": 1,
                "euchromatin": 1, "heterochromatin": 1}

    @app.get("/documents/x")
    def doc():
        return JSONResponse({"gene_id": "x"}, status_code=202, headers={"x-example": "yes"})

    @app.post("/v1/chat/completions")
    def upstream():
        return {"gene_id": "upstream-owned"}

    @app.get("/stream")
    def stream():
        return StreamingResponse(iter(['{"gene_id":"stream-owned"}']), media_type="application/json")

    with TestClient(app) as client:
        expected = ({"total_documents": 3, "total_fragments": 7, "open": 1, "warm": 1, "cold": 1}
                    if mode == "canonical" else stats())
        assert client.get("/stats").json() == expected
        response = client.get("/documents/x")
        assert response.status_code == 202 and response.headers["x-example"] == "yes"
        assert int(response.headers["content-length"]) == len(response.content)
        assert response.json() == {"document_id" if mode == "canonical" else "gene_id": "x"}
        assert client.post("/v1/chat/completions").json() == {"gene_id": "upstream-owned"}
        assert client.get("/stream").json() == {"gene_id": "stream-owned"}


def test_cli_json_uses_same_projection(capsys):
    from cymatix_context.cli.output import print_json
    print_json({"gene_id": "x"}, wire_format="canonical")
    assert json.loads(capsys.readouterr().out) == {"document_id": "x"}


@pytest.mark.parametrize("mode", ["legacy", "canonical"])
def test_real_http_document_stats_health_and_packet(mode):
    from cymatix_context.config import BudgetConfig
    from tests.conftest import make_client, make_cymatix_config, make_gene
    client = make_client(make_cymatix_config(budget=BudgetConfig(wire_format=mode)))
    store = client.app.state.cymatix.genome
    doc = make_gene(gene_id="wire-fixture", content="Wire fixture storage text")
    try:
        store.upsert_doc(doc, apply_gate=False)
        result = client.get("/documents/wire-fixture").json()
        assert result["document_id" if mode == "canonical" else "gene_id"] == "wire-fixture"
        assert result["content"] == doc.content
        # Both route aliases obey the selected wire version.
        assert client.get("/genes/wire-fixture").json() == result
        stats = client.get("/stats").json()
        assert stats["total_documents" if mode == "canonical" else "total_genes"] == 1
        assert ("warm" if mode == "canonical" else "euchromatin") in stats
        health = client.get("/health").json()
        assert ("documents" if mode == "canonical" else "genes") in health
        packet = client.post("/context/packet", json={"query": "Wire fixture storage", "read_only": True}).json()
        if packet.get("miss"):
            assert packet["miss"]["do_not_answer_from_knowledge_store" if mode == "canonical" else "do_not_answer_from_genome"] is True
        # Persistence still writes the legacy field names in both modes.
        stored = store.get_doc("wire-fixture").model_dump()
        assert stored["gene_id"] == "wire-fixture" and "document_id" not in stored
        assert store.read_conn.execute("SELECT gene_id FROM genes WHERE gene_id=?", ("wire-fixture",)).fetchone()[0] == "wire-fixture"
    finally:
        client.close()
        store.close()


@pytest.mark.parametrize("command", ["document", "gene"])
def test_cli_commands_use_session_wire_mode(command, monkeypatch):
    from cymatix_context.api import CymatixSession
    from cymatix_context.config import BudgetConfig
    from cymatix_context.context_manager import CymatixContextManager
    from tests.conftest import make_cymatix_config, make_gene, run_cli
    manager = CymatixContextManager(make_cymatix_config(budget=BudgetConfig(wire_format="canonical")))
    session = CymatixSession(manager)
    manager.genome.upsert_doc(make_gene(gene_id="wire-cli"), apply_gate=False)
    monkeypatch.setattr("cymatix_context.cli.cmd_gene.open_session", lambda: session)
    try:
        rc, out, err = run_cli([command, "get", "wire-cli", "--json"])
        assert rc == 0, err
        assert json.loads(out)["document_id"] == "wire-cli"
    finally:
        session.close()
        manager.genome.close()


def test_query_result_serializes_canonical_miss():
    from cymatix_context.api import QueryResult
    from cymatix_context.schemas import MissBlock
    result = QueryResult(expressed_context="", document_ids=[],
                         miss=MissBlock(reason="abstain", top_score=0, ratio=0, escalate_to=["grep"]), wire_format="canonical")
    assert result.to_agent_json()["miss"]["do_not_answer_from_knowledge_store"] is True


def test_packet_source_identity_does_not_collide_with_local_document_id():
    from cymatix_context.schemas import ContextItem
    item = ContextItem(gene_id="local-hash", document_id="src/config.py",
                       title="Config", content="configured port")
    result = wire_module().to_wire(item.model_dump(), "canonical")
    assert result["document_id"] == "local-hash"
    assert result["source_document_id"] == "src/config.py"
    assert result["kind"] == "document"
    assert wire_module().to_wire(result, "canonical") == result

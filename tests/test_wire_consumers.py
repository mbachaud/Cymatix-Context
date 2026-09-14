"""Exercise canonical HTTP payloads through the existing CLI/MCP consumers."""
import importlib
import json
from types import SimpleNamespace

import pytest

from cymatix_context.config import BudgetConfig
from tests.conftest import make_client, make_cymatix_config, make_gene, run_cli


@pytest.mark.parametrize("mode", ["legacy", "canonical"])
def test_legacy_mcp_stats_renders_real_http_history(mode, monkeypatch):
    from cymatix_context.mcp import server

    client = make_client(make_cymatix_config(budget=BudgetConfig(wire_format=mode)))
    store = client.app.state.cymatix.genome
    try:
        store.upsert_doc(make_gene(gene_id="history-doc"), apply_gate=False)
        store.log_health("wire history query", 0.5, 0.6, 0.7, 0.8, 2, 7, "healthy")
        # Replace only the network transport; actual routes serialize and
        # the MCP handler parses the resulting HTTP response bodies.
        monkeypatch.setattr(server, "httpx", SimpleNamespace(
            Client=lambda **kwargs: client,
            ConnectError=server.httpx.ConnectError,
        ))
        rendered = server._handle_cymatix_stats({"include_history": True})
        assert "Documents: 1" in rendered
        assert "Recent Queries:" in rendered
        assert "2/7" in rendered
        assert "wire history query" in rendered
    finally:
        client.close()
        store.close()


@pytest.mark.parametrize("mode", ["legacy", "canonical"])
def test_primary_mcp_document_preserves_metadata_and_health_count(mode, monkeypatch):
    mcp = importlib.import_module("cymatix_context.mcp.mcp_server")
    client = make_client(make_cymatix_config(budget=BudgetConfig(wire_format=mode)))
    store = client.app.state.cymatix.genome
    metadata = {"gene_id": "user-owned", "chromatin": 9, "genes": {"gene_id": "nested-user"}}
    try:
        doc = make_gene(gene_id="mcp-doc")
        doc.promoter.metadata = metadata
        store.upsert_doc(doc, apply_gate=False)

        def local_http(method, path, body=None):
            return client.request(method, path, json=body).json()

        monkeypatch.setattr(mcp, "_http", local_http)
        result = mcp.cymatix_document_get("mcp-doc")
        assert result["document_id" if mode == "canonical" else "gene_id"] == "mcp-doc"
        assert result["tags" if mode == "canonical" else "promoter"]["metadata"] == metadata
        assert mcp.cymatix_gene_get("mcp-doc") == result
        health = mcp.cymatix_health()
        assert health["server"]["documents" if mode == "canonical" else "genes"] == 1
        assert "empty" not in health["next_action"]
    finally:
        client.close()
        store.close()


@pytest.mark.parametrize("mode", ["legacy", "canonical"])
def test_primary_mcp_packet_preserves_distinct_local_and_source_ids(mode, monkeypatch):
    mcp = importlib.import_module("cymatix_context.mcp.mcp_server")
    client = make_client(make_cymatix_config(budget=BudgetConfig(wire_format=mode)))
    store = client.app.state.cymatix.genome
    try:
        doc = make_gene(gene_id="local-packet-id", content="wirepacket configuration content",
                        domains=["wirepacket"])
        doc.source_id = "src/wirepacket.py"
        store.upsert_doc(doc, apply_gate=False)

        def local_http(method, path, body=None):
            response = client.request(method, path, json=body)
            assert response.status_code == 200, response.text
            return response.json()

        monkeypatch.setattr(mcp, "_http", local_http)
        result = mcp.cymatix_context_packet("wirepacket")
        items = result["verified"] + result["stale_risk"] + result["contradictions"]
        assert len(items) == 1
        item = items[0]
        assert item["document_id" if mode == "canonical" else "gene_id"] == "local-packet-id"
        assert item["source_document_id" if mode == "canonical" else "document_id"] == "src/wirepacket.py"
        assert item["kind"] == ("document" if mode == "canonical" else "gene")
    finally:
        client.close()
        store.close()


@pytest.mark.parametrize("mode", ["legacy", "canonical"])
@pytest.mark.parametrize("explicit_db", [False, True])
def test_cli_diag_bed_projects_owned_counts(mode, explicit_db, tmp_path, monkeypatch):
    from cymatix_context.knowledge_store import KnowledgeStore
    from cymatix_context.storage import provenance

    path = str(tmp_path / "wire-bed.db")
    store = KnowledgeStore(path)
    config = make_cymatix_config(budget=BudgetConfig(wire_format=mode))
    config.genome.path = path
    monkeypatch.setattr("cymatix_context.config.load_config", lambda *args, **kwargs: config)
    try:
        store.upsert_doc(make_gene(gene_id="bed-doc"), apply_gate=False)
        assert provenance.record_event(store.conn, "wire fixture", overrides={"gene_id": "user value"})
        expected_events = provenance.read_events(store.conn)
        args = ["diag", "bed", "--json"]
        if explicit_db:
            args.extend(["--db", path])
        rc, out, err = run_cli(args)
        assert rc == 0, err
        payload = json.loads(out)
        key = "live_document_count" if mode == "canonical" else "live_gene_count"
        assert payload[key] == 1
        assert payload["events"] == expected_events
    finally:
        store.close()


def test_cli_diag_explicit_bed_remains_available_with_invalid_config(tmp_path, monkeypatch):
    from cymatix_context.knowledge_store import KnowledgeStore

    path = str(tmp_path / "invalid-config-bed.db")
    store = KnowledgeStore(path)

    def invalid_config():
        raise ValueError("broken config")

    monkeypatch.setattr("cymatix_context.config.load_config", invalid_config)
    try:
        rc, out, err = run_cli(["diag", "bed", "--db", path, "--json"])
        assert rc == 0, err
        assert json.loads(out)["live_gene_count"] == 0
    finally:
        store.close()


@pytest.mark.parametrize("mode,batching", [("legacy", False), ("canonical", True)])
@pytest.mark.parametrize("changed", [(), ("wire",), ("harmonic",), ("wire", "harmonic")])
def test_admin_reload_keeps_startup_flags_active_and_reloads_other_config(
    mode, batching, changed, monkeypatch,
):
    from cymatix_context.config import RetrievalConfig

    config = make_cymatix_config(
        budget=BudgetConfig(wire_format=mode, max_genes_per_turn=8),
        retrieval=RetrievalConfig(harmonic_batching_enabled=batching),
    )
    client = make_client(config)
    manager = client.app.state.cymatix
    try:
        manager.genome.upsert_doc(make_gene(gene_id="reload-doc"), apply_gate=False)
        requested_mode = ("canonical" if mode == "legacy" else "legacy") if "wire" in changed else mode
        requested_batching = not batching if "harmonic" in changed else batching
        requested = make_cymatix_config(
            budget=BudgetConfig(wire_format=requested_mode, max_genes_per_turn=11),
            retrieval=RetrievalConfig(harmonic_batching_enabled=requested_batching),
        )
        monkeypatch.setattr("cymatix_context.config.load_config", lambda: requested)
        response = client.post("/admin/reload")
        assert response.status_code == 200
        payload = response.json()
        assert payload["reloaded"] is True
        expected_restart = []
        if "wire" in changed:
            expected_restart.append("budget.wire_format")
        if "harmonic" in changed:
            expected_restart.append("retrieval.harmonic_batching_enabled")
        if expected_restart:
            assert payload["restart_required"] == expected_restart
        else:
            assert "restart_required" not in payload
            # Existing reload response shape stays unchanged for ordinary
            # config changes; even the legacy JSON projection is untouched.
            assert set(payload) == {"reloaded", "changes"}
        assert payload["changes"]["max_genes_per_turn"] == {"old": 8, "new": 11}
        assert manager.config.budget.max_genes_per_turn == 11
        assert manager.config.budget.wire_format == mode
        assert manager.config.retrieval.harmonic_batching_enabled is batching
        assert manager.genome._harmonic_batching_enabled is batching
        result = client.get("/documents/reload-doc").json()
        assert result["document_id" if mode == "canonical" else "gene_id"] == "reload-doc"
    finally:
        client.close()
        manager.genome.close()

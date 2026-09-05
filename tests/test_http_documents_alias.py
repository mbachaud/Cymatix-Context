"""GET /documents/{document_id} -- Tier 2 alias of GET /genes/{gene_id}.

Rosetta Tier 2 (docs/ROSETTA.md): the software-lexicon route name must
behave identically to the legacy biology-lexicon route it aliases --
same status code, same JSON body, for both the found and not-found
cases. This file only proves behavioral equivalence; it does not
re-test /genes/{gene_id}'s own correctness (test_server.py's
TestDebugIntrospectionEndpoints already covers that).
"""

from __future__ import annotations

import pytest

from tests.conftest import make_client, make_gene


@pytest.fixture
def client():
    return make_client()


@pytest.fixture
def ingested_gene_id(client):
    """Seed a single document directly into the store (bypassing the
    ribosome), the way tests/test_mem_sync.py's TestTombstoneRoute
    seeds genes for route tests."""
    genome = client.app.state.cymatix.genome
    gene = make_gene(
        content="Rosetta Tier 2 alias fixture content",
        domains=["test"],
        entities=["TestEntity"],
        gene_id="documents-alias-fixture-gene",
    )
    genome.upsert_gene(gene, apply_gate=False)
    return gene.gene_id


class TestDocumentsRouteAliasesGenes:
    def test_documents_route_aliases_genes(self, client, ingested_gene_id):
        a = client.get(f"/genes/{ingested_gene_id}")
        b = client.get(f"/documents/{ingested_gene_id}")
        assert a.status_code == 200
        assert b.status_code == a.status_code
        assert b.json() == a.json()

    def test_documents_route_404_matches(self, client):
        a = client.get("/genes/nope")
        b = client.get("/documents/nope")
        assert a.status_code == 404
        assert b.status_code == a.status_code
        assert b.json() == a.json()

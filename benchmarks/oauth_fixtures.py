"""Reusable OAuth canary fixtures for live and in-process benchmarks."""

from __future__ import annotations

import time
from typing import Any

from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.schemas import EpigeneticMarkers, Gene, PromoterTags

OAUTH_REQUIRED_FIXTURES: dict[str, str] = {
    "oauth_attr_01_claim_to_gene": (
        "tenant_a participant_id vendor_host_acme oauth attribution claim"
    ),
    "oauth_scope_01_cross_party_no_leak": (
        "tenant_a OAuth callback URL tenant-a.example/callback"
    ),
    "oauth_scope_02_legacy_unattributed_visible": (
        "legacy_oauth_redirect legacy.example/oauth/callback"
    ),
    "oauth_scope_03_dense_no_cross_party_leak": (
        "tenant_a_dense OAuth callback tenant-a.example/dense/callback"
    ),
    "oauth_session_01_vendor_host_identity": (
        "tenant_a vendor_host idp.tenant-a.example OAuth session"
    ),
}

OAUTH_FORBIDDEN_FIXTURES: list[str] = [
    "tenant_b tenant_b_secret tenant-b.example/callback vendor_host_bravo",
    "tenant_b_dense tenant_b_secret tenant-b.example/dense/callback",
    "tenant_b vendor_host idp.tenant-b.example OAuth session",
]


def _make_doc(content: str, *, domains: list[str], entities: list[str]) -> Gene:
    doc_id = KnowledgeStore.make_gene_id(content)  # static hasher — name is the on-disk contract
    return Gene(
        gene_id=doc_id,
        content=content,
        complement=f"OAuth bench fixture: {content[:80]}",
        codons=["chunk_0"],
        promoter=PromoterTags(
            domains=domains,
            entities=entities,
            intent="oauth",
            summary=content[:80],
        ),
        epigenetics=EpigeneticMarkers(),
    )


def _insert_party(store: KnowledgeStore, party_id: str, *, now: float) -> None:
    store.conn.execute(
        "INSERT OR IGNORE INTO parties (party_id, display_name, created_at) "
        "VALUES (?, ?, ?)",
        (party_id, party_id, now),
    )


def _attribute_doc(store: KnowledgeStore, doc_id: str, party_id: str, *, now: float) -> None:
    store.conn.execute(
        "INSERT OR REPLACE INTO gene_attribution "
        "(gene_id, party_id, participant_id, authored_at) VALUES (?, ?, NULL, ?)",
        (doc_id, party_id, now),
    )


def seed_oauth_fixtures(store: KnowledgeStore, *, now: float | None = None) -> dict[str, Any]:
    """Seed deterministic OAuth canary documents and tenant attribution.

    Tenant A fixtures are attributed to ``tenant_a`` except for the legacy
    redirect fixture, which intentionally remains unattributed so scoped
    retrieval can verify the legacy fallback. Tenant B fixtures are attributed
    to ``tenant_b`` and should be hidden from Tenant A scoped retrieval.
    """

    seeded_at = time.time() if now is None else now
    _insert_party(store, "tenant_a", now=seeded_at)
    _insert_party(store, "tenant_b", now=seeded_at)

    required = 0
    forbidden = 0
    attributed = 0

    for task_id, content in OAUTH_REQUIRED_FIXTURES.items():
        category = "scope"
        if "_attr_" in task_id:
            category = "attribute"
        elif "_session_" in task_id:
            category = "session"

        doc_id = store.upsert_doc(
            _make_doc(content, domains=["oauth", category], entities=["tenant_a"]),
            apply_gate=False,
        )
        required += 1
        if "legacy" not in task_id:
            _attribute_doc(store, doc_id, "tenant_a", now=seeded_at)
            attributed += 1

    for content in OAUTH_FORBIDDEN_FIXTURES:
        doc_id = store.upsert_doc(
            _make_doc(content, domains=["oauth", "scope"], entities=["tenant_b"]),
            apply_gate=False,
        )
        _attribute_doc(store, doc_id, "tenant_b", now=seeded_at)
        forbidden += 1
        attributed += 1

    store.conn.commit()
    return {
        "required_fixtures": required,
        "forbidden_fixtures": forbidden,
        "attributed_fixtures": attributed,
        "parties": ["tenant_a", "tenant_b"],
    }

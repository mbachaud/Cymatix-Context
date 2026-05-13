"""Frozen OAuth-shaped task records for model-free Helix benchmarks."""

from __future__ import annotations

from typing import Final

VALID_CATEGORIES: Final = frozenset({
    "attribute",
    "scope",
    "session",
    "impossible",
})

OAUTH_TASKS: list[dict] = [
    {
        "id": "oauth_attr_01_claim_to_gene",
        "category": "attribute",
        "query": (
            "Which OAuth participant claim maps the acme identity host to "
            "tenant A's genome records?"
        ),
        "required_in_context": [
            "tenant_a",
            "participant_id",
            "vendor_host_acme",
        ],
        "forbidden_in_context": [
            "tenant_b",
            "vendor_host_bravo",
        ],
        "required_fact": r"vendor_host_acme",
        "party_id": "tenant_a",
        "expected": "tenant A participant claim resolves to vendor_host_acme",
        "ground_truth_source": "synthetic oauth fixture: tenant_a_claims",
    },
    {
        "id": "oauth_scope_01_cross_party_no_leak",
        "category": "scope",
        "query": "What is tenant A's OAuth callback URL?",
        "required_in_context": [
            "tenant_a",
            "tenant-a.example/callback",
        ],
        "forbidden_in_context": [
            "tenant_b_secret",
            "tenant-b.example/callback",
        ],
        "required_fact": r"tenant-a\.example/callback",
        "party_id": "tenant_a",
        "expected": "tenant A callback only",
        "ground_truth_source": "synthetic oauth fixture: tenant_a_callback",
    },
    {
        "id": "oauth_scope_02_legacy_unattributed_visible",
        "category": "scope",
        "query": (
            "Which legacy OAuth redirect URI remains visible from tenant A's "
            "scoped retrieval?"
        ),
        "required_in_context": [
            "legacy_oauth_redirect",
            "legacy.example/oauth/callback",
        ],
        "forbidden_in_context": [
            "tenant_b_secret",
            "tenant-b.example/callback",
        ],
        "required_fact": r"legacy\.example/oauth/callback",
        "party_id": "tenant_a",
        "expected": "legacy unattributed redirect remains visible",
        "ground_truth_source": "synthetic oauth fixture: legacy_redirect",
    },
    {
        "id": "oauth_scope_03_dense_no_cross_party_leak",
        "category": "scope",
        "query": (
            "Find the dense-recall OAuth callback for tenant A without using "
            "the similarly named tenant B callback."
        ),
        "required_in_context": [
            "tenant_a_dense",
            "tenant-a.example/dense/callback",
        ],
        "forbidden_in_context": [
            "tenant_b_dense",
            "tenant-b.example/dense/callback",
            "tenant_b_secret",
        ],
        "required_fact": r"tenant-a\.example/dense/callback",
        "party_id": "tenant_a",
        "expected": "dense recall returns tenant A callback and not tenant B",
        "ground_truth_source": "synthetic oauth fixture: dense_scope_guard",
    },
    {
        "id": "oauth_session_01_vendor_host_identity",
        "category": "session",
        "query": (
            "What vendor host identity is registered for tenant A's OAuth "
            "session?"
        ),
        "required_in_context": [
            "tenant_a",
            "vendor_host",
            "idp.tenant-a.example",
        ],
        "forbidden_in_context": [
            "idp.tenant-b.example",
        ],
        "required_fact": r"idp\.tenant-a\.example",
        "party_id": "tenant_a",
        "expected": "tenant A session uses idp.tenant-a.example",
        "ground_truth_source": "synthetic oauth fixture: tenant_a_session",
    },
    {
        "id": "oauth_imp_01_no_google_client_id",
        "category": "impossible",
        "query": "What is tenant A's Google OAuth client ID?",
        "required_in_context": [],
        "forbidden_in_context": [
            "google_client_id",
            "apps.googleusercontent.com",
        ],
        "required_fact": r"\b\d{6,}-[a-z0-9-]+\.apps\.googleusercontent\.com\b",
        "party_id": "tenant_a",
        "expected": "no Google client ID exists in the fixture",
        "ground_truth_source": "synthetic oauth fixture: explicit absence",
    },
    {
        "id": "oauth_imp_02_no_refresh_token_fabrication",
        "category": "impossible",
        "query": "What refresh token should tenant A use for offline OAuth access?",
        "required_in_context": [],
        "forbidden_in_context": [
            "refresh_token",
            "offline_access",
        ],
        "required_fact": r"(?:ya29\.|1//)[A-Za-z0-9_-]+",
        "party_id": "tenant_a",
        "expected": "no refresh token exists in the fixture",
        "ground_truth_source": "synthetic oauth fixture: explicit absence",
    },
]

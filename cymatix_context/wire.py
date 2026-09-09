"""Opt-in Tier 3 response vocabulary; SQL and Python model fields stay frozen.

Only owned field names are projected. Strings, identifier values and opaque
user dictionaries are never rewritten. The legacy path returns its input
unchanged, including serialization order. See docs/ROSETTA.md.
"""
from __future__ import annotations

from typing import Any


FIELD_NAMES = {
    "gene_id": "document_id",
    "gene_ids": "document_ids",
    "gene_id_a": "document_id_a",
    "gene_id_b": "document_id_b",
    "gene_id_match": "document_id_match",
    "expressed_gene_ids": "expressed_document_ids",
    "delivered_gene_ids": "delivered_document_ids",
    "do_not_answer_from_genome": "do_not_answer_from_knowledge_store",
    "genes": "documents",
    "total_genes": "total_documents",
    "total_codons": "total_fragments",
    "genes_expressed": "documents_expressed",
    "genes_available": "documents_available",
    "genes_created": "documents_created",
    "genes_ingested": "documents_ingested",
    "genes_updated": "documents_updated",
    "genes_deleted": "documents_deleted",
    "genes_removed": "documents_removed",
    "genes_checked": "documents_checked",
    "genes_warmed": "documents_warmed",
    "genes_cooled": "documents_cooled",
    "genes_repacked": "documents_repacked",
    "gene_count": "document_count",
    "live_gene_count": "live_document_count",
    "max_genes": "max_documents",
    "presence_gene_id": "presence_document_id",
    "codon_count": "fragment_count",
    "codons": "fragments",
    "promoter": "tags",
    "epigenetics": "signals",
    "chromatin": "tier",
    "chromatin_state": "tier",
    "euchromatin": "warm",
    "heterochromatin": "cold",
    "euchromatin_summary": "warm_summary",
    "heterochromatin_cold": "cold_archive",
    "to_euchromatin": "to_warm",
    "to_heterochromatin": "to_cold",
    "chromatin_open": "open",
    "chromatin_eu": "warm",
    "chromatin_hetero": "cold",
    "chromatin_euchromatin": "warm",
    "chromatin_heterochromatin": "cold",
    "genome": "knowledge_store",
    "genome_path": "knowledge_store_path",
    "genome_size": "knowledge_store_size",
    "genome_bytes": "knowledge_store_bytes",
    "genome_genes": "knowledge_store_documents",
    "genome_error": "knowledge_store_error",
    "genome_ready": "knowledge_store_ready",
    "genome_total_genes": "knowledge_store_total_documents",
    "genome_hetero_count": "knowledge_store_cold_count",
    "ribosome": "compressor",
    "ribosome_prompt": "decoder_prompt",
    "ribosome_model": "compressor_model",
    "ribosome_backend": "compressor_backend",
    "ribosome_configured_backend": "compressor_configured_backend",
    "ribosome_cost_class": "compressor_cost_class",
    "ribosome_enabled": "compressor_enabled",
    "ribosome_tokens": "decoder_tokens",
    "harmonic_links": "coactivation_edges",
    "gene_attribution": "document_attribution",
}

# Document metadata, extracted fact objects, model configuration and maps
# keyed by document IDs are application/user data, not response schemas.
_OPAQUE_FIELDS = frozenset({
    "metadata", "key_values", "config", "retrieval_scores",
    "tier_contributions", "retrieval_tiers", "scores",
    "events", "config_drift",
})


def to_wire(value: Any, wire_format: str = "legacy") -> Any:
    """Project a JSON-shaped payload without changing models or their inputs.

    Equal aliases collapse; conflicting aliases fail visibly rather than
    silently choosing data. Unknown keys are retained for forward compatibility.
    """
    if wire_format != "canonical":
        return value
    if isinstance(value, (list, tuple)):
        return [to_wire(item, wire_format) for item in value]
    if not isinstance(value, dict):
        return value
    # ContextItem already uses document_id for the source-derived portable
    # identity, distinct from its local gene_id. Preserve BOTH identities.
    is_context_item = {"gene_id", "document_id", "kind", "relevance_score",
                       "live_truth_score", "citations"}.issubset(value)
    result = {}
    for key, item in value.items():
        target = "source_document_id" if is_context_item and key == "document_id" else FIELD_NAMES.get(key, key)
        projected = item if key in _OPAQUE_FIELDS else to_wire(item, wire_format)
        if is_context_item and key == "kind" and projected == "gene":
            projected = "document"
        if target in result and result[target] != projected:
            raise ValueError(f"Canonical wire field collision for {target!r}")
        result[target] = projected
    return result


def wire_route_class(wire_format: str):
    """Build a per-app JSON response boundary (never a global model setting).

    OpenAI proxy responses and streams belong to the upstream provider.
    They bypass this projection. Explicit JSONResponse status, headers and
    background tasks are retained. Legacy installs use FastAPI's own route.
    """
    from fastapi.routing import APIRoute

    if wire_format != "canonical":
        return APIRoute

    import json

    class CanonicalWireRoute(APIRoute):
        def get_route_handler(self):
            handler = super().get_route_handler()

            async def canonical_response(request):
                response = await handler(request)
                if (request.url.path.startswith("/v1/")
                        or request.url.path in {"/config", "/admin/config"}
                        or not hasattr(response, "body")
                        or response.headers.get("content-type", "").split(";")[0] != "application/json"):
                    return response
                payload = json.loads(response.body)
                response.body = json.dumps(
                    to_wire(payload, wire_format), ensure_ascii=False,
                    allow_nan=False, separators=(",", ":"),
                ).encode("utf-8")
                response.headers["content-length"] = str(len(response.body))
                return response

            return canonical_response

    return CanonicalWireRoute

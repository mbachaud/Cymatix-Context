"""Strict HTTP client for the benchmark-owned Cymatix sidecar."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import TreatmentConfig
from .workspace import SourceRecord

log = logging.getLogger("cymatix.scbench.client")


class CymatixProtocolError(RuntimeError):
    """The sidecar returned a successful but contract-invalid response."""


@dataclass(frozen=True)
class IngestVerification:
    source_id: str
    gene_ids: tuple[str, ...]
    count: int


@dataclass(frozen=True)
class PacketResponse:
    query: str
    task_type: str
    payload: dict[str, Any]


class CymatixClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CymatixClient":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    @staticmethod
    def _json_object(response: httpx.Response, operation: str) -> dict[str, Any]:
        response.raise_for_status()
        try:
            payload = response.json()
        except Exception as exc:
            log.warning("%s returned invalid JSON", operation, exc_info=True)
            raise CymatixProtocolError(
                f"{operation} returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise CymatixProtocolError(f"{operation} response must be an object")
        return payload

    def health(self) -> dict[str, Any]:
        payload = self._json_object(self._client.get("/health"), "health")
        checks = payload.get("checks")
        if not isinstance(checks, dict) or not isinstance(
            checks.get("genome_ready"), bool
        ):
            raise CymatixProtocolError("health missing checks.genome_ready")
        if not isinstance(payload.get("status"), str):
            raise CymatixProtocolError("health missing status")
        if not isinstance(payload.get("genes"), int):
            raise CymatixProtocolError("health missing genes")
        return payload

    def stats(self) -> dict[str, Any]:
        payload = self._json_object(self._client.get("/stats"), "stats")
        total_genes = payload.get("total_genes")
        if not isinstance(total_genes, int) or total_genes < 0:
            raise CymatixProtocolError("stats missing non-negative total_genes")
        return payload

    def _verify_ingest(
        self,
        response: httpx.Response,
        source_id: str,
    ) -> IngestVerification:
        payload = self._json_object(response, "ingest")
        gene_ids = payload.get("gene_ids")
        count = payload.get("count")
        if not isinstance(gene_ids, list) or not all(
            isinstance(gene_id, str) and gene_id for gene_id in gene_ids
        ):
            raise CymatixProtocolError("ingest returned invalid gene ids")
        if not gene_ids:
            raise CymatixProtocolError("ingest returned no gene ids")
        if not isinstance(count, int) or count != len(gene_ids):
            raise CymatixProtocolError("ingest count does not match gene ids")
        return IngestVerification(
            source_id=source_id,
            gene_ids=tuple(gene_ids),
            count=count,
        )

    def ingest_source(self, source: SourceRecord) -> IngestVerification:
        problem = urlsplit(source.source_id).netloc
        if not problem:
            raise CymatixProtocolError("source id is missing a problem")
        response = self._client.post(
            "/ingest",
            json={
                "content": source.content,
                "content_type": source.content_type,
                "metadata": {
                    "path": source.source_id,
                    "relative_path": source.relative_path,
                    "source_id": source.source_id,
                    "problem": problem,
                    "content_sha256": source.sha256,
                    "source_kind": "code",
                },
                "local_federation": False,
            },
        )
        return self._verify_ingest(response, source.source_id)

    def ingest_conversation(
        self,
        *,
        campaign: str,
        pair: str,
        replicate: int,
        checkpoint: int,
        content: str,
    ) -> IngestVerification:
        source_id = (
            f"conversation://{campaign}/{pair}/{replicate}/{checkpoint}"
        )
        response = self._client.post(
            "/ingest",
            json={
                "content": content,
                "content_type": "conversation",
                "metadata": {
                    "path": source_id,
                    "source_id": source_id,
                    "campaign": campaign,
                    "pair": pair,
                    "replicate": replicate,
                    "checkpoint": checkpoint,
                    "source_kind": "conversation",
                },
                "local_federation": False,
            },
        )
        return self._verify_ingest(response, source_id)

    def tombstone(self, source_id: str) -> tuple[str, ...]:
        payload = self._json_object(
            self._client.post(
                "/admin/genes/tombstone",
                json={"source_id": source_id},
            ),
            "tombstone",
        )
        gene_ids = payload.get("gene_ids")
        if payload.get("source_id") != source_id:
            raise CymatixProtocolError("tombstone source id mismatch")
        if not isinstance(gene_ids, list) or not all(
            isinstance(gene_id, str) for gene_id in gene_ids
        ):
            raise CymatixProtocolError("tombstone returned invalid gene ids")
        if payload.get("tombstoned") != len(gene_ids):
            raise CymatixProtocolError("tombstone count does not match gene ids")
        return tuple(gene_ids)

    def context_packet(
        self,
        query: str,
        *,
        task_type: str,
        session_id: str,
        treatment: TreatmentConfig,
    ) -> PacketResponse:
        payload = self._json_object(
            self._client.post(
                "/context/packet",
                json={
                    "query": query,
                    "task_type": task_type,
                    "session_id": session_id,
                    "max_genes": treatment.max_genes,
                    "include_raw": treatment.include_raw,
                    "max_item_chars": treatment.max_item_chars,
                    "ignore_delivered": treatment.ignore_delivered,
                    "read_only": treatment.read_only,
                },
            ),
            "context packet",
        )
        for field in ("verified", "stale_risk", "refresh_targets", "notes"):
            if not isinstance(payload.get(field), list):
                raise CymatixProtocolError(
                    f"context packet missing list field: {field}"
                )
        if payload.get("query") != query or payload.get("task_type") != task_type:
            raise CymatixProtocolError("context packet request echo mismatch")
        return PacketResponse(query=query, task_type=task_type, payload=payload)

    def wait_ready(
        self,
        *,
        deadline_s: float,
        poll_interval_s: float = 0.1,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, deadline_s)
        last_error: Exception | None = None
        while True:
            try:
                payload = self.health()
                if payload["checks"]["genome_ready"] is True:
                    return payload
                last_error = CymatixProtocolError(
                    "health checks.genome_ready is false"
                )
            except httpx.HTTPError as exc:
                last_error = exc

            if time.monotonic() >= deadline:
                raise CymatixProtocolError(
                    f"genome_ready was not reached: {last_error}"
                ) from last_error
            time.sleep(max(0.001, poll_interval_s))

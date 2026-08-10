"""Fail-closed closed-loop coordination for SCBench treatment checkpoints."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .client import CymatixClient, PacketResponse
from .config import CampaignConfig
from .packet import (
    RenderedPacket,
    build_retrieval_query,
    render_packet,
)
from .receipts import (
    ArtifactRef,
    PacketItemProvenance,
    RetrievalReceipt,
    SyncReceipt,
    write_artifact,
)
from .server_process import CymatixServerProcess
from .workspace import (
    WorkspaceManifest,
    diff_workspace,
    scan_workspace,
)

log = logging.getLogger("cymatix.scbench.coordinator")


class TreatmentInvalidError(RuntimeError):
    """The treatment observation is invalid and must not fall back to control."""


class _State(Enum):
    NEW = "new"
    READY = "ready"
    PREPARED = "prepared"
    LEARNED = "learned"
    INVALID = "invalid"
    CLOSED = "closed"


@dataclass(frozen=True)
class PreparedPrompt:
    original_prompt: str
    prompt: str
    packet: RenderedPacket | None
    checkpoint_index: int
    prompt_artifact: ArtifactRef
    query_artifact: ArtifactRef | None = None
    packet_artifact: ArtifactRef | None = None
    rendered_packet_artifact: ArtifactRef | None = None
    retrieval: RetrievalReceipt | None = None


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _manifest_evidence(manifest: WorkspaceManifest) -> dict[str, Any]:
    return {
        "problem": manifest.problem,
        "manifest_hash": manifest.manifest_hash(),
        "sources": [
            {
                "source_id": source.source_id,
                "relative_path": source.relative_path,
                "sha256": source.sha256,
                "size_bytes": source.size_bytes,
                "content_type": source.content_type,
            }
            for source in manifest.sources.values()
        ],
    }


def _packet_provenance(
    response: PacketResponse,
    rendered: RenderedPacket,
) -> tuple[PacketItemProvenance, ...]:
    included = set(rendered.included_source_ids)
    provenance: list[PacketItemProvenance] = []
    for category in ("verified", "stale_risk", "contradictions"):
        for item in response.payload.get(category, []):
            if not isinstance(item, dict):
                continue
            source_id = item.get("source_id")
            content = item.get("content")
            if source_id not in included or not isinstance(content, str):
                continue
            relevance = item.get("relevance_score", 0.0)
            live_truth = item.get("live_truth_score", 0.0)
            provenance.append(
                PacketItemProvenance(
                    gene_id=str(item.get("gene_id", "")),
                    source_id=source_id,
                    status=str(item.get("status", category)),
                    content_sha256=hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest(),
                    relevance_score=float(relevance),
                    live_truth_score=float(live_truth),
                )
            )
    return tuple(provenance)


class TreatmentCoordinator:
    """Own one problem/replicate Cymatix treatment state machine."""

    def __init__(
        self,
        *,
        campaign: CampaignConfig,
        pair_id: str,
        replicate: int,
        problem: str,
        workspace_root: Path,
        receipt_root: Path,
        client: CymatixClient | Any | None = None,
        server_process: CymatixServerProcess | None = None,
        evaluation_roots: tuple[Path, ...] = (),
        warmup_enabled: bool = True,
        timeout_s: float = 20.0,
    ) -> None:
        if problem not in campaign.problems:
            raise ValueError(f"problem is not in the frozen campaign: {problem}")
        if replicate not in {spec.id for spec in campaign.replicates}:
            raise ValueError(f"replicate is not in the frozen campaign: {replicate}")
        self.campaign = campaign
        self.pair_id = pair_id
        self.replicate = replicate
        self.problem = problem
        self.workspace_root = workspace_root.resolve()
        self.receipt_root = receipt_root.resolve()
        self.evaluation_roots = evaluation_roots
        self.warmup_enabled = warmup_enabled
        self.timeout_s = timeout_s
        self.session_id = (
            f"{campaign.campaign_id}:{pair_id}:{replicate}:{problem}"
        )

        self._client = client
        self._server = server_process
        self._state = _State.NEW
        self._invalid_reason: str | None = None
        self._manifest: WorkspaceManifest | None = None
        self._checkpoint_index = 0
        self._prepared: PreparedPrompt | None = None
        self._last_sync_complete = False
        self._deleted_source_ids: set[str] = set()
        self.initial_sync: SyncReceipt | None = None
        self.last_sync: SyncReceipt | None = None
        self.warmup_elapsed_ms = 0

    @property
    def deleted_source_ids(self) -> frozenset[str]:
        return frozenset(self._deleted_source_ids)

    @property
    def checkpoint_index(self) -> int:
        return self._checkpoint_index

    def _invalidate(self, operation: str, exc: Exception) -> TreatmentInvalidError:
        reason = f"{operation} failure: {exc}"
        self._state = _State.INVALID
        self._invalid_reason = reason
        self._last_sync_complete = False
        log.warning("SCBench treatment invalidated: %s", reason, exc_info=True)
        return TreatmentInvalidError(reason)

    def _require_state(self, expected: _State) -> None:
        if self._state == _State.INVALID:
            raise TreatmentInvalidError(
                self._invalid_reason or "treatment is invalid"
            )
        if self._state == _State.CLOSED:
            raise TreatmentInvalidError("treatment coordinator is closed")
        if self._state != expected:
            raise RuntimeError(
                f"coordinator state is {self._state.value}; "
                f"expected {expected.value}"
            )

    def _client_or_raise(self) -> Any:
        if self._client is None:
            raise RuntimeError("Cymatix client is not initialized")
        return self._client

    def _stats(self) -> dict[str, Any]:
        payload = self._client_or_raise().stats()
        if not isinstance(payload, dict) or not isinstance(
            payload.get("total_genes"), int
        ):
            raise RuntimeError("Cymatix stats omitted total_genes")
        return payload

    def _write_manifest_artifact(
        self,
        manifest: WorkspaceManifest,
    ) -> ArtifactRef:
        return write_artifact(
            self.receipt_root,
            "workspace-manifest",
            _canonical_json(_manifest_evidence(manifest)),
            media_type="application/json",
        )

    def _write_sync_artifact(self, sync: SyncReceipt) -> ArtifactRef:
        return write_artifact(
            self.receipt_root,
            "sync",
            _canonical_json(sync.model_dump(mode="json")),
            media_type="application/json",
        )

    def initialize(self) -> None:
        self._require_state(_State.NEW)
        try:
            if self._client is None:
                if self._server is None:
                    self._server = CymatixServerProcess.for_run(
                        self.receipt_root / "sidecar",
                        timeout_s=self.timeout_s,
                    )
                self._server.start(wait_ready=True, deadline_s=30.0)
                self._client = CymatixClient(
                    self._server.base_url,
                    timeout_s=self.timeout_s,
                )
            else:
                self._client.wait_ready(deadline_s=30.0)

            before_stats = self._stats()
            manifest = scan_workspace(
                self.workspace_root,
                self.problem,
                self.campaign.source_policy,
            )

            started = time.monotonic()
            ingested = 0
            for source in manifest.sources.values():
                verification = self._client.ingest_source(source)
                ingested += verification.count
            after_stats = self._stats()
            if manifest.sources and (
                ingested <= 0
                or after_stats["total_genes"] <= before_stats["total_genes"]
            ):
                raise RuntimeError("initial ingest did not grow the genome")

            self._write_manifest_artifact(manifest)
            sync = SyncReceipt(
                pre_manifest_hash=_hash_json(
                    {"problem": self.problem, "sources": []}
                ),
                post_manifest_hash=manifest.manifest_hash(),
                genome_id=self.session_id,
                pre_genome_checksum=_hash_json(before_stats),
                post_genome_checksum=_hash_json(after_stats),
                ingested=ingested,
                skipped=0,
                deleted_source_ids=(),
                latency_ms=_elapsed_ms(started),
                verified=True,
            )
            self._write_sync_artifact(sync)
            self.initial_sync = sync
            self.last_sync = sync
            self._manifest = manifest

            if self.warmup_enabled and manifest.sources:
                warmup_started = time.monotonic()
                warmup = self._client.context_packet(
                    "SCBench unscored warm-up packet verification",
                    task_type="edit",
                    session_id=self.session_id,
                    treatment=self.campaign.treatment,
                )
                render_packet(warmup.payload, manifest, self.campaign.treatment)
                self.warmup_elapsed_ms = _elapsed_ms(warmup_started)
        except Exception as exc:
            raise self._invalidate("ingest", exc) from exc

        self._last_sync_complete = True
        self._state = _State.READY

    def before_checkpoint(self, original_prompt: str) -> PreparedPrompt:
        self._require_state(_State.READY)
        if not self._last_sync_complete:
            raise TreatmentInvalidError("prior checkpoint sync is incomplete")
        if not isinstance(original_prompt, str) or not original_prompt.strip():
            raise ValueError("checkpoint prompt must be non-empty")

        checkpoint_index = self._checkpoint_index + 1
        if checkpoint_index == 1:
            try:
                prompt_artifact = write_artifact(
                    self.receipt_root,
                    "prompt",
                    original_prompt,
                    media_type="text/plain",
                )
            except Exception as exc:
                raise self._invalidate("receipt", exc) from exc
            prepared = PreparedPrompt(
                original_prompt=original_prompt,
                prompt=original_prompt,
                packet=None,
                checkpoint_index=checkpoint_index,
                prompt_artifact=prompt_artifact,
            )
        else:
            manifest = self._manifest
            if manifest is None:
                raise TreatmentInvalidError("workspace manifest is unavailable")
            try:
                query = build_retrieval_query(
                    problem=self.problem,
                    checkpoint_index=checkpoint_index,
                    original_prompt=original_prompt,
                    evaluation_roots=self.evaluation_roots,
                )
                packet_started = time.monotonic()
                response = self._client_or_raise().context_packet(
                    query,
                    task_type="edit",
                    session_id=self.session_id,
                    treatment=self.campaign.treatment,
                )
                rendered = render_packet(
                    response.payload,
                    manifest,
                    self.campaign.treatment,
                )
                packet_latency_ms = _elapsed_ms(packet_started)
                prepared_text = f"{original_prompt}\n\n{rendered.text}"
            except Exception as exc:
                raise self._invalidate("packet", exc) from exc

            try:
                query_artifact = write_artifact(
                    self.receipt_root,
                    "query",
                    query,
                    media_type="text/plain",
                )
                raw_packet = _canonical_json(response.payload)
                packet_artifact = write_artifact(
                    self.receipt_root,
                    "packet",
                    raw_packet,
                    media_type="application/json",
                )
                rendered_artifact = write_artifact(
                    self.receipt_root,
                    "rendered-packet",
                    rendered.text,
                    media_type="text/plain",
                )
                prompt_artifact = write_artifact(
                    self.receipt_root,
                    "prompt",
                    prepared_text,
                    media_type="text/plain",
                )
                retrieval = RetrievalReceipt(
                    query_artifact=query_artifact,
                    query_sha256=query_artifact.sha256,
                    packet_artifact=packet_artifact,
                    packet_sha256=packet_artifact.sha256,
                    rendered_artifact=rendered_artifact,
                    rendered_token_count=rendered.token_count,
                    tokenizer=rendered.tokenizer,
                    latency_ms=packet_latency_ms,
                    items=_packet_provenance(response, rendered),
                    deleted_source_filtered=rendered.deleted_source_filtered,
                    budget_dropped=rendered.budget_dropped,
                )
            except Exception as exc:
                raise self._invalidate("receipt", exc) from exc
            prepared = PreparedPrompt(
                original_prompt=original_prompt,
                prompt=prepared_text,
                packet=rendered,
                checkpoint_index=checkpoint_index,
                prompt_artifact=prompt_artifact,
                query_artifact=query_artifact,
                packet_artifact=packet_artifact,
                rendered_packet_artifact=rendered_artifact,
                retrieval=retrieval,
            )

        self._checkpoint_index = checkpoint_index
        self._prepared = prepared
        self._last_sync_complete = False
        self._state = _State.PREPARED
        return prepared

    def after_checkpoint(
        self,
        original_prompt: str,
        final_agent_message: str,
    ) -> SyncReceipt:
        self._require_state(_State.PREPARED)
        prepared = self._prepared
        if prepared is None:
            raise RuntimeError("prepared checkpoint is unavailable")
        if original_prompt != prepared.original_prompt:
            raise TreatmentInvalidError("checkpoint prompt does not match preparation")
        if not isinstance(final_agent_message, str) or not final_agent_message.strip():
            raise TreatmentInvalidError("final agent message must be non-empty")

        previous = self._manifest
        if previous is None:
            raise TreatmentInvalidError("workspace manifest is unavailable")
        started = time.monotonic()
        try:
            before_stats = self._stats()
            conversation = (
                f"User:\n{original_prompt}\n\n"
                f"Assistant:\n{final_agent_message}"
            )
            conversation_verification = self._client_or_raise().ingest_conversation(
                campaign=self.campaign.campaign_id,
                pair=self.pair_id,
                replicate=self.replicate,
                checkpoint=self._checkpoint_index,
                content=conversation,
            )
            current = scan_workspace(
                self.workspace_root,
                self.problem,
                self.campaign.source_policy,
            )
            delta = diff_workspace(previous, current)
            ingested = conversation_verification.count
            for source_id in (*delta.created, *delta.changed):
                verification = self._client.ingest_source(
                    current.sources[source_id]
                )
                ingested += verification.count
            for source_id in delta.deleted:
                self._client.tombstone(source_id)
            after_stats = self._stats()
            if after_stats["total_genes"] < before_stats["total_genes"]:
                raise RuntimeError("genome count decreased after checkpoint sync")
            health = self._client.health()
            checks = health.get("checks") if isinstance(health, dict) else None
            if not isinstance(checks, dict) or checks.get("genome_ready") is not True:
                raise RuntimeError(
                    "health checks.genome_ready is false after checkpoint sync"
                )
            sync = SyncReceipt(
                pre_manifest_hash=previous.manifest_hash(),
                post_manifest_hash=current.manifest_hash(),
                genome_id=self.session_id,
                pre_genome_checksum=_hash_json(before_stats),
                post_genome_checksum=_hash_json(after_stats),
                ingested=ingested,
                skipped=len(delta.unchanged),
                deleted_source_ids=delta.deleted,
                latency_ms=_elapsed_ms(started),
                verified=True,
            )
        except Exception as exc:
            raise self._invalidate("ingest", exc) from exc

        try:
            write_artifact(
                self.receipt_root,
                "agent-message",
                final_agent_message,
                media_type="text/plain",
            )
            self._write_manifest_artifact(current)
            self._write_sync_artifact(sync)
        except Exception as exc:
            raise self._invalidate("receipt", exc) from exc

        self._deleted_source_ids.update(delta.deleted)
        self._manifest = current
        self.last_sync = sync
        self._last_sync_complete = True
        self._state = _State.LEARNED
        return sync

    def finish_checkpoint(self) -> None:
        self._require_state(_State.LEARNED)
        if not self._last_sync_complete:
            raise TreatmentInvalidError("checkpoint sync is incomplete")
        self._prepared = None
        self._state = _State.READY

    def close(self) -> None:
        if self._state == _State.CLOSED:
            return
        client = self._client
        server = self._server
        try:
            if client is not None:
                client.close()
        except Exception:
            log.warning("failed to close Cymatix benchmark client", exc_info=True)
        try:
            if server is not None:
                server.stop()
        except Exception:
            log.warning("failed to stop Cymatix benchmark sidecar", exc_info=True)
        self._state = _State.CLOSED

    def __enter__(self) -> "TreatmentCoordinator":
        self.initialize()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

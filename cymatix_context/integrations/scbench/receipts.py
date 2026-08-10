"""Strict, tamper-evident receipts for the SCBench Cymatix campaign."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Arm

log = logging.getLogger("cymatix.scbench.receipts")

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
_ARTIFACT_CATEGORY = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SECRET_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "openai_api_key",
    "password",
    "refresh_token",
    "secret",
    "token",
}


class ReceiptIntegrityError(RuntimeError):
    """A receipt cannot prove the campaign observation it describes."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactRef(_FrozenModel):
    """Repository-relative reference to content-addressed evidence."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1)

    @model_validator(mode="after")
    def _require_safe_relative_path(self) -> "ArtifactRef":
        normalized = self.path.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            normalized != self.path
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("artifact path must be a normalized relative path")
        return self


class HostObservation(_FrozenModel):
    fingerprint: str = Field(min_length=1)
    load: Literal["idle", "normal", "busy", "unknown"] = "unknown"
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    memory_percent: float | None = Field(default=None, ge=0, le=100)


class SyncReceipt(_FrozenModel):
    pre_manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    post_manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    genome_id: str = Field(min_length=1)
    pre_genome_checksum: str = Field(pattern=_SHA256_PATTERN)
    post_genome_checksum: str = Field(pattern=_SHA256_PATTERN)
    ingested: int = Field(ge=0)
    skipped: int = Field(ge=0)
    deleted_source_ids: tuple[str, ...] = ()
    latency_ms: int = Field(ge=0)
    verified: bool


class PacketItemProvenance(_FrozenModel):
    gene_id: str = ""
    source_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    relevance_score: float
    live_truth_score: float


class RetrievalReceipt(_FrozenModel):
    query_artifact: ArtifactRef
    query_sha256: str = Field(pattern=_SHA256_PATTERN)
    packet_artifact: ArtifactRef
    packet_sha256: str = Field(pattern=_SHA256_PATTERN)
    rendered_artifact: ArtifactRef
    rendered_token_count: int = Field(ge=0)
    tokenizer: str = Field(min_length=1)
    latency_ms: int = Field(ge=0)
    items: tuple[PacketItemProvenance, ...] = ()
    deleted_source_filtered: tuple[str, ...] = ()
    budget_dropped: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _hashes_match_artifacts(self) -> "RetrievalReceipt":
        if self.query_sha256 != self.query_artifact.sha256:
            raise ValueError("query hash must match query artifact")
        if self.packet_sha256 != self.packet_artifact.sha256:
            raise ValueError("packet hash must match packet artifact")
        return self


class CodexReceipt(_FrozenModel):
    exit_status: int
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    rate_limit_events: tuple[str, ...] = ()
    result_artifact: ArtifactRef


class ScoreReceipt(_FrozenModel):
    strict_pass_rate: float = Field(ge=0, le=1)
    isolated_pass_rate: float = Field(ge=0, le=1)
    erosion: float
    verbosity: float
    raw_artifact: ArtifactRef


class CheckpointReceipt(_FrozenModel):
    receipt_type: Literal["checkpoint"] = "checkpoint"
    schema_version: Literal[1] = 1
    campaign_id: str = Field(min_length=1)
    pair_id: str = Field(min_length=1)
    replicate: int = Field(ge=1)
    arm: Arm
    order_index: int = Field(ge=1, le=2)
    problem: str = Field(min_length=1)
    checkpoint: int = Field(ge=1)
    config_hash: str = Field(pattern=_SHA256_PATTERN)
    cymatix_commit: str = Field(pattern=_COMMIT_PATTERN)
    scbench_commit: str = Field(pattern=_COMMIT_PATTERN)
    codex_version: str = Field(min_length=1)
    codex_model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    image_digest: str = Field(min_length=1)
    auth_type: Literal["chatgpt_subscription"] = "chatgpt_subscription"
    host: HostObservation
    started_at: str = Field(min_length=1)
    ended_at: str = Field(min_length=1)
    elapsed_ms: int = Field(ge=0)
    workspace_manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    workspace_manifest_artifact: ArtifactRef | None = None
    prompt_artifact: ArtifactRef
    sync: SyncReceipt | None = None
    retrieval: RetrievalReceipt | None = None
    codex: CodexReceipt | None = None
    score: ScoreReceipt | None = None
    terminal_state: Literal["completed", "failed", "invalidated"]
    valid: bool
    invalidation_reason: str | None
    retry_of: str | None

    @model_validator(mode="after")
    def _validate_terminal_state(self) -> "CheckpointReceipt":
        if self.terminal_state == "completed":
            if not self.valid:
                raise ValueError("completed checkpoint must be valid")
            if self.invalidation_reason is not None:
                raise ValueError(
                    "completed checkpoint cannot have an invalidation reason"
                )
        else:
            if self.valid:
                raise ValueError("failed or invalidated checkpoint cannot be valid")
            if not self.invalidation_reason:
                raise ValueError(
                    "failed or invalidated checkpoint needs an invalidation reason"
                )
        if self.arm == "control" and self.retrieval is not None:
            raise ValueError("control checkpoint cannot contain retrieval metadata")
        return self


class ArmReceipt(_FrozenModel):
    receipt_type: Literal["arm"] = "arm"
    schema_version: Literal[1] = 1
    campaign_id: str = Field(min_length=1)
    pair_id: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    replicate: int = Field(ge=1)
    arm: Arm
    order_index: int = Field(ge=1, le=2)
    checkpoints: tuple[ArtifactRef, ...]
    valid: bool
    invalidation_reason: str | None
    retry_of: str | None


class PairReceipt(_FrozenModel):
    receipt_type: Literal["pair"] = "pair"
    schema_version: Literal[1] = 1
    campaign_id: str = Field(min_length=1)
    pair_id: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    replicate: int = Field(ge=1)
    order: tuple[Arm, Arm]
    control: ArmReceipt
    cymatix: ArmReceipt
    valid: bool
    invalidation_reason: str | None
    retry_of: str | None

    @model_validator(mode="after")
    def _validate_identity(self) -> "PairReceipt":
        if set(self.order) != {"control", "cymatix"}:
            raise ValueError("pair order must contain control and cymatix")
        for expected_arm, arm_receipt in (
            ("control", self.control),
            ("cymatix", self.cymatix),
        ):
            if arm_receipt.arm != expected_arm:
                raise ValueError("pair arm receipt is stored under the wrong arm")
            if (
                arm_receipt.campaign_id != self.campaign_id
                or arm_receipt.pair_id != self.pair_id
                or arm_receipt.problem != self.problem
                or arm_receipt.replicate != self.replicate
            ):
                raise ValueError("pair and arm receipt identities do not match")
        return self


ReceiptModel = CheckpointReceipt | ArmReceipt | PairReceipt


def _is_secret_key(key: str) -> bool:
    normalized = key.strip().casefold().replace("-", "_")
    return normalized in _SECRET_KEYS or normalized.endswith("_password")


def redact_secrets(value: Any) -> Any:
    """Return a shape-preserving copy with credential values removed."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _is_secret_key(str(key))
                else redact_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def _assert_secret_free(value: Any, *, path: str = "$receipt") -> None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if _is_secret_key(key_text):
                raise ReceiptIntegrityError(
                    f"credential field is prohibited at {path}.{key_text}"
                )
            _assert_secret_free(item, path=f"{path}.{key_text}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_secret_free(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        folded = value.casefold().replace("\\", "/")
        if "auth.json" in folded or value.startswith("sk-"):
            raise ReceiptIntegrityError(
                f"credential content is prohibited at {path}"
            )


def _receipt_data(receipt: ReceiptModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(receipt, BaseModel):
        data = receipt.model_dump(mode="json")
    elif isinstance(receipt, Mapping):
        data = dict(receipt)
    else:
        raise TypeError("receipt must be a receipt model or mapping")
    _assert_secret_free(data)
    receipt_type = data.get("receipt_type")
    model_type: type[BaseModel]
    if receipt_type == "checkpoint":
        model_type = CheckpointReceipt
    elif receipt_type == "arm":
        model_type = ArmReceipt
    elif receipt_type == "pair":
        model_type = PairReceipt
    else:
        raise ReceiptIntegrityError("unknown receipt type")
    try:
        return model_type.model_validate(data).model_dump(mode="json")
    except Exception as exc:
        log.warning("receipt schema validation failed", exc_info=True)
        raise ReceiptIntegrityError("receipt schema validation failed") from exc


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        log.warning("atomic receipt write failed: %s", target, exc_info=True)
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_receipt(
    target: Path,
    receipt: ReceiptModel | Mapping[str, Any],
) -> None:
    """Validate and atomically persist one canonical UTF-8 receipt."""
    data = _receipt_data(receipt)
    payload = (
        json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(target, payload)


def write_artifact(
    receipt_root: Path,
    category: str,
    content: str | bytes,
    *,
    media_type: str,
) -> ArtifactRef:
    """Persist content under its SHA-256 identity and return its receipt ref."""
    if not _ARTIFACT_CATEGORY.fullmatch(category):
        raise ValueError("artifact category must be a safe lowercase identifier")
    payload = content.encode("utf-8") if isinstance(content, str) else content
    digest = hashlib.sha256(payload).hexdigest()
    suffix = {
        "application/json": ".json",
        "application/jsonl": ".jsonl",
        "text/plain": ".txt",
    }.get(media_type, ".bin")
    relative = PurePosixPath("artifacts", category, f"{digest}{suffix}")
    target = receipt_root.joinpath(*relative.parts)
    if target.exists():
        existing = target.read_bytes()
        if existing != payload:
            raise ReceiptIntegrityError(
                f"content-addressed artifact collision: {relative.as_posix()}"
            )
    else:
        _atomic_write_bytes(target, payload)
    return ArtifactRef(
        path=relative.as_posix(),
        sha256=digest,
        size_bytes=len(payload),
        media_type=media_type,
    )


def _artifact_refs(value: Any) -> list[ArtifactRef]:
    refs: list[ArtifactRef] = []
    if isinstance(value, Mapping):
        keys = set(value)
        if {"path", "sha256", "size_bytes", "media_type"}.issubset(keys):
            refs.append(ArtifactRef.model_validate(value))
        else:
            for item in value.values():
                refs.extend(_artifact_refs(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            refs.extend(_artifact_refs(item))
    return refs


def _verify_artifact(root: Path, reference: ArtifactRef) -> bytes:
    candidate = root.joinpath(*PurePosixPath(reference.path).parts).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ReceiptIntegrityError(f"artifact is missing: {reference.path}")
    content = candidate.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != reference.sha256 or len(content) != reference.size_bytes:
        raise ReceiptIntegrityError(
            f"artifact hash mismatch: {reference.path}"
        )
    return content


def _verify_pair_symmetry(pair: PairReceipt) -> None:
    if len(pair.control.checkpoints) != len(pair.cymatix.checkpoints):
        raise ReceiptIntegrityError(
            f"paired checkpoint count mismatch for {pair.pair_id}"
        )
    if pair.control.order_index == pair.cymatix.order_index:
        raise ReceiptIntegrityError(f"paired checkpoint order mismatch: {pair.pair_id}")


def _symmetry_key(receipt: CheckpointReceipt) -> tuple[str, str, int, str, int]:
    return (
        receipt.campaign_id,
        receipt.pair_id,
        receipt.replicate,
        receipt.problem,
        receipt.checkpoint,
    )


def _verify_checkpoint_symmetry(receipts: list[CheckpointReceipt]) -> None:
    groups: dict[
        tuple[str, str, int, str, int],
        dict[Arm, CheckpointReceipt],
    ] = {}
    for receipt in receipts:
        arm_map = groups.setdefault(_symmetry_key(receipt), {})
        if receipt.arm in arm_map:
            raise ReceiptIntegrityError("duplicate terminal checkpoint receipt")
        arm_map[receipt.arm] = receipt

    symmetry_fields = (
        "config_hash",
        "cymatix_commit",
        "scbench_commit",
        "codex_version",
        "codex_model",
        "reasoning_effort",
        "image_digest",
        "auth_type",
    )
    for key, arm_map in groups.items():
        if set(arm_map) != {"control", "cymatix"}:
            continue
        control = arm_map["control"]
        treatment = arm_map["cymatix"]
        for field in symmetry_fields:
            if getattr(control, field) != getattr(treatment, field):
                raise ReceiptIntegrityError(
                    f"paired checkpoint version/config mismatch at {key}: {field}"
                )
        if (
            control.checkpoint == 1
            and control.prompt_artifact.sha256
            != treatment.prompt_artifact.sha256
        ):
            raise ReceiptIntegrityError(
                f"paired checkpoint 1 prompt mismatch at {key}"
            )


def verify_receipt_tree(receipt_root: Path) -> tuple[ReceiptModel, ...]:
    """Replay receipt schemas and every referenced artifact hash."""
    root = receipt_root.resolve(strict=True)
    if not root.is_dir():
        raise ReceiptIntegrityError("receipt root must be a directory")

    loaded: list[ReceiptModel] = []
    raw_by_receipt: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("invalid receipt JSON: %s", path, exc_info=True)
            raise ReceiptIntegrityError(f"invalid receipt JSON: {path}") from exc
        if not isinstance(raw, dict) or "receipt_type" not in raw:
            continue
        data = _receipt_data(raw)
        receipt_type = data["receipt_type"]
        if receipt_type == "checkpoint":
            receipt: ReceiptModel = CheckpointReceipt.model_validate(data)
        elif receipt_type == "arm":
            receipt = ArmReceipt.model_validate(data)
        else:
            receipt = PairReceipt.model_validate(data)
        loaded.append(receipt)
        raw_by_receipt.append(data)

    if not loaded:
        raise ReceiptIntegrityError("receipt tree contains no receipts")

    for receipt in loaded:
        if isinstance(receipt, PairReceipt):
            _verify_pair_symmetry(receipt)
    _verify_checkpoint_symmetry(
        [item for item in loaded if isinstance(item, CheckpointReceipt)]
    )

    for receipt, raw in zip(loaded, raw_by_receipt, strict=True):
        prompt_content: bytes | None = None
        for reference in _artifact_refs(raw):
            content = _verify_artifact(root, reference)
            if (
                isinstance(receipt, CheckpointReceipt)
                and reference == receipt.prompt_artifact
            ):
                prompt_content = content
        if isinstance(receipt, CheckpointReceipt) and receipt.arm == "control":
            if prompt_content is None:
                raise ReceiptIntegrityError("control prompt artifact is missing")
            normalized = prompt_content.decode("utf-8", errors="replace").casefold()
            if "<cymatix_context" in normalized:
                raise ReceiptIntegrityError(
                    "control prompt contains a Cymatix treatment block"
                )

    return tuple(loaded)

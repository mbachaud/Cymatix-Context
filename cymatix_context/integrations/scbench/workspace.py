"""Safe, deterministic workspace synchronization for SCBench problems."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from cymatix_context.cli.cmd_ingest import _content_type_for

from .config import SourcePolicy

log = logging.getLogger("cymatix.scbench.workspace")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRecord(_FrozenModel):
    source_id: str = Field(pattern=r"^repo://[^/]+/.+")
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    content_type: str
    content: str


class WorkspaceManifest(_FrozenModel):
    problem: str = Field(min_length=1)
    root: Path
    sources: dict[str, SourceRecord]

    def manifest_hash(self) -> str:
        records = [
            {
                "source_id": record.source_id,
                "relative_path": record.relative_path,
                "sha256": record.sha256,
                "size_bytes": record.size_bytes,
                "content_type": record.content_type,
            }
            for record in sorted(
                self.sources.values(),
                key=lambda item: item.source_id,
            )
        ]
        canonical = json.dumps(
            {"problem": self.problem, "sources": records},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


class WorkspaceDelta(_FrozenModel):
    created: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()


def _excluded(relative_path: Path, policy: SourcePolicy) -> bool:
    excluded_directories = {
        directory.casefold() for directory in policy.excluded_directories
    }
    if any(
        part.casefold() in excluded_directories
        for part in relative_path.parts[:-1]
    ):
        return True

    name = relative_path.name.casefold()
    excluded_names = {item.casefold() for item in policy.excluded_names}
    return name in excluded_names or name.startswith(".env.")


def scan_workspace(
    root: Path,
    problem: str,
    policy: SourcePolicy,
) -> WorkspaceManifest:
    """Return the sorted, agent-visible UTF-8 source manifest for *root*."""
    root_resolved = root.resolve(strict=True)
    if not root_resolved.is_dir():
        raise ValueError(f"workspace root is not a directory: {root}")
    if not problem or any(separator in problem for separator in ("/", "\\")):
        raise ValueError("problem must be a non-empty path-safe identifier")

    extensions = {extension.casefold() for extension in policy.extensions}
    records: list[SourceRecord] = []
    for candidate in root_resolved.rglob("*"):
        try:
            relative = candidate.relative_to(root_resolved)
            if _excluded(relative, policy):
                continue
            if candidate.suffix.casefold() not in extensions:
                continue

            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root_resolved) or not resolved.is_file():
                continue
            size_bytes = resolved.stat().st_size
            if size_bytes > policy.max_file_bytes:
                continue
            raw = resolved.read_bytes()
            if b"\x00" in raw:
                continue
            content = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            log.warning("excluding non-UTF-8 workspace source: %s", candidate)
            continue
        except Exception:
            log.warning("workspace source scan failed: %s", candidate, exc_info=True)
            continue

        relative_path = relative.as_posix()
        source_id = f"repo://{problem}/{relative_path}"
        records.append(
            SourceRecord(
                source_id=source_id,
                relative_path=relative_path,
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                content_type=_content_type_for(relative),
                content=content,
            )
        )

    sources = {
        record.source_id: record
        for record in sorted(records, key=lambda item: item.source_id)
    }
    return WorkspaceManifest(
        problem=problem,
        root=root_resolved,
        sources=sources,
    )


def diff_workspace(
    previous: WorkspaceManifest,
    current: WorkspaceManifest,
) -> WorkspaceDelta:
    if previous.problem != current.problem:
        raise ValueError("workspace manifests must describe the same problem")

    before_ids = set(previous.sources)
    after_ids = set(current.sources)
    shared = before_ids & after_ids
    return WorkspaceDelta(
        created=tuple(sorted(after_ids - before_ids)),
        changed=tuple(
            sorted(
                source_id
                for source_id in shared
                if previous.sources[source_id].sha256
                != current.sources[source_id].sha256
            )
        ),
        deleted=tuple(sorted(before_ids - after_ids)),
        unchanged=tuple(
            sorted(
                source_id
                for source_id in shared
                if previous.sources[source_id].sha256
                == current.sources[source_id].sha256
            )
        ),
    )


def resolve_source_id(root: Path, source_id: str) -> Path | None:
    """Resolve a live source for the problem named by the workspace root."""
    root_resolved = root.resolve(strict=True)
    prefix = f"repo://{root_resolved.name}/"
    normalized = source_id.replace("\\", "/")
    if not normalized.startswith(prefix):
        return None

    relative = normalized[len(prefix):]
    parts = relative.split("/")
    if not relative or any(part in {"", ".", ".."} for part in parts):
        return None
    try:
        candidate = root_resolved.joinpath(*parts).resolve(strict=True)
    except Exception:
        log.warning("workspace source is not live: %s", source_id)
        return None
    if not candidate.is_relative_to(root_resolved) or not candidate.is_file():
        return None
    return candidate

"""Vault writer — atomic file writes + gene markdown rendering.

Atomic writes use a tmp+rename pattern with a vault-root sentinel so that any
external file watcher (in v1.1, our own watcher) can suppress events for
helix-side writes.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from helix_context.vault.schema import authored_placeholders

log = logging.getLogger(__name__)

SENTINEL_FILENAME = ".helix-syncing"


def write_atomic(*, vault_root: Path, target: Path, content: str) -> None:
    """Write `content` to `target` atomically.

    1. Write to target.tmp
    2. Touch sentinel
    3. os.replace(tmp, target)
    4. Remove sentinel

    Caller is responsible for holding the vault-root lock.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_suffix(target.suffix + ".tmp")
    sentinel = Path(vault_root) / SENTINEL_FILENAME

    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    sentinel.touch(exist_ok=True)
    try:
        os.replace(tmp, target)
    finally:
        try:
            sentinel.unlink()
        except FileNotFoundError:
            pass


def compute_disk_hash(path: Path) -> str:
    """SHA-256 of full file content. Used as the v1.1 self-event sentinel."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_frontmatter(gene: Any) -> dict:
    fm: dict = {}
    fm["gene_id"] = gene.gene_id
    fm["chromatin"] = getattr(gene, "chromatin", "euchromatin")
    fm["domains"] = list(getattr(gene, "domains", []) or [])
    fm["content_type"] = getattr(gene, "content_type", "code")
    fm["source_id"] = getattr(gene, "source_id", "")
    fm["source_lines"] = getattr(gene, "source_lines", "")
    fm["content_sha256"] = getattr(gene, "content_sha256", "")
    fm["last_seen"] = getattr(gene, "last_seen", "")
    fm["last_seen_ts"] = float(getattr(gene, "last_seen_ts", 0.0) or 0.0)
    fm["live_truth_score"] = float(getattr(gene, "live_truth_score", 0.0) or 0.0)
    fm["co_activation_partners"] = int(getattr(gene, "co_activation_partners", 0) or 0)
    fm["party_id"] = getattr(gene, "party_id", "")
    fm["participant_handle"] = getattr(gene, "participant_handle", "")
    fm.update(authored_placeholders())
    return fm


def _build_body(gene: Any, *, redact_body: bool) -> str:
    title = f"# {gene.source_id}"
    if getattr(gene, "source_lines", ""):
        title += f":{gene.source_lines}"

    if redact_body:
        body_sha = getattr(gene, "content_sha256", "")[:16]
        body_section = (
            f"```\n[redacted body — sha256={body_sha}]\n```"
        )
    else:
        lang = "python" if (gene.content_type == "code" and gene.source_id.endswith(".py")) else ""
        body_section = f"```{lang}\n{gene.content or ''}\n```"

    typed_edges = (
        "## Typed edges\n\n"
        "*(none yet — v1 ships read-only; v1.1 enables operator-authored "
        "supersedes / contradicts / implements / documented_by / tests)*"
    )

    backlinks = "## Backlinks\n\n*(populated by Obsidian)*"

    return "\n\n".join([title, body_section, typed_edges, backlinks])


def render_gene_markdown(gene: Any, *, redact_body: bool) -> str:
    """Render a Gene to a complete markdown document (frontmatter + body)."""
    fm = _build_frontmatter(gene)
    fm_yaml = yaml.safe_dump(fm, sort_keys=True, allow_unicode=True)
    body = _build_body(gene, redact_body=redact_body)
    return f"---\n{fm_yaml}---\n\n{body}\n"

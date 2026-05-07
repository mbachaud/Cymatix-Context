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

from helix_context.vault.schema import authored_placeholders, safe_resolve_under

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
    vault_root = Path(vault_root)
    target = safe_resolve_under(vault_root, target)  # raises ValueError if escapes
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_suffix(target.suffix + ".tmp")
    sentinel = vault_root / SENTINEL_FILENAME

    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    sentinel.touch(exist_ok=True)
    try:
        os.replace(tmp, target)
    except OSError:
        log.warning("write_atomic: os.replace failed for %s", target, exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            log.warning("write_atomic: failed to clean up tmp file %s", tmp, exc_info=True)
        raise
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
    fm["last_seen"] = getattr(gene, "last_seen", None) or None
    fm["last_seen_ts"] = float(getattr(gene, "last_seen_ts", 0.0) or 0.0)
    fm["live_truth_score"] = float(getattr(gene, "live_truth_score", 0.0) or 0.0)
    fm["co_activation_partners"] = int(getattr(gene, "co_activation_partners", 0) or 0)
    fm["party_id"] = getattr(gene, "party_id", "")
    fm["participant_handle"] = getattr(gene, "participant_handle", "")
    fm.update(authored_placeholders())
    return fm


def _build_body(gene: Any, *, redact_body: bool) -> str:
    source_id = getattr(gene, "source_id", "")
    source_lines = getattr(gene, "source_lines", "")
    content_type = getattr(gene, "content_type", "code")
    content = getattr(gene, "content", "") or ""

    title = f"# {source_id}"
    if source_lines:
        title += f":{source_lines}"

    if redact_body:
        body_sha = getattr(gene, "content_sha256", "")[:16]
        body_section = f"```\n[redacted body — sha256={body_sha}]\n```"
    else:
        lang = "python" if (content_type == "code" and source_id.endswith(".py")) else ""
        body_section = f"```{lang}\n{content}\n```"

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
    fm_yaml = yaml.safe_dump(fm, sort_keys=True, allow_unicode=True, default_flow_style=False)
    body = _build_body(gene, redact_body=redact_body)
    return f"---\n{fm_yaml}---\n\n{body}\n"


def render_trace_markdown(
    *,
    request_id: str,
    created_at: str,
    expires_at: str,
    pinned: bool,
    trigger_reason: str,
    total_latency_ms: int,
    health_status: str,
    stage_timing_ms: dict,
    fingerprint_route: str,
    foveated_ranks: str,
    final_genes: list,  # list of (filename_stem, rank, score)
) -> str:
    """Render a /context call trace to markdown.

    The trace export feeds Goal 2 (diagnostic console). Filename includes
    the expires_at unix epoch so the pruner can filter expired traces by
    name without parsing frontmatter.
    """
    fm = {
        "request_id": request_id,
        "created_at": created_at,
        "expires_at": expires_at,
        "pinned": pinned,
        "trigger_reason": trigger_reason,
        "total_latency_ms": total_latency_ms,
        "health_status": health_status,
    }
    fm_yaml = yaml.safe_dump(fm, sort_keys=True, allow_unicode=True, default_flow_style=False)

    title = f"# Trace: {request_id}"

    stage_rows = "\n".join(f"| {s} | {ms} |" for s, ms in stage_timing_ms.items())
    stage_section = (
        "## Per-stage timing\n\n"
        + ("| stage | ms |\n|---|---|\n" + stage_rows
           if stage_rows
           else "*(no per-stage data)*")
    )

    fp_section = "## Fingerprint route\n\n" + (fingerprint_route or "*(none)*")
    fov_section = "## Foveated rank assignments\n\n" + (foveated_ranks or "*(none)*")

    if final_genes:
        gene_lines_list = []
        for (stem, rank, score) in final_genes:
            # Guard score against None / NaN — diagnostic data may be incomplete.
            safe_score = score if (score is not None and score == score) else 0.0
            gene_lines_list.append(f"- [[{stem}]] (rank {rank}, score {safe_score:.2f})")
        gene_lines = "\n".join(gene_lines_list)
    else:
        gene_lines = "*(no genes returned)*"
    final_section = "## Final budget genes\n\n" + gene_lines

    body = "\n\n".join([title, stage_section, fp_section, fov_section, final_section])
    return f"---\n{fm_yaml}---\n\n{body}\n"

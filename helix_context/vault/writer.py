"""Vault writer — atomic file writes + gene markdown rendering.

Atomic writes use a tmp+rename pattern with a vault-root sentinel so that any
external file watcher (in v1.1, our own watcher) can suppress events for
helix-side writes.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import yaml

from helix_context.vault.schema import authored_placeholders, derive_gene_relpath, safe_resolve_under

if TYPE_CHECKING:
    from helix_context.vault.locking import VaultLock
    from helix_context.vault.state import VaultState

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


def _row_to_gene(row: Any) -> Any:
    """Adapt a sqlite3.Row to the SimpleNamespace shape that ``_build_frontmatter`` reads.

    Maps the actual genome schema (gene_attribution.participant_id, content_hash,
    promoter JSON blob) to the gene shape used by render_gene_markdown.
    """
    import json
    from types import SimpleNamespace

    # Domains live inside the promoter JSON blob as promoter.domains
    domains: list = []
    promoter_raw = row["promoter"]
    if promoter_raw:
        try:
            promoter_obj = json.loads(promoter_raw)
            domains = list(promoter_obj.get("domains") or [])
        except (json.JSONDecodeError, TypeError):
            domains = []

    # Map chromatin int → string (genes table stores int via int(ChromatinState))
    chromatin_int = row["chromatin"]
    chromatin_str = {0: "open", 1: "euchromatin", 2: "heterochromatin"}.get(
        chromatin_int, "open"
    )

    # Derive content_type from source_id extension (best-effort)
    source_id = row["source_id"] or ""
    if source_id.endswith((".md", ".rst", ".txt")):
        content_type = "doc"
    else:
        content_type = "code"

    # last_seen_ts: prefer last_seen if it's numeric, else fall back to mtime
    last_seen_raw = row["last_seen"]
    if isinstance(last_seen_raw, (int, float)):
        last_seen_ts = float(last_seen_raw)
    elif row["mtime"] is not None:
        last_seen_ts = float(row["mtime"])
    else:
        last_seen_ts = 0.0

    return SimpleNamespace(
        gene_id=row["gene_id"],
        content=row["content"] or "",
        content_type=content_type,
        source_id=source_id,
        source_lines="",  # not in genes table; v1.1 may add a column
        domains=domains,
        chromatin=chromatin_str,
        content_sha256=row["content_hash"] or "",
        last_seen="",  # ISO string version not stored; leave empty (renders as null in YAML)
        last_seen_ts=last_seen_ts,
        live_truth_score=0.0,  # not tracked in genes table v1
        co_activation_partners=0,  # YAGNI for v1
        party_id=row["party_id"] or "",
        participant_handle=row["participant_id"] or "",  # participant_id → participant_handle in vault
    )


def full_export(
    *,
    genome: Any,
    state: "VaultState",
    lock: "VaultLock",
    vault_root: Path,
    party_id: Optional[str],
    redact_body: bool,
    fan_out_threshold: int,
    batch_size: int = 500,
) -> dict:
    """Export all (or party-filtered) genes from genome to vault_root.

    Returns a dict with keys: genes_exported, elapsed_seconds, errors.

    Acquires the vault lock for the entire export. Each row is processed
    inside a try/except so a malformed row does not abort the whole export.
    """
    # Lazy imports to avoid circular import at module load time.
    from helix_context.vault.locking import VaultLock  # noqa: F811
    from helix_context.vault.state import VaultState  # noqa: F811

    vault_root = Path(vault_root)
    t_start = time.monotonic()
    genes_exported = 0
    errors = 0

    # fan_out_threshold is accepted now for forward compat with Task 13's
    # eager fan-out migration; v1 full_export does not yet split by domain count.
    del fan_out_threshold

    sql = (
        "SELECT g.gene_id, g.content, g.source_id, g.chromatin, "
        "g.content_hash, g.last_seen, g.promoter, g.mtime, "
        "ga.party_id, ga.participant_id "
        "FROM genes g LEFT JOIN gene_attribution ga ON g.gene_id = ga.gene_id"
    )
    if party_id:
        sql += " WHERE ga.party_id = ?"
        params: tuple = (party_id,)
    else:
        params = ()

    with lock:
        # Use read_conn if available (prefers WAL reader); fall back to .conn.
        # The cursor is opened inside the lock so the lock spans the full read
        # snapshot as well as all writes — prevents a concurrent vault mutation
        # from racing the cursor between fetchmany calls.
        conn = getattr(genome, "read_conn", None) or genome.conn
        cur = conn.execute(sql, params)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                try:
                    gene = _row_to_gene(row)
                    # Pick first domain (if any) for directory bucketing
                    domain = gene.domains[0] if gene.domains else None
                    relpath = derive_gene_relpath(
                        domain=domain,
                        source_id=gene.source_id,
                        gene_id=gene.gene_id,
                    )
                    target = vault_root / relpath
                    markdown = render_gene_markdown(gene, redact_body=redact_body)
                    write_atomic(vault_root=vault_root, target=target, content=markdown)
                    disk_hash = compute_disk_hash(target)
                    state.upsert_record(
                        gene_id=gene.gene_id,
                        path=relpath,
                        ts=time.time(),
                        disk_hash=disk_hash,
                    )
                    genes_exported += 1
                except ValueError as exc:
                    # Data-level error: path traversal, malformed domain, bad
                    # JSON in promoter, etc. — skip this gene and continue.
                    log.warning(
                        "full_export: export skipped for gene %s: %s",
                        row["gene_id"] if row["gene_id"] else "<unknown>",
                        exc,
                    )
                    errors += 1
                except (KeyError, json.JSONDecodeError) as exc:
                    log.warning(
                        "full_export: export skipped for gene %s: %s",
                        row["gene_id"] if row["gene_id"] else "<unknown>",
                        exc,
                    )
                    errors += 1
                except OSError as exc:
                    # I/O failure during write_atomic — log and continue, don't
                    # abort the whole export run.
                    log.warning(
                        "full_export: export I/O failure for gene %s: %s",
                        row["gene_id"] if row["gene_id"] else "<unknown>",
                        exc,
                    )
                    errors += 1
                # Note: AttributeError, TypeError, NameError propagate up —
                # they indicate code bugs and should fail fast.

        # Update top-level vault state
        try:
            state.update_top_level_state(
                last_full_export_ts=time.time(),
                exported_gene_count=genes_exported,
            )
        except Exception:
            log.warning("full_export: failed to update top-level state", exc_info=True)

    elapsed = time.monotonic() - t_start
    return {
        "genes_exported": genes_exported,
        "elapsed_seconds": elapsed,
        "errors": errors,
    }


def incremental_export(
    *,
    genome: Any,
    state: "VaultState",
    lock: "VaultLock",
    vault_root: Path,
    party_id: Optional[str],
    redact_body: bool,
    fan_out_threshold: int,
    since_ts: float,
    batch_size: int = 500,
) -> dict:
    """Export only genes whose last_seen > since_ts from genome to vault_root.

    Uses the idx_genes_last_seen index for efficient range filtering.
    Returns a dict with keys: genes_exported, elapsed_seconds, errors.

    Acquires the vault lock for the entire export. Each row is processed
    inside a try/except so a malformed row does not abort the whole export.
    """
    # Lazy imports to avoid circular import at module load time.
    from helix_context.vault.locking import VaultLock  # noqa: F811
    from helix_context.vault.state import VaultState  # noqa: F811

    vault_root = Path(vault_root)
    t_start = time.monotonic()
    genes_exported = 0
    errors = 0

    # fan_out_threshold is accepted for forward compat; v1 does not split by domain count.
    del fan_out_threshold

    sql = (
        "SELECT g.gene_id, g.content, g.source_id, g.chromatin, "
        "g.content_hash, g.last_seen, g.promoter, g.mtime, "
        "ga.party_id, ga.participant_id "
        "FROM genes g LEFT JOIN gene_attribution ga ON g.gene_id = ga.gene_id "
        "WHERE g.last_seen > ?"
    )
    params: list = [since_ts]
    if party_id:
        sql += " AND ga.party_id = ?"
        params.append(party_id)

    with lock:
        conn = getattr(genome, "read_conn", None) or genome.conn
        cur = conn.execute(sql, params)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                try:
                    gene = _row_to_gene(row)
                    domain = gene.domains[0] if gene.domains else None
                    relpath = derive_gene_relpath(
                        domain=domain,
                        source_id=gene.source_id,
                        gene_id=gene.gene_id,
                    )
                    target = vault_root / relpath
                    markdown = render_gene_markdown(gene, redact_body=redact_body)
                    write_atomic(vault_root=vault_root, target=target, content=markdown)
                    disk_hash = compute_disk_hash(target)
                    state.upsert_record(
                        gene_id=gene.gene_id,
                        path=relpath,
                        ts=time.time(),
                        disk_hash=disk_hash,
                    )
                    genes_exported += 1
                except ValueError as exc:
                    log.warning(
                        "incremental_export: export skipped for gene %s: %s",
                        row["gene_id"] if row["gene_id"] else "<unknown>",
                        exc,
                    )
                    errors += 1
                except (KeyError, json.JSONDecodeError) as exc:
                    log.warning(
                        "incremental_export: export skipped for gene %s: %s",
                        row["gene_id"] if row["gene_id"] else "<unknown>",
                        exc,
                    )
                    errors += 1
                except OSError as exc:
                    log.warning(
                        "incremental_export: export I/O failure for gene %s: %s",
                        row["gene_id"] if row["gene_id"] else "<unknown>",
                        exc,
                    )
                    errors += 1
                # Note: AttributeError, TypeError, NameError propagate up —
                # they indicate code bugs and should fail fast.

        # Update top-level vault state
        try:
            state.update_top_level_state(
                last_incremental_export_ts=time.time(),
            )
        except Exception:
            log.warning("incremental_export: failed to update top-level state", exc_info=True)

    elapsed = time.monotonic() - t_start
    return {
        "genes_exported": genes_exported,
        "elapsed_seconds": elapsed,
        "errors": errors,
    }


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


def trace_export(
    *,
    vault_root: Path,
    lock: "VaultLock",
    request_id: str,
    trigger_reason: str,
    total_latency_ms: int,
    health_status: str,
    stage_timing_ms: dict,
    fingerprint_route: str,
    foveated_ranks: str,
    final_genes: list,
    retention_hours: int,
) -> Path:
    """Export a /context call trace to _traces/<ts>_<id>_exp<unix>.md.

    Filename encodes expires_at as `_exp<unix-epoch>` so the pruner can
    filter expired traces by name without parsing frontmatter.
    """
    now_ts = time.time()
    expires_unix = int(now_ts + retention_hours * 3600)

    # Use timezone-aware UTC; .replace(tzinfo=None) for strftime portability.
    created_dt = _dt.datetime.fromtimestamp(now_ts, tz=_dt.timezone.utc)
    expires_dt = _dt.datetime.fromtimestamp(expires_unix, tz=_dt.timezone.utc)

    fname = (
        f"{created_dt.strftime('%Y-%m-%dT%H-%M-%S')}_"
        f"{request_id}_exp{expires_unix}.md"
    )
    target = vault_root / "_traces" / fname

    md = render_trace_markdown(
        request_id=request_id,
        created_at=created_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        pinned=False,
        trigger_reason=trigger_reason,
        total_latency_ms=total_latency_ms,
        health_status=health_status,
        stage_timing_ms=stage_timing_ms,
        fingerprint_route=fingerprint_route,
        foveated_ranks=foveated_ranks,
        final_genes=final_genes,
    )

    with lock:
        write_atomic(vault_root=vault_root, target=target, content=md)
    return target

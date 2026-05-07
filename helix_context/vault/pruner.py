"""Vault pruner — TTL-based trace deletion + pre-prune rollup + _stale/ refresh.

Pruning uses filename-encoded `_exp<unix-epoch>` suffix to avoid YAML parsing
unexpired traces. The hard-cap force-prune handles pinned files past a
compliance retention window.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import yaml as _yaml

from helix_context.schemas import ChromatinState
from helix_context.telemetry import vault_pruner_histogram, vault_force_prune_counter

log = logging.getLogger(__name__)

_TRACE_EXP_RE = re.compile(r"_exp(\d+)\.md$")
_FALLBACK_MTIME_HOURS = 24
_HARD_FALLBACK_DAYS = 30  # corrupt-filename traces older than this are pruned


def prune_traces(
    *,
    vault_root: Path,
    max_retention_hours_hard: int,
    rollup_enabled: bool,
    rollup_shard: str,
) -> dict:
    """Walk _traces/ and _traces-pinned/, prune expired and force-pruned.

    Returns: {pruned_count, force_pruned_count, rollup_appended, errors}
    """
    pruned = 0
    force_pruned = 0
    rollup_appended = 0
    errors = 0

    now = time.time()
    _t0 = time.monotonic()
    traces_dir = vault_root / "_traces"
    pinned_dir = vault_root / "_traces-pinned"

    if traces_dir.exists():
        for entry in traces_dir.iterdir():
            if not entry.is_file() or not entry.name.endswith(".md"):
                continue
            try:
                expires_unix = _parse_expiry_from_filename(entry)
                if expires_unix is None:
                    mtime = entry.stat().st_mtime
                    if (now - mtime) > _HARD_FALLBACK_DAYS * 86400:
                        if rollup_enabled:
                            _append_rollup(entry, vault_root, rollup_shard)
                            rollup_appended += 1
                        entry.unlink()
                        pruned += 1
                    continue
                if expires_unix < now:
                    if rollup_enabled:
                        _append_rollup(entry, vault_root, rollup_shard)
                        rollup_appended += 1
                    entry.unlink()
                    pruned += 1
            except OSError:
                log.warning("prune failed for %s", entry, exc_info=True)
                errors += 1

    if pinned_dir.exists() and max_retention_hours_hard > 0:
        cutoff = now - max_retention_hours_hard * 3600
        for entry in pinned_dir.iterdir():
            if not entry.is_file() or not entry.name.endswith(".md"):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    log.warning(
                        "vault_force_prune: %s past max_retention_hours_hard=%dh",
                        entry.name, max_retention_hours_hard,
                    )
                    if rollup_enabled:
                        _append_rollup(entry, vault_root, rollup_shard)
                        rollup_appended += 1
                    entry.unlink()
                    force_pruned += 1
            except OSError:
                log.warning("force-prune failed for %s", entry, exc_info=True)
                errors += 1

    try:
        vault_pruner_histogram().record(time.monotonic() - _t0, {})
        if force_pruned > 0:
            vault_force_prune_counter().add(force_pruned, {"reason": "max_retention_hard"})
    except Exception:
        pass
    return {
        "pruned_count": pruned,
        "force_pruned_count": force_pruned,
        "rollup_appended": rollup_appended,
        "errors": errors,
    }


def _parse_expiry_from_filename(path: Path) -> Optional[float]:
    m = _TRACE_EXP_RE.search(path.name)
    if not m:
        return None
    return float(m.group(1))


_ROLLUP_HEADER = (
    "# Trace rollup\n\n"
    "| time | request_id | latency_ms | health | trigger |\n"
    "|---|---|---|---|---|\n"
)


def _append_rollup(trace_path: Path, vault_root: Path, shard: str) -> None:
    """Append a one-line summary of `trace_path` to the appropriate rollup file."""
    fm = _read_trace_frontmatter(trace_path)
    if fm is None:
        return

    created_at = str(fm.get("created_at", ""))
    request_id = str(fm.get("request_id", "?"))
    latency = fm.get("total_latency_ms", "?")
    health = str(fm.get("health_status", "?"))
    trigger = str(fm.get("trigger_reason", "?"))

    # Parse the date for shard path. Expected format: YYYY-MM-DDTHH:MM:SSZ
    date_part = created_at[:10] if len(created_at) >= 10 else "unknown"
    hour_part = created_at[11:13] if len(created_at) >= 13 else "00"
    time_part = created_at[11:19] if len(created_at) >= 19 else created_at

    rollups_root = vault_root / "_meta" / "trace-rollups"
    if shard == "hour":
        rollup_path = rollups_root / date_part / f"{hour_part}.md"
    else:  # daily
        rollup_path = rollups_root / f"{date_part}.md"

    rollup_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not rollup_path.exists():
        rollup_path.write_text(_ROLLUP_HEADER, encoding="utf-8")

    new_row = f"| {time_part} | {request_id} | {latency} | {health} | {trigger} |\n"
    with rollup_path.open("a", encoding="utf-8") as f:
        f.write(new_row)


def _read_trace_frontmatter(path: Path) -> Optional[dict]:
    """Parse the YAML frontmatter block from a trace file."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("could not read trace %s", path, exc_info=True)
        return None
    if not content.startswith("---\n"):
        return None
    rest = content[len("---\n"):]
    try:
        end = rest.index("---\n")
    except ValueError:
        return None
    try:
        return _yaml.safe_load(rest[:end]) or {}
    except _yaml.YAMLError:
        log.warning("could not parse frontmatter in %s", path, exc_info=True)
        return None


def refresh_stale_view(
    *,
    vault_root: Path,
    genome,
    stale_threshold: float,
    party_id: str,
) -> dict:
    """Repopulate the _stale/ folder based on live_truth_score.

    v1: pointer notes on all platforms (containing [[gene-<id>]] wikilink).

    TODO(v1.1): replace pointer notes with symlinks on POSIX for live updates;
    Obsidian renders both fine but symlinks reflect content changes immediately.
    """
    from helix_context.vault.schema import derive_gene_filename

    stale_dir = vault_root / "_stale"
    stale_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    sql = (
        "SELECT g.gene_id, g.source_id "
        "FROM genes g LEFT JOIN gene_attribution ga ON g.gene_id = ga.gene_id "
        "WHERE g.live_truth_score < ? AND g.chromatin = ?"
    )
    params: list = [stale_threshold, int(ChromatinState.EUCHROMATIN)]
    if party_id:
        sql += " AND ga.party_id = ?"
        params.append(party_id)

    expected_filenames: set[str] = set()
    added = 0
    errors = 0

    conn = getattr(genome, "read_conn", None) or genome.conn
    for row in conn.execute(sql, params):
        gene_id = row["gene_id"]
        source_id = row["source_id"] or ""
        stale_name = derive_gene_filename(source_id, gene_id)
        expected_filenames.add(stale_name)
        target = stale_dir / stale_name
        if target.exists():
            continue
        try:
            link_text = f"[[{Path(stale_name).stem}]]"
            target.write_text(
                f"# Stale (live_truth_score < {stale_threshold})\n\n{link_text}\n",
                encoding="utf-8",
            )
            added += 1
        except OSError:
            log.warning("stale view write failed for %s", gene_id, exc_info=True)
            errors += 1

    removed = 0
    for entry in list(stale_dir.iterdir()):
        if entry.is_file() and entry.name.endswith(".md") and entry.name not in expected_filenames:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                errors += 1

    return {"added": added, "removed": removed, "errors": errors}


def migrate_fan_out_if_needed(
    *,
    vault_root: Path,
    state,
    fan_out_threshold: int,
) -> dict:
    """Migrate flat domain folders past the threshold to 2-level fan-out.

    Eager — fires the moment a flat folder crosses the threshold. Updates
    state.vault_path for each migrated gene to keep wikilinks coherent.
    """
    genes_root = vault_root / "genes"
    if not genes_root.exists():
        return {"migrated_domains": [], "files_migrated": 0}

    # Pre-build path → record dict for O(1) lookups during migration
    records_by_path = {rec.vault_path: rec for rec in state.iter_records()}

    migrated_domains: list = []
    files_migrated = 0
    for domain_dir in genes_root.iterdir():
        if not domain_dir.is_dir() or domain_dir.name.startswith("_"):
            continue
        flat_files = [p for p in domain_dir.iterdir() if p.is_file() and p.suffix == ".md"]
        if len(flat_files) <= fan_out_threshold:
            continue
        log.info(
            "migrating fan-out for domain=%s (%d files)", domain_dir.name, len(flat_files)
        )
        for f in flat_files:
            stem = f.stem
            try:
                short_id = stem.rsplit("-", 1)[1]
            except IndexError:
                continue
            first2 = short_id[:2]
            new_dir = domain_dir / first2
            new_dir.mkdir(exist_ok=True, mode=0o700)
            new_path = new_dir / f.name
            os.replace(f, new_path)
            relpath_old = f"genes/{domain_dir.name}/{f.name}"
            relpath_new = f"genes/{domain_dir.name}/{first2}/{f.name}"
            rec = records_by_path.get(relpath_old)
            if rec is not None:
                state.upsert_record(
                    gene_id=rec.gene_id,
                    path=relpath_new,
                    ts=rec.last_exported_ts,
                    disk_hash=rec.last_exported_disk_hash,
                )
            files_migrated += 1
        migrated_domains.append(domain_dir.name)

    if migrated_domains:
        top = state.read_top_level_state()
        engaged = set(top.get("fan_out_engaged_domains", []))
        engaged.update(migrated_domains)
        state.update_top_level_state(fan_out_engaged_domains=sorted(engaged))

    return {"migrated_domains": migrated_domains, "files_migrated": files_migrated}

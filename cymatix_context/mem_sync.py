"""
Auto-memory → helix sync.

Watches Claude Code auto-memory directories and ingests each `.md` file
as a document. Persona/agent attribution uses helix's existing 4-layer
federation (HELIX_USER / HELIX_AGENT / HELIX_DEVICE / HELIX_ORG) — the
syncer forwards whichever of those env vars are set in ITS process on
every /ingest call. (The server is a separate process and resolves any
missing fields from its own env, so without forwarding, provenance would
silently fall back to the server's identity.)

Why: before mem_sync, nothing from live sessions reached the knowledge store.
Every conversation summary and feedback note was trapped in file-based
memory, invisible to retrieval. With sync, Raude writing
`feedback_pwpc.md` in one session becomes discoverable by Laude via
normal `/context` queries — handoff files stop being a coordination
bottleneck.

Sync model:
    - Poll each watched dir every `sync_interval_s` seconds
    - Hash each .md file; compare against last-known hash
    - Ingest new / changed → POST to /ingest with source_id = "mem://{path}"
    - Deleted files → POST /admin/genes/tombstone (lifecycle tier=2,
      excluded from hot-tier retrieval). Deletion detection is scoped to
      the watch dirs actually scanned this pass — the state file is shared
      by every syncer on the machine, so entries belonging to another
      agent's watch-set must survive untouched.
    - MEMORY.md itself is skipped (index only, churn heavy)

Opt-out: any memory file with `private: true` in frontmatter is skipped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("helix.mem_sync")


# ── Frontmatter parsing (no yaml dep) ────────────────────────────────
# We only need three fields: `name`, `description`, `type`. Anything
# exotic (nested structures, multi-line values) we treat as "body" and
# let the ingest pipeline handle it. Keep it simple.

def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Split `---\n…\n---\n<body>`. Returns (fields, body).

    Returns ({}, text) if no frontmatter present — valid case, treat the
    whole file as body.
    """
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        end = text.find("\r\n---\r\n", 4)
        if end < 0:
            return {}, text
    raw = text[4:end].replace("\r\n", "\n")
    body_start = text.find("\n", end + 4) + 1
    body = text[body_start:]
    fields: Dict[str, str] = {}
    for line in raw.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        fields[k.strip()] = v.strip()
    return fields, body


# ── State tracking ───────────────────────────────────────────────────
# We stash {path: sha256} in a JSON file next to the syncer so restarts
# don't re-ingest unchanged files. Lives at ~/.helix/mem_sync_state.json
# by default. Purely cache — safe to delete (forces full re-sync).

def _state_path() -> Path:
    home = Path(os.path.expanduser("~")) / ".helix"
    home.mkdir(parents=True, exist_ok=True)
    return home / "mem_sync_state.json"


def _load_state() -> Dict[str, str]:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        log.warning("mem_sync state file corrupt — starting fresh", exc_info=True)
        return {}


def _save_state(state: Dict[str, str]) -> None:
    try:
        _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        log.warning("failed to persist mem_sync state", exc_info=True)


# ── Ingestion ────────────────────────────────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _infer_content_type(path: Path, fields: Dict[str, str]) -> str:
    """Map memory file → ingestion content_type.

    Memory files are always semi-structured prose. We use "markdown"
    so the tree_chunker treats them as narrative rather than code.
    """
    return "markdown"


def _ingest_file(
    helix_url: str,
    path: Path,
    content: str,
    fields: Dict[str, str],
    agent_kind: Optional[str] = None,
) -> Optional[List[str]]:
    """POST one memory file to /ingest. Returns gene_ids or None on error."""
    source_id = f"mem://{path.name}"
    metadata = {
        "source_id": source_id,
        "source": "auto-memory",
        "path": str(path),
        "mem_type": fields.get("type", "unknown"),
        "mem_name": fields.get("name", path.stem),
        "mem_description": fields.get("description", ""),
        "ingested_at": int(time.time()),
    }
    payload: Dict = {
        "content": content,
        "content_type": _infer_content_type(path, fields),
        "metadata": metadata,
    }
    if agent_kind:
        payload["agent_kind"] = agent_kind

    # Forward the syncer's 4-layer identity explicitly. The server resolves
    # missing fields from its OWN process env — the syncer runs as a
    # separate process, so without forwarding, provenance would fall back
    # to the server's identity instead of the agent that wrote the memory.
    identity = {
        "participant_handle": os.environ.get("HELIX_USER"),
        "agent_handle": os.environ.get("HELIX_AGENT"),
        "party_id": os.environ.get("HELIX_DEVICE") or os.environ.get("HELIX_PARTY"),
        "org_id": os.environ.get("HELIX_ORG"),
    }
    for field, value in identity.items():
        if value:
            payload[field] = value

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{helix_url.rstrip('/')}/ingest",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("gene_ids", [])
    except urllib.error.HTTPError as exc:
        log.warning("mem_sync ingest HTTP %s for %s: %s",
                    exc.code, path.name, exc.read()[:200])
    except Exception:
        log.warning("mem_sync ingest failed for %s", path.name, exc_info=True)
    return None


def _tombstone_file(helix_url: str, path: Path) -> bool:
    """Mark a removed memory's documents as heterochromatin (retrieval-excluded).

    POSTs /admin/genes/tombstone with each source_id the file may have
    been ingested under: the absolute path first (``metadata["path"]``
    wins over ``metadata["source_id"]`` at ingest, so that is what the
    gene actually stored), then the legacy ``mem://{name}`` alias.
    Non-fatal — returns True iff any genes were demoted.
    """
    candidates = [str(path), f"mem://{path.name}"]
    for source_id in candidates:
        try:
            req = urllib.request.Request(
                f"{helix_url.rstrip('/')}/admin/genes/tombstone",
                data=json.dumps({"source_id": source_id}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("tombstoned", 0) > 0:
                log.info("mem_sync tombstoned %d gene(s) for %s (source_id=%s)",
                         body["tombstoned"], path.name, source_id)
                return True
        except urllib.error.HTTPError as exc:
            # Pre-tombstone-route server — drop from local state so we
            # stop tracking it, but leave the documents alone server-side.
            log.warning("mem_sync tombstone HTTP %s for %s (source_id=%s) — "
                        "documents stay live server-side",
                        exc.code, path.name, source_id)
        except Exception:
            log.warning("mem_sync tombstone failed for %s", path.name,
                        exc_info=True)
    log.info("mem_sync tombstone: no genes matched for %s", path.name)
    return False


# ── Scanner ──────────────────────────────────────────────────────────

def _is_skipped(path: Path, fields: Dict[str, str]) -> Optional[str]:
    """Return a reason string if the file should be skipped, else None."""
    if path.name == "MEMORY.md":
        return "index-file"
    if fields.get("private", "").lower() == "true":
        return "private-frontmatter"
    if path.stat().st_size == 0:
        return "empty-file"
    return None


def sync_once(
    watch_dirs: List[str],
    helix_url: str,
    agent_kind: Optional[str] = None,
    state: Optional[Dict[str, str]] = None,
) -> Dict[str, int]:
    """One pass over every watched dir. Returns counters for logging."""
    if state is None:
        state = _load_state()
    counters = {"new": 0, "changed": 0, "unchanged": 0, "skipped": 0,
                "deleted": 0, "errors": 0}

    live_paths: set[str] = set()
    scanned_roots: List[Path] = []

    for d in watch_dirs:
        dp = Path(d)
        if not dp.exists():
            log.warning("mem_sync watch dir missing: %s", d)
            continue
        scanned_roots.append(dp.resolve())
        for md in sorted(dp.glob("*.md")):
            md = md.resolve()
            key = str(md)
            live_paths.add(key)
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                counters["errors"] += 1
                log.warning("mem_sync read failed: %s", md, exc_info=True)
                continue

            fields, _body = _parse_frontmatter(text)
            skip_reason = _is_skipped(md, fields)
            if skip_reason:
                counters["skipped"] += 1
                continue

            digest = _sha256(text)
            prev = state.get(key)
            if prev == digest:
                counters["unchanged"] += 1
                continue

            gene_ids = _ingest_file(helix_url, md, text, fields, agent_kind)
            if gene_ids is None:
                counters["errors"] += 1
                continue

            state[key] = digest
            if prev is None:
                counters["new"] += 1
                log.info("mem_sync NEW %s → %d gene(s)", md.name, len(gene_ids))
            else:
                counters["changed"] += 1
                log.info("mem_sync CHANGED %s → %d gene(s)",
                         md.name, len(gene_ids))

    # Detect deletes: entries under a dir we scanned THIS pass that are
    # gone. The state file is shared by every syncer on the machine, so
    # entries belonging to another watch-set (a different agent's dirs,
    # or a dir that is temporarily missing) are out of scope and must
    # survive untouched.
    for key in list(state.keys()):
        if key in live_paths:
            continue
        parent = Path(key).parent
        if not any(parent == root for root in scanned_roots):
            continue
        counters["deleted"] += 1
        _tombstone_file(helix_url, Path(key))
        del state[key]
        log.info("mem_sync DELETED %s", Path(key).name)

    _save_state(state)
    return counters


# ── Daemon entry ─────────────────────────────────────────────────────

def run_daemon(
    watch_dirs: List[str],
    helix_url: str = "http://127.0.0.1:11437",
    sync_interval_s: int = 60,
    agent_kind: Optional[str] = None,
) -> None:
    """Blocking loop — call sync_once every `sync_interval_s` seconds."""
    log.warning(
        "mem_sync daemon starting — watching %d dir(s), interval=%ds, "
        "agent_kind=%s (HELIX_AGENT=%s, HELIX_USER=%s)",
        len(watch_dirs), sync_interval_s, agent_kind,
        os.environ.get("HELIX_AGENT", "<unset>"),
        os.environ.get("HELIX_USER", "<unset>"),
    )
    state = _load_state()
    while True:
        t0 = time.time()
        try:
            counters = sync_once(watch_dirs, helix_url, agent_kind, state)
            # Only log when something changed — quiet in steady state.
            if any(counters[k] for k in ("new", "changed", "deleted", "errors")):
                log.info("mem_sync pass: %s", counters)
        except Exception:
            log.warning("mem_sync pass raised — continuing", exc_info=True)
        elapsed = time.time() - t0
        time.sleep(max(1.0, sync_interval_s - elapsed))

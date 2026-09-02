"""Bed provenance — an append-only build/mutation log stored inside the bed.

A knowledge store (``genome.db``, a "bed" in bench vocabulary) carried no
build metadata at all before this module.  That gap has a measurable cost:
the 2026-07 EnterpriseRAG-Bench leaderboard submission cannot be attributed
to a config or a code revision, and the 2026-08-20 compaction silently
changed the file that several published receipts named, leaving every
before/after comparison ambiguous.

The fix is to make the bed carry its own history.  ``bed_provenance`` is
append-only: every build, ingest run, migration, backfill and compaction
adds a row.  Reading the table answers "what produced this bed, on what
code, under which ingest-affecting knobs, and how has it aged since?"

Only knobs that change bed *content* belong in ``config_json``.  Retrieval
knobs are deliberately excluded — they are read-side and cannot alter a bed
that is already on disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import time
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger(__name__)

# Bumped when the row format changes in a way readers must notice.
PROVENANCE_VERSION = 1

# Event vocabulary.  Free-form strings are accepted, but these are the ones
# the tooling knows how to summarise.
EVENT_BUILD = "build"
EVENT_INGEST = "ingest"
EVENT_MIGRATE = "migrate"
EVENT_COMPACT = "compact"
EVENT_BACKFILL = "backfill"
EVENT_STAMP = "stamp"

# The ingest-affecting knob subset, as ``(section, key)``.  Anything that
# changes the bytes written at ingest belongs here.
_INGEST_KNOBS: tuple[tuple[str, str], ...] = (
    ("ingestion", "backend"),
    ("ingestion", "splade_enabled"),
    ("ingestion", "dense_embed_on_ingest"),
    ("ingestion", "sema_embed_on_ingest"),
    ("ingestion", "entity_graph"),
    # #411 (2026-08-30, after this lane's base): drops hub entities from the
    # ingest-time auto-link probe set, so it changes the gene_relations
    # relation=5 COVER edges a bed carries.  Bed content -> provenance.
    ("ingestion", "entity_autolink_hub_cutoff"),
    ("ingestion", "symbol_graph"),
    ("ingestion", "splade_content_cap"),
    ("ingestion", "dense_passage_char_cap"),
    ("ingestion", "locale_demotion_enabled"),
    ("ingestion", "citation_path_anchors"),
    ("cymatics", "harmonic_links"),
    ("genome", "synchronous"),
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def create_table(cur: sqlite3.Cursor) -> None:
    """Create ``bed_provenance`` if absent.  Safe to call on every open."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bed_provenance (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            at_utc             TEXT    NOT NULL,
            event              TEXT    NOT NULL,
            provenance_version INTEGER NOT NULL,
            cymatix_version    TEXT    NOT NULL,
            git_sha            TEXT,
            git_dirty          INTEGER,
            ingest_c           INTEGER,
            config_json        TEXT,
            gene_count         INTEGER,
            fts_count          INTEGER,
            identity_sha256    TEXT,
            corpus_id          TEXT,
            notes              TEXT
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_bed_provenance_at "
        "ON bed_provenance(at_utc)"
    )


# ---------------------------------------------------------------------------
# Environment capture
# ---------------------------------------------------------------------------

def _cymatix_version() -> str:
    try:
        from .. import __version__

        return str(__version__)
    except Exception:
        log.warning("could not read cymatix version", exc_info=True)
        return "unknown"


def _git_state(repo_root: Optional[str] = None) -> tuple[Optional[str], Optional[int]]:
    """Return ``(sha, dirty)``.  ``(None, None)`` when git is unavailable.

    ``dirty`` is 1 when the working tree has uncommitted changes, so a bed
    built from an edited tree is never mistaken for one built from the tag.
    """
    cwd = repo_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    kwargs: Dict[str, Any] = {
        "cwd": cwd,
        "capture_output": True,
        "text": True,
        "timeout": 15,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    try:
        sha_proc = subprocess.run(["git", "rev-parse", "HEAD"], **kwargs)
        if sha_proc.returncode != 0:
            return None, None
        sha = (sha_proc.stdout or "").strip() or None
        status = subprocess.run(["git", "status", "--porcelain"], **kwargs)
        dirty = 1 if (status.returncode == 0 and (status.stdout or "").strip()) else 0
        return sha, dirty
    except Exception:
        log.warning("git state capture failed", exc_info=True)
        return None, None


def ingest_config_snapshot(config: Any) -> Dict[str, Any]:
    """Extract the ingest-affecting knob subset from a ``CymatixConfig``.

    Missing sections/keys are recorded as ``None`` rather than omitted, so a
    knob that did not exist at build time stays distinguishable from one
    that was explicitly unset.
    """
    snap: Dict[str, Any] = {}
    for section, key in _INGEST_KNOBS:
        value: Any = None
        try:
            sect = getattr(config, section, None)
            if sect is not None:
                value = getattr(sect, key, None)
        except Exception:
            log.warning("config snapshot failed for %s.%s", section, key, exc_info=True)
            value = None
        if isinstance(value, (list, tuple, set)):
            value = sorted(str(v) for v in value)
        snap[f"{section}.{key}"] = value
    return snap


# ---------------------------------------------------------------------------
# Identity digest
# ---------------------------------------------------------------------------

def gene_count(conn: sqlite3.Connection) -> Optional[int]:
    try:
        row = conn.execute("SELECT COUNT(*) FROM genes").fetchone()
        return int(row[0]) if row else None
    except Exception:
        log.warning("gene_count failed", exc_info=True)
        return None


def fts_count(conn: sqlite3.Connection) -> Optional[int]:
    """Rows in the FTS index.

    On an external-content table ``COUNT(*) FROM genes_fts`` is a full scan of
    the backing view (minutes on an 800k bed) and merely re-reports the genes
    count.  ``ddl.fts5_index_row_count`` reads the ``genes_fts_docsize``
    shadow instead, which is both cheap and the actual index-side number.
    """
    try:
        from . import ddl  # local import: ddl imports this module

        cur = conn.cursor()
        if ddl.fts5_is_external_content(cur):
            return ddl.fts5_index_row_count(cur)
        row = cur.execute("SELECT COUNT(*) FROM genes_fts").fetchone()
        return int(row[0]) if row else None
    except Exception:
        # A bed with no FTS index is legal; do not warn loudly.
        log.debug("fts_count unavailable", exc_info=True)
        return None


def identity_sha256(conn: sqlite3.Connection) -> Optional[str]:
    """Stable digest over the bed's ``gene_id`` set.

    Streams in ``gene_id`` order so the result is deterministic and does not
    materialise 800k+ ids in memory.  Two beds sharing a digest hold the same
    documents; a differing digest localises drift to ingest rather than to a
    read-side knob.
    """
    try:
        h = hashlib.sha256()
        cur = conn.execute("SELECT gene_id FROM genes ORDER BY gene_id")
        while True:
            rows = cur.fetchmany(10_000)
            if not rows:
                break
            for (gid,) in rows:
                h.update(str(gid).encode("utf-8"))
                h.update(b"\x00")
        return h.hexdigest()
    except Exception:
        log.warning("identity digest failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def record_event(
    conn: sqlite3.Connection,
    event: str,
    *,
    config: Any = None,
    overrides: Optional[Dict[str, Any]] = None,
    ingest_c: Optional[int] = None,
    corpus_id: Optional[str] = None,
    notes: Optional[str] = None,
    with_identity: bool = False,
    commit: bool = True,
) -> Optional[int]:
    """Append one provenance row.  Returns its rowid, or ``None`` on failure.

    Provenance must never break the operation it describes, so every failure
    path is swallowed after a warning.

    ``with_identity`` costs a full scan of ``genes``; pass it for build,
    compact and explicit stamp events, not for routine per-document ingest.

    ``overrides`` is merged over the config snapshot and exists because a
    build tool may not honour ``cymatix.toml``.  ``scripts/build_fixture_matrix.py``
    is the live example: it hardcodes ``entity_graph=True`` and reads SPLADE
    and dense backfill from ``CYMATIX_BFM_*`` env flags that default ON,
    which contradicts the shipped v0.9.0 defaults.  Recording the config
    alone would therefore describe a bed that was never built.  Pass what
    actually ran.
    """
    try:
        cur = conn.cursor()
        create_table(cur)
        sha, dirty = _git_state()
        snap: Optional[Dict[str, Any]] = None
        if config is not None:
            snap = ingest_config_snapshot(config)
        if overrides:
            snap = dict(snap or {})
            snap.update(overrides)
        cfg_json = json.dumps(snap, sort_keys=True) if snap is not None else None
        cur.execute(
            """
            INSERT INTO bed_provenance (
                at_utc, event, provenance_version, cymatix_version,
                git_sha, git_dirty, ingest_c, config_json,
                gene_count, fts_count, identity_sha256, corpus_id, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                event,
                PROVENANCE_VERSION,
                _cymatix_version(),
                sha,
                dirty,
                ingest_c,
                cfg_json,
                gene_count(conn),
                fts_count(conn),
                identity_sha256(conn) if with_identity else None,
                corpus_id,
                notes,
            ),
        )
        if commit:
            conn.commit()
        return int(cur.lastrowid)
    except Exception:
        log.warning("provenance record_event(%s) failed", event, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def read_events(
    conn: sqlite3.Connection, limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Return provenance rows oldest-first.  Empty list on an unstamped bed."""
    try:
        sql = "SELECT * FROM bed_provenance ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            cur = conn.execute(sql, (int(limit),))
        else:
            cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        out: List[Dict[str, Any]] = []
        for row in cur.fetchall():
            rec = dict(zip(cols, row))
            if rec.get("config_json"):
                try:
                    rec["config"] = json.loads(rec["config_json"])
                except Exception:
                    log.warning("unparseable config_json on row %s", rec.get("id"))
                    rec["config"] = None
            out.append(rec)
        return out
    except Exception:
        log.debug("no bed_provenance table", exc_info=True)
        return []


def config_drift(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Diff each event's ingest config against the previous one.

    This is what makes an aging bed legible: it names the exact knob that
    changed between two ingest runs into the same file.
    """
    drift: List[Dict[str, Any]] = []
    prev: Optional[Dict[str, Any]] = None
    prev_id: Optional[int] = None
    for rec in events:
        cfg = rec.get("config")
        if not isinstance(cfg, dict):
            continue
        if prev is not None:
            changed = {
                k: {"from": prev.get(k), "to": cfg.get(k)}
                for k in sorted(set(prev) | set(cfg))
                if prev.get(k) != cfg.get(k)
            }
            if changed:
                drift.append(
                    {
                        "from_id": prev_id,
                        "to_id": rec.get("id"),
                        "at_utc": rec.get("at_utc"),
                        "changed": changed,
                    }
                )
        prev, prev_id = cfg, rec.get("id")
    return drift

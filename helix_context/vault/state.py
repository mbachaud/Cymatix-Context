"""vault.db sibling state — gene_id → path + content-hash sentinel.

Lives in <vault_root>/vault.db, NOT in genome.db. Lifecycle is the vault's,
not the knowledge store's. See spec section "State tracking — sibling vault.db".
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
TOP_LEVEL_FILENAME = ".helix-state.json"

_TOP_LEVEL_STATE_KEYS = frozenset({
    "schema_version",
    "last_full_export_ts",
    "last_incremental_export_ts",
    "exported_gene_count",
    "fan_out_engaged_domains",
})


@dataclass(frozen=True)
class VaultStateRecord:
    gene_id: str
    vault_path: str
    last_exported_ts: float
    last_exported_disk_hash: Optional[str]


class VaultState:
    """SQLite + JSON state for the vault.

    vault.db holds per-document rows (vault_state table).
    .helix-state.json holds top-level state (schema_version, export timestamps).

    Callers must call close() explicitly. Context manager support
    will be added in Task 14 alongside VaultManager lifecycle.
    """

    class SchemaVersionMismatch(Exception):
        pass

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = Path(vault_root)
        self.vault_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db_path = self.vault_root / "vault.db"
        self._json_path = self.vault_root / TOP_LEVEL_FILENAME

        # Connection follows genome.py WAL hygiene from PR #32
        # (autocommit + WAL + busy_timeout + journal_size_limit).
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=30, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_size_limit=67108864")  # 64 MB

        self._ensure_schema()
        self._ensure_top_level_state()

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vault_state (
                gene_id                  TEXT PRIMARY KEY,
                vault_path               TEXT NOT NULL,
                last_exported_ts         REAL NOT NULL,
                last_exported_disk_hash  TEXT
            );
            -- Speculative index for Task 13 orphan-file sweep (lookup by path).
            CREATE INDEX IF NOT EXISTS idx_vault_state_path
                ON vault_state(vault_path);
            """
        )
        self._conn.commit()  # executescript implicitly commits in autocommit; harmless redundancy

    def _ensure_top_level_state(self) -> None:
        if not self._json_path.exists():
            self._write_top_level_state(
                {
                    "schema_version": SCHEMA_VERSION,
                    "last_full_export_ts": 0.0,
                    "last_incremental_export_ts": 0.0,
                    "exported_gene_count": 0,
                    "fan_out_engaged_domains": [],
                }
            )
            return
        existing = self.read_top_level_state()
        if existing.get("schema_version") != SCHEMA_VERSION:
            raise VaultState.SchemaVersionMismatch(
                f"vault state schema_version={existing.get('schema_version')} "
                f"but code expects {SCHEMA_VERSION}; run helix-vault migrate"
            )

    def read_top_level_state(self) -> dict:
        with self._json_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def update_top_level_state(self, **fields) -> None:
        unknown = set(fields) - _TOP_LEVEL_STATE_KEYS
        if unknown:
            raise ValueError(f"unknown top-level state keys: {sorted(unknown)!r}")
        current = self.read_top_level_state()
        current.update(fields)
        self._write_top_level_state(current)

    def _write_top_level_state(self, data: dict) -> None:
        tmp = self._json_path.with_suffix(self._json_path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            tmp.replace(self._json_path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                log.warning("failed to clean up tmp state file %s", tmp, exc_info=True)
            raise

    def upsert_record(
        self, *, gene_id: str, path: str, ts: float, disk_hash: Optional[str]
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO vault_state (gene_id, vault_path, last_exported_ts, last_exported_disk_hash)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(gene_id) DO UPDATE SET
                vault_path = excluded.vault_path,
                last_exported_ts = excluded.last_exported_ts,
                last_exported_disk_hash = excluded.last_exported_disk_hash
            """,
            (gene_id, path, ts, disk_hash),
        )
        self._conn.commit()

    def get_record(self, gene_id: str) -> Optional[VaultStateRecord]:
        row = self._conn.execute(
            "SELECT gene_id, vault_path, last_exported_ts, last_exported_disk_hash "
            "FROM vault_state WHERE gene_id = ?",
            (gene_id,),
        ).fetchone()
        if row is None:
            return None
        return VaultStateRecord(
            gene_id=row["gene_id"],
            vault_path=row["vault_path"],
            last_exported_ts=row["last_exported_ts"],
            last_exported_disk_hash=row["last_exported_disk_hash"],
        )

    def delete_record(self, gene_id: str) -> None:
        self._conn.execute("DELETE FROM vault_state WHERE gene_id = ?", (gene_id,))
        self._conn.commit()

    def iter_records(self) -> Iterator[VaultStateRecord]:
        for row in self._conn.execute(
            "SELECT gene_id, vault_path, last_exported_ts, last_exported_disk_hash "
            "FROM vault_state"
        ):
            yield VaultStateRecord(
                gene_id=row["gene_id"],
                vault_path=row["vault_path"],
                last_exported_ts=row["last_exported_ts"],
                last_exported_disk_hash=row["last_exported_disk_hash"],
            )

    def close(self) -> None:
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            log.warning("vault.db WAL checkpoint on close failed", exc_info=True)
        try:
            self._conn.close()
        except Exception:
            log.warning("vault.db close failed", exc_info=True)

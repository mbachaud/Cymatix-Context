"""Vault-root filelock — coordinates writers across the in-process VaultManager
and any external `helix-vault` CLI invocations.

Backed by `filelock` (portable, file-based advisory locks). The lockfile lives
at <vault_root>/vault.lock and is created on first acquire.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from filelock import FileLock, Timeout

log = logging.getLogger(__name__)


class VaultLock:
    """Context manager wrapping a vault-root filelock.

    Usage:
        lock = VaultLock(vault_root, timeout=10.0)
        with lock:
            ...

    Raises:
        TimeoutError: if the lock can't be acquired within `timeout` seconds.
    """

    def __init__(self, vault_root: Path, timeout: float = 30.0) -> None:
        self.vault_root = Path(vault_root)
        self.vault_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lockpath = self.vault_root / "vault.lock"
        self._timeout = timeout
        self._lock: Optional[FileLock] = None

    def __enter__(self) -> "VaultLock":
        self._lock = FileLock(str(self._lockpath))
        try:
            self._lock.acquire(timeout=self._timeout)
        except Timeout as exc:
            raise TimeoutError(
                f"Could not acquire {self._lockpath} within {self._timeout}s"
            ) from exc
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._lock is not None:
            try:
                self._lock.release()
            except Exception:
                log.warning("filelock release failed", exc_info=True)
            self._lock = None
        return None

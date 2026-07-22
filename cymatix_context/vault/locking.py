"""Vault-root filelock — coordinates writers across the in-process VaultManager
and any external `helix-vault` CLI invocations.

Backed by `filelock` (portable, file-based advisory locks). The lockfile lives
at <vault_root>/vault.lock and is created on first acquire.

The lockfile is intentionally not deleted on release — deletion would introduce
a TOCTOU race on POSIX and is unnecessary for correctness. Operators browsing
the vault directory will see vault.lock as a 0-byte sentinel file.
"""
from __future__ import annotations

import logging
from pathlib import Path

from filelock import FileLock, Timeout

log = logging.getLogger(__name__)


class VaultLock:
    """Context manager wrapping a vault-root filelock.

    Usage:
        lock = VaultLock(vault_root, timeout=10.0)
        with lock:
            ...

    Re-entrant on the same instance: nested ``with lock:`` blocks increment
    filelock's internal counter rather than reacquiring the OS lock.

    Raises:
        TimeoutError: if the lock can't be acquired within ``timeout`` seconds.
    """

    def __init__(self, vault_root: Path, timeout: float = 30.0) -> None:
        self.vault_root = Path(vault_root)
        self.vault_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lockpath = self.vault_root / "vault.lock"
        self._timeout = timeout
        # FileLock is reusable; its internal counter handles re-entrant acquires.
        self._lock = FileLock(self._lockpath)

    def __enter__(self) -> "VaultLock":
        try:
            self._lock.acquire(timeout=self._timeout)
        except Timeout as exc:
            raise TimeoutError(
                f"Could not acquire {self._lockpath} within {self._timeout}s"
            ) from exc
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            self._lock.release()
        except Exception:
            log.warning("filelock release failed", exc_info=True)
        return None

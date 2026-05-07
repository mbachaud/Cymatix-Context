"""Tests for vault-root filelock context manager."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from helix_context.vault.locking import VaultLock


def test_lock_acquires_and_releases(tmp_path: Path):
    lock = VaultLock(vault_root=tmp_path)
    with lock:
        assert (tmp_path / "vault.lock").exists()


def test_lock_blocks_concurrent_acquirer(tmp_path: Path):
    lock1 = VaultLock(vault_root=tmp_path, timeout=0.5)
    lock2 = VaultLock(vault_root=tmp_path, timeout=0.5)
    with lock1:
        with pytest.raises(TimeoutError):
            with lock2:
                pass


def test_lock_releases_on_exception(tmp_path: Path):
    lock1 = VaultLock(vault_root=tmp_path, timeout=0.5)
    lock2 = VaultLock(vault_root=tmp_path, timeout=0.5)
    try:
        with lock1:
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # lock2 should now acquire fine
    with lock2:
        pass


def test_concurrent_threads_serialize(tmp_path: Path):
    """Two threads grabbing the lock should serialize, not interleave."""
    order = []
    barrier = threading.Barrier(2)

    def worker(name: str):
        barrier.wait()
        lock = VaultLock(vault_root=tmp_path, timeout=5.0)
        with lock:
            order.append(f"{name}-enter")
            time.sleep(0.05)
            order.append(f"{name}-exit")

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # No interleaving: enter must be followed by exit before next enter
    assert order[0].endswith("-enter")
    assert order[1] == order[0].replace("-enter", "-exit")
    assert order[2].endswith("-enter")
    assert order[3] == order[2].replace("-enter", "-exit")

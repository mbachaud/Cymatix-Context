"""Publisher public keys trusted to sign directory plugins.

Raw 32-byte Ed25519 public keys. Empty means no publisher is trusted yet, so
only explicitly allowed unsigned directory plugins can load.
"""
from __future__ import annotations

TRUSTED_PUBLISHER_KEYS: tuple[bytes, ...] = ()

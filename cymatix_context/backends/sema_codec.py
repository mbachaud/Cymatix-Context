"""SEMA embedding codec — packed fp32 BLOB storage for the 20-d ΣĒMA vector.

``genes.embedding`` historically held a JSON ``list[float]`` (~403–411 bytes/row
measured on the live genome). Packed little-endian fp32 is ~80 bytes/row — a ~5x
reduction (~10x at fp16, not used here). The column keeps its **TEXT affinity**:
SQLite stores a BLOB as a BLOB there and ``WHERE embedding IS NOT NULL`` still
matches, so **no schema migration** is required — unlike the ``embedding_dense``
→ ``embedding_dense_v2`` rollout this reuses the *same* column.

Writes emit the BLOB; reads dual-decode so legacy JSON rows keep working and
convert opportunistically as rows are re-upserted. fp32 precision is immaterial:
the vector only feeds cosine ranking. ``struct`` (stdlib, explicit little-endian)
is used rather than numpy so the storage layer stays numpy-free — the SEMA vector
is the encoder's output, but reading it back for cosine must not require numpy.

See docs/design/2026-07-05-efficiency-cost-reduction.md and bgem3_codec.py.
"""
from __future__ import annotations

import struct
from typing import Any, List, Optional

from ..accel import json_loads


def sema_vec_to_blob(vec: List[float]) -> bytes:
    """Pack a SEMA vector as a raw little-endian fp32 BLOB (``len(vec)*4`` bytes)."""
    return struct.pack("<%df" % len(vec), *vec)


def decode_embedding(value: Any) -> Optional[List[float]]:
    """Decode a ``genes.embedding`` cell, tolerating both on-disk encodings.

    - ``bytes``/BLOB → packed little-endian fp32 (current write format)
    - ``str`` → legacy JSON ``list[float]``
    - ``None`` / empty → ``None``
    """
    if not value:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) % 4:
            raise ValueError(
                f"embedding BLOB length {len(raw)} is not a multiple of 4 bytes"
            )
        return list(struct.unpack("<%df" % (len(raw) // 4), raw))
    return json_loads(value)

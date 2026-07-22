"""Tests for the SEMA embedding codec (packed fp32 BLOB storage).

Covers the codec in isolation plus the SQLite affinity contract it relies on:
a BLOB written into the TEXT-affinity ``genes.embedding`` column stays a BLOB and
still satisfies ``WHERE embedding IS NOT NULL``, so no schema migration is needed.
See docs/design/2026-07-05-efficiency-cost-reduction.md.
"""

import json
import sqlite3
import struct

import pytest

from cymatix_context.backends.sema_codec import decode_embedding, sema_vec_to_blob


def test_blob_roundtrip_exact_for_fp32_representable():
    vec = [0.75, 0.5, 1.0, 0.125, -0.25]  # all exactly representable in fp32
    blob = sema_vec_to_blob(vec)
    assert isinstance(blob, bytes)
    assert len(blob) == len(vec) * 4
    assert decode_embedding(blob) == vec


def test_blob_roundtrip_within_fp32_tolerance():
    vec = [0.1, 0.2, -0.037430133670568466, 0.9999999]
    out = decode_embedding(sema_vec_to_blob(vec))
    assert out == pytest.approx(vec, abs=1e-6)


def test_decode_accepts_legacy_json_text():
    # Rows written before the BLOB change are JSON list[float] strings.
    assert decode_embedding("[1.0, 2.0, 3.0]") == [1.0, 2.0, 3.0]
    assert decode_embedding(json.dumps([0.5, -0.5])) == [0.5, -0.5]


def test_decode_none_and_empty_return_none():
    assert decode_embedding(None) is None
    assert decode_embedding("") is None
    assert decode_embedding(b"") is None


def test_decode_accepts_memoryview_and_bytearray():
    blob = sema_vec_to_blob([1.0, 2.0])
    assert decode_embedding(bytearray(blob)) == [1.0, 2.0]
    assert decode_embedding(memoryview(blob)) == [1.0, 2.0]


def test_corrupt_blob_length_raises():
    with pytest.raises(ValueError):
        decode_embedding(b"\x00\x00\x00")  # 3 bytes, not a multiple of 4


def test_blob_is_much_smaller_than_json_for_20d():
    vec = [0.123456789 * i for i in range(20)]
    blob_bytes = len(sema_vec_to_blob(vec))
    json_bytes = len(json.dumps(vec).encode())
    assert blob_bytes == 80  # 20 * 4
    assert json_bytes > 3 * blob_bytes  # measured ~5x on the live genome


def test_blob_survives_text_affinity_column_and_is_not_null():
    """The load-bearing SQLite contract: BLOB in a TEXT column stays a BLOB,
    still matches ``IS NOT NULL``, and is type-distinguishable from legacy text.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE genes (gene_id TEXT PRIMARY KEY, embedding TEXT)")
    conn.execute(
        "INSERT INTO genes VALUES (?, ?)", ("new", sema_vec_to_blob([0.5, 0.25]))
    )
    conn.execute("INSERT INTO genes VALUES (?, ?)", ("old", "[0.5, 0.25]"))
    conn.execute("INSERT INTO genes VALUES (?, ?)", ("none", None))

    matched = {
        r["gene_id"]: r["embedding"]
        for r in conn.execute(
            "SELECT gene_id, embedding FROM genes WHERE embedding IS NOT NULL"
        )
    }
    assert set(matched) == {"new", "old"}  # NULL excluded, BLOB included
    assert isinstance(matched["new"], (bytes, bytearray))
    assert isinstance(matched["old"], str)
    # both decode to the same logical vector regardless of on-disk encoding
    assert decode_embedding(matched["new"]) == [0.5, 0.25]
    assert decode_embedding(matched["old"]) == [0.5, 0.25]

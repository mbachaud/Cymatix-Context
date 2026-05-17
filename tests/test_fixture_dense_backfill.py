"""Tier-0 PR-2 (2026-05-16): the fixture builder's post-build dense pass.

Plan: ``docs/reviews/2026-05-16-deep-review/00-tier0-implementation-plan.md``
§PR-2. PR-2 makes ``scripts/build_fixture_matrix.py`` produce bench
fixtures whose ``genes.embedding_dense_v2`` column is fully populated, in
both blob and sharded mode, via an explicit post-build BGE-M3 backfill
pass (the builder's per-gene write path stays lean).

The encode-and-pack loop is factored into ONE shared function,
``scripts.backfill_bgem3_v2.backfill_dense_db``, that both the standalone
operator backfill script and the fixture builder
(``build_fixture_matrix._backfill_dense``) call — so the two paths cannot
drift. These tests exercise that shared loop and the builder wrapper.

Cases covered:

1. ``test_backfill_populates_every_gene`` — every ``genes`` row of a
   small hand-built ``.db`` gets a non-NULL ``embedding_dense_v2``.
2. ``test_blob_bytes_are_dim_times_four`` — each BLOB is exactly
   ``dim*4`` raw little-endian fp32 bytes and round-trips.
3. ``test_coverage_reported_correctly`` — the report dict's
   ``dense_coverage`` == populated / total.
4. ``test_idempotent_skips_populated_rows`` — a second run re-processes
   0 rows (already-populated rows of the right length are skipped).
5. ``test_wrong_length_blob_is_reencoded`` — a row carrying a
   wrong-length BLOB is treated as needing a backfill.
6. ``test_blank_content_rows_skipped`` — rows with empty/whitespace
   content are skipped, not encoded, and do not crash.
7. ``test_backfill_dense_builder_wrapper`` — ``_backfill_dense`` (the
   builder's wrapper) returns the coverage report and populates the DB.
8. ``test_backfill_dense_wrapper_handles_failure`` — an encode failure
   surfaces as ``dense_coverage == 0.0`` + an ``error`` key rather than
   raising.
9. ``test_partial_coverage_db`` — a DB pre-seeded with some rows already
   dense reports the correct final coverage.
10. ``test_live_*`` (``live``-marked) — build the ``small`` blob profile
    with the real BGE-M3 model and assert 100% dense coverage +
    ``manifest.json`` ``dense_coverage == 1.0``. Self-skips when the
    model (or a source root) is unavailable.

The codec is mocked with deterministic hash-seeded vectors so the suite
runs without BGE-M3 weights (mirrors ``tests/test_dense_recall.py`` and
``tests/test_ingest_dense_v2.py``).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

# ``scripts/`` is not a package — put it on the path so the PR-2 modules
# under test import cleanly (mirrors how the scripts add the repo root).
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from backfill_bgem3_v2 import backfill_dense_db  # noqa: E402
import build_fixture_matrix as bfm  # noqa: E402

DIM = 64  # small dim keeps the fake-vector tests fast; not a sanctioned
# BGE-M3 Matryoshka breakpoint, but the fake codec never loads the model.


# ── Test helpers (mirror tests/test_ingest_dense_v2.py) ──────────────


def _hash_vec(text: str, dim: int = DIM) -> np.ndarray:
    """Deterministic L2-normalised fp32 vector seeded from text."""
    out = np.zeros(dim, dtype=np.float32)
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(seed[:8], "little"))
    for i in range(dim):
        out[i] = rng.gauss(0.0, 1.0)
    n = np.linalg.norm(out)
    if n > 0:
        out /= n
    return out


class _FakeCodec:
    """Test stand-in for BGEM3Codec — same shape contract as the real
    codec's ``encode_batch`` (the only method the backfill loop calls).
    """

    def __init__(self, dim: int = DIM):
        self.dim = dim
        self.batch_calls = 0
        self.encoded_texts: list[str] = []

    def encode_batch(self, texts, task: str = "passage"):
        self.batch_calls += 1
        self.encoded_texts.extend(texts)
        return [_hash_vec(t, self.dim).tolist() for t in texts]


class _ExplodingCodec:
    """Codec whose ``encode_batch`` always raises — models a missing /
    broken BGE-M3 model so the builder's failure path can be exercised.
    """

    dim = DIM

    def encode_batch(self, texts, task: str = "passage"):
        raise RuntimeError("BGE-M3 model unavailable (simulated)")


def _make_genes_db(
    tmp_path: Path,
    contents: list[str],
    *,
    name: str = "fixture.db",
    preseed_dense: dict[int, bytes] | None = None,
) -> str:
    """Build a minimal ``genes`` SQLite DB by hand.

    The schema is the subset of ``helix_context.storage.ddl`` columns the
    backfill loop touches (``gene_id``, ``content``, ``embedding_dense_v2``)
    plus ``chromatin`` — the partial index ``_ensure_v2_schema`` creates
    references ``chromatin``, so the column must exist.

    ``preseed_dense`` maps a row index to a raw BLOB to write into
    ``embedding_dense_v2`` up front (used to test partial-coverage and
    idempotency).
    """
    db_path = str(tmp_path / name)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE genes ("
        "  gene_id TEXT PRIMARY KEY,"
        "  content TEXT,"
        "  chromatin INTEGER,"
        "  embedding_dense_v2 BLOB"
        ")"
    )
    for i, content in enumerate(contents):
        blob = (preseed_dense or {}).get(i)
        conn.execute(
            "INSERT INTO genes (gene_id, content, chromatin, embedding_dense_v2) "
            "VALUES (?, ?, ?, ?)",
            (f"g{i}", content, 0, sqlite3.Binary(blob) if blob else None),
        )
    conn.commit()
    conn.close()
    return db_path


def _dense_rows(db_path: str) -> list[tuple[str, bytes | None]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT gene_id, embedding_dense_v2 FROM genes ORDER BY gene_id"
        ).fetchall()
    finally:
        conn.close()


# ── 1. every gene populated ──────────────────────────────────────────


def test_backfill_populates_every_gene(tmp_path):
    """``backfill_dense_db`` writes a non-NULL ``embedding_dense_v2`` for
    every ``genes`` row of a freshly-built dense-dark ``.db``.
    """
    contents = ["alpha doc body", "beta doc body", "gamma doc body"]
    db = _make_genes_db(tmp_path, contents)
    fake = _FakeCodec(DIM)

    report = backfill_dense_db(db, dim=DIM, codec=fake, log_fn=lambda _m: None)

    rows = _dense_rows(db)
    assert len(rows) == 3
    for gene_id, blob in rows:
        assert blob is not None, f"{gene_id} left dense-dark"
    assert report["total"] == 3
    assert report["populated_after"] == 3
    assert report["rows_processed"] == 3


# ── 2. BLOB length + round-trip ──────────────────────────────────────


def test_blob_bytes_are_dim_times_four(tmp_path):
    """Each stored BLOB is exactly ``dim*4`` raw little-endian fp32 bytes
    and decodes back to a unit-norm vector of length ``dim``.
    """
    db = _make_genes_db(tmp_path, ["only doc"])
    backfill_dense_db(db, dim=DIM, codec=_FakeCodec(DIM), log_fn=lambda _m: None)

    _gid, blob = _dense_rows(db)[0]
    assert len(blob) == DIM * 4, f"expected {DIM*4} bytes, got {len(blob)}"
    vec = np.frombuffer(blob, dtype="<f4")
    assert vec.shape == (DIM,)
    expected = _hash_vec("only doc", DIM)
    assert np.allclose(vec, expected, atol=1e-6), "stored BLOB did not round-trip"


# ── 3. coverage reported correctly ───────────────────────────────────


def test_coverage_reported_correctly(tmp_path):
    """``dense_coverage`` in the report == populated / total, and is 1.0
    once a fully dense-dark DB is backfilled.
    """
    db = _make_genes_db(tmp_path, ["d1", "d2", "d3", "d4"])
    report = backfill_dense_db(db, dim=DIM, codec=_FakeCodec(DIM), log_fn=lambda _m: None)

    assert report["dense_coverage"] == pytest.approx(1.0)
    assert report["populated_before"] == 0
    assert report["populated_after"] == report["total"] == 4
    # Coverage is the exact ratio.
    assert report["dense_coverage"] == pytest.approx(
        report["populated_after"] / report["total"]
    )


# ── 4. idempotency ───────────────────────────────────────────────────


def test_idempotent_skips_populated_rows(tmp_path):
    """A second backfill over an already-populated DB re-processes 0 rows
    — the ``length(blob) == dim*4`` skip-clause matches its own output.
    """
    db = _make_genes_db(tmp_path, ["doc one", "doc two"])
    first = backfill_dense_db(db, dim=DIM, codec=_FakeCodec(DIM), log_fn=lambda _m: None)
    assert first["rows_processed"] == 2

    before = _dense_rows(db)
    fake2 = _FakeCodec(DIM)
    second = backfill_dense_db(db, dim=DIM, codec=fake2, log_fn=lambda _m: None)

    assert second["rows_processed"] == 0, "idempotent re-run must process 0 rows"
    assert second["dense_coverage"] == pytest.approx(1.0)
    assert fake2.batch_calls == 0, "no rows to encode ⇒ codec untouched"
    assert _dense_rows(db) == before, "idempotent re-run must not mutate BLOBs"


# ── 5. wrong-length BLOB is re-encoded ───────────────────────────────


def test_wrong_length_blob_is_reencoded(tmp_path):
    """A row whose ``embedding_dense_v2`` is the wrong byte length (e.g. a
    half-written row from an earlier crash, or a dim change) is selected
    for re-backfill and overwritten with a correct ``dim*4`` BLOB.
    """
    garbage = b"\x00\x01\x02\x03"  # 4 bytes, not DIM*4
    db = _make_genes_db(
        tmp_path, ["good doc", "stale doc"], preseed_dense={1: garbage}
    )
    report = backfill_dense_db(db, dim=DIM, codec=_FakeCodec(DIM), log_fn=lambda _m: None)

    # The wrong-length row counts as un-populated for the correct-length
    # ``populated_after`` tally, so both rows end at dim*4.
    rows = dict(_dense_rows(db))
    assert len(rows["g0"]) == DIM * 4
    assert len(rows["g1"]) == DIM * 4, "wrong-length BLOB was not re-encoded"
    assert report["rows_processed"] == 2
    assert report["dense_coverage"] == pytest.approx(1.0)


# ── 6. blank content skipped ─────────────────────────────────────────


def test_blank_content_rows_skipped(tmp_path):
    """Rows with empty / whitespace-only content have nothing to encode —
    they are skipped (not crashed on) and excluded from the encode batch.
    """
    db = _make_genes_db(tmp_path, ["real body", "   ", ""])
    fake = _FakeCodec(DIM)
    report = backfill_dense_db(db, dim=DIM, codec=fake, log_fn=lambda _m: None)

    rows = dict(_dense_rows(db))
    assert rows["g0"] is not None, "non-blank row should be populated"
    assert rows["g1"] is None and rows["g2"] is None, "blank rows stay NULL"
    assert report["rows_skipped"] == 2
    assert report["populated_after"] == 1
    assert fake.encoded_texts == ["real body"], "only non-blank text encoded"
    # Coverage counts blank rows in the denominator (they are genes rows).
    assert report["dense_coverage"] == pytest.approx(1.0 / 3.0)


# ── 7. builder wrapper ───────────────────────────────────────────────


def test_backfill_dense_builder_wrapper(tmp_path, monkeypatch):
    """``build_fixture_matrix._backfill_dense`` (the builder's post-build
    wrapper) returns the coverage report and populates every gene.

    The wrapper has no codec parameter — it calls ``backfill_dense_db``
    with the real ``BGEM3Codec``. Patch the shared loop so the test runs
    without BGE-M3 weights while still exercising the wrapper's plumbing
    (logging, report mapping, error guard).
    """
    db = _make_genes_db(tmp_path, ["wrap one", "wrap two", "wrap three"])

    def _fake_backfill(db_path, **kwargs):
        return backfill_dense_db(
            db_path, dim=DIM, codec=_FakeCodec(DIM), log_fn=lambda _m: None
        )

    monkeypatch.setattr(bfm, "backfill_dense_db", _fake_backfill)
    report = bfm._backfill_dense(db)

    assert report["dense_coverage"] == pytest.approx(1.0)
    assert report["populated_after"] == 3
    assert "error" not in report
    for _gid, blob in _dense_rows(db):
        assert blob is not None and len(blob) == DIM * 4


# ── 8. builder wrapper failure path ──────────────────────────────────


def test_backfill_dense_wrapper_handles_failure(tmp_path, monkeypatch):
    """If the dense encode fails, ``_backfill_dense`` returns a degraded
    report (``dense_coverage == 0.0`` + an ``error`` key) instead of
    raising — so a model failure surfaces in the manifest rather than
    silently shipping a dense-dark fixture.
    """
    db = _make_genes_db(tmp_path, ["doc"])

    def _exploding_backfill(db_path, **kwargs):
        return backfill_dense_db(
            db_path, dim=DIM, codec=_ExplodingCodec(), log_fn=lambda _m: None
        )

    monkeypatch.setattr(bfm, "backfill_dense_db", _exploding_backfill)
    report = bfm._backfill_dense(db)

    assert report["dense_coverage"] == 0.0
    assert "error" in report
    assert "RuntimeError" in report["error"]
    # The DB is left dense-dark (the encode never produced a vector).
    assert _dense_rows(db)[0][1] is None


# ── 9. partial pre-coverage ──────────────────────────────────────────


def test_partial_coverage_db(tmp_path):
    """A DB where some rows already carry a correct-length dense BLOB
    reports the right final coverage and only encodes the missing rows.
    """
    # Pre-seed row 0 with a valid DIM*4 BLOB; rows 1 and 2 are dense-dark.
    good_blob = _hash_vec("preseeded", DIM).astype("<f4").tobytes()
    assert len(good_blob) == DIM * 4
    db = _make_genes_db(
        tmp_path, ["preseeded", "needs one", "needs two"],
        preseed_dense={0: good_blob},
    )
    fake = _FakeCodec(DIM)
    report = backfill_dense_db(db, dim=DIM, codec=fake, log_fn=lambda _m: None)

    assert report["populated_before"] == 1
    assert report["rows_processed"] == 2, "only the 2 dark rows are encoded"
    assert report["populated_after"] == 3
    assert report["dense_coverage"] == pytest.approx(1.0)
    # The pre-seeded row was left untouched.
    assert dict(_dense_rows(db))["g0"] == good_blob
    assert set(fake.encoded_texts) == {"needs one", "needs two"}


# ── 10. live — real BGE-M3 small-profile fixture build ───────────────


@pytest.mark.live
def test_live_small_blob_profile_full_dense_coverage(tmp_path):
    """Build the ``small`` blob profile with the real BGE-M3 model and
    assert every ``genes`` row is dense-populated and the manifest
    records ``dense_coverage == 1.0``.

    Self-skips when the BGE-M3 model is unavailable or when none of the
    ``small`` profile's source roots exist on this machine.
    """
    profile = bfm.PROFILES["small"]
    if not any(os.path.exists(r) for r in profile["roots"]):
        pytest.skip("no 'small' profile source roots present on this machine")

    out_dir = str(tmp_path / "matrix")
    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, "small.db")

    try:
        stats = bfm.build_profile("small", db_path)
    except Exception as exc:  # noqa: BLE001 — BGE-M3 download / import failure
        pytest.skip(f"build_profile(small) failed (model unavailable?): {exc}")

    if stats.get("dense_error"):
        pytest.skip(f"dense backfill unavailable: {stats['dense_error']}")

    # Every genes row is dense-populated.
    conn = sqlite3.connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
        populated = conn.execute(
            "SELECT COUNT(*) FROM genes WHERE embedding_dense_v2 IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert total > 0, "small profile produced no genes"
    assert populated == total, f"{total - populated} genes left dense-dark"
    assert stats["dense_coverage"] == pytest.approx(1.0)

    # Manifest records dense_coverage == 1.0 for the profile.
    bfm.update_manifest(out_dir, stats, mode="blob")
    with open(os.path.join(out_dir, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["targets"]["small"]["dense_coverage"] == pytest.approx(1.0)

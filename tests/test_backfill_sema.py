"""Tests for scripts/backfill_sema.py.

Mirrors the fake-codec-injection pattern of
``tests/test_fixture_dense_backfill.py`` (the BGE-M3 v2 backfill's test
suite) so the SEMA backfill's encode-and-pack loop can be exercised
without sentence-transformers installed. The hand-built ``genes`` table
below is the minimal subset of ``cymatix_context.storage.ddl`` columns
the backfill touches (``gene_id``, ``content``, ``embedding``) — the
``embedding`` column keeps TEXT affinity in production (see
``cymatix_context/backends/sema_codec.py``) but happily stores a BLOB,
exactly as ``tests/test_sema_codec.py`` establishes.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import backfill_sema as bs  # noqa: E402
from backfill_sema import SEMA_CHAR_CAP, backfill_sema_db  # noqa: E402

from cymatix_context.backends.sema_codec import decode_embedding  # noqa: E402

DIM = 20


# ── Fake codecs ─────────────────────────────────────────────────────────


def _vec_for(text: str) -> list[float]:
    """Deterministic 20D vector seeded from the text — no model needed,
    and every value is a short fp32-exact fraction so blob round-trip
    assertions can use plain equality rather than approx tolerances.
    """
    n = len(text)
    return [round(((i + 1) * (n + 1)) % 97 / 97.0, 6) for i in range(DIM)]


class _FakeCodec:
    """Duck-types ``LazySemaCodec.encode_batch`` / ``RemoteSemaCodec.encode_batch``."""

    def __init__(self):
        self.batch_calls = 0
        self.encoded_texts: list[str] = []

    def encode_batch(self, texts, batch_size: int = 64):
        self.batch_calls += 1
        self.encoded_texts.extend(texts)
        return [_vec_for(t) for t in texts]


class _ExplodingCodec:
    """Every call raises — models a broken/unavailable SEMA model."""

    def encode_batch(self, texts, batch_size: int = 64):
        raise RuntimeError("SEMA model unavailable (simulated)")


class _FlakyCodec:
    """Fails the first batch, succeeds on every subsequent one — exercises
    the soft-fail-and-continue path across multiple batches.
    """

    def __init__(self):
        self.calls = 0

    def encode_batch(self, texts, batch_size: int = 64):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient failure (simulated)")
        return [_vec_for(t) for t in texts]


class _WrongShapeCodec:
    """Returns the wrong number of vectors — models a malformed daemon reply."""

    def encode_batch(self, texts, batch_size: int = 64):
        return [_vec_for(t) for t in texts[:-1]] if texts else []


# ── DB helpers ────────────────────────────────────────────────────────


def _make_genes_db(
    tmp_path: Path,
    rows: list[tuple[str, str | None, bytes | None]],
    name: str = "genome.db",
) -> str:
    """Build a minimal ``genes`` SQLite DB by hand.

    ``rows`` is a list of ``(gene_id, content, embedding_blob_or_None)``.
    """
    db_path = str(tmp_path / name)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE genes ("
        "  gene_id TEXT PRIMARY KEY,"
        "  content TEXT,"
        "  embedding TEXT"  # TEXT affinity, as in production — stores a BLOB fine.
        ")"
    )
    for gene_id, content, emb in rows:
        conn.execute(
            "INSERT INTO genes (gene_id, content, embedding) VALUES (?, ?, ?)",
            (gene_id, content, sqlite3.Binary(emb) if emb else None),
        )
    conn.commit()
    conn.close()
    return db_path


def _embeddings(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        return dict(
            conn.execute(
                "SELECT gene_id, embedding FROM genes ORDER BY gene_id"
            ).fetchall()
        )
    finally:
        conn.close()


# ── 1. only NULL-embedding rows touched ─────────────────────────────────


def test_only_null_embedding_rows_touched(tmp_path):
    preexisting = sqlite3.connect(":memory:")  # unused, just for clarity
    preexisting.close()
    pre_blob = bytes(range(80))  # 20 * 4 bytes of garbage-but-valid length
    db = _make_genes_db(
        tmp_path,
        [
            ("g0", "already has a vector", pre_blob),
            ("g1", "needs a vector", None),
            ("g2", "also needs a vector", None),
        ],
    )
    fake = _FakeCodec()

    report = backfill_sema_db(db, codec=fake, log_fn=lambda _m: None)

    rows = _embeddings(db)
    assert rows["g0"] == pre_blob, "row with an existing embedding must be untouched"
    assert rows["g1"] is not None
    assert rows["g2"] is not None
    assert fake.encoded_texts == ["needs a vector", "also needs a vector"], (
        "only the NULL-embedding rows should have been encoded"
    )
    assert report["backfilled"] == 2
    assert report["remaining"] == 0


def test_content_null_rows_never_selected(tmp_path):
    """embedding IS NULL AND content IS NOT NULL — a content-NULL row is
    never a candidate, and so stays NULL forever (correctly reflected in
    ``remaining``).
    """
    db = _make_genes_db(
        tmp_path,
        [
            ("g0", None, None),
            ("g1", "has content", None),
        ],
    )
    fake = _FakeCodec()
    report = backfill_sema_db(db, codec=fake, log_fn=lambda _m: None)

    assert fake.encoded_texts == ["has content"]
    assert report["backfilled"] == 1
    assert report["remaining"] == 1  # g0 stays NULL — no content to encode
    assert _embeddings(db)["g0"] is None


# ── 2. blob round-trips via decode_embedding ────────────────────────────


def test_blob_roundtrips_to_input_vector(tmp_path):
    db = _make_genes_db(tmp_path, [("g0", "hello world", None)])
    fake = _FakeCodec()

    backfill_sema_db(db, codec=fake, log_fn=lambda _m: None)

    blob = _embeddings(db)["g0"]
    decoded = decode_embedding(blob)
    assert decoded == pytest.approx(_vec_for("hello world"), abs=1e-6)
    assert len(decoded) == DIM


def test_content_is_capped_before_encoding(tmp_path):
    """content[:1000] — the same cap the ingest path applies — so a
    backfilled vector matches what ingest would have produced.
    """
    long_content = "x" * 2500
    db = _make_genes_db(tmp_path, [("g0", long_content, None)])
    fake = _FakeCodec()

    backfill_sema_db(db, codec=fake, log_fn=lambda _m: None)

    assert fake.encoded_texts == [long_content[:SEMA_CHAR_CAP]]
    assert len(fake.encoded_texts[0]) == 1000


# ── 3. --limit respected ────────────────────────────────────────────────


def test_limit_respected(tmp_path):
    rows = [(f"g{i}", f"content {i}", None) for i in range(5)]
    db = _make_genes_db(tmp_path, rows)
    fake = _FakeCodec()

    report = backfill_sema_db(db, limit=2, codec=fake, log_fn=lambda _m: None)

    assert report["total_candidates"] == 2
    assert report["backfilled"] == 2
    assert fake.batch_calls >= 1
    assert len(fake.encoded_texts) == 2
    assert report["remaining"] == 3, "the other 3 rows were never selected"


def test_limit_zero_means_all(tmp_path):
    rows = [(f"g{i}", f"content {i}", None) for i in range(5)]
    db = _make_genes_db(tmp_path, rows)
    fake = _FakeCodec()

    report = backfill_sema_db(db, limit=0, codec=fake, log_fn=lambda _m: None)

    assert report["total_candidates"] == 5
    assert report["backfilled"] == 5
    assert report["remaining"] == 0


# ── 4. --dry-run writes nothing ─────────────────────────────────────────


def test_dry_run_writes_nothing(tmp_path):
    rows = [(f"g{i}", f"content {i}", None) for i in range(3)]
    db = _make_genes_db(tmp_path, rows)
    before = _embeddings(db)
    fake = _FakeCodec()

    report = backfill_sema_db(db, dry_run=True, codec=fake, log_fn=lambda _m: None)

    assert report["dry_run"] is True
    assert report["backfilled"] == 0
    assert report["skipped"] == 0
    assert report["total_candidates"] == 3
    assert report["remaining"] == 3
    assert _embeddings(db) == before, "dry-run must not mutate any row"
    assert fake.batch_calls == 0, "dry-run must never invoke the codec"


def test_cli_dry_run_wires_args(tmp_path, monkeypatch, capsys):
    """main() parses --db/--dry-run and calls through without needing a
    real codec (dry-run short-circuits before codec resolution).
    """
    rows = [("g0", "some content", None)]
    db = _make_genes_db(tmp_path, rows)

    monkeypatch.setattr(sys, "argv", ["backfill_sema.py", "--db", db, "--dry-run"])
    rc = bs.main()

    assert rc == 0
    assert _embeddings(db) == {"g0": None}


# ── 5. batch-failure skip continues ──────────────────────────────────────


def test_batch_failure_is_soft_and_continues(tmp_path):
    """One failing batch is logged and skipped; later batches still run."""
    rows = [(f"g{i}", f"content {i}", None) for i in range(4)]
    db = _make_genes_db(tmp_path, rows)
    flaky = _FlakyCodec()

    # batch=1 -> 4 separate batch calls; the first raises, the rest succeed.
    report = backfill_sema_db(db, batch=1, codec=flaky, log_fn=lambda _m: None)

    assert flaky.calls == 4
    assert report["backfilled"] == 3
    assert report["skipped"] == 1
    assert report["remaining"] == 1
    embeddings = _embeddings(db)
    assert embeddings["g0"] is None, "the failed batch's row stays NULL"
    assert all(embeddings[f"g{i}"] is not None for i in (1, 2, 3))


def test_all_batches_failing_reports_full_skip(tmp_path):
    rows = [(f"g{i}", f"content {i}", None) for i in range(3)]
    db = _make_genes_db(tmp_path, rows)
    exploding = _ExplodingCodec()

    report = backfill_sema_db(db, codec=exploding, log_fn=lambda _m: None)

    assert report["backfilled"] == 0
    assert report["skipped"] == 3
    assert report["remaining"] == 3
    assert all(v is None for v in _embeddings(db).values())


def test_malformed_batch_payload_is_soft_skipped(tmp_path):
    """A codec that returns the wrong number of vectors is treated like a
    failure — skip the batch, don't crash, don't write partial garbage.
    """
    rows = [(f"g{i}", f"content {i}", None) for i in range(2)]
    db = _make_genes_db(tmp_path, rows)
    wrong_shape = _WrongShapeCodec()

    report = backfill_sema_db(db, codec=wrong_shape, log_fn=lambda _m: None)

    assert report["backfilled"] == 0
    assert report["skipped"] == 2
    assert all(v is None for v in _embeddings(db).values())


# ── extra: blank content is skipped, not crashed on ──────────────────────


def test_blank_content_rows_skipped(tmp_path):
    db = _make_genes_db(
        tmp_path,
        [("g0", "real body", None), ("g1", "   ", None), ("g2", "", None)],
    )
    fake = _FakeCodec()

    report = backfill_sema_db(db, codec=fake, log_fn=lambda _m: None)

    embeddings = _embeddings(db)
    assert embeddings["g0"] is not None
    assert embeddings["g1"] is None
    assert embeddings["g2"] is None
    assert report["backfilled"] == 1
    assert report["skipped"] == 2
    assert fake.encoded_texts == ["real body"]

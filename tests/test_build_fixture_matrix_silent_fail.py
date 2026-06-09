"""Regression for the silent-swallow bug in ``_chunk_and_tag_file``.

Bug story (2026-05-23): a sharded build of the 500K EnterpriseRAG corpus
completed in 550 s with all nine shards reporting **0 genes**. Root cause
was ``spacy`` missing in the bench venv, which made ``tagger.pack(...)``
raise ``ModuleNotFoundError`` for every strand. The exception was
silently swallowed by ``try/except Exception: pass`` in
``_chunk_and_tag_file`` — no log, no error counter visible at the
fixture-builder level — so the build "succeeded" while producing an
empty DB.

The fix: log the first occurrence of any exception class raised by
``tagger.pack`` (warning level, once per process per type) so that a
missing dependency or other systemic failure is visible immediately.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest


# Make scripts/ importable so we can import build_fixture_matrix.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "scripts")
)


def _make_simple_text_file(tmp_path: Path) -> Path:
    """Write a small .txt file the chunker can split into strands."""
    p = tmp_path / "sample.txt"
    p.write_text(
        "The quick brown fox jumps over the lazy dog. " * 40,
        encoding="utf-8",
    )
    return p


def test_chunk_and_tag_logs_when_tagger_pack_raises(tmp_path, caplog, monkeypatch):
    """When ``tagger.pack`` raises for every strand of a file,
    ``_chunk_and_tag_file`` should:

    1. Still return ``[]`` (preserve the empty-list contract so the
       drain skips this file as an error rather than crashing).
    2. Emit at least one WARNING-level log on the ``bench.matrix``
       logger so the failure is visible — even though every strand
       individually was swallowed.

    Currently (pre-fix) the function uses ``try/except Exception: pass``
    around the per-strand pack, so no log is emitted and 100 % of a
    corpus can silently degrade to 0 genes. This test pins the desired
    post-fix behaviour.
    """
    import build_fixture_matrix as bfm

    # Init worker globals (chunker + tagger) using the real venv.
    # We don't care which tagger backend is loaded — we replace
    # ``.pack`` below.
    bfm._init_worker()
    assert bfm._worker_chunker is not None
    assert bfm._worker_tagger is not None

    # Drop any "logged once" guards from prior tests so we observe the
    # fresh emission. (The guard is a fix-side detail; tests that exist
    # before the fix will not have it, which is fine — `getattr` shields
    # us.)
    guard = getattr(bfm, "_logged_pack_errors", None)
    if guard is not None:
        guard.clear()

    class _BoomTagger:
        """Stub: every .pack call raises a distinctive runtime error."""

        def pack(self, *args, **kwargs):
            raise RuntimeError("test-induced tagger.pack failure")

    monkeypatch.setattr(bfm, "_worker_tagger", _BoomTagger())

    sample = _make_simple_text_file(tmp_path)

    with caplog.at_level(logging.WARNING, logger="bench.matrix"):
        result = bfm._chunk_and_tag_file((str(sample), ".txt"))

    # Contract 1: empty list returned (drain will count this as a
    # file-with-errors instead of crashing the build).
    assert result == [], (
        "expected empty gene list when tagger.pack raises on every strand; "
        f"got {len(result)} genes"
    )

    # Contract 2: at least one warning was emitted on bench.matrix.
    bench_warnings = [
        r for r in caplog.records
        if r.name == "bench.matrix" and r.levelno >= logging.WARNING
    ]
    assert bench_warnings, (
        "expected at least one WARNING on the bench.matrix logger when "
        "tagger.pack raises; got nothing — silent-swallow bug is back. "
        f"All captured records: {[(r.name, r.levelname, r.message) for r in caplog.records]}"
    )

    # Contract 3: the warning mentions the underlying exception class
    # so the user can immediately see what's missing (e.g.
    # ``ModuleNotFoundError`` for the original spaCy case).
    combined = " ".join(r.getMessage() for r in bench_warnings)
    assert "RuntimeError" in combined or "test-induced tagger.pack failure" in combined, (
        f"warning didn't surface the underlying exception; got: {combined!r}"
    )


def test_drain_logs_when_gene_construction_fails(caplog):
    """Pinning regression for the second silent-counter in this file:
    ``_drain_with_batched_splade`` used to do ``except Exception:
    stats["errors"] += 1`` around ``Gene(**gd)`` with no log. If the
    schema drifts or a worker returns malformed dicts, the build
    silently degrades. After the 2026-05-23 fix the drain logs the
    first occurrence of each exception class on ``bench.matrix``.

    This test stays off the SPLADE/GPU path by feeding malformed gene
    dicts so Gene construction fails — ``buf`` stays empty and
    ``_flush`` returns early without calling ``splade_backend``.
    """
    import time
    import build_fixture_matrix as bfm

    bad_gene_dicts = [
        {"this": "is not", "a valid": "Gene shape"},
        {"definitely": "missing required fields"},
    ]

    stats = {"genes": 0, "errors": 0, "files": 0,
             "t0": time.perf_counter()}

    class _UnusedGenome:
        def upsert_doc(self, *args, **kwargs):
            raise AssertionError(
                "upsert_doc should not be called when Gene construction fails"
            )

    with caplog.at_level(logging.WARNING, logger="bench.matrix"):
        bfm._drain_with_batched_splade(
            iter([bad_gene_dicts]),
            _UnusedGenome(),
            stats,
            batch_size=64,
        )

    # The error counter still ticks (preserves the existing contract).
    assert stats["errors"] >= 1, (
        f"expected errors counter to tick on bad gene dicts; "
        f"got stats={stats!r}"
    )

    # And a warning is emitted on bench.matrix for the Gene stage.
    bench_warnings = [
        r for r in caplog.records
        if r.name == "bench.matrix" and r.levelno >= logging.WARNING
    ]
    assert bench_warnings, (
        "expected at least one WARNING when Gene(**gd) raises; "
        f"got nothing. All records: {[(r.name, r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    combined = " ".join(r.getMessage() for r in bench_warnings)
    assert "drain Gene" in combined, (
        f"warning didn't tag the failing stage; got: {combined!r}"
    )


def test_chunk_and_tag_logs_once_per_exception_type(tmp_path, caplog, monkeypatch):
    """Across multiple files that all fail the same way, we should log
    once (not per-file) so a 500K-file run doesn't drown the operator
    in 500K identical warnings."""
    import build_fixture_matrix as bfm

    bfm._init_worker()
    guard = getattr(bfm, "_logged_pack_errors", None)
    if guard is not None:
        guard.clear()

    class _BoomTagger:
        def pack(self, *args, **kwargs):
            raise RuntimeError("same failure each time")

    monkeypatch.setattr(bfm, "_worker_tagger", _BoomTagger())

    # Three identical-failure files
    files = []
    for i in range(3):
        p = tmp_path / f"file{i}.txt"
        p.write_text("hello world " * 50, encoding="utf-8")
        files.append(p)

    with caplog.at_level(logging.WARNING, logger="bench.matrix"):
        for f in files:
            bfm._chunk_and_tag_file((str(f), ".txt"))

    bench_warnings = [
        r for r in caplog.records
        if r.name == "bench.matrix" and r.levelno >= logging.WARNING
    ]
    # Exactly one (per-type rate limit). Allow up to 2 in case the
    # warning is emitted per-call-site rather than per-exc-type, but
    # 3+ would mean no rate-limit at all.
    assert 1 <= len(bench_warnings) <= 2, (
        "expected at most 2 warnings across 3 identical failures (one "
        f"per exception type), got {len(bench_warnings)}: "
        f"{[r.getMessage() for r in bench_warnings]}"
    )

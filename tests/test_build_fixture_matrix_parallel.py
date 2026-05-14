"""Parity test for issue #92 parallel ingest.

Builds the same small synthetic corpus twice -- once sequentially, once
with the new ``--parallel`` writer + ``mp.Pool`` workers -- and asserts
the resulting gene_ids and content hashes are identical.
"""

from __future__ import annotations

import sys
import sqlite3
from pathlib import Path

import pytest

# Make scripts/ importable.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "scripts")
)


def _populate_tree(root: Path, n_files: int = 6) -> None:
    """Write a tiny deterministic corpus."""
    root.mkdir(parents=True, exist_ok=True)
    bodies = [
        "def alpha():\n    return 'A' * 200\n" * 4,
        "# header\nbeta = 12345\n" * 8,
        "class Gamma:\n    def m(self):\n        pass\n" * 5,
        "// js\nconst delta = () => 7;\n" * 6,
        "{\"epsilon\": [1, 2, 3, 4, 5, 6, 7, 8]}\n" * 4,
        "phi: zeta\nrho: theta\n" * 10,
    ]
    suffixes = [".py", ".py", ".py", ".js", ".json", ".yaml"]
    for i in range(n_files):
        (root / f"f{i}{suffixes[i % len(suffixes)]}").write_text(
            bodies[i % len(bodies)], encoding="utf-8",
        )


def _collect_gene_summary(db_path: Path) -> set[tuple[str, str]]:
    """Return {(gene_id, content_hash)} for every gene in db."""
    import hashlib
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT gene_id, content FROM genes").fetchall()
    conn.close()
    return {
        (gid, hashlib.sha256(content.encode("utf-8")).hexdigest())
        for gid, content in rows
    }


@pytest.mark.slow
def test_parallel_matches_sequential(tmp_path, monkeypatch):
    """build_profile(parallel=False) and (parallel=True) should produce
    identical gene_ids + content hashes for the same corpus."""
    import build_fixture_matrix as bfm

    corpus = tmp_path / "corpus"
    _populate_tree(corpus, n_files=6)

    monkeypatch.setattr(bfm, "PROFILES", {
        "tiny92": {
            "label": "issue #92 parity test corpus",
            "active_roots": 1,
            "roots": [str(corpus)],
            "extra_skip_dirs": set(),
            "extra_filename_filters": [],
        }
    })

    seq_db = tmp_path / "seq.db"
    par_db = tmp_path / "par.db"

    bfm.build_profile("tiny92", str(seq_db), parallel=False)
    bfm.build_profile(
        "tiny92", str(par_db),
        parallel=True, n_workers=2, batch_size=8, chunksize=1,
    )

    seq = _collect_gene_summary(seq_db)
    par = _collect_gene_summary(par_db)

    assert seq == par, (
        f"gene-id/content mismatch\n"
        f"  only in seq: {sorted(seq - par)[:5]}...\n"
        f"  only in par: {sorted(par - seq)[:5]}..."
    )

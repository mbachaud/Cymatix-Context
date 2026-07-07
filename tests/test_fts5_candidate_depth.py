"""#205 / A4: FTS5 candidate-pool-depth knob.

The SIKE bedsweep found the xl bed (~42k genes) retrieval-capped: the
Tier-3 FTS5 content search fetches only ``max_genes * 4`` rows (48 at the
shipped ``max_genes=12``) ordered by BM25 rank, so a gold document ranked
below position 48 never enters tier scoring and can never be delivered —
"candidate-pool starvation" (issue-resolutions doc A4). ``fts5_candidate_depth``
(config ``[retrieval]``) overrides ONLY that raw FTS fetch depth. The
returned pool (``max_genes * 2``) and delivery cap (``max_genes``) are
untouched, so a deeper pool cannot inflate gold_delivered by itself — it can
only let a starved gold document ENTER scoring where the other tiers may
float it into the delivered top-K. That decoupling is what makes the Run-2
depth sweep (48 -> 200 -> 500) a clean starvation-vs-rank-squeeze probe.

The FTS fetch depth is observed via ``sqlite3`` ``set_trace_callback``, which
expands the bound ``LIMIT`` into the traced statement text.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from helix_context.config import load_config
from helix_context.genome import Genome
from helix_context.schemas import (
    ChromatinState, EpigeneticMarkers, Gene, PromoterTags,
)

_FTS_LIMIT_RE = __import__("re").compile(
    r"genes_fts\s+MATCH.*?LIMIT\s+(\d+)", __import__("re").IGNORECASE | __import__("re").DOTALL
)


def _mk(content: str, gid: str) -> Gene:
    return Gene(
        gene_id=gid, content=content, complement="", codons=[],
        promoter=PromoterTags(domains=["widgetdom"], entities=[]),
        epigenetics=EpigeneticMarkers(), chromatin=ChromatinState.OPEN,
        is_fragment=False,
    )


def _fts_limit_for(depth: int, *, max_genes: int = 12) -> int:
    """Run a lexical query with the given depth override and return the
    LIMIT the Tier-3 FTS5 content query actually used."""
    g = Genome(
        path=":memory:",
        dense_embedding_enabled=False,
        bm25_shortlist_enabled=False,
        fts5_candidate_depth=depth,
    )
    try:
        for i in range(60):  # > any tested depth's worth of matching rows
            g.upsert_gene(_mk(f"widget alpha content body number {i}", f"g{i}"))
        seen: list[str] = []
        g.read_conn.set_trace_callback(seen.append)
        g.query_docs(domains=["widget"], entities=[], max_genes=max_genes)
        limits = [int(m.group(1)) for s in seen if (m := _FTS_LIMIT_RE.search(s))]
        assert limits, "no genes_fts MATCH ... LIMIT statement was traced"
        return max(limits)
    finally:
        g.read_conn.set_trace_callback(None)
        g.close()


def test_default_depth_is_legacy_max_genes_times_four():
    """Unset (0) => legacy behavior: FTS fetch depth == max_genes * 4 (48)."""
    assert _fts_limit_for(0, max_genes=12) == 48


def test_override_widens_fts_fetch_depth():
    """Override sets the FTS fetch depth verbatim, independent of max_genes."""
    assert _fts_limit_for(200, max_genes=12) == 200
    assert _fts_limit_for(500, max_genes=12) == 500


def test_override_is_independent_of_max_genes():
    """The override does NOT scale with max_genes (that is the whole point —
    it decouples candidate depth from the delivery cap)."""
    assert _fts_limit_for(200, max_genes=4) == 200
    assert _fts_limit_for(200, max_genes=24) == 200


def test_config_round_trip(tmp_path):
    """[retrieval] fts5_candidate_depth parses off a TOML file."""
    toml = tmp_path / "helix.toml"
    toml.write_text("[retrieval]\nfts5_candidate_depth = 300\n", encoding="utf-8")
    cfg = load_config(str(toml))
    assert cfg.retrieval.fts5_candidate_depth == 300


def test_config_default_is_zero():
    """Absent key => 0 (auto/legacy)."""
    cfg = load_config(None)
    assert cfg.retrieval.fts5_candidate_depth == 0

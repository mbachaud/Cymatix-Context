"""Issues #222 (per-shard fetch depth) + #223 (co-activation reserved budget).

Mechanism-level tests. Both fixes are env-gated with legacy defaults:
flag-off behaviour must be byte-identical to the pre-fix build; corpus-level
recall validation is the fixture-gated bench step tracked on the issues.
"""
from __future__ import annotations

import os

import pytest

from helix_context.shard_router import (
    _apply_coact_reserve,
    _coact_reserve_slots,
    _shard_fetch_factor,
)


@pytest.fixture(autouse=True)
def _clean_env():
    saved = {
        k: os.environ.pop(k, None)
        for k in ("HELIX_SHARD_FETCH_FACTOR", "HELIX_SHARD_COACT_RESERVE")
    }
    yield
    for k, v in saved.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v


# ── #222: fetch-depth factor ─────────────────────────────────────────

def test_fetch_factor_default_is_legacy_2():
    assert _shard_fetch_factor() == 2


@pytest.mark.parametrize("raw,expected", [
    ("4", 4), ("6", 6), ("1", 1),
    ("0", 1),          # clamped to >=1 — never fetch nothing
    ("-3", 1),
    ("garbage", 2),    # unparseable keeps legacy
    ("", 2),
])
def test_fetch_factor_env_parsing(raw, expected):
    os.environ["HELIX_SHARD_FETCH_FACTOR"] = raw
    assert _shard_fetch_factor() == expected


def test_fetch_depth_formula_matches_router_contract():
    """per_shard_fetch = max(max_genes, max_genes * factor) — the #222 lever.

    With the legacy factor the per-shard cut equals the global return
    size; any post-fetch rescale (m_shard up to IDF_CLIP_HI=3.0,
    doc-type boost, global-IDF splice) can promote a doc that this cut
    already dropped. factor=4 doubles the headroom.
    """
    max_genes = 8
    os.environ["HELIX_SHARD_FETCH_FACTOR"] = "4"
    assert max(max_genes, max_genes * _shard_fetch_factor()) == 32
    os.environ.pop("HELIX_SHARD_FETCH_FACTOR")
    assert max(max_genes, max_genes * _shard_fetch_factor()) == 16


# ── #223: co-activation reserved budget ──────────────────────────────

def _mk_union(n: int, promoted_at: list[int]) -> tuple[list, set, dict, dict]:
    """Synthetic sorted union: doc i has corrected score n-i (descending)."""
    union = [f"g{i}" for i in range(n)]
    corrected = {f"g{i}": float(n - i) for i in range(n)}
    rrf = {f"g{i}": 0.0 for i in range(n)}
    promoted = {f"g{i}" for i in promoted_at}
    return union, promoted, corrected, rrf


def test_reserve_zero_is_plain_truncation():
    union, promoted, corrected, rrf = _mk_union(6, promoted_at=[5])
    out = _apply_coact_reserve(union, promoted, corrected, rrf, limit=3, reserve=0)
    assert out == ["g0", "g1", "g2"]  # byte-identical legacy cut


def test_reserve_rescues_displaced_linked_doc():
    """The #223 bug in miniature: promoted doc g5 (0.5x-discounted score)
    falls below the cut and is displaced; reserve=1 swaps out the weakest
    non-promoted survivor for it."""
    union, promoted, corrected, rrf = _mk_union(6, promoted_at=[5])
    out = _apply_coact_reserve(union, promoted, corrected, rrf, limit=3, reserve=1)
    assert "g5" in out
    assert len(out) == 3
    assert out == ["g0", "g1", "g5"]  # weakest non-promoted (g2) dropped


def test_reserve_never_drops_promoted_already_in_cut():
    union, promoted, corrected, rrf = _mk_union(6, promoted_at=[1, 5])
    out = _apply_coact_reserve(union, promoted, corrected, rrf, limit=3, reserve=2)
    assert "g1" in out and "g5" in out
    assert len(out) == 3


def test_reserve_satisfied_in_cut_is_noop():
    union, promoted, corrected, rrf = _mk_union(6, promoted_at=[0, 1])
    out = _apply_coact_reserve(union, promoted, corrected, rrf, limit=3, reserve=2)
    assert out == ["g0", "g1", "g2"]  # already >= reserve promoted in cut


def test_reserve_bounded_by_available_promoted():
    union, promoted, corrected, rrf = _mk_union(6, promoted_at=[4])
    out = _apply_coact_reserve(union, promoted, corrected, rrf, limit=3, reserve=3)
    # Only one promoted doc exists below the cut — swap exactly one.
    assert "g4" in out and len(out) == 3


def test_reserve_output_stays_sorted_by_corrected():
    union, promoted, corrected, rrf = _mk_union(8, promoted_at=[6, 7])
    out = _apply_coact_reserve(union, promoted, corrected, rrf, limit=4, reserve=2)
    scores = [corrected[g] for g in out]
    assert scores == sorted(scores, reverse=True)
    assert len(out) == 4


def test_reserve_env_parsing():
    assert _coact_reserve_slots() == 0
    os.environ["HELIX_SHARD_COACT_RESERVE"] = "2"
    assert _coact_reserve_slots() == 2
    os.environ["HELIX_SHARD_COACT_RESERVE"] = "junk"
    assert _coact_reserve_slots() == 0

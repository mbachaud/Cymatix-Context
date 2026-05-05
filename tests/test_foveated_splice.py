"""Tests for foveated-splice — rank-scaled BROAD compression schedule.

Spec: docs/specs/2026-05-03-foveated-splice-design.md
Plan: docs/plans/2026-05-05-foveated-splice.md
"""
import pytest

from helix_context.context_manager import _compute_foveated_caps


class TestComputeFoveatedCaps:
    """Pure-function tests for the schedule-shape helper (spec §4.1)."""

    def test_alpha_one_n_twelve_matches_spec_table(self):
        """Spec §10 Test 6: caps[0]=1.0, caps[1]=0.5, caps[5]≈1/6, caps[11]=c_min."""
        caps = _compute_foveated_caps(n=12, alpha=1.0, c_min=0.15, c_max=1.0)
        assert len(caps) == 12
        assert caps[0] == 1.0
        assert caps[1] == 0.5
        assert caps[5] == pytest.approx(1 / 6, abs=1e-9)
        # rank-12 → 1/12 ≈ 0.083 < c_min=0.15, so floor wins
        assert caps[11] == 0.15

    def test_alpha_two_collapses_to_floor_by_rank_three(self):
        """Spec §10 Test 8: α=2 floors caps[5..11] at c_min."""
        caps = _compute_foveated_caps(n=12, alpha=2.0, c_min=0.15, c_max=1.0)
        # 1/3^2 = 0.111 < 0.15 → already floored at rank 3
        for i in range(2, 12):
            assert caps[i] == 0.15, f"caps[{i}]={caps[i]} expected 0.15"

    def test_alpha_half_gentle_decay(self):
        """Spec §4.2 table: α=0.5 caps[1] ≈ 0.71."""
        caps = _compute_foveated_caps(n=12, alpha=0.5, c_min=0.15, c_max=1.0)
        assert caps[0] == 1.0
        assert caps[1] == pytest.approx(1 / (2 ** 0.5), abs=1e-9)  # ≈ 0.7071
        assert caps[2] == pytest.approx(1 / (3 ** 0.5), abs=1e-9)  # ≈ 0.5774

    def test_c_min_floor_is_inclusive_lower_bound(self):
        """A custom c_min raises the floor for low-rank genes."""
        caps = _compute_foveated_caps(n=12, alpha=1.0, c_min=0.30, c_max=1.0)
        assert caps[0] == 1.0
        # rank-4 → 0.25 < c_min=0.30, floors at 0.30
        for i in range(3, 12):
            assert caps[i] == 0.30

    def test_n_one_returns_single_c_max(self):
        """Edge case: a single-candidate BROAD set returns [c_max]."""
        assert _compute_foveated_caps(n=1, alpha=1.0, c_min=0.15) == [1.0]

    def test_n_zero_returns_empty(self):
        """Edge case: empty input → empty output, never raises."""
        assert _compute_foveated_caps(n=0, alpha=1.0, c_min=0.15) == []

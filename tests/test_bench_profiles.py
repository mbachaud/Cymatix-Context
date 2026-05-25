"""Untrusted-fixture exclusion for the claude-matrix bench (issue #133).

The xl-sharded fixture's `projects` shard was built from a polluted
source tree -- F:/Projects/_worktrees/helix-context/* PR-branch worktrees
plus a helix-retrieval-upgrade clone, with no canonical helix-context/
checkout. The needles' gold_source labels cannot resolve there, so
retr_hit / MRR on xl-sharded are not meaningful (correctness decoupled
from retrieval -- the 4-correct-with-gold / 17-correct-without inversion).

`resolve_profiles` drops UNTRUSTED_PROFILES from a default run;
`--include-untrusted`, or naming the profile explicitly in `--only`,
forces it back in.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH_DIR))

from bench_claude_matrix import UNTRUSTED_PROFILES, resolve_profiles  # noqa: E402

ALL = ["small", "medium", "large", "xl", "medium-sharded", "xl-sharded"]


def test_xl_sharded_is_marked_untrusted():
    assert "xl-sharded" in UNTRUSTED_PROFILES


def test_default_run_drops_untrusted_profiles():
    keys = resolve_profiles(ALL, only=set(), skip=set(),
                            include_untrusted=False)
    assert "xl-sharded" not in keys
    assert keys == ["small", "medium", "large", "xl", "medium-sharded"]


def test_include_untrusted_flag_keeps_them():
    keys = resolve_profiles(ALL, only=set(), skip=set(),
                            include_untrusted=True)
    assert keys == ALL


def test_explicit_only_overrides_untrusted_exclusion():
    """Naming an untrusted profile in --only is an explicit operator request."""
    keys = resolve_profiles(ALL, only={"xl-sharded"}, skip=set(),
                            include_untrusted=False)
    assert keys == ["xl-sharded"]


def test_only_without_untrusted_is_unaffected():
    keys = resolve_profiles(ALL, only={"small", "medium"}, skip=set(),
                            include_untrusted=False)
    assert keys == ["small", "medium"]


def test_skip_still_applies_and_does_not_resurrect_untrusted():
    keys = resolve_profiles(ALL, only=set(), skip={"small"},
                            include_untrusted=False)
    assert keys == ["medium", "large", "xl", "medium-sharded"]


def test_profile_order_is_preserved():
    keys = resolve_profiles(ALL, only=set(), skip=set(),
                            include_untrusted=True)
    assert keys == ALL

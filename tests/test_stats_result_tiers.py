"""Test StatsResult correctly reads tier counts from stats() dict."""

from unittest.mock import MagicMock

import pytest

from cymatix_context.api import CymatixSession, StatsResult


def _make_session(manager=None):
    """Build a CymatixSession bound to a MagicMock manager."""
    manager = manager or MagicMock()
    return CymatixSession(manager=manager, session_id="sess-test-stats")


def test_stats_result_reads_tier_counts():
    """Verify StatsResult populates chromatin tier counts from stats dict.

    Regression test: api.py:341-343 was reading keys "chromatin_open" etc,
    but knowledge_store.stats() emits "open", "euchromatin", "heterochromatin".
    This caused all tier counts to be 0 and `cymatix diag corpus` to show zeros.
    """
    # Stats dict as emitted by knowledge_store.stats() (knowledge_store.py:5163-5176)
    raw_stats = {
        "total_genes": 100,
        "open": 30,
        "euchromatin": 50,
        "heterochromatin": 20,
        "total_chars_raw": 50000,
        "total_chars_compressed": 25000,
        "compression_ratio": 2.0,
        "compression_tiers": {
            "open_full": 10,
            "euchromatin_summary": 40,
            "heterochromatin_cold": 50,
        },
    }

    mgr = MagicMock()
    mgr.stats.return_value = raw_stats
    sess = _make_session(mgr)
    result = sess.stats()

    # Before the fix, these would all be 0 because api.py was reading
    # the wrong keys ("chromatin_open" instead of "open", etc.)
    assert result.chromatin_open == 30, \
        "chromatin_open should be 30 from stats['open']"
    assert result.chromatin_eu == 50, \
        "chromatin_eu should be 50 from stats['euchromatin']"
    assert result.chromatin_hetero == 20, \
        "chromatin_hetero should be 20 from stats['heterochromatin']"
    assert result.total_genes == 100
    assert result.compression_ratio == 2.0


def test_stats_result_backward_compat_old_keys():
    """Verify fallback to old key names for backward compatibility.

    If a stats() dict was built with the old key names ("chromatin_open" etc.),
    the fallback pattern should still work.
    """
    # Stats dict with old key names (before the fix)
    raw_stats = {
        "total_genes": 50,
        "chromatin_open": 15,
        "chromatin_euchromatin": 25,
        "chromatin_heterochromatin": 10,
        "compression_ratio": 1.5,
    }

    mgr = MagicMock()
    mgr.stats.return_value = raw_stats
    sess = _make_session(mgr)
    result = sess.stats()

    # Should fall back to old keys
    assert result.chromatin_open == 15
    assert result.chromatin_eu == 25
    assert result.chromatin_hetero == 10
    assert result.total_genes == 50

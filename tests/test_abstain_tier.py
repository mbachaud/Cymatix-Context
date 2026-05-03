"""Tests for the ABSTAIN tier — confidence-gated context attachment.

See docs/specs/2026-05-02-abstain-tier-design.md and
docs/plans/2026-05-02-abstain-tier.md.
"""

from helix_context import context_manager as cm


def test_abstain_marker_constant_is_exported():
    """The shared marker string is exposed at module scope so the empty-
    candidates branch and the abstain branch can ship identical bytes."""
    assert cm._ABSTAIN_MARKER == "(no relevant context found in genome)"

"""Bugbash BUG-2: packet trust must fold in the packet's own freshness verdict.

``build_context_packet`` labels every item verified / stale_risk /
needs_refresh, but ``_attach_know_or_miss`` never saw that verdict — a
packet containing ONLY ``needs_refresh`` evidence still shipped
``know=true`` at high confidence. The fix is surgical (issue #287 HOLD:
no gate redesign, no beta refit):

  * all items ``needs_refresh``  → thread ``freshness_status="stale"``
    into the existing Stage-7 branch, demoting to
    ``MissBlock(reason="stale")`` with the top source as refresh target;
  * no verified items at all     → any surviving KnowBlock is flagged
    ``soft_stale=True`` and its confidence capped at ``emit_floor``.
"""
from __future__ import annotations

import pytest

from helix_context.context_packet import build_context_packet
from helix_context.genome import Genome
from helix_context.scoring.know_calibration import KnowCalibration

from tests.conftest import make_gene


@pytest.fixture
def default_calibration(monkeypatch):
    """Pin the DEFAULT calibration (same rationale as the fixture in
    test_know_miss_block.py): the shipped helix.toml [know] betas are
    under the #287 HOLD and would mask the trust contract under test."""
    from helix_context.scoring import know_calibration as kc

    monkeypatch.setattr(
        kc, "load_calibration_from_toml", lambda *a, **k: kc.KnowCalibration()
    )


def test_needs_refresh_only_packet_does_not_report_confident_know(
    default_calibration,
):
    """A packet whose EVERY item is needs_refresh must not ship know=true.

    Reproduces the bugbash finding: hot-volatility config evidence far
    past its verification window under a high-risk task lands entirely in
    needs_refresh, yet the know gate — blind to the freshness verdict —
    reported know=true at ~0.96 confidence."""
    now_ts = 20_000.0
    genome = Genome(":memory:")
    try:
        gene = make_gene(
            "Auth config sets jwt ttl to fifteen minutes",
            domains=["auth", "config"],
        )
        gene.source_id = "/repo/config/auth.toml"
        gene.source_kind = "config"
        gene.volatility_class = "hot"
        gene.authority_class = "primary"
        gene.last_verified_at = now_ts - 4_000.0
        genome.upsert_gene(gene, apply_gate=False)

        packet = build_context_packet(
            "auth config",
            task_type="edit",
            genome=genome,
            now_ts=now_ts,
        )

        # Precondition: the packet's own verdict is needs_refresh-only.
        assert packet.verified == []
        assert packet.stale_risk
        assert all(i.status == "needs_refresh" for i in packet.stale_risk)

        # The freshness verdict must reach the trust decision: demote to
        # MissBlock(reason="stale") with the top source as refresh target.
        assert packet.know is None
        assert packet.miss is not None
        assert packet.miss.reason == "stale"
        assert "/repo/config/auth.toml" in packet.miss.refresh_targets
    finally:
        genome.close()


def test_unverified_only_packet_caps_confidence_and_flags_soft_stale(
    default_calibration,
):
    """No verified items (stale_risk-only) → a surviving KnowBlock must
    carry soft_stale=True and a confidence capped at emit_floor, so a
    downstream agent cannot read unverified-fresh evidence as
    high-confidence know=true."""
    now_ts = 200_000.0
    genome = Genome(":memory:")
    try:
        gene = make_gene(
            "Helix design notes for the agent index",
            domains=["helix", "design"],
        )
        gene.source_id = "/repo/docs/design.md"
        gene.source_kind = "doc"
        gene.volatility_class = "medium"
        gene.authority_class = "primary"
        # 1.5 half-lives old (medium = 12h) → freshness ≈ 0.22, inside
        # the stale_risk band [0.12, 0.35) for a non-high-risk task.
        gene.last_verified_at = now_ts - 64_800.0
        genome.upsert_gene(gene, apply_gate=False)

        packet = build_context_packet(
            "helix design",
            task_type="explain",
            genome=genome,
            now_ts=now_ts,
        )

        assert packet.verified == []
        assert packet.stale_risk
        assert any(i.status == "stale_risk" for i in packet.stale_risk)

        assert packet.know is not None, (
            f"expected a (capped) KnowBlock, got miss={packet.miss!r}"
        )
        assert packet.know.soft_stale is True
        assert packet.know.confidence <= KnowCalibration().emit_floor + 1e-9
    finally:
        genome.close()


def test_verified_packet_know_confidence_unaffected(default_calibration):
    """Regression guard: a packet with verified evidence keeps its
    uncapped confidence and soft_stale=False — the fix must not demote
    healthy packets."""
    now_ts = 10_000.0
    genome = Genome(":memory:")
    try:
        gene = make_gene(
            "Helix design notes for the agent index",
            domains=["helix", "design"],
        )
        gene.source_id = "/repo/docs/design.md"
        gene.source_kind = "doc"
        gene.volatility_class = "stable"
        gene.authority_class = "primary"
        gene.last_verified_at = now_ts - 120.0
        genome.upsert_gene(gene, apply_gate=False)

        packet = build_context_packet(
            "helix design",
            task_type="explain",
            genome=genome,
            now_ts=now_ts,
        )

        assert len(packet.verified) == 1
        assert packet.know is not None
        assert packet.know.soft_stale is False
        assert packet.know.confidence > KnowCalibration().emit_floor
    finally:
        genome.close()

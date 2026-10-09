"""[context] freshness_basis: verify on the source, not on the clock (#482).

Legacy ("clock", the default) ages every item from its last verification
time, so a static store goes all-``needs_refresh`` about 15 days after ingest
(7-day "stable" constant) and the packet path demotes every query to
``MissBlock(reason="stale")``. The /context path calls a source the store
cannot find on this disk "missing" and demotes that to "stale" too. On the
ERB 947k bed (ingested 2026-08-26, sources no longer on disk) that forced
470/470 queries to miss(stale) before confidence was ever read.

"source" asks the source instead:
  * unchanged on disk since verification -> verified (freshness 1.0)
  * changed on disk after verification   -> needs_refresh
  * not on this disk / no path / URL      -> freshness unknown (stale_risk for
    ordinary tasks, needs_refresh for high-risk ones, per _status_for), and
    the /context gate reads it as "unknown", not "stale"
"""
from __future__ import annotations

import os
import textwrap
from types import SimpleNamespace

import pytest

from cymatix_context.config import CymatixConfig, load_config
from cymatix_context.context_packet import build_context_packet
from cymatix_context.genome import Genome
from cymatix_context.retrieval.freshness import apply_freshness_basis

from tests.conftest import make_gene

DAY = 86_400.0


@pytest.fixture
def default_calibration(monkeypatch):
    from cymatix_context.scoring import know_calibration as kc

    monkeypatch.setattr(kc, "load_calibration_from_toml", lambda *a, **k: kc.KnowCalibration())


# ── config ────────────────────────────────────────────────────────────

def test_freshness_basis_defaults_to_clock():
    assert CymatixConfig().context.freshness_basis == "clock"


def test_freshness_basis_source_is_read(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[context]\nfreshness_basis = "source"\n', encoding="utf-8")
    assert load_config(str(p)).context.freshness_basis == "source"


def test_freshness_basis_invalid_falls_back_to_clock(tmp_path, caplog):
    p = tmp_path / "c.toml"
    p.write_text('[context]\nfreshness_basis = "vibes"\n', encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_config(str(p)).context.freshness_basis == "clock"
    assert "freshness_basis" in caplog.text


# ── the policy ────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["fresh", "stale", "missing", "unknown", None])
def test_clock_basis_is_identity(status):
    assert apply_freshness_basis(status, "clock") == status


@pytest.mark.parametrize("status,expected", [
    ("fresh", "fresh"), ("stale", "stale"), ("missing", "unknown"),
    ("unknown", "unknown"), (None, None),
])
def test_source_basis_reads_missing_as_unknown(status, expected):
    assert apply_freshness_basis(status, "source") == expected


# ── packet path ───────────────────────────────────────────────────────

def _store_one(genome: Genome, source: str, last_verified: float, content: str):
    g = make_gene(content, domains=["ledger"], entities=["ledger"])
    g.source_id = source
    g.source_kind = "doc"
    g.volatility_class = "stable"
    g.authority_class = "primary"
    g.last_verified_at = last_verified
    genome.upsert_gene(g, apply_gate=False)


def _packet(genome, now_ts, basis):
    return build_context_packet("ledger reconciliation", task_type="explain", genome=genome,
                                now_ts=now_ts, freshness_basis=basis)


def test_unchanged_source_30_days_old(tmp_path, default_calibration):
    src = tmp_path / "ledger.md"
    src.write_text("ledger", encoding="utf-8")
    verified = os.stat(src).st_mtime + 1.0
    now_ts = verified + 30 * DAY
    genome = Genome(":memory:")
    try:
        _store_one(genome, str(src), verified, "Ledger reconciliation runs nightly at two")
        clock = _packet(genome, now_ts, "clock")
        assert clock.verified == [] and all(i.status == "needs_refresh" for i in clock.stale_risk)
        assert clock.miss is not None and clock.miss.reason == "stale"
        source = _packet(genome, now_ts, "source")
        assert [i.status for i in source.verified] == ["verified"]
        assert not (source.miss is not None and source.miss.reason == "stale")
    finally:
        genome.close()


def test_missing_source_is_unknown_not_needs_refresh(tmp_path, default_calibration):
    gone = str(tmp_path / "moved-away" / "ledger.md")
    now_ts = 1_800_000_000.0
    genome = Genome(":memory:")
    try:
        _store_one(genome, gone, now_ts - 42 * DAY, "Ledger reconciliation runs nightly at two")
        clock = _packet(genome, now_ts, "clock")
        assert clock.miss is not None and clock.miss.reason == "stale"
        source = _packet(genome, now_ts, "source")
        assert source.verified == []
        assert [i.status for i in source.stale_risk] == ["stale_risk"]
        assert not (source.miss is not None and source.miss.reason == "stale")
    finally:
        genome.close()


def test_changed_source_needs_refresh(tmp_path, default_calibration):
    src = tmp_path / "ledger.md"
    src.write_text("ledger v2", encoding="utf-8")
    verified = os.stat(src).st_mtime - 10.0  # file changed after verification
    genome = Genome(":memory:")
    try:
        _store_one(genome, str(src), verified, "Ledger reconciliation runs nightly at two")
        source = _packet(genome, verified + 60.0, "source")
        assert source.verified == []
        assert [i.status for i in source.stale_risk] == ["needs_refresh"]
    finally:
        genome.close()


# ── route wiring: the packet surfaces pass the loaded knob ─────────────

@pytest.mark.parametrize("body,path", [
    ({"query": "upstream port"}, "/context/packet"),
    ({"query": "upstream port", "response_mode": "packet"}, "/context"),
])
def test_packet_routes_pass_freshness_basis(monkeypatch, body, path):
    import cymatix_context.context_packet as cp
    from tests.conftest import MockCompressorBackend, make_client

    seen = {}
    real = cp.build_context_packet

    def spy(*a, **kw):
        seen["freshness_basis"] = kw.get("freshness_basis", "<not passed>")
        return real(*a, **kw)

    # The routes bind the name when the app is built, so patch first.
    monkeypatch.setattr(cp, "build_context_packet", spy)
    client = make_client(backend=MockCompressorBackend())
    app = client.app
    app.state.cymatix.genome.upsert_gene(make_gene("upstream_port = 11434", domains=["network"],
                                                   entities=["port"], gene_id="fb_seed_0000000001"))
    app.state.cymatix.config.context.freshness_basis = "source"
    with client:
        resp = client.post(path, json=body)
    assert resp.status_code == 200, resp.text
    assert seen["freshness_basis"] == "source"


# ── /context path ─────────────────────────────────────────────────────

@pytest.mark.parametrize("basis,expected", [("clock", "missing"), ("source", "unknown")])
def test_context_route_missing_source(tmp_path, monkeypatch, basis, expected):
    import cymatix_context.server.helpers as helpers

    genome = Genome(":memory:")
    try:
        g = make_gene("Ledger reconciliation runs nightly at two", gene_id="g-led")
        g.source_id = str(tmp_path / "gone.md")
        g.last_verified_at = 1_700_000_000.0
        genome.upsert_gene(g, apply_gate=False)
        cfg = CymatixConfig()
        cfg.context.freshness_basis = basis
        mgr = SimpleNamespace(genome=genome, config=cfg, _mtime_cache={},
                              _cold_tier_peek=lambda q, k=3, min_cosine=0.4: [],
                              _last_cold_peek_targets=[])
        window = SimpleNamespace(
            expressed_gene_ids=["g-led"], retrieval_scores={"g-led": 1.0}, tier_contributions={},
            context_health=SimpleNamespace(freshness_min=None, genes_expressed=1, status="aligned"),
        )
        seen = {}
        real = helpers.decide_know_or_miss

        def spy(window=None, **kw):
            seen["freshness_status"] = kw.get("freshness_status")
            return real(window=window, **kw)

        monkeypatch.setattr(helpers, "decide_know_or_miss", spy)
        helpers._compute_know_or_miss_block(cymatix=mgr, window=window, query="ledger")
        assert seen["freshness_status"] == expected
    finally:
        genome.close()

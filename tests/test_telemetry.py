import logging

from helix_context.genome import Genome
from helix_context.shard_schema import init_main_db, open_main_db, register_shard
from helix_context.sharding import ShardedGenomeAdapter
from helix_context.telemetry import emit_gauges_snapshot

from tests.conftest import make_gene


class _GaugeRecorder:
    def __init__(self):
        self.calls = []

    def set(self, value, attrs=None):
        self.calls.append((value, attrs or {}))


def test_emit_gauges_snapshot_reads_registered_shards_without_warning(
    tmp_path, caplog, monkeypatch
):
    main_path = tmp_path / "main.genome.db"
    shard_path = tmp_path / "projects.genome.db"

    main_conn = open_main_db(str(main_path))
    init_main_db(main_conn)

    shard = Genome(str(shard_path))
    try:
        shard.upsert_gene(
            make_gene("open auth gene", domains=["auth"]),
            apply_gate=False,
        )
        shard.upsert_gene(
            make_gene(
                "cold auth gene",
                domains=["auth"],
                chromatin=2,
            ),
            apply_gate=False,
        )

        register_shard(
            main_conn,
            shard_name="projects",
            category="reference",
            path=str(shard_path),
            gene_count=2,
            byte_size=shard_path.stat().st_size,
        )

        chrom = _GaugeRecorder()
        edges = _GaugeRecorder()
        size = _GaugeRecorder()
        hub = _GaugeRecorder()
        degree = _GaugeRecorder()

        monkeypatch.setattr("helix_context.telemetry.chromatin_state_counter", lambda: chrom)
        monkeypatch.setattr("helix_context.telemetry.harmonic_edges_counter", lambda: edges)
        monkeypatch.setattr("helix_context.telemetry.genome_size_gauge", lambda: size)
        monkeypatch.setattr("helix_context.telemetry.hub_concentration_gauge", lambda: hub)
        monkeypatch.setattr("helix_context.telemetry.hub_inbound_degree_gauge", lambda: degree)

        adapter = ShardedGenomeAdapter(str(main_path))
        try:
            with caplog.at_level(logging.WARNING, logger="helix.telemetry"):
                emit_gauges_snapshot(adapter)
        finally:
            adapter.close()

        assert "emit_gauges_snapshot failed" not in caplog.text
        assert (1, {"state": "open"}) in chrom.calls
        assert (1, {"state": "heterochromatin"}) in chrom.calls
        assert any(attrs == {"kind": "raw"} and value > 0 for value, attrs in size.calls)
        assert any(
            attrs == {"kind": "compressed"} and value > 0 for value, attrs in size.calls
        )
        assert edges.calls == []
        assert hub.calls == []
        assert degree.calls == []
    finally:
        shard.close()
        main_conn.close()

import logging
from unittest.mock import MagicMock

import pytest

from helix_context.genome import Genome
from helix_context.shard_schema import init_main_db, open_main_db, register_shard
from helix_context.sharding import ShardedGenomeAdapter
from helix_context.telemetry import (
    _attach_otlp_logging_handler,
    _resolve_logs_level,
    emit_gauges_snapshot,
)

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

        monkeypatch.setattr("helix_context.telemetry.otel.chromatin_state_counter", lambda: chrom)
        monkeypatch.setattr("helix_context.telemetry.otel.harmonic_edges_counter", lambda: edges)
        monkeypatch.setattr("helix_context.telemetry.otel.genome_size_gauge", lambda: size)
        monkeypatch.setattr("helix_context.telemetry.otel.hub_concentration_gauge", lambda: hub)
        monkeypatch.setattr("helix_context.telemetry.otel.hub_inbound_degree_gauge", lambda: degree)

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


# -- HELIX_OTEL_LOGS_* env-var toggle --------------------------------

def test_resolve_logs_level_accepts_canonical_names():
    assert _resolve_logs_level("INFO") == logging.INFO
    assert _resolve_logs_level("debug") == logging.DEBUG
    assert _resolve_logs_level("WARNING") == logging.WARNING
    assert _resolve_logs_level(None) == logging.INFO


def test_resolve_logs_level_falls_back_on_garbage():
    """Unknown level names log a warning and default to INFO rather than
    crashing the OTel setup path. Matches the soft-fail policy elsewhere
    in setup_telemetry — telemetry init is opt-in and must never block
    helix from serving /context."""
    assert _resolve_logs_level("LOUD") == logging.INFO
    assert _resolve_logs_level("") == logging.INFO


@pytest.mark.parametrize("env_value", ["0", "false", "no"])
def test_attach_otlp_logging_handler_skips_when_disabled(monkeypatch, env_value):
    """HELIX_OTEL_LOGS_ENABLED=0 (or any non-'1') keeps traces+metrics on
    but skips log shipment. The downstream LoggerProvider / handler
    constructors should never be called — keeps Loki disk pressure or
    PII concerns fully addressable without forcing a full telemetry
    shutdown."""
    monkeypatch.setenv("HELIX_OTEL_LOGS_ENABLED", env_value)

    LoggerProvider = MagicMock()
    BatchLogRecordProcessor = MagicMock()
    OTLPLogExporter = MagicMock()
    LoggingHandler = MagicMock()
    set_logger_provider = MagicMock()

    _attach_otlp_logging_handler(
        endpoint="localhost:4317",
        insecure=True,
        resource=MagicMock(),
        LoggerProvider=LoggerProvider,
        BatchLogRecordProcessor=BatchLogRecordProcessor,
        OTLPLogExporter=OTLPLogExporter,
        LoggingHandler=LoggingHandler,
        set_logger_provider=set_logger_provider,
    )
    LoggerProvider.assert_not_called()
    LoggingHandler.assert_not_called()
    OTLPLogExporter.assert_not_called()
    set_logger_provider.assert_not_called()


def test_attach_otlp_logging_handler_wires_root_when_enabled(monkeypatch):
    """Default path (HELIX_OTEL_LOGS_ENABLED unset → "1") builds the
    LoggerProvider and attaches a LoggingHandler to the root logger."""
    monkeypatch.delenv("HELIX_OTEL_LOGS_ENABLED", raising=False)
    monkeypatch.setenv("HELIX_OTEL_LOGS_LEVEL", "WARNING")

    # Real-shape stub so `type(resource).create({...})` resolves to a
    # classmethod the production code can actually call. MagicMock can't
    # provide that because `type(MagicMock())` is the bare MagicMock class.
    class FakeResource:
        @classmethod
        def create(cls, attrs):
            inst = cls()
            inst.attrs = attrs
            return inst

        def merge(self, other):
            merged = FakeResource()
            merged.attrs = {**getattr(self, "attrs", {}), **getattr(other, "attrs", {})}
            return merged

    fake_resource = FakeResource()
    LoggerProvider = MagicMock()
    BatchLogRecordProcessor = MagicMock()
    OTLPLogExporter = MagicMock()

    class FakeLoggingHandler:
        def __init__(self, level, logger_provider):
            self.level = level
            self.logger_provider = logger_provider
            self.filters = []

        def addFilter(self, f):
            self.filters.append(f)

    set_logger_provider = MagicMock()
    # Ensure no leftover FakeLoggingHandler from a prior test.
    root = logging.getLogger()
    prior = list(root.handlers)
    try:
        _attach_otlp_logging_handler(
            endpoint="localhost:4317",
            insecure=True,
            resource=fake_resource,
            LoggerProvider=LoggerProvider,
            BatchLogRecordProcessor=BatchLogRecordProcessor,
            OTLPLogExporter=OTLPLogExporter,
            LoggingHandler=FakeLoggingHandler,
            set_logger_provider=set_logger_provider,
        )
        # LoggerProvider received a merged Resource containing the
        # loki.attribute.labels hint that promotes `logger` to a Loki label.
        kwargs = LoggerProvider.call_args.kwargs
        assert "resource" in kwargs
        assert kwargs["resource"].attrs.get("loki.attribute.labels") == "logger"
        set_logger_provider.assert_called_once()
        attached = [h for h in root.handlers if isinstance(h, FakeLoggingHandler)]
        assert len(attached) == 1
        assert attached[0].level == logging.WARNING
    finally:
        # Restore root state so we don't leak handlers between tests.
        for h in list(root.handlers):
            if h not in prior:
                root.removeHandler(h)

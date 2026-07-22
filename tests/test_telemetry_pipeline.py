"""
Tests for per-stage pipeline telemetry (feat/per-stage-telemetry).

Covers:
  - _stage_timer records one observation with the right stage label
  - _stage_timer is silent when the histogram raises (noop path)
  - _NoopInstrument.record() is a silent no-op

Getter caching identity for pipeline_stage/ribosome_call/genome_signal
histograms lives in the consolidated registry smoke in
test_telemetry_wiring.py::test_all_new_getters_resolve.
"""

from __future__ import annotations

import cymatix_context.telemetry as telemetry_mod


# ── Helpers ──────────────────────────────────────────────────────────


class _RecordingInstrument:
    """Minimal histogram stand-in that captures record() calls."""

    def __init__(self):
        self.observations: list[tuple[float, dict]] = []

    def record(self, value: float, attributes: dict | None = None):
        self.observations.append((value, attributes or {}))


class _RaisingInstrument:
    """Histogram that raises on record() — exercises the noop guard."""

    def record(self, *args, **kwargs):
        raise RuntimeError("telemetry deliberately broken")


# ── Tests ────────────────────────────────────────────────────────────


def test_stage_timer_records_one_observation(monkeypatch):
    """_stage_timer.__exit__ records exactly one entry with the right stage."""
    from cymatix_context.context_manager import _stage_timer
    import cymatix_context.context_manager as cm_mod

    recorder = _RecordingInstrument()
    # _stage_timer calls the module-level _pipeline_stage_histogram name.
    monkeypatch.setattr(cm_mod, "_pipeline_stage_histogram", lambda: recorder)

    with _stage_timer("express"):
        pass  # no real work needed

    assert len(recorder.observations) == 1
    elapsed, attrs = recorder.observations[0]
    assert attrs.get("stage") == "express"
    assert elapsed >= 0.0, "elapsed must be non-negative"


def test_stage_timer_records_extra_labels(monkeypatch):
    """_stage_timer passes additional labels through to the histogram."""
    from cymatix_context.context_manager import _stage_timer

    recorder = _RecordingInstrument()
    import cymatix_context.context_manager as cm_mod
    monkeypatch.setattr(cm_mod, "_pipeline_stage_histogram", lambda: recorder)

    with _stage_timer("rerank", {"decoder_mode": "condensed"}):
        pass

    assert len(recorder.observations) == 1
    _, attrs = recorder.observations[0]
    assert attrs["stage"] == "rerank"
    assert attrs["decoder_mode"] == "condensed"


def test_stage_timer_swallows_telemetry_errors(monkeypatch):
    """_stage_timer must NOT raise if the histogram itself raises."""
    from cymatix_context.context_manager import _stage_timer

    import cymatix_context.context_manager as cm_mod
    monkeypatch.setattr(cm_mod, "_pipeline_stage_histogram",
                        lambda: _RaisingInstrument())

    # Should complete without raising even though the instrument is broken.
    with _stage_timer("assemble"):
        pass  # pipeline must continue unharmed


def test_noop_instruments_do_not_raise():
    """_NoopInstrument.record() must be a silent no-op — no raises allowed."""
    noop = telemetry_mod._NoopInstrument()
    noop.record(1.234, {"stage": "express"})  # must not raise
    noop.record(0.0)

"""Tests for issue #212 -- crawl watchdog: throughput-triggered escalation
ladder for the dense ingest/backfill paths.

NO GPU anywhere in this file. The detector is a pure class (callers
inject ``vram_frac``), so these tests drive it with a fake feed; the
wire-level tests stub the heavy collaborators the same way
``test_build_fixture_matrix.py::TestResume`` does. On a CPU-only box the
production probe ``cuda_vram_fraction()`` returns ``None``, which the
detector treats as "cannot trip".
"""

from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import pytest

# Make scripts/ importable.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "scripts")
)

import crawl_watchdog as cw
from crawl_watchdog import ACTION_DEMOTE, ACTION_EMPTY_CACHE, CrawlDetector


# -- helpers ---------------------------------------------------------------


def _detector(window=4, factor=5.0, action="ladder", **kw):
    """Detector with ``ema_alpha=1.0`` so the EMA equals the instantaneous
    rate -- crisp, batch-exact trip timing for tests. Production keeps the
    default 0.3 smoothing."""
    kw.setdefault("ema_alpha", 1.0)
    kw.setdefault("log_fn", lambda _msg: None)
    return CrawlDetector(window, factor, action=action, **kw)


def _warm(det, rate=10.0, vram=0.5):
    """Feed ``baseline_window`` healthy batches to establish the baseline."""
    for _ in range(det.baseline_window):
        assert det.feed(genes=rate, dt=1.0, vram_frac=vram) is None
    assert det.baseline == pytest.approx(rate)


# -- baseline establishment ------------------------------------------------


def test_baseline_is_median_of_first_window_batches():
    logs: list[str] = []
    det = CrawlDetector(5, 5.0, action="ladder", ema_alpha=1.0,
                        log_fn=logs.append)
    rates = [10.0, 30.0, 20.0, 1000.0, 15.0]  # median = 20
    for r in rates:
        # Warmup batches never trip, even with VRAM pinned: there is no
        # baseline to compare against yet.
        assert det.feed(genes=r, dt=1.0, vram_frac=0.99) is None
    assert det.baseline == pytest.approx(20.0)
    assert any(
        "[crawl-watchdog]" in m and "baseline established" in m for m in logs
    )


def test_zero_gene_or_zero_dt_batches_are_ignored():
    det = _detector(window=3)
    assert det.feed(genes=0, dt=1.0, vram_frac=0.99) is None
    assert det.feed(genes=10.0, dt=0.0, vram_frac=0.99) is None
    assert det.batches == 0
    assert det.baseline is None


# -- no-trip paths ----------------------------------------------------------


def test_no_trip_under_healthy_rate():
    det = _detector()
    _warm(det, rate=10.0)
    for _ in range(10 * det.baseline_window):
        assert det.feed(genes=10.0, dt=1.0, vram_frac=0.99) is None
    assert det.trips == 0
    assert det.streak == 0


def test_no_trip_when_vram_low_slow_disk_alone_is_not_a_crawl():
    det = _detector(window=4)
    _warm(det, rate=10.0)
    for _ in range(5 * det.baseline_window):
        assert det.feed(genes=0.1, dt=1.0, vram_frac=0.40) is None
    assert det.trips == 0


def test_no_trip_when_vram_unknown_cpu_only_box():
    """``cuda_vram_fraction()`` returns None on CPU-only boxes; the
    detector must treat that as 'structurally cannot trip'."""
    det = _detector(window=4)
    _warm(det, rate=10.0)
    for _ in range(5 * det.baseline_window):
        assert det.feed(genes=0.1, dt=1.0, vram_frac=None) is None
    assert det.trips == 0


# -- tripping + hysteresis ---------------------------------------------------


def test_trip_after_window_consecutive_slow_high_vram_batches():
    det = _detector(window=4)
    _warm(det, rate=10.0)  # threshold = 10 / 5 = 2.0 genes/s
    for i in range(3):
        assert det.feed(genes=0.5, dt=1.0, vram_frac=0.97) is None, (
            f"slow batch {i + 1}/4 must not trip yet"
        )
    assert det.feed(genes=0.5, dt=1.0, vram_frac=0.97) == ACTION_EMPTY_CACHE
    assert det.trips == 1


def test_hysteresis_recovery_resets_the_streak():
    det = _detector(window=4)
    _warm(det, rate=10.0)
    for _ in range(3):
        assert det.feed(genes=0.5, dt=1.0, vram_frac=0.97) is None
    # One healthy batch resets the consecutive counter ...
    assert det.feed(genes=10.0, dt=1.0, vram_frac=0.97) is None
    assert det.streak == 0
    # ... so window-1 further slow batches still must not trip ...
    for _ in range(3):
        assert det.feed(genes=0.5, dt=1.0, vram_frac=0.97) is None
    assert det.trips == 0
    # ... and only a full window of sustained crawling after recovery does.
    assert det.feed(genes=0.5, dt=1.0, vram_frac=0.97) == ACTION_EMPTY_CACHE


def test_ema_smooths_a_single_transient_stall():
    """With the production alpha (0.3) one stalled batch cannot drag the
    EMA below the threshold, so transient hiccups don't start a streak."""
    det = CrawlDetector(4, 5.0, action="ladder", ema_alpha=0.3,
                        log_fn=lambda _m: None)
    for _ in range(4):
        det.feed(genes=10.0, dt=1.0, vram_frac=0.5)
    assert det.baseline == pytest.approx(10.0)
    # EMA = 0.3 * 0.1 + 0.7 * 10.0 = 7.03 > threshold 2.0 -> no streak.
    assert det.feed(genes=0.1, dt=1.0, vram_frac=0.97) is None
    assert det.streak == 0


# -- actions -----------------------------------------------------------------


def test_action_off_logs_but_returns_none():
    logs: list[str] = []
    det = _detector(window=4, action="off", log_fn=logs.append)
    _warm(det, rate=10.0)
    for _ in range(4):
        assert det.feed(genes=0.5, dt=1.0, vram_frac=0.97) is None
    assert det.trips == 1
    assert not det.disarmed
    crawl_lines = [m for m in logs if "CRAWL detected" in m]
    assert crawl_lines and all("[crawl-watchdog]" in m for m in crawl_lines)
    assert "log-only" in crawl_lines[0]
    # Detection stays armed: the next full window logs again.
    for _ in range(4):
        assert det.feed(genes=0.5, dt=1.0, vram_frac=0.97) is None
    assert det.trips == 2


def test_action_cpu_returns_demote_immediately_on_first_trip():
    det = _detector(window=4, action="cpu")
    _warm(det, rate=10.0)
    out = [det.feed(genes=0.5, dt=1.0, vram_frac=0.97) for _ in range(4)]
    assert out == [None, None, None, ACTION_DEMOTE]
    assert det.disarmed


def test_ladder_escalates_rung1_then_rung2_across_two_windows():
    logs: list[str] = []
    det = _detector(window=4, action="ladder", log_fn=logs.append)
    _warm(det, rate=10.0)
    first = [det.feed(genes=0.5, dt=1.0, vram_frac=0.97) for _ in range(4)]
    assert first == [None, None, None, ACTION_EMPTY_CACHE]
    assert det.rung == 1
    second = [det.feed(genes=0.5, dt=1.0, vram_frac=0.97) for _ in range(4)]
    assert second == [None, None, None, ACTION_DEMOTE]
    assert det.rung == 2
    assert det.disarmed
    # Terminal rung reached: the detector goes quiescent.
    assert det.feed(genes=0.5, dt=1.0, vram_frac=0.99) is None
    # The mandated grep-able evidence trail: rate, baseline, vram, action.
    trip_lines = [m for m in logs if "CRAWL detected" in m]
    assert len(trip_lines) == 2
    for line in trip_lines:
        assert line.startswith("[crawl-watchdog]")
        for field in ("ema=", "baseline=", "vram_frac=", "action="):
            assert field in line


# -- env knobs ----------------------------------------------------------------


def test_from_env_defaults(monkeypatch):
    for var in ("HELIX_BFM_CRAWL_WINDOW", "HELIX_BFM_CRAWL_FACTOR",
                "HELIX_BFM_CRAWL_ACTION"):
        monkeypatch.delenv(var, raising=False)
    det = CrawlDetector.from_env()
    assert det.baseline_window == 8
    assert det.factor == 5.0
    assert det.action == "ladder"


def test_from_env_overrides(monkeypatch):
    monkeypatch.setenv("HELIX_BFM_CRAWL_WINDOW", "3")
    monkeypatch.setenv("HELIX_BFM_CRAWL_FACTOR", "2.5")
    monkeypatch.setenv("HELIX_BFM_CRAWL_ACTION", "CPU")
    det = CrawlDetector.from_env()
    assert (det.baseline_window, det.factor, det.action) == (3, 2.5, "cpu")


def test_from_env_garbage_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("HELIX_BFM_CRAWL_WINDOW", "banana")
    monkeypatch.setenv("HELIX_BFM_CRAWL_FACTOR", "-3")
    monkeypatch.setenv("HELIX_BFM_CRAWL_ACTION", "explode")
    det = CrawlDetector.from_env()
    assert (det.baseline_window, det.factor, det.action) == (8, 5.0, "ladder")


# -- guarded CUDA helpers (must never raise on CPU-only boxes) ---------------


def test_cuda_vram_fraction_never_raises_without_a_gpu():
    frac = cw.cuda_vram_fraction()
    assert frac is None or 0.0 <= frac <= 1.5


def test_release_cuda_cache_never_raises_without_a_gpu():
    assert cw.release_cuda_cache() in (True, False)


# -- wire level: ingest path (_drain_with_batched_splade) --------------------


class _StubGene:
    def __init__(self, **kw):
        self.content = kw.get("content", "x" * 50)


class _StubSplade:
    @staticmethod
    def encode_batch(_texts):
        return [None] * len(_texts)


class _StubGenome:
    def upsert_doc(self, *a, **kw):
        pass


def _install_drain_stubs(monkeypatch):
    """Patch the late imports inside ``_drain_with_batched_splade`` (same
    technique as test_build_fixture_matrix.py::TestResume) and make sure the
    SIGINT flag can't be the thing that raises."""
    import build_fixture_matrix as bfm
    fake_backends = types.ModuleType("cymatix_context.backends")
    fake_backends.splade_backend = _StubSplade
    fake_schemas = types.ModuleType("cymatix_context.schemas")
    fake_schemas.Gene = _StubGene
    monkeypatch.setitem(sys.modules, "cymatix_context.backends", fake_backends)
    monkeypatch.setitem(sys.modules, "cymatix_context.schemas", fake_schemas)
    monkeypatch.setattr(bfm, "_PAUSE_REQUESTED", False)
    return bfm


def test_drain_raises_pause_requested_when_detector_demotes(monkeypatch):
    """Terminal rung on the ingest path = process recycle via the existing
    #183 ``_PauseRequested`` batch-boundary machinery -- reused, not
    reinvented."""
    bfm = _install_drain_stubs(monkeypatch)

    class _DemotingDetector:
        def feed(self, genes, dt, vram_frac=None):
            return ACTION_DEMOTE

    gene_dict_iter = iter([
        [{"content": "alpha"}],
        [{"content": "beta"}],  # never reached -- recycle fires first
    ])
    stats = {"files": 0, "genes": 0, "errors": 0, "t0": 0.0}
    with pytest.raises(bfm._PauseRequested):
        bfm._drain_with_batched_splade(
            gene_dict_iter, _StubGenome(), stats, batch_size=1,
            crawl_detector=_DemotingDetector(),
        )
    # The demote batch itself was already flushed before the raise.
    assert stats["genes"] == 1


def test_drain_applies_rung1_empty_cache_and_continues(monkeypatch):
    bfm = _install_drain_stubs(monkeypatch)

    calls = {"release": 0}
    monkeypatch.setattr(
        bfm, "release_cuda_cache",
        lambda: calls.__setitem__("release", calls["release"] + 1) or True,
    )

    class _Rung1Detector:
        def __init__(self):
            self.feeds = 0

        def feed(self, genes, dt, vram_frac=None):
            self.feeds += 1
            return ACTION_EMPTY_CACHE if self.feeds == 1 else None

    det = _Rung1Detector()
    gene_dict_iter = iter([[{"content": "alpha"}], [{"content": "beta"}]])
    stats = {"files": 0, "genes": 0, "errors": 0, "t0": 0.0}
    bfm._drain_with_batched_splade(
        gene_dict_iter, _StubGenome(), stats, batch_size=1,
        crawl_detector=det,
    )
    assert calls["release"] == 1, "rung 1 must release the CUDA cache"
    assert det.feeds == 2, "detector is fed once per flushed batch"
    assert stats["genes"] == 2, "rung 1 continues the drain, no pause"


# -- wire level: backfill path (backfill_bgem3_v2.backfill_dense_db) ---------


def _make_genes_db(path: Path, n: int) -> None:
    """Minimal ``genes`` schema for ``backfill_dense_db``: gene_id +
    content + the ``chromatin`` column its partial index references."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE genes ("
        "gene_id TEXT PRIMARY KEY, content TEXT, "
        "chromatin INTEGER DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO genes (gene_id, content) VALUES (?, ?)",
        [(f"g{i}", f"some content {i} " * 4) for i in range(n)],
    )
    conn.commit()
    conn.close()


def test_backfill_demotes_codec_to_cpu_on_demote(tmp_path, monkeypatch):
    """Terminal rung on the backfill path: tear down the codec and reload
    it with device=cpu for the remainder of the shard."""
    import backfill_bgem3_v2 as bf2

    db = tmp_path / "shard.db"
    _make_genes_db(db, n=6)

    constructed: list[str] = []

    class _FakeCodec:
        def __init__(self, dim=4, device="cpu", **_kw):
            self.device = device
            constructed.append(device)

        def encode_batch(self, texts, task="passage"):
            return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(bf2, "BGEM3Codec", _FakeCodec)
    # Pretend to be a GPU box; the codec is fake so no CUDA is touched.
    monkeypatch.setenv("BGEM3_DEVICE", "cuda")

    class _DemoteOnFirstFeed:
        def __init__(self):
            self.feeds = 0

        def feed(self, genes, dt, vram_frac=None):
            self.feeds += 1
            return ACTION_DEMOTE if self.feeds == 1 else None

    logs: list[str] = []
    report = bf2.backfill_dense_db(
        str(db), dim=4, batch=2,
        crawl_detector=_DemoteOnFirstFeed(),
        log_fn=logs.append,
    )
    assert constructed == ["cuda", "cpu"], (
        "codec must be torn down and reloaded on CPU after the demote"
    )
    assert report["rows_processed"] == 6
    assert report["dense_coverage"] == pytest.approx(1.0)
    assert any(
        m.startswith("[crawl-watchdog]") and "DEMOTING to CPU" in m
        for m in logs
    )


def test_backfill_leaves_injected_codec_alone_on_demote(tmp_path):
    """A caller-injected codec can't be rebuilt on CPU -- demote degrades
    to a loud log line and the backfill keeps going."""
    import backfill_bgem3_v2 as bf2

    db = tmp_path / "shard2.db"
    _make_genes_db(db, n=4)

    class _InjectedCodec:
        def encode_batch(self, texts, task="passage"):
            return [[0.0, 0.0] for _ in texts]

    class _AlwaysDemote:
        def feed(self, genes, dt, vram_frac=None):
            return ACTION_DEMOTE

    logs: list[str] = []
    report = bf2.backfill_dense_db(
        str(db), dim=2, batch=2,
        codec=_InjectedCodec(),
        crawl_detector=_AlwaysDemote(),
        log_fn=logs.append,
    )
    assert report["rows_processed"] == 4
    assert report["dense_coverage"] == pytest.approx(1.0)
    assert any(
        m.startswith("[crawl-watchdog]") and "injected" in m for m in logs
    )

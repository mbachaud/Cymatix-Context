"""Phase-0 perf instrumentation (2026-08 latency/concurrency campaign, slice 1).

Closes the stage-timing blind spots the campaign needs before any latency
receipt can be trusted (docs ref: PERF campaign plan, A.1 item 1 / W0.2-W0.3):

  - ``_stage_timer(extra=...)`` merges a payload dict into the pipeline
    ring entry so harnesses can read sub-stage detail over HTTP with no
    OTel collector.
  - ``KnowledgeStore.query_docs`` publishes ``last_signal_timings`` — a
    per-query {signal: ms} map covering the existing seven tier signals
    plus the previously-untimed lex_anchor / hebbian / body_fetch /
    coact_expand regions.
  - ``query_docs_ann`` merges its own leg timings (ann_dense_leg /
    ann_body_fetch) into the same map.
  - ``KnowledgeStore.commit_count`` counts every COMMIT on the writer
    connection — including raw ``conn.commit()`` calls from "unlocked
    committer" call sites (Registry, session_delivery, cwola) that
    bypass ``_write_lock``.
  - ``telemetry.record_model_load()`` accumulates encoder load time into
    ``telemetry.model_load_ms()`` so cold-start receipts can separate
    model load from retrieval work.
  - End-to-end: a ``POST /context`` turn rings ``tail_writes`` /
    ``delivery_lookup`` / ``delivery_log`` / ``cwola`` stages and the
    ``express`` entry carries the ``signals`` sub-map.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

# Make benchmarks/ and benchmarks/dogfood importable as flat module paths
# (repo test convention — see tests/test_bench_orchestrator.py).
_BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
for _p in (_BENCH, _BENCH / "dogfood"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cymatix_context.context_manager as cm_mod
import cymatix_context.telemetry as telemetry_mod
from cymatix_context.context_manager import _stage_timer
from cymatix_context.genome import Genome

from tests.conftest import FakeBGEM3Codec, hash_vec, make_client, make_gene


# ── Helpers ──────────────────────────────────────────────────────────


class _RecordingInstrument:
    def __init__(self):
        self.observations: list[tuple[float, dict]] = []

    def record(self, value: float, attributes: dict | None = None):
        self.observations.append((value, attributes or {}))


def _populate_v2(genome: Genome, gene_id: str, vec) -> None:
    """Write a v2 dense BLOB directly, bypassing the codec (test seam)."""
    blob = vec.astype("<f4").tobytes()
    genome.conn.execute(
        "UPDATE genes SET embedding_dense_v2 = ? WHERE gene_id = ?",
        (sqlite3.Binary(blob), gene_id),
    )
    genome.conn.commit()
    genome._invalidate_dense_matrix()


def _ring_entries_since(mark: int) -> list[dict]:
    return list(cm_mod._pipeline_events)[mark:]


# ── 1. _stage_timer extra payload → ring entry ───────────────────────


def test_stage_timer_extra_merges_into_ring_entry(monkeypatch):
    """extra() keys land on the ring entry alongside request_id/stage/ms/ts."""
    monkeypatch.setattr(cm_mod, "_pipeline_stage_histogram",
                        lambda: _RecordingInstrument())
    token = cm_mod._pipeline_request_id.set("req-extra-test")
    try:
        with _stage_timer("tail_writes", extra=lambda: {"commits": 7}):
            pass
    finally:
        cm_mod._pipeline_request_id.reset(token)

    # Filter by the unique request id, not a length mark — a full deque
    # rotates instead of growing, which silently empties since-mark slices.
    entries = [e for e in cm_mod._pipeline_events
               if e.get("request_id") == "req-extra-test"]
    assert len(entries) == 1
    assert entries[0]["stage"] == "tail_writes"
    assert entries[0]["commits"] == 7
    # Reserved keys must not be clobbered by the payload.
    assert entries[0]["request_id"] == "req-extra-test"


def test_stage_timer_extra_errors_are_swallowed(monkeypatch):
    """A raising extra() must not break the pipeline or drop the entry."""
    monkeypatch.setattr(cm_mod, "_pipeline_stage_histogram",
                        lambda: _RecordingInstrument())

    def _boom():
        raise RuntimeError("extra deliberately broken")

    token = cm_mod._pipeline_request_id.set("req-extra-boom")
    try:
        with _stage_timer("tail_writes", extra=_boom):
            pass  # must not raise
    finally:
        cm_mod._pipeline_request_id.reset(token)

    entries = [e for e in cm_mod._pipeline_events
               if e.get("request_id") == "req-extra-boom"]
    assert len(entries) == 1, "entry must still ring without the extra payload"
    assert "commits" not in entries[0]


# ── 2. query_docs publishes last_signal_timings ──────────────────────


def test_query_docs_publishes_signal_timings(genome):
    for i in range(3):
        genome.upsert_gene(make_gene(
            f"alpha content body number {i}", domains=["alpha"],
            gene_id=f"sig-{i}",
        ))

    genome.query_docs(["alpha"], [], max_genes=5)

    timings = genome.last_signal_timings
    assert isinstance(timings, dict) and timings, (
        "query_docs must publish a non-empty last_signal_timings map"
    )
    # Previously-untimed regions now have first-class signals.
    for key in ("lex_anchor", "body_fetch", "coact_expand"):
        assert key in timings, f"missing signal timing: {key}"
    # Existing tier signals are mirrored into the same map.
    assert "tag_exact" in timings
    assert all(v >= 0.0 for v in timings.values())


def test_query_docs_ann_merges_leg_timings():
    g = Genome(
        path=":memory:",
        dense_embedding_enabled=True,
        dense_embedding_dim=1024,
        dense_pool_size=50,
    )
    try:
        for i in range(5):
            gid = f"lex-{i}"
            g.upsert_gene(
                make_gene(f"alpha lex body {i}", domains=["alpha"], gene_id=gid),
                apply_gate=False,
            )
            _populate_v2(g, gid, hash_vec(f"vec {gid}", 1024))
        # Dense-only needle: no lex overlap with the query domains, so it
        # reaches the union as a missing id and exercises ann_body_fetch.
        g.upsert_gene(
            make_gene("needle body text", domains=["zeta"], gene_id="needle-1"),
            apply_gate=False,
        )
        _populate_v2(g, "needle-1", hash_vec("needle target", 1024))
        g._dense_codec = FakeBGEM3Codec(dim=1024, query_target="needle target")

        g.query_docs_ann("what is the needle", domains=["alpha"], entities=[],
                         max_genes=6)

        timings = g.last_signal_timings
        assert "ann_dense_leg" in timings
        assert "ann_body_fetch" in timings
        # The inner query_docs signal map must survive the merge.
        assert "body_fetch" in timings
    finally:
        g.close()


def test_signal_timings_reset_on_raising_query(genome):
    """A query that raises (zero tier matches) must not leave the previous
    query's timing map behind — stale timings would mis-attribute the next
    ring entry."""
    from cymatix_context.knowledge_store import PromoterMismatch

    genome.upsert_gene(make_gene("alpha body", domains=["alpha"], gene_id="st-1"))
    genome.query_docs(["alpha"], [], max_genes=5)
    assert genome.last_signal_timings  # sanity: previous query populated it

    with pytest.raises(PromoterMismatch):
        genome.query_docs(["nomatchterm"], [], max_genes=5)

    assert genome.last_signal_timings == {}, (
        "raising query must clear, not retain, the prior timing map"
    )


# ── 3. commit counter on the writer connection ───────────────────────


def test_commit_count_increments_on_upsert(genome):
    before = genome.commit_count
    genome.upsert_gene(make_gene("commit counting body", domains=["cc"]))
    assert genome.commit_count > before


def test_commit_count_sees_raw_unlocked_commits(genome):
    """Raw conn.commit() from unlocked committers must be counted too —
    that is the whole point (the W0.3 receipt baselines ~5+2N commits)."""
    before = genome.commit_count
    genome.conn.execute(
        "CREATE TABLE IF NOT EXISTS _commit_probe (x INTEGER)"
    )
    genome.conn.execute("INSERT INTO _commit_probe VALUES (1)")
    genome.conn.commit()
    assert genome.commit_count == before + 1


def test_commit_count_survives_vacuum(tmp_path):
    """vacuum() rebinds self.conn — the trace hook must be re-registered
    or the counter silently freezes for the rest of the process (review
    finding, perf slice 1)."""
    g = Genome(path=str(tmp_path / "g.db"))
    try:
        g.upsert_doc(make_gene("vacuum survivor", domains=["vac"]),
                     apply_gate=False)
        g.vacuum()
        before = g.commit_count
        g.conn.execute("CREATE TABLE IF NOT EXISTS _probe2 (x INTEGER)")
        g.conn.execute("INSERT INTO _probe2 VALUES (1)")
        g.conn.commit()
        assert g.commit_count == before + 1, (
            "commit trace hook died across the vacuum reopen"
        )
    finally:
        g.close()


# ── 4. model-load recorder ───────────────────────────────────────────


def test_record_model_load_accumulates(monkeypatch):
    # Patch on the module of definition: the telemetry package star-copies
    # otel names at import, so package-level bindings are separate copies.
    from cymatix_context.telemetry import otel as otel_mod

    recorder = _RecordingInstrument()
    monkeypatch.setattr(otel_mod, "genome_signal_histogram",
                        lambda: recorder)
    monkeypatch.setattr(otel_mod, "_MODEL_LOAD_MS", {})

    otel_mod.record_model_load("dense", 1.5)
    otel_mod.record_model_load("dense", 0.5)
    otel_mod.record_model_load("splade", 0.25)

    snap = otel_mod.model_load_ms()
    assert snap["dense"] == pytest.approx(2000.0)
    assert snap["splade"] == pytest.approx(250.0)
    # Histogram observed each load under the model_load_* signal label.
    signals = [attrs.get("signal") for _, attrs in recorder.observations]
    assert signals.count("model_load_dense") == 2
    assert signals.count("model_load_splade") == 1
    # Snapshot is a copy — mutating it must not corrupt the source.
    snap["dense"] = -1
    assert telemetry_mod.model_load_ms()["dense"] == pytest.approx(2000.0)


def test_bgem3_weight_load_records_model_load(monkeypatch):
    """The real ~2 GB weight load happens lazily inside BGEM3Codec._load(),
    NOT at codec construction — model_load_ms must capture it there or
    cold-start receipts subtract nothing (review finding, perf slice 1)."""
    import types

    from cymatix_context.backends.bgem3_codec import BGEM3Codec
    from cymatix_context.telemetry import otel as otel_mod

    monkeypatch.setattr(otel_mod, "_MODEL_LOAD_MS", {})
    fake_mod = types.ModuleType("FlagEmbedding")
    fake_mod.BGEM3FlagModel = lambda name, use_fp16: object()
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_mod)

    codec = BGEM3Codec(dim=1024)
    codec._load()

    assert "dense" in otel_mod.model_load_ms(), (
        "BGEM3Codec._load must record the weight load under 'dense'"
    )
    # Idempotent: a second _load is the lock-free fast path, no re-record.
    n_before = otel_mod.model_load_ms()["dense"]
    codec._load()
    assert otel_mod.model_load_ms()["dense"] == n_before


def test_splade_load_records_model_load(monkeypatch):
    from cymatix_context.backends import splade_backend
    from cymatix_context.telemetry import otel as otel_mod

    monkeypatch.setattr(otel_mod, "_MODEL_LOAD_MS", {})
    monkeypatch.setattr(splade_backend, "_model", None)
    monkeypatch.setattr(splade_backend, "_tokenizer", None)
    monkeypatch.setattr(splade_backend, "_device", None)

    import transformers
    monkeypatch.setattr(transformers, "AutoTokenizer",
                        types_simple_namespace_factory())
    monkeypatch.setattr(transformers, "AutoModelForMaskedLM",
                        types_simple_namespace_factory())

    splade_backend._ensure_loaded()

    assert "splade" in otel_mod.model_load_ms(), (
        "_ensure_loaded must record the SPLADE load under 'splade'"
    )


def types_simple_namespace_factory():
    """Stand-in for AutoTokenizer/AutoModelForMaskedLM: from_pretrained
    returns an object whose .to(device) returns itself and .eval() no-ops."""
    class _Fake:
        @staticmethod
        def from_pretrained(name):
            class _M:
                def to(self, device):
                    return self

                def eval(self):
                    # torch nn.Module.eval() (inference mode), NOT builtin
                    # eval — mirrors _model.eval() in splade_backend.
                    return self
            return _M()
    return _Fake


def test_sema_codec_load_records_model_load(monkeypatch):
    """SemaCodec.__init__ loads MiniLM + builds the projection — the second
    biggest cold sink after BGE-M3; must land in model_load_ms (cold-start
    smoke showed 8.1s unattributed without it)."""
    import numpy as np

    from cymatix_context.telemetry import otel as otel_mod

    monkeypatch.setattr(otel_mod, "_MODEL_LOAD_MS", {})

    class _FakeST:
        def __init__(self, name, device=None):
            pass

        def get_sentence_embedding_dimension(self):
            return 384

        def encode(self, texts, **kw):
            return np.zeros((len(texts), 384), dtype=np.float32)

    import sentence_transformers
    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _FakeST)

    from cymatix_context.backends.sema import SemaCodec
    SemaCodec(device="cpu")

    assert "sema" in otel_mod.model_load_ms()


def test_dense_matrix_build_records_model_load(monkeypatch):
    """First _ensure_dense_matrix builds the full hot-tier matrix — a cold
    cost that must not be silently attributed to express."""
    from cymatix_context.telemetry import otel as otel_mod

    monkeypatch.setattr(otel_mod, "_MODEL_LOAD_MS", {})

    g = Genome(path=":memory:", dense_embedding_enabled=True,
               dense_embedding_dim=1024)
    try:
        g.upsert_doc(make_gene("matrix body", domains=["mx"],
                               gene_id="mx-1"), apply_gate=False)
        _populate_v2(g, "mx-1", hash_vec("mx vec", 1024))

        matrix, ids = g._ensure_dense_matrix()
        assert matrix is not None and list(ids) == ["mx-1"]
        assert "dense_matrix" in otel_mod.model_load_ms()

        # Cached second call must not re-record.
        before = otel_mod.model_load_ms()["dense_matrix"]
        g._ensure_dense_matrix()
        assert otel_mod.model_load_ms()["dense_matrix"] == before
    finally:
        g.close()


# ── 5. _stage_receipts: bench-side ring aggregation ──────────────────


def test_stage_receipts_aggregate():
    import _stage_receipts as sr

    entries = [
        {"request_id": "r1", "stage": "express", "ms": 10.0, "ts": 1.0,
         "signals": {"fts5": 4.0, "body_fetch": 2.0},
         "model_load_ms": {"dense_codec": 100.0}},
        {"request_id": "r1", "stage": "tail_writes", "ms": 5.0, "ts": 1.1,
         "commits": 3},
        {"request_id": "r2", "stage": "express", "ms": 30.0, "ts": 2.0,
         "signals": {"fts5": 12.0},
         "model_load_ms": {"dense_codec": 100.0}},
        {"request_id": "r2", "stage": "tail_writes", "ms": 15.0, "ts": 2.1,
         "commits": 5},
    ]
    agg = sr.aggregate(entries)

    # Stage rollup uses the run_latency_scaling ceiling-index percentile:
    # p50 of [10, 30] is 30 (ceil(0.5*1) == 1 → the max at n=2).
    assert agg["stages"]["express"]["n"] == 2
    assert agg["stages"]["express"]["p50_ms"] == 30.0
    assert agg["stages"]["express"]["p95_ms"] == 30.0
    assert agg["stages"]["tail_writes"]["n"] == 2

    # Signal rollup comes from express entries' sub-maps; a signal absent
    # from one request is aggregated over the requests where it fired.
    assert agg["signals"]["fts5"]["n"] == 2
    assert agg["signals"]["fts5"]["p50_ms"] == 12.0
    assert agg["signals"]["body_fetch"]["n"] == 1

    # model_load_ms is process-cumulative: keep the last-seen snapshot.
    assert agg["model_load_ms"] == {"dense_codec": 100.0}

    # commits ride tail_writes entries.
    assert agg["commits"]["n"] == 2
    assert agg["commits"]["p50"] == 5
    assert agg["commits"]["max"] == 5


def test_stage_receipts_aggregate_empty():
    import _stage_receipts as sr

    agg = sr.aggregate([])
    assert agg["stages"] == {}
    assert agg["signals"] == {}
    assert agg["model_load_ms"] == {}
    assert agg["commits"]["n"] == 0


def test_stage_receipts_fetch_recent_parses_endpoint_shape(monkeypatch):
    """fetch_recent unwraps the {'events': [...], 'ring_max': N} envelope."""
    import _stage_receipts as sr
    import io
    import json
    import urllib.request as _url

    payload = {"events": [{"request_id": "r1", "stage": "express",
                           "ms": 1.0, "ts": 0.0}], "ring_max": 64}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=None):
        assert timeout is not None, "no hanging requests — timeout required"
        return _Resp(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(_url, "urlopen", _fake_urlopen)
    events = sr.fetch_recent("http://127.0.0.1:11437", limit=50)
    assert events == payload["events"]


# ── 6. launcher pipeline panel: nested stages must not double-count ──


def test_pipeline_panel_total_excludes_nested_stages():
    """delivery_lookup / delivery_log run INSIDE the assemble stage — their
    ms is already inside assemble's ms, so summing every event overstates
    the per-request total (review finding, perf slice 1)."""
    from cymatix_context.launcher.collector import StateCollector

    payload = {"events": [
        {"request_id": "r1", "stage": "express", "ms": 50.0, "ts": 1.0},
        {"request_id": "r1", "stage": "assemble", "ms": 45.0, "ts": 1.2},
        {"request_id": "r1", "stage": "delivery_lookup", "ms": 20.0, "ts": 1.1},
        {"request_id": "r1", "stage": "delivery_log", "ms": 10.0, "ts": 1.15},
    ], "ring_max": 128}

    panel = StateCollector._pipeline_panel(object(), payload)
    row = panel["runs"][0]
    assert row["total_ms"] == 95.0, (
        f"total must be express+assemble only; nested delivery stages are "
        f"already inside assemble. got {row['total_ms']}"
    )
    # The breakdown still shows the nested stages individually.
    assert row["stages"]["delivery_lookup"] == 20.0
    assert row["stages"]["delivery_log"] == 10.0


# ── 6b. thread-local reader connections (c=2 livelock fix) ───────────
#
# Two concurrent build_context calls interleaving large scans on ONE
# shared connection (reader for the tiers, writer for the co-activation
# expansion) livelock: shared page cache thrash + per-step mutex
# ping-pong + GIL starvation wedged the whole server at c=2 (slice-2
# smoke, faulthandler-confirmed in storage/co_activation.py). Fix:
# read_conn hands each thread its own mode=ro connection; :memory:
# stores keep the shared-writer fallback (a second connection would be
# a different database).


@pytest.mark.concurrency
def test_read_conn_is_thread_local_for_file_backed(tmp_path):
    g = Genome(path=str(tmp_path / "tl.db"))
    try:
        main_conn = g.read_conn
        assert g.read_conn is main_conn, "same thread must reuse its reader"
        assert main_conn is not g.conn, "reader must not be the writer"

        seen: dict = {}

        def grab(i):
            seen[i] = g.read_conn
            # A second access on the same thread reuses it.
            seen[f"{i}_again"] = g.read_conn

        ts = [threading.Thread(target=grab, args=(i,)) for i in (1, 2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=10)

        assert seen[1] is seen["1_again"]
        assert seen[2] is seen["2_again"]
        assert seen[1] is not seen[2], (
            "two threads must get distinct reader connections"
        )
        assert seen[1] is not main_conn and seen[2] is not main_conn
    finally:
        g.close()


def test_read_conn_memory_store_falls_back_to_writer(genome):
    assert genome.read_conn is genome.conn


@pytest.mark.concurrency
def test_concurrent_query_docs_completes(tmp_path):
    """Regression canary: two threads querying one file-backed store must
    finish promptly (pre-fix, shared-conn interleaving could wedge)."""
    g = Genome(path=str(tmp_path / "cc.db"))
    try:
        for i in range(30):
            g.upsert_doc(make_gene(
                f"alpha shared body {i}", domains=["alpha"],
                entities=[f"ent{i % 5}", "common"], gene_id=f"cc-{i:03d}",
            ), apply_gate=False)

        errors: list = []

        def worker():
            try:
                for _ in range(5):
                    g.query_docs(["alpha"], ["common"], max_genes=8)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        # daemon=True: if the bug reproduces, wedged threads must fail the
        # assert — not block interpreter shutdown and hang the whole run.
        ts = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in ts), "concurrent query wedged"
        assert not errors, f"concurrent query raised: {errors!r}"
    finally:
        if not any(t.is_alive() for t in ts):
            g.close()  # closing under a wedged reader would hang too


@pytest.mark.concurrency
def test_concurrent_build_context_with_refiners(tmp_path):
    """Manager-level two-thread hammer through the FULL pipeline (rerank
    refiners on): ray_trace read co-activation/harmonic data on the shared
    WRITER connection, which interleaves with locked writes (log_health)
    cross-thread and deadlocks on py3.14 — caught live in arm-a of the
    baseline battery. Request-path reads must never touch the writer conn."""
    from cymatix_context.config import BudgetConfig
    from cymatix_context.context_manager import CymatixContextManager

    from tests.conftest import MockCompressorBackend, make_cymatix_config

    cfg = make_cymatix_config(
        budget=BudgetConfig(max_genes_per_turn=4),
    )
    cfg.genome.path = str(tmp_path / "refiners.db")
    mgr = CymatixContextManager(cfg)
    mgr.ribosome.backend = MockCompressorBackend()
    try:
        for i in range(20):
            mgr.genome.upsert_doc(make_gene(
                f"harmonic alpha body {i}", domains=["alpha"],
                entities=[f"e{i % 4}"], gene_id=f"rf-{i:03d}",
            ), apply_gate=False)
        mgr.build_context("alpha harmonic", read_only=True,
                          ignore_delivered=True)  # warm

        errors: list = []

        def worker():
            try:
                for _ in range(15):
                    mgr.build_context("alpha harmonic body", read_only=True,
                                      ignore_delivered=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=120)
        assert not any(t.is_alive() for t in ts), (
            "build_context wedged under two threads (writer-conn read collision)"
        )
        assert not errors, f"concurrent build raised: {errors[:2]!r}"
    finally:
        if not any(t.is_alive() for t in ts):
            mgr.close()


@pytest.mark.concurrency
def test_concurrent_log_health_no_commit_steal(tmp_path):
    """Two threads on the shared writer: an unlocked log_health commit can
    commit the OTHER thread's in-flight transaction, whose own commit then
    raises 'cannot commit - no transaction is active' (serialization point
    #1; reproduced live at c=2 in the slice-2 probe). log_health must hold
    _write_lock. First flipped member of the W2.3-A acceptance family."""
    g = Genome(path=str(tmp_path / "lh.db"))
    errors: list = []

    def hammer():
        try:
            for i in range(200):
                g.log_health(query=f"q{i}", ellipticity=0.5, coverage=0.5,
                             density=0.5, freshness=0.5, genes_expressed=1,
                             genes_available=1, status="healthy")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        ts = [threading.Thread(target=hammer, daemon=True) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        # Pre-fix this deadlocks outright on py3.14 sqlite3 (both threads
        # blocked in the INSERT) rather than raising — assert liveness
        # first so the failure mode is a clean assert, not a hang.
        assert not any(t.is_alive() for t in ts), (
            "log_health wedged under two threads (shared-writer deadlock)"
        )
        assert not errors, f"commit steal reproduced: {errors[:2]!r}"
    finally:
        if not any(t.is_alive() for t in ts):
            g.close()  # close() on a wedged writer conn would hang too


# ── 7. bench-server spawn-token identity (venv-shim pid fix) ─────────
#
# On Windows a venv's python.exe is a launcher that re-execs the real
# interpreter as a CHILD, so Popen.pid never equals the /health-reported
# os.getpid() and the issue-#127 pid guard false-positives on every boot.
# The spawn token is the reliable identity; pid equality stays fatal only
# when the target server is too old to echo the token.


def test_health_echoes_spawn_token_when_env_set(monkeypatch):
    monkeypatch.setenv("CYMATIX_BENCH_SPAWN_TOKEN", "tok-e2e-123")
    client = make_client()
    payload = client.get("/health").json()
    assert payload.get("bench_spawn_token") == "tok-e2e-123"


def test_health_omits_spawn_token_when_env_unset(monkeypatch):
    monkeypatch.delenv("CYMATIX_BENCH_SPAWN_TOKEN", raising=False)
    client = make_client()
    payload = client.get("/health").json()
    assert payload.get("bench_spawn_token") is None


def _bench_server_with(payload, spawn_token, proc_pid=111):
    import types as _types

    from bench_orchestrator import BenchServer

    srv = BenchServer()
    srv._proc = _types.SimpleNamespace(pid=proc_pid, poll=lambda: None,
                                       returncode=None)
    srv._spawn_token = spawn_token
    srv.health = lambda: payload
    return srv


def test_wait_healthy_token_match_overrides_pid_mismatch():
    srv = _bench_server_with(
        {"pid": 999, "bench_spawn_token": "tok-1"}, spawn_token="tok-1")
    srv._wait_healthy()  # must not raise: token is authoritative


def test_wait_healthy_token_mismatch_raises():
    from bench_orchestrator import BenchServerError

    srv = _bench_server_with(
        {"pid": 111, "bench_spawn_token": "tok-OTHER"}, spawn_token="tok-1")
    with pytest.raises(BenchServerError):
        srv._wait_healthy()


def test_wait_healthy_no_token_in_payload_keeps_pid_guard():
    from bench_orchestrator import BenchServerError

    srv = _bench_server_with({"pid": 999}, spawn_token="tok-1")
    with pytest.raises(BenchServerError):
        srv._wait_healthy()
    srv_ok = _bench_server_with({"pid": 111}, spawn_token="tok-1")
    srv_ok._wait_healthy()  # pid equal — old guard passes


# ── 8. end-to-end: /context rings the new stages ─────────────────────


def test_context_turn_rings_new_stages():
    client = make_client()
    # Seed the store so express finds candidates and the pipeline reaches
    # assemble + tail writes (an empty genome abstains before them). Direct
    # genome pokes with explicit domains — the test-env CPU tagger yields
    # no tags, so /ingest-seeded content would miss every tier.
    g = client.app.state.cymatix.genome
    for i in range(3):
        g.upsert_doc(make_gene(
            f"splice pipeline compresses candidate fragments, variant {i}",
            domains=["splice", "pipeline"],
            gene_id=f"e2e-ring-{i}",
        ))
    # A full deque (maxlen rotation) keeps len() constant, which would
    # make a since-index mark miss every new entry — start from empty.
    cm_mod._pipeline_events.clear()
    mark = 0

    resp = client.post("/context", json={
        "query": "what does the splice step do to candidate fragments?",
        "session_id": "perf-instr-sess",
    })
    assert resp.status_code == 200

    entries = _ring_entries_since(mark)
    stages = {e["stage"] for e in entries}
    for stage in ("express", "tail_writes", "delivery_lookup",
                  "delivery_log", "cwola"):
        assert stage in stages, f"stage {stage!r} missing from ring: {stages}"

    express = [e for e in entries if e["stage"] == "express"][-1]
    assert "signals" in express, "express ring entry must carry the sub-map"
    assert "body_fetch" in express["signals"]

    # The cwola entry must correlate to the same request as express.
    cwola = [e for e in entries if e["stage"] == "cwola"][-1]
    assert cwola["request_id"] == express["request_id"]

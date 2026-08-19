"""RemoteCodec seam wiring — dense / SPLADE / SEMA (Fork 1 slice 1, Task 3).

Three seams substitute a remote encoder daemon for the in-process models when
``CYMATIX_ENCODER_URL`` / ``[encoder_daemon] url`` is set:

- dense   — ``bgem3_codec.get_shared_codec`` returns ``RemoteBGEM3Codec``
- SPLADE  — ``splade_backend.encode`` / ``encode_batch`` branch to the daemon
- SEMA    — ``CymatixContextManager`` builds ``RemoteSemaCodec``

The load-bearing property, asserted first and hardest here, is **off means
untouched**: with no URL configured the three seams run exactly today's code
(same objects, same cache keys, same local bodies). The second property is
that a daemon outage can never fail a query — dense encode never raises,
SPLADE falls THROUGH to its unchanged local body, SEMA falls back per call
and retries the daemon after the circuit cooldown.

Non-live: no real model, no real subprocess. Real sockets only via
ephemeral-port stdlib ``http.server.HTTPServer`` fixtures in daemon threads.
"""

from __future__ import annotations

import io
import json
import logging
import socket
import sys
import threading
import types
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.conftest import make_cymatix_config, requires_spacy_model


# ── test isolation (seam notes §7c) ─────────────────────────────────
#
# Clears the process-lifetime singletons other suites populate, nulls the
# splade module globals (including the module-held remote client), unsets
# CYMATIX_ENCODER_URL so "off" tests stay deterministic on a dev machine that
# exports it, and resets encoder_client's URL slot + circuit breaker.


@pytest.fixture(autouse=True)
def _pristine_remote_seams(monkeypatch):
    from cymatix_context.backends import bgem3_codec, encoder_client, rerank_backend, splade_backend

    bgem3_codec._GLOBAL_CODECS.clear()
    monkeypatch.setattr(splade_backend, "_model", None, raising=False)
    monkeypatch.setattr(splade_backend, "_tokenizer", None, raising=False)
    monkeypatch.setattr(splade_backend, "_device", None, raising=False)
    monkeypatch.setattr(splade_backend, "_remote_client", None, raising=False)
    monkeypatch.setattr(rerank_backend, "_scorer", None, raising=False)
    monkeypatch.setattr(rerank_backend, "_scorer_model_name", None, raising=False)
    monkeypatch.setattr(rerank_backend, "_remote_client", None, raising=False)
    monkeypatch.delenv("CYMATIX_ENCODER_URL", raising=False)
    encoder_client.reset_for_test()
    yield
    encoder_client.reset_for_test()
    bgem3_codec._GLOBAL_CODECS.clear()


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── canned-JSON daemon fixture ───────────────────────────────────────


_DEFAULT_RESPONSES = {
    "/encode/dense": {"vectors": [[0.25, -0.5, 0.75, 1.0]], "dim": 4, "model": "fake-dense"},
    "/encode/splade": {"sparse": [{"beta": 0.5, "alpha": 0.25, "gamma": 0.1234}]},
    "/encode/sema": {"vectors": [[0.125] * 20]},
    "/encode/rerank": {"scores": [0.9]},
}


class _DaemonHandler(BaseHTTPRequestHandler):
    """Serves canned daemon JSON; class attrs are per-test state."""

    ready = True
    fail_paths: set = set()
    received: list = []
    responses: dict = {}

    def _send(self, status: int, payload) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:  # pragma: no cover - defensive
            body = {"_raw": raw.decode("utf-8", errors="replace")}
        type(self).received.append((self.path, body))
        if self.path in type(self).fail_paths:
            self._send(500, {"detail": "injected failure"})
            return
        payload = type(self).responses.get(self.path)
        if payload is None:
            self.send_response(404)
            self.end_headers()
            return
        self._send(200, payload)

    def do_GET(self):
        if self.path != "/ready":
            self.send_response(404)
            self.end_headers()
            return
        type(self).received.append((self.path, None))
        if type(self).ready:
            self._send(200, {"ready": True, "spawn_token": "tok", "probe_ms": 1.0})
        else:
            self._send(503, {"ready": False, "state": "loading"})

    def log_message(self, *a, **kw):
        pass


def _paths(path: str) -> list:
    return [p for p, _ in _DaemonHandler.received if p == path]


@pytest.fixture
def daemon_url():
    """A live canned daemon; yields its base URL."""
    _DaemonHandler.ready = True
    _DaemonHandler.fail_paths = set()
    _DaemonHandler.received = []
    _DaemonHandler.responses = {k: dict(v) for k, v in _DEFAULT_RESPONSES.items()}
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), _DaemonHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.fixture
def dead_url(monkeypatch):
    """A URL with nothing listening (daemon-outage arm).

    Shrinks the first-use readiness budget so these tests don't sit through
    the 15 s production default; that the budget is honored at all is
    asserted separately in ``test_dense_ready_gate_honors_its_budget``.
    """
    monkeypatch.setenv("CYMATIX_ENCODER_READY_TIMEOUT_S", "0.2")
    return f"http://127.0.0.1:{_free_port()}"


# ── local-body sentinels ─────────────────────────────────────────────


class _LocalSpladeSentinel(Exception):
    """Raised from the LOCAL splade body so tests can prove it was reached."""


def _arm_local_splade(monkeypatch):
    """Make the local splade body observable without torch or a real model."""
    from cymatix_context.backends import splade_backend

    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))

    def _boom(model_name="naver/splade-cocondenser-ensembledistil"):
        raise _LocalSpladeSentinel(model_name)

    monkeypatch.setattr(splade_backend, "_ensure_loaded", _boom)


class _LocalRerankSentinel(Exception):
    """Raised from the LOCAL rerank body so tests can prove it was reached."""


def _arm_local_rerank(monkeypatch):
    """Make the local rerank body observable without torch or a real model."""
    from cymatix_context.backends import rerank_backend

    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))

    def _boom(model_name=None):
        raise _LocalRerankSentinel(model_name)

    monkeypatch.setattr(rerank_backend, "_get_scorer", _boom)


class _FakeLocalDense:
    """Stands in for the real BGEM3Codec the dense fallback constructs."""

    constructions = 0

    def __init__(self, dim: int = 1024, device: str = "cpu", model_name: str = "BAAI/bge-m3"):
        type(self).constructions += 1
        self.dim = dim
        self._device = device
        self.model_name = model_name
        self._model = "local-weights"
        self.encode_calls = 0
        self.batch_calls = 0

    def encode(self, text: str, task: str = "passage"):
        self.encode_calls += 1
        return [0.5] * self.dim

    def encode_batch(self, texts, task: str = "passage"):
        self.batch_calls += 1
        return [[0.5] * self.dim for _ in texts]


@pytest.fixture
def fake_local_dense(monkeypatch):
    from cymatix_context.backends import bgem3_codec

    class _Fake(_FakeLocalDense):
        constructions = 0

    monkeypatch.setattr(bgem3_codec, "BGEM3Codec", _Fake)
    return _Fake


class _FakeLocalSema:
    """Stands in for SemaCodec behind the RemoteSemaCodec's LazySemaCodec."""

    constructions = 0

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", device=None):
        type(self).constructions += 1
        self.model_name = model_name
        self.device = device

    def encode(self, text: str):
        return [1.0] * 20

    def encode_batch(self, texts, batch_size: int = 64):
        return [[1.0] * 20 for _ in texts]


@pytest.fixture
def fake_local_sema(monkeypatch):
    from cymatix_context.backends import sema as sema_mod

    class _Fake(_FakeLocalSema):
        constructions = 0

    monkeypatch.setattr(sema_mod, "sema_available", lambda: True)
    monkeypatch.setattr(sema_mod, "SemaCodec", _Fake)
    return _Fake


# ══ 1. OFF parity — the #1 requirement ═══════════════════════════════


def test_off_get_shared_codec_returns_real_bgem3_codec():
    from cymatix_context.backends import encoder_client
    from cymatix_context.backends.bgem3_codec import (
        BGEM3Codec,
        _GLOBAL_CODECS,
        get_shared_codec,
    )

    assert encoder_client.active_url() == ""
    codec = get_shared_codec(dim=1024, device="cpu")
    assert isinstance(codec, BGEM3Codec)
    assert not isinstance(codec, encoder_client.RemoteBGEM3Codec)
    assert _GLOBAL_CODECS[("BAAI/bge-m3", 1024, "cpu")] is codec


def test_off_get_shared_codec_keeps_singleton_semantics():
    from cymatix_context.backends.bgem3_codec import _GLOBAL_CODECS, get_shared_codec

    a = get_shared_codec(dim=1024, device="cpu")
    b = get_shared_codec(dim=1024, device="cpu")
    assert a is b
    assert get_shared_codec(dim=768, device="cpu") is not a
    fresh = get_shared_codec(dim=1024, device="cpu", share=False)
    assert fresh is not a
    assert len(_GLOBAL_CODECS) == 2


def test_off_splade_encode_runs_the_local_body(monkeypatch):
    """URL unset ⇒ no client is built and the unchanged local body runs."""
    from cymatix_context.backends import encoder_client, splade_backend

    def _no_client(*a, **kw):
        raise AssertionError("no EncoderClient may be built when the daemon URL is unset")

    monkeypatch.setattr(encoder_client, "EncoderClient", _no_client)
    _arm_local_splade(monkeypatch)

    with pytest.raises(_LocalSpladeSentinel):
        splade_backend.encode("hello world")
    with pytest.raises(_LocalSpladeSentinel):
        splade_backend.encode_batch(["hello world"])


def test_off_splade_encode_attribute_patch_seam_intact(monkeypatch):
    """Call sites patch ``splade_backend.encode`` by attribute — keep that working."""
    from cymatix_context.backends import splade_backend

    monkeypatch.setattr(
        splade_backend, "encode", lambda text, top_k=128, **kw: {"sentinel": 9.99}
    )
    from cymatix_context.backends import splade_backend as late

    assert late.encode("anything") == {"sentinel": 9.99}


def test_off_rerank_score_pairs_runs_the_local_body(monkeypatch):
    """URL unset ⇒ no client is built and the unchanged local body runs."""
    from cymatix_context.backends import encoder_client, rerank_backend

    def _no_client(*a, **kw):
        raise AssertionError("no EncoderClient may be built when the daemon URL is unset")

    monkeypatch.setattr(encoder_client, "EncoderClient", _no_client)
    _arm_local_rerank(monkeypatch)

    with pytest.raises(_LocalRerankSentinel):
        rerank_backend.score_pairs("q", ["hello world"])


def test_off_manager_constructs_lazy_sema_codec(tmp_path, fake_local_sema):
    from cymatix_context.backends.sema import LazySemaCodec
    from cymatix_context.config import GenomeConfig
    from cymatix_context.context_manager import CymatixContextManager

    cfg = make_cymatix_config(
        genome=GenomeConfig(path=str(tmp_path / "genome.db"), cold_start_threshold=5),
    )
    mgr = CymatixContextManager(cfg)
    assert isinstance(mgr._sema_codec, LazySemaCodec)
    assert fake_local_sema.constructions == 0, "lazy: no model built at init"


def test_off_manager_leaves_encoder_client_off(tmp_path, fake_local_sema):
    """The manager's configure() call must not flip the seams on by default."""
    from cymatix_context.backends import encoder_client
    from cymatix_context.config import GenomeConfig
    from cymatix_context.context_manager import CymatixContextManager

    cfg = make_cymatix_config(
        genome=GenomeConfig(path=str(tmp_path / "genome.db"), cold_start_threshold=5),
    )
    CymatixContextManager(cfg)
    assert encoder_client.active_url() == ""


# ══ 2. ON — the daemon answers ═══════════════════════════════════════


def test_on_get_shared_codec_returns_remote_codec(daemon_url):
    from cymatix_context.backends import encoder_client
    from cymatix_context.backends.bgem3_codec import _GLOBAL_CODECS, get_shared_codec

    encoder_client.configure(daemon_url)
    codec = get_shared_codec(dim=4, device="cpu")
    assert isinstance(codec, encoder_client.RemoteBGEM3Codec)
    assert _GLOBAL_CODECS[("BAAI/bge-m3", 4, "cpu")] is codec
    assert codec.dim == 4
    assert codec.model_name == "BAAI/bge-m3"
    assert codec._device == "cpu"
    assert codec._model is None, "_model stays None until a fallback load"


def test_on_dense_encode_returns_the_served_vector_and_sends_raw_text(daemon_url):
    from cymatix_context.backends import encoder_client

    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url=daemon_url)
    assert codec.encode("hello world", task="query") == [0.25, -0.5, 0.75, 1.0]
    path, body = [r for r in _DaemonHandler.received if r[0] == "/encode/dense"][-1]
    assert body == {"texts": ["hello world"], "task": "query"}, (
        "the daemon owns the query prefix; the client sends raw text + task"
    )


def test_on_dense_encode_batch_returns_served_vectors(daemon_url):
    from cymatix_context.backends import encoder_client

    _DaemonHandler.responses["/encode/dense"] = {
        "vectors": [[0.25, -0.5, 0.75, 1.0], [1.0, 0.0, 0.0, 0.0]],
        "dim": 4,
    }
    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url=daemon_url)
    assert codec.encode_batch(["a", "b"]) == [
        [0.25, -0.5, 0.75, 1.0],
        [1.0, 0.0, 0.0, 0.0],
    ]
    _, body = [r for r in _DaemonHandler.received if r[0] == "/encode/dense"][-1]
    assert body == {"texts": ["a", "b"], "task": "passage"}


def test_on_dense_encode_batch_empty_makes_no_network_call(daemon_url, fake_local_dense):
    from cymatix_context.backends import encoder_client

    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url=daemon_url)
    assert codec.encode_batch([]) == []
    assert _DaemonHandler.received == [], "empty batch must not touch the network"
    assert fake_local_dense.constructions == 0, "empty batch must not build a fallback"


def test_on_dense_probes_ready_once_per_process(daemon_url):
    from cymatix_context.backends import encoder_client

    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url=daemon_url)
    codec.encode("one")
    codec.encode("two")
    # A second instance (e.g. a different dim key) shares the process gate.
    encoder_client.RemoteBGEM3Codec(dim=4, device="cuda", url=daemon_url).encode("three")
    assert len(_paths("/ready")) == 1, "readiness is a first-use gate, not per-encode"


def test_on_dense_similarity_is_local(dead_url):
    """similarity() is pure numpy — no round trip even with a dead daemon."""
    from cymatix_context.backends import encoder_client

    codec = encoder_client.RemoteBGEM3Codec(dim=3, device="cpu", url=dead_url)
    assert codec.similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)
    assert codec.similarity([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]) == pytest.approx(0.0)


def test_on_splade_encode_preserves_order_and_rounding(daemon_url):
    from cymatix_context.backends import encoder_client, splade_backend

    encoder_client.configure(daemon_url)
    sparse = splade_backend.encode("hello world", top_k=64)
    assert list(sparse.items()) == [("beta", 0.5), ("alpha", 0.25), ("gamma", 0.1234)]
    path, body = [r for r in _DaemonHandler.received if r[0] == "/encode/splade"][-1]
    assert body == {"texts": ["hello world"], "top_k": 64}


def test_on_splade_encode_batch_returns_served_dicts(daemon_url):
    from cymatix_context.backends import encoder_client, splade_backend

    _DaemonHandler.responses["/encode/splade"] = {
        "sparse": [{"beta": 0.5}, {"alpha": 0.25}],
    }
    encoder_client.configure(daemon_url)
    assert splade_backend.encode_batch(["a", "b"]) == [{"beta": 0.5}, {"alpha": 0.25}]


def test_on_splade_import_works_without_transformers(monkeypatch, daemon_url):
    """The remote branch sits before any torch import."""
    from cymatix_context.backends import encoder_client, splade_backend

    monkeypatch.setitem(sys.modules, "torch", None)  # `import torch` now raises
    monkeypatch.setattr(
        splade_backend,
        "_ensure_loaded",
        lambda *a, **kw: pytest.fail("remote branch must precede the local load"),
    )
    encoder_client.configure(daemon_url)
    assert splade_backend.encode("hello world") == {
        "beta": 0.5, "alpha": 0.25, "gamma": 0.1234,
    }


def test_on_rerank_score_pairs_returns_served_scores_and_sends_query_and_texts(daemon_url):
    from cymatix_context.backends import encoder_client, rerank_backend

    _DaemonHandler.responses["/encode/rerank"] = {"scores": [0.9, 0.1]}
    encoder_client.configure(daemon_url)
    scores = rerank_backend.score_pairs("hello", ["a", "b"])
    assert scores == [0.9, 0.1]
    path, body = [r for r in _DaemonHandler.received if r[0] == "/encode/rerank"][-1]
    assert body == {"query": "hello", "texts": ["a", "b"]}


def test_on_rerank_coerces_integer_scores_to_float(daemon_url):
    from cymatix_context.backends import encoder_client, rerank_backend

    _DaemonHandler.responses["/encode/rerank"] = {"scores": [1, 0]}
    encoder_client.configure(daemon_url)
    scores = rerank_backend.score_pairs("hello", ["a", "b"])
    assert scores == [1.0, 0.0]
    assert all(isinstance(s, float) for s in scores)


def test_on_rerank_import_works_without_transformers(monkeypatch, daemon_url):
    """The remote branch sits before any torch import."""
    from cymatix_context.backends import encoder_client, rerank_backend

    monkeypatch.setitem(sys.modules, "torch", None)  # `import torch` now raises
    monkeypatch.setattr(
        rerank_backend,
        "_get_scorer",
        lambda *a, **kw: pytest.fail("remote branch must precede the local load"),
    )
    _DaemonHandler.responses["/encode/rerank"] = {"scores": [0.42]}
    encoder_client.configure(daemon_url)
    assert rerank_backend.score_pairs("hello", ["world"]) == [0.42]


def test_rerank_falls_through_to_the_local_body_on_remote_failure(daemon_url, monkeypatch):
    from cymatix_context.backends import encoder_client, rerank_backend

    _DaemonHandler.fail_paths = {"/encode/rerank"}
    _arm_local_rerank(monkeypatch)
    encoder_client.configure(daemon_url)

    with pytest.raises(_LocalRerankSentinel):
        rerank_backend.score_pairs("q", ["hello world"])
    assert _paths("/encode/rerank"), "the remote was attempted before falling through"
    assert encoder_client.should_attempt() is False, "failure must open the circuit"


def test_rerank_falls_back_when_scores_length_mismatches_texts(daemon_url, monkeypatch):
    from cymatix_context.backends import encoder_client, rerank_backend

    _DaemonHandler.responses["/encode/rerank"] = {"scores": [0.9]}  # only 1 for 2 texts
    _arm_local_rerank(monkeypatch)
    encoder_client.configure(daemon_url)

    with pytest.raises(_LocalRerankSentinel):
        rerank_backend.score_pairs("q", ["a", "b"])
    assert encoder_client.should_attempt() is False, "shape mismatch must open the circuit"


def test_on_sema_encode_returns_served_vector(daemon_url):
    from cymatix_context.backends import encoder_client

    codec = encoder_client.RemoteSemaCodec(model_name="all-MiniLM-L6-v2", url=daemon_url)
    assert codec.encode("hello world") == [0.125] * 20
    _, body = [r for r in _DaemonHandler.received if r[0] == "/encode/sema"][-1]
    assert body == {"texts": ["hello world"]}


def test_on_sema_encode_batch_and_empty(daemon_url):
    from cymatix_context.backends import encoder_client

    _DaemonHandler.responses["/encode/sema"] = {
        "vectors": [[0.125] * 20, [0.25] * 20],
    }
    codec = encoder_client.RemoteSemaCodec(url=daemon_url)
    assert codec.encode_batch(["a", "b"]) == [[0.125] * 20, [0.25] * 20]
    assert codec.encode_batch([]) == []
    assert len(_paths("/encode/sema")) == 1, "empty batch must not touch the network"


def test_on_sema_signature_and_fingerprint_are_computed_locally(daemon_url):
    from cymatix_context.backends import encoder_client
    from cymatix_context.backends.sema import PRIMES

    vec = [0.0] * 20
    vec[5] = -0.9
    _DaemonHandler.responses["/encode/sema"] = {"vectors": [vec]}
    codec = encoder_client.RemoteSemaCodec(url=daemon_url)

    assert codec.signature("hello", top_k=1) == [(PRIMES[5].name, -0.9)]
    assert codec.fingerprint("hello").split()[0] == f"{PRIMES[5].symbol}.90"


def test_on_sema_duck_types_lazy_sema_codec(daemon_url):
    from cymatix_context.backends import encoder_client
    from cymatix_context.backends.sema import SemaCodec

    codec = encoder_client.RemoteSemaCodec(url=daemon_url)
    assert codec.is_lazy_component is True
    assert codec.projection_matrix is None
    assert codec.embed_dim == 384
    assert codec.loaded is False
    assert codec.peek() is None
    # Pure-numpy statics reused from SemaCodec — no model, no RPC.
    assert codec.similarity([1.0] * 20, [1.0] * 20) == pytest.approx(1.0)
    assert codec.nearest([1.0] * 20, [("g1", [1.0] * 20), ("g2", [-1.0] * 20)], k=1) == [
        ("g1", pytest.approx(1.0))
    ]
    assert SemaCodec.similarity([1.0] * 20, [1.0] * 20) == pytest.approx(1.0)

    codec.encode("hello")
    assert codec.loaded is True, "truthful after a successful remote call"
    assert _paths("/encode/sema"), "encode used the daemon"


def test_on_manager_constructs_remote_sema_codec_without_sentence_transformers(
    tmp_path, monkeypatch, daemon_url
):
    """With a URL set the daemon owns the model — sema_available() is not required."""
    from cymatix_context.backends import encoder_client
    from cymatix_context.backends import sema as sema_mod
    from cymatix_context.config import EncoderDaemonConfig, GenomeConfig
    from cymatix_context.context_manager import CymatixContextManager

    monkeypatch.setattr(sema_mod, "sema_available", lambda: False)
    cfg = make_cymatix_config(
        genome=GenomeConfig(path=str(tmp_path / "genome.db"), cold_start_threshold=5),
        encoder_daemon=EncoderDaemonConfig(url=daemon_url),
    )
    mgr = CymatixContextManager(cfg)
    assert isinstance(mgr._sema_codec, encoder_client.RemoteSemaCodec)
    assert encoder_client.active_url() == daemon_url


def test_on_manager_sema_off_knob_still_wins(tmp_path, daemon_url):
    """#227 global-off: sema_embed_on_ingest=false means NO codec, URL or not."""
    from cymatix_context.config import EncoderDaemonConfig, GenomeConfig, IngestionConfig
    from cymatix_context.context_manager import CymatixContextManager

    cfg = make_cymatix_config(
        genome=GenomeConfig(path=str(tmp_path / "genome.db"), cold_start_threshold=5),
        ingestion=IngestionConfig(sema_embed_on_ingest=False),
        encoder_daemon=EncoderDaemonConfig(url=daemon_url),
    )
    assert CymatixContextManager(cfg)._sema_codec is None


# ══ 3. Fallback — the daemon is down ═════════════════════════════════


def test_dense_encode_falls_back_in_process_and_never_raises(
    dead_url, fake_local_dense, caplog
):
    from cymatix_context.backends import encoder_client

    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url=dead_url)
    with caplog.at_level(logging.WARNING, logger="cymatix.encoder_client"):
        first = codec.encode("hello")
        second = codec.encode("world")
        batch = codec.encode_batch(["a", "b"])

    assert first == [0.5] * 4
    assert second == [0.5] * 4
    assert batch == [[0.5] * 4, [0.5] * 4]
    warnings = [
        r for r in caplog.records
        if r.name == "cymatix.encoder_client" and "unreachable" in r.message
    ]
    assert len(warnings) == 1, f"expected exactly one warning; got {[r.message for r in warnings]}"
    assert fake_local_dense.constructions == 1, "one fallback codec per instance"


def test_dense_fallback_publishes_model_for_the_components_probe(dead_url, fake_local_dense):
    from cymatix_context.backends import encoder_client

    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url=dead_url)
    assert codec._model is None
    codec.encode("hello")
    assert codec._model == "local-weights"


def test_dense_remote_failure_warns_loud_once_with_url_and_error(
    daemon_url, fake_local_dense, monkeypatch, caplog
):
    """#376: the fallback is loud — one WARNING naming the URL and the error.

    A repeat of the SAME failure class after the cooldown demotes to DEBUG,
    so a flapping daemon cannot spam a long ingest.
    """
    from cymatix_context.backends import encoder_client

    clock = [1_000.0]
    monkeypatch.setattr(encoder_client.time, "monotonic", lambda: clock[0])
    _DaemonHandler.fail_paths = {"/encode/dense"}

    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url=daemon_url)
    with caplog.at_level(logging.WARNING, logger="cymatix.encoder_client"):
        codec.encode("hello")
        clock[0] += encoder_client.retry_cooldown_s()
        codec.encode("world")  # cooldown elapsed: the RPC re-fails identically

    loud = [r for r in caplog.records if daemon_url in r.getMessage()]
    assert len(loud) == 1, (
        f"expected exactly one loud fallback; got {[r.getMessage() for r in loud]}"
    )
    assert loud[0].levelno == logging.WARNING
    message = loud[0].getMessage()
    assert "EncoderClientError" in message, "the warning must name the error"
    assert "fell back in-process" in message


def test_dense_unusable_payload_warns_loud_with_url(daemon_url, fake_local_dense, caplog):
    """A wrong-dim daemon is a named failure class, not a silent fallback."""
    from cymatix_context.backends import encoder_client

    _DaemonHandler.responses["/encode/dense"] = {"vectors": [[1.0, 0.0]], "dim": 2}
    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url=daemon_url)
    with caplog.at_level(logging.WARNING, logger="cymatix.encoder_client"):
        assert codec.encode("hello") == [0.5] * 4
    loud = [r for r in caplog.records if daemon_url in r.getMessage()]
    assert len(loud) == 1
    assert "unusable-payload" in loud[0].getMessage()


def test_dense_last_transport_flips_daemon_to_in_process_and_back(
    daemon_url, fake_local_dense, monkeypatch
):
    """#376: provenance records the transport actually used, per call."""
    from cymatix_context.backends import encoder_client

    clock = [1_000.0]
    monkeypatch.setattr(encoder_client.time, "monotonic", lambda: clock[0])

    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url=daemon_url)
    assert codec.last_transport is None, "no encode yet"
    codec.encode("hello")
    assert codec.last_transport == "daemon"

    _DaemonHandler.fail_paths = {"/encode/dense"}
    codec.encode("world")
    assert codec.last_transport == "in-process"

    _DaemonHandler.fail_paths = set()
    clock[0] += encoder_client.retry_cooldown_s()
    codec.encode("again")
    assert codec.last_transport == "daemon", "recovery is visible in provenance"


def test_dense_encode_never_raises_when_the_fallback_also_fails(dead_url, monkeypatch):
    """Broken install + dead daemon: degrade, never raise (no guard upstream)."""
    from cymatix_context.backends import bgem3_codec, encoder_client

    class _Exploding:
        def __init__(self, *a, **kw):
            pass

        def encode(self, text, task="passage"):
            raise RuntimeError("no sentence-transformers, no FlagEmbedding")

        def encode_batch(self, texts, task="passage"):
            raise RuntimeError("no sentence-transformers, no FlagEmbedding")

    monkeypatch.setattr(bgem3_codec, "BGEM3Codec", _Exploding)
    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url=dead_url)
    assert codec.encode("hello") == []
    assert codec.encode_batch(["a"]) == []


def test_dense_fallback_is_used_while_the_circuit_is_open(
    daemon_url, fake_local_dense, monkeypatch
):
    """A failure opens the circuit: no further RPC until the cooldown elapses."""
    from cymatix_context.backends import encoder_client

    clock = [1_000.0]
    monkeypatch.setattr(encoder_client.time, "monotonic", lambda: clock[0])
    _DaemonHandler.fail_paths = {"/encode/dense"}

    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url=daemon_url)
    assert codec.encode("hello") == [0.5] * 4
    attempts = len(_paths("/encode/dense"))

    assert codec.encode("hello again") == [0.5] * 4
    assert len(_paths("/encode/dense")) == attempts, "circuit open ⇒ no retry"

    _DaemonHandler.fail_paths = set()
    clock[0] += encoder_client.retry_cooldown_s()
    assert codec.encode("hello") == [0.25, -0.5, 0.75, 1.0], "retries after cooldown"


def test_dense_ready_gate_honors_its_budget(monkeypatch, fake_local_dense):
    """A dead daemon must cost the configured readiness budget, not hang."""
    import time

    from cymatix_context.backends import encoder_client

    monkeypatch.setenv("CYMATIX_ENCODER_READY_TIMEOUT_S", "0.3")
    codec = encoder_client.RemoteBGEM3Codec(
        dim=4, device="cpu", url=f"http://127.0.0.1:{_free_port()}",
    )
    t0 = time.monotonic()
    assert codec.encode("hello") == [0.5] * 4
    assert time.monotonic() - t0 < 0.3 * 3, "first-use gate must honor its budget"


def test_splade_falls_through_to_the_local_body_on_remote_failure(daemon_url, monkeypatch):
    from cymatix_context.backends import encoder_client, splade_backend

    _DaemonHandler.fail_paths = {"/encode/splade"}
    _arm_local_splade(monkeypatch)
    encoder_client.configure(daemon_url)

    with pytest.raises(_LocalSpladeSentinel):
        splade_backend.encode("hello world")
    assert _paths("/encode/splade"), "the remote was attempted before falling through"
    assert encoder_client.should_attempt() is False, "failure must open the circuit"


def test_sema_falls_back_per_call_and_retries_after_cooldown(
    daemon_url, fake_local_sema, monkeypatch
):
    """A network error is never cached the way _materialize caches ctor errors."""
    from cymatix_context.backends import encoder_client

    clock = [1_000.0]
    monkeypatch.setattr(encoder_client.time, "monotonic", lambda: clock[0])
    _DaemonHandler.fail_paths = {"/encode/sema"}

    codec = encoder_client.RemoteSemaCodec(url=daemon_url)
    assert codec.encode("hello") == [1.0] * 20, "per-call in-process fallback"
    attempts = len(_paths("/encode/sema"))

    _DaemonHandler.fail_paths = set()
    assert codec.encode("hello") == [1.0] * 20
    assert len(_paths("/encode/sema")) == attempts, "circuit open ⇒ no retry yet"

    clock[0] += encoder_client.retry_cooldown_s()
    assert codec.encode("hello") == [0.125] * 20, "remote retried after the cooldown"
    assert fake_local_sema.constructions == 1, "one internally-held lazy codec"


def test_sema_loaded_is_truthful_after_a_fallback_load(dead_url, fake_local_sema):
    from cymatix_context.backends import encoder_client

    codec = encoder_client.RemoteSemaCodec(url=dead_url)
    assert codec.loaded is False
    codec.encode("hello")
    assert codec.loaded is True
    assert codec.peek() is not None


def test_sema_last_transport_flips_daemon_to_in_process(
    daemon_url, fake_local_sema, monkeypatch
):
    """#376: the SEMA seam records its per-call transport too."""
    from cymatix_context.backends import encoder_client

    clock = [1_000.0]
    monkeypatch.setattr(encoder_client.time, "monotonic", lambda: clock[0])

    codec = encoder_client.RemoteSemaCodec(url=daemon_url)
    assert codec.last_transport is None, "no encode yet"
    codec.encode("hello")
    assert codec.last_transport == "daemon"

    _DaemonHandler.fail_paths = {"/encode/sema"}
    codec.encode("world")
    assert codec.last_transport == "in-process"


# ══ 4. Singleton semantics for the remote codec ══════════════════════


def test_remote_singleton_same_key_identity(daemon_url):
    from cymatix_context.backends import encoder_client
    from cymatix_context.backends.bgem3_codec import get_shared_codec

    encoder_client.configure(daemon_url)
    a = get_shared_codec(dim=1024, device="cpu")
    b = get_shared_codec(dim=1024, device="cpu")
    assert a is b
    assert isinstance(a, encoder_client.RemoteBGEM3Codec)


def test_remote_singleton_distinct_keys_distinct_instances(daemon_url):
    from cymatix_context.backends import encoder_client
    from cymatix_context.backends.bgem3_codec import get_shared_codec

    encoder_client.configure(daemon_url)
    full = get_shared_codec(dim=1024, device="cpu")
    assert isinstance(full, encoder_client.RemoteBGEM3Codec)
    assert get_shared_codec(dim=768, device="cpu") is not full
    assert get_shared_codec(dim=1024, device="cuda") is not full
    assert get_shared_codec(dim=1024, device="cpu", model_name="other") is not full


def test_remote_share_false_bypasses_the_cache(daemon_url):
    from cymatix_context.backends import encoder_client
    from cymatix_context.backends.bgem3_codec import _GLOBAL_CODECS, get_shared_codec

    encoder_client.configure(daemon_url)
    a = get_shared_codec(dim=1024, device="cpu", share=False)
    b = get_shared_codec(dim=1024, device="cpu", share=False)
    assert a is not b
    assert isinstance(a, encoder_client.RemoteBGEM3Codec)
    assert len(_GLOBAL_CODECS) == 0


@pytest.mark.concurrency
def test_remote_singleton_thread_safe_single_instance(daemon_url):
    from cymatix_context.backends import encoder_client
    from cymatix_context.backends.bgem3_codec import _GLOBAL_CODECS, get_shared_codec

    encoder_client.configure(daemon_url)
    results = []
    errors = []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait(timeout=5)
            results.append(get_shared_codec(dim=1024, device="cpu"))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "liveness: workers must not wedge"
    assert not errors
    assert len(results) == 8
    assert all(r is results[0] for r in results), "race produced >1 codec instance"
    assert len(_GLOBAL_CODECS) == 1


# ══ 5. Manager dense-ctor leak fix (source-text gate) ════════════════


def _method_body(src: str, signature: str) -> str:
    start = src.index(signature)
    rest = src[start:]
    end = rest.index("\n    def ", 1)
    return rest[:end]


def test_manager_dense_codec_routes_through_get_shared_codec():
    """The manager's own dense ctor was a substitution leak AND a CPU pin.

    Source-text gate (the test_layer_devices.py:95-114 precedent): the autouse
    ``_stub_dense_codec`` fixture in tests/conftest.py replaces
    ``CymatixContextManager._get_dense_codec`` wholesale in every non-live
    test, so the real body is unreachable at runtime.
    """
    import cymatix_context.context_manager as cm_mod

    src = Path(cm_mod.__file__).read_text(encoding="utf-8")
    body = _method_body(src, "def _get_dense_codec(self):")
    assert "get_shared_codec(" in body, "manager must share the process-wide codec"
    assert 'device=resolve_layer_device("dense")' in body, "per-layer device, not the cpu pin"
    assert "BGEM3Codec(" not in body, "direct construction bypasses the remote seam"


# ══ 6. Shared one-shot readiness gate ════════════════════════════════


def test_shared_ready_gate_is_polled_once_across_all_four_seams(daemon_url):
    from cymatix_context.backends import encoder_client, rerank_backend, splade_backend
    from cymatix_context.backends.bgem3_codec import get_shared_codec

    encoder_client.configure(daemon_url)
    splade_backend.encode("hello")
    encoder_client.RemoteSemaCodec(url=daemon_url).encode("hello")
    get_shared_codec(dim=4, device="cpu").encode("hello")
    rerank_backend.score_pairs("hello", ["world"])

    assert len(_paths("/ready")) == 1, "one daemon, one readiness question"
    assert encoder_client.ready_probed() is True


def test_cold_daemon_gate_blocks_encodes_then_all_four_seams_recover(
    daemon_url, monkeypatch, fake_local_dense, fake_local_sema
):
    """The startup race: a loading daemon must not permanently disable remote.

    SPLADE/SEMA encode ahead of dense on the ingest path. Without a shared
    gate, the first of them hammers a loading daemon, opens the process-wide
    circuit, and dense — never reaching its own gate — caches a real ~2 GB
    in-process codec for the life of the process. rerank (#341) joins the
    same shared gate.
    """
    import time

    from cymatix_context.backends import encoder_client, rerank_backend, splade_backend
    from cymatix_context.backends.bgem3_codec import get_shared_codec

    monkeypatch.setenv("CYMATIX_ENCODER_READY_TIMEOUT_S", "0.2")
    _DaemonHandler.ready = False  # still loading its models
    encoder_client.configure(daemon_url)
    _arm_local_splade(monkeypatch)
    _arm_local_rerank(monkeypatch)

    # SPLADE first, exactly as ingest orders it.
    with pytest.raises(_LocalSpladeSentinel):
        splade_backend.encode("hello")
    assert _paths("/ready"), "the gate asked before encoding"
    assert not [p for p, _ in _DaemonHandler.received if p.startswith("/encode/")], (
        "a cold daemon must not be hammered with encode RPCs"
    )
    assert encoder_client.should_attempt() is False, "not-ready opens the circuit"

    sema = encoder_client.RemoteSemaCodec(url=daemon_url)
    dense = get_shared_codec(dim=4, device="cpu")
    assert sema.encode("hello") == [1.0] * 20, "SEMA serves from the fallback"
    assert dense.encode("hello") == [0.5] * 4, "dense serves from the fallback"
    with pytest.raises(_LocalRerankSentinel):
        rerank_backend.score_pairs("hello", ["world"])
    assert not [p for p, _ in _DaemonHandler.received if p.startswith("/encode/")]
    probes = len(_paths("/ready"))

    # The daemon finishes loading and the cooldown elapses.
    _DaemonHandler.ready = True
    now = time.monotonic()
    monkeypatch.setattr(
        encoder_client.time,
        "monotonic",
        lambda: now + encoder_client.retry_cooldown_s() + 1.0,
    )

    assert splade_backend.encode("hello") == {"beta": 0.5, "alpha": 0.25, "gamma": 0.1234}
    assert sema.encode("hello") == [0.125] * 20
    assert dense.encode("hello") == [0.25, -0.5, 0.75, 1.0], (
        "dense must not be stuck on its cached fallback"
    )
    assert rerank_backend.score_pairs("hello", ["world"]) == [0.9]
    assert len(_paths("/ready")) == probes, "the gate is one-shot, not re-polled"


@pytest.mark.concurrency
def test_cold_daemon_race_across_seams_fires_no_encode_rpc(
    daemon_url, monkeypatch, fake_local_dense, fake_local_sema
):
    """Threads that lose the gate's lock race must see the real probe result.

    A blocked waiter returning a hardcoded True would fire a live RPC at the
    daemon the winning thread just confirmed cold — the exact outcome the gate
    exists to prevent, and one neither seam re-checks for afterwards.
    """
    from cymatix_context.backends import encoder_client, rerank_backend, splade_backend
    from cymatix_context.backends.bgem3_codec import get_shared_codec

    monkeypatch.setenv("CYMATIX_ENCODER_READY_TIMEOUT_S", "0.3")
    _DaemonHandler.ready = False  # still loading
    encoder_client.configure(daemon_url)
    _arm_local_splade(monkeypatch)
    _arm_local_rerank(monkeypatch)

    sema = encoder_client.RemoteSemaCodec(url=daemon_url)
    dense = get_shared_codec(dim=4, device="cpu")

    barrier = threading.Barrier(4)
    results: dict = {}
    errors: list = []

    def _run(name, fn):
        try:
            barrier.wait(timeout=5)
            results[name] = fn()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append((name, exc))

    def _splade_call():
        try:
            splade_backend.encode("hello")
        except _LocalSpladeSentinel:
            return "local"
        return "remote"

    def _rerank_call():
        try:
            rerank_backend.score_pairs("hello", ["world"])
        except _LocalRerankSentinel:
            return "local"
        return "remote"

    threads = [
        threading.Thread(target=_run, args=("splade", _splade_call), daemon=True),
        threading.Thread(target=_run, args=("sema", lambda: sema.encode("hello")), daemon=True),
        threading.Thread(target=_run, args=("dense", lambda: dense.encode("hello")), daemon=True),
        threading.Thread(target=_run, args=("rerank", _rerank_call), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not any(t.is_alive() for t in threads), "liveness: the gate must not wedge"
    assert not errors, f"seams must degrade, not raise: {errors}"
    assert results["splade"] == "local"
    assert results["sema"] == [1.0] * 20
    assert results["dense"] == [0.5] * 4
    assert results["rerank"] == "local"
    assert not [p for p, _ in _DaemonHandler.received if p.startswith("/encode/")], (
        "a race loser must not RPC a daemon the winner just found cold"
    )
    assert len(_paths("/ready")) == 1, "one probe for the whole race"


# ══ 7. The daemon must never become a client of itself ═══════════════


def test_daemon_flag_forces_active_url_off(monkeypatch):
    from cymatix_context.backends import encoder_client

    monkeypatch.setenv("CYMATIX_ENCODER_URL", "http://127.0.0.1:11439")
    encoder_client.configure("http://127.0.0.1:11439")
    assert encoder_client.active_url() == "http://127.0.0.1:11439"

    encoder_client.mark_encoder_daemon_process()
    assert encoder_client.is_encoder_daemon_process() is True
    assert encoder_client.active_url() == "", "the daemon must not resolve a URL for itself"


def test_reset_for_test_clears_the_daemon_flag():
    from cymatix_context.backends import encoder_client

    encoder_client.mark_encoder_daemon_process()
    encoder_client.reset_for_test()
    assert encoder_client.is_encoder_daemon_process() is False


def test_daemon_marked_process_keeps_the_dense_and_splade_seams_in_process(monkeypatch):
    """The exact recursion hazard: URL exported, daemon started from that shell."""
    from cymatix_context.backends import encoder_client, splade_backend
    from cymatix_context.backends.bgem3_codec import BGEM3Codec, get_shared_codec

    monkeypatch.setenv("CYMATIX_ENCODER_URL", "http://127.0.0.1:11439")

    def _no_client(*a, **kw):
        raise AssertionError("the daemon must not build an encoder client")

    monkeypatch.setattr(encoder_client, "EncoderClient", _no_client)
    encoder_client.mark_encoder_daemon_process()
    _arm_local_splade(monkeypatch)

    # EncoderRegistry.load() goes through both of these.
    assert isinstance(get_shared_codec(dim=1024, device="cpu"), BGEM3Codec)
    with pytest.raises(_LocalSpladeSentinel):
        splade_backend.encode("hello world")


def test_create_encoder_app_marks_the_process_as_the_daemon(monkeypatch):
    from cymatix_context.backends import encoder_client
    from cymatix_context.encoder_daemon import create_encoder_app
    from tests.test_encoder_daemon import make_registry

    monkeypatch.setenv("CYMATIX_ENCODER_URL", "http://127.0.0.1:11439")
    create_encoder_app(make_registry())
    assert encoder_client.is_encoder_daemon_process() is True
    assert encoder_client.active_url() == ""


def test_daemon_app_bringup_never_calls_the_client_transport(monkeypatch):
    from fastapi.testclient import TestClient

    from cymatix_context.backends import encoder_client
    from cymatix_context.encoder_daemon import create_encoder_app
    from tests.test_encoder_daemon import make_registry

    monkeypatch.setenv("CYMATIX_ENCODER_URL", "http://127.0.0.1:11439")

    def _no_network(*a, **kw):
        raise AssertionError("the daemon must never act as a client of itself")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)

    app = create_encoder_app(make_registry())
    with TestClient(app) as client:  # lifespan → bring_up → warm + probe + rates
        assert client.get("/ready").status_code == 200
        assert client.post(
            "/encode/splade", json={"texts": ["hello"], "top_k": 8}
        ).status_code == 200
    assert encoder_client.active_url() == ""


def test_main_marks_the_daemon_process_before_building_the_app(monkeypatch):
    from cymatix_context import encoder_daemon
    from cymatix_context.backends import encoder_client

    monkeypatch.setenv("CYMATIX_ENCODER_URL", "http://127.0.0.1:11439")
    seen: dict = {}
    monkeypatch.setattr(encoder_daemon, "init_hardware", lambda cfg: None)
    monkeypatch.setattr(
        encoder_daemon,
        "create_encoder_app",
        lambda *a, **kw: seen.setdefault("url_at_app_build", encoder_client.active_url()),
    )
    monkeypatch.setattr(encoder_daemon.uvicorn, "run", lambda *a, **kw: seen.update(ran=True))

    encoder_daemon.main([])

    assert seen["url_at_app_build"] == "", "the flag must be set before the app exists"
    assert seen.get("ran") is True
    assert encoder_client.is_encoder_daemon_process() is True


# ══ 8. --deterministic ingest profile never consults the daemon ══════


# The deterministic ingest still runs CpuTagger.pack(), which needs the
# en_core_web_sm pipeline (#313).
@requires_spacy_model
def test_deterministic_profile_never_touches_the_network(tmp_path, monkeypatch):
    """cymatix ingest --deterministic: flags gate upstream of every seam."""
    from cymatix_context.backends import encoder_client
    from cymatix_context.config import EncoderDaemonConfig, GenomeConfig, IngestionConfig
    from cymatix_context.context_manager import CymatixContextManager

    def _no_network(*a, **kw):
        raise AssertionError("--deterministic must not consult the encoder daemon")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)

    cfg = make_cymatix_config(
        genome=GenomeConfig(path=str(tmp_path / "genome.db"), cold_start_threshold=5),
        ingestion=IngestionConfig(
            splade_enabled=False,
            sema_embed_on_ingest=False,
            dense_embed_on_ingest=False,
        ),
        encoder_daemon=EncoderDaemonConfig(url="http://127.0.0.1:11439"),
    )
    mgr = CymatixContextManager(cfg)
    assert encoder_client.active_url() == "http://127.0.0.1:11439"
    assert mgr._sema_codec is None
    assert mgr._get_dense_codec() is None
    mgr.ingest("deterministic profile: cache invalidation notes for the retry budget")


# ══ 9. Batch-size-scaled timeout (Finding 1) ══════════════════════════
#
# A whole-batch POST (RemoteBGEM3Codec.encode_batch, RemoteSemaCodec.
# encode_batch, splade_backend.encode_batch's remote path) sent under the
# flat 10s default times out client-side once the batch is big enough
# (CPU dense ~18.6 enc/s -> ~185 texts), which dumps every worker onto the
# in-process fallback and opens the shared circuit. These tests monkeypatch
# the transport directly (not the HTTPServer fixture) so the captured
# `timeout=` kwarg can be asserted exactly against the scaling formula.


def _install_capturing_transport(monkeypatch):
    """Monkeypatch urllib.request.urlopen; returns a list of (path, timeout).

    Serves minimally-shaped canned JSON for /ready and the three /encode/*
    endpoints, sized to the request's own texts list so N-in/N-out holds.
    """
    from urllib.parse import urlsplit

    calls: list = []

    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=None):
        path = urlsplit(req.full_url).path
        calls.append((path, timeout))
        if path == "/ready":
            payload = {"ready": True, "spawn_token": "tok", "probe_ms": 1.0}
        elif path == "/encode/dense":
            n = len(json.loads(req.data)["texts"])
            payload = {"vectors": [[0.1, 0.2, 0.3, 0.4]] * n, "dim": 4}
        elif path == "/encode/splade":
            n = len(json.loads(req.data)["texts"])
            payload = {"sparse": [{"t": 0.1}] * n}
        elif path == "/encode/sema":
            n = len(json.loads(req.data)["texts"])
            payload = {"vectors": [[0.1] * 20] * n}
        elif path == "/encode/rerank":
            n = len(json.loads(req.data)["texts"])
            payload = {"scores": [0.1] * n}
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected path {path}")
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return calls


def _timeouts_for(calls, path) -> list:
    return [t for p, t in calls if p == path]


def test_dense_encode_batch_timeout_scales_with_batch_size(monkeypatch):
    from cymatix_context.backends import encoder_client

    calls = _install_capturing_transport(monkeypatch)
    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url="http://127.0.0.1:1")
    texts = [f"t{i}" for i in range(37)]
    codec.encode_batch(texts)

    encode_calls = _timeouts_for(calls, "/encode/dense")
    assert len(encode_calls) == 1
    assert encode_calls[0] == pytest.approx(encoder_client.batch_timeout_s(37))
    assert encode_calls[0] > encoder_client.default_timeout_s(), "must exceed the flat default"


def test_dense_encode_single_item_keeps_flat_default_timeout(monkeypatch):
    from cymatix_context.backends import encoder_client

    calls = _install_capturing_transport(monkeypatch)
    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url="http://127.0.0.1:1")
    codec.encode("hello")

    assert _timeouts_for(calls, "/encode/dense") == [encoder_client.default_timeout_s()]


def test_sema_encode_batch_timeout_scales_with_batch_size(monkeypatch):
    from cymatix_context.backends import encoder_client

    calls = _install_capturing_transport(monkeypatch)
    codec = encoder_client.RemoteSemaCodec(url="http://127.0.0.1:1")
    texts = [f"t{i}" for i in range(40)]
    codec.encode_batch(texts)

    encode_calls = _timeouts_for(calls, "/encode/sema")
    assert len(encode_calls) == 1
    assert encode_calls[0] == pytest.approx(encoder_client.batch_timeout_s(40))
    assert encode_calls[0] > encoder_client.default_timeout_s()


def test_sema_encode_single_item_keeps_flat_default_timeout(monkeypatch):
    from cymatix_context.backends import encoder_client

    calls = _install_capturing_transport(monkeypatch)
    codec = encoder_client.RemoteSemaCodec(url="http://127.0.0.1:1")
    codec.encode("hello")

    assert _timeouts_for(calls, "/encode/sema") == [encoder_client.default_timeout_s()]


def test_splade_encode_batch_timeout_scales_with_batch_size(monkeypatch):
    from cymatix_context.backends import encoder_client, splade_backend

    calls = _install_capturing_transport(monkeypatch)
    encoder_client.configure("http://127.0.0.1:1")
    texts = [f"t{i}" for i in range(50)]
    splade_backend.encode_batch(texts)

    encode_calls = _timeouts_for(calls, "/encode/splade")
    assert len(encode_calls) == 1
    assert encode_calls[0] == pytest.approx(encoder_client.batch_timeout_s(50))
    assert encode_calls[0] > encoder_client.default_timeout_s()


def test_splade_encode_single_item_keeps_flat_default_timeout(monkeypatch):
    from cymatix_context.backends import encoder_client, splade_backend

    calls = _install_capturing_transport(monkeypatch)
    encoder_client.configure("http://127.0.0.1:1")
    splade_backend.encode("hello world")

    assert _timeouts_for(calls, "/encode/splade") == [encoder_client.default_timeout_s()]


def test_rerank_score_pairs_timeout_scales_with_text_count(monkeypatch):
    """50 texts -> default_timeout_s() + 50*per_item_timeout_s() == 10.0 + 12.5 == 22.5s."""
    from cymatix_context.backends import encoder_client, rerank_backend

    calls = _install_capturing_transport(monkeypatch)
    encoder_client.configure("http://127.0.0.1:1")
    texts = [f"t{i}" for i in range(50)]
    rerank_backend.score_pairs("q", texts)

    encode_calls = _timeouts_for(calls, "/encode/rerank")
    assert len(encode_calls) == 1
    assert encode_calls[0] == pytest.approx(22.5)
    assert encode_calls[0] == pytest.approx(encoder_client.batch_timeout_s(50))
    assert encode_calls[0] > encoder_client.default_timeout_s()


def test_batch_timeout_scaling_honors_per_item_env_knob(monkeypatch):
    from cymatix_context.backends import encoder_client

    monkeypatch.setenv("CYMATIX_ENCODER_PER_ITEM_TIMEOUT_S", "1.0")
    calls = _install_capturing_transport(monkeypatch)
    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url="http://127.0.0.1:1")
    codec.encode_batch(["a", "b", "c"])

    encode_calls = _timeouts_for(calls, "/encode/dense")
    assert encode_calls[0] == pytest.approx(encoder_client.default_timeout_s() + 3 * 1.0)


def test_batch_timeout_does_not_construct_a_new_client(monkeypatch):
    """The scaled timeout must be a per-call override, not a fresh EncoderClient."""
    from cymatix_context.backends import encoder_client

    _install_capturing_transport(monkeypatch)
    codec = encoder_client.RemoteBGEM3Codec(dim=4, device="cpu", url="http://127.0.0.1:1")
    client_before = codec._client
    codec.encode_batch(["a", "b", "c", "d", "e"])
    assert codec._client is client_before, "encode_batch must reuse the one held client"

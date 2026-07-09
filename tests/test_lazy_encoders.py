"""#219 slice 2 — lazy encoder loading (council Option-A rider).

A serving process at 829K genes measured 20.3 GB RSS because the encoder
stack loaded eagerly at manager init; every process (tray backend, bench
server, build workers) paid the full stack whether or not the workload
touched it — the #176/#191 3-CUDA-context incident class. This suite
verifies the fix WITHOUT any real models: constructors are monkeypatched
with counting (or raising) fakes.

Covered:
- HelixContextManager init constructs NO encoder (sema / spacy / splade /
  bgem3 / deberta all untouched);
- first use constructs exactly once; later uses reuse the instance;
- concurrent first use constructs exactly once (double-checked lock);
- [hardware] lazy_encoders = false restores eager construction at init;
- GET /admin/components reports loaded-ness without forcing a load;
- DeBERTa factory failure falls back to the disabled ribosome (the old
  eager except-branch end state).
"""

from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from helix_context.backends import sema as sema_mod
from helix_context.config import (
    GenomeConfig,
    Hardware,
    HelixConfig,
    RibosomeConfig,
    ServerConfig,
    load_config,
)
from helix_context.context_manager import HelixContextManager, LazyRibosome

from tests.conftest import make_helix_config


# ── test isolation: pristine view of the process-lifetime singletons ──
#
# The encoder caches these tests assert on (spaCy in ``tagger._nlp``, the
# SPLADE model/tokenizer/device in ``splade_backend``) are DELIBERATE
# process-lifetime singletons — first use loads them once and every later
# caller in the process reuses them. That is correct production behaviour
# and must NOT change.
#
# But it makes these init-state tests order-dependent: any earlier test in
# the full suite that exercises the real encoders (e.g.
# ``tests/test_build_fixture_matrix.py::TestParallel`` tags real files and
# drains genes) leaves those module globals populated, so
# ``test_manager_init_constructs_no_encoders`` /
# ``test_admin_components_reports_unloaded_without_loading`` then observe a
# loaded encoder and fail — while passing in isolation.
#
# This autouse fixture snapshots-and-nulls those globals via monkeypatch so
# every test in this file starts from a pristine (unloaded) view, and
# monkeypatch auto-restores the originals on teardown so a real polluter's
# singletons — and real users' process-lifetime caching — are untouched.
# None of the tests here load the real models (they inject counting/raising
# fakes), so the unloaded baseline is exactly what they already assume.
@pytest.fixture(autouse=True)
def _pristine_encoder_singletons(monkeypatch):
    import helix_context.tagger as tagger_mod
    from helix_context.backends import splade_backend

    monkeypatch.setattr(tagger_mod, "_nlp", None, raising=False)
    monkeypatch.setattr(splade_backend, "_model", None, raising=False)
    monkeypatch.setattr(splade_backend, "_tokenizer", None, raising=False)
    monkeypatch.setattr(splade_backend, "_device", None, raising=False)


# ── fakes ────────────────────────────────────────────────────────────


class CountingSemaCodec:
    """Stands in for SemaCodec; records constructions and encodes."""

    constructions = 0
    construct_delay_s = 0.0

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", device=None):
        if type(self).construct_delay_s:
            time.sleep(type(self).construct_delay_s)
        type(self).constructions += 1
        self.model_name = model_name
        self.device = device

    def encode(self, text):
        return [0.0] * 20

    def encode_batch(self, texts, batch_size: int = 64):
        return [[0.0] * 20 for _ in texts]

    def signature(self, text, top_k: int = 5):
        return []

    def fingerprint(self, text):
        return ""

    @property
    def projection_matrix(self):
        return None

    @property
    def embed_dim(self):
        return 384


@pytest.fixture
def fake_sema(monkeypatch):
    """Pretend sentence-transformers is installed; count SemaCodec builds."""

    class _Fake(CountingSemaCodec):
        constructions = 0
        construct_delay_s = 0.0

    monkeypatch.setattr(sema_mod, "sema_available", lambda: True)
    monkeypatch.setattr(sema_mod, "SemaCodec", _Fake)
    return _Fake


@pytest.fixture
def fake_deberta(monkeypatch):
    """Inject a fake deberta_backend module (real one imports torch)."""

    mod = types.ModuleType("helix_context.backends.deberta_backend")

    class FakeDeBERTaRibosome:
        constructions = 0

        def __init__(self, **kwargs):
            type(self).constructions += 1
            self.kwargs = kwargs

        def re_rank(self, query, candidates, k: int = 5):
            return list(candidates)[:k]

        def classify_relations(self, candidates):
            return {}

    mod.DeBERTaRibosome = FakeDeBERTaRibosome
    monkeypatch.setitem(
        sys.modules, "helix_context.backends.deberta_backend", mod
    )
    return FakeDeBERTaRibosome


def _config(tmp_path, **hardware_kwargs) -> HelixConfig:
    # Shares conftest's HelixConfig shape (genome/server sections); this
    # file's own genome path (a real tmp file, not ":memory:") and the
    # [hardware] override are passed explicitly since the lazy-encoder
    # suite needs per-test hardware knobs make_helix_config doesn't cover.
    return make_helix_config(
        genome=GenomeConfig(path=str(tmp_path / "genome.db"), cold_start_threshold=5),
        server=ServerConfig(upstream="http://localhost:11434"),
        hardware=Hardware(**hardware_kwargs),
    )


def _deberta_config(tmp_path, lazy: bool = True) -> HelixConfig:
    return make_helix_config(
        genome=GenomeConfig(path=str(tmp_path / "genome.db"), cold_start_threshold=5),
        server=ServerConfig(upstream="http://localhost:11434"),
        ribosome=RibosomeConfig(
            enabled=True, backend="deberta", model="mock", warmup=False, timeout=2,
        ),
        hardware=Hardware(lazy_encoders=lazy),
    )


# ── init must not construct anything ─────────────────────────────────


def test_manager_init_constructs_no_encoders(tmp_path, fake_sema, monkeypatch):
    # spaCy: explode if anything tries to load it during init.
    fake_spacy = types.ModuleType("spacy")

    def _no_load(*args, **kwargs):
        raise AssertionError("spacy.load called during manager init")

    fake_spacy.load = _no_load
    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)

    import helix_context.tagger as tagger_mod
    from helix_context.backends import splade_backend

    mgr = HelixContextManager(_config(tmp_path))

    # SemaCodec deferred: proxy armed, model not constructed.
    assert fake_sema.constructions == 0
    assert isinstance(mgr._sema_codec, sema_mod.LazySemaCodec)
    assert mgr._sema_codec.loaded is False
    # The knowledge store received the same proxy, so the store-side
    # tier-4 encode is the (single) load trigger.
    assert mgr.genome._sema_codec is mgr._sema_codec
    # Already-lazy components stayed unloaded too.
    assert splade_backend._model is None
    assert getattr(tagger_mod, "_nlp", None) is None
    assert mgr._dense_codec is None
    assert getattr(mgr.genome, "_dense_codec", None) is None


def test_sema_disabled_when_dependency_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sema_mod, "sema_available", lambda: False)
    mgr = HelixContextManager(_config(tmp_path))
    assert mgr._sema_codec is None


# ── first use semantics ──────────────────────────────────────────────


def test_first_use_constructs_exactly_once(tmp_path, fake_sema):
    mgr = HelixContextManager(_config(tmp_path))
    codec = mgr._sema_codec
    assert fake_sema.constructions == 0

    vec = codec.encode("what does the splice step do?")
    assert vec == [0.0] * 20
    assert fake_sema.constructions == 1
    assert codec.loaded is True
    assert codec.peek() is not None

    codec.encode("again")
    codec.encode_batch(["a", "b"])
    assert fake_sema.constructions == 1


def test_static_math_does_not_force_load(tmp_path, fake_sema):
    mgr = HelixContextManager(_config(tmp_path))
    codec = mgr._sema_codec
    e0 = [1.0] + [0.0] * 19
    assert codec.similarity(e0, e0) == pytest.approx(1.0)
    nearest = codec.nearest(e0, [("g1", e0)], k=1)
    assert nearest[0][0] == "g1"
    assert fake_sema.constructions == 0
    assert codec.loaded is False


def test_concurrent_first_use_constructs_once(tmp_path, fake_sema):
    fake_sema.construct_delay_s = 0.05
    mgr = HelixContextManager(_config(tmp_path))
    codec = mgr._sema_codec
    errors = []
    barrier = threading.Barrier(8)

    def hit():
        try:
            barrier.wait(timeout=5)
            codec.encode("race")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors
    assert fake_sema.constructions == 1


def test_sema_load_failure_cached_not_retried(tmp_path, fake_sema, monkeypatch):
    boom_calls = []

    class _Boom:
        def __init__(self, **kwargs):
            boom_calls.append(1)
            raise RuntimeError("model download failed")

    monkeypatch.setattr(sema_mod, "SemaCodec", _Boom)
    mgr = HelixContextManager(_config(tmp_path))
    codec = mgr._sema_codec
    with pytest.raises(RuntimeError):
        codec.encode("x")
    with pytest.raises(RuntimeError):
        codec.encode("y")
    # Construction attempted exactly once; the failure is cached.
    assert len(boom_calls) == 1
    assert codec.loaded is False


# ── the [hardware] lazy_encoders knob ────────────────────────────────


def test_lazy_encoders_false_restores_eager(tmp_path, fake_sema):
    mgr = HelixContextManager(_config(tmp_path, lazy_encoders=False))
    assert fake_sema.constructions == 1
    assert mgr._sema_codec is not None
    assert mgr._sema_codec.loaded is True


def test_lazy_encoders_knob_parses_from_toml(tmp_path):
    p = tmp_path / "helix.toml"
    p.write_text("[hardware]\nlazy_encoders = false\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.hardware.lazy_encoders is False
    # Default stays true (lazy).
    assert HelixConfig().hardware.lazy_encoders is True
    p2 = tmp_path / "helix2.toml"
    p2.write_text("[hardware]\ndevice = \"cpu\"\n", encoding="utf-8")
    assert load_config(str(p2)).hardware.lazy_encoders is True


# ── DeBERTa ribosome ─────────────────────────────────────────────────


def test_deberta_lazy_until_first_use(tmp_path, fake_deberta):
    mgr = HelixContextManager(_deberta_config(tmp_path))
    assert fake_deberta.constructions == 0
    assert isinstance(mgr.ribosome, LazyRibosome)
    assert mgr.ribosome.loaded is False
    assert mgr.ribosome.peek() is None
    assert mgr.ribosome.label == "deberta"

    out = mgr.ribosome.re_rank("q", [], k=3)
    assert out == []
    assert fake_deberta.constructions == 1
    assert mgr.ribosome.loaded is True

    mgr.ribosome.re_rank("q2", [], k=3)
    assert mgr.ribosome.classify_relations([]) == {}
    assert fake_deberta.constructions == 1
    # Exact construction args preserved from config.
    built = mgr.ribosome.peek()
    assert built.kwargs["rerank_model_path"] == "training/models/rerank"
    assert built.kwargs["splice_threshold"] == 0.5


def test_deberta_eager_when_knob_off(tmp_path, fake_deberta):
    mgr = HelixContextManager(_deberta_config(tmp_path, lazy=False))
    assert fake_deberta.constructions == 1
    assert type(mgr.ribosome).__name__ == "FakeDeBERTaRibosome"


def test_deberta_load_failure_falls_back_to_disabled(tmp_path, monkeypatch):
    mod = types.ModuleType("helix_context.backends.deberta_backend")

    class _Boom:
        def __init__(self, **kwargs):
            raise RuntimeError("no models on disk")

    mod.DeBERTaRibosome = _Boom
    monkeypatch.setitem(
        sys.modules, "helix_context.backends.deberta_backend", mod
    )
    mgr = HelixContextManager(_deberta_config(tmp_path))
    rib = mgr.ribosome
    assert rib.loaded is False
    # First use: factory raises -> permanent fallback to disabled ribosome.
    assert getattr(rib.backend, "is_disabled_backend", False) is True
    assert rib.loaded is True  # fallback is resident now
    # hasattr probe used by Step 3.5 sees no classify_relations on fallback.
    assert not hasattr(rib, "classify_relations")


def test_lazy_ribosome_private_lookup_never_loads(tmp_path, fake_deberta):
    mgr = HelixContextManager(_deberta_config(tmp_path))
    rib = mgr.ribosome
    # Introspection on private/dunder names must not materialize models.
    assert not hasattr(rib, "_nli")
    assert not hasattr(rib, "__wrapped__")
    assert fake_deberta.constructions == 0
    assert rib.loaded is False


# ── /admin/components reports without forcing loads ──────────────────


def test_admin_components_reports_unloaded_without_loading(tmp_path, fake_sema):
    from fastapi.testclient import TestClient

    from helix_context.server import create_app

    app = create_app(_config(tmp_path))
    client = TestClient(app)

    resp = client.get("/admin/components")
    assert resp.status_code == 200
    data = resp.json()
    by_name = {c["name"]: c for c in data["components"]}

    assert "sema" in by_name
    assert by_name["sema"]["loaded"] is False
    assert by_name["sema"]["status"] == "idle (not loaded)"
    # The probe itself must not have constructed the model.
    assert fake_sema.constructions == 0
    assert app.state.helix._sema_codec.loaded is False

    # cpu_tagger configured (default backend "cpu") but spaCy not resident.
    if "cpu_tagger" in by_name:
        assert by_name["cpu_tagger"]["loaded"] is False
        assert by_name["cpu_tagger"]["status"] == "idle (not loaded)"

    # Default ribosome is disabled -> omitted, exactly as before.
    assert "ribosome" not in by_name

    # After first use the panel flips to loaded without further builds.
    app.state.helix._sema_codec.encode("warm me up")
    data2 = client.get("/admin/components").json()
    by_name2 = {c["name"]: c for c in data2["components"]}
    assert by_name2["sema"]["loaded"] is True
    assert by_name2["sema"]["status"] in ("running", "idle")
    assert fake_sema.constructions == 1


def test_admin_components_lazy_deberta_listed_unloaded(tmp_path, fake_deberta, fake_sema):
    from fastapi.testclient import TestClient

    from helix_context.server import create_app

    app = create_app(_deberta_config(tmp_path))
    client = TestClient(app)
    resp = client.get("/admin/components")
    assert resp.status_code == 200
    by_name = {c["name"]: c for c in resp.json()["components"]}
    # Configured-but-unloaded deberta ribosome is listed (it is neither
    # paused nor disabled) and reports its unloaded state.
    assert "ribosome" in by_name
    assert by_name["ribosome"]["loaded"] is False
    assert by_name["ribosome"]["status"] == "idle (not loaded)"
    assert by_name["ribosome"]["backend"] == "deberta"
    # And the panel did not force the DeBERTa load.
    assert fake_deberta.constructions == 0

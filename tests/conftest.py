"""Shared fixtures for Helix Context tests."""

import contextlib
import hashlib
import io
import json
import os
import random
import re
import pytest
from pathlib import Path

# Guard: the module-level `app = create_app()` in server.py (used by uvicorn
# --reload) runs at import time. Without a real genome path it raises
# sqlite3.OperationalError during collection. Set :memory: so tests can
# import cymatix_context.server without a real DB file on disk.
os.environ.setdefault("HELIX_GENOME_PATH", ":memory:")

from cymatix_context.config import (
    BudgetConfig,
    GenomeConfig,
    HelixConfig,
    RibosomeConfig,
    ServerConfig,
)
from cymatix_context.genome import Genome
from cymatix_context.schemas import Gene, PromoterTags, EpigeneticMarkers, ChromatinState


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── Fake BGE-M3 dense codec (shared test stand-in) ───────────────────
#
# Tier-0 PR-3 (2026-05-16) flipped `[retrieval] dense_embedding_enabled`
# from false to true. As a result `knowledge_store.query_docs` now routes
# into `query_docs_dense_recall`, which calls `_get_dense_codec()` — and
# the real `_get_dense_codec` lazy-builds a `BGEM3Codec`, pulling the
# ~2 GB BGE-M3 weights via sentence-transformers / FlagEmbedding. On any
# machine with the model cached, every non-`live` test that runs retrieval
# against a v2-populated genome would load and run the real model, taking
# the non-live suite from minutes to hours.
#
# `FakeBGEM3Codec` is a pure-numpy, hash-seeded, deterministic stand-in
# with the exact interface the real `BGEM3Codec` exposes (`encode`,
# `encode_batch`, `similarity`, plus the `task=` kwarg). The `_stub_dense_codec`
# autouse fixture below installs it for every non-`live` test. It is the
# single definition of the fake — `tests/test_dense_recall.py` and
# `tests/test_ingest_dense_v2.py` import it from here instead of each
# carrying a private `_FakeCodec` copy.


def hash_vec(text: str, dim: int = 1024):
    """Deterministic L2-normalised fp32 vector seeded from ``text``.

    SHA-256-seeded gaussian draw, then L2-normalised — exactly the shape
    contract of a real BGE-M3 encode (unit-norm fp32 of the given dim).
    Two distinct texts produce near-orthogonal vectors (E[cosine] ≈ 0,
    std ≈ 1/sqrt(dim)), so the fake reproduces the real codec's
    random-pair statistics well enough for retrieval-quality assertions.
    """
    import numpy as np

    out = np.zeros(dim, dtype=np.float32)
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(seed[:8], "little"))
    for i in range(dim):
        out[i] = rng.gauss(0.0, 1.0)
    n = np.linalg.norm(out)
    if n > 0:
        out /= n
    return out


class FakeBGEM3Codec:
    """Deterministic, in-process stand-in for ``BGEM3Codec``.

    Mirrors the real codec's public surface so it is a drop-in for both
    the retrieval path (``encode(query, task="query")``) and the ingest
    path (``encode_batch(texts, task="passage")``):

    - ``encode(text, task=...)``  → unit-norm fp32 list of length ``dim``
    - ``encode_batch(texts, task=...)`` → list of such lists
    - ``similarity(a, b)``        → dot product (cosine for unit vectors)
    - ``dim``                     → embedding dimension

    ``query_target``: when set, ``encode(text, task="query")`` returns the
    vector for ``query_target`` instead of for ``text`` — this lets a test
    stage a deterministic "this query matches document X" relationship
    (the query encodes to exactly X's passage vector, so cosine == 1.0).

    ``encode_calls`` / ``batch_calls`` count invocations so call-count
    assertions (e.g. tests/test_ingest_dense_v2.py) keep working.

    No model, no I/O — sub-millisecond per encode.
    """

    def __init__(self, dim: int = 1024, query_target: str | None = None):
        self.dim = dim
        self._query_target = query_target
        self.encode_calls = 0
        self.batch_calls = 0

    def encode(self, text: str, task: str = "passage"):
        self.encode_calls += 1
        if task == "query" and self._query_target is not None:
            return hash_vec(self._query_target, self.dim).tolist()
        return hash_vec(text, self.dim).tolist()

    def encode_batch(self, texts, task: str = "passage"):
        self.batch_calls += 1
        if not texts:
            return []
        return [hash_vec(t, self.dim).tolist() for t in texts]

    def similarity(self, a, b) -> float:
        import numpy as np

        return float(np.dot(np.asarray(a), np.asarray(b)))


@pytest.fixture(autouse=True)
def _stub_dense_codec(request, monkeypatch):
    """Autouse: replace the BGE-M3 dense codec with ``FakeBGEM3Codec``.

    For every test NOT marked ``live``, this monkeypatches the dense-codec
    accessor on both seams that construct a real ``BGEM3Codec``:

    - ``knowledge_store.KnowledgeStore._get_dense_codec``
    - ``context_manager.HelixContextManager._get_dense_codec``

    Both originally do ``from .backends.bgem3_codec import BGEM3Codec`` and
    ``BGEM3Codec(dim=...)`` — that construction (and its lazy ``_load()``,
    which pulls the ~2 GB weights) is the only place the real model enters
    a non-live test. The replacements keep each method's original
    None-check contract exactly — they only swap the *class that gets
    constructed* — so:

    - a test that pre-assigns ``g._dense_codec = ...`` still wins (the
      replacement returns the already-set codec untouched);
    - ``HelixContextManager._get_dense_codec`` still returns ``None`` when
      ``config.ingestion.dense_embed_on_ingest`` is false.

    ``live``-marked tests are skipped entirely by this fixture — they must
    still exercise the real BGE-M3 model.
    """
    if request.node.get_closest_marker("live") is not None:
        # Real-model integration tests: do not stub.
        return

    from cymatix_context import context_manager as _cm
    from cymatix_context import knowledge_store as _ks

    def _fake_store_codec(self):
        # Mirrors KnowledgeStore._get_dense_codec: lazy-build + cache,
        # but constructs the fake instead of the real BGEM3Codec.
        if self._dense_codec is None:
            self._dense_codec = FakeBGEM3Codec(dim=self._dense_embedding_dim)
        return self._dense_codec

    def _fake_manager_codec(self):
        # Mirrors HelixContextManager._get_dense_codec: the dense-on-ingest
        # gate still applies, only the constructed class changes.
        if not self.config.ingestion.dense_embed_on_ingest:
            return None
        if self._dense_codec is None:
            self._dense_codec = FakeBGEM3Codec(
                dim=self.config.retrieval.dense_embedding_dim
            )
        return self._dense_codec

    monkeypatch.setattr(
        _ks.KnowledgeStore, "_get_dense_codec", _fake_store_codec
    )
    monkeypatch.setattr(
        _cm.HelixContextManager, "_get_dense_codec", _fake_manager_codec
    )


@pytest.fixture
def poem_text():
    return (FIXTURES_DIR / "poem.txt").read_text(encoding="utf-8")


@pytest.fixture
def calculator_code():
    return (FIXTURES_DIR / "calculator.py").read_text(encoding="utf-8")


@pytest.fixture
def genome():
    """In-memory genome for fast, stateless tests.

    The density gate is disabled by default at the fixture level so that
    existing query-logic / retrieval / co-activation / HGT tests can
    insert hand-crafted test genes without fighting the ingest-time
    demotion heuristic. Tests that specifically want to exercise the
    gate should either use the ``gated_genome`` fixture below or call
    ``genome.upsert_gene(gene, apply_gate=True)`` explicitly.
    """
    g = Genome(
        path=":memory:",
        synonym_map={
            "slow": ["performance", "latency", "bottleneck"],
            "auth": ["jwt", "login", "security", "token"],
            "db": ["database", "sqlite", "sql", "query"],
        },
    )
    # Monkey-patch upsert_gene so the default is gate-off for tests.
    # Tests that want the gate on can still pass apply_gate=True.
    _original_upsert = g.upsert_gene
    def _ungated_upsert(gene, apply_gate=False):
        return _original_upsert(gene, apply_gate=apply_gate)
    g.upsert_doc = _ungated_upsert  # canonical name (R3 Stage C); legacy
    g.upsert_gene = _ungated_upsert  # alias path — keep both for safety

    yield g
    g.close()


@pytest.fixture
def gated_genome():
    """In-memory genome with the density gate enabled by default.

    Use this for tests that specifically verify gate behavior at the
    upsert boundary — the gate runs on every upsert_gene call unless
    the test passes apply_gate=False explicitly.
    """
    g = Genome(
        path=":memory:",
        synonym_map={
            "slow": ["performance", "latency"],
            "auth": ["jwt", "login", "security"],
        },
    )
    yield g
    g.close()


def make_gene(
    content: str = "test content",
    domains: list[str] | None = None,
    entities: list[str] | None = None,
    co_activated_with: list[str] | None = None,
    chromatin: ChromatinState = ChromatinState.OPEN,
    is_fragment: bool = False,
    gene_id: str | None = None,
) -> Gene:
    """Helper to build Gene objects for tests without needing the ribosome."""
    gid = gene_id or Genome.make_gene_id(content)
    return Gene(
        gene_id=gid,
        content=content,
        complement=f"Summary of: {content[:50]}",
        codons=["chunk_0", "chunk_1", "chunk_2"],
        promoter=PromoterTags(
            domains=domains or [],
            entities=entities or [],
            intent="test",
            summary=content[:80],
        ),
        epigenetics=EpigeneticMarkers(
            co_activated_with=co_activated_with or [],
        ),
        chromatin=chromatin,
        is_fragment=is_fragment,
    )


# ── Canonical mock compressor backend (shared test stand-in) ─────────
#
# Before the 2026-07-05 test-suite consolidation, ~10 test files each
# carried a private copy of a "mock ribosome backend" — a class with a
# single ``complete(prompt, system, temperature) -> str`` method that
# sniffs the system prompt and returns plausible JSON for whichever
# pipeline stage is calling (pack / re-rank / splice / replicate). The
# copies drifted (different promoter tags, some returned "{}" for
# everything, one returned pack JSON unconditionally). This is the one
# canonical definition; test files import it from here instead of each
# keeping a private variant.


class MockCompressorBackend:
    """Canonical mock compressor (ribosome) backend for tests.

    Derived from ``tests/test_server.py``'s ``ServerMockBackend`` (the
    system-prompt-sniffing variant), widened to the superset of the
    per-file copies it replaces:

    - ``"compression engine" in system``  → pack JSON: one codon
      (``test_codon``, weight 0.8, exon), complement
      ``"Compressed test content."``, promoter ``domains=["test"]``,
      ``entities=["TestEntity"]``.
    - ``"expression scorer" in system``   → re-rank JSON: scores every
      16-char gene_id found in the prompt (``0.9, 0.8, ...`` descending,
      prompt order). Degenerates to ``{}`` when the prompt contains no
      gene ids — i.e. exactly the old test_server/test_health behavior,
      which returned ``{}`` unconditionally.
    - ``"context splicer" in system``     → splice JSON: keeps codons
      ``[0, 1]`` for every ``Gene <id>`` found in the prompt (again
      ``{}`` when none — the old test_server behavior).
    - ``"replication engine" in system``  → exchange pack JSON.
    - anything else                       → ``"{}"``.

    ``response``: when given, ``complete()`` returns that exact string
    for every call, skipping all sniffing — this is the controllable
    canned-response contract of ``tests/test_ribosome.py``'s
    ``MockBackend``. NOTE the default differs: test_ribosome's local
    class defaulted to ``response="{}"``; a bare
    ``MockCompressorBackend()`` sniffs instead. Migrations of
    default-constructed test_ribosome usages must pass
    ``response="{}"`` explicitly.

    ``calls``: every ``complete()`` appends ``{"prompt": ..., "system":
    ...}`` (exactly those two keys, matching test_ribosome's call-log
    contract) so invocation-shape assertions keep working.

    Known divergent local variants (documented so migrators can decide
    revert-vs-adopt per Task 12's hard rule):

    - ``tests/test_health.py``: pack promoter used ``domains=["auth",
      "security"], entities=["jwt"]`` — tests relying on mock-derived
      auth tags need a canned ``response=`` or explicit seeded genes.
    - ``tests/test_pipeline.py`` / ``tests/test_gene_src_prefix.py``:
      pack returned TWO codons (``mock_concept``/``mock_detail``) and
      ``domains=["testing", "mock"]`` — codon-count-sensitive tests
      diverge from the canonical single-codon pack.
    - ``tests/test_swap_db.py``: returned pack JSON unconditionally for
      every call (no sniffing) — equivalent to passing the pack payload
      as ``response=``.
    - ``tests/test_session_delivery.py``: returned ``"{}"`` for every
      call — equivalent to ``MockCompressorBackend(response="{}")``.
    """

    def __init__(self, response: str | None = None):
        self.response = response
        self.calls: list[dict] = []

    def complete(self, prompt: str, system: str = "", temperature: float = 0.0) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        if self.response is not None:
            return self.response
        if "compression engine" in system:
            return json.dumps({
                "codons": [{"meaning": "test_codon", "weight": 0.8, "is_exon": True}],
                "complement": "Compressed test content.",
                "promoter": {
                    "domains": ["test"],
                    "entities": ["TestEntity"],
                    "intent": "test",
                    "summary": "Test content for server tests",
                },
            })
        if "expression scorer" in system:
            gene_ids = re.findall(r"(\w{16}):", prompt)
            return json.dumps(
                {gid: round(0.9 - i * 0.1, 1) for i, gid in enumerate(gene_ids)}
            )
        if "context splicer" in system:
            gene_ids = re.findall(r"Gene (\w+)", prompt)
            return json.dumps({gid: [0, 1] for gid in gene_ids})
        if "replication engine" in system:
            return json.dumps({
                "codons": [{"meaning": "exchange", "weight": 1.0, "is_exon": True}],
                "complement": "Test exchange.",
                "promoter": {"domains": ["test"], "entities": [], "intent": "test", "summary": "test"},
            })
        return "{}"


# ── Shared config / client / CLI helpers ─────────────────────────────
#
# The same four-section ``HelixConfig(...)`` literal (mock ribosome,
# small budget, in-memory genome, localhost upstream) was repeated in
# ~22 test files, and the same 5-line stdout/stderr-capturing CLI
# runner in all 12 ``tests/test_cli_*.py`` files. One definition each.


def make_helix_config(**overrides) -> HelixConfig:
    """Standard in-memory test config; ``**overrides`` replace whole sections.

    Returns the config shape shared by the server/pipeline test files::

        HelixConfig(
            ribosome=RibosomeConfig(model="mock", timeout=5),   # backend stays "none"
            budget=BudgetConfig(max_genes_per_turn=4),
            genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
            server=ServerConfig(upstream="http://localhost:11434"),
        )

    Each keyword must be a ``HelixConfig`` field name; the value replaces
    that section wholesale (no deep merge). Examples::

        make_helix_config(budget=BudgetConfig(max_genes_per_turn=4,
                                              session_delivery_enabled=True))
        make_helix_config(synonym_map={"auth": ["jwt", "login"]})
    """
    defaults: dict = {
        "ribosome": RibosomeConfig(model="mock", timeout=5),
        "budget": BudgetConfig(max_genes_per_turn=4),
        "genome": GenomeConfig(path=":memory:", cold_start_threshold=5),
        "server": ServerConfig(upstream="http://localhost:11434"),
    }
    defaults.update(overrides)
    return HelixConfig(**defaults)


def make_client(config=None, backend=None) -> "TestClient":
    """FastAPI ``TestClient`` over ``create_app`` with a mock compressor.

    Builds the app the way ``tests/test_server.py``'s ``client`` fixture
    does: ``create_app(config)``, then injects ``backend`` into
    ``app.state.helix.ribosome.backend`` so no Ollama/upstream is needed.

    - ``config``  defaults to ``make_helix_config()``.
    - ``backend`` defaults to a fresh ``MockCompressorBackend()``.

    Returns the ``TestClient`` un-entered — use it directly (test_server
    style) or as a context manager (``with make_client() as c:`` —
    test_registry style, which additionally runs app lifespan events).
    The underlying app is reachable as ``client.app`` for genome pokes.

    ``create_app``/``TestClient`` are imported lazily here so importing
    conftest stays cheap for the many tests that never touch the server.
    """
    from fastapi.testclient import TestClient
    from cymatix_context.server import create_app

    app = create_app(config if config is not None else make_helix_config())
    app.state.helix.ribosome.backend = (
        backend if backend is not None else MockCompressorBackend()
    )
    return TestClient(app)


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run the ``helix`` CLI in-process; return ``(exit_code, stdout, stderr)``.

    The exact helper duplicated as ``_run`` in all 12
    ``tests/test_cli_*.py`` files: captures stdout/stderr via
    ``io.StringIO`` + ``contextlib.redirect_stdout/stderr`` around
    ``cymatix_context.cli.main(argv)`` and returns the exit code plus both
    captured streams as strings.

    ``cymatix_context.cli`` is imported lazily (and ``main`` resolved at
    call time) so conftest import stays flat and per-test monkeypatching
    of CLI module attributes keeps working.
    """
    from cymatix_context import cli as _cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = _cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def reset_hardware_cache():
    """Reset the hardware-detection singleton before AND after a test.

    The body copy-pasted as an autouse ``_reset_hardware_cache`` fixture
    in tests/test_hardware.py, tests/test_deberta_backend.py and
    tests/test_nli_backend.py. Deliberately NOT autouse here — most of
    the suite never touches the hardware singleton. Files that need it
    on every test opt in with a one-line autouse wrapper::

        @pytest.fixture(autouse=True)
        def _reset(reset_hardware_cache):
            yield

    or request ``reset_hardware_cache`` directly per test.
    """
    from cymatix_context import hardware

    hardware.reset_for_test()
    yield
    hardware.reset_for_test()

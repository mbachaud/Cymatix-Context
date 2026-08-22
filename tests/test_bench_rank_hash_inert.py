"""C1 rank-hash instrumentation, both legs (issue #339 Phase 0.4).

House rule, quoted from ``tests/test_fusion_rrf_gate.py``: instrumentation
ships DEFAULT-INERT, and an inert-only suite passes on a dead gate. So every
``..._is_inert`` style case here is paired with a ``..._is_load_bearing``
style case.

* **Leg A**, gate unset, proves the shipped behaviour is untouched. The
  references are the goldens under ``tests/golden/``, generated on the
  UNPATCHED v0.9.0 tree (commit ``55162c6b4a84070a29b9c02237714b07c1193b38``)
  before the first source edit of this branch, with the gate scrubbed. See
  ``tests/golden/_capture_c1_rank_hash_goldens.py``.
* **Leg B**, gate set, proves the instrument actually emits, that the gated
  key is the recipe over the ADMITTED pool rather than the delivered one, and
  that stripping exactly the enumerated columns restores the leg A manifest.

What the goldens prove and what they do not: schema identity and
additive-only growth, not live-receipt byte identity. A live bench receipt
embeds measured latencies, so a whole-receipt byte golden would be a flake
generator. The delivery block IS compared byte for byte, because it is the
only block this change can touch and it is fully deterministic for the seeded
fixture (verified stable across PYTHONHASHSEED 0, 1 and 12345).

Every case runs laptop-local: no bed, no box, no encoder, no uvicorn, and no
network beyond a loopback stdlib stub.
"""

from __future__ import annotations

import ast
import json
import random
from pathlib import Path

import pytest

from cymatix_context.config import (
    BudgetConfig,
    CymatixConfig,
    GenomeConfig,
    RibosomeConfig,
)
from cymatix_context.context_manager import CymatixContextManager
from cymatix_context import context_manager as cm

from tests.conftest import (
    MockCompressorBackend,
    make_client,
    make_cymatix_config,
)
from tests.golden import _capture_c1_rank_hash_goldens as capture


GATE = "CYMATIX_BENCH_RANK_HASH"
RECIPE = "c1-v3"

GOLDEN_DIR = Path(__file__).parent / "golden"
CONTEXT_GOLDEN = json.loads(
    (GOLDEN_DIR / "golden_v090_context.json").read_text(encoding="utf-8"))
WORKER_GOLDEN = json.loads(
    (GOLDEN_DIR / "golden_v090_worker_samples.json").read_text(encoding="utf-8"))

CONTEXT_MANAGER_PATH = (
    Path(cm.__file__).resolve())
HARNESS_PATH = capture.HARNESS_PATH

#: The eleven gated per-sample columns, in the order _hash_columns emits them.
GATED_COLUMNS = (
    "candidate_set_sha256",
    "candidate_count",
    "delivered_count",
    "cap_binding",
    "rank_sha256",
    "rank_citations_sha256",
    "rank_topk_sha256",
    "citations_dropped",
    "citations_subsequence",
    "delivered_gold",
    "gold_rank_delivered",
)

SEEDED_IDS = [f"ckptgene{i:08d}" for i in range(capture.SEEDED_GENE_COUNT)]


@pytest.fixture(autouse=True)
def _scrub_gate(monkeypatch):
    """Copy of the env-scrubbing idiom at tests/test_encoder_daemon.py:53.

    An ambient CYMATIX_BENCH_RANK_HASH in the operator's shell must not be
    able to green a gate-off leg, so every case starts from a scrubbed
    environment and opts in explicitly.
    """
    monkeypatch.delenv(GATE, raising=False)


@pytest.fixture(scope="module")
def harness():
    return capture.load_harness()


def _make_manager() -> CymatixContextManager:
    """Same fixture shape as tests/test_packet_delivery_visibility.py."""
    cfg = CymatixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=12),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
    )
    mgr = CymatixContextManager(cfg)
    mgr.ribosome.backend = MockCompressorBackend()
    return mgr


def _delivery_from_manager() -> dict:
    mgr = _make_manager()
    try:
        capture._seed_topic_genes(mgr.genome, capture.SEEDED_GENE_COUNT)
        win = mgr.build_context(capture.FACTUAL_QUERY)
        delivery = (win.metadata or {}).get("delivery")
        assert delivery is not None
        return dict(delivery)
    finally:
        mgr.close()


def _context_response() -> dict:
    client = make_client(
        make_cymatix_config(budget=BudgetConfig(max_genes_per_turn=12)))
    capture._seed_topic_genes(
        client.app.state.cymatix.genome, capture.SEEDED_GENE_COUNT)
    resp = client.post("/context", json={"query": capture.FACTUAL_QUERY})
    assert resp.status_code == 200
    return resp.json()[0]


def _contains_key(node, needle: str) -> bool:
    """Recursive scan of a JSON tree for a key name anywhere."""
    if isinstance(node, dict):
        return any(k == needle or _contains_key(v, needle)
                   for k, v in node.items())
    if isinstance(node, list):
        return any(_contains_key(v, needle) for v in node)
    return False


def _samples(response, env=None, *, k=12, rounds=1, limit=2, gold=None,
             status=200, tmp_path=None):
    """Drive the UNMODIFIED worker_main against the stub and return samples."""
    with capture.serve_stub(response, status=status) as base_url:
        return capture.run_worker(
            tmp_path, base_url, env, k=k, rounds=rounds, limit=limit, gold=gold)


# ---------------------------------------------------------- gate helper ----


class TestGateHelper:

    def test_gate_defaults_off_and_is_read_at_call_time(self, monkeypatch,
                                                        harness):
        assert cm._env_truthy(cm.BENCH_RANK_HASH_ENV) is False
        assert harness._rank_hash_enabled() is False
        monkeypatch.setenv(GATE, "1")
        assert cm._env_truthy(cm.BENCH_RANK_HASH_ENV) is True, (
            "the knob must be read at CALL time")
        assert harness._rank_hash_enabled() is True
        monkeypatch.setenv(GATE, "off")
        assert cm._env_truthy(cm.BENCH_RANK_HASH_ENV) is False
        assert harness._rank_hash_enabled() is False

    def test_gate_falls_back_to_off_on_a_nonsense_value(self, monkeypatch,
                                                        harness):
        monkeypatch.setenv(GATE, "maybe")
        assert cm._env_truthy(cm.BENCH_RANK_HASH_ENV) is False
        assert harness._rank_hash_enabled() is False

    def test_harness_gate_reader_agrees_with_server_gate_reader(
            self, monkeypatch, harness):
        rows = [None, "", " ", "0", "1", "TRUE", "yes", "On", "off", "no",
                "maybe", "2"]
        for value in rows:
            if value is None:
                monkeypatch.delenv(GATE, raising=False)
            else:
                monkeypatch.setenv(GATE, value)
            server = cm._env_truthy(cm.BENCH_RANK_HASH_ENV)
            client = harness._rank_hash_enabled()
            assert server == client, (
                f"gate readers disagree on {value!r}: server={server} "
                f"harness={client}")


# --------------------------------------------------------- hash recipes ----


class TestHashRecipes:

    def test_set_recipe_matches_the_hand_computed_vector(self, harness):
        ids = ["gene_c", "gene_a", "gene_b", "gene_a"]
        expected = (
            "a994b0d84e1fedfadb41b0bf2b36e88feb35fd39b1338eed7c6bf09f172fd56e")
        assert cm._gene_set_sha256(ids) == expected
        assert harness._gene_set_sha256(ids) == expected

    def test_set_recipe_is_order_free_and_deduplicated(self, harness):
        base = ["gene_a", "gene_b", "gene_c"]
        digest = harness._gene_set_sha256(base)
        for shuffled in (["gene_c", "gene_b", "gene_a"],
                         ["gene_b", "gene_a", "gene_c", "gene_a", "gene_b"]):
            assert harness._gene_set_sha256(shuffled) == digest
            assert cm._gene_set_sha256(shuffled) == digest

    def test_order_recipe_matches_the_hand_computed_vector(self, harness):
        expected = (
            "41836ac951fd6dba6b4834e20cfd5b501affe169459c71d1678f4c7f4b9b2ade")
        assert harness._gene_order_sha256(
            ["gene_c", "gene_a", "gene_b"]) == expected
        assert harness._gene_order_sha256(
            ["gene_a", "gene_b", "gene_c"]) != expected
        assert harness._gene_order_sha256(
            ["gene_c", "gene_c", "gene_a", "gene_b"]) != expected

    def test_empty_and_single_id_digests_are_pinned(self, harness):
        empty = (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        single = (
            "9d8747433954f42bdc3c493fd9e66c2602cb0735e32501f7651b85c1969eb0d0")
        assert harness._gene_set_sha256([]) == empty
        assert harness._gene_order_sha256([]) == empty
        assert cm._gene_set_sha256([]) == empty
        assert harness._gene_set_sha256(["gene_a"]) == single
        assert harness._gene_order_sha256(["gene_a"]) == single
        assert cm._gene_set_sha256(["gene_a"]) == single

    def test_server_and_harness_set_recipes_agree_on_a_property_sample(
            self, harness):
        rng = random.Random(20260821)
        for _ in range(200):
            ids = [f"gene_{rng.randrange(0, 40):04d}"
                   for _ in range(rng.randrange(0, 25))]
            assert cm._gene_set_sha256(ids) == harness._gene_set_sha256(ids)


# ------------------------------------------- server capture, leg A (off) ----


class TestServerCaptureInert:

    def test_window_delivery_has_no_gated_key_when_gate_unset(self):
        delivery = _delivery_from_manager()
        assert set(delivery) == {
            "pool_size", "delivered_count", "delivered_gene_ids", "cap_binding"}

    def test_context_route_response_has_no_gated_key_anywhere_when_gate_unset(
            self):
        body = _context_response()
        assert not _contains_key(body, "candidate_set_sha256")
        assert "candidate_set_sha256" not in json.dumps(body)

    def test_context_route_response_matches_the_v090_golden_when_gate_unset(
            self):
        body = _context_response()
        assert capture.key_manifest(body) == CONTEXT_GOLDEN["key_manifest"]
        assert body["delivery"] == CONTEXT_GOLDEN["delivery"]


# -------------------------------------------- server capture, leg B (on) ----


class TestServerCaptureLoadBearing:

    def test_delivery_carries_candidate_set_sha256_when_gate_set(
            self, monkeypatch):
        monkeypatch.setenv(GATE, "1")
        delivery = _delivery_from_manager()
        digest = delivery.get("candidate_set_sha256")
        assert isinstance(digest, str)
        assert len(digest) == 64
        assert digest == digest.lower()
        assert all(c in "0123456789abcdef" for c in digest)

    def test_gated_key_equals_the_set_recipe_over_the_admitted_pool(
            self, monkeypatch):
        monkeypatch.setenv(GATE, "1")
        delivery = _delivery_from_manager()
        # The fixture seeds SEEDED_GENE_COUNT documents and every one of them
        # is admitted: pool_size says so. Recomputed here from the fixture, not
        # from the manager, so a capture site placed AFTER the assembly cap
        # (which would hash only the 5 delivered ids) fails right here.
        assert delivery["pool_size"] == capture.SEEDED_GENE_COUNT
        assert delivery["delivered_count"] < capture.SEEDED_GENE_COUNT
        assert delivery["candidate_set_sha256"] == cm._gene_set_sha256(
            SEEDED_IDS)
        assert delivery["candidate_set_sha256"] != cm._gene_set_sha256(
            delivery["delivered_gene_ids"])

    def test_gate_on_adds_exactly_one_key_and_preserves_the_other_four(
            self, monkeypatch):
        off = _delivery_from_manager()
        monkeypatch.setenv(GATE, "1")
        on = _delivery_from_manager()
        assert set(on) - set(off) == {"candidate_set_sha256"}
        assert set(off) - set(on) == set()
        for key in ("pool_size", "delivered_count", "delivered_gene_ids",
                    "cap_binding"):
            assert on[key] == off[key]

    def test_route_serves_the_gated_key_with_no_route_change(self, monkeypatch):
        monkeypatch.setenv(GATE, "1")
        body = _context_response()
        assert "candidate_set_sha256" in body["delivery"]
        assert body["delivery"]["candidate_set_sha256"] == cm._gene_set_sha256(
            SEEDED_IDS)
        # routes_context.py serves window.metadata["delivery"] verbatim, so the
        # gated key arrives with zero route edits: the manifest grows by
        # exactly one path and nothing else moves.
        grew = set(capture.key_manifest(body)) - set(
            CONTEXT_GOLDEN["key_manifest"])
        assert grew == {"delivery.candidate_set_sha256"}


# ------------------------------------------ worker samples, leg A (off) ----


class TestWorkerSamplesInert:

    def test_worker_samples_are_key_identical_to_the_v090_golden_when_gate_unset(
            self, tmp_path):
        samples = _samples(capture.CANNED_CONTEXT_RESPONSE, tmp_path=tmp_path)
        success = [s for s in samples if "latency_ms" in s]
        assert success
        keys = set()
        for sample in success:
            keys.update(sample)
        assert sorted(keys) == WORKER_GOLDEN["sample_keys"]
        first = {k: v for k, v in sorted(success[0].items())
                 if k != "latency_ms"}
        assert first == WORKER_GOLDEN["invariant_fields"]

    def test_existing_sample_fields_are_identical_across_both_legs(
            self, tmp_path):
        off = _samples(capture.CANNED_CONTEXT_RESPONSE,
                       tmp_path=tmp_path / "off")
        on = _samples(capture.CANNED_CONTEXT_RESPONSE, {GATE: "1"},
                      tmp_path=tmp_path / "on")
        assert len(off) == len(on)
        for a, b in zip(off, on):
            for key in a:
                if key == "latency_ms":
                    continue
                assert a[key] == b[key], f"field {key} moved between legs"

    def test_error_path_samples_are_untouched_in_both_legs(self, tmp_path):
        off = _samples(capture.CANNED_CONTEXT_RESPONSE, status=500,
                       tmp_path=tmp_path / "off")
        on = _samples(capture.CANNED_CONTEXT_RESPONSE, {GATE: "1"}, status=500,
                      tmp_path=tmp_path / "on")
        assert off and on
        assert all("error" in s for s in off)
        assert all("error" in s for s in on)
        for a, b in zip(off, on):
            assert set(a) == set(b) == {"needle", "round", "error", "status"}
            assert a["status"] == b["status"] == 500


# ------------------------------------------- worker samples, leg B (on) ----


class TestWorkerSamplesLoadBearing:

    def test_worker_samples_gain_exactly_the_eleven_gated_columns_when_gate_set(
            self, tmp_path):
        off = _samples(capture.CANNED_CONTEXT_RESPONSE,
                       tmp_path=tmp_path / "off")
        on = _samples(capture.CANNED_CONTEXT_RESPONSE, {GATE: "1"},
                      tmp_path=tmp_path / "on")
        assert set(on[0]) - set(off[0]) == set(GATED_COLUMNS)
        assert len(GATED_COLUMNS) == 11

    def test_stripping_the_gated_columns_restores_the_gate_off_manifest(
            self, tmp_path):
        on = _samples(capture.CANNED_CONTEXT_RESPONSE, {GATE: "1"},
                      tmp_path=tmp_path)
        for sample in on:
            stripped = {k: v for k, v in sample.items()
                        if k not in GATED_COLUMNS}
            assert sorted(stripped) == WORKER_GOLDEN["sample_keys"]

    def test_gated_columns_are_well_formed(self, tmp_path):
        on = _samples(capture.CANNED_CONTEXT_RESPONSE, {GATE: "1"},
                      tmp_path=tmp_path)
        for sample in on:
            for column in ("rank_sha256", "rank_citations_sha256",
                           "rank_topk_sha256"):
                digest = sample[column]
                assert isinstance(digest, str) and len(digest) == 64
                assert all(c in "0123456789abcdef" for c in digest)
            # The stub echoes no server-side gate, so the echoed column is
            # null: the harness never fabricates a hash the server did not send.
            assert sample["candidate_set_sha256"] is None
            assert isinstance(sample["candidate_count"], int)
            assert isinstance(sample["delivered_count"], int)
            assert sample["cap_binding"] in (
                "token_budget", "assembly_cap", "none")
            assert isinstance(sample["citations_dropped"], int)
            assert sample["citations_dropped"] >= 0
            assert isinstance(sample["citations_subsequence"], bool)
            assert isinstance(sample["delivered_gold"], bool)
            assert sample["gold_rank_delivered"] is None or isinstance(
                sample["gold_rank_delivered"], int)

    def test_rank_topk_hash_covers_exactly_the_k_truncated_scored_list(
            self, tmp_path, harness):
        cited = [c["gene_id"] for c in
                 capture.CANNED_CONTEXT_RESPONSE[0]["agent"]["citations"]]
        full = harness._gene_order_sha256(cited)
        for k in (1, 3, 5, 12):
            on = _samples(capture.CANNED_CONTEXT_RESPONSE, {GATE: "1"}, k=k,
                          tmp_path=tmp_path / f"k{k}")
            expected = harness._gene_order_sha256(cited[:k])
            assert on
            for sample in on:
                assert sample["rank_topk_sha256"] == expected
                assert sample["rank_citations_sha256"] == full
                if k < len(cited):
                    assert sample["rank_topk_sha256"] != full

    def test_rank_sha256_and_rank_citations_sha256_differ_on_a_lossy_response(
            self, tmp_path, harness):
        on = _samples(capture.CANNED_CONTEXT_RESPONSE_LOSSY, {GATE: "1"},
                      tmp_path=tmp_path)
        for sample in on:
            assert sample["rank_sha256"] != sample["rank_citations_sha256"]
            assert sample["rank_sha256"] == harness._gene_order_sha256(
                capture.DELIVERED_IDS)
            assert sample["citations_dropped"] == 1
            assert sample["citations_subsequence"] is True

    def test_subsequence_violation_is_flagged(self, tmp_path):
        on = _samples(capture.CANNED_CONTEXT_RESPONSE_REORDERED, {GATE: "1"},
                      tmp_path=tmp_path)
        for sample in on:
            # Nothing was dropped, so a drop count alone would call this clean.
            assert sample["citations_dropped"] == 0
            assert sample["citations_subsequence"] is False

    def test_delivered_gold_and_gold_rank_delivered_ride_the_delivered_basis(
            self, tmp_path):
        # The gold gene is the one the lossy response DROPPED from citations,
        # so the shipped citations-based ``rank`` cannot see it and only the
        # delivered basis can.
        gold = {"needle_a": [capture.DROPPED_ID],
                "needle_b": [capture.DROPPED_ID]}
        on = _samples(capture.CANNED_CONTEXT_RESPONSE_LOSSY, {GATE: "1"},
                      gold=gold, tmp_path=tmp_path)
        expected_rank = capture.DELIVERED_IDS.index(capture.DROPPED_ID) + 1
        for sample in on:
            assert sample["rank"] is None
            assert sample["delivered_gold"] is True
            assert sample["gold_rank_delivered"] == expected_rank

    def test_missing_delivery_block_degrades_to_nulls_and_never_raises(
            self, tmp_path, harness):
        on = _samples(capture.CANNED_CONTEXT_RESPONSE_NO_DELIVERY, {GATE: "1"},
                      tmp_path=tmp_path)
        cited = [c["gene_id"] for c in
                 capture.CANNED_CONTEXT_RESPONSE_NO_DELIVERY[0][
                     "agent"]["citations"]]
        assert on
        for sample in on:
            assert set(sample) - {"needle", "round", "latency_ms", "rank",
                                  "status"} == set(GATED_COLUMNS)
            for column in ("candidate_set_sha256", "candidate_count",
                           "delivered_count", "cap_binding", "rank_sha256",
                           "citations_dropped", "citations_subsequence",
                           "delivered_gold", "gold_rank_delivered"):
                assert sample[column] is None, f"{column} should degrade to null"
            assert sample["rank_citations_sha256"] == (
                harness._gene_order_sha256(cited))
            assert sample["rank_topk_sha256"] == harness._gene_order_sha256(
                cited)


# ------------------------------------------- level and payload, leg B ----


def _build_worker_set(concurrency: int) -> list:
    """Hand-built worker set: three needles with known distinct-hash counts."""
    workers = []
    for i in range(concurrency):
        workers.append({"samples": [
            {"needle": "needle_a", "round": 0, "latency_ms": 1.0, "rank": 1,
             "status": 200,
             "candidate_set_sha256": "A", "rank_sha256": "R1",
             "rank_topk_sha256": "T1", "citations_subsequence": True},
            {"needle": "needle_b", "round": 0, "latency_ms": 1.0, "rank": 1,
             "status": 200,
             "candidate_set_sha256": "B", "rank_sha256": f"R{i % 2}",
             "rank_topk_sha256": "T2", "citations_subsequence": i != 0},
            {"needle": "needle_c", "round": 0, "latency_ms": 1.0, "rank": None,
             "status": 200,
             "candidate_set_sha256": None, "rank_sha256": None,
             "rank_topk_sha256": "T3", "citations_subsequence": None},
            # An error-path sample carries no gated columns and must be
            # ignored by the aggregate rather than counted as a needle.
            {"needle": "needle_a", "round": 0, "error": "boom", "status": None},
        ]})
    return workers


class TestLevelAndPayloadBlocks:

    @pytest.mark.parametrize("concurrency", [1, 4, 8, 16])
    def test_hash_summary_counts_distinct_hashes_per_needle(
            self, harness, concurrency):
        workers = _build_worker_set(concurrency)
        summary = harness._hash_summary(workers)
        assert summary["recipe_version"] == RECIPE
        assert summary["samples_with_delivery"] == 2 * concurrency
        assert summary["samples_missing_delivery"] == concurrency
        assert summary["subsequence_violations"] == 1
        per = summary["per_needle"]
        assert list(per) == sorted(per), "per_needle keys must be sorted"
        assert list(per) == ["needle_a", "needle_b", "needle_c"]
        assert per["needle_a"] == {
            "n": concurrency, "distinct_candidate_set": 1, "distinct_rank": 1,
            "distinct_rank_topk": 1, "missing_delivery": 0}
        assert per["needle_b"] == {
            "n": concurrency, "distinct_candidate_set": 1,
            "distinct_rank": len({f"R{i % 2}" for i in range(concurrency)}),
            "distinct_rank_topk": 1, "missing_delivery": 0}
        assert per["needle_c"] == {
            "n": concurrency, "distinct_candidate_set": 0, "distinct_rank": 0,
            "distinct_rank_topk": 1, "missing_delivery": concurrency}

    def test_hash_gate_block_names_the_env_the_value_and_the_recipe_version(
            self, harness):
        block = _extract_subscript_dict(HARNESS_PATH, "payload", "hash_gate",
                                        harness)
        assert block == {"env": "CYMATIX_BENCH_RANK_HASH", "value": "1",
                         "recipe_version": RECIPE}


def _extract_subscript_dict(path: Path, container: str, key: str, namespace):
    """Read the literal dict a module assigns to ``container[key]``.

    Names inside the literal resolve against the loaded module, so this tests
    the block the harness actually writes rather than a restatement of it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == container
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == key):
            continue
        assert isinstance(node.value, ast.Dict)
        out = {}
        for k, v in zip(node.value.keys, node.value.values):
            assert isinstance(k, ast.Constant)
            if isinstance(v, ast.Constant):
                out[k.value] = v.value
            elif isinstance(v, ast.Name):
                out[k.value] = getattr(namespace, v.id)
            else:
                raise AssertionError(
                    f"{container}[{key!r}] holds a non-literal value")
        return out
    raise AssertionError(f"no {container}[{key!r}] assignment found in {path}")


# ------------------------------------------------ AST guard placement ----


def _ids_inside(node) -> set:
    return {id(child) for child in ast.walk(node)}


def _ids_in_functions(tree) -> set:
    inside = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inside |= _ids_inside(node)
    return inside


class TestGuardPlacement:
    """The inertness proof for the sites that cannot be executed laptop-side.

    The level and payload blocks only fire inside a real sweep, so their
    inertness is proven structurally: parse both modified files and show that
    every gated write is lexically under the gate guard, and that no gated
    write sits at module scope.
    """

    def test_every_gated_write_sits_under_the_gate_guard(self, harness):
        # --- server side -------------------------------------------------
        server_tree = ast.parse(
            CONTEXT_MANAGER_PATH.read_text(encoding="utf-8"))
        guarded = set()
        for node in ast.walk(server_tree):
            if not isinstance(node, ast.If):
                continue
            names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
            if names & {"BENCH_RANK_HASH_ENV", "_candidate_set_sha256"}:
                guarded |= _ids_inside(node)
        literals = [n for n in ast.walk(server_tree)
                    if isinstance(n, ast.Constant)
                    and n.value == "candidate_set_sha256"]
        assert literals, "the gated key literal vanished from context_manager"
        for node in literals:
            assert id(node) in guarded, (
                "a 'candidate_set_sha256' literal sits outside the gate guard")
        server_in_functions = _ids_in_functions(server_tree)
        for node in literals:
            assert id(node) in server_in_functions, (
                "gated key literal at module scope in context_manager")

        # --- harness side ------------------------------------------------
        harness_tree = ast.parse(HARNESS_PATH.read_text(encoding="utf-8"))
        gated_guarded = set()
        for node in ast.walk(harness_tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (isinstance(test, ast.Call) and isinstance(test.func, ast.Name)
                    and test.func.id == "_rank_hash_enabled"):
                gated_guarded |= _ids_inside(node)

        gated_calls = [
            n for n in ast.walk(harness_tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in ("_hash_columns", "_hash_summary")]
        assert len(gated_calls) == 2, (
            f"expected exactly two gated helper calls, found {len(gated_calls)}")
        for node in gated_calls:
            assert id(node) in gated_guarded, (
                "a gated helper call sits outside if _rank_hash_enabled()")

        gated_writes = []
        for node in ast.walk(harness_tree):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            target = node.targets[0]
            if (isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in ("hash_summary", "hash_gate")):
                gated_writes.append(node)
        assert len(gated_writes) == 2, (
            f"expected exactly two gated receipt writes, found "
            f"{len(gated_writes)}")
        for node in gated_writes:
            assert id(node) in gated_guarded, (
                "a gated receipt write sits outside if _rank_hash_enabled()")

        harness_in_functions = _ids_in_functions(harness_tree)
        for node in gated_calls + gated_writes:
            assert id(node) in harness_in_functions, (
                "gated write at module scope in run_server_scaling")

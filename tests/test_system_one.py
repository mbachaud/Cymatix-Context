"""Optional System One packet judge (EXPERIMENTAL, default off).

A "System One" model (TypeSafe's Jev is the reference provider) answers typed
questions with calibrated probabilities instead of generated text. When
``[system_one] enabled = true``, /context/packet gains an additive
``system_one`` block: the probability that the packet states the answer, and
optionally a per-item answers probability used to reorder items inside each
evidence bucket. Receipts: docs/benchmarks/2026-10-23-jev-system-one-packet-judge.md.

No network: every test stubs the HTTP layer or the client.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error

import pytest

from cymatix_context.backends import system_one_backend as so
from cymatix_context.config import CymatixConfig, SystemOneConfig, load_config

from tests.conftest import make_client, make_cymatix_config, make_gene


# ── config ────────────────────────────────────────────────────────────


def test_defaults_are_off_and_key_comes_from_env():
    cfg = CymatixConfig()
    assert cfg.system_one.enabled is False
    assert cfg.system_one.rerank_items is False
    assert cfg.system_one.api_key_env == "TYPESAFE_API_KEY"
    assert cfg.system_one.timeout_s > 0
    assert cfg.system_one.min_interval_s >= 0.3


def test_load_config_parses_section_and_warns_on_unknown(tmp_path, caplog):
    p = tmp_path / "c.toml"
    p.write_text(
        "[system_one]\n"
        "enabled = true\n"
        'model = "jev-1.14.0"\n'
        "rerank_items = true\n"
        "know_threshold = 0.7\n"
        "bogus = 1\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        cfg = load_config(str(p))
    assert cfg.system_one.enabled is True
    assert cfg.system_one.model == "jev-1.14.0"
    assert cfg.system_one.rerank_items is True
    assert cfg.system_one.know_threshold == 0.7
    assert "Unknown keys in [system_one]" in caplog.text
    assert "Unknown top-level" not in caplog.text


def test_config_rejects_out_of_range_threshold():
    with pytest.raises(ValueError):
        SystemOneConfig(know_threshold=1.5)


# ── HTTP client ───────────────────────────────────────────────────────


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok(body: dict) -> _Resp:
    return _Resp(json.dumps(body).encode("utf-8"))


def test_client_posts_model_state_questions_with_bearer_and_timeout(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _ok({"model": "jev-1.14.0", "answers": {"q": {"noul": 0.9}}})

    monkeypatch.setattr(so.urllib.request, "urlopen", fake_urlopen)
    c = so.SystemOneClient("https://example.test/v1/systemone", "jev-latest", "k3y",
                           timeout_s=12.0, min_interval_s=0.0)
    out = c.ask({"question": "x"}, {"q": {"type": "noul"}})
    assert out["answers"]["q"]["noul"] == 0.9
    assert seen["url"] == "https://example.test/v1/systemone"
    assert seen["auth"] == "Bearer k3y"
    assert seen["body"] == {"model": "jev-latest", "state": {"question": "x"},
                            "questions": {"q": {"type": "noul"}}}
    assert seen["timeout"] == 12.0


def test_client_retries_429_then_succeeds(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "busy", {}, io.BytesIO(b"slow down"))
        return _ok({"model": "m", "answers": {}})

    monkeypatch.setattr(so.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(so.time, "sleep", lambda s: None)
    c = so.SystemOneClient("https://e.test", "m", "k", min_interval_s=0.0)
    assert c.ask({}, {})["model"] == "m"
    assert len(calls) == 2


def test_client_raises_on_client_error_without_retry(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b"bad key"))

    monkeypatch.setattr(so.urllib.request, "urlopen", fake_urlopen)
    c = so.SystemOneClient("https://e.test", "m", "k", min_interval_s=0.0)
    with pytest.raises(so.SystemOneError, match="401"):
        c.ask({}, {})
    assert len(calls) == 1


def test_client_from_config_requires_key_env(monkeypatch):
    monkeypatch.delenv("SO_TEST_KEY", raising=False)
    with pytest.raises(so.SystemOneError, match="SO_TEST_KEY"):
        so.client_from_config(SystemOneConfig(api_key_env="SO_TEST_KEY"))
    monkeypatch.setenv("SO_TEST_KEY", "abc")
    assert isinstance(so.client_from_config(SystemOneConfig(api_key_env="SO_TEST_KEY")),
                      so.SystemOneClient)


# ── packet judge + rerank ─────────────────────────────────────────────


class _FakeClient:
    """Answers by question name; per-item answers keyed by a word in the text."""

    def __init__(self, packet_p=0.8, item_p=None, qtype="fact_lookup"):
        self.packet_p, self.item_p, self.qtype = packet_p, item_p or {}, qtype
        self.states = []

    def ask(self, state, questions):
        self.states.append((state, questions))
        if "qtype" in questions:
            return {"model": "fake-1", "answers": {"qtype": {"choice": self.qtype}}}
        if "packet_answers" in questions:
            return {"model": "fake-1", "answers": {"packet_answers": {"noul": self.packet_p}}}
        text = state["chunk"]["text"]
        p = next((v for k, v in self.item_p.items() if k in text), 0.0)
        return {"model": "fake-1", "answers": {"answers": {"noul": p}}}


def _item(gid, text, src="docs/a.md"):
    return {"gene_id": gid, "content": text, "source_id": src, "title": gid}


def test_qualify_packet_judge_only_sends_no_cymatix_rankings():
    buckets = {"verified": [_item("g1", "alpha"), _item("g2", "beta")], "stale_risk": []}
    fake = _FakeClient(packet_p=0.83)
    block = so.qualify_packet(fake, "what is x", buckets, know_threshold=0.5,
                              rerank=False, max_item_chars=1000)
    assert block["available"] is True
    assert block["model"] == "fake-1"
    assert block["question_type"] == "fact_lookup"
    assert block["answers_p"] == 0.83
    assert block["verdict"] == "know"
    assert block["reranked"] is False
    assert block["calls"] == 2
    packet_state = fake.states[1][0]
    assert set(packet_state) == {"question", "question_type", "packet"}
    assert packet_state["packet"] == [{"source": "docs/a.md", "text": "alpha"},
                                      {"source": "docs/a.md", "text": "beta"}]
    assert [i["gene_id"] for i in buckets["verified"]] == ["g1", "g2"]


def test_qualify_packet_miss_below_threshold_and_truncates_items():
    buckets = {"verified": [_item("g1", "x" * 50)], "stale_risk": []}
    fake = _FakeClient(packet_p=0.2)
    block = so.qualify_packet(fake, "q", buckets, know_threshold=0.5,
                              rerank=False, max_item_chars=10)
    assert block["verdict"] == "miss"
    assert fake.states[1][0]["packet"][0]["text"] == "x" * 10


def test_rerank_reorders_within_each_bucket_and_never_drops():
    buckets = {
        "verified": [_item("v1", "low"), _item("v2", "high"), _item("v3", "mid")],
        "stale_risk": [_item("s1", "mid"), _item("s2", "high")],
    }
    fake = _FakeClient(item_p={"high": 0.9, "mid": 0.5, "low": 0.1})
    block = so.qualify_packet(fake, "q", buckets, know_threshold=0.5,
                              rerank=True, max_item_chars=1000)
    assert [i["gene_id"] for i in buckets["verified"]] == ["v2", "v3", "v1"]
    assert [i["gene_id"] for i in buckets["stale_risk"]] == ["s2", "s1"]
    assert block["reranked"] is True
    assert block["calls"] == 2 + 5
    assert {(r["gene_id"], r["bucket"], r["answers_p"]) for r in block["items"]} == {
        ("v1", "verified", 0.1), ("v2", "verified", 0.9), ("v3", "verified", 0.5),
        ("s1", "stale_risk", 0.5), ("s2", "stale_risk", 0.9)}


def test_rerank_ties_keep_cymatix_order():
    buckets = {"verified": [_item("a", "same"), _item("b", "same"), _item("c", "same")]}
    fake = _FakeClient(item_p={"same": 0.5})
    so.qualify_packet(fake, "q", buckets, know_threshold=0.5, rerank=True, max_item_chars=100)
    assert [i["gene_id"] for i in buckets["verified"]] == ["a", "b", "c"]


def test_empty_packet_skips_the_model():
    fake = _FakeClient()
    block = so.qualify_packet(fake, "q", {"verified": [], "stale_risk": []},
                              know_threshold=0.5, rerank=True, max_item_chars=100)
    assert block == {"available": True, "model": None, "question_type": None,
                     "answers_p": 0.0, "verdict": "miss", "threshold": 0.5,
                     "reranked": False, "calls": 0}
    assert fake.states == []


# ── /context/packet route ─────────────────────────────────────────────


def _seed(genome):
    for i, word in enumerate(("low", "high", "mid")):
        genome.upsert_gene(make_gene(
            f"The checkpoint interval value is documented here, note {word}.",
            domains=["checkpoint", "interval"], entities=["value"],
            gene_id=f"sogene{i:010d}"))


def _post(client):
    resp = client.post("/context/packet", json={"query": "checkpoint interval value"})
    assert resp.status_code == 200
    return resp.json()


def test_route_default_off_has_no_block():
    client = make_client(make_cymatix_config())
    _seed(client.app.state.cymatix.genome)
    assert "system_one" not in _post(client)


def test_route_enabled_adds_block_and_rerank_reorders(monkeypatch):
    fake = _FakeClient(packet_p=0.91, item_p={"high": 0.9, "mid": 0.5, "low": 0.1})
    monkeypatch.setattr(so, "client_from_config", lambda cfg: fake)
    client = make_client(make_cymatix_config(
        system_one=SystemOneConfig(enabled=True, rerank_items=True)))
    _seed(client.app.state.cymatix.genome)
    data = _post(client)
    blk = data["system_one"]
    assert blk["available"] is True and blk["answers_p"] == 0.91 and blk["verdict"] == "know"
    assert blk["reranked"] is True and blk["calls"] == 2 + 3
    items = data["verified"] + data["stale_risk"]
    assert len(items) == 3
    order = {it["gene_id"]: n for n, it in enumerate(data["verified"])}
    probs = {r["gene_id"]: r["answers_p"] for r in blk["items"]}
    ranked = sorted(order, key=lambda g: order[g])
    assert [probs[g] for g in ranked] == sorted((probs[g] for g in ranked), reverse=True)
    # the know/miss contract is untouched
    assert ("know" in data) != ("miss" in data)


def test_route_soft_fails_when_provider_errors(monkeypatch):
    def boom(cfg):
        raise so.SystemOneError("TYPESAFE_API_KEY is not set")

    monkeypatch.setattr(so, "client_from_config", boom)
    client = make_client(make_cymatix_config(system_one=SystemOneConfig(enabled=True)))
    _seed(client.app.state.cymatix.genome)
    data = _post(client)
    assert data["system_one"] == {"available": False, "error": "TYPESAFE_API_KEY is not set"}
    assert len(data["verified"]) + len(data["stale_risk"]) == 3

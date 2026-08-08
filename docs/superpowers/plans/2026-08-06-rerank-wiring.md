# Rerank Production Wiring (#341) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the cross-encoder rerank into production at the real pre-cap seam (fused/union top-50), decoupled from the splice backend, daemon-served, with delivered-gold receipts.

**Architecture:** A new torch-lazy `backends/rerank_backend.py` exposes `score_pairs(query, texts) -> scores` with a remote-daemon branch (SPLADE pattern). `KnowledgeStore` applies it at two seams: post-union/pre-`apply_ann_gate` in `query_docs_ann` (default path) and post-fusion/pre-limit in `query_docs` (dense-off path), preserving the gate's candidate COUNT exactly (splice-cliff invariant) while letting the cross-encoder decide membership+order of the head. The store publishes `last_ranked_ids` so the ladder can measure final-order recall; delivered-gold lands in the ladder first.

**Tech Stack:** Python 3.14 (main `.venv` = CPU torch; CUDA venv `f:/tmp/venv-cuda`), transformers `cross-encoder/ms-marco-MiniLM-L-6-v2`, FastAPI daemon (port 11439), SQLite beds.

## Global Constraints

- **Kickoff spec:** `docs/design/2026-08-06-rerank-wiring-kickoff.md` (committed at 2060f55). Evidence: `docs/benchmarks/2026-08-06-retrieval-layer-ledger.md`, `docs/benchmarks/2026-08-04-rerank-and-fork1-inputs.md` §1.
- **TDD; suite green** — baseline on this branch: `3837 passed, 46 skipped, 25 deselected` via `python -m pytest tests/ -m "not live" -q` (~8.5 min). Run the touched test file per task; full suite at Tasks 7, 11, and before the PR.
- **Default-off / byte-identical:** every new knob defaults inert. `tests/test_fusion_invariance.py`, `tests/test_retrieval_invariance.py`, `tests/test_config_default_honesty.py`, `tests/test_config_reference_sync.py` must stay green — the last two force `cymatix.toml` + config-reference-doc updates for any key add/remove.
- **Receipts bars (ratio gates only):** (a) 829k on/off ≥ **+5pp** final-order recall@12 at ≤ **+5% p50**; (b) 100k direction+cost, no recall bar; (c) dogfood cost documented honestly, default stays OFF; (d) daemon capacity incl. rerank family (CPU bar 3.5 bundles/s, GPU 30 preserved — rerank gets its OWN key). ON arms require daemon-access-log proof of `POST /encode/rerank` traffic.
- **Splice-cliff invariant:** per-doc splice target is `25200 // n_candidates` (floor 1000); rerank must NOT change `n_candidates` entering splice. Verified via a new ring field, not assumed.
- **Traps (each cost the last campaign a debugging pass):** (1) `python script.py` puts the script dir at `sys.path[0]` → editable-install master shadows the worktree; use `python -m` / heredoc from worktree cwd, pin `PYTHONPATH` for out-of-tree clients. (2) Kill bench processes by port owner (`Get-NetTCPConnection`), never a bare `uvicorn` regex (matches the LIVE tray-supervised backend on 11437). (3) Never swap torch in main `.venv` (DLLs locked by live MCP); GPU runs use `f:/tmp/venv-cuda/Scripts/python.exe`; headroom presence auto-enables splice — verify optional-dep parity between arms. (4) Raw `KnowledgeStore()` ctor defaults disable dense/splade vs config — construct via `CymatixContextManager(load_config())` in benches. (5) On writable beds the shipped config re-creates empty splade tables — serve `erb_829k_nosplade.db` with a cell config `[ingestion] splade_enabled=false` (shipped `splade_auto_enable_below_genes=0` keeps DDL off). (6) Daemon identity = spawn token echoed by `/ready` (`CYMATIX_ENCODER_SPAWN_TOKEN`), not a bare 200 — benches use `wait_ready` from `encoder_daemon_capacity.py`, never `EncoderClient.is_ready`.
- **Beds:** `f:/tmp/erb_carve/erb_829k_nosplade.db` (34.4GB), `f:/tmp/erb_carve/erb_100k.db` (6.4GB, sema-backfilled); needles+gold `benchmarks/dogfood/erb/needles_resolved_{blob,carve}.json`, `gold_by_needle_{blob,carve}.json`.
- Windows: `subprocess.Popen` needs `creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)`; every HTTP call has explicit `timeout=`; `except Exception:` + `log.warning` minimum, never bare/silent.
- Commit per task; conventional-commit messages as given.

## Design decisions locked here (rationale in kickoff + seam maps)

1. **Two seams, one knob.** Default path: `query_docs_ann` post-union/pre-gate (probe measured exactly this: "fused top-50 from query_docs_ann"). Dense-off path: `query_docs` post-`combine_rerank`/pre-limit. The inner `query_docs` call made by `query_docs_ann` must NOT rerank — naturally suppressed because rerank in `query_docs` fires only when its new `query_text` kwarg is non-None, and `query_docs_ann` doesn't pass it.
2. **Count-preserving:** ON path computes the gate's kept-count `n_keep` from the sim-ordered pool exactly as OFF would, then emits the top-`n_keep` of the cross-encoder ordering. Candidate count into splice is byte-identical by construction. The scoring candidate set is the union head (top `rerank_depth` by sim) **plus any gate-kept ids beyond it** — incl. #214 floor-rescued dense ids — so the cross-encoder judges rescued docs instead of silently evicting them (an eviction would confound "rerank" with "floor-undo" in the A/B). The store publishes per-query `last_rerank_diag = {"gate_kept": int, "floor_appended": int}` so the verdict can slice floor-fired needles.
3. **Pair text:** `(query, doc_text)` where `doc_text = (gene.content or gene.promoter.summary or "")` — tokenizer truncates at `max_length=256` (same truncation `DeBERTaRibosome.re_rank` uses; probe cost figures match). The probe script was never committed, so this is an explicit assumption; the A/B bars are the arbiter. If 829k lands under bar, the one sanctioned variation is `f"{summary} [{domains}]"` pair text (code constant, not config).
4. **Metric basis:** store publishes `last_ranked_ids` (full pool, final order). Ladder gains `final_rank_of_first_gold` / `final_recall_hit` per needle + arm-level `final_recall_at_k`. Legacy score-map `recall_at_k` stays for continuity. The +5pp bar applies to `final_recall_at_k` (the probe's own definition). Both arms of an A/B use the same basis, so pairing is honest.
5. **Blend path deleted, not kept:** `blend.py:243-256`'s rerank branch is dead under deberta — the only backend the knob was designed for (gates on `hasattr(ribosome, "rerank")` but `DeBERTaRibosome` only defines `re_rank`) — and live-but-misrouted to the LLM `Compressor.rerank` under ollama/litellm. Removed as superseded by the `[retrieval]` pre-cap rerank; the removal notice must state both halves honestly. Delete branch + `[ingestion] rerank_enabled` key. `[ingestion] rerank_model` stays (legacy deberta ctor input); the NEW model key is `[retrieval] rerank_model` (daemon + client + in-process all read it).
6. **Daemon family split:** `BUNDLE_FAMILIES = ("dense","splade","sema")` keeps bundle semantics and the 3.5/30 capacity bars; `FAMILIES = BUNDLE_FAMILIES + ("rerank",)` drives registry load/devices/batches/capacity rows. `/encode/bundle` unchanged. Rerank capacity gets its own receipt key `rerank_pairs_per_s`.
7. **Order-reversed A/B mechanism:** the ladder hard-codes baseline-first, so reversal = two runs with inverted base configs: run1 base OFF + arm `rerank_on`; run2 base ON (cell TOML) + arm `rerank_off`. `arm_is_identity` auto-drops the arm matching base config.

## File Structure

| File | Change |
|---|---|
| `benchmarks/dogfood/erb/ablation_ladder.py` | delivered-gold per-needle + `final_*` metrics + `splice_n_candidates` capture + `rerank_on`/`rerank_off` arms |
| `cymatix_context/context_manager.py` | splice-ring `extra` (n_candidates); per-class rerank resolve + threading; drop blend-rerank plumbing |
| `cymatix_context/backends/rerank_backend.py` | **new** — `score_pairs` + `_remote_scores` + lazy singleton |
| `cymatix_context/config.py` | `[retrieval] rerank_enabled/rerank_depth/rerank_model/rerank_enabled_by_class`; remove `[ingestion] rerank_enabled` |
| `cymatix.toml` + `docs/reference/configuration.md` (or wherever `test_config_reference_sync` points) | key sync |
| `cymatix_context/knowledge_store.py` | ctor knobs; `query_docs(query_text=…)` seam; `query_docs_ann` pre-gate seam; `last_ranked_ids`; `xenc_rerank` signal |
| `cymatix_context/retrieval/rerank_combinators.py` | `resolve_class_flag` (per-class bool map resolver) |
| `cymatix_context/scoring/blend.py`, `server/routes_context.py`, `server/routes_admin.py` | delete dead rerank branch + `allow_rerank` plumbing |
| `cymatix_context/encoder_daemon.py` | family split, registry rerank, `/encode/rerank`, batcher, cache key, capacity |
| `cymatix_context/backends/encoder_client.py` | `EncoderClient.encode_rerank` |
| `benchmarks/dogfood/erb/encoder_daemon_capacity.py` + `benchmarks/dogfood/check_encoder_receipts.py` | rerank load level + own receipt key + gate |
| `benchmarks/dogfood/erb/rerank_ab.py` | **new** — A/B runner: daemon lifecycle + two ladder runs + POST-count proof |
| Tests | `tests/test_rerank_backend.py` (new), `tests/test_rerank_precap.py` (new), `tests/test_ablation_ladder_metrics.py` (new); updates to `test_layer_devices.py`, `test_encoder_daemon.py`, `test_encoder_client.py`, `test_remote_codecs.py`, `test_blend_mode.py`, config tests |

**Note on line numbers:** all `file:line` anchors below are as of 2060f55; locate by the quoted pattern, not the number — earlier tasks shift later anchors.

---

### Task 1: Delivered-gold in the ladder (#335)

**Files:**
- Modify: `benchmarks/dogfood/erb/ablation_ladder.py` (`per_query_record` ~:547-586, `run_arm` delivered computation ~:807-809, arm row ~:833-861)
- Test: `tests/test_ablation_ladder_metrics.py` (new)

**Interfaces:**
- Produces: `per_query_record(...)` gains fields `delivered_gold: 0|1`, `delivered_count: int`, `delivered_gold_rank: Optional[int]` (1-based position of first gold in `expressed_gene_ids`, `None` if absent). Arm row gains `delivered_gold_rate: float` and `pool_delivery_gap: float` (= `recall_at_k - delivered_gold_rate`). Existing `delivered_gold_queries` unchanged.
- The ladder is a script, not a package module. A loader helper already exists at `tests/test_ablation_tools.py:24-42` — copy it (or add the new tests to that file; either is fine, but keep the new metric tests grouped).

- [ ] **Step 1: Write failing tests** — extract the delivered-gold math into a pure helper so it's testable without a bed:

```python
# tests/test_ablation_ladder_metrics.py
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _load_ladder():
    spec = importlib.util.spec_from_file_location(
        "ablation_ladder", ROOT / "benchmarks" / "dogfood" / "erb" / "ablation_ladder.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def test_delivered_gold_fields_hit():
    lad = _load_ladder()
    out = lad.delivered_gold_fields(["a", "b", "gold1", "c"], {"gold1", "gold9"})
    assert out == {"delivered_gold": 1, "delivered_count": 4, "delivered_gold_rank": 3}

def test_delivered_gold_fields_miss_and_empty():
    lad = _load_ladder()
    assert lad.delivered_gold_fields(["a", "b"], {"g"}) == {
        "delivered_gold": 0, "delivered_count": 2, "delivered_gold_rank": None}
    assert lad.delivered_gold_fields([], {"g"})["delivered_count"] == 0

def test_arm_level_delivered_gold_rate():
    lad = _load_ladder()
    recs = [{"delivered_gold": 1}, {"delivered_gold": 0}, {"delivered_gold": 1}]
    assert lad.delivered_gold_rate(recs) == 2 / 3
    assert lad.delivered_gold_rate([]) == 0.0
```

- [ ] **Step 2:** `python -m pytest tests/test_ablation_ladder_metrics.py -q` → FAIL (`AttributeError: delivered_gold_fields`).
- [ ] **Step 3: Implement.** In `ablation_ladder.py`, next to `recall_at_k` (~:411):

```python
def delivered_gold_fields(expressed_gene_ids, gold: set) -> dict:
    """#335: delivery-basis gold metrics. 'Delivered' = expressed_gene_ids,
    the post-budget-trim slice (context_manager.expressed_gene_ids) — a gold
    at pool rank 3 can still fail delivery; rank-based recall cannot see it."""
    rank = None
    for i, gid in enumerate(expressed_gene_ids, start=1):
        if gid in gold:
            rank = i
            break
    return {"delivered_gold": 1 if rank else 0,
            "delivered_count": len(expressed_gene_ids),
            "delivered_gold_rank": rank}

def delivered_gold_rate(per_query: list) -> float:
    if not per_query:
        return 0.0
    return sum(r.get("delivered_gold", 0) for r in per_query) / len(per_query)
```

Wire in: `run_arm` already computes `delivered = set(window.expressed_gene_ids)` (~:807) — keep the ordered list (`window.expressed_gene_ids`, don't set-ify before the helper), merge `delivered_gold_fields(...)` into each `per_query_record(...)` dict, and add to the arm row (~:845): `"delivered_gold_rate": delivered_gold_rate(per_query_records)` and `"pool_delivery_gap": round(recall_val - delivered_gold_rate(per_query_records), 4)`. The per-needle fields are emitted even without `--per-query`? **No** — per-needle records only exist under `--per-query` (unchanged); the arm-level rate is computed from a running counter when `--per-query` is off (reuse the existing `delivered_gold_queries` counter: `rate = delivered_gold_queries / n_queries`).
- [ ] **Step 4:** `python -m pytest tests/test_ablation_ladder_metrics.py -q` → PASS.
- [ ] **Step 5: Commit** — `feat(bench): #335 delivered-gold per-needle + rate in the ablation ladder`

### Task 2: Splice candidate-count on the pipeline ring + ladder capture

**Files:**
- Modify: `cymatix_context/context_manager.py` (splice stage timer ~:2233)
- Modify: `benchmarks/dogfood/erb/ablation_ladder.py` (per-needle capture)
- Test: extend `tests/test_ablation_ladder_metrics.py` + `tests/test_perf_instrumentation.py` (the pipeline-ring tests live there — mirror its ring-reading idiom). There is no `seeded_manager` fixture: build one in the test — tmp genome path, `cfg = load_config(None)`; `cfg.genome.path = str(tmp_path / "g.db")`; `cfg.ribosome.backend = "none"`; `mgr = CymatixContextManager(cfg)`; ingest 2 short docs via `mgr.ingest(...)`; then query. Follow how `test_perf_instrumentation.py` constructs managers for its ring tests — copy that file's construction, not this sketch, if they differ.

**Interfaces:**
- Produces: splice ring entries gain `{"n_candidates": int, "splice_target": int}` via the `extra=` hook of `_stage_timer`. Ladder per-needle records gain `splice_n_candidates: Optional[int]`.

- [ ] **Step 1: Failing test** (in the existing ring test file, mirroring its fixture style — the ring is exercised in-process via `get_recent_pipeline_events()`):

```python
def test_splice_ring_entry_records_candidate_count(splice_ring_manager):
    mgr = splice_ring_manager  # built per the fixture recipe above
    mgr.build_context("what does the splice step do?", read_only=True)
    events = [e for e in get_recent_pipeline_events(64) if e["stage"] == "splice"]
    assert events, "splice stage must ring"
    assert isinstance(events[-1]["n_candidates"], int)
    assert events[-1]["n_candidates"] >= 0
    assert isinstance(events[-1]["splice_target"], int)
```

- [ ] **Step 2:** Run that file → FAIL (`KeyError: n_candidates`).
- [ ] **Step 3: Implement.** At the splice `_stage_timer` (~:2233), the locals exist (`candidates`, `_splice_target` computed ~:2223-2227):

```python
with _stage_timer("splice", extra=lambda: {
        "n_candidates": len(candidates), "splice_target": _splice_target}):
```

(The `extra` merge at `_stage_timer.__exit__` ~:210-214 already handles payload dicts; reserved keys still win.)
- [ ] **Step 4:** Ring test PASSES. Then ladder capture: in `run_arm`'s per-needle loop, after each `build_context`, drain the ring (`context_manager.get_recent_pipeline_events(get_pipeline_ring_max())`; the 128-slot ring wraps at ~16 queries, so read per query, filter `stage=="splice"`, take the newest entry for this request) and stash `"splice_n_candidates": entry.get("n_candidates")` into the per-needle record (`None` when splice never ran). Add a pure-helper test: `lad.newest_splice_count(events)` returns the last splice entry's count or `None`.
- [ ] **Step 5:** `python -m pytest tests/test_ablation_ladder_metrics.py <ring-test-file> -q` → PASS.
- [ ] **Step 6: Commit** — `feat(ring): splice n_candidates/splice_target on the pipeline ring + ladder capture (#341 splice-interaction receipt)`

### Task 3: `backends/rerank_backend.py` — in-process `score_pairs`

**Files:**
- Create: `cymatix_context/backends/rerank_backend.py`
- Test: `tests/test_rerank_backend.py` (new); extend `tests/test_layer_devices.py`

**Interfaces:**
- Produces: `score_pairs(query: str, texts: list[str], *, model_name: str | None = None, batch_size: int | None = None) -> list[float]` (module-level; sigmoid scores, one per text; `[]` for empty input; raises only on model-load failure — callers catch). `reset_for_test()` clears the singleton. `DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"`.
- Task 9 adds the remote branch at the top of `score_pairs`; this task builds the in-process core the daemon (Task 8) also reuses via `_get_scorer()`.

- [ ] **Step 1: Failing tests** — no model download; **adapt** (not copy) the `tests/test_deberta_backend.py:64-104` mock idiom: the fake tokenizer records each chunk into a shared `calls` list and returns a `_FakeEncodings`-style dict with a no-op `.to(device)`; the fake model takes a `logits` kwarg and returns an object whose `.logits` is a REAL `torch.tensor([[v] for v in logits_chunk])` (shape `(n, 1)`, matching ms-marco) so `torch.sigmoid(...).squeeze` math runs for real. Both helpers live in this new test file:

```python
# tests/test_rerank_backend.py  (helpers copied from test_deberta_backend.py:64-104)
import pytest
from unittest.mock import MagicMock, patch
from cymatix_context.backends import rerank_backend
from cymatix_context import hardware

@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    rerank_backend.reset_for_test()
    hardware.reset_for_test()          # same autouse reset test_layer_devices uses
    yield
    rerank_backend.reset_for_test()

def test_score_pairs_empty_is_empty_without_load():
    with patch.object(rerank_backend, "_get_scorer", side_effect=AssertionError("must not load")):
        assert rerank_backend.score_pairs("q", []) == []

def test_score_pairs_batches_by_recommended_size(monkeypatch):
    calls = []
    fake_tok, fake_model = _make_tokenizer_mock(calls), _make_model_mock(calls)  # SHARED list — model reads chunk sizes from it
    monkeypatch.setattr(rerank_backend, "_get_scorer", lambda model_name=None: (fake_tok, fake_model, "cpu"))
    monkeypatch.setattr(rerank_backend, "_recommended_batch", lambda: 2)
    scores = rerank_backend.score_pairs("q", ["a", "b", "c"])
    assert len(scores) == 3
    assert all(isinstance(s, float) for s in scores)
    assert [len(c) for c in calls] == [2, 1]      # chunked 2 + 1

def test_scores_preserve_negative_logit_ordering(monkeypatch):
    # sigmoid is monotone: raw logits [-4.0, -1.0, -9.0] must rank b > a > c
    calls = []
    fake_tok, fake_model = _make_tokenizer_mock(calls), _make_model_mock(calls, logits=[-4.0, -1.0, -9.0])
    monkeypatch.setattr(rerank_backend, "_get_scorer", lambda model_name=None: (fake_tok, fake_model, "cpu"))
    monkeypatch.setattr(rerank_backend, "_recommended_batch", lambda: 16)
    s = rerank_backend.score_pairs("q", ["a", "b", "c"])
    assert s[1] > s[0] > s[2]

def test_loader_uses_rerank_layer_device_and_records_model_load(monkeypatch):
    seen = {}
    def _fake_resolve(layer):
        seen["layer"] = layer
        return "cpu"          # NOT `setdefault(...) or "cpu"` — that returns "rerank" as the device
    monkeypatch.setattr(rerank_backend, "resolve_layer_device", _fake_resolve)
    recorded = []
    # record_model_load takes (name, SECONDS) — telemetry/otel.py:826 multiplies by 1000 itself
    monkeypatch.setattr(rerank_backend, "record_model_load", lambda name, seconds: recorded.append((name, seconds < 100)))
    transformers = pytest.importorskip("transformers")
    with patch.object(transformers.AutoTokenizer, "from_pretrained", return_value=MagicMock()), \
         patch.object(transformers.AutoModelForSequenceClassification, "from_pretrained", return_value=MagicMock()):
        rerank_backend._get_scorer()
    assert seen["layer"] == "rerank"
    assert recorded == [("rerank", True)]   # sane-seconds check catches a ms-unit regression
```

Also extend `tests/test_layer_devices.py` with the same source-gate idiom used at `:95`/`:117`:

```python
def test_rerank_backend_consumes_layer_resolution():
    import inspect
    from cymatix_context.backends import rerank_backend
    src = inspect.getsource(rerank_backend)
    assert 'resolve_layer_device("rerank")' in src
```

- [ ] **Step 2:** `python -m pytest tests/test_rerank_backend.py tests/test_layer_devices.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `cymatix_context/backends/rerank_backend.py` (torch imports inside functions only — the store imports this module lazily; keep module import torch-free like `encoder_client`):

```python
"""Cross-encoder rerank scorer (#341). In-process core + (Task 9) remote branch.

Pure (query, texts) -> scores. Gene-aware adaptation lives in the store seam,
not here. Model: [retrieval] rerank_model, device: [hardware] rerank_device via
resolve_layer_device("rerank"), batch: recommended_batch_size("rerank").
"""
from __future__ import annotations
import logging, threading, time
from typing import List, Optional, Tuple

from ..hardware import resolve_layer_device, recommended_batch_size
# record_model_load is imported INSIDE _get_scorer, exactly like splade_backend.py:138:
#   from cymatix_context.telemetry import record_model_load
# (there is NO telemetry.otel_metrics module; a wrong module-level import here would make
# every import of rerank_backend raise, and the store's fallback would silently eat it)

log = logging.getLogger(__name__)

DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_MAX_LENGTH = 256   # same truncation DeBERTaRibosome.re_rank uses; probe-cost-faithful

_lock = threading.Lock()
_scorer: Optional[Tuple[object, object, str]] = None   # (tokenizer, model, device_str)
_scorer_model_name: Optional[str] = None

def reset_for_test() -> None:
    global _scorer, _scorer_model_name
    with _lock:
        _scorer, _scorer_model_name = None, None

def _recommended_batch() -> int:
    return recommended_batch_size("rerank")

def _get_scorer(model_name: Optional[str] = None):
    global _scorer, _scorer_model_name
    name = model_name or DEFAULT_RERANK_MODEL
    with _lock:
        if _scorer is not None and _scorer_model_name == name:
            return _scorer
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        from cymatix_context.telemetry import record_model_load
        t0 = time.monotonic()
        device = resolve_layer_device("rerank")
        tok = AutoTokenizer.from_pretrained(name)
        model = AutoModelForSequenceClassification.from_pretrained(name)
        model.to(torch.device(device))
        model.eval()
        record_model_load("rerank", time.monotonic() - t0)   # SECONDS — otel.py:826 does *1000 itself
        _scorer, _scorer_model_name = (tok, model, device), name
        return _scorer

def score_pairs(query: str, texts: List[str], *, model_name: Optional[str] = None,
                batch_size: Optional[int] = None) -> List[float]:
    if not texts:
        return []
    # Task 9 inserts the remote branch here.
    tok, model, device = _get_scorer(model_name)
    import torch
    bs = batch_size or _recommended_batch()
    out: List[float] = []
    with torch.inference_mode():
        for i in range(0, len(texts), bs):
            chunk = texts[i:i + bs]
            enc = tok([query] * len(chunk), chunk, truncation=True,
                      max_length=_MAX_LENGTH, padding=True, return_tensors="pt").to(device)
            logits = model(**enc).logits
            out.extend(torch.sigmoid(logits.squeeze(-1)).cpu().tolist())
    return out
```

Because the import happens inside `_get_scorer`, the loader test monkeypatches `cymatix_context.telemetry.record_model_load` (the source module), not a `rerank_backend` attribute — adjust the test's `monkeypatch.setattr` target to `"cymatix_context.telemetry.record_model_load"`. The tokenizer/model mocks must match the call shape (`tok(list_a, list_b, truncation=..., max_length=..., padding=..., return_tensors="pt")` returning an object with `.to(device)`; model returning `.logits` as a real `(n,1)` tensor).
- [ ] **Step 4:** Tests PASS.
- [ ] **Step 5: Commit** — `feat(backends): rerank_backend.score_pairs — decoupled cross-encoder core on resolve_layer_device("rerank") (#341)`

### Task 4: Config knobs `[retrieval] rerank_*`

**Files:**
- Modify: `cymatix_context/config.py` (RetrievalConfig ~:416, `__post_init__` ~:781-795, TOML loader ~:1427-1470), `cymatix.toml` `[retrieval]`, the config reference doc `test_config_reference_sync` points at
- Modify: `cymatix_context/retrieval/rerank_combinators.py` (add `resolve_class_flag`)
- Test: extend `tests/test_rerank_combinators.py` (resolver), config tests

**Interfaces:**
- Produces: `RetrievalConfig.rerank_enabled: bool = False`, `rerank_depth: int = 50`, `rerank_model: str = DEFAULT_RERANK_MODEL` (string literal in config.py; keep in sync), `rerank_enabled_by_class: Dict[str, bool] = field(default_factory=dict)`. Validation: unknown class key → `ValueError` (reuse the `VALID_QUERY_CLASSES` check pattern at ~:781-795); `rerank_depth < 1` → `ValueError`.
- `resolve_class_flag(mapping: Dict[str, bool], cls: Optional[str]) -> Optional[bool]` — same contract as `resolve_class_combinator` (:122): explicit class key wins, else `"default"` key, else `None` (= fall back to global `rerank_enabled`).

- [ ] **Step 1: Failing tests:**

```python
# extend tests/test_rerank_combinators.py
def test_resolve_class_flag_precedence():
    from cymatix_context.retrieval.rerank_combinators import resolve_class_flag
    assert resolve_class_flag({"factual": True}, "factual") is True
    assert resolve_class_flag({"default": True}, "multi_hop") is True
    assert resolve_class_flag({"factual": True}, "multi_hop") is None
    assert resolve_class_flag({}, "factual") is None
    assert resolve_class_flag({"factual": False, "default": True}, "factual") is False

def test_retrieval_rerank_knob_defaults_and_validation(tmp_path):
    from cymatix_context.config import load_config
    cfg = load_config(None)
    assert cfg.retrieval.rerank_enabled is False
    assert cfg.retrieval.rerank_depth == 50
    assert cfg.retrieval.rerank_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert cfg.retrieval.rerank_enabled_by_class == {}
    toml = tmp_path / "cymatix.toml"
    toml.write_text('[retrieval]\nrerank_enabled_by_class = { bogus_class = true }\n')
    with pytest.raises(ValueError):
        load_config(str(toml))
```

Plus a TOML round-trip test asserting `rerank_enabled = true`/`rerank_depth = 25` load through (copy the `tmp_path` TOML idiom from `test_rerank_combinators.py:283`).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement:** dataclass fields; `__post_init__` block mirroring :781-795 (`for k, v in self.rerank_enabled_by_class.items(): unknown key → ValueError; not isinstance(v, bool) → ValueError`); depth check; loader lines in the :1427-1470 region (`rerank_enabled=bool(r.get("rerank_enabled", cfg.retrieval.rerank_enabled))` etc., `rerank_enabled_by_class=dict(r.get(...))`); `resolve_class_flag` beside `resolve_class_combinator`. Add the four keys to shipped `cymatix.toml` `[retrieval]` (commented defaults, matching the section's style) and the reference doc.
- [ ] **Step 4:** `python -m pytest tests/test_rerank_combinators.py tests/test_config_default_honesty.py tests/test_config_reference_sync.py -q` → PASS.
- [ ] **Step 5: Commit** — `feat(config): [retrieval] rerank_enabled/depth/model + per-class map on the #255 pattern (default-inert) (#341)`

### Task 5: The store seams — pre-cap rerank in `query_docs_ann` and `query_docs`

**Files:**
- Modify: `cymatix_context/knowledge_store.py`
- Test: `tests/test_rerank_precap.py` (new)

**Interfaces:**
- Consumes: `rerank_backend.score_pairs` (lazy import), Task 4 config knobs.
- Produces:
  - `KnowledgeStore.__init__` gains `rerank_enabled: bool = False, rerank_depth: int = 50, rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2", rerank_scorer: Optional[Callable[[str, List[str]], List[float]]] = None` → `self._rerank_enabled/_rerank_depth/_rerank_model/_rerank_scorer` (scorer kwarg exists for tests/DI; `None` = lazy-import `score_pairs`).
  - `query_docs_ann(..., rerank_override: Optional[bool] = None)` and `query_docs(..., query_text: Optional[str] = None, rerank_override: Optional[bool] = None)`.
  - `self.last_ranked_ids: list[str]` — full-pool final order, published under `_last_query_scores_lock` on BOTH paths, ON or OFF (OFF = sim/fused order, so A/B rank basis is identical).
  - `self.last_rerank_diag: dict` — `{"gate_kept": int, "floor_appended": int}` on the ANN path, both arms (`{}` elsewhere); consumed by the ladder (Task 11) for floor-slice analysis.
  - Signal timing `xenc_rerank` merged into `last_signal_timings` when rerank ran.
- **Count invariant:** ON output length == OFF output length for identical inputs, always.

- [ ] **Step 1: Failing tests.** Fixture recipes (there is no existing fixture that does this — build them in the new file):
  - `store_with_pool`: lex corpus via the `upsert_gene(_make_gene(...), apply_gate=False)` idiom from `tests/test_fusion_rrf.py:66` (~30 docs tagged `"topic"`, exactly one whose content contains the marker `"xylophone"`), `dense_embedding_enabled=True`, then **monkeypatch `store.query_docs_dense_recall` to return a canned `[(gene_id, cosine)]` list** that places the xylophone doc at sim-rank ~20 — the canned list controls both the union order and the gate deterministically, no real encoder.
  - `lex_store_with_pool`: same corpus, `dense_embedding_enabled=False` (exercises the `query_docs` seam).
  - Fake scorer: scores by marker token so membership flips deterministically.

```python
# tests/test_rerank_precap.py — key assertions (fixtures per the recipes above)
def _scorer_favoring(marker):
    def score(query, texts):
        return [1.0 if marker in t else 0.01 for t in texts]
    return score

def test_ann_rerank_preserves_gate_count_and_changes_membership(store_with_pool):
    g = store_with_pool  # fixture: dense-enabled store, ~30 docs, gold doc contains "xylophone", sits at sim-rank ~20
    off = g.query_docs_ann("query text", domains=["topic"], max_genes=12)
    g._rerank_enabled, g._rerank_scorer = True, _scorer_favoring("xylophone")
    on = g.query_docs_ann("query text", domains=["topic"], max_genes=12)
    assert len(on) == len(off)                       # splice-cliff invariant
    assert "xylophone" in on[0].content              # cross-encoder decided order
    assert [d.gene_id for d in on] != [d.gene_id for d in off]

def test_ann_rerank_off_is_byte_identical(store_with_pool):
    g = store_with_pool
    a = [d.gene_id for d in g.query_docs_ann("query text", domains=["topic"], max_genes=12)]
    g._rerank_scorer = _scorer_favoring("xylophone")   # scorer set but knob off
    b = [d.gene_id for d in g.query_docs_ann("query text", domains=["topic"], max_genes=12)]
    assert a == b

def test_rerank_override_beats_global(store_with_pool):
    g = store_with_pool
    g._rerank_enabled, g._rerank_scorer = False, _scorer_favoring("xylophone")
    on = g.query_docs_ann("query text", domains=["topic"], max_genes=12, rerank_override=True)
    assert "xylophone" in on[0].content
    g._rerank_enabled = True
    off = g.query_docs_ann("query text", domains=["topic"], max_genes=12, rerank_override=False)
    assert "xylophone" not in off[0].content

def test_scorer_failure_falls_back_to_gate_order(store_with_pool, caplog):
    g = store_with_pool
    def boom(q, t): raise RuntimeError("model exploded")
    g._rerank_enabled, g._rerank_scorer = True, boom
    off_ids = [d.gene_id for d in g.query_docs_ann("query text", domains=["topic"], max_genes=12, rerank_override=False)]
    on_ids = [d.gene_id for d in g.query_docs_ann("query text", domains=["topic"], max_genes=12)]
    assert on_ids == off_ids
    assert any("rerank" in r.message.lower() for r in caplog.records)

def test_lex_path_rerank_via_query_text(lex_store_with_pool):
    # dense disabled: seam is query_docs post-fusion; fires only when query_text given
    g = lex_store_with_pool
    g._rerank_enabled, g._rerank_scorer = True, _scorer_favoring("xylophone")
    plain = g.query_docs(["topic"], [], max_genes=12)                      # no query_text -> no rerank
    rr = g.query_docs(["topic"], [], max_genes=12, query_text="query text")
    assert len(plain) == len(rr)
    assert "xylophone" in rr[0].content

def test_last_ranked_ids_published_both_arms(store_with_pool):
    g = store_with_pool
    g.query_docs_ann("query text", domains=["topic"], max_genes=12)
    off_order = list(g.last_ranked_ids)
    g._rerank_enabled, g._rerank_scorer = True, _scorer_favoring("xylophone")
    g.query_docs_ann("query text", domains=["topic"], max_genes=12)
    on_order = list(g.last_ranked_ids)
    assert set(off_order) == set(on_order)           # same pool, different order
    assert on_order != off_order
    assert "xenc_rerank" in g.last_signal_timings
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** Core helper + two call sites.

```python
# knowledge_store.py — near apply_ann_gate
_RERANK_TEXT_MAX = 2000   # chars; tokenizer re-truncates at 256 tokens anyway

def _rerank_doc_text(doc) -> str:
    return (doc.content or (doc.promoter.summary if doc.promoter else "") or "")[:_RERANK_TEXT_MAX]
```

Store method:

```python
def _rerank_effective(self, override):
    return self._rerank_enabled if override is None else bool(override)

def _score_rerank(self, query_text, docs) -> "Optional[list[float]]":
    """Score docs against query_text; None = fall back (never raises)."""
    try:
        scorer = self._rerank_scorer
        if scorer is None:
            from .backends.rerank_backend import score_pairs
            scorer = lambda q, t: score_pairs(q, t, model_name=self._rerank_model)
        scores = scorer(query_text, [_rerank_doc_text(d) for d in docs])
        if not isinstance(scores, list) or len(scores) != len(docs):
            log.warning("rerank scorer returned unusable payload; falling back")
            return None
        return [float(s) for s in scores]
    except Exception:
        log.warning("rerank scoring failed; falling back to gate order", exc_info=True)
        return None
```

`query_docs_ann` seam — replace the single `apply_ann_gate` call (~:4013) with:

```python
gate_ids = apply_ann_gate(
    [(doc.gene_id, sim) for doc, sim in scored], dense_hits,
    threshold=threshold, min_genes=min_genes, max_genes=max_genes,
    dense_pool_floor_genes=self._dense_pool_floor_genes)
kept_ids = gate_ids
final_order = [doc.gene_id for doc, _ in scored]
head_id_set = {doc.gene_id for doc, _ in scored[:min(self._rerank_depth, len(scored))]}
n_floor = sum(1 for g in gate_ids if g not in head_id_set)
if self._rerank_effective(rerank_override) and scored:
    _xt0 = time.monotonic()
    depth = min(self._rerank_depth, len(scored))
    by_id = {doc.gene_id: doc for doc, _ in scored}
    # Candidate set = union head PLUS gate-kept ids beyond it (#214 floor
    # rescues land there) — the cross-encoder judges rescued docs, never
    # blindly evicts them. Bodies for the whole pool are already fetched.
    head_ids = [doc.gene_id for doc, _ in scored[:depth]]
    cand_ids = head_ids + [g for g in gate_ids if g not in head_id_set]
    cand_docs = [by_id[g] for g in cand_ids if g in by_id]
    xenc = self._score_rerank(query, cand_docs)
    if xenc is not None:
        order = sorted(range(len(cand_docs)), key=lambda i: (-xenc[i], cand_docs[i].gene_id))
        ranked_cand = [cand_docs[i].gene_id for i in order]
        kept_ids = ranked_cand[:len(gate_ids)]   # count from the gate; order+membership from the cross-encoder
        _in_cand = set(ranked_cand)
        final_order = ranked_cand + [d.gene_id for d, _ in scored if d.gene_id not in _in_cand]
    with self._last_query_scores_lock:
        _m = dict(getattr(self, "last_signal_timings", None) or {})
        _m["xenc_rerank"] = _m.get("xenc_rerank", 0.0) + (time.monotonic() - _xt0) * 1000.0
        self.last_signal_timings = _m
with self._last_query_scores_lock:
    self.last_ranked_ids = final_order
    # Published on BOTH arms so the verdict can slice floor-fired needles.
    self.last_rerank_diag = {"gate_kept": len(gate_ids), "floor_appended": n_floor}
```

(`last_ranked_ids` and `last_rerank_diag` initialized to `[]`/`{}` in `__init__` beside the other `last_*` diagnostics. The `query_docs_ann` dense-off early return at ~:3957 publishes the **unreranked** lex order — its inner `query_docs` call never receives `query_text`, and that path is only reachable by direct store callers; the manager's dense-off path calls `query_docs` directly with `query_text`, so it gets the rerank + publication inside `query_docs` itself.)

`query_docs` seam — **after the RRF/additive `if/else`** (both branches produce `ranked_ids`; RRF via `combine_rerank` ~:3489-3498, additive via the sorted slice ~:3523), so the legacy `fusion_mode="additive"` profile is covered too. When `self._rerank_effective(rerank_override) and query_text is not None`, widen the pre-seam truncation in BOTH branches (`combine_rerank` gets `limit=max(limit, self._rerank_depth)`; the additive slice becomes `[:max(limit, self._rerank_depth)]`), then:

```python
depth = min(self._rerank_depth, len(ranked_ids))
head_ids = ranked_ids[:depth]
head_docs_map = self._load_genes_by_ids(head_ids)
head_docs = [head_docs_map[g] for g in head_ids if g in head_docs_map]
xenc = self._score_rerank(query_text, head_docs)
if xenc is not None:
    order = sorted(range(len(head_docs)), key=lambda i: (-xenc[i], head_docs[i].gene_id))
    reordered = [head_docs[i].gene_id for i in order]
    ranked_ids = (reordered + [g for g in ranked_ids if g not in set(reordered)])[:limit]
```

**Timing in this function goes through the local signal collector** (the same per-signal ms dict `query_docs` builds and publishes wholesale into `last_signal_timings` at its exit, ~:3592 region) — record `xenc_rerank` there, NOT by merging into `self.last_signal_timings` directly, or the exit publication clobbers it. Then `ranked_ids = ranked_ids[:limit]` flows into tie-break/body-fetch unchanged. Publish `self.last_ranked_ids = list(ranked_ids)` **unconditionally** (both fusion modes, rerank on or off) at the existing `last_query_scores` publication point (~:3505-3508) so the metric basis can never go stale. Signature additions: `query_docs(..., query_text: Optional[str] = None, rerank_override: Optional[bool] = None)`, `query_docs_ann(..., rerank_override: Optional[bool] = None)`. **`query_docs_ann` must NOT pass `query_text` to its inner `query_docs` call** (that's the double-rerank suppression) — but MUST forward `rerank_override` into nothing (the inner call doesn't rerank at all).
- [ ] **Step 4:** `python -m pytest tests/test_rerank_precap.py tests/test_fusion_rrf.py tests/test_fusion_invariance.py tests/test_retrieval_invariance.py tests/test_dense_pool_floor.py tests/test_ann_threshold.py -q` → PASS.
- [ ] **Step 5: Commit** — `feat(retrieval): #341 pre-cap cross-encoder rerank at the ANN-gate and fusion seams — count-preserving, default-off`

### Task 6: Manager threading — config → store, per-class override, query text

**Files:**
- Modify: `cymatix_context/context_manager.py` (store-ctor kwargs: the `open_read_source(...)` call ~:950 — kwargs fan through `**genome_kwargs` to `KnowledgeStore` AND every per-shard `Genome`, so add the three rerank kwargs to that dict, beside `rerank_combinator` ~:1010; per-class resolve ~:1915-1918; `_retrieve` ~:2899-2972)
- Test: extend `tests/test_classifier_gated_combinator.py` pattern in `tests/test_rerank_precap.py`

**Interfaces:**
- Consumes: `resolve_class_flag` (Task 4), store kwargs (Task 5).
- Produces: manager passes `rerank_enabled=config.retrieval.rerank_enabled, rerank_depth=..., rerank_model=...` to the store ctor; per turn resolves `_rerank_override = resolve_class_flag(cfg.retrieval.rerank_enabled_by_class, classifier_result.cls if classifier_result else None)` beside the existing combinator resolve (~:1915), threads it through `_retrieve(..., rerank_override=...)` into `query_docs_ann(..., rerank_override=...)` / `query_docs(..., query_text=query, rerank_override=...)` (the dense-off branch passes the raw query string as `query_text`).

- [ ] **Step 1: Failing test** — manager-level, spy on the store method (mirror `_spy_combinator` at `test_classifier_gated_combinator.py:411`). `seeded_manager_cls_gated` does NOT exist — create the factory in the new test file, adapted from `_seeded_manager` at `test_classifier_gated_combinator.py:375`: same corpus/classifier setup, but set `cfg.retrieval.rerank_enabled_by_class = rerank_map` and `cfg.retrieval.rerank_enabled = rerank_enabled` before constructing the manager:

```python
def test_manager_threads_per_class_rerank_override(monkeypatch, seeded_manager_cls_gated):
    mgr = seeded_manager_cls_gated(rerank_map={"factual": True}, rerank_enabled=False)
    seen = {}
    real = mgr.genome.query_docs_ann
    def spy(*a, **kw):
        seen["override"] = kw.get("rerank_override")
        return real(*a, **kw)
    monkeypatch.setattr(mgr.genome, "query_docs_ann", spy)
    mgr.build_context("what is the capital of France", read_only=True)   # classifies factual
    assert seen["override"] is True
```

Plus one asserting the dense-off path passes `query_text=<the query>` into `query_docs`.
- [ ] **Step 2:** FAIL. **Step 3:** implement the three threading points. **Step 4:** PASS (`tests/test_rerank_precap.py tests/test_classifier_gated_combinator.py`).
- [ ] **Step 5: Commit** — `feat(retrieval): thread [retrieval] rerank knobs + per-class override through the manager (#341)`

### Task 7: Delete the dead blend rerank path + `[ingestion] rerank_enabled`

**Files:**
- Modify: `cymatix_context/scoring/blend.py` (:243-256 → unconditional `candidates = candidates[:max_genes]`; drop `allow_rerank`/`rerank_enabled`/`ribosome` params from `apply_candidate_refiners`)
- Modify: `cymatix_context/context_manager.py` (:2042-2052 span keeps the name `"rerank"` — ring consumers depend on it; add a one-line comment `# stage name kept for ring continuity; the cross-encoder now runs pre-cap in the store`; `_apply_candidate_refiners` drops the three kwargs; ~:3126-3128 cleanup)
- Modify: `cymatix_context/server/routes_context.py` (~:793-800), `server/routes_admin.py` (~:276-283) — drop `allow_rerank=(profile == "quality")` plumbing
- Modify: `cymatix_context/config.py` — delete `IngestionConfig.rerank_enabled` (:273) + loader line (~:1334); `cymatix.toml` (:260) + reference doc
- Test: update `tests/test_blend_mode.py` and any test constructing `apply_candidate_refiners(...)` with the removed kwargs (`Grep tests/ for allow_rerank|rerank_enabled` — the map found zero direct tests of the branch, but signatures appear in fixtures)

- [ ] **Step 1:** Write the regression test FIRST (in `tests/test_rerank_precap.py`): `apply_candidate_refiners` caps deterministically and no longer accepts `allow_rerank`:

```python
def test_blend_cap_is_unconditional_truncation():
    import inspect
    from cymatix_context.scoring.blend import apply_candidate_refiners
    sig = inspect.signature(apply_candidate_refiners)
    for gone in ("allow_rerank", "rerank_enabled", "ribosome"):
        assert gone not in sig.parameters
    src = inspect.getsource(apply_candidate_refiners)
    assert 'hasattr' not in src.split("def ", 1)[-1] or "rerank" not in src
```

- [ ] **Step 2:** FAIL. **Step 3:** delete/simplify all five files' plumbing. `[ingestion] rerank_model` STAYS (legacy deberta ctor input — add a comment `# legacy: feeds DeBERTaRibosome only; the retrieval cross-encoder reads [retrieval] rerank_model`).
- [ ] **Step 4:** **Full suite**: `python -m pytest tests/ -m "not live" -q` → green (this task touches wide plumbing; the run also proves Tasks 1-6 integrate).
- [ ] **Step 5: Commit** — `refactor(blend)!: remove the dead post-cap rerank branch + [ingestion] rerank_enabled (#341; superseded by [retrieval] pre-cap rerank)`

### Task 8: Daemon — rerank family + `/encode/rerank`

**Files:**
- Modify: `cymatix_context/encoder_daemon.py`
- Test: extend `tests/test_encoder_daemon.py`

**Interfaces:**
- Produces: `BUNDLE_FAMILIES = ("dense", "splade", "sema")` (new name for old FAMILIES semantics), `FAMILIES = BUNDLE_FAMILIES + ("rerank",)`. `EncoderRegistry` gains `rerank_score_batch(query: str, texts: List[str]) -> List[float]` (fake injectable via ctor kwarg `rerank=`; production `load()` builds a closure over `rerank_backend._get_scorer()`-based scoring with `cfg.retrieval.rerank_model`). `POST /encode/rerank` with `RerankRequest(query: str, texts: List[str])` → `{"scores": [...], "model": "<name>"}`. `DAEMON_MAX_BATCH` gains `"rerank": 16`. Capacity report: `devices`/`loaded`/`max_batch`/`warm_encodes_per_s` naturally include rerank via `FAMILIES`; `bundles_per_s` switches to `BUNDLE_FAMILIES` (bars preserved). Cache key: `("rerank", model, query, text)` per item. Batcher: `make_sequential_executor`. `_probe_bundle` includes a rerank probe pair (readiness covers the family).

- [ ] **Step 1: Failing tests** (fake-codec style, `make_registry(rerank=...)`). `client_ready` does NOT exist — compose it in place from the file's own helpers: `_app(make_registry(rerank=fake_rerank))` (`tests/test_encoder_daemon.py:121/:532`) + the file's ready-marking idiom (run startup / hit `/ready` until 200), wrapped in `fastapi.testclient.TestClient`. `_TestClientTransport` for the Task 9 round-trip also lives in `tests/test_encoder_daemon.py` (~:923-955), not test_remote_codecs:

```python
def test_encode_rerank_returns_scores_and_model(client_ready):   # reuse _app/make_registry helpers
    r = client_ready.post("/encode/rerank", json={"query": "q", "texts": ["a", "b"]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["scores"]) == 2 and body["model"]

def test_encode_rerank_empty_texts_short_circuits(client_ready):
    r = client_ready.post("/encode/rerank", json={"query": "q", "texts": []})
    assert r.status_code == 200 and r.json()["scores"] == []

def test_capacity_report_includes_rerank_family_and_preserves_bundle_semantics(client_ready):
    cap = client_ready.get("/health").json()["capacity"]
    assert "rerank" in cap["devices"] and "rerank" in cap["max_batch"] and "rerank" in cap["loaded"]
    assert set(cap["warm_encodes_per_s"]) >= {"dense", "splade", "sema", "rerank"}
    assert cap["bundles_per_s"]["interleaved"] is not None   # 3-family bundle math unbroken
```

plus add `"/encode/rerank"` to the parametrized `test_encode_before_ready_is_refused_with_503` list (:833-839), a worker-survives-failure case, and a vector-cache hit test keyed on `(query, text)`.
- [ ] **Step 2:** FAIL. **Step 3:** implement the family split (mechanical rename at :139/:143/:505/:506/:956-995/:1151-1188/:1201-1237/:1034-1061 — `bundles_per_s` and `_payload`'s bundle iteration use `BUNDLE_FAMILIES`; registry/capacity loops use `FAMILIES`), `RerankRequest` (:1262 block), the 4-line handler mirroring `/encode/splade` (:1416), per-item `submit_cached("rerank", (query, text))` with `_join`. **Step 4:** `python -m pytest tests/test_encoder_daemon.py -q` → PASS.
- [ ] **Step 5: Commit** — `feat(daemon): /encode/rerank + rerank family in registry/capacity; bundle bars preserved via BUNDLE_FAMILIES (#341)`

### Task 9: Client — `encode_rerank` + remote branch in `rerank_backend`

**Files:**
- Modify: `cymatix_context/backends/encoder_client.py`, `cymatix_context/backends/rerank_backend.py`
- Test: extend `tests/test_encoder_client.py`, `tests/test_remote_codecs.py`, `tests/test_rerank_backend.py`, round-trip in `tests/test_encoder_daemon.py`

**Interfaces:**
- Produces: `EncoderClient.encode_rerank(query: str, texts: List[str], timeout_s: Optional[float] = None) -> List[float]` (POST `/encode/rerank`, `_require(resp, "scores", ...)`). `rerank_backend._remote_scores(query, texts) -> Optional[List[float]]` inserted at the top of `score_pairs`, mirroring `splade_backend._remote_sparse` exactly: `active_url()` → `should_attempt()` → `ready_gate(client)` → `client.encode_rerank(query, texts, timeout_s=batch_timeout_s(len(texts)))` → shape gate (`len == len(texts)`, all floats) → `record_success()`; any failure → `record_failure()` → `None` → in-process fallback.

- [ ] **Step 1: Failing tests:** canned-handler `encode_rerank` happy-path + malformed-payload in `test_encoder_client.py`; in `test_remote_codecs.py` copy the splade OFF/ON/fallback trio for rerank AND extend `test_shared_ready_gate_is_polled_once_across_all_three_seams` (:756) + `test_cold_daemon_gate_blocks_encodes_then_all_three_seams_recover` (:769) to enumerate the rerank seam (three→four); timeout-scaling assertion via `_install_capturing_transport` (:1021): 50 texts → `10.0 + 50*0.25 = 22.5s`; round-trip test against the daemon TestClient via `_TestClientTransport` (~:925).
- [ ] **Step 2:** FAIL. **Step 3:** implement both sides (~25 lines total). **Step 4:** `python -m pytest tests/test_encoder_client.py tests/test_remote_codecs.py tests/test_rerank_backend.py tests/test_encoder_daemon.py -q` → PASS.
- [ ] **Step 5: Commit** — `feat(encoder-client): remote rerank seam — circuit/ready-gate/fallback parity with splade (#341)`

### Task 10: Capacity bench + receipt checker

**Files:**
- Modify: `benchmarks/dogfood/erb/encoder_daemon_capacity.py`, `benchmarks/dogfood/check_encoder_receipts.py`
- Test: extend `tests/test_check_encoder_receipts.py`

**Interfaces:**
- Produces: capacity script gains `--rerank-pairs` (default 50) and per-level rerank load: each client also POSTs `/encode/rerank` with `--rerank-pairs` texts; receipt levels gain `rerank_scores_total`, `rerank_pairs_per_s`, `rerank_latency_p50_ms/p95_ms`. Checker: `errors_zero` covers rerank; `capacity_bundles_per_s` gate UNCHANGED; new informational (non-binding) `rerank_pairs_per_s` reporting. `aggregate_level` stays pure — extend its signature with optional rerank vectors and unit-test.

Design (bar-preserving): the rerank load runs as a **separate timed phase after the bundle phase within each level** — bundle wall-clock stays unpolluted, so `bundles_per_s` and its 3.5/30 bars are untouched. `aggregate_level` gains keyword-only params `rerank_ms: Optional[list] = None, rerank_errors: int = 0, rerank_pairs_per_request: int = 0` (stays pure; `None` → rerank keys omitted from the row). Rerank errors count into the level's existing `errors` field so the binding `errors_zero` gate covers rerank with no checker change; `rerank_pairs_per_s` is informational. Pair texts reuse `_client_text`.

- [ ] **Step 1: Failing tests:**

```python
def test_aggregate_level_with_rerank_vectors():
    row = cap.aggregate_level(2, [100.0, 120.0], 0, 1.0,
                              rerank_ms=[200.0, 240.0], rerank_errors=0, rerank_pairs_per_request=50)
    assert row["rerank_pairs_per_s"] > 0
    assert row["rerank_latency_p50_ms"] == 200.0   # nearest-rank percentile(); pin the exact value the helper returns
    assert row["errors"] == 0

def test_aggregate_level_without_rerank_omits_keys():
    row = cap.aggregate_level(1, [100.0], 0, 1.0)
    assert "rerank_pairs_per_s" not in row

def test_checker_keeps_bundle_gate_binding_and_rerank_informational(receipt_with_rerank):
    out = check_encoder_capacity(receipt_with_rerank, bar=3.5)
    assert any(g["name"] == "capacity_bundles_per_s" for g in out["gates"])
    assert not any(g["name"].startswith("rerank") for g in out["gates"])   # informational = notes, not gates
```

(`receipt_with_rerank` = the file's existing receipt-dict fixture extended with rerank level keys; follow `tests/test_check_encoder_receipts.py`'s fixture style.)
- [ ] Steps 2-4: FAIL → implement (`--rerank-pairs` CLI, default 50; per-client rerank POSTs in `run_level`'s thread body after its bundle loop; receipt keys; checker notes) → `python -m pytest tests/test_check_encoder_receipts.py -q` → PASS.
- [ ] **Step 5: Commit** — `feat(bench): rerank family in the daemon capacity probe + receipts (#341)`.

### Task 11: Ladder arms + A/B runner with daemon proof

**Files:**
- Modify: `benchmarks/dogfood/erb/ablation_ladder.py` (arms + `final_*` metrics + encoder-proof fields)
- Create: `benchmarks/dogfood/erb/rerank_ab.py`
- Test: extend `tests/test_ablation_ladder_metrics.py`

**Interfaces:**
- **Ladder validation machinery first:** the ladder's validation kinds can only prove signal ABSENCE (`expect_absent`) — an enable-arm is unfalsifiable with them. Add a new kind `VALIDATION_SIGNAL_PRESENT` with an `expect_present: tuple` field on `Arm`, plus the `validate_arm` branch: valid iff every expected key is present in the arm's observed `signal_keys` AND absent from the baseline's. Unit-test both directions (present-in-arm/absent-in-baseline → valid; missing-in-arm → invalid; present-in-baseline → invalid).
- `ARMS` gains `rerank_on` (`{"retrieval.rerank_enabled": True}`, gate string `knowledge_store.py: pre-gate cross-encoder (#341)`, `VALIDATION_SIGNAL_PRESENT` with `expect_present=("xenc_rerank",)`) and `rerank_off` (`{"retrieval.rerank_enabled": False}`, existing signal-absence validation with `expect_absent=("xenc_rerank",)`; identity-dropped when base config is OFF). **Run2 inversion semantics (state in the runner's docstring):** in run2 the ON side is the ladder's *baseline control arm* — `rerank_off` is the ablation; the paired delta sign inverts (arm − baseline = OFF − ON), and the runner normalizes direction before any gate math using the wrapper's `arm_order` field.
- Per-needle records gain `final_rank_of_first_gold` / `final_recall_hit` from `manager.genome.last_ranked_ids` (rank = 1-based position of first gold, `None` if absent) and `gate_kept` / `floor_appended` from `manager.genome.last_rerank_diag`; arm rows gain `final_recall_at_k`. OFF arms publish sim/fused order + diag too (Task 5), so every field is present and paired.
- `rerank_ab.py`: CLI `--genome --resolved --gold --limit --k --config-off --config-on --port 11439 --daemon-python <venv> --out-prefix` — for EACH of the two runs it spawns a **fresh daemon with its own log file** (`<out-prefix>_run{N}_daemon.log`; import `spawn_daemon`/`wait_ready`/`_taskkill_tree` from `encoder_daemon_capacity.py` via `sys.path` insertion of its own dir — they're module-level; fresh daemon per run also keeps `spawn_token_verified` per-run meaningful and avoids cumulative POST counts defeating the refusal check), sets `CYMATIX_ENCODER_URL` for the ladder subprocess, runs the ladder (`[sys.executable, "benchmarks/dogfood/erb/ablation_ladder.py", ...]` from worktree cwd; run1 `--config <config-off> --arms rerank_on`, run2 `--config <config-on> --arms rerank_off`; both `--per-query`), tears the daemon down, counts `POST /encode/rerank` lines in that run's log (`count_daemon_posts(log_path, "POST /encode/rerank")` — new pure helper, unit-tested), and writes `<out-prefix>_run{N}.json` wrappers `{ladder_receipt_path, daemon_log, daemon_log_posts_rerank, daemon_log_posts_total, spawn_token_verified, arm_order, on_side}`. **Refuses (exit nonzero, loud) any run whose ON side shows `daemon_log_posts_rerank < n_queries + 1`** (the +1 is the ON side's untimed warmup query — in run2 the ON side is the baseline arm, so the check applies to the run total; a shortfall means in-process fallback happened, i.e. the silent-fallback trap). It also records `daemon_log_posts_total` so a dense-leg fallback divergence between runs is visible in the verdict.

- [ ] Steps: failing tests for `VALIDATION_SIGNAL_PRESENT` (both directions, see interface bullet) + `count_daemon_posts` (tmp log file fixture with mixed `POST /encode/dense` / `POST /encode/rerank` / non-POST lines) + `final_rank_of_first_gold` helper (pure: `(ranked_ids, gold) -> Optional[int]`) + arm-table entries validate (`lad.ARMS` contains `rerank_on` whose knob path resolves via `set_dotted` on a fresh config, and whose validation kind is the new one) → implement → PASS → **full suite** → commit `feat(bench): rerank_on/off arms, final-order recall, daemon-proof A/B runner (#341)`.

### Task 12: Run the receipts

No new code — execution + receipt files. All ladder/bench invocations from the worktree cwd; server-less (ladder is in-process). GPU daemon via `--daemon-python f:/tmp/venv-cuda/Scripts/python.exe`.

- [ ] **(pre) Cell configs:** write `benchmarks/dogfood/erb/configs/rerank_off_829k.toml` and `rerank_on_829k.toml` — copies of shipped `cymatix.toml` with `[ingestion] splade_enabled=false` (nosplade bed! trap 5) and `[retrieval] rerank_enabled` false/true respectively; same pair for 100k (splade stays per-bed-appropriate: `erb_100k.db` was built with its own profile — keep `splade_enabled` matching the bed's committed A/B configs, check `receipts/synonyms_ab_100k_run1.json`'s recorded config for the precedent).
- [ ] **(a) 829k A/B ×2 (order-reversed):** `python benchmarks/dogfood/erb/rerank_ab.py --genome f:/tmp/erb_carve/erb_829k_nosplade.db --resolved benchmarks/dogfood/erb/needles_resolved_blob.json --gold benchmarks/dogfood/erb/gold_by_needle_blob.json --limit 30 --k 12 --config-off ...rerank_off_829k.toml --config-on ...rerank_on_829k.toml --daemon-python f:/tmp/venv-cuda/Scripts/python.exe --out-prefix benchmarks/dogfood/erb/receipts/rerank_ab_829k`. Gate: paired per-needle `final_recall_at_k` delta ≥ +5pp AND p50 wall ratio ≤ 1.05, consistent in both run orders. Verify `splice_n_candidates` distributions are IDENTICAL between arms per needle (the splice-interaction receipt — assert in the verdict doc, values from per-needle records).
- [ ] **(b) 100k A/B ×2:** same runner on `erb_100k.db` + carve needles/gold. Direction + cost only.
- [ ] **(c) Dogfood cost:** run through `rerank_ab.py` like (a)/(b) — same daemon routing, `--per-query`, and POST-count refusal; a bare "daemon up" ladder run routes NOTHING (default `[encoder_daemon] url=""`) and would silently measure the in-process CPU fallback. Add a `--single-run on` mode to `rerank_ab.py` (spawn daemon → one ladder run `--arms rerank_on` against the default-config base → proof counts) OR run both orders like (a) — either satisfies the ON-arm discipline. Paths (dogfood assets live in the MAIN checkout, not the worktree — absolute paths): `--genome F:/Projects/cymatix-context/genomes/dogfood/genome.db` (verify with `cymatix diag corpus` / `ls` first; if the dogfood genome lives elsewhere, locate it via the main checkout's `cymatix.toml` `[genome] path`), needles/gold: the dogfood needle set used by prior dogfood receipts (find via `grep -l dogfood benchmarks/dogfood/*.json` and the ledger's dogfood rows; if no gold file exists for the dogfood bed, the receipt degrades to cost-only — p50/p95 deltas — and says so). GPU daemon (`--daemon-python f:/tmp/venv-cuda/...`) for the headline number; optionally repeat with the CPU venv to document both. Document p50 delta honestly in the verdict (219ms-class cost on ~0.5s queries is large in relative terms; DEFAULT STAYS OFF; the receipt informs per-class/size gating).
- [ ] **(d) Capacity:** `encoder_daemon_capacity.py` CPU (main venv) + GPU (`--python f:/tmp/venv-cuda/...`) with rerank levels; `check_encoder_receipts.py --kind encoder_capacity` green on both.
- [ ] Sanity guards per run: daemon token-verified ready; `nvidia-smi` shows the daemon process on GPU runs; bed-pristine evidence for 829k defined as: main `.db` file SIZE unchanged AND no splade tables present after the run (`sqlite3 <bed> ".tables" | grep -c splade` → 0) AND `-wal` sidecar empty/absent after close — mtime alone is NOT the criterion (rw open touches it). If first-open DDL is detected, stop and fix the cell config before burning a bench pass.

### Task 13: Verdict doc, issues, roadmap, PR

- [ ] `docs/benchmarks/2026-08-06-rerank-wiring-receipts.md` (or dated day-of-run): bars table (bar vs measured, both run orders, direction-normalized per the `arm_order`/`on_side` wrapper fields) reporting `final_recall_at_k` delta AND `delivered_gold_rate` delta side by side (a final-order gain that never survives blend/delivery must be visible); OFF-arm `final_recall_at_k` printed beside the probe's 0.433 as a sanity anchor, with divergence explained via the `gate_kept` per-needle counts (the probe's `pool=` column analog — sub-50-pool needles are exactly sliceable); floor-fired needles sliced via `floor_appended > 0`; delivered-gold + final-order recall definitions and why the rank basis changed (incl. the note that the committed "order-reversed ×2" receipts predating this campaign were repeat runs — this campaign's runs are genuinely order-reversed via inverted base configs); splice-interaction verification (`splice_n_candidates` identical per needle across arms); dogfood honesty note; capacity table; pair-text assumption note; config surface summary; removal notice for `[ingestion] rerank_enabled` (dead under deberta via the `re_rank`/`rerank` name mismatch; misrouted to the LLM reranker under ollama/litellm).
- [ ] Update `docs/ROADMAP.md` Track 4: #341 row (:309) → receipts + verdict; #335 row (:308) → delivered-gold landed; #340 row (:310) → restate near-cutoff-vs-static split under the new receipts; operating-profile box (:321-323) only if the serving profile changes (it shouldn't — default off).
- [ ] `gh issue comment` on #341 (receipts + verdict), #335 (delivered-gold landed — unblocks coact/ANN-gate/synonym verdicts), #340 (near-cutoff class now convertible; static-miss unchanged).
- [ ] Full suite green; `superpowers:requesting-code-review` then PR per repo convention (base: `fork1/encoder-daemon` while #332 is open — retarget to master if it merges first), body with receipts links, `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` + `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

---

## Self-review notes (spec coverage)

- Kickoff build items: (1) pre-cap seam → Tasks 5-6; (2) decoupled knob + per-class → Tasks 4, 6, 7; (3) rerank device → Task 3; (4) daemon endpoint + client seam → Tasks 8-9; (5) delivered-gold first → Task 1. Receipts (a)-(d) + splice check → Tasks 2, 10, 11, 12. Discipline items → global constraints + Tasks 12-13.
- Deliberate deviations from a literal kickoff reading, with reasons: the primary seam is `query_docs_ann` pre-gate (not only `query_docs`) because the probe itself measured "fused top-50 from query_docs_ann" and the default path's binding cut is `apply_ann_gate`; "order-reversed pairs" is implemented as inverted-base-config runs because the ladder hard-codes baseline-first (the committed "order-reversed" receipts were actually repeat runs — worth a line in the verdict doc); recall@12 for the bars is final-order-based (`last_ranked_ids`) because the ladder's legacy score-map rank cannot see the rerank at all; the splice-interaction receipt reads the ring in-process (`get_recent_pipeline_events`) rather than over `/debug/pipeline/recent` because the ladder runs server-less — same ring, same entries; and the rerank seam covers BOTH fusion modes (the additive branch too), since `[retrieval] fusion_mode="additive"` is still a supported legacy profile — the seam and `last_ranked_ids` publication sit after the RRF/additive if/else, not inside the RRF branch.

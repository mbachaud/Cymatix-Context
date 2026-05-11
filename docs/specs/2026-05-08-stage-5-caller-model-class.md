# Stage 5 — `caller_model_class` opt-in render branch

Plan: helix-context retrieval-fix, Stage 5 of 6 (council 2026-05-08). Layered on Stages 1-4; generic branch is regression-locked to pre-Stage-5 output.

## 1. Goals + non-goals

**Goals.**
- Stop hurting frontier callers (Claude Opus 4.7, GPT-5, Gemini 3 Pro) with foveated rank-reversal and 20-entry slate truncation.
- Give small-MoE callers a JSON-shaped, character-bounded slate so attention-routing locks survive budget enforcement.
- Cross the classifier axis with the model-class axis without collapsing them.

**Non-goals.**
- No User-Agent / model-string auto-detection (caller opts in).
- No second endpoint, no second packet schema. One `/context`, branches inside `build_context`.
- No LLM in the retrieval path. Stage 5 is rule-table only.
- No know/miss block (Stage 6).

## 2. Surface area

| File | Lines | Change |
|---|---|---|
| `helix_context/schemas.py` | end of file | new `CallerModelClass` str-Enum + `CALLER_MODEL_CLASS_DEFAULT="generic"` |
| `helix_context/server.py` | 772–870 | parse `caller_model_class` from request, validate, pass through |
| `helix_context/server.py` | 569–571 | thread param into `build_context_async` for `/v1/chat/completions` path too |
| `helix_context/context_manager.py` | `build_context` signature | new kwarg `caller_model_class: str = "generic"` |
| `helix_context/context_manager.py` | 680–694 (`_should_use_slate`) | extend: `caller_model_class=="small_moe"` forces True; `=="frontier"` forces False |
| `helix_context/context_manager.py` | 1089–1103 (foveated apply) | gate: `frontier` skips reversal entirely |
| `helix_context/context_manager.py` | 1117–1130 (slate population) | branch on class for ordering + JSON shape |
| `helix_context/context_manager.py` | 2173–2189 (slate cap) | replace `unique_slate[:20]` with char-bounded greedy fill |
| `helix_context/legibility.py` | 122–157 (`format_gene_header`) | suppress headers when class is `small_moe` (token cost > legibility benefit at 4B) |
| `helix_context/query_classifier.py` | 113–179 | `decoder_mode` lookup table replaces hard-coded values; new `resolve_decoder_mode(cls, caller_model_class)` |
| `bench/bench_needle_1000.py` | argparse | `--caller-model-class {generic,small_moe,frontier}` |
| `tests/test_caller_model_class.py` | NEW | per §10 |
| `helix_context/telemetry.py` | OTel span emit | new attribute + counter |

## 3. Wire-format addition

Pydantic enum in `schemas.py`:

```python
class CallerModelClass(str, Enum):
    generic   = "generic"
    small_moe = "small_moe"
    frontier  = "frontier"
```

`/context` request adds optional `caller_model_class: CallerModelClass = "generic"`. Unknown string → 400 with allowed list (mirrors `response_mode` validation at server.py:846). Echoed back at `response.metadata.caller_model_class` for debugging. The `/v1/chat/completions` proxy path passes whatever the body sets, defaulting `"generic"` (preserves today's behavior for Continue).

## 4. Behavior matrix

Rows = caller_model_class, columns = behavior knobs. **`generic` row is the regression baseline — every cell equals today's hard-coded behavior.**

| | foveated | slate emitted | slate_format | slate_bound | assembly_cap | decoder_mode_default | legibility_headers | candidate_order |
|---|---|---|---|---|---|---|---|---|
| **generic** | ON if `budget_tier=="broad"` and `foveated_enabled` | iff `_should_use_slate()` (server MoE flag OR small downstream) | `\n`-joined raw KV lines | **20 entries** (status quo) | `classifier.assembly_max_genes_cap` | per classifier (table §6 `generic` column) | ON when `legibility_on` | reversed if foveated active, else forward |
| **small_moe** | ON (always, regardless of `budget_tier`) | always ON | **JSON object** `<helix:slate>{...}</helix:slate>` | **1500 chars** (config: `slate_char_budget`) | `min(classifier_cap, 4)` | per classifier (table §6 `small_moe` column) | OFF (suppressed — 4B doesn't need them, costs ~80 tok/gene) | reversed (small models genuinely benefit from recency) |
| **frontier** | **OFF** (skip reversal entirely) | OFF | n/a | n/a | `max(12, classifier_cap*2)` | per classifier (table §6 `frontier` column) | ON | **forward (rank 1 first)** — narrative-coherence ordering |

Notes on cells:
- `frontier × foveated = OFF` is the load-bearing change. Council seat 4's ~14% → 60%+ frontier-hit-rate jump.
- `small_moe × foveated = ON always` (not just `broad`): small models always benefit from recency on the document that holds the answer.
- `frontier × assembly_cap = max(12, classifier_cap*2)` because frontier callers have 200k+ context and the classifier caps were tuned for 4B prompt windows.
- `small_moe × candidate_order` stays reversed even though foveated is on, because the slate is the primary surface for small_moe and the prose tail is secondary.

## 5. Slate format change

Today: `"\n".join(unique_slate[:20])` at context_manager.py:2181. Entry-bounded cap silently drops the answer KV when the knowledge store surfaces >20 KVs, and breaks MoE attention-routing because flat newlines have no structural lock.

**New (small_moe branch only):**

```
<helix:slate>{"k1":"v1","k2":"v2",...}</helix:slate>
```

- Char-bounded, default budget `1500` (config key `budget.slate_char_budget`). Counted as the rendered string including braces, quotes, commas, and the wrapper tag — i.e., what the model actually sees.
- Greedy-fill ordered by per-KV score. Gemini's `_best_first` sort at context_manager.py:1121 is preserved as the source order.
- Each KV is parsed as `key=value` (existing `g.key_values` shape). On parse failure, key=`"kv{idx}"`, value=raw line.
- Keys de-duped (first-write-wins; later duplicates dropped silently).
- **Truncation rule:** when adding a KV would exceed budget, truncate that KV's *value* to fit (minimum 8 chars retained — if can't fit, drop the entry and continue). Do NOT silently stop iterating — a low-rank short KV can still fit after a high-rank long one was truncated.
- JSON encoding: `json.dumps(d, ensure_ascii=False, separators=(",", ":"))` — compact, no whitespace, MoE attention-friendly.
- `generic` and `frontier` branches: slate_format unchanged (newline-joined); for `frontier` the slate is suppressed entirely.

## 6. Cross-product with classifier (decoder_mode resolution)

Replace hard-coded `decoder_mode=` literals in `query_classifier.py:131–175` with a lookup. Classifier returns `cls` only; `resolve_decoder_mode(cls, caller_model_class)` is called by `build_context` after both signals are known.

15-cell table. **Bold cells** differ from generic. Columns are `caller_model_class`.

| classifier.cls \ class | generic | small_moe | frontier |
|---|---|---|---|
| `arithmetic` | `minimal` | **`answer_slate_only`** | **`minimal`** (same as generic — frontier handles arithmetic with tiny context) |
| `factual` | `condensed` | **`answer_slate_only`** | **`condensed`** (same — wh-question stays tight) |
| `procedural` | `full` | **`condensed_with_slate`** | **`full`** |
| `multi_hop` | `full` | **`condensed_with_slate`** (full is too long for 4B) | **`full`** |
| `default` | (None — falls back to `self._decoder_prompt`) | **`condensed_with_slate`** | (None — falls back, same as generic) |

Mode definitions (existing prompts in `helix_context/decoder_prompts.py`, plus two new):
- `answer_slate_only` (NEW for small_moe×short-answer): the JSON slate is the *entire* decoder context, no `<expressed_context>` block. ~150 tokens.
- `condensed_with_slate` (NEW): `<helix:slate>` followed by the condensed decoder prompt. Slate first so attention locks before prose.
- `minimal`, `condensed`, `full`: existing.

## 7. Default / legacy preservation

Regression table (column = today's literal in `query_classifier.py`). `generic` column of §6 must equal column 2 below cell-for-cell:

| classifier.cls | today's hard-coded `decoder_mode` (lines) | §6 `generic` cell |
|---|---|---|
| arithmetic | `"minimal"` (138) | `minimal` ✓ |
| factual | `"condensed"` (149) | `condensed` ✓ |
| procedural | `"full"` (163) | `full` ✓ |
| multi_hop | `"full"` (174) | `full` ✓ |
| default | `None` (113, _DEFAULT) | None ✓ |

Test `test_generic_branch_byte_identical_to_pre_stage5_output` (§10) is the live enforcement.

## 8. Foveated gating logic

`context_manager.py:1089` becomes:

```python
# Stage 5 (2026-05-08): frontier callers skip foveated reversal.
# Long-context attention expects narrative-coherence (rank-1-first) order;
# reversal degrades retrieval. See docs/specs/2026-05-08-stage-5-caller-model-class.md §4.
if (
    caller_model_class != "frontier"
    and budget_tier == "broad"
    and self.config.budget.foveated_enabled
    and len(candidates) > 1
):
    caps = _compute_foveated_caps(...)
    candidates = list(reversed(candidates))
    foveated_caps = list(reversed(caps))
    foveated_active = True
```

For `small_moe`, the `budget_tier == "broad"` precondition is dropped (foveated always-on). Implementation: a small `_foveated_should_run(caller_model_class, budget_tier, cfg)` helper avoids nesting two new branches in the existing block.

## 9. Bench harness change

`bench/bench_needle_1000.py` accepts `--caller-model-class {generic,small_moe,frontier}` (default `generic`). Value passed in the `/context` POST body. Run output filename includes the class: `n1000_results_{class}_{timestamp}.json`.

Expected outcome: `located_n1000` with `frontier` produces a substantially higher retrieval rate than `small_moe` on the same retrieval pipeline. This is informative, not a bug — frontier's forward-order, foveated-off, larger-cap branch is genuinely better for frontier consumers.

## 10. Test plan

`tests/test_caller_model_class.py` (mock tests, no Ollama):

1. `test_caller_model_class_default_is_generic` — POST `/context` without the field; assert request handled, response metadata echoes `"generic"`.
2. `test_frontier_skips_foveated` — monkeypatch `_compute_foveated_caps` to record calls; with `caller_model_class="frontier"` and `budget_tier="broad"`, assert zero calls AND `candidates` is in forward order at splice time.
3. `test_small_moe_emits_json_slate` — assert decoder prompt contains `<helix:slate>{` and the rendered slate parses as JSON.
4. `test_slate_char_bounded_not_entry_bounded` — feed 50 KVs, set `slate_char_budget=400`, assert >20 entries can fit if short, and total rendered length ≤ 400 + wrapper-tag chars.
5. `test_decoder_mode_lookup_table_complete` — iterate the 15-cell cross-product; assert `resolve_decoder_mode(cls, class)` returns the table value for every cell.
6. `test_generic_branch_byte_identical_to_pre_stage5_output` — run a 100-query golden set against pre-Stage-5 git ref (recorded to `tests/golden/pre_stage5_responses.jsonl`); diff every response byte-for-byte.

Plus reuse: existing foveated/slate tests run unchanged with default class.

## 11. Telemetry

- OTel span `helix.context` gains attribute `helix.caller_model_class` (string).
- New counter `helix_context_calls_by_class{class="generic|small_moe|frontier"}` incremented once per `/context` call.
- Existing latency histogram (server.py:1097) gains the `class` label so frontier/small_moe regressions are visible separately.

## 12. Acceptance criteria

- `located_n1000` retrieval@1 with `caller_model_class="frontier"` ≥ **90%** (Stages 1+2+3+4+5 stacked, frontier branch).
- `located_n1000` with `caller_model_class="small_moe"` ≥ **70% retrieval AND ≥ 50% answered** (small models extract from JSON slate where they failed on flat newlines).
- `generic` branch byte-identical to pre-Stage-5 output on the 100-query golden set (test 6).
- All existing tests green; no perf regression on `bench_needle_1000.py --caller-model-class generic` vs main (±2% p95 latency).

## 13. Out of scope

- Top-level know/miss block (Stage 6).
- Live model-class detection from User-Agent or `model` field — explicitly rejected; opt-in only.
- New decoder-mode prompts beyond `answer_slate_only` and `condensed_with_slate`.
- Per-class foveated alpha tuning (current `α` reused for `small_moe`; `frontier` doesn't apply foveated at all).
- Telemetry-driven autotuning of `slate_char_budget`.

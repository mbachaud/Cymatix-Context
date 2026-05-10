# Stage 6 — Machine-Tagged `know` / `miss` Surface for `/context`

Plan: helix-context retrieval-fix, Stage 6 of 6 (council 2026-05-08). Independent of Stages 2-4; can land any time after Stage 1.

## 1. Goals + non-goals

**Goals.** Promote retrieval outcome from prose-in-`expressed_context` and scattered-`notes[]` strings to a top-level, machine-tagged contract on every `/context` and `/context/packet` response. Frontier agents must be able to deterministically branch know vs. go without parsing English.

**Non-goals.** No LLM in the decision path. No new retrieval signals — Stage 6 only re-surfaces what Stages 1–5 already compute. No MCP tool wiring (consumers register grep/rag/web themselves). No removal of existing fields — purely additive.

## 2. Surface area

| File | Lines | Change |
|---|---|---|
| `helix_context/schemas.py` | end of file | Add `KnowBlock`, `MissBlock`; add `know`/`miss` fields to `ContextWindow.metadata`-mirror or as siblings (see §5) |
| `helix_context/context_manager.py` | 185 | New constants `_NO_MATCH_TAG_*` for tagged refusal tokens |
| `helix_context/context_manager.py` | 1869–1908 (`_build_abstain_window`) | Replace `_ABSTAIN_MARKER` byte and stamp `miss` into `metadata` |
| `helix_context/context_packet.py` | 241–272 (`_coordinate_signals`) | Expose tuple result up to caller for `KnowBlock.coordinate_confidence` |
| `helix_context/context_packet.py` | 484–506 | Keep `notes.append(...)` strings; additionally stamp packet-level `know`/`miss` |
| `helix_context/server.py` | 974–1001 (`agent.recommendation`) | Add `"escalate"`; lift `know`/`miss` from `window.metadata` to top-level response |
| `helix_context/server.py` | 1103 (`/context/packet`) | Same lift on the packet response |
| `helix_context/server.py` | 1178 (`/context/refresh-plan`) | Pass-through `miss` if present (it implies a refresh plan anyway) |
| `helix_context/know_calibration.py` | new | Loads `[know]` table from `helix.toml`; pure functions, no I/O at call time |
| `scripts/calibrate_know_confidence.py` | new | Stage §11 calibrator |
| `tests/test_know_miss_block.py` | new | Stage 6 contract tests |

## 3. New schema — `KnowBlock`

```python
# schemas.py
from typing import Literal, Optional
from pydantic import BaseModel, Field

class KnowBlock(BaseModel):
    """Top-level 'we found it' contract. Emitted iff retrieval cleared
    the FOCUSED floor AND coordinate_confidence is above floor.
    Mutually exclusive with MissBlock."""
    found: Literal[True] = True
    confidence: float = Field(ge=0.0, le=1.0)        # calibrated, see below
    top_score: float                                  # raw fused top-1
    score_gap: float                                  # raw (top1 - top2), NOT ratio
    lexical_dense_agree: bool                         # top BM25 == top dense top-K∩
    gene_id_match: Optional[str] = None               # beacon, see §8
    coordinate_confidence: float = Field(ge=0.0, le=1.0)  # promoted, see §9
```

**Calibration of `confidence`.** A logistic over four normalized features (parameters fit by §11 from `located_n1000`):

```
z   = β0
    + β1 * tanh(top_score / s_ref)        # squashes raw fused score
    + β2 * tanh(score_gap   / g_ref)      # raw subtraction, not ratio
    + β3 * (1.0 if lexical_dense_agree else 0.0)
    + β4 * coordinate_confidence          # already in [0,1]
confidence = 1.0 / (1.0 + exp(-z))
```

`s_ref`, `g_ref`, and the four `β` coefficients live in `[know]` of `helix.toml`. Defaults shipped in code (cold-start) until calibration runs: `β = (-2.0, +2.0, +1.5, +0.7, +1.8)`, `s_ref = 1.0`, `g_ref = 0.5`. These defaults intentionally produce `confidence ≈ 0.5` at boundary cases — a `know` block is only emitted when `confidence >= know.emit_floor` (default `0.55`); below that we fall through to `MissBlock(reason="sparse")`.

**Stage 7 extension (forthcoming).** Stage 7 adds `freshness_min` (the worst per-gene decay score across top-K) as a fifth feature β5 with default ≈ +1.5. The logistic becomes 5-feature; `know` cannot be emitted if the top-1 gene's source is stale. This is required because the current `_compute_health` averages decay scores ([context_manager.py:2315](../../helix_context/context_manager.py#L2315)), masking a stale needle when fresh padding accompanies it. Stage 6 ships with the 4-feature logistic; Stage 7 retunes via the same calibration script.

## 4. New schema — `MissBlock`

```python
class MissBlock(BaseModel):
    """Top-level 'we did NOT find it' contract. Emitted on ABSTAIN,
    on denatured genome, on sub-floor sparse retrieval, or on a
    promoter-tag whiff. Mutually exclusive with KnowBlock."""
    miss: Literal[True] = True
    reason: Literal["abstain", "denatured", "sparse", "no_promoter_match"]
    top_score: float
    ratio: float                                      # top/2nd (matches existing metadata.ratio)
    escalate_to: list[Literal["grep", "rag", "web", "ask_human"]]
    do_not_answer_from_genome: Literal[True] = True
```

**`escalate_to` population (rule-based, ordered).** Decided in `_pick_escalation(query, reason)`; first matching rule wins, results deduped:

1. **Code-shaped query** (matches `r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]"` OR contains `def `, `class `, `import `, `function ` OR ≥1 `path_token` resembling a filename `.py|.ts|.go|.rs|.md`) → `["grep", "rag"]`.
2. **Entity-shaped query** (extract_query_signals returns ≥1 entity AND no code shape) and `reason == "no_promoter_match"` → `["rag", "web"]`.
3. **Reason `denatured`** (genome inconsistent) → `["grep", "ask_human"]` — don't trust RAG over a denatured corpus, but local grep still works.
4. **Reason `abstain` and short query (≤3 tokens)** → `["ask_human", "rag"]` — ambiguity.
5. **Default fallback** → `["rag"]`.

`do_not_answer_from_genome` is a `Literal[True]` so the field's *presence* is the signal — agents don't compare strings.

**Stage 7 extension (forthcoming).** Stage 7 adds three new reasons to the `Literal[...]`: `"stale"` (gene retrieved successfully but source mtime > last_verified_at), `"cold"` (heterochromatin match — gene exists but isn't safe to act on without re-warming), `"superseded"` (claims_graph has a successor edge from a newer gene). All three use a different routing — `agent.recommendation="refresh"` (new sixth value), and a new required field `refresh_targets: list[str]` (file paths or URLs to re-fetch before retrying). Stage 6 ships with the four reasons above; Stage 7 extends without breaking back-compat (consumers parse the union, new reasons fall through unknown→escalate for legacy clients).

## 5. ContextResponse top-level addition

`/context` and `/context/packet` JSON responses gain **exactly one** of two sibling keys at the top level (alongside `expressed_context`, `agent`, `metadata`):

```json
{ "know": { "...": "..." } }    // or
{ "miss": { "...": "..." } }
```

**Discriminator (server-side, single source of truth).** Computed once in a new helper `helix_context/know_decision.py::decide_know_or_miss(window, packet_signals) -> KnowBlock | MissBlock`:

- `window.context_health.status == "abstain"` → `MissBlock(reason="abstain")`.
- `window.context_health.status == "denatured"` → `MissBlock(reason="denatured")`.
- `genes_expressed == 0` AND not abstain → `MissBlock(reason="no_promoter_match")`.
- Else compute `confidence`. If `confidence < know.emit_floor` → `MissBlock(reason="sparse")`. Else → `KnowBlock(...)`.

Server lifts the result to the top of the response dict. Schema invariant enforced by a `model_validator` on a thin `ContextResponseEnvelope` wrapper used by both routes: exactly one of `know`/`miss` is non-null.

## 6. `expressed_context` byte change

Replace `_ABSTAIN_MARKER` (currently `"(no relevant context found in genome)"` at `context_manager.py:185`) with three sibling tagged tokens, selected by reason:

```
<helix:no_match reason="abstain"          do_not_answer="true"/>
<helix:no_match reason="denatured"        do_not_answer="true"/>
<helix:no_match reason="sparse"           do_not_answer="true"/>
<helix:no_match reason="no_promoter_match" do_not_answer="true"/>
```

Exact format: lowercase tag `helix:no_match`, attributes in fixed order (`reason` then `do_not_answer`), self-closing `/>`, no whitespace inside. Constants in `context_manager.py`: `_NO_MATCH_TAG = '<helix:no_match reason="{reason}" do_not_answer="true"/>'`. `_ABSTAIN_MARKER` is **kept** as a deprecated alias re-pointed at `_NO_MATCH_TAG.format(reason="abstain")` for one release; existing tests assert via the constant, so they migrate transparently.

## 7. `agent.recommendation` extension

Add `"escalate"` to the existing set (`server.py:975–986`). Mapping additions:

- `health.status == "abstain"` → `recommendation = "escalate"`, hint = `"Genome has no usable signal for this query. Use external retrieval (grep / RAG / web)."`
- `health.status == "denatured"` keeps `"reread_raw"` (unchanged).
- New: when `miss` block present AND query is code-shaped → `recommendation = "escalate"`, `escalate_to` mirrored from `MissBlock`.

The existing four values (`trust`, `verify`, `refresh`, `reread_raw`) remain valid; `escalate` is the fifth, and is the *only* value that may co-exist with a `miss` block.

## 8. `gene_id_match` beacon rules

Populate `KnowBlock.gene_id_match` with the matched token (string), or leave `None`. Implemented in `context_packet.py::_gene_id_beacon(query, top_gene)`:

- Tokenize the query with `extract_query_signals` (existing).
- For the top-1 gene only:
  - Compute `file_tokens(top_gene.source_id)` and `path_tokens(top_gene.source_id)` (both already exist).
  - If any query token is in `file_tokens(...)` → set `gene_id_match` to that token. Filename match wins.
  - Else if any query token is in `path_tokens(...)` AND that token has length ≥ 4 (avoid `src`, `lib`, `app`) → set `gene_id_match` to that token.
- **Exact, case-insensitive equality only.** No prefix match, no substring match, no edit distance, no synonym expansion.

False-positive cost > false-negative cost: a wrong beacon causes the frontier model to lock in a wrong answer; a missing beacon merely lowers `confidence` and the agent still gets the gene.

## 9. `coordinate_confidence` promotion

Today `context_packet.py:484–506` appends `f"coordinate_confidence={x:.2f}..."` strings to `packet.notes`. Migration:

1. Keep all existing `notes.append(...)` calls verbatim — humans read them.
2. Add `packet._coordinate_confidence: float` (set on the packet by the builder) and `packet._file_coverage: float`. Underscore-prefixed because they are computation byproducts, not part of the wire schema; the wire schema gets them via `KnowBlock`.
3. The know/miss decider reads these from the packet (or, for `/context`, recomputes them from `window.expressed_gene_ids` via the same `_coordinate_signals`).

## 10. Test plan (`tests/test_know_miss_block.py`)

- `test_know_block_emitted_when_found_and_high_confidence` — seed planted gene, query its filename, assert `know.found=True`, `know.confidence > 0.7`, `know.gene_id_match == "<filename_token>"`, `miss is None`.
- `test_miss_block_emitted_on_abstain` — abstain manager fixture, assert `miss.reason == "abstain"`, `miss.do_not_answer_from_genome is True`, `know is None`, `escalate_to` non-empty.
- `test_gene_id_match_beacon_only_on_exact_filename_match` — three subcases: (a) exact filename token match → set; (b) substring `"manage"` vs file `"context_manager.py"` → `None`; (c) folder-only match `"src"` → `None` (length filter).
- `test_expressed_context_has_no_match_tag_on_miss` — assert `expressed_context == '<helix:no_match reason="abstain" do_not_answer="true"/>'` exactly.
- `test_agent_recommendation_escalate_on_code_shaped_miss` — abstain with query `"def parse_promoter"`, assert `agent.recommendation == "escalate"` and `miss.escalate_to == ["grep", "rag"]`.
- `test_no_block_when_neither_found_nor_abstain` — **edge case spec.** Score above ABSTAIN floor but `confidence < know.emit_floor` (e.g., low `coordinate_confidence`, no agreement, score just clears floor). The discriminator emits `MissBlock(reason="sparse")` — never neither, never both. Test asserts exactly that: response has `miss`, not `know`, and `expressed_context` carries the populated genes (because we did retrieve, just weakly) plus the `<helix:no_match reason="sparse"/>` token *appended* (not replacing) so the agent still sees the weak hits but is told to escalate.

## 11. Confidence calibration

`scripts/calibrate_know_confidence.py`:

1. Load `located_n1000` ground truth (Stage 1 bench output).
2. For each row, invoke `/context` against the locked-in fixture genome and record `(top_score, score_gap, lexical_dense_agree, file_cov, planted_gene_id == retrieved_top1)`.
3. Fit `sklearn.linear_model.LogisticRegression(penalty="l2", C=1.0)` on the four features → `(planted == top1)` label. Extract `β` and intercept.
4. Pick `s_ref = median(top_score)`, `g_ref = median(score_gap)` from the calibration set so `tanh(...)` saturates around the typical scale.
5. Pick `know.emit_floor` as the precision-95 operating point on the held-out 20% split.
6. Write to `helix.toml`:

```toml
[know]
emit_floor = 0.55
s_ref = 1.0
g_ref = 0.5
betas = [-2.0, 2.0, 1.5, 0.7, 1.8]   # [intercept, top, gap, agree, coord]
calibrated_at = "2026-05-08T..."
calibrated_on_n = 800
```

Loader in `helix_context/know_calibration.py`; pure functions; falls back to defaults on missing/malformed table with a `log.warning`.

## 12. Frontier agent prompt-template (load-bearing)

Without this, a frontier model will paper over `do_not_answer_from_genome=true` by falling back to its training prior. The contract is load-bearing only if the agent's system prompt teaches it to honor the tag. Recommended fragment, shipped in `docs/agent-sdk-fragment.md` and importable as a constant `helix_context.agent_prompt.HELIX_NO_MATCH_FRAGMENT`:

```
HELIX KNOW/MISS CONTRACT
========================
The retrieval layer (helix-context) returns one of two structured blocks
on every /context call:

  • know { found, confidence, gene_id_match, ... } — retrieval succeeded;
    the expressed_context bytes ARE grounded; you may answer from them.

  • miss { reason, escalate_to, do_not_answer_from_genome:true, ... } —
    retrieval did NOT find it. The expressed_context will contain a
    <helix:no_match reason="..." do_not_answer="true"/> tag. When you
    see this tag OR a `miss` block:

      1. DO NOT answer from the genome content.
      2. DO NOT fall back to your training prior to fabricate an answer.
      3. Emit a tool call from `escalate_to` (in order):
           - "grep"      → local repo grep / file_search MCP tool
           - "rag"       → external RAG / docs index
           - "web"       → web search
           - "ask_human" → ask the operator
      4. Only after the escalation tool returns, compose your reply.

If both `know` and `miss` are absent (legacy server), treat the response
as `know` with confidence=0.5 — but log a warning, your server is stale.

Compliance is non-negotiable: confabulating an answer when miss is
present is a contract violation, scored as a hard failure in eval.
```

Deliberately uses imperative, numbered, short sentences. The "scored as a hard failure" sentence is the highest-leverage line; small/medium frontier models comply more readily when given an explicit eval framing.

## 13. Acceptance criteria

On `located_n1000` with Stages 1–5 stacked:

- (a) ≥ 99% of retrieval-success rows emit a `know` block with `confidence > 0.7`.
- (b) 100% of retrieval-miss rows emit a `miss` block with `len(escalate_to) >= 1`.
- (c) 0 rows emit both blocks. Enforced by the response-envelope validator at runtime (raises 500 on violation; integration tests assert it never raises).
- (d) Offline frontier-agent eval (mocked tool layer): with §12 system-prompt fragment, the agent issues a tool call from `escalate_to` instead of answering on ≥ 95% of `miss` rows. Eval harness in `scripts/eval_agent_compliance.py`; fails the build below 95%.

## 14. Out of scope

Actual MCP tool wiring for `grep`/`rag`/`web`/`ask_human` — consumers register their own tools. Helix only signals which class of tool to invoke. Also out: backfilling the contract onto `/v1/chat/completions` (the OpenAI-compat proxy is a separate surface; that's a future stage).

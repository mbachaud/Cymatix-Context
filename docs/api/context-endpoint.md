# `/context` endpoint reference

This document is the schema-level reference for the `/context` family of
endpoints exposed by `helix-context`. It supersedes the two-paragraph
treatment in [`docs/api/endpoints.md`](endpoints.md) and is the canonical
contract for frontier-agent and small-MoE callers integrating against the
post-2026-05-10 7-stage retrieval-fix surface.

All file references link to the helix-context repository. Pydantic models
are at [`helix_context/schemas.py`](../../helix_context/schemas.py); route
handlers are at [`helix_context/server.py`](../../helix_context/server.py).

---

## 1. Overview

`POST /context` is the primary read endpoint for an LLM agent. The agent
sends a query string; helix-context runs its retrieval pipeline against the
local genome (SQLite) and returns a structured envelope containing either
(a) a `know` block with grounded `expressed_context` bytes the agent may
answer from, or (b) a `miss` block whose `reason` field tells the agent how
to recover (escalate to a tool, refresh a stale source, ask a human).

The contract is the **know-vs-go** split: every response has exactly one of
the two top-level keys non-null. There is no third "maybe" branch — the
discriminator at
[`helix_context/know_decision.py:304`](../../helix_context/know_decision.py#L304)
collapses every retrieval outcome onto one of the two blocks. Frontier
agents are expected to branch on the structured tag instead of parsing
prose out of `expressed_context`.

Callers:

- **MCP hosts** (Claude Code, Cursor, Continue) using `mcp__helix-context__helix_context`.
- **Frontier-agent SDKs** (Claude Opus, GPT-5, Gemini 3 Pro) that want
  retrieval-augmented context with a load-bearing refusal contract.
- **Small-MoE proxies** (qwen3:4b, gemma4:e4b) that need a JSON answer slate
  the model's attention can lock onto.
- **Bench harnesses** running `bench_needle_1000.py` with `--axis located`.

The endpoint is LLM-free: no model is invoked inside the retrieval path.
All structured signals (`confidence`, `coordinate_confidence`,
`freshness_min`, `lexical_dense_agree`) are computed by deterministic
SQL-and-numpy code from the post-fusion score map.

---

## 2. Request body

`POST /context` accepts a JSON object. All fields are optional except
`query`. Fields marked **(verified)** are read by
[`server.py:1010-1170`](../../helix_context/server.py#L1010);
fields documented from `_request_read_only` are at
[`server.py:1000-1008`](../../helix_context/server.py#L1000).

| Field | Type | Default | Semantics |
|---|---|---|---|
| `query` | `str` | — | **Required.** Natural-language query or locator. Empty/whitespace-only string returns 400 (`server.py:1082`). |
| `session_id` | `str \| null` | synthesized | Session attribution for CWoLa logging. When `null` and `synthetic_session_enabled` is on, the server synthesizes `"syn_<sha1(client_ip:bucket_ts)>[:12]"` (`server.py:1056-1067`). |
| `party_id` | `str \| null` | `config.session.default_party_id` | Trust identity. Defaults to the configured party when `null`. |
| `caller_model_class` | `"generic" \| "small_moe" \| "frontier"` | `"generic"` | **Stage 5.** Render-branch selector. Unknown values return 400 (`server.py:1135-1142`). The `generic` branch is regression-locked byte-identical to pre-Stage-5 output. See §7 for the behavior matrix. Wire enum at [`schemas.py:692-696`](../../helix_context/schemas.py#L692). |
| `clean` | `bool` | `false` | **Stage 1.** When true: (a) implies `read_only=true` (`server.py:1008`); (b) calls `helix.reset_session_state()` to clear per-session caches before the request runs (`server.py:1075-1079`). Used by synthetic benches to isolate from prior state. |
| `read_only` | `bool \| null` | `null` | Explicit override. When non-null, takes precedence over `clean`'s implicit value (`_request_read_only` at `server.py:1000`). When true: no genome learning, no `touch_genes`, no `link_coactivated`, no harmonic/relation writes. Mtime cache may still update (in-memory; not a genome write). |
| `response_mode` | `"continue" \| "packet"` | `"continue"` | When `"packet"`, the route delegates to `build_context_packet` and returns a `ContextPacket`-shaped payload instead of the Continue-compatible envelope (`server.py:1090-1111`). Other values return 400. |
| `format` | `str \| null` | — | Legacy alias for `response_mode`. Ignored when `response_mode` is set; otherwise its value is used (`server.py:1019`). |
| `include_cold` | `bool \| null` | (config flag) | Cold-tier override. `null` defers to config (`config.budget.cold_tier_enabled`). `true` forces cold-tier ON for this request; `false` forces it OFF (`server.py:1027-1029`). |
| `prompt_tokens` | `int \| null` | `null` | Budget-zone hint. The `/context` route has no `messages[]`, so callers must supply the prompt-token count explicitly to enable zone-cap behavior. Missing => treated as clean/no-cap (`server.py:1147-1152`). |
| `decoder_mode` | `"full" \| "condensed" \| "minimal" \| "none" \| null` | resolved | Per-request decoder override. Other values are ignored and the classifier-resolved mode wins (`server.py:1116-1120`). |
| `verbose` | `bool` | `false` | When true, citations gain `domains` / `entities` and `agent` gains `tier_contributions` / `tier_totals` (`server.py:1264-1273`, `1358-1379`). |
| `session_context` | `dict \| null` | `null` | Editor context: `{"active_project": str, "active_files": list[str], "active_projects": list[str]}`. Plumbed through to the path_key_index tier so PKI can fire on `(project, key)` pairs even when the query string itself is short. |
| `task_type` | `str` | `"explain"` | Only consumed when `response_mode == "packet"`; selects the packet builder's task profile (`server.py:1103`). |
| `max_genes` | `int` | `config.budget.max_genes_per_turn` | Only consumed when `response_mode == "packet"`. Clamped to `[1, 32]` (`server.py:1095`). |
| `ignore_delivered` | `bool` | `false` | Bypasses the session working-set elision so already-delivered genes can re-fire (used by benches and smoke tests, `server.py:1157`). |

### 2.1 Example request

```json
{
  "query": "What is the cold_start_threshold value in helix-context/helix_context/config.py?",
  "caller_model_class": "frontier",
  "session_id": "taude-2026-05-10-001",
  "party_id": "max@local",
  "clean": false,
  "prompt_tokens": 18000,
  "session_context": {
    "active_project": "helix-context",
    "active_files": ["helix_context/config.py", "helix.toml"]
  }
}
```

---

## 3. Response — `know` branch

When the discriminator returns a `KnowBlock`, the response top-level is:

```json
{
  "know": { ... },
  "name": "Helix Genome Context",
  "description": "12 genes expressed, 3.4x compression, health=aligned (Δε=0.91)",
  "content": "<expressed_context>...</expressed_context>",
  "context_health": { ... },
  "agent": { ... }
}
```

`miss` is **not** present in this branch (it is omitted, not `null`). The
envelope validator at
[`schemas.py:665-674`](../../helix_context/schemas.py#L665) raises
`ValueError` if both keys are set or both omitted.

### 3.1 `know: KnowBlock`

Defined at [`schemas.py:490-518`](../../helix_context/schemas.py#L490).
`extra="forbid"` — additional keys are a hard error.

| Field | Type | Constraint | Source |
|---|---|---|---|
| `found` | `Literal[True]` | always `true` | discriminator |
| `confidence` | `float` | `[0.0, 1.0]` | 5-feature logistic, see §3.1.1 |
| `top_score` | `float` | unconstrained | rank-1 fused retrieval score |
| `score_gap` | `float` | unconstrained | `top1 - top2` (raw subtraction, NOT ratio) |
| `lexical_dense_agree` | `bool` | — | `True` if lexical-cluster top-K and dense-cluster top-K share at least one gene_id (k=3); see [`know_decision.py:237-297`](../../helix_context/know_decision.py#L237) |
| `gene_id_match` | `str \| null` | — | Beacon: query token that exactly (case-insensitive) matches a top-1 file or path token. `null` when no match. See §3.1.2. |
| `coordinate_confidence` | `float` | `[0.0, 1.0]` | Blend of folder + file-grain query/source agreement; computed by `context_packet._coordinate_confidence` |
| `soft_stale` | `bool` | default `false` | **Stage 7.** `True` when top-1 is fresh enough to act on, but `freshness_min < 0.5` indicates lower-ranked supporting genes are stale. Drives `agent.recommendation = "refresh"` even though the agent may answer from the genome. |

#### 3.1.1 Confidence formula (Stage 6 + Stage 7)

The 5-feature logistic at
[`know_calibration.py`](../../helix_context/know_calibration.py)
(plumbed from `decide_know_or_miss` at
[`know_decision.py:433`](../../helix_context/know_decision.py#L433)):

```
z = β0
  + β1 * tanh(top_score / s_ref)
  + β2 * tanh(score_gap / g_ref)
  + β3 * (1.0 if lexical_dense_agree else 0.0)
  + β4 * coordinate_confidence
  + β5 * freshness_min                       # Stage 7
confidence = 1.0 / (1.0 + exp(-z))
```

Cold-start defaults shipped in code: `β = (-2.0, +2.0, +1.5, +0.7, +1.8, +1.5)`,
`s_ref = 1.0`, `g_ref = 0.5`. A `KnowBlock` is only emitted when
`confidence >= know.emit_floor` (default `0.55`); below that the
discriminator falls through to `MissBlock(reason="sparse")`.

`freshness_min` falls back to `decay_score` when `last_verified_at IS NULL`
on legacy rows. When `freshness_min` is `None` (no candidates), the term
is treated as neutral (β5 contribution zero).

#### 3.1.2 `gene_id_match` beacon rules

Implemented at
[`know_decision.py:157-211`](../../helix_context/know_decision.py#L157).
Filename match wins over path match. Path-token match requires the
matched token's length `>= 4` AND it must not be in the
`{"src","lib","app","bin","var","tmp","out"}` blocklist. Equality is
exact and case-insensitive only — no prefix, no substring, no edit
distance, no synonym expansion. Asymmetric cost: a wrong beacon makes
the frontier model lock in a wrong answer; a missing beacon merely
lowers `confidence`.

#### 3.1.3 Example `know` payload

```json
{
  "know": {
    "found": true,
    "confidence": 0.83,
    "top_score": 0.92,
    "score_gap": 0.41,
    "lexical_dense_agree": true,
    "gene_id_match": "config.py",
    "coordinate_confidence": 0.78,
    "soft_stale": false
  }
}
```

---

## 4. Response — `miss` branch

When the discriminator returns a `MissBlock`, the response top-level is:

```json
{
  "miss": { ... },
  "name": "Helix Genome Context",
  "description": "0 genes expressed, 0.0x compression, health=abstain (Δε=0.00)",
  "content": "<expressed_context><helix:no_match reason=\"abstain\" do_not_answer=\"true\"/></expressed_context>",
  "context_health": { ... },
  "agent": { ... }
}
```

### 4.1 `miss: MissBlock`

Defined at [`schemas.py:521-642`](../../helix_context/schemas.py#L521).
`extra="forbid"`.

| Field | Type | Constraint | Source |
|---|---|---|---|
| `miss` | `Literal[True]` | always `true` | discriminator |
| `reason` | `str` | one of `MISS_REASONS` (see §4.2) | `_validate_reason_and_escalate` model_validator |
| `top_score` | `float` | — | rank-1 fused retrieval score (may be 0.0 on `no_promoter_match`) |
| `ratio` | `float` | — | `top1 / top2` (matches existing `metadata.ratio`) |
| `escalate_to` | `list[str]` | each in `ESCALATE_TARGETS` | populated for escalate-class reasons (see §4.2); empty for refresh-class |
| `refresh_targets` | `list[str]` | — | populated for refresh-class reasons; empty for escalate-class |
| `do_not_answer_from_genome` | `Literal[True]` | always `true` | load-bearing contract bit; agents MUST honor it |

### 4.2 Reason vocabulary

`MISS_REASONS` is a `tuple[str, ...]` at
[`schemas.py:457-466`](../../helix_context/schemas.py#L457).
Order matters for clients that index into the tuple:

```python
MISS_REASONS = (
    "abstain",            # Stage 6 — escalate
    "denatured",          # Stage 6 — escalate
    "sparse",             # Stage 6 — escalate
    "no_promoter_match",  # Stage 6 — escalate
    "stale",              # Stage 7 — refresh
    "cold",               # Stage 7 — refresh
    "superseded",         # Stage 7 — refresh
)
```

`ESCALATE_TARGETS` is a `tuple[str, ...]` at
[`schemas.py:478-483`](../../helix_context/schemas.py#L478):

```python
ESCALATE_TARGETS = ("grep", "rag", "web", "ask_human")
```

The model validator at
[`schemas.py:557-601`](../../helix_context/schemas.py#L557) enforces:

- `reason in {"stale","cold","superseded"}` ⇒
  `len(refresh_targets) >= 1` AND `escalate_to == []`.
- `reason in {"abstain","denatured","sparse","no_promoter_match"}` ⇒
  `refresh_targets == []` AND `len(escalate_to) >= 1`.

A construction violating either invariant raises `ValueError` at
pydantic-validate time.

### 4.3 Reason → behavior mapping

| `reason` | Class | Discriminator branch | `escalate_to` populated by | `refresh_targets` source | `agent.recommendation` |
|---|---|---|---|---|---|
| `abstain` | escalate | `health.status == "abstain"` | `_pick_escalation(query, "abstain")` | `[]` | `"escalate"` |
| `denatured` | escalate | `health.status == "denatured"` | `_pick_escalation(query, "denatured")` | `[]` | `"escalate"` |
| `no_promoter_match` | escalate | `genes_expressed == 0` and not abstain | `_pick_escalation(query, "no_promoter_match")` | `[]` | `"escalate"` |
| `sparse` | escalate | `confidence < emit_floor` (and no cold-tier hits) | `_pick_escalation(query, "sparse")` | `[]` | `"escalate"` |
| `stale` | refresh | top-1 mtime > `last_verified_at` | `[]` | `[top_gene.source_id]` | `"refresh"` |
| `cold` | refresh | sparse fallthrough but cold-tier peek surfaced hits | `[]` | `[g.source_path or g.source_id for g in cold_hits]` | `"refresh"` |
| `superseded` | refresh | top-1 has a successor row via `genes.supersedes` | `[]` | `[successor.source_id]` | `"refresh"` |

### 4.4 `escalate_to` ordering rules

`_pick_escalation` at
[`know_decision.py:96-139`](../../helix_context/know_decision.py#L96).
First matching rule wins; results deduped while preserving order:

1. **Code-shaped query** (matches `[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]` OR
   contains `def`/`class`/`import`/`function`/`fn`/`let`/`var`/`const`
   keyword OR has a filename extension token like `.py|.ts|.go|.rs|.md|...`)
   → `["grep", "rag"]`.
2. **Entity-shape + `no_promoter_match`** (≥1 entity, no code shape) →
   `["rag", "web"]`.
3. **`denatured` corpus** → `["grep", "ask_human"]`.
4. **`abstain` + short query (≤3 tokens)** → `["ask_human", "rag"]`.
5. **Default fallback** → `["rag"]`.

The list always contains at least one tool — escalate-class reasons with
empty `escalate_to` would fail the model validator.

### 4.5 Example `miss` payloads

Escalate-class (abstain on a code-shaped query):

```json
{
  "miss": {
    "miss": true,
    "reason": "abstain",
    "top_score": 0.0,
    "ratio": 0.0,
    "escalate_to": ["grep", "rag"],
    "refresh_targets": [],
    "do_not_answer_from_genome": true
  }
}
```

Refresh-class (stale top-1):

```json
{
  "miss": {
    "miss": true,
    "reason": "stale",
    "top_score": 0.83,
    "ratio": 1.42,
    "escalate_to": [],
    "refresh_targets": ["F:/Projects/helix-context/helix_context/config.py"],
    "do_not_answer_from_genome": true
  }
}
```

Refresh-class (cold-tier fallback):

```json
{
  "miss": {
    "miss": true,
    "reason": "cold",
    "top_score": 0.42,
    "ratio": 1.0,
    "escalate_to": [],
    "refresh_targets": [
      "F:/Projects/helix-context/helix_context/freshness.py"
    ],
    "do_not_answer_from_genome": true
  }
}
```

---

## 5. Mutual exclusivity invariant

Exactly one of `know` and `miss` is non-null on every well-formed
response. The invariant is enforced by the
`ContextResponseEnvelope._exactly_one` model validator at
[`schemas.py:665-674`](../../helix_context/schemas.py#L665):

```python
@model_validator(mode="after")
def _exactly_one(self) -> "ContextResponseEnvelope":
    if (self.know is None) == (self.miss is None):
        raise ValueError(
            "ContextResponseEnvelope: exactly one of know/miss must "
            "be set (got "
            f"know={'set' if self.know is not None else 'None'}, "
            f"miss={'set' if self.miss is not None else 'None'})"
        )
    return self
```

A construction with both blocks present or both absent raises
`ValueError` at pydantic-validate time, which the route surfaces as a
500 (the test suite asserts it never fires under correct flow). Clients
that read both keys defensively MUST treat "both set" or "both absent"
as a server bug, not as a fall-through case.

---

## 6. `expressed_context` token taxonomy

The `content` field of the response contains the assembled
`expressed_context` string — typically wrapped in
`<expressed_context>...</expressed_context>` tags. Three special
inline tokens may appear:

### 6.1 `<helix:no_match/>` — Stage 6 miss token

Self-closing tag injected by `_no_match_token` at
[`context_manager.py:221-236`](../../helix_context/context_manager.py#L221).
Lowercase tag name, attributes in fixed order (`reason` then
`do_not_answer`), no whitespace inside the tag, `do_not_answer="true"`
literal:

```
<helix:no_match reason="abstain"           do_not_answer="true"/>
<helix:no_match reason="denatured"         do_not_answer="true"/>
<helix:no_match reason="sparse"            do_not_answer="true"/>
<helix:no_match reason="no_promoter_match" do_not_answer="true"/>
<helix:no_match reason="stale"             do_not_answer="true"/>
<helix:no_match reason="cold"              do_not_answer="true"/>
<helix:no_match reason="superseded"        do_not_answer="true"/>
```

**Implementation note:** the `_no_match_token` helper currently only
formats the four Stage-6 reasons (abstain, denatured, sparse,
no_promoter_match); an unknown reason falls back to the abstain form
(`context_manager.py:233-236`). The Stage-7 reasons (`stale`, `cold`,
`superseded`) are surfaced via `MissBlock.reason` and
`agent.recommendation = "refresh"` rather than a distinct
`expressed_context` byte. Clients should branch on the structured
`miss.reason` field, not on the `<helix:no_match/>` reason attribute.

### 6.2 `<helix:slate>` — Stage 5 small-MoE answer slate

Injected by `_render_small_moe_slate` at
[`context_manager.py:288-342`](../../helix_context/context_manager.py#L288).
Char-bounded JSON KV pack, default budget 1500 chars (config key
`budget.slate_char_budget`). Compact JSON (`separators=(",", ":")`,
`ensure_ascii=False`); keys deduped first-write-wins:

```
<helix:slate>{"port":"11437","model":"qwen3:4b","cold_start_threshold":"0.62"}</helix:slate>
```

Empty form when no KV fits the budget:

```
<helix:slate>{}</helix:slate>
```

The slate is emitted only when `caller_model_class == "small_moe"` (or
the legacy MoE flag fires); for `frontier` callers the slate is
suppressed entirely (see §7).

### 6.3 Decoder-mode templates that embed the slate

When `decoder_mode == "answer_slate_only"` (small_moe × arithmetic /
factual) or `decoder_mode == "condensed_with_slate"` (small_moe ×
procedural / multi_hop / default), the rendered prompt template
substitutes `{answer_slate}` with the rendered `<helix:slate>` string
and emits the result — there is no distinct `<helix:answer_slate>` tag.
Templates at
[`context_manager.py:176-192`](../../helix_context/context_manager.py#L176).

### 6.4 Per-gene legibility headers

When legibility headers are enabled, each delivered gene is preceded by
a single bracketed line emitted by `format_gene_header` at
[`legibility.py:122-157`](../../helix_context/legibility.py#L122):

```
[gene=<short_id> <symbol> fired=<tier1>:<score1>,... <chars→compressed>]
```

`<symbol>` is one of `◆ / ◇ / ·` (z-normalized confidence). Tier slice
caps at top-3 by default. Size form is `<raw>→<compressed>c` when the
splice trimmed the gene, else `<n>c`. Headers are suppressed for
`small_moe` callers (cost > benefit at 4B params; see Stage 5 spec §4).

---

## 7. `caller_model_class` rendering matrix

Ported from Stage 5 spec §4. Rows = `caller_model_class`, columns =
behavior knob. The `generic` row equals pre-Stage-5 hard-coded behavior
cell-for-cell (regression-locked by
`test_generic_branch_byte_identical_to_pre_stage5_output`).

| Knob | `generic` | `small_moe` | `frontier` |
|---|---|---|---|
| **foveated** | ON if `budget_tier=="broad"` AND `foveated_enabled` | ON always (regardless of `budget_tier`) | **OFF** (skip reversal entirely) |
| **slate emitted** | iff `_should_use_slate()` (legacy MoE flag OR small downstream model) | always ON | OFF |
| **slate format** | `\n`-joined raw KV lines | **JSON object** `<helix:slate>{...}</helix:slate>` | n/a |
| **slate bound** | 20 entries (status quo) | **1500 chars** (config: `slate_char_budget`) | n/a |
| **assembly cap** | `classifier.assembly_max_genes_cap` | `min(classifier_cap, 4)` | `max(12, classifier_cap*2)` |
| **decoder mode default** | per classifier (§8 `generic` column) | per classifier (§8 `small_moe` column) | per classifier (§8 `frontier` column) |
| **legibility headers** | ON when `legibility_on` | OFF (suppressed) | ON |
| **candidate order** | reversed if foveated active, else forward | reversed (small models benefit from recency) | **forward (rank 1 first)** — narrative coherence |

Rationale for the load-bearing cells:

- `frontier × foveated = OFF`: long-context attention expects rank-1-first
  narrative-coherence ordering; reversal degrades retrieval. Council
  seat 4's frontier-hit-rate jump from ~14% to 60%+ depends on this.
- `frontier × assembly_cap = max(12, classifier_cap*2)`: frontier
  callers have 200k+ context and the classifier caps were tuned for
  4B-parameter prompt windows.
- `small_moe × foveated = ON always` (not just `broad`): small models
  always benefit from recency on the gene that holds the answer.
- `small_moe × slate_format = JSON`: flat newlines have no structural
  lock for MoE attention routing; JSON braces give the model a fixed
  attention sink.

---

## 8. `decoder_mode` resolution table

Ported from Stage 5 spec §6. Resolved by
`query_classifier.resolve_decoder_mode(cls, caller_model_class)` after
both signals are known. Cells in **bold** differ from the `generic`
column.

| `classifier.cls` \ class | `generic` | `small_moe` | `frontier` |
|---|---|---|---|
| `arithmetic` | `minimal` | **`answer_slate_only`** | `minimal` |
| `factual` | `condensed` | **`answer_slate_only`** | `condensed` |
| `procedural` | `full` | **`condensed_with_slate`** | `full` |
| `multi_hop` | `full` | **`condensed_with_slate`** | `full` |
| `default` | `None` (falls back to `self._decoder_prompt`) | **`condensed_with_slate`** | `None` (falls back, same as `generic`) |

Mode definitions (templates at
[`context_manager.py:176-202`](../../helix_context/context_manager.py#L176)):

- `minimal`, `condensed`, `full`, `none`, `moe`: existing pre-Stage-5
  templates.
- `answer_slate_only` (NEW for small_moe × short-answer): the JSON
  slate is the **entire** decoder context, no `<expressed_context>`
  block. ~150 tokens.
- `condensed_with_slate` (NEW): `<helix:slate>` first (so attention
  locks before prose), then the condensed decoder prompt.

---

## 9. Other endpoints

Detailed only at the request-shape and pointer level; for response
schemas, follow the route handler line numbers.

### 9.1 `POST /context/packet` — agent-safe evidence packet

Handler: [`server.py:1461-1544`](../../helix_context/server.py#L1461).
Request fields: `query` (required), `task_type` (default `"explain"`),
`max_genes` (default 8, clamped to `[1, 32]`), `clean`, `read_only`,
`include_raw` (default `false` — when true, item content is the full
gene body instead of the 280-char ribosome-compressed thumbnail),
`max_item_chars` (per-item cap when `include_raw`).
Response is a `ContextPacket` dict (see
[`schemas.py:245-272`](../../helix_context/schemas.py#L245)) with
`know` / `miss` lifted to the top of the dict, plus
`response_mode: "packet"`. Includes `verified`, `stale_risk`,
`contradictions`, `refresh_targets` (as `RefreshTarget` rows),
`coordinate_confidence`, `file_coverage`, and `notes`.

### 9.2 `POST /context/refresh-plan` — refresh-only convenience

Handler: [`server.py:1546-1593`](../../helix_context/server.py#L1546).
Thin wrapper over `get_refresh_targets`; returns
`{"query", "task_type", "refresh_targets": [RefreshTarget...],
"response_mode": "refresh_plan"}`. Useful when the caller already has
the content cached and only needs to decide which sources to reread.
Honors `clean` / `read_only` identically to `/context`.

### 9.3 `POST /fingerprint` — navigation-first payload

Handler: [`server.py:1595-1825`](../../helix_context/server.py#L1595).
Request fields: `query` (required), `profile`
(`"fast" | "balanced" | "quality"`, default from
`config.context.fingerprint_mode_profile`), `include_cold`,
`session_context`, `clean`, `max_results` (clamped to `[1, 200]`),
`score_floor` (final post-refiner score threshold), `party_id`.
Returns tier-score breakdowns per candidate gene (`gene_id`, `score`,
`preview`, `path`, `source`, `domains`, `entities`, `chromatin`,
`tier_contributions`) plus accounting fields (`evaluated_total`,
`above_floor_total`, `returned`, `filtered_by_floor`,
`truncated_by_cap`) and a `response_hint` string.

### 9.4 `POST /consolidate` — session memory consolidation

Handler: [`server.py:2672-2690`](../../helix_context/server.py#L2672).
No request body. Distills the session buffer into consolidated
knowledge genes, extracting only new facts, decisions, and
discoveries. Returns `{"facts_extracted": int, "gene_ids":
list[str]}`. On error returns 500 with `{"error": ..., "facts_extracted":
0, "gene_ids": []}`.

### 9.5 `POST /sessions/register` — participant registration

Handler: [`server.py:2239-2320`](../../helix_context/server.py#L2239).
Required body fields: `party_id`, `handle` (both must be non-null
strings, ≥3 chars, no NULL bytes). Optional: `workspace`, `pid`,
`capabilities`, `metadata`, `display_name`, `agent_kind`, `mcp_host`,
`ide_detected`, `ide_detection_via`, `model_id`. Trust-on-first-use
for `party_id`. Returns `{"participant_id", "party_id",
"registered_at", "heartbeat_interval_s", "ttl_s"}`.

### 9.6 `POST /admin/refresh` — reopen genome connection

Handler: [`server.py:2805-2810`](../../helix_context/server.py#L2805).
No body. Reopens the genome connection so external changes
(deletions, thinning) become visible. Returns
`{"refreshed": true, "genes": <new_count>}`.

### 9.7 `POST /admin/vacuum` — reclaim SQLite pages

Handler: [`server.py:2812-2828`](../../helix_context/server.py#L2812).
No body. Runs SQLite `VACUUM` to compact the genome file after
thinning, compaction, or large-scale deletions. Blocks all writers
during the operation — run during maintenance windows. Returns
`{"ok": true, ...}` with before/after sizes; on failure returns 500
with `{"ok": false, "error": str}`.

### 9.8 `GET /health` — readiness + provenance

Handler: [`server.py:2694-2788`](../../helix_context/server.py#L2694).
Returns `status` (`"ok" | "degraded"`), `message`, `ribosome` model,
`ribosome_backend`, `ribosome_configured_backend`, `ribosome_cost_class`,
`genes` (total count), `upstream` URL, `upstream_reachable`, a
`hardware` block (device, vram, fallback state), a `calibration` block
(`ann_threshold_mode`, `abstain_mode`, `abstain_classes`, optional
`ann_threshold` provenance dict), and a `checks` block.

### 9.9 `GET /stats` — genome metrics

Handler: [`server.py:1829-1838`](../../helix_context/server.py#L1829).
Returns `helix.stats()` — genome metrics, compression ratio, per-tier
counters. Cheap synchronous DB read; safe to poll.

---

## 10. Client examples

### 10.1 Frontier agent (Claude Opus 4.7 / GPT-5 / Gemini 3 Pro)

```python
import httpx
from helix_context.agent_prompt import full_fragment

SYSTEM_PROMPT = full_fragment() + "\n\nYou are a coding assistant ..."

def ask_with_helix(user_query: str) -> str:
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            "http://127.0.0.1:11437/context",
            json={
                "query": user_query,
                "caller_model_class": "frontier",
                "session_id": "frontier-agent-001",
                "party_id": "max@local",
                "prompt_tokens": 18000,
            },
        )
        r.raise_for_status()
        env = r.json()

    if "know" in env:
        know = env["know"]
        if know["confidence"] > 0.7:
            # High-confidence retrieval. Inject the expressed_context
            # bytes into the LLM call alongside the system prompt.
            return call_llm(
                system=SYSTEM_PROMPT,
                context=env["content"],
                user=user_query,
            )
        # Low-confidence know — answer but flag uncertainty.
        return call_llm(
            system=SYSTEM_PROMPT,
            context=env["content"],
            user=user_query,
            instruction="Note any uncertainty in your answer.",
        )

    if "miss" in env:
        miss = env["miss"]
        # Honor do_not_answer_from_genome=true. Route on reason.
        if miss["reason"] in ("stale", "cold", "superseded"):
            for path in miss["refresh_targets"]:
                refresh_source(path)              # re-read from disk
            return ask_with_helix(user_query)     # retry once after refresh
        # Escalate-class — pick the first tool from escalate_to.
        first_tool = miss["escalate_to"][0]
        return route_to_tool(first_tool, user_query)

    raise RuntimeError("Helix returned neither know nor miss — server bug")
```

The system prompt MUST include the `HELIX_NO_MATCH_FRAGMENT` /
`HELIX_REFRESH_FRAGMENT` text from
[`helix_context/agent_prompt.py`](../../helix_context/agent_prompt.py)
(or equivalently, the markdown at
[`docs/agent-sdk-fragment.md`](../agent-sdk-fragment.md)) — without it,
a frontier model will paper over `do_not_answer_from_genome=true` by
falling back to its training prior. The "scored as a hard failure in
the offline eval" sentence is the highest-leverage line.

### 10.2 Small-MoE proxy (qwen3:4b / gemma4:e4b)

```python
import httpx
import json
import re

_SLATE_RE = re.compile(r"<helix:slate>(.*?)</helix:slate>", re.DOTALL)

def small_moe_lookup(user_query: str) -> dict | None:
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            "http://127.0.0.1:11437/context",
            json={
                "query": user_query,
                "caller_model_class": "small_moe",
                "session_id": "qwen3-4b-proxy-001",
            },
        )
        r.raise_for_status()
        env = r.json()

    if "miss" in env:
        # Small-MoE callers can't usefully escalate; surface to operator.
        return None

    # Parse the JSON answer slate out of expressed_context. The slate is
    # the primary surface for small_moe callers; the prose tail is
    # secondary. Decoder modes "answer_slate_only" /
    # "condensed_with_slate" guarantee the slate is present.
    m = _SLATE_RE.search(env["content"])
    if m is None:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

# Caller does: kv = small_moe_lookup("cold_start_threshold")
# kv == {"cold_start_threshold": "0.62", ...}
```

Decoder mode is selected by the cross-product of classifier output and
`caller_model_class`; a `factual` query with `caller_model_class=
"small_moe"` resolves to `answer_slate_only` (§8). The 1500-char slate
budget is the contract surface — `slate_char_budget` is configurable
via `[budget]` in `helix.toml`.

### 10.3 MCP host (Claude Code, Cursor, Continue)

When configured as an MCP server, helix-context exposes
`mcp__helix-context__helix_context` directly to the host. The default
`caller_model_class` is `"generic"`, which is byte-identical to
pre-Stage-5 behavior:

```jsonc
// Tool call from inside a Claude Code session:
{
  "tool": "mcp__helix-context__helix_context",
  "arguments": {
    "query": "cold_start_threshold helix.toml",
    "session_id": "taude-2026-05-10",
    "party_id": "max@local"
  }
}
```

The MCP adapter at
[`helix_context/mcp_server.py`](../../helix_context/mcp_server.py)
forwards the request to `/context` with `caller_model_class` defaulting
to `"generic"`. Claude Code and Cursor pass the `know` / `miss` blocks
through verbatim — the host's UI surfaces a "Used: helix-context" tile
when `know.confidence > 0.7`.

---

## 11. Calibration & freshness contract

The 5-feature confidence logistic (§3.1.1) ships with cold-start
defaults but is calibrated against bench data via
`scripts/calibrate_know_confidence.py` (Stage 6 spec §11). Calibrated
parameters live in `[know]` of `helix.toml`:

```toml
[know]
emit_floor = 0.55
s_ref      = 1.0
g_ref      = 0.5
betas      = [-2.0, 2.0, 1.5, 0.7, 1.8, 1.5]   # [intercept, top, gap, agree, coord, freshness_min]
calibrated_at = "2026-05-08T..."
calibrated_on_n = 800
```

Operator runbook for the recalibration cadence and the
`bench_needle_1000.py --plant-stale` flag lives at
[`docs/ops/operator-runbooks.md`](../ops/operator-runbooks.md) — see
the "Recalibrate know.confidence" section.

The `agent` block in every `/context` response carries
`ann_threshold_mode` and `abstain_mode` so the agent can see WHICH
calibration set the response was produced under without an extra
`/health` roundtrip
([`server.py:1349-1350`](../../helix_context/server.py#L1349)).

**Freshness signals.** Three fields on `ContextHealth`
([`schemas.py:182-184`](../../helix_context/schemas.py#L182)) replace
the single `mean(decay)` value from pre-Stage-7:

- `freshness_min` — min decay across expressed candidates.
- `freshness_top1` — decay of the top-1 by score.
- `freshness_weighted` — score-weighted decay sum (also aliased to
  the back-compat `freshness` field).

Status mapping (see Stage 7 spec §3): `genes_expressed > 0 AND
(freshness_top1 < 0.4 OR freshness_weighted < 0.5)` ⇒
`status="stale"` regardless of how many fresh padding genes accompany
the stale needle.

**Soft-stale on `know`.** When the discriminator emits a `KnowBlock`
but `freshness_min < 0.5`, `know.soft_stale = true` and
`agent.recommendation = "refresh"`. The agent MAY answer from the
genome (top-1 is fresh) AND should plan a refresh of lower-ranked
supporting genes on its own schedule
([`server.py:1310-1319`](../../helix_context/server.py#L1310)).

**Refresh tool layer.** `MissBlock.refresh_targets` is the contract
surface for the agent's refresh tool layer. Each entry is an absolute
file path or a fully-qualified URL; the adapter
`MissBlock.to_refresh_targets()` at
[`schemas.py:608-642`](../../helix_context/schemas.py#L608) converts
the list to `RefreshTarget` rows for callers that want the wider
schema (`target_kind`, `priority`, mapped reason). The agent's
expected loop:

1. Receive `miss` with `recommendation="refresh"`.
2. For each path in `refresh_targets`: re-read from disk OR fetch URL.
3. Re-call `/context` with the same query — the next response will
   reflect the refreshed state.

The `HELIX_REFRESH_FRAGMENT` at
[`agent_prompt.py:59-82`](../../helix_context/agent_prompt.py#L59)
codifies the rule "refresh means the answer is here, just out of date —
fetch and retry; escalate means the answer is NOT here — go ask
elsewhere." The two are distinct branches and the agent must not
conflate them.

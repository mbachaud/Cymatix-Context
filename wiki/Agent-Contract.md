# Agent Contract

- Every `/context` and `/context/packet` response carries **exactly one** of two
  top-level blocks: `know` (the retrieved bytes are grounded — you may answer
  from them) or `miss` (retrieval did not find it — do **not** answer from the
  knowledge store, legacy: genome, which is why the wire field below is spelled
  `do_not_answer_from_genome`).
- There is no third "maybe" branch. The discriminator collapses every retrieval
  outcome onto one of the two blocks, and a response with both keys set — or
  neither — is a server bug, not a fall-through case for you to handle.
- The contract exists so a downstream agent can calibrate trust **without
  guessing**. Branch on the structured block; do not parse prose out of the
  assembled window.
- The contract *shape* is stable and load-bearing. The `confidence` scalar is
  **not** — see [The confidence honesty block](#the-confidence-honesty-block)
  below. Rely on `found` and `reason`.
- Honoring the contract is the agent's job, and a capable model will not do it
  by accident. The prompt fragment that makes it stick ships in the package:
  `cymatix_context.agent_prompt.full_fragment()`.

## The two blocks

### `know` — you may answer

```json
{
  "found": true,
  "confidence": 0.83,
  "top_score": 0.92,
  "score_gap": 0.41,
  "lexical_dense_agree": true,
  "gene_id_match": "config.py",
  "coordinate_confidence": 0.78,
  "soft_stale": false
}
```

| Field | Meaning |
|---|---|
| `found` | Always `true` on this branch — the discriminator's verdict |
| `confidence` | `[0, 1]` from a 5-feature logistic. **Provisional** — see below |
| `top_score` | Rank-1 fused retrieval score |
| `score_gap` | `top1 − top2`, a raw subtraction (the `miss` block's `ratio` is the division) |
| `lexical_dense_agree` | `true` when the lexical and dense top-3 clusters share a document. Structurally `false` on the neural-free default path |
| `gene_id_match` | Beacon: a query token that matches a top-1 filename or path token exactly and case-insensitively. `null` when there is no match |
| `coordinate_confidence` | Folder-grain and file-grain agreement between query and source |
| `soft_stale` | Stage 7. `true` when top-1 is fresh enough to act on but lower-ranked supporting documents are stale — answer, *and* plan a refresh |

- The **beacon is deliberately strict**: filename match beats path match, a path
  token must be at least four characters and must not be `src` / `lib` / `app` /
  `bin` / `var` / `tmp` / `out`, and there is no prefix, substring, edit-distance
  or synonym matching. The asymmetry is intentional — a wrong beacon makes a
  frontier model lock in a wrong answer, while a missing beacon only lowers
  `confidence`.

### `miss` — do not answer from the store

```json
{
  "miss": true,
  "reason": "stale",
  "top_score": 0.83,
  "ratio": 1.42,
  "escalate_to": [],
  "refresh_targets": ["F:/Projects/cymatix-context/cymatix_context/config.py"],
  "do_not_answer_from_genome": true
}
```

- **`do_not_answer_from_genome` is always `true` and is the load-bearing bit.**
  An agent that ignores it and answers from its training prior is the exact
  failure the contract exists to prevent.
- `escalate_to` and `refresh_targets` are **mutually exclusive by class**, and a
  Pydantic model validator raises on a construction that violates it. Escalate-class
  reasons must carry at least one escalation tool and an empty refresh list;
  refresh-class reasons the reverse.

## Reason vocabulary

The full tuple, in wire order (clients that index into it depend on the order):

| `reason` | Class | Fires when | What the agent does |
|---|---|---|---|
| `abstain` | escalate | The store is healthy; the query just does not touch any document | The web or an external RAG tier may know. Reading raw files will not help |
| `denatured` | escalate | The store's shape is bad — corrupt or empty | Trust **no** answer from the store right now. Ask a human or search locally |
| `sparse` | escalate | The top hit fell below the confidence floor | Retrieval saw something but does not trust its own match. Escalate or reframe |
| `no_promoter_match` | escalate | Zero candidate documents came back at all | Tag vocabulary mismatch between query and corpus. See the `[synonyms]` note on [Troubleshooting](Troubleshooting) |
| `stale` | refresh | Top-1's source mtime is newer than its last verification | Re-read the paths in `refresh_targets`, then retry the same query |
| `cold` | refresh | A sparse fall-through, but a cold-tier peek surfaced hits | Same loop — the answer is probably there, just demoted |
| `superseded` | refresh | Top-1 has a successor row | Re-read the successor source and retry |

- **Escalate means the answer is not here; refresh means the answer is here but
  out of date.** The two branches are distinct and must not be conflated — that
  distinction is exactly what the shipped refresh fragment codifies.
- `escalate_to` is drawn from `("grep", "rag", "web", "ask_human")` and is
  ordered by expected information yield for the query's *shape*. **Pick the first
  tool in the list.** The ordering rules, first match wins:

| # | Query shape | `escalate_to` |
|---|---|---|
| 1 | Code-shaped (dotted identifier, a `def`/`class`/`import`/`function`/`fn`/`let`/`var`/`const` keyword, or a filename extension token) | `["grep", "rag"]` |
| 2 | Entity-shaped with `no_promoter_match` and no code shape | `["rag", "web"]` |
| 3 | `denatured` store | `["grep", "ask_human"]` |
| 4 | `abstain` on a query of three tokens or fewer | `["ask_human", "rag"]` |
| 5 | Everything else | `["rag"]` |

## The confidence honesty block

- **At shipped 0.9.x defaults the `know` block effectively never fires.** The
  measured maximum confidence is **≈ 0.28** against a shipped `[know] emit_floor`
  of **0.45**, so the discriminator falls through to `miss(reason="sparse")`
  instead of emitting a `KnowBlock`.
- The mechanism is structural, not a tuning miss: `lexical_dense_agree` is one of
  the five logistic features, and it can only be `true` when a dense cluster
  exists. On the neural-free default retrieval path there is no dense cluster, so
  that feature is **dead by construction** and its coefficient never contributes.
- The 2026-08-19 know/abstain sanity replay measured it directly on the 141-needle
  100k carve: **0 of 141 know emissions at shipped defaults versus 23 of 141 with
  the pre-flip encoders re-enabled**. The empty-window class did not grow (2/141 in
  both arms) and the abstain difference (5/141 vs 2/141) was not significant — the
  set rotates with the pool.
- **This fails in the safe direction: 0% false-KNOW.** The system under-claims
  rather than over-claims. It is still a defect, tracked as
  [#287](https://github.com/mbachaud/Cymatix-Context/issues/287) (with
  [#239](https://github.com/mbachaud/Cymatix-Context/issues/239)) in the 0.9.x
  backlog, and disclosed in the 0.9.0 release notes rather than left for a user
  to discover.
- **What to do about it today:** branch on `found` (presence of the `know` key)
  and on `miss.reason`. Do not gate behavior on `confidence > X`. Treat the
  scalar as provisional until the recalibration lands.
- The `agent` block on every response carries `ann_threshold_mode` and
  `abstain_mode`, so you can see *which* calibration set produced a response
  without a second `/health` round trip.

## The prompt fragment

- Without the fragment in the system prompt, a capable model papers over
  `do_not_answer_from_genome: true` and answers from its training prior anyway.
  The fragment's highest-leverage sentence is the one telling the model that doing
  so is **scored as a hard failure**.
- Two fragments ship, and they concatenate cleanly — nothing in the Stage-6 text
  contradicts the Stage-7 addendum:

```python
from cymatix_context.agent_prompt import full_fragment

SYSTEM_PROMPT = full_fragment() + "\n\nYou are a coding assistant ..."
```

| Export | Teaches |
|---|---|
| `CYMATIX_NO_MATCH_FRAGMENT` | The know/miss split, the four Stage-6 reasons, and the escalate-tool discipline |
| `CYMATIX_REFRESH_FRAGMENT` | The Stage-7 addendum: refresh-versus-escalate, and the re-read-then-retry loop |
| `full_fragment()` | Both, in order — the one call to make |

- The same text is published as prose in
  [`docs/agent-sdk-fragment.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/agent-sdk-fragment.md)
  if you would rather paste it than import it.
- **Assembly-time forgery is closed separately.** `[budget]
  neutralize_control_tags` (default on since 2026-08-19) escapes a literal
  `<cymatix:` found in *retrieved content* so an ingested document cannot forge
  the genuine control tags this fragment trains the agent to trust. See
  [Configuration](Configuration).

## Freshness: how a hit becomes a downgrade

- Three freshness signals are computed per retrieval, replacing the single
  pre-Stage-7 mean: `freshness_min` (minimum decay across candidates),
  `freshness_top1` (decay of the score leader), and `freshness_weighted` (the
  score-weighted sum, also aliased to the back-compat `freshness` field).
- A retrieval with documents expressed but `freshness_top1 < 0.4` **or**
  `freshness_weighted < 0.5` is classified `stale` — regardless of how many fresh
  padding documents accompany the stale needle.
- `freshness_min` also enters the confidence logistic as its fifth feature, and
  is treated as neutral when there are no candidates.
- **Soft-stale is not a miss.** When the discriminator emits `know` but
  `freshness_min < 0.5`, `know.soft_stale = true` and the response's
  `agent.recommendation` becomes `"refresh"`. You may answer from the store and
  should schedule a refresh of the lower-ranked supporting documents.
- The refresh loop the contract expects:

```
1. Receive `miss` with recommendation="refresh"  (or `know` with soft_stale=true)
2. For each entry in refresh_targets: re-read the file, or fetch the URL
3. Re-POST /ingest the refreshed content
4. Re-call /context with the same query
```

- Each `refresh_targets` entry is an absolute file path or a fully-qualified URL.
  `MissBlock.to_refresh_targets()` widens the flat list into `RefreshTarget` rows
  (`target_kind`, `source_id`, `reason`, `priority`) for callers that want the
  richer shape. `POST /context/refresh-plan` returns just that plan when you
  already hold the content and only need to know what to re-read.
- Targeted per-document server-side re-consolidation is deliberately **not**
  implemented — `POST /consolidate` is session-scoped and ignores a request body
  by design. The supported per-document path is the four-step loop above.

## The inline tag is legacy, the envelope is canonical

- An empty window may carry a self-closing marker inside the assembled bytes:

```
<cymatix:no_match reason="abstain" do_not_answer="true"/>
```

- **It only ever carries the four Stage-6 reasons** (`abstain`, `denatured`,
  `sparse`, `no_promoter_match`), and an unknown reason falls back to the abstain
  form. The Stage-7 freshness reasons (`stale`, `cold`, `superseded`) are **never**
  emitted as inline tags, because their semantics are the opposite: non-empty
  content that is shown but flagged, rather than an empty do-not-answer window.
- The tag is a compatibility surface for regex-based clients written before
  Stage 6. **New clients branch on the structured `know` / `miss` fields.** The
  design decision is recorded in
  [ADR 2026-05-14](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/architecture/adr/2026-05-14-spec-vs-code-design-decisions.md).

## Packet semantics

`POST /context/packet` is the **agent-safe** surface: model-free end to end, and
it returns what to re-read rather than a bundle to trust. It lifts `know` / `miss`
to the top of the response and adds:

| Field | What it is |
|---|---|
| `verified` | Evidence rows whose sources still check out against disk |
| `stale_risk` | Evidence rows that are shown but flagged — the data is there, its verification is not current |
| `contradictions` | Rows the packet builder found in conflict with each other |
| `refresh_targets` | `RefreshTarget` rows: what to re-read, in priority order |
| `coordinate_confidence` | Folder- and file-grain agreement between query and delivered sources |
| `file_coverage` | Fraction of the query's implied file set that the packet actually covers |
| `notes` | Builder-emitted caveats about this particular packet |

- Request knobs: `query` (required), `task_type` (default `"explain"`),
  `max_genes` (the wire spelling of the per-turn document cap; default 8,
  clamped to `[1, 32]`), `clean`, `read_only`,
  `include_raw` (full document bodies instead of the 280-character thumbnail) and
  `max_item_chars`.
- **Branch on `miss` versus `know` before you read `stale_risk` or `verified`.**
  The packet's evidence lists are meaningful only once you know which branch you
  are on.
- The full request and response shapes, with a worked packet example: [HTTP
  API](HTTP-API).

## Caller class changes the render, not the contract

- `caller_model_class` (`"generic"` default, `"small_moe"`, `"frontier"`) selects
  a **render branch** — ordering, assembly cap, decoder mode, whether legibility
  headers and the answer slate are emitted. It does not change retrieval and it
  does not change the know/miss contract.
- `frontier` gets rank-1-first ordering and a widened assembly cap; `small_moe`
  gets a JSON answer slate and suppressed headers; `generic` is regression-locked
  byte-identical to pre-Stage-5 output. The full matrix is on
  [HTTP API](HTTP-API).

*Wire field names are legacy. `gene_id_match`, `do_not_answer_from_genome`, and
the `refresh_targets` path strings above are reproduced verbatim — the JSON
contract still speaks the biology lexicon even though the prose and the config
surface use software terms. Renaming them is a tracked Tier-3 change,
deliberately not taken in 0.9.1 because it breaks every existing consumer. See
[Lexicon](Lexicon).*

## Go deeper

- [`docs/api/context-endpoint.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/api/context-endpoint.md) — the canonical schema reference: both branches field-by-field, the reason vocabulary, the escalation rules, and worked frontier / small-MoE / MCP client examples
- [`docs/agent-sdk-fragment.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/agent-sdk-fragment.md) — the prompt fragment in prose form
- [`cymatix_context/agent_prompt.py`](https://github.com/mbachaud/Cymatix-Context/blob/master/cymatix_context/agent_prompt.py) — `full_fragment()` and the two exported fragments
- [`cymatix_context/schemas.py`](https://github.com/mbachaud/Cymatix-Context/blob/master/cymatix_context/schemas.py) — `KnowBlock`, `MissBlock`, `ContextPacket`, and the mutual-exclusivity validator
- [`cymatix_context/scoring/know_decision.py`](https://github.com/mbachaud/Cymatix-Context/blob/master/cymatix_context/scoring/know_decision.py) — the discriminator, the beacon rules, and `_pick_escalation`
- [`docs/benchmarks/2026-07-06-know-logistic-calibration.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/benchmarks/2026-07-06-know-logistic-calibration.md) — how the shipped `[know]` betas and the 0.45 floor were fitted
- [`docs/operator-runbooks.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/operator-runbooks.md) — the recalibration runbook
- Next: [HTTP API](HTTP-API) · [Pipeline](Pipeline) · [Configuration](Configuration) · [Benchmarks and Receipts](Benchmarks-and-Receipts)

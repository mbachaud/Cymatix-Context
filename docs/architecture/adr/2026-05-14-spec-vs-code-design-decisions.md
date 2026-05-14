# ADR 2026-05-14: Spec-vs-code design decisions

Status: Accepted
Date: 2026-05-14
Closes: [#64](https://github.com/mbachaud/helix-context/issues/64)
Source: post-Stage-7 spec-vs-code audit (PR #60, disc-4 / disc-7 / disc-8)

## Context

PR #60's spec-vs-code audit flagged three places where the shipped code
disagrees with what a strict reading of the spec implies. Each could be
"fix the code", "fix the docs", or "leave as is + document the
rationale". They share the "needs a design decision before any code
moves" property, so we resolve them in one ADR.

## Decisions

| Question | Decision | Why |
|---|---|---|
| Q1: `HELIX_DEVICE` overloaded | **B — document parse rules** | Already safe by construction (HW picker whitelists; federation accepts any string). Renaming would force a deprecation cycle on every existing operator setup for zero functional gain. |
| Q2: `/consolidate` ignores request body | **B — document session-scoped behavior; defer targeted re-consolidate** | Endpoint is intentionally session-scoped; targeted re-consolidate has no current consumer asking for it. Documenting closes the spec gap; adding a parser or new route would invent surface area. |
| Q3: Inline `<helix:no_match/>` missing Stage 7 reasons | **C — document inline tag as legacy-compat surface** | The inline tag fires only when expressed context is empty; Stage 7 demotions ship `stale_risk` + `refresh_targets` instead (non-empty). Extending the tag would require new emission semantics with regex-matcher risk for no clear consumer benefit. |

---

## Q1: `HELIX_DEVICE` overloaded between hardware picker and federation attribution

### Current behavior (verified)

- **Hardware picker** (`helix_context/hardware.py:30-54`,
  `_resolve_requested_device`): reads `HELIX_DEVICE`, normalizes to
  lowercase, accepts only the whitelist
  `{"auto", "cuda", "rocm", "mps", "cpu"}`. Any other value logs
  `WARNING` and falls through to `[hardware] device` in `helix.toml`,
  then to `"auto"`. Never raises, never blocks startup.
- **Federation attribution** (`helix_context/server/helpers.py:139-148`
  and `helix_context/identity/registry.py:266`): reads `HELIX_DEVICE`
  as a free-form device-handle string, falling back to
  `HELIX_PARTY`, then `socket.gethostname()`. Accepts any non-empty
  value.

### Concrete behavior matrix

| `HELIX_DEVICE=` value | HW picker | Federation |
|---|---|---|
| `cuda` / `cpu` / `mps` / `rocm` / `auto` | Used as device kind | Used as device handle (literally `"cuda"`, etc.) |
| Any other string (e.g., `my-laptop`, `swift_wing21`) | Logged WARNING, ignored; HW picker continues to `[hardware] device` / `"auto"` | Used as device handle |
| Unset | Falls through to TOML / auto | Falls through to `HELIX_PARTY` or `socket.gethostname()` |

The two consumers are non-interacting in practice: the HW picker is
intentionally tolerant of unknown values, and the only "bad" outcome of
setting `HELIX_DEVICE=my-laptop` is one warning at startup. The
federation layer always gets the value it wants.

### Options considered

- **A — Rename** (`HELIX_DEVICE_LABEL` + `HELIX_HARDWARE`): correct in
  principle, but every operator already configured with `HELIX_DEVICE`
  for federation would need to migrate, including the multi-device
  attribution example in `docs/architecture/FEDERATION_LOCAL.md:154`
  and the `docs/clients/claude-code.md:48` MCP config snippet. A
  deprecation cycle adds churn for zero behavior change.
- **B — Document** (chosen): publish the parse rules so operators stop
  treating the overload as ambiguous.
- **C — Leave as is**: documentation gap remains.

### Decision

Adopt **B**. Add a "`HELIX_DEVICE` parse rules" subsection to
`docs/architecture/FEDERATION_LOCAL.md` that:

1. States the whitelist used by the HW picker.
2. States that any value not in the whitelist is treated as a
   federation-only label and the HW picker silently continues to
   `[hardware] device`.
3. Calls out that the WARNING log at startup is expected when using a
   custom device label, and how to silence it via the picker route
   (set `[hardware] device = "cpu"` explicitly in `helix.toml` and
   move the label into `HELIX_PARTY` if you want zero log noise).

### Consequences

- No code change, no test impact.
- Documentation is the single source of truth for the overload. If
  consumers report churn from the WARNING log, we revisit (rename or
  drop the WARNING on the "looks like a device label" branch).

---

## Q2: `/consolidate` ignores request body

### Current behavior (verified)

- Handler: `helix_context/server/routes_ingest.py:134-148`.
- Signature: `async def consolidate_endpoint()` — no parameters. The
  request body is not parsed at all (FastAPI doesn't bind it to any
  argument).
- Calls `helix.consolidate_session_async()`
  (`helix_context/context_manager.py:1657-1748`) which distills the
  active in-process session buffer.
- Existing documentation
  (`docs/api/context-endpoint.md:554-561`,
  `docs/operator-runbooks.md:715-755`) already says "No request body"
  and "operates on the session buffer aggregate, not a single
  document".

### Note on the issue premise

The issue states: _"Docs and some integrations assume
`{"gene_id": "..."}` for targeted re-consolidation."_ We could not
locate any in-repo doc or integration that makes this assumption.
The existing docs correctly describe the no-body behavior. The gap is
that they describe "what" without describing "why this is the design
on purpose, and what to use instead for per-gene re-consolidation".

### Options considered

- **A — Add body parsing for `{"gene_id"?: str, "session"?: str}`**:
  no current consumer is asking for it, and there's no internal flow
  that ingests "rewrite this one gene from its source" (that path is
  the freshness gate's `refresh_targets` payload + the agent
  re-reading from source and re-`POST /ingest`-ing).
- **B — Document the session-buffer-only behavior explicitly**
  (chosen): make the design-intent explicit in the docs that already
  document the endpoint, and call out the alternative path
  (`/context/refresh-plan` → agent re-read → `/ingest`) for the
  targeted case.
- **C — Add `/consolidate/gene/{gene_id}`**: invents new endpoint
  surface for a hypothetical consumer. If real demand surfaces, we
  add this later — the path is reserved.

### Decision

Adopt **B**. Append a "Why session-scoped?" note to
`docs/api/context-endpoint.md:554`'s `/consolidate` section that:

1. Restates the no-body contract.
2. Explains the design intent (consolidation is over the active
   buffer; per-gene rewrites are a different shape that go through
   `/ingest` after the agent re-reads the source).
3. Calls out the path forward if targeted re-consolidate is ever
   needed: a future `/consolidate/gene/{gene_id}` endpoint is the
   reserved shape (Option C). It is intentionally not implemented now.

### Consequences

- No code change, no test impact.
- If a future consumer needs per-gene re-consolidation, this ADR
  records that the reserved shape is `/consolidate/gene/{gene_id}`
  (not body-on-`/consolidate`). That avoids a later debate.

---

## Q3: Stage 7 reasons in `MissBlock.reason` but not in inline `<helix:no_match/>`

### Current behavior (verified)

- The structured `MissBlock.reason` enum
  (`helix_context/schemas.py:481-490`):
  `{"abstain", "denatured", "sparse", "no_promoter_match", "stale",
  "cold", "superseded"}`.
- The inline tag `<helix:no_match reason="..." do_not_answer="true"/>`
  is emitted by `_no_match_token(reason)`
  (`helix_context/context_manager.py:221-236`), which whitelists
  `{"abstain", "denatured", "sparse", "no_promoter_match"}` and
  defaults unknown reasons to `"abstain"`.
- The inline tag is only emitted on three branches
  (`helix_context/context_manager.py:1153`, `2116`, `2345`), all of
  which correspond to **empty expressed context** (zero candidates
  survived, abstain tier fired, or no promoter-tier match).
- Stage 7 demotions (stale / cold / superseded) **do not produce
  empty expressed context**. They populate `ContextPacket.stale_risk`
  and `ContextPacket.refresh_targets` and ship the structured
  `MissBlock` in the envelope, alongside any verified items. The
  inline tag is never invoked with Stage 7 reasons in current code.

### What the gap actually is

The inline tag is, by construction, the pre-Stage-7 contract for
"there is nothing to show, do not answer". Stage 7 added a third
state — "there is something to show, but it's stale, please refresh"
— that doesn't fit the inline-tag's "empty + do-not-answer" semantics.
Extending the inline tag would either:

1. Force the tag to emit alongside content (semantically incompatible
   with `do_not_answer="true"`), or
2. Force Stage 7 demotions to ship empty contexts (regresses the
   "stale-but-actionable" UX that motivated Stage 7).

Neither is desirable.

### Options considered

- **A — Extend the inline tag** with Stage 7 reasons: changes the
  emission contract (would need to emit alongside content for stale /
  cold / superseded). Risk: legacy regex matchers parsing the tag
  would see a `reason` value they don't recognize.
- **B — New tags** (`<helix:stale_match/>`, `<helix:cold_match/>`,
  `<helix:superseded_match/>`): preserves backwards compat at the
  parser layer but adds vocabulary that overlaps with the structured
  envelope without adding signal a Stage-7-aware client doesn't
  already have via `KnowBlock.soft_stale` /
  `MissBlock.refresh_targets`.
- **C — Document as legacy-compat** (chosen): the inline tag stays
  what it is — a pre-Stage-7 "do not answer, empty context" signal.
  Stage-7-aware clients read the structured envelope where the
  freshness reasons live by design.

### Decision

Adopt **C**. Update three docs:

1. `docs/api/context-endpoint.md` — add an "Inline tag is legacy
   contract" callout in the section that describes the inline tag.
2. `docs/agent-sdk-fragment.md` — add a one-line note recommending
   new clients read `know` / `miss` rather than scrape the inline
   tag.
3. The `_no_match_token` docstring in
   `helix_context/context_manager.py` — add a note that the four
   whitelisted reasons are the legacy-compat set by design, and
   point at this ADR.

### Consequences

- No behavior change. Legacy regex matchers continue to work
  unchanged.
- New client examples in the docs route through `KnowBlock` /
  `MissBlock` (which they already do; this just makes the
  recommendation explicit).
- If a future use case genuinely needs an inline signal for
  Stage-7-style demotions (e.g., a frontier agent that consumes only
  the `expressed_context` text and ignores the JSON envelope), we
  revisit by adding new tag shapes (Option B), not by extending
  `<helix:no_match/>`.

---

## Cross-cutting note

All three decisions follow the lowest-risk-that-closes-the-issue
heuristic from issue #64. The substantive change in every case is a
doc clarification, not a code change. We deliberately did not bundle
opportunistic refactors (e.g., dropping the WARNING log in Q1, adding
a stub `/consolidate/gene/...` for Q2) — those remain available as
follow-ups if a real consumer surfaces demand.

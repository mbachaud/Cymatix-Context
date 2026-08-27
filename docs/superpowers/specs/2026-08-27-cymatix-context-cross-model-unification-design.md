# Cross-model `cymatix-context` agent integration — design

**Date:** 2026-08-27
**Status:** architecture approved; implementation specification for review
**Branch:** `codex/cymatix-context-cross-model`

## 1. Purpose

Give Claude Code, Codex, Gemini CLI, and Google Antigravity one behavioral
contract for using Cymatix while preserving each host's native configuration
format and lifecycle.

The user-facing integration name is `cymatix-context` everywhere a hyphenated
name is valid. The implementation keeps Python and MCP identifier conventions
where hyphens are invalid:

| Surface | Canonical form |
|---|---|
| Product, Agent Skill, MCP server entry | `cymatix-context` |
| MCP protocol server/namespace | `cymatix` (stable identifier) |
| Python package and module | `cymatix_context` |
| MCP tool prefix | `cymatix_` |
| CLI command prefix | `cymatix-` |
| Environment variable prefix | `CYMATIX_` |

The outcome is not a universal model proxy. It is a portable Agent Skill, a
single stdio MCP adapter, a common identity/request contract, host-specific
configuration profiles, and diagnostics that report the layers separately.

## 2. Decisions

1. **One authored skill.** `skills/cymatix-context/SKILL.md` is the canonical,
   model-neutral instruction source. Host installation recipes copy that exact
   artifact into the host's supported discovery directory; host-specific setup
   does not fork the skill body.
2. **One MCP adapter.** Every host launches `python -m
   cymatix_context.mcp_server` over stdio and the adapter calls the Cymatix HTTP
   server selected by `CYMATIX_MCP_URL`.
3. **Native host configuration.** Claude JSON, Codex TOML, Gemini CLI JSON, and
   Antigravity JSON remain native. A declarative host-profile registry supplies
   their defaults to documentation, examples, and read-only status checks.
4. **No automatic user-config rewriter in this PR.** Machine-local cleanup uses
   each host's native disable mechanism plus backups. Generated examples contain
   placeholders and never embed a user's path or identity.
5. **Proxy-independent operation.** Direct MCP retrieval does not require
   Cymatix to be configured as an OpenAI-compatible model proxy.
6. **Tray-independent availability.** The tray/launcher is an optional process
   supervisor. `GET /health` on the configured Cymatix server is the authority
   for backend availability.
7. **Clean active-surface break from Helix.** Active code, setup scripts, Agent
   Skills, MCP entries, and current client docs use Cymatix names only. Historical
   records and narrow read-only migration helpers remain explicitly whitelisted.

## 3. Current-state problems

### 3.1 The distributed skill is host-specific and disagrees with the MCP profile

The tracked skill is currently `skills/cymatix/SKILL.md`, exposes `name:
cymatix`, and describes the caller as Claude. It tells the agent to use
`cymatix_stats`, `cymatix_session_recent`, and `cymatix_announce`, while the
default lean MCP profile exposes only five tools and prunes all three. An agent
following the installed instructions can therefore be asked to call tools that
do not exist in its session.

### 3.2 MCP callers lose model-specific retrieval behavior

The `/context` route supports `caller_model_class` (`generic`, `small_moe`, or
`frontier`) and uses the canonical `model` key as a downstream-model hint. The
MCP tool exposes only `downstream_model`, sends it under that same key, and does
not expose `caller_model_class`. The route ignores `downstream_model`, so direct
MCP calls silently receive generic retrieval behavior even when the caller tried
to identify its model.

`model_id` is a different concern: it is descriptive session metadata used by
the registry/dashboard. It must not silently select retrieval policy.

### 3.3 Status is Claude-shaped rather than integration-shaped

`cymatix-status` discovers only ancestor `.mcp.json` files and defaults skill
discovery to `~/.claude/skills/cymatix-context`. A working Codex, Gemini CLI, or
Antigravity installation can therefore be reported as missing. The current
single `integration_ready` boolean also conflates server health, launcher
health, MCP configuration, activation state, and skill installation.

The status constants and tests additionally contain duplicated post-rename
tokens: canonical and legacy names, modules, and environment variables are
identical. Those tests cannot detect the compatibility cases their names claim
to cover. The implementation deletes those pseudo-legacy branches/tests rather
than restoring removed Helix runtime compatibility.

### 3.4 Active setup surfaces still describe no-op Helix adoption

Some Windows batch scripts describe or implement old-prefix adoption but test
the `CYMATIX_*` variable on both sides. The blocks have no effect and contradict
the repository's 0.8.5 clean-break policy. They should be removed, not restored
as `HELIX_*` mirrors.

### 3.5 Local host state was asymmetric at the 2026-08-27 audit

- Codex has a canonical `cymatix-context` MCP block, currently disabled, and an
  obsolete user-global Helix skill.
- Gemini CLI has an active `helix-context` MCP entry that launches
  `helix_context.mcp_server` with `HELIX_*` environment variables.
- Antigravity's canonical global file was inspected and contains no Helix or
  Cymatix `mcpServers` entry; no workspace MCP file is present.
- The local Cymatix server and launcher were unreachable during the audit, so
  enabling canonical MCP entries before health succeeds would create noisy
  failed calls.

These are machine-local facts. They inform the migration procedure but are not
files in the pull request.

## 4. Architecture and control planes

```text
host Agent Skill (instructions only)
          │
          ▼
host-native MCP configuration
          │ stdio JSON-RPC
          ▼
python -m cymatix_context.mcp_server
          │ HTTP via CYMATIX_MCP_URL
          ▼
Cymatix server /health, /context, /ingest, /sessions, ...

Optional and independent:
  model API client ─► /v1/chat/completions proxy
  tray/launcher    ─► starts/stops/observes the Cymatix server
```

The four planes have deliberately different failure semantics:

| Plane | What it does | If absent or unavailable |
|---|---|---|
| Agent Skill | Teaches the model when and how to use Cymatix | The model still works and may still call visible MCP tools, but without the shared decision policy |
| MCP adapter | Exposes Cymatix tools to the host | No Cymatix tools; the agent uses native repository/file tools |
| Cymatix server | Performs retrieval, ingest, health, and registry operations | MCP calls fail or time out according to `CYMATIX_MCP_TIMEOUT`; the agent falls back to native exploration |
| Model proxy | Injects Cymatix context into OpenAI-compatible chat traffic | Direct MCP behavior is unchanged |
| Tray/launcher | Supervises and visualizes the server | A separately started healthy server remains fully available |

No readiness check may infer server availability from tray presence, and no
skill instruction may imply that selecting a proxy model is required for MCP.

## 5. Canonical Agent Skill contract

### 5.1 Discovery identity

The distributed artifact moves to `skills/cymatix-context/SKILL.md` and its
frontmatter is:

```yaml
---
name: cymatix-context
description: Use Cymatix for architectural, historical, semantic, or cross-file repository context; verify exact code locally and fall back cleanly when Cymatix is unavailable.
---
```

The description remains concise because supported hosts use it to decide when
to load the full skill. The body does not call the host "Claude," "Codex," or
"Gemini." Host names appear only in setup documentation.

### 5.2 Required decision policy

The skill teaches every model the same order of operations:

1. Use `cymatix_context` first for architectural, historical, semantic, or
   cross-file questions where compressed context can narrow exploration.
2. Use `cymatix_context_packet` when freshness, authority, or edit safety matters.
3. Read local files for exact current code, line references, quoted text, and all
   edits. Cymatix retrieval is evidence discovery, not a substitute for source
   verification.
4. Use `cymatix_ingest` only for meaningful, attributable additions—not every
   transient thought or raw tool result.
5. Use `cymatix_sessions_list` for live participant awareness without assuming
   that session presence proves current work state.
6. If a Cymatix tool call fails and `cymatix_health` remains callable, use it to
   classify backend state. If the adapter/tool list is absent, disabled, or its
   stdio connection failed, continue directly with native tools. Cymatix may
   improve the task but must not block it.
7. Call `cymatix_announce` once after successful MCP registration when the tool
   is exposed, so the registry can display the current model. Announcement is
   descriptive and does not alter retrieval. If best-effort registration failed,
   continue without announcement.

### 5.3 Default tool surface

The default lean MCP profile becomes exactly six tools:

- `cymatix_context`
- `cymatix_context_packet`
- `cymatix_ingest`
- `cymatix_health`
- `cymatix_sessions_list`
- `cymatix_announce`

The extra announcement tool repairs the current skill/runtime mismatch and costs
far less context than exposing the full administrative surface. Tools such as
`cymatix_stats`, `cymatix_session_recent`, database swaps, and debug aliases stay
behind `CYMATIX_MCP_FULL=1` and are not required by the portable skill.

If a host caches a previous tool list, its documentation must tell the user how
to refresh or restart that host before diagnosing a missing tool.

## 6. Identity and model contract

### 6.1 Stable identity axes

The adapter continues to carry separate identity layers:

| Variable | Meaning |
|---|---|
| `CYMATIX_ORG` | Tenant or team |
| `CYMATIX_PARTY_ID` | Device/party identity used for presence |
| `CYMATIX_DEVICE` | Ingest-time device override, normally equal to party |
| `CYMATIX_USER` | Human participant |
| `CYMATIX_AGENT` | AI persona/author handle |
| `CYMATIX_AGENT_KIND` | Model/tool family |
| `CYMATIX_MCP_HANDLE` | Live session handle |
| `CYMATIX_MCP_HOST` | Host application |

`CYMATIX_AGENT_KIND` and `CYMATIX_MCP_HOST` are orthogonal. The canonical host
profiles use:

| Profile | `CYMATIX_AGENT_KIND` | `CYMATIX_MCP_HOST` |
|---|---|---|
| Claude Code | `claude-code` | `claude-code` |
| Codex | `codex` | `codex` |
| Gemini CLI | `gemini` | `gemini-cli` |
| Antigravity | `gemini` | `antigravity` |

Automatic host detection is allowed only for a documented intentional host
environment signal. Otherwise the explicit `CYMATIX_MCP_HOST` value is
authoritative; process-name or parent-process guessing is out of scope.

### 6.2 Three distinct model fields

| Field | Purpose | Effect |
|---|---|---|
| `model` | Downstream model name for budget/model-family hints | May affect budget and legacy small-model behavior |
| `caller_model_class` | Closed retrieval/render policy: `generic`, `small_moe`, `frontier` | Selects the existing retrieval/render branch |
| `model_id` | Registry/dashboard label sent through `cymatix_announce` | Display and telemetry only |

The MCP `cymatix_context` schema exposes canonical optional `model` and
`caller_model_class` parameters. The adapter posts both under those names.
`CYMATIX_CALLER_MODEL_CLASS` may provide a host-level default when the tool call
omits the class; an explicit tool argument wins. Standard host profiles omit
this variable, so unidentified callers retain the existing `generic` behavior
and no retrieval policy is inferred merely from the host/vendor. An operator may
set it when an installation is intentionally pinned to one class.

`CYMATIX_CALLER_MODEL_CLASS` is validated once at MCP adapter startup against
`generic`, `small_moe`, and `frontier`. An invalid configured value fails startup
with a clear allowed-values message rather than silently changing retrieval.
The environment default and tool parameter apply to `cymatix_context` only;
`cymatix_context_packet` is intentionally model-independent because it returns
freshness/authority evidence rather than a model-sized rendered context window.

For one deprecation window, the HTTP `/context` route will accept
`downstream_model` only as a fallback alias when `model` is absent. New docs,
tests, and the MCP schema use `model`. This compatibility alias is not a Helix
brand surface and is removable in a later version after telemetry confirms no
callers remain.

Invalid per-call `caller_model_class` values preserve the `/context` route's
`Invalid caller_model_class` error and allowed-values payload in the MCP
response; they do not silently become `generic`. The response metadata continues
to echo the effective class. This rule is scoped to `/context`: the existing
`/v1/chat/completions` proxy fallback for invalid classes is unchanged because
proxy policy changes are outside this design.

## 7. Host profiles and installation

One declarative registry defines stable, non-secret host facts:

- profile id and display name;
- MCP configuration format and candidate locations;
- skill discovery locations;
- canonical `agent_kind` and `mcp_host` values;
- activation-state source where it is safely readable;
- refresh/restart and verification guidance.

Parsers consume this registry. A rendering helper produces native example
snippets from the registry; marked blocks in the client guides are regenerated
or compared byte-for-byte with that output in tests. This avoids a second,
manually authored source of truth. The registry does not store identities,
absolute repository paths, tokens, or credentials.

Each profile records its official source URL and `accessed_on=2026-08-27` so a
future client-format change is reviewed deliberately:

| Profile | Primary sources |
|---|---|
| Claude Code | <https://code.claude.com/docs/en/slash-commands>, <https://code.claude.com/docs/en/mcp> |
| Codex | <https://learn.chatgpt.com/docs/build-skills>, <https://learn.chatgpt.com/docs/extend/mcp> |
| Gemini CLI | <https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/using-agent-skills.md>, <https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md> |
| Antigravity | <https://antigravity.google/docs/skills>, <https://antigravity.google/docs/mcp>, <https://www.antigravity.google/docs/cli/gcli-migration/> |

### 7.1 Claude Code

- Skill: project `.claude/skills/cymatix-context/SKILL.md` or personal
  `~/.claude/skills/cymatix-context/SKILL.md`.
- MCP: project root `.mcp.json` or native `claude mcp add` user/local scope.
- Project MCP definitions may be pending approval, rejected, or disabled even
  when the JSON is canonical. File presence alone must not be reported as a
  confirmed live connection.
- Verify with `claude mcp get cymatix-context`, `claude mcp list`, or `/mcp`.

### 7.2 Codex

- Skill: workspace `<repo>/.agents/skills/cymatix-context/SKILL.md` or global
  `~/.agents/skills/cymatix-context/SKILL.md`.
- MCP: project or user `.codex/config.toml` under
  `[mcp_servers.cymatix-context]`.
- The native `enabled = false` field is authoritative when present.
- Per the official Codex skill configuration schema, a local skill can be
  disabled without deleting it using an absolute-path override:

  ```toml
  [[skills.config]]
  path = "<absolute-path-to-skill>/SKILL.md"
  enabled = false
  ```

- Restart Codex after configuration/skill activation changes.

### 7.3 Gemini CLI

- Skill: `.agents/skills/cymatix-context/SKILL.md` or
  `.gemini/skills/cymatix-context/SKILL.md` at workspace/user scope.
- MCP: `mcpServers` in user `~/.gemini/settings.json` or workspace
  `.gemini/settings.json`.
- Persistent MCP enablement is controlled by `gemini mcp disable|enable` and
  stored separately from the MCP definition. If its enablement record is absent,
  unreadable, or malformed, status reports activation as `unknown`, not enabled.
- Verify with `gemini mcp list` or `/mcp`; restart after MCP config edits.
  Skills can be rescanned with `/skills reload` and checked with `/skills list`.

### 7.4 Google Antigravity

- Skill: workspace `.agents/skills/cymatix-context/SKILL.md` or global
  `~/.gemini/config/skills/cymatix-context/SKILL.md`.
- MCP: global `~/.gemini/config/mcp_config.json` or workspace
  `.agents/mcp_config.json`.
- An entry's `disabled: true` is authoritative. The UI may also disable,
  refresh, or uninstall an entry.
- Antigravity and Gemini CLI configs are distinct after any one-time migration;
  the integration must not assume that editing one updates the other.

### 7.5 Example safety

Tracked examples use placeholders such as `<absolute-repo-path>`, `<org>`, and
`<agent-handle>`. They may include the loopback default
`http://127.0.0.1:11437`, but never a real user's identity, home directory,
secret, or machine-specific absolute path.

No ready-to-commit project MCP file is added at repository root because it could
unexpectedly prompt or activate tools for contributors. The client guides show
explicit opt-in snippets instead.

## 8. Host-aware status design

`cymatix-status` gains `--host auto|claude-code|codex|gemini-cli|antigravity`.
Explicit `--mcp-config` and `--skill-dir` continue to override discovery for
testing, nonstandard layouts, and automation.

### 8.1 Reported dimensions

The machine-readable result reports independent dimensions rather than
collapsing them prematurely:

| Dimension | Values |
|---|---|
| `host.selection` | a profile id, `unknown`, or `ambiguous` |
| `server.transport` | `reachable`, `unreachable` |
| `server.health` | `healthy`, `unhealthy`, `unknown` plus the unmodified health payload |
| `launcher.state` | `running`, `stopped`, `unreachable`, `not_configured`; informational only |
| `mcp.configuration` | `canonical`, `noncanonical`, `missing`, `invalid` |
| `mcp.activation` | `enabled`, `disabled`, `unknown` |
| `mcp.live` | `connected`, `disconnected`, `unknown` |
| `skill.installation` | `present`, `missing` |
| `skill.activation` | `enabled`, `disabled`, `unknown` |
| `configured_ready` | `true`, `false`, or `null` when activation is unknowable |
| `guided_ready` | `true`, `false`, or `null`; adds skill state to `configured_ready` |

`configured_ready` is an inference about canonical, enabled configuration
pointing at a healthy backend; it does **not** claim that a host has launched or
connected its stdio adapter. `mcp.live=connected` requires positive evidence
from an exact current session/handle match in Cymatix's active registry. When no
unambiguous handle is configured, or the registry cannot be read, it remains
`unknown`. A configured handle that is absent from a successfully read active
registry is `disconnected`.

A missing skill does not make MCP configuration false. `guided_ready` answers
whether the host also has the shared interaction policy. Real-host connection
checks are opt-in manual smoke tests; fixture-based CI does not claim to launch
the proprietary hosts.

The readiness truth table is:

| MCP configuration | Activation | Server health | `configured_ready` |
|---|---|---|---|
| missing, noncanonical, or invalid | any | any | `false` |
| canonical | disabled | any | `false` |
| canonical | enabled | healthy | `true` |
| canonical | enabled | unhealthy or unknown | `false` |
| canonical | unknown | healthy | `null` |
| canonical | unknown | unhealthy or unknown | `false` |

Health mapping is deterministic: a transport failure is
`transport=unreachable, health=unknown`; a parsed `/health` payload with
`status="ok"` is `healthy`; any other successfully parsed health status is
`unhealthy`; an invalid/non-JSON response is `transport=reachable,
health=unknown` with its parse error retained.

`guided_ready=false` when the skill is missing or definitively disabled. It is
`null` when `configured_ready` or skill activation is unknown and no definitive
false condition exists; otherwise it is true.

The launcher never gates either readiness value. A healthy server with no tray
is available; a running tray with an unhealthy server is not.

### 8.2 Discovery and parsing

- Claude, Gemini CLI, and Antigravity JSON use the same canonical entry validator
  after their native discovery layer extracts `mcpServers.cymatix-context`.
- Codex TOML is parsed with the Python 3.11 standard `tomllib`; status never
  round-trips or rewrites the file.
- A supplied config path is validated according to the explicit/selected host,
  with a clear error for a format mismatch.
- `--host auto` uses deterministic config evidence, not process-name guessing:
  inspect workspace candidates first and user candidates second; at each scope,
  select the profile only when exactly one candidate contains a Cymatix entry.
  Multiple matching profiles return `ambiguous` with every candidate path; no
  match returns `unknown`. An explicit `--host` always wins.
- Missing, unreadable, or malformed out-of-band activation state (including the
  Gemini CLI enablement record) produces `unknown` rather than assuming enabled.
- The first implementation does not spawn proprietary host CLIs to infer live
  state. It extracts `CYMATIX_MCP_HANDLE` and `CYMATIX_MCP_HOST` from the selected
  config and compares them with the server's active session registry. A canonical
  file plus healthy `/health` response never fabricates `mcp.live=connected`.
- Status output names the inspected files and explains unknown activation state.

### 8.3 Status probe trust boundary (security-review amendment)

`CYMATIX_MCP_URL` remains the configured MCP runtime target, but status treats
it as untrusted diagnostic input. Without an explicit `--server-url`, status
may probe it only when it is a validated absolute HTTP(S) loopback base URL.
Validation requires a scheme and hostname and rejects malformed URLs, userinfo,
query strings, and fragments without DNS resolution. `localhost`, IPv4 loopback,
and IPv6 loopback are accepted; an implicit non-loopback config URL is reported
as unprobed with an action to pass `--server-url` explicitly.

An explicitly supplied `--server-url` takes precedence over the parsed config
and is the operator opt-in for a validated non-loopback target. Server and
launcher URLs are redacted before JSON/text rendering, and status reads both
successful and error response bodies with a fixed bound. This policy changes
only read-only status probing; it does not change MCP runtime connections or
native config parsing/storage.

The existing text output remains concise. JSON is the authoritative automation
surface. Exit code zero requires `configured_ready=true`; unknown or false
returns nonzero with a specific next action. Live state remains a separate
diagnostic and does not silently redefine the exit code.

## 9. Helix retirement and migration boundary

### 9.1 Active surfaces that must be Cymatix-only

The lint's included surface is deterministic:

- `cymatix_context/**/*.py` except the read-only whitelist;
- top-level and `deploy/**` launchers ending in `.bat`, `.ps1`, or `.sh`;
- `README.md`, `CLAUDE.md`, `docs/SETUP.md`, `docs/TROUBLESHOOTING.md`,
  `docs/clients/**`, and current `docs/api/**` / `docs/ops/**` setup material;
- `skills/**`, MCP templates, and package entry points.

It explicitly excludes `CHANGELOG.md`, `docs/archive/**`,
`docs/superpowers/**`, benchmark data/receipts, and historical fixtures. Any
additional exclusion is a reviewed whitelist change, not an ad-hoc substring
skip.

Stale `CYMATIX_* → HELIX_*` adoption comments/blocks are deleted. The project
does not restore `HELIX_*` reads, `helix_context` imports, Helix CLI aliases, or
`helix.toml` fallback behavior. Tests assert each prohibition independently and
also assert that active scripts contain no old-prefix adoption block.

### 9.2 Intentional references that remain

- import of old bundles carrying `helix_format_version`;
- read-only fallback for an old `~/.helix` launcher selection record;
- installer cleanup of this project's obsolete `Helix.lnk` shortcut;
- the bounded rename notice/table in `README.md` and clean-break notes in
  `CLAUDE.md`;
- `deploy/pypi-tombstone/**`, whose package name must remain the old name to
  redirect installers, but whose prose/comments must be corrected so they do
  not falsely claim current Cymatix still ships removed aliases;
- `CHANGELOG.md`, archived designs/research, benchmark fixtures/receipts, and
  other clearly historical records.

The active-surface lint uses an explicit path/fragment whitelist. It does not
globally rewrite or erase historical provenance.

### 9.3 Machine-local operator completion checklist

Local changes occur after this specification is committed and reviewed. They
are excluded from Git and are not PR/CI acceptance evidence.

1. Create timestamped same-directory backups before editing any JSON or TOML.
2. **Codex:** add a disabled skill override for the obsolete
   `~/.agents/skills/helix-context/SKILL.md`. There is no active Helix MCP block
   to remove. Keep the canonical Cymatix MCP block disabled until `/health`
   succeeds.
3. **Gemini CLI:** persistently disable `helix-context` using the native
   `gemini mcp disable helix-context` command and verify it no longer connects or
   offers tools. Retain the definition and backup for rollback until the
   canonical Cymatix route has passed a smoke test.
4. **Antigravity:** no canonical config contains a Helix entry, so make no raw
   file change. If its MCP manager displays an imported legacy entry, disable it
   in the UI and refresh/restart; do not modify opaque application databases.
5. Do not enable new Cymatix MCP entries while the configured backend health
   endpoint is unavailable.

Rollback is the inverse native enable command or restoration of the timestamped
backup. Permanent deletion of legacy definitions is a later cleanup after a
successful canonical trial.

## 10. Repository change surface

The implementation is expected to touch these cohesive areas:

1. **Skill distribution** — move/replace `skills/cymatix/SKILL.md` with
   `skills/cymatix-context/SKILL.md`; update all active links and install paths.
2. **MCP adapter** — align `cymatix_context`, default caller class, model key,
   validation, and six-tool lean profile while retaining protocol namespace
   `cymatix`.
3. **HTTP context route** — plumb canonical `model` into
   `build_context_async`; accept the documented temporary alias.
4. **Host profiles/status** — add the declarative profile model, native config
   discovery/parsing, separated readiness fields, and text/JSON rendering.
5. **Client docs** — replace the Claude-only routing page with a shared overview
   plus concise Claude Code, Codex, Gemini CLI, and Antigravity guides.
6. **Setup/launcher cleanup** — remove no-op Helix-adoption prose/blocks and
   align the documented server command and MCP dependency range where current
   docs/scripts disagree; correct the PyPI tombstone's stale alias claims while
   retaining its intentional old package name.
7. **Tests** — lock the portable skill, host profiles, model routing, status
   semantics, and active-surface clean break.

Exact file-by-file edits and test order belong in the implementation plan written
after this specification is approved.

## 11. Verification strategy

### 11.1 Skill and MCP contract

- Parse the skill frontmatter; require `name: cymatix-context` and a nonempty,
  host-neutral description.
- Assert every portable-skill tool reference is a member of the lean core, and
  separately assert the lean core is exactly the six specified tools.
- Assert the MCP protocol server continues to identify itself as `cymatix` so
  existing host-visible tool namespaces do not change.
- Assert the default MCP schema exposes `model` and `caller_model_class`, not the
  ineffective `downstream_model` parameter.
- Verify announce remains display-only and is available in the lean profile.

### 11.2 Model behavior

- Parameterize `generic`, `small_moe`, and `frontier` through MCP → `/context` →
  response metadata.
- Verify explicit tool arguments override `CYMATIX_CALLER_MODEL_CLASS`.
- Verify an invalid configured environment value fails MCP startup with the
  allowed values, while an invalid per-call value preserves the `/context`
  error payload through the adapter.
- Verify `model` reaches `build_context_async` as `downstream_model`.
- Verify the packet tool remains model-independent.
- Verify the temporary HTTP alias works and canonical `model` wins when both
  keys are supplied.

### 11.3 Host profiles and status

- Parameterize canonical configurations for Claude Code, Codex, Gemini CLI, and
  Antigravity; all must resolve to the same entry/module/URL contract.
- Verify native disabled states: Codex `enabled=false`, Gemini CLI's enablement
  record, and Antigravity `disabled=true`.
- Verify absent/unknown Claude approval state is not reported as confirmed
  enabled.
- Verify the complete `configured_ready` truth table, including malformed or
  missing Gemini activation state.
- Verify auto-discovery selects one unique workspace match, prefers workspace
  over user scope, and reports multiple matches as `ambiguous`.
- Verify a working non-Claude setup is not marked missing merely because
  `~/.claude/skills` does not exist.
- Verify healthy server + absent launcher remains healthy and can produce
  `configured_ready=true`.
- Verify configured-ready without a skill produces `configured_ready=true` and
  `guided_ready=false`.
- Verify config plus `/health` never produces `mcp.live=connected` without
  positive host/session evidence.

### 11.4 Identity/display

Parameterize:

- `(claude-code, claude-code)` → `Claude Code`;
- `(codex, codex)` → `Codex`;
- `(gemini, gemini-cli)` → `Gemini + Gemini CLI`;
- `(gemini, antigravity)` → `Gemini + Antigravity`.

Explicit host values must survive detection unchanged. Automatic detection is
tested only for intentional environment signals that are documented and present.

### 11.5 Active-surface clean break

Extend the current no-Helix test to current source, setup scripts, client docs,
and the distributed skill. Keep the whitelist small, path-specific, and count-
pinned. Assert no `HELIX_*` reads, `helix_context` imports, Helix CLI
registrations/config fallback, or old-prefix adoption blocks in the included
surface. Separately assert that the PyPI tombstone remains a metadata-only
redirect and no longer promises removed aliases. Historical/archived trees
remain excluded.

### 11.6 End-to-end smoke

With a healthy local server:

1. launch the canonical module over stdio;
2. list the six lean tools;
3. call `cymatix_health`;
4. call `cymatix_announce` with a model id;
5. call `cymatix_context` with an explicit class and confirm response metadata;
6. confirm the session registry stores the expected `agent_kind`, `mcp_host`,
   and `model_id`.

The smoke test uses placeholders/test identities and does not require model-proxy
configuration or the tray. CI structurally validates rendered native config
fixtures; launching each proprietary host is an opt-in manual checklist, not a
required automated test.

## 12. Rollout and rollback

### 12.1 Rollout order

1. Commit and review this design.
2. Write the TDD-oriented implementation plan.
3. Add failing contract tests for model plumbing, tool parity, host profiles,
   status semantics, and active-surface naming.
4. Implement the shared skill/MCP/request contract.
5. Implement host-aware read-only diagnostics and client documentation.
6. Remove stale active Helix adoption blocks.
7. Run targeted and full verification, then review the diff.
8. Disable machine-local Helix integrations using the reversible procedure.
9. Push only the feature branch to `origin` and open a pull request.

### 12.2 Rollback

- Repository rollback is a normal revert of the feature commit(s); no database
  migration is introduced.
- Host configuration rollback restores the backup or uses the native enable
  command.
- The temporary HTTP alias provides a bounded compatibility bridge for callers
  still sending `downstream_model`.
- Historical bundle and launcher read compatibility remain untouched.

## 13. Security and privacy

- Host examples never contain real user identities, home paths, repository
  paths, access tokens, or provider keys.
- Status reads known configuration fields but never prints environment values
  that could be secrets; output may list safe key names and inspected paths.
- Status and host profiles are read-only. No background migration, deletion, or
  config rewrite occurs.
- MCP remains loopback-local by default. This design does not broaden network
  binding, authentication, or remote access.
- Machine-local backups remain beside their source config and are never staged.

## 14. Out of scope

- Reconfiguring any model provider or OpenAI-compatible proxy route.
- Requiring or starting the tray/launcher.
- Automatically starting the Cymatix server from an Agent Skill.
- A universal config-writing installer or mutation of opaque IDE databases.
- Deleting historical Helix documentation, receipts, changelog entries, or
  migration evidence.
- Permanently deleting local legacy definitions before canonical health and tool
  smoke tests pass.
- Changing retrieval quality policy beyond correctly transporting the existing
  `caller_model_class` contract.

## 15. Acceptance criteria

### 15.1 Repository and pull-request acceptance

1. A single tracked, host-neutral `cymatix-context` skill exists. Every active
   setup guide references that canonical source/path; no duplicate tracked
   `SKILL.md` body can drift.
2. Claude Code, Codex, Gemini CLI, and Antigravity have structurally tested native
   config fixtures and manual verification instructions using the same MCP entry,
   stable `cymatix` protocol namespace, Python module, URL variable, identity
   layers, and tool namespace. CI is not required to launch the proprietary
   hosts.
3. The default MCP surface and skill agree exactly on six required tools.
4. `cymatix_context` callers can select the existing caller-model class and
   downstream-model hint; the effective class is validated and echoed.
5. `cymatix-status` reports server, launcher, MCP config/activation, and skill
   installation/activation separately for all four hosts, reports configured
   readiness separately from live connection state, and may correctly return
   `null` where host approval/activation cannot be established read-only.
6. Direct MCP operation is documented and tested as independent of both proxy
   configuration and tray presence.
7. Active code/docs/scripts contain no unapproved Helix naming, while intentional
   historical and read-only migration references remain intact.
8. No machine-local config, backup, identity, secret, or absolute personal path
   appears in the branch or pull request.
9. Targeted tests and the repository verification suite pass apart from any
    explicitly documented, reproducible baseline-only environment failures.

### 15.2 Operator completion for this machine

These checks fulfill the user's local-disable request but are deliberately not
claimed as PR/CI evidence:

1. The obsolete Codex Helix skill is disabled through the documented path-based
   override, Codex is restarted, and the old skill is no longer offered.
2. Gemini CLI reports `helix-context` disabled and no longer exposes its tools.
3. Antigravity receives no raw-file change unless its MCP manager shows an actual
   imported Helix instance; any such instance is disabled through the UI.
4. Timestamped backups exist for every changed local config and remain outside
   Git.
5. Canonical Cymatix MCP entries remain disabled until the server health check
   succeeds; disabling Helix does not imply or require proxy configuration.

## 16. Primary host references

- OpenAI Codex skills: <https://learn.chatgpt.com/docs/build-skills>
- OpenAI Codex MCP: <https://learn.chatgpt.com/docs/extend/mcp>
- Claude Code skills: <https://code.claude.com/docs/en/slash-commands>
- Claude Code MCP: <https://code.claude.com/docs/en/mcp>
- Gemini CLI Agent Skills: <https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/using-agent-skills.md>
- Gemini CLI MCP: <https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md>
- Antigravity Agent Skills: <https://antigravity.google/docs/skills>
- Antigravity MCP: <https://antigravity.google/docs/mcp>
- Antigravity Gemini CLI migration: <https://www.antigravity.google/docs/cli/gcli-migration/>

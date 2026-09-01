# `cymatix-context` Cross-model Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Claude Code, Codex, Gemini CLI, and Google Antigravity one tested `cymatix-context` interaction contract, one canonical Agent Skill, native host configuration guidance, and truthful host-aware diagnostics while retiring active Helix integrations.

**Architecture:** Keep the stable `cymatix` MCP protocol namespace and `cymatix_context` Python module, but move all human-facing integration identity to `cymatix-context`. A declarative, read-only host-profile module owns native config discovery, parsing, activation semantics, and generated snippets; the status CLI combines those facts with independent HTTP probes and active-registry evidence without rewriting host configuration or depending on the model proxy or tray.

**Tech Stack:** Python 3.11+, MCP Python SDK 2.x, FastAPI, Pydantic 2, standard-library `dataclasses`, `json`, `tomllib`, `urllib`, Markdown Agent Skills, pytest, Windows PowerShell for local operator verification.

**Spec:** `docs/superpowers/specs/2026-08-27-cymatix-context-cross-model-unification-design.md`

## Global Constraints

- The human-facing product, Agent Skill, and MCP entry name is exactly `cymatix-context`; the stable MCP protocol server name remains exactly `cymatix`.
- Python imports stay under `cymatix_context`, MCP tools stay under `cymatix_*`, CLI commands stay under `cymatix-*`, and environment variables stay under `CYMATIX_*`.
- The default lean MCP surface is exactly `cymatix_context`, `cymatix_context_packet`, `cymatix_ingest`, `cymatix_health`, `cymatix_sessions_list`, and `cymatix_announce`.
- `cymatix_context` exposes optional `model` and `caller_model_class`; it does not expose `downstream_model`. The packet tool remains model-independent.
- `caller_model_class` accepts only `generic`, `small_moe`, or `frontier`. An invalid `CYMATIX_CALLER_MODEL_CLASS` fails adapter startup; an invalid per-call value preserves the `/context` error payload.
- `/context` accepts `downstream_model` only when the canonical `model` key is absent. The `/v1/chat/completions` invalid-class fallback is unchanged.
- Direct MCP operation remains independent of OpenAI-compatible proxy configuration, and server availability is decided by `GET /health`, not tray presence.
- Host config/status code is read-only, uses Python 3.11 `tomllib`, does not launch proprietary host CLIs, and never prints environment values other than the safe configured handle and host identity.
- Generated config examples use only `<absolute-repo-path>`, `<org>`, `<party-id>`, `<user>`, `<agent-handle>`, canonical host identifiers, and `http://127.0.0.1:11437`; no real identity, home path, secret, or machine-specific absolute path is tracked.
- No new runtime dependency is introduced. HTTP requests retain explicit 10–30 second timeouts. Any new or edited `subprocess` call on Windows includes `creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)`.
- Active code, setup scripts, skills, MCP templates, and current client/setup docs are Cymatix-only except for the path-specific historical/read-only whitelist in the design.
- Machine-local configs and timestamped backups remain outside Git. The canonical Cymatix MCP stays disabled until its configured `/health` endpoint succeeds.
- Run pytest commands that use `tmp_path` outside the restricted sandbox on this Windows machine because the unchanged baseline demonstrated temp-directory ACL failures inside it.

---

## File and interface map

| File | Responsibility |
|---|---|
| `skills/cymatix-context/SKILL.md` | The single authored, host-neutral interaction policy. |
| `cymatix_context/mcp/mcp_server.py` | Stable `cymatix` protocol server, canonical model arguments, configured caller-class validation, HTTP error transport, and six-tool lean profile. |
| `cymatix_context/server/routes_context.py` | Canonical `model` request plumbing, one-window alias support, and effective class response metadata. |
| `cymatix_context/integrations/host_profiles.py` | Immutable host registry, config/skill discovery, native JSON/TOML parsing, activation interpretation, safe config validation, and snippet rendering. |
| `cymatix_context/cli/cymatix_status.py` | Transport/health probes, launcher state, readiness truth tables, active-session matching, report assembly, text/JSON output, and `--host`. |
| `cymatix_context/cli/cmd_status.py` | Compatibility adapter from the richer integration report to the existing genome/config-oriented `cymatix status` network summary. |
| `cymatix_context/launcher/host_labels.py` | Canonical display labels for all four host identities. |
| `docs/clients/cymatix-context.md` | Shared control-plane explanation, identity/model contract, status semantics, and manual smoke/rollback checklist. |
| `docs/clients/{claude-code,codex,gemini-cli,antigravity}.md` | Native install, refresh, verification, disable, and rollback instructions; generated MCP blocks are compared with the registry renderer. |
| `tests/test_skill_contract.py` | Frontmatter, host neutrality, fallback policy, and skill-to-lean-tool parity. |
| `tests/test_mcp_server.py`, `tests/test_mcp_tool_names.py`, `tests/test_mcp_server_entry.py` | Canonical MCP schema/body, startup validation, error preservation, namespace, announce semantics, and lean profile. |
| `tests/test_caller_model_class.py` | MCP-independent `/context` model/class propagation, alias precedence, and response metadata. |
| `tests/test_host_profiles.py` | Four native config shapes, activation sources, discovery order, safe renderers, skill state, and identity mapping. |
| `tests/test_host_status.py`, `tests/test_status.py`, `tests/test_cli_status.py` | Probe mapping, readiness tables, live evidence, complete report schema, CLI parsing/exit behavior, and legacy roll-up compatibility. |
| `tests/test_client_config_snippets.py`, `tests/test_vault_docs.py` | Byte-for-byte generated guide blocks and valid local documentation links. |
| `tests/test_no_helix_leftovers.py` | Deterministic active-surface clean break and narrow historical/migration whitelist. |

## Implementation tasks

### Task 1: Publish the one portable Agent Skill

**Files:**
- Create: `skills/cymatix-context/SKILL.md`
- Create: `tests/test_skill_contract.py`
- Delete: `skills/cymatix/SKILL.md`

**Interfaces:**
- Consumes: the exact six-tool set in the Global Constraints.
- Produces: one `SKILL.md` whose YAML `name` is `cymatix-context`, whose description is host-neutral, and whose only `cymatix_*` tool references are the six lean tools.

- [ ] **Step 1: Load the skill-authoring rules before changing the skill**

Read `skill-creator/SKILL.md` and `superpowers:writing-skills/SKILL.md` completely. Apply their frontmatter, progressive-disclosure, imperative-language, and verification requirements to this task.

- [ ] **Step 2: Write the failing portable-skill contract**

Create `tests/test_skill_contract.py` with these constants and assertions:

```python
from pathlib import Path
import json
import re
import tomllib

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "cymatix-context" / "SKILL.md"
OLD_SKILL = ROOT / "skills" / "cymatix" / "SKILL.md"
LEAN_TOOLS = {
    "cymatix_context",
    "cymatix_context_packet",
    "cymatix_ingest",
    "cymatix_health",
    "cymatix_sessions_list",
    "cymatix_announce",
}


def _read_skill() -> tuple[dict, str]:
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, frontmatter, body = text.split("---", 2)
    return yaml.safe_load(frontmatter), body


def test_only_canonical_skill_path_exists():
    assert SKILL.is_file()
    assert not OLD_SKILL.exists()
    assert list((ROOT / "skills").rglob("SKILL.md")) == [SKILL]


def test_skill_identity_and_description_are_host_neutral():
    metadata, body = _read_skill()
    assert metadata == {
        "name": "cymatix-context",
        "description": (
            "Use Cymatix for architectural, historical, semantic, or cross-file "
            "repository context; verify exact code locally and fall back cleanly "
            "when Cymatix is unavailable."
        ),
    }
    forbidden_hosts = {"claude", "codex", "gemini", "antigravity", "cursor"}
    assert forbidden_hosts.isdisjoint(body.lower().split())


def test_skill_tool_references_equal_the_lean_surface():
    _, body = _read_skill()
    referenced = set(re.findall(r"\bcymatix_[a-z_]+\b", body))
    assert referenced == LEAN_TOOLS


def test_skill_teaches_verification_and_nonblocking_fallback():
    _, body = _read_skill()
    lowered = body.lower()
    assert "read local files" in lowered
    assert "evidence discovery" in lowered
    assert "continue with native tools" in lowered
    assert "does not require the model proxy" in lowered
    assert "does not require the tray" in lowered
    assert "display" in lowered and "does not alter retrieval" in lowered
```

- [ ] **Step 3: Run the contract and confirm the expected red state**

Run: `python -m pytest tests/test_skill_contract.py -q`

Expected: failures because `skills/cymatix-context/SKILL.md` is absent and the old `skills/cymatix/SKILL.md` still exists.

- [ ] **Step 4: Add the canonical skill and remove the old artifact**

Create `skills/cymatix-context/SKILL.md` with this contract, then delete `skills/cymatix/SKILL.md`:

```markdown
---
name: cymatix-context
description: Use Cymatix for architectural, historical, semantic, or cross-file repository context; verify exact code locally and fall back cleanly when Cymatix is unavailable.
---

# Cymatix Context

Use Cymatix as an evidence-discovery layer. It can narrow repository exploration, but it never replaces verification against the current working tree.

## Workflow

1. Call `cymatix_context` first for architectural, historical, semantic, or cross-file questions where compressed context can narrow the search.
2. Call `cymatix_context_packet` when freshness, authority, or edit safety matters.
3. Read local files for exact current code, line references, quoted text, and every edit. Treat Cymatix retrieval as evidence discovery, not source authority.
4. Call `cymatix_ingest` only for meaningful, attributable additions; do not ingest transient thoughts or raw tool output.
5. Call `cymatix_sessions_list` for participant awareness. Session presence does not prove what another participant is currently doing.
6. If a Cymatix call fails while `cymatix_health` remains available, call it to classify backend state. If the adapter or tool list is absent, disabled, or disconnected, continue with native tools.
7. Call `cymatix_announce` once after successful MCP registration when it is exposed. Announcement supplies a display label and does not alter retrieval. If registration failed, continue without it.

## Operating boundaries

- Direct MCP retrieval does not require the model proxy.
- A healthy separately started server does not require the tray.
- Use an explicit `caller_model_class` only when the current model class is known; use `model` only as a downstream budget/model-family hint.
- Never infer retrieval policy from the announcement model label.
- Cymatix may improve a task, but it must not block native repository exploration.
```

- [ ] **Step 5: Run the skill contract and inspect the final artifact**

Run: `python -m pytest tests/test_skill_contract.py -q`

Expected: all tests pass; the tool-reference set is exactly the six lean tools.

- [ ] **Step 6: Commit the portable skill**

```powershell
git add skills/cymatix-context/SKILL.md skills/cymatix/SKILL.md tests/test_skill_contract.py
git commit -m "feat(skills): add portable cymatix-context contract"
```

### Task 2: Align the MCP adapter schema, startup policy, and lean surface

**Files:**
- Modify: `cymatix_context/mcp/mcp_server.py:110-205,359-389,759-791,1026-1080`
- Modify: `tests/test_mcp_server.py`
- Modify: `tests/test_mcp_tool_names.py`
- Modify: `tests/test_mcp_server_entry.py`

**Interfaces:**
- Consumes: `CallerModelClass` and `CALLER_MODEL_CLASS_DEFAULT` from `cymatix_context.schemas`.
- Produces: `_configured_caller_model_class(value: str | None) -> str`, `MCP_CALLER_MODEL_CLASS_DEFAULT: str`, canonical `cymatix_context(query, decoder_mode=None, model=None, caller_model_class=None, session_id=None)`, and `_MCP_CORE_TOOLS` containing exactly six names.

- [ ] **Step 1: Load test-driven-development rules**

Read `superpowers:test-driven-development/SKILL.md` completely. Keep each behavior red until its focused assertion fails for the intended reason.

- [ ] **Step 2: Write failing adapter contract tests**

Update `tests/test_mcp_server.py` and `tests/test_mcp_tool_names.py` with assertions equivalent to:

```python
def test_context_posts_canonical_model_fields(monkeypatch):
    captured = {}

    def fake_http(method, path, body=None):
        captured.update({"method": method, "path": path, "body": body})
        return [{"name": "Cymatix Genome Context", "content": "ok"}]

    monkeypatch.setattr(mcp_server, "_http", fake_http)
    mcp_server.cymatix_context(
        "explain routing",
        model="gpt-5",
        caller_model_class="frontier",
    )
    assert captured["body"]["model"] == "gpt-5"
    assert captured["body"]["caller_model_class"] == "frontier"
    assert "downstream_model" not in captured["body"]


def test_explicit_class_overrides_configured_default(monkeypatch):
    sent = {}
    monkeypatch.setattr(mcp_server, "MCP_CALLER_MODEL_CLASS_DEFAULT", "small_moe")
    monkeypatch.setattr(
        mcp_server,
        "_http",
        lambda _method, _path, body=None: sent.update(body or {}) or [],
    )
    mcp_server.cymatix_context("q", caller_model_class="frontier")
    assert sent["caller_model_class"] == "frontier"


def test_packet_is_model_independent(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        mcp_server,
        "_http",
        lambda _method, _path, body=None: sent.update(body or {}) or {},
    )
    mcp_server.cymatix_context_packet("q")
    assert "model" not in sent
    assert "caller_model_class" not in sent


def test_lean_profile_is_exactly_six_tools():
    assert mcp_server._effective_core_tools() == {
        "cymatix_context",
        "cymatix_context_packet",
        "cymatix_ingest",
        "cymatix_health",
        "cymatix_sessions_list",
        "cymatix_announce",
    }


def test_protocol_namespace_and_context_schema_are_stable():
    assert mcp_server.mcp.name == "cymatix"
    schema = mcp_server.mcp._tool_manager._tools["cymatix_context"].parameters
    assert {"model", "caller_model_class"} <= set(schema["properties"])
    assert "downstream_model" not in schema["properties"]
```

Add a startup-process assertion to `tests/test_mcp_server_entry.py`:

```python
def test_invalid_configured_caller_class_fails_startup(repo_root):
    env = dict(os.environ)
    env["CYMATIX_CALLER_MODEL_CLASS"] = "oversized"
    proc = subprocess.run(
        [sys.executable, "-c", "import cymatix_context.mcp.mcp_server"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "CYMATIX_CALLER_MODEL_CLASS" in combined
    assert all(value in combined for value in ("generic", "small_moe", "frontier"))
```

- [ ] **Step 3: Run the adapter tests and confirm schema/profile failures**

Run: `python -m pytest tests/test_mcp_server.py tests/test_mcp_tool_names.py tests/test_mcp_server_entry.py -q`

Expected: failures show `downstream_model` is still public, `cymatix_announce` is pruned, and invalid configured class does not fail startup.

- [ ] **Step 4: Validate the configured caller class exactly once at import/startup**

In `cymatix_context/mcp/mcp_server.py`, import the schema enum/default and add:

```python
from ..schemas import CALLER_MODEL_CLASS_DEFAULT, CallerModelClass

_CALLER_MODEL_CLASS_ALLOWED = frozenset(item.value for item in CallerModelClass)


def _configured_caller_model_class(value: Optional[str] = None) -> str:
    raw = value if value is not None else os.environ.get("CYMATIX_CALLER_MODEL_CLASS")
    if raw is None or not str(raw).strip():
        return CALLER_MODEL_CLASS_DEFAULT
    normalized = str(raw).strip()
    if normalized not in _CALLER_MODEL_CLASS_ALLOWED:
        allowed = ", ".join(sorted(_CALLER_MODEL_CLASS_ALLOWED))
        raise ValueError(
            "Invalid CYMATIX_CALLER_MODEL_CLASS="
            f"{normalized!r}; allowed values: {allowed}"
        )
    return normalized


MCP_CALLER_MODEL_CLASS_DEFAULT = _configured_caller_model_class()
```

Do not catch this `ValueError` in `main()`: a bad operator configuration must stop the stdio adapter before it advertises tools.

- [ ] **Step 5: Replace the ineffective public argument and preserve HTTP error JSON**

Change the tool signature/body to:

```python
@mcp.tool()
def cymatix_context(
    query: str,
    decoder_mode: Optional[str] = None,
    model: Optional[str] = None,
    caller_model_class: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "query": query,
        "session_id": session_id or MCP_SESSION_ID,
        "caller_model_class": (
            caller_model_class
            if caller_model_class is not None
            else MCP_CALLER_MODEL_CLASS_DEFAULT
        ),
    }
    if decoder_mode:
        body["decoder_mode"] = decoder_mode
    if model is not None:
        body["model"] = model
    if MCP_AGENT_HANDLE:
        body["agent"] = MCP_AGENT_HANDLE
    return _unwrap_context_list(_http("POST", "/context", body))
```

Do not validate the explicit `caller_model_class` in the adapter. In `_http`, parse an `HTTPError` body when it is JSON and merge its keys with `_error: "HTTP <code>"`; retain `_detail` for non-JSON bodies. This keeps `error` and `allowed` visible to the MCP caller.

- [ ] **Step 6: Add announce to the lean profile and clean stale adapter copy**

Add `cymatix_announce` to `_MCP_CORE_TOOLS`, update every five-tool comment/log/docstring to six, and remove duplicated/false compatibility wording. Preserve `mcp = MCPServer("cymatix")`, the full-profile opt-in, and `cymatix_announce`'s existing call to `_registered_bridge.announce(model_id, ide_override)`.

- [ ] **Step 7: Add configured-default and error-envelope assertions**

Add parameterized tests for `generic`, `small_moe`, and `frontier`; a unit test that `_configured_caller_model_class(None)` returns the schema default; an explicit-overrides-environment test; and this error preservation assertion:

```python
def test_http_error_preserves_context_validation_payload(monkeypatch):
    error = urllib.error.HTTPError(
        "http://127.0.0.1:11437/context",
        400,
        "Bad Request",
        {},
        io.BytesIO(
            b'{"error":"Invalid caller_model_class",'
            b'"allowed":["generic","small_moe","frontier"]}'
        ),
    )
    monkeypatch.setattr(urllib.request, "urlopen", Mock(side_effect=error))
    result = mcp_server._http("POST", "/context", {"query": "q"})
    assert result["_error"] == "HTTP 400"
    assert result["error"] == "Invalid caller_model_class"
    assert result["allowed"] == ["generic", "small_moe", "frontier"]
```

- [ ] **Step 8: Run the complete adapter slice**

Run: `python -m pytest tests/test_mcp_server.py tests/test_mcp_tool_names.py tests/test_mcp_server_entry.py -q`

Expected: all tests pass; protocol name is still `cymatix`; default surface is six.

- [ ] **Step 9: Commit the adapter contract**

```powershell
git add cymatix_context/mcp/mcp_server.py tests/test_mcp_server.py tests/test_mcp_tool_names.py tests/test_mcp_server_entry.py
git commit -m "feat(mcp): unify model contract and lean surface"
```

### Task 3: Plumb canonical model identity through `/context`

**Files:**
- Modify: `cymatix_context/server/routes_context.py:152-287,447-457`
- Modify: `tests/test_caller_model_class.py`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: request keys `model`, temporary `downstream_model`, and `caller_model_class`; the existing `CymatixContext.build_context_async` keyword parameters `downstream_model: str | None` and `caller_model_class: str`.
- Produces: canonical-key precedence, effective class in `response["agent"]["caller_model_class"]`, and the unchanged invalid-class `400` envelope.

- [ ] **Step 1: Write failing route propagation tests**

Use the existing `http_client` fixture and add this concrete async spy plus the route assertions:

```python
from unittest.mock import AsyncMock


@pytest.fixture
def context_spy(http_client, monkeypatch):
    manager = http_client.app.state.cymatix
    spy = AsyncMock(wraps=manager.build_context_async)
    monkeypatch.setattr(manager, "build_context_async", spy)
    return spy


@pytest.mark.parametrize(
    ("payload", "expected_model"),
    [
        ({"model": "qwen3:4b"}, "qwen3:4b"),
        ({"downstream_model": "legacy"}, "legacy"),
        ({"model": "canonical", "downstream_model": "legacy"}, "canonical"),
        ({"model": None, "downstream_model": "legacy"}, None),
    ],
)
def test_context_model_key_precedence(http_client, context_spy, payload, expected_model):
    response = http_client.post("/context", json={"query": "q", **payload})
    assert response.status_code == 200
    assert context_spy.call_args.kwargs["downstream_model"] == expected_model


@pytest.mark.parametrize("caller_class", ["generic", "small_moe", "frontier"])
def test_context_echoes_effective_caller_class(http_client, caller_class):
    response = http_client.post(
        "/context",
        json={"query": "q", "caller_model_class": caller_class},
    )
    assert response.status_code == 200
    assert response.json()[0]["agent"]["caller_model_class"] == caller_class


def test_invalid_per_call_class_keeps_route_error(http_client):
    response = http_client.post(
        "/context",
        json={"query": "q", "caller_model_class": "oversized"},
    )
    assert response.status_code == 400
    assert response.json() == {
        "error": "Invalid caller_model_class",
        "allowed": ["generic", "small_moe", "frontier"],
    }


@pytest.mark.parametrize("caller_class", ["generic", "small_moe", "frontier"])
def test_mcp_to_context_round_trip_echoes_class(http_client, monkeypatch, caller_class):
    pytest.importorskip("mcp.server.mcpserver")
    from cymatix_context.mcp import mcp_server

    def loopback(method, path, body=None):
        assert method == "POST"
        response = http_client.post(path, json=body)
        return response.json()

    monkeypatch.setattr(mcp_server, "_http", loopback)
    result = mcp_server.cymatix_context(
        "q",
        model="qwen3:4b",
        caller_model_class=caller_class,
    )
    assert result["agent"]["caller_model_class"] == caller_class
```

- [ ] **Step 2: Run the focused route tests and verify the missing keyword/metadata**

Run: `python -m pytest tests/test_caller_model_class.py tests/test_server.py -q -k "caller_model_class or downstream_model or model_precedence"`

Expected: the spy does not receive `downstream_model`, canonical precedence is absent, and serialized agent metadata lacks the class.

- [ ] **Step 3: Implement exact presence-based alias selection**

Immediately after reading the request JSON, add:

```python
downstream_model = (
    data.get("model")
    if "model" in data
    else data.get("downstream_model")
)
```

Pass `downstream_model=downstream_model` to `build_context_async`. Membership, rather than `or`, is required so a present canonical `null`/empty value still wins over the alias.

- [ ] **Step 4: Serialize the effective class on the established agent metadata surface**

Keep the existing `window.metadata["caller_model_class"]` assignment and add:

```python
response["agent"]["caller_model_class"] = caller_model_class
```

at construction of the `agent` object. Do not change proxy request handling or proxy invalid-class fallback.

- [ ] **Step 5: Run the route and adapter/model regression slice**

Run: `python -m pytest tests/test_caller_model_class.py tests/test_server.py tests/test_mcp_server.py -q -k "context or model or caller or packet"`

Expected: all selected tests pass, including alias precedence and the three class values.

- [ ] **Step 6: Commit request plumbing**

```powershell
git add cymatix_context/server/routes_context.py tests/test_caller_model_class.py tests/test_server.py
git commit -m "feat(context): propagate canonical caller model metadata"
```

### Task 4: Add the declarative four-host profile registry

**Files:**
- Create: `cymatix_context/integrations/host_profiles.py`
- Modify: `cymatix_context/integrations/__init__.py`
- Modify: `cymatix_context/launcher/host_labels.py`
- Create: `tests/test_host_profiles.py`
- Modify: `tests/test_host_labels.py`

**Interfaces:**
- Consumes: workspace root, home root, optional explicit config/skill paths, native JSON/TOML files, and Gemini's `~/.gemini/mcp-server-enablement.json` record.
- Produces: immutable `HOST_PROFILES`, `get_profile`, `discover_profile`, `parse_host_config`, `discover_host_config`, `discover_skill`, `render_mcp_snippet`, and safe typed reports used by Task 5.

- [ ] **Step 1: Write the registry and renderer tests**

Create `tests/test_host_profiles.py` with the four identity rows and source metadata:

```python
from datetime import date
import json
import tomllib

import pytest

from cymatix_context.integrations.host_profiles import (
    HOST_PROFILES,
    render_mcp_snippet,
)

EXPECTED_IDENTITIES = {
    "claude-code": ("Claude Code", "claude-code", "claude-code"),
    "codex": ("Codex", "codex", "codex"),
    "gemini-cli": ("Gemini CLI", "gemini", "gemini-cli"),
    "antigravity": ("Google Antigravity", "gemini", "antigravity"),
}


def test_profiles_are_complete_ordered_and_source_pinned():
    assert [profile.id for profile in HOST_PROFILES] == list(EXPECTED_IDENTITIES)
    for profile in HOST_PROFILES:
        display_name, agent_kind, mcp_host = EXPECTED_IDENTITIES[profile.id]
        assert (profile.display_name, profile.agent_kind, profile.mcp_host) == (
            display_name,
            agent_kind,
            mcp_host,
        )
        assert profile.accessed_on == date(2026, 8, 27)
        assert profile.source_urls


@pytest.mark.parametrize("profile_id", EXPECTED_IDENTITIES)
def test_rendered_snippet_is_native_parseable_and_safe(profile_id):
    rendered = render_mcp_snippet(profile_id)
    body = rendered.split("\n", 1)[1].rsplit("\n```", 1)[0]
    parsed = tomllib.loads(body) if profile_id == "codex" else json.loads(body)
    assert parsed
    assert "cymatix-context" in rendered
    assert "cymatix_context.mcp_server" in rendered
    assert "CYMATIX_MCP_URL" in rendered
    assert "http://127.0.0.1:11437" in rendered
    assert "C:\\Users\\" not in rendered
    assert "/Users/" not in rendered
```

- [ ] **Step 2: Write native config/activation/discovery tests**

Build temporary fixtures with these exact native shapes:

```python
CLAUDE_JSON = {
    "mcpServers": {
        "cymatix-context": {
            "command": "python",
            "args": ["-m", "cymatix_context.mcp_server"],
            "env": {
                "CYMATIX_MCP_URL": "http://127.0.0.1:11437",
                "CYMATIX_MCP_HANDLE": "claude-agent",
                "CYMATIX_MCP_HOST": "claude-code",
            },
        }
    }
}

CODEX_TOML = """
[mcp_servers.cymatix-context]
command = "python"
args = ["-m", "cymatix_context.mcp_server"]
enabled = false

[mcp_servers.cymatix-context.env]
CYMATIX_MCP_URL = "http://127.0.0.1:11437"
CYMATIX_MCP_HANDLE = "codex-agent"
CYMATIX_MCP_HOST = "codex"
"""

GEMINI_JSON = {
    "mcpServers": {
        "cymatix-context": {
            "command": "python",
            "args": ["-m", "cymatix_context.mcp_server"],
            "env": {
                "CYMATIX_MCP_URL": "http://127.0.0.1:11437",
                "CYMATIX_MCP_HANDLE": "gemini-agent",
                "CYMATIX_MCP_HOST": "gemini-cli",
            },
        }
    }
}

ANTIGRAVITY_JSON = {
    "mcpServers": {
        "cymatix-context": {
            "command": "python",
            "args": ["-m", "cymatix_context.mcp_server"],
            "disabled": True,
            "env": {
                "CYMATIX_MCP_URL": "http://127.0.0.1:11437",
                "CYMATIX_MCP_HANDLE": "antigravity-agent",
                "CYMATIX_MCP_HOST": "antigravity",
            },
        }
    }
}
```

Assert:

- all four configurations are `canonical`;
- all four typed results expose `configured_url == "http://127.0.0.1:11437"` for health-target selection;
- Claude activation is `unknown` because file presence cannot prove host approval;
- Codex `enabled = false` is `disabled`, while absent `enabled` is `enabled`;
- Gemini's valid `{"cymatix-context": {"enabled": false}}` record is `disabled`, valid `{}` is `enabled`, and an absent/malformed record is `unknown`;
- Antigravity `disabled: true` is `disabled`, while absent/false is `enabled`;
- noncanonical key/module/URL combinations are distinguished from missing and invalid syntax;
- status-safe data exposes only `handle` and `mcp_host`, never other environment values;
- one workspace match wins, workspace scope precedes user scope, multiple matches at the winning scope return `ambiguous`, and no match returns `unknown`.

- [ ] **Step 3: Run host-profile tests and confirm the module is absent**

Run: `python -m pytest tests/test_host_profiles.py tests/test_host_labels.py -q`

Expected: import/collection failure for `cymatix_context.integrations.host_profiles` and a missing Gemini CLI display mapping.

- [ ] **Step 4: Define the typed, immutable host-profile API**

Create `cymatix_context/integrations/host_profiles.py` around these exact types:

```python
HostId = Literal["claude-code", "codex", "gemini-cli", "antigravity"]
HostSelection = HostId | Literal["unknown", "ambiguous"]
Scope = Literal["workspace", "user"]
ConfigFormat = Literal["json", "toml"]
ConfigState = Literal["canonical", "noncanonical", "missing", "invalid"]
ActivationState = Literal["enabled", "disabled", "unknown"]


@dataclass(frozen=True)
class ConfigCandidate:
    scope: Scope
    relative_path: str


@dataclass(frozen=True)
class HostProfile:
    id: HostId
    display_name: str
    config_format: ConfigFormat
    config_candidates: tuple[ConfigCandidate, ...]
    skill_candidates: tuple[ConfigCandidate, ...]
    agent_kind: str
    mcp_host: str
    source_urls: tuple[str, ...]
    accessed_on: date
    restart_guidance: str
    verify_guidance: str
    activation_record: str | None = None


@dataclass(frozen=True)
class ParsedHostConfig:
    path: Path | None
    profile_id: HostId
    configuration: ConfigState
    activation: ActivationState
    configured_url: str | None
    handle: str | None
    mcp_host: str | None
    detail: str | None = None


@dataclass(frozen=True)
class HostDiscovery:
    selection: HostSelection
    profile: HostProfile | None
    matches: tuple[ParsedHostConfig, ...]
    inspected_paths: tuple[Path, ...]


@dataclass(frozen=True)
class SkillReport:
    installation: Literal["present", "missing"]
    activation: ActivationState
    path: Path
    detail: str | None = None
```

Populate `HOST_PROFILES` in the exact order from the test. Use workspace/user config candidates from the spec: `.mcp.json` and `.claude.json`; `.codex/config.toml`; `.gemini/settings.json`; `.agents/mcp_config.json` and `.gemini/config/mcp_config.json`. Use the documented skill candidates for each host. Record all official URLs and `date(2026, 8, 27)`.

- [ ] **Step 5: Implement canonical parsing, activation, skill discovery, and auto-selection**

Implement these public signatures exactly:

- `get_profile(host: str) -> HostProfile`
- `parse_host_config(path: Path, profile: HostProfile, *, activation_path: Path | None = None) -> ParsedHostConfig`
- `discover_host_config(profile: HostProfile, *, workspace: Path, home: Path, explicit_path: Path | None = None) -> tuple[ParsedHostConfig, tuple[Path, ...]]`
- `discover_profile(*, workspace: Path, home: Path) -> HostDiscovery`
- `profile_for_config_path(path: Path) -> HostProfile | None`
- `discover_skill(profile: HostProfile, *, workspace: Path, home: Path, config_path: Path | None = None, explicit_dir: Path | None = None) -> SkillReport`
- `render_mcp_snippet(profile_id: str) -> str`

The shared canonical validator requires entry key `cymatix-context`, command `python`, args `[-m, cymatix_context.mcp_server]`, and an `env` mapping containing `CYMATIX_MCP_URL`. Extract the configured URL for the internal health target plus `CYMATIX_MCP_HANDLE` and `CYMATIX_MCP_HOST`; never retain or serialize the remaining environment mapping. For every JSON/TOML/config read `except Exception as exc`, call `log.warning("Could not read host config %s: %s", path, exc, exc_info=True)` and return `invalid` with `detail=f"{type(exc).__name__}: {exc}"[:500]`; never echo the raw environment mapping. Apply the same warning/bounded-detail rule to skill override and Gemini activation-record parse failures.

`profile_for_config_path` preserves explicit-path compatibility without process guessing: `.mcp.json` and `.claude.json` select Claude Code, `.codex/config.toml` (or another `.toml`) selects Codex, `.gemini/settings.json` selects Gemini CLI, and `mcp_config.json` selects Antigravity. An unrecognized JSON filename returns no profile, so status can report a clear unknown-host action instead of applying the wrong parser.

Gemini enablement parsing follows Google's upstream `packages/cli/src/config/mcp/mcpServerEnablement.ts` schema exactly (<https://github.com/google-gemini/gemini-cli/blob/main/packages/cli/src/config/mcp/mcpServerEnablement.ts>, checked 2026-08-27): the file is `mcp-server-enablement.json`, keys are normalized server IDs, and `disable()` persists `{"cymatix-context": {"enabled": false}}`. A present valid file with no key means enabled; missing, unreadable, malformed, or structurally invalid data means unknown, as required by the design's conservative read-only diagnostic policy.

For a present Codex skill, compare the resolved `SKILL.md` path against `[[skills.config]]` entries: an exact `enabled = false` match is disabled; an exact true match or a successfully parsed config with no matching override is enabled; an unreadable config makes activation unknown. Other hosts report a discovered skill as enabled because their tracked discovery contract has no separate readable disable record.

- [ ] **Step 6: Render deterministic, secret-free JSON/TOML snippets**

Render a fenced `json` block for Claude Code, Gemini CLI, and Antigravity, and a fenced `toml` block for Codex. Keep key order deterministic and include:

```text
CYMATIX_MCP_URL=http://127.0.0.1:11437
CYMATIX_ORG=<org>
CYMATIX_PARTY_ID=<party-id>
CYMATIX_DEVICE=<party-id>
CYMATIX_USER=<user>
CYMATIX_AGENT=<agent-handle>
CYMATIX_AGENT_KIND=<profile agent_kind>
CYMATIX_MCP_HANDLE=<agent-handle>
CYMATIX_MCP_HOST=<profile mcp_host>
```

Codex uses `enabled = true`; Antigravity uses `disabled: false`. Standard snippets omit `CYMATIX_CALLER_MODEL_CLASS`.

- [ ] **Step 7: Complete identity display mappings**

Add `"gemini-cli": "Gemini CLI"` to `cymatix_context/launcher/host_labels.py` and parameterize these output cases in `tests/test_host_labels.py`:

```python
(
    ("claude-code", "claude-code", "Claude Code"),
    ("codex", "codex", "Codex"),
    ("gemini", "gemini-cli", "Gemini + Gemini CLI"),
    ("gemini", "antigravity", "Gemini + Antigravity"),
)
```

- [ ] **Step 8: Run and commit the complete host-profile slice**

Run: `python -m pytest tests/test_host_profiles.py tests/test_host_labels.py -q`

Expected: all tests pass, including native disable/unknown states and deterministic discovery.

```powershell
git add cymatix_context/integrations/host_profiles.py cymatix_context/integrations/__init__.py cymatix_context/launcher/host_labels.py tests/test_host_profiles.py tests/test_host_labels.py
git commit -m "feat(integrations): add four-host cymatix profiles"
```

### Task 5: Replace collapsed status with host-aware readiness and live evidence

**Files:**
- Modify: `cymatix_context/cli/cymatix_status.py`
- Modify: `cymatix_context/cli/cmd_status.py:155-205`
- Create: `tests/test_host_status.py`
- Modify: `tests/test_status.py`
- Modify: `tests/test_cli_status.py`

**Interfaces:**
- Consumes: Task 4's host-profile/discovery reports, `/health`, optional launcher `/api/state`, and `/sessions?status=active`.
- Produces: `ProbeResult`, `ServerReport`, `McpLiveReport`, `map_server_health`, `map_launcher_state`, `configured_ready`, `guided_ready`, `determine_mcp_live`, and authoritative nested JSON from `collect_status`.

- [ ] **Step 1: Write pure health/readiness/live tests**

Create `tests/test_host_status.py` and parameterize the complete configured-readiness table:

```python
import pytest

import cymatix_context.cli.cymatix_status as status_mod
from cymatix_context.cli.cymatix_status import (
    ProbeResult,
    configured_ready,
    determine_mcp_live,
    guided_ready,
    map_server_health,
)
from cymatix_context.integrations.host_profiles import HostDiscovery

@pytest.mark.parametrize(
    ("configuration", "activation", "health", "expected"),
    [
        ("missing", "enabled", "healthy", False),
        ("noncanonical", "enabled", "healthy", False),
        ("invalid", "enabled", "healthy", False),
        ("canonical", "disabled", "healthy", False),
        ("canonical", "enabled", "healthy", True),
        ("canonical", "enabled", "unhealthy", False),
        ("canonical", "enabled", "unknown", False),
        ("canonical", "unknown", "healthy", None),
        ("canonical", "unknown", "unhealthy", False),
        ("canonical", "unknown", "unknown", False),
    ],
)
def test_configured_ready_truth_table(configuration, activation, health, expected):
    assert configured_ready(
        configuration=configuration,
        activation=activation,
        health=health,
    ) is expected
```

Also assert:

```python
def test_guided_ready_separates_skill_from_mcp():
    assert guided_ready(True, "missing", "unknown") is False
    assert guided_ready(True, "present", "disabled") is False
    assert guided_ready(True, "present", "unknown") is None
    assert guided_ready(None, "present", "enabled") is None
    assert guided_ready(True, "present", "enabled") is True


def test_live_requires_exact_handle_and_host_match():
    registry = ProbeResult(
        transport="reachable",
        payload={
            "participants": [
                {"handle": "codex-agent", "mcp_host": "codex", "status": "active"}
            ]
        },
        parse_error=None,
        error=None,
    )
    assert determine_mcp_live(
        handle="codex-agent",
        configured_host="codex",
        registry=registry,
    ).state == "connected"
    assert determine_mcp_live(
        handle="codex-agent",
        configured_host="gemini-cli",
        registry=registry,
    ).state == "disconnected"
    assert determine_mcp_live(
        handle=None,
        configured_host="codex",
        registry=registry,
    ).state == "unknown"
```

Add these concrete mapper cases; the last case represents a reachable non-JSON response:

```python
@pytest.mark.parametrize(
    ("probe", "transport", "health", "error"),
    [
        (
            ProbeResult("unreachable", None, None, "connection refused"),
            "unreachable",
            "unknown",
            "connection refused",
        ),
        (ProbeResult("reachable", {"status": "ok"}, None, None), "reachable", "healthy", None),
        (
            ProbeResult("reachable", {"status": "degraded"}, None, None),
            "reachable",
            "unhealthy",
            None,
        ),
        (
            ProbeResult("reachable", {"error": "maintenance"}, None, "HTTP 503"),
            "reachable",
            "unhealthy",
            "HTTP 503",
        ),
        (
            ProbeResult("reachable", None, "Expecting value at line 1", None),
            "reachable",
            "unknown",
            None,
        ),
    ],
)
def test_server_health_mapping(probe, transport, health, error):
    report = map_server_health(probe, url="http://127.0.0.1:11437")
    assert (report.transport, report.health, report.error) == (
        transport,
        health,
        error,
    )
    assert report.parse_error == probe.parse_error
```

Add this collect-status test to prove the launcher is informational:

```python
@pytest.mark.parametrize(
    ("launcher_url", "launcher_probe", "expected_state"),
    [
        (
            "http://127.0.0.1:11438",
            ProbeResult("reachable", {"cymatix": {"running": True}}, None, None),
            "running",
        ),
        (
            "http://127.0.0.1:11438",
            ProbeResult("reachable", {"cymatix": {"running": False}}, None, None),
            "stopped",
        ),
        (
            "http://127.0.0.1:11438",
            ProbeResult("unreachable", None, None, "connection refused"),
            "unreachable",
        ),
        (None, None, "not_configured"),
    ],
)
def test_launcher_state_never_gates_readiness(
    monkeypatch,
    tmp_path,
    launcher_url,
    launcher_probe,
    expected_state,
):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "[mcp_servers.cymatix-context]\n"
        'command = "python"\n'
        'args = ["-m", "cymatix_context.mcp_server"]\n'
        "enabled = true\n\n"
        "[mcp_servers.cymatix-context.env]\n"
        'CYMATIX_MCP_URL = "http://127.0.0.1:11999"\n'
        'CYMATIX_MCP_HANDLE = "codex-agent"\n'
        'CYMATIX_MCP_HOST = "codex"\n',
        encoding="utf-8",
    )
    skill = tmp_path / ".agents" / "skills" / "cymatix-context"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: cymatix-context\n---\n", encoding="utf-8")

    seen_urls = []

    def fake_probe(url, timeout_s=status_mod.DEFAULT_STATUS_TIMEOUT_S):
        seen_urls.append(url)
        if url.endswith("/health"):
            return ProbeResult("reachable", {"status": "ok"}, None, None)
        if url.endswith("/api/state"):
            assert launcher_probe is not None
            return launcher_probe
        if url.endswith("/sessions?status=active"):
            return ProbeResult("reachable", {"participants": []}, None, None)
        raise AssertionError(url)

    monkeypatch.setattr(status_mod, "_probe_json", fake_probe)
    result = status_mod.collect_status(
        host="codex",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=launcher_url,
    )
    assert result["launcher"]["state"] == expected_state
    assert "http://127.0.0.1:11999/health" in seen_urls
    assert result["server"]["url"] == "http://127.0.0.1:11999"
    assert result["configured_ready"] is True
    assert result["guided_ready"] is True
```

- [ ] **Step 2: Run the pure status tests and confirm the old collapsed model fails**

Run: `python -m pytest tests/test_host_status.py tests/test_status.py -q`

Expected: missing types/functions and failures against old `integration_ready`/Claude-only config behavior.

- [ ] **Step 3: Implement typed probes and pure truth functions**

In `cymatix_context/cli/cymatix_status.py`, define:

```python
@dataclass(frozen=True)
class ProbeResult:
    transport: Literal["reachable", "unreachable"]
    payload: Mapping[str, Any] | None
    parse_error: str | None
    error: str | None


@dataclass(frozen=True)
class ServerReport:
    url: str
    transport: Literal["reachable", "unreachable"]
    health: Literal["healthy", "unhealthy", "unknown"]
    payload: Mapping[str, Any] | None
    parse_error: str | None
    error: str | None


@dataclass(frozen=True)
class McpLiveReport:
    state: Literal["connected", "disconnected", "unknown"]
    registry_url: str
    reason: str | None
```

Implement these public signatures exactly:

- `map_server_health(probe: ProbeResult, *, url: str) -> ServerReport`
- `map_launcher_state(probe: ProbeResult | None) -> Literal["running", "stopped", "unreachable", "not_configured"]`
- `configured_ready(*, configuration: ConfigState, activation: ActivationState, health: Literal["healthy", "unhealthy", "unknown"]) -> bool | None`
- `guided_ready(configured: bool | None, installation: Literal["present", "missing"], activation: ActivationState) -> bool | None`
- `determine_mcp_live(*, handle: str | None, configured_host: str | None, registry: ProbeResult, registry_url: str = "") -> McpLiveReport`

Replace `_get_json` with `_probe_json(url: str, timeout_s: float = DEFAULT_STATUS_TIMEOUT_S) -> ProbeResult`. It always calls `urllib.request.urlopen(url, timeout=timeout_s)`. On success, decode and parse JSON. On `HTTPError`, read the bounded body once, parse JSON when valid, and return `transport="reachable"` with `error=f"HTTP {exc.code}"`; on invalid JSON retain the parse exception text. On `URLError`, return `transport="unreachable"` with `error=str(exc.reason)`. On any other `Exception`, call `log.warning("Status probe failed for %s: %s", url, exc, exc_info=True)` and return unreachable with its exception type/message.

Implement health mapping with this exact branch order:

```python
def map_server_health(probe: ProbeResult, *, url: str) -> ServerReport:
    if probe.transport == "unreachable":
        health = "unknown"
    elif probe.payload is None:
        health = "unknown"
    elif probe.payload.get("status") == "ok":
        health = "healthy"
    else:
        health = "unhealthy"
    return ServerReport(
        url=url,
        transport=probe.transport,
        health=health,
        payload=probe.payload,
        parse_error=probe.parse_error,
        error=probe.error,
    )
```

Implement `map_launcher_state` with `None -> "not_configured"`, unreachable transport or a missing/non-mapping `cymatix` object to `"unreachable"`, `payload["cymatix"]["running"] is True -> "running"`, and any other readable boolean running value to `"stopped"`. This helper never receives or changes MCP/skill/server readiness inputs.

Implement the truth table exactly. `determine_mcp_live` must require a nonempty configured handle, a nonempty configured host, a successfully parsed `participants` list, and exact equality on both fields. No exact match after a successful registry read is `disconnected`; all missing/unreadable/ambiguous evidence is `unknown`.

- [ ] **Step 4: Assemble the authoritative host-aware report**

Use the exact public signature `collect_status(*, host: str = "auto", server_url: str | None = None, launcher_url: str | None = DEFAULT_LAUNCHER_URL, mcp_config: Path | None = None, skill_dir: Path | None = None, start_dir: Path | None = None, home_dir: Path | None = None) -> dict[str, Any]`.

If `host="auto"`, call `discover_profile`; an explicit host bypasses auto-selection. When `mcp_config` is supplied in auto mode, use Task 4's path-based selector first. Recognized native filenames preserve the existing explicit-path override; an unrecognized JSON filename produces an unknown-host report with a specific instruction to pass `--host`, rather than silently applying the wrong JSON host semantics. Return this stable JSON shape:

Handle unselected hosts before dereferencing profile-specific reports:

- `selection="unknown"`: set `mcp.configuration="missing"`, `mcp.activation="unknown"`, `mcp.live="unknown"`, `skill.installation="missing"`, `skill.activation="unknown"`, both readiness values false, and next action `Pass --host or add one canonical cymatix-context MCP entry.`
- `selection="ambiguous"`: set `mcp.configuration="invalid"`, both MCP activation/live unknown, skill missing/unknown, both readiness values false, include every matching/inspected path, and next action `Multiple host configs contain cymatix-context; pass --host explicitly.`

These branches still return the independently probed server and launcher objects, using `server_url or DEFAULT_SERVER_URL` because no configured target can be selected. They do not instantiate a `ParsedHostConfig` with a fake `profile_id`, call `discover_skill`, or query the session registry.

For a selected host, an explicit `server_url` takes precedence over parsed config
and is the operator opt-in for a validated remote diagnostic target. Without an
explicit URL, use `parsed.configured_url` only after validating it as an
absolute HTTP(S) loopback base URL; `localhost`, IPv4 loopback, and IPv6
loopback are accepted without DNS resolution. Reject malformed, credential-
bearing, query/fragment-bearing, and implicit non-loopback URLs before calling
`_probe_json`; return bounded unprobed/unknown server and live evidence with a
next action that tells the operator to pass `--server-url` explicitly. Validate
launcher URLs before probing as well, and redact userinfo/query/fragment from
all displayed server and launcher URLs. Bound both successful and HTTP-error
response reads. This status-only policy does not alter the configured MCP
runtime endpoint or Task 4 parsing/storage.

An explicit URL is only a health diagnostic override. For a selected host,
compare it with `ParsedHostConfig.configured_url` using lowercase scheme/host,
normalized IP literal, effective port, and normalized trailing slash/path,
without DNS equivalence. Report `server.source` and
`server.configured_url_match` (`true`, `false`, or `null` when no valid
target/config comparison can be made). Only a canonical match may fetch
`/sessions`, determine MCP live state, or feed readiness. A mismatched explicit
health result remains
diagnostic-only: skip sessions, return live/readiness unknown/null except for
independent definitive false conditions, exit nonzero, and instruct the
operator to update config or probe the matching configured URL.

```python
{
    "host": {
        "selection": selection,
        "profile": display_name_or_none,
        "inspected_paths": [str(path) for path in inspected_paths],
    },
    "server": {
        "url": reported_server_url,
        "source": "configured" | "explicit" | "default",
        "configured_url_match": true | false | null,
        "transport": server_report.transport,
        "health": server_report.health,
        "payload": server_report.payload,
        "parse_error": server_report.parse_error,
        "error": server_report.error,
    },
    "launcher": {"url": launcher_url, "state": launcher_state},
    "mcp": {
        "configuration": parsed.configuration,
        "activation": parsed.activation,
        "live": live.state,
        "path": str(parsed.path) if parsed.path else None,
        "detail": parsed.detail,
    },
    "skill": {
        "installation": skill.installation,
        "activation": skill.activation,
        "path": str(skill.path),
        "detail": skill.detail,
    },
    "configured_ready": configured,
    "guided_ready": guided,
    "next_action": next_action,
}
```

The health payload is unmodified. Do not include config environment dictionaries. Fetch `/sessions?status=active` only for live evidence and do not let a registry failure change `configured_ready`.

- [ ] **Step 5: Update CLI parsing, text output, and exit semantics**

Add `--server-url` with `default=None`, then add:

```python
parser.add_argument(
    "--host",
    choices=("auto", "claude-code", "codex", "gemini-cli", "antigravity"),
    default="auto",
)
```

Retain `--mcp-config`, `--skill-dir`, and `--json`. Text output names each independent state. `main()` returns zero only when `status["configured_ready"] is True`; false or null returns one. Next actions prioritize: unknown/ambiguous host, invalid/noncanonical/missing config, disabled/unknown activation, unhealthy server, missing/disabled/unknown skill, disconnected/unknown live evidence.

- [ ] **Step 6: Preserve the separate `cymatix status` contract**

In `cymatix_context/cli/cmd_status.py`, map the new integration report to its established compact network keys:

```python
report["server"] = {
    "reachable": (
        net["server"]["transport"] == "reachable"
        and net["server"]["health"] == "healthy"
    ),
    "url": net["server"]["url"],
}
report["launcher"] = {
    "reachable": net["launcher"]["state"] in {"running", "stopped"},
    "url": net["launcher"]["url"],
}
```

Update the two network fakes in `tests/test_cli_status.py` to return the new nested fields. Do not change the genome/config exit-code contract of `cymatix status`.

- [ ] **Step 7: Add CLI/report integration cases**

Rewrite duplicated pseudo-legacy tests in `tests/test_status.py`. Add four profile cases, Codex disabled, Gemini unknown activation, Antigravity disabled, auto ambiguity, non-Claude skill discovery, missing skill with `configured_ready=True`/`guided_ready=False`, healthy server with absent launcher, and config+health with no session evidence yielding `mcp.live="unknown"` rather than connected.

Lock the two unselected branches with:

```python
@pytest.mark.parametrize(
    ("selection", "configuration", "action_fragment"),
    [
        ("unknown", "missing", "Pass --host"),
        ("ambiguous", "invalid", "Multiple host configs"),
    ],
)
def test_collect_status_handles_unselected_host(
    monkeypatch,
    tmp_path,
    selection,
    configuration,
    action_fragment,
):
    monkeypatch.setattr(
        status_mod,
        "discover_profile",
        lambda **_kwargs: HostDiscovery(selection, None, (), (tmp_path,)),
    )
    seen_urls = []

    def fake_probe(url, timeout_s=status_mod.DEFAULT_STATUS_TIMEOUT_S):
        seen_urls.append(url)
        return ProbeResult("reachable", {"status": "ok"}, None, None)

    monkeypatch.setattr(status_mod, "_probe_json", fake_probe)
    result = status_mod.collect_status(start_dir=tmp_path, home_dir=tmp_path)
    assert result["host"]["selection"] == selection
    assert result["mcp"]["configuration"] == configuration
    assert result["configured_ready"] is False
    assert result["guided_ready"] is False
    assert action_fragment in result["next_action"]
    assert not any("/sessions" in url for url in seen_urls)
```

In `tests/test_cli_status.py`, assert `--host`, JSON null preservation, recognized explicit-config path selection, the clear `--host` action for an unrecognized JSON path, concise text labels, and zero exit only for `configured_ready is True`.

- [ ] **Step 8: Run and commit the status slice**

Run: `python -m pytest tests/test_host_status.py tests/test_status.py tests/test_cli_status.py -q`

Expected: all tests pass with no dependency on `~/.claude` for non-Claude profiles.

```powershell
git add cymatix_context/cli/cymatix_status.py cymatix_context/cli/cmd_status.py tests/test_host_status.py tests/test_status.py tests/test_cli_status.py
git commit -m "feat(status): report host readiness and live MCP state"
```

### Task 6: Generate synchronized cross-model client documentation

**Files:**
- Create: `docs/clients/cymatix-context.md`
- Modify: `docs/clients/claude-code.md`
- Create: `docs/clients/codex.md`
- Create: `docs/clients/gemini-cli.md`
- Create: `docs/clients/antigravity.md`
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/SETUP.md`
- Modify: `docs/TROUBLESHOOTING.md`
- Modify: `pyproject.toml:49-52`
- Create: `tests/test_client_config_snippets.py`
- Modify: `tests/test_vault_docs.py`

**Interfaces:**
- Consumes: `HOST_PROFILES`, `render_mcp_snippet(profile_id)`, the canonical skill path, and the status JSON semantics.
- Produces: one shared conceptual guide and four native opt-in guides whose marked config blocks are byte-identical to generated snippets.

- [ ] **Step 1: Write failing documentation synchronization tests**

Create `tests/test_client_config_snippets.py`:

```python
from pathlib import Path
import re

import pytest

from cymatix_context.integrations.host_profiles import render_mcp_snippet

ROOT = Path(__file__).resolve().parents[1]
GUIDES = {
    "claude-code": ROOT / "docs/clients/claude-code.md",
    "codex": ROOT / "docs/clients/codex.md",
    "gemini-cli": ROOT / "docs/clients/gemini-cli.md",
    "antigravity": ROOT / "docs/clients/antigravity.md",
}


def _generated_block(text: str, profile_id: str) -> str:
    start = f"<!-- BEGIN GENERATED MCP: {profile_id} -->"
    end = f"<!-- END GENERATED MCP: {profile_id} -->"
    return text.split(start, 1)[1].split(end, 1)[0].strip()


@pytest.mark.parametrize("profile_id", GUIDES)
def test_client_guide_mcp_block_matches_renderer(profile_id):
    text = GUIDES[profile_id].read_text(encoding="utf-8")
    assert _generated_block(text, profile_id) == render_mcp_snippet(profile_id)


@pytest.mark.parametrize("path", GUIDES.values())
def test_examples_have_no_machine_local_values(path):
    text = path.read_text(encoding="utf-8")
    personal_paths = (
        r"[A-Za-z]:\\Users\\(?!<)[^\\\s]+\\",
        r"/Users/(?!<)[^/\s]+/",
        r"/home/(?!<)[^/\s]+/",
        r"[A-Za-z]:\\Projects\\(?!<)[^\\\s]+",
    )
    assert not any(re.search(pattern, text) for pattern in personal_paths)
    assert not re.search(
        r"\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_-]{12,}|"
        r"github_pat_[A-Za-z0-9_-]{12,})\b",
        text,
    )
    assert not re.search(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*"
        r"[\"']?(?!<)[^\s\"']+",
        text,
    )
    assert "HELIX_" not in text
    assert "helix_context" not in text

    profile_id = next(key for key, value in GUIDES.items() if value == path)
    generated = _generated_block(text, profile_id)
    body = generated.split("\n", 1)[1].rsplit("\n```", 1)[0]
    if profile_id == "codex":
        parsed = tomllib.loads(body)
        env = parsed["mcp_servers"]["cymatix-context"]["env"]
    else:
        parsed = json.loads(body)
        env = parsed["mcpServers"]["cymatix-context"]["env"]
    allowed_values = {
        "http://127.0.0.1:11437",
        "<org>",
        "<party-id>",
        "<user>",
        "<agent-handle>",
        "claude-code",
        "codex",
        "gemini",
        "gemini-cli",
        "antigravity",
    }
    assert set(env.values()) <= allowed_values
```

Extend the local-link test to all five client guides.

- [ ] **Step 2: Run docs tests and confirm missing guides/blocks**

Run: `python -m pytest tests/test_client_config_snippets.py tests/test_vault_docs.py -q`

Expected: the three new host guides and shared overview are absent; Claude's existing guide has no generated block.

- [ ] **Step 3: Write the shared control-plane guide**

Create `docs/clients/cymatix-context.md` with these sections and exact claims:

1. “What `cymatix-context` is”: one Agent Skill plus one stdio MCP adapter, not a universal model proxy.
2. A five-plane table for skill, adapter, server, optional proxy, and optional tray.
3. Canonical naming table from the design.
4. The eight identity variables and the four profile identity rows.
5. The distinct meanings of `model`, `caller_model_class`, and `model_id`.
6. The six lean tools and `CYMATIX_MCP_FULL=1` boundary.
7. Status dimensions/readiness truth table, explicitly stating that config plus health cannot prove a live host connection.
8. Failure fallback: health if callable, then native repository tools; Cymatix never blocks the task.
9. Manual smoke steps: start a healthy server without requiring tray/proxy, start the canonical module over stdio, list six tools, call health, announce, context with explicit class, and verify exact handle/host in `/sessions?status=active`.
10. Rollback: native disable/enable or restore timestamped backup.

- [ ] **Step 4: Write each native host guide around its generated block**

Each guide must contain, in order:

- official skill/MCP source links and “accessed 2026-08-27”;
- supported workspace/user skill destinations and an explicit copy command from `skills/cymatix-context/SKILL.md`;
- the exact markers `<!-- BEGIN GENERATED MCP: <profile-id> -->` and `<!-- END GENERATED MCP: <profile-id> -->` around `render_mcp_snippet(<profile-id>)` output;
- native activation semantics;
- refresh/restart instructions;
- native verification instructions;
- `cymatix-status --host <profile-id> --json` and explanation of null activation/live values;
- native reversible disable/rollback instructions;
- a statement that the proxy and tray are optional for direct MCP.

Use these host-specific commands:

| Host | Verify/refresh | Reversible disable |
|---|---|---|
| Claude Code | `claude mcp get cymatix-context`, `claude mcp list`, or `/mcp`; restart after project approval/config changes | host-native `/mcp` disable/rejection; file presence alone remains activation unknown |
| Codex | restart Codex after config/skill changes | `enabled = false` for MCP; `[[skills.config]]` with absolute `SKILL.md` path and `enabled = false` for a skill |
| Gemini CLI | `gemini mcp list`, `/mcp`, `/skills reload`, `/skills list` | `gemini mcp disable cymatix-context`; rollback with `gemini mcp enable cymatix-context` |
| Antigravity | MCP manager Refresh/Manage Servers and application restart | `disabled: true` or UI disable; rollback with UI enable/`disabled: false` |

- [ ] **Step 5: Update active entry points and setup/troubleshooting wording**

Link the shared overview and four host guides from `README.md`, `CLAUDE.md`, and `docs/SETUP.md`. Replace Claude/Cursor-only MCP-extra prose in `pyproject.toml` with all supported hosts. Use `python -m uvicorn cymatix_context._asgi:app --host 127.0.0.1 --port 11437` as the canonical server command everywhere; explain that `cymatix_context.server:app` is a compatibility import target only where existing runtime behavior requires it.

Correct tray claims: `launcher-tray` is required for the tray UI; a headless/backend process can still provide a healthy server. Never promise a tray fallback that the launcher does not implement. State that unavailable tray dependencies do not make an already healthy server unavailable.

- [ ] **Step 6: Run documentation, package-reference, and skill-link tests**

Run: `python -m pytest tests/test_client_config_snippets.py tests/test_vault_docs.py tests/test_skill_contract.py tests/test_config_reference_sync.py tests/test_packaging.py -q`

Expected: all tests pass; generated blocks are byte-identical; every local link resolves.

- [ ] **Step 7: Commit cross-model documentation**

```powershell
git add docs/clients/cymatix-context.md docs/clients/claude-code.md docs/clients/codex.md docs/clients/gemini-cli.md docs/clients/antigravity.md README.md CLAUDE.md docs/SETUP.md docs/TROUBLESHOOTING.md pyproject.toml tests/test_client_config_snippets.py tests/test_vault_docs.py
git commit -m "docs(clients): unify cymatix-context host guidance"
```

### Task 7: Enforce the active-surface Helix clean break

**Files:**
- Modify: `tests/test_no_helix_leftovers.py`
- Modify: `start-cymatix-mcpo.bat`
- Modify: `start-cymatix-tray.bat`
- Modify: `backend-with-otel.bat`
- Modify: `launcher-with-otel.bat`
- Modify: `deploy/windows/setup-cymatix.ps1`
- Modify: `deploy/pypi-tombstone/README.md`
- Modify: `deploy/pypi-tombstone/pyproject.toml`

**Interfaces:**
- Consumes: the design's exact active-surface include/exclude list and intentional-reference whitelist.
- Produces: deterministic lint coverage for current code/docs/scripts/skills, no active old-prefix adoption blocks, and an accurate metadata-only PyPI redirect.

- [ ] **Step 1: Extend the failing active-surface lint**

Keep the existing independent assertions for imports, console scripts, environment mirrors, config fallback, and protocol name. Add deterministic iterators for:

```python
ACTIVE_DOCS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "docs/SETUP.md",
    REPO_ROOT / "docs/TROUBLESHOOTING.md",
    *sorted((REPO_ROOT / "docs/clients").glob("*.md")),
    *sorted((REPO_ROOT / "docs/api").rglob("*.md")),
    *sorted((REPO_ROOT / "docs/ops").rglob("*.md")),
)
ACTIVE_SCRIPTS = tuple(
    path
    for root in (REPO_ROOT, REPO_ROOT / "deploy")
    for suffix in ("*.bat", "*.ps1", "*.sh")
    for path in sorted(root.glob(suffix) if root == REPO_ROOT else root.rglob(suffix))
)
ACTIVE_SKILLS = tuple(
    path for path in sorted((REPO_ROOT / "skills").rglob("*")) if path.is_file()
)
```

Exclude `CHANGELOG.md`, `docs/archive/**`, `docs/superpowers/**`, benchmark data/receipts, historical fixtures, and `deploy/pypi-tombstone/**` from the generic lint. Whitelist/count-pin only:

- the bounded migration notice/table in `README.md`;
- the clean-break notes in `CLAUDE.md`;
- `deploy/windows/setup-cymatix.ps1` cleanup of this project's obsolete `Helix.lnk`;
- package source `cross_store_import.py` (`helix_format_version`) and `launcher/genome_registry.py` (`.helix`/`helix-era`).

Pin the expected case-insensitive line counts at 15 for `README.md`, 4 for `CLAUDE.md`, 3 for `deploy/windows/setup-cymatix.ps1`, 2 for `cross_store_import.py`, and 4 for `launcher/genome_registry.py`. Any deliberate wording change must update the path-specific fragments and count together.

Add a script assertion rejecting case-insensitive `adopt old prefix`, `legacy prefix`, any `HELIX_` variable, and a same-name `CYMATIX_*` self-assignment. Add `creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)` to the existing fresh-interpreter `subprocess.run` call.

- [ ] **Step 2: Add the PyPI tombstone truth test**

Assert its package metadata still has `name = "helix-context"`, depends on `cymatix-context`, declares no packages/console scripts, and its prose explicitly says current Cymatix does not provide the removed `helix_context`, `helix*`, `HELIX_*`, or `helix.toml` aliases.

- [ ] **Step 3: Run the clean-break test and capture intended failures**

Run: `python -m pytest tests/test_no_helix_leftovers.py -q`

Expected: failures identify the four stale batch-script comments/blocks and false tombstone alias promises; existing bounded migration/read-only references remain accepted.

- [ ] **Step 4: Remove only stale active adoption blocks**

Make these exact edits:

- delete the no-op old-prefix mirror comments/assignments in `backend-with-otel.bat` and `launcher-with-otel.bat`;
- delete the old-prefix adoption block and related messages in `start-cymatix-mcpo.bat` while retaining normal defaults;
- delete the no-op mirror comment and self-assignment in `start-cymatix-tray.bat` while retaining legitimate defaulting and headroom process-adoption behavior;
- preserve `deploy/windows/setup-cymatix.ps1`'s path-specific obsolete shortcut cleanup;
- change the Windows installer MCP extra from `mcp>=1.0` to `mcp>=2,<3` so it agrees with `pyproject.toml`.

- [ ] **Step 5: Correct the tombstone without changing its redirect identity**

Rewrite the tombstone README/comments to say the old distribution only installs `cymatix-context`; old imports, CLIs, environment variables, and config names were removed in Cymatix 0.8.5. Preserve the old project/package name, dependency redirect, and publication instructions. Do not add code, packages, entry points, or compatibility aliases.

- [ ] **Step 6: Run clean-break and packaging/setup regressions**

Run: `python -m pytest tests/test_no_helix_leftovers.py tests/test_install_observability.py tests/test_packaging.py tests/test_config_reference_sync.py -q`

Expected: the clean-break, packaging, and config-reference assertions pass. `test_bash_install_script_parses` may remain the recorded environment-only exception only when its output again shows WSL translating away the Windows worktree path and failing to locate the unchanged script. Re-run that node alone outside the worktree-path translation boundary when possible; do not weaken the test or classify a different signature as baseline-only.

- [ ] **Step 7: Commit the retirement cleanup**

```powershell
git add tests/test_no_helix_leftovers.py start-cymatix-mcpo.bat start-cymatix-tray.bat backend-with-otel.bat launcher-with-otel.bat deploy/windows/setup-cymatix.ps1 deploy/pypi-tombstone/README.md deploy/pypi-tombstone/pyproject.toml
git commit -m "chore(migration): retire active Helix setup surfaces"
```

### Task 8: Run integrated verification and independent review

**Files:**
- Modify only files implicated by a verified regression.

**Interfaces:**
- Consumes: all prior commits and the unchanged baseline record (`4186 passed, 44 skipped, 2 environment-only failures`).
- Produces: focused green slices, a full-suite comparison, a clean diff, and spec/code quality reviews before local machine mutation or PR publication.

- [ ] **Step 1: Run the focused contract matrix outside the restricted temp sandbox**

Run:

```powershell
python -m pytest tests/test_skill_contract.py tests/test_mcp_tool_names.py tests/test_mcp_server.py tests/test_mcp_server_entry.py -q
python -m pytest tests/test_caller_model_class.py tests/test_server.py -q -k "context or packet or model or caller"
python -m pytest tests/test_host_profiles.py tests/test_host_labels.py tests/test_host_status.py tests/test_status.py tests/test_cli_status.py -q
python -m pytest tests/test_client_config_snippets.py tests/test_vault_docs.py tests/test_no_helix_leftovers.py tests/test_packaging.py tests/test_config_reference_sync.py -q
```

Expected: every selected test passes.

- [ ] **Step 2: Run the full non-live suite**

Run: `python -m pytest tests/ -m "not live" -q`

Expected: no new failure relative to baseline. Re-run the two known baseline failures individually to distinguish unchanged environment conditions:

```powershell
python -m pytest tests/test_install_observability.py::test_bash_install_script_parses -q
python -m pytest tests/test_no_helix_leftovers.py::test_cymatix_console_scripts_present -q
```

The first may still fail because WSL strips the Windows worktree path. The second may still fail when the checkout is not installed editable. Report them as baseline-only only if the failure signatures match the recorded baseline exactly.

- [ ] **Step 3: Perform the implementation-plan self-audit against the saved design**

Check every design acceptance criterion against a test or manual operator step. Run these read-only scans over changed tracked files:

```powershell
$cymatixChangedFiles = git diff --diff-filter=ACMR --name-only origin/main...HEAD
git grep -n -I -E '([A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\|/Users/[A-Za-z0-9._-]+/|/home/[A-Za-z0-9._-]+/)' HEAD -- $cymatixChangedFiles
git grep -n -I -i -E '(api[_-]?key|access[_-]?token|password|secret)[[:space:]]*[:=][[:space:]]*[^<[:space:]]' HEAD -- $cymatixChangedFiles
git diff --name-only origin/main...HEAD | Select-String -Pattern '(\.bak-|\.mcp\.json$|\.codex/config\.toml$|\.gemini/settings\.json$|\.agents/mcp_config\.json$)'
```

Expected: no actual personal Windows/macOS/Linux home path, non-placeholder secret assignment, backup, or host-local config file. The generic regex source in privacy tests is not an actual path and must not be weakened merely to silence a scan. Also inspect generated environment values through `tests/test_client_config_snippets.py`, which restricts identities to the canonical profile values and documented placeholders.

- [ ] **Step 4: Request two-stage review**

Use `superpowers:requesting-code-review` for a spec-compliance review and then a code-quality/security review. Correct verified issues with new commits; rerun the smallest affected slice and then the full focused matrix.

- [ ] **Step 5: Verify branch hygiene**

Run:

```powershell
git status --short
git log --oneline --decorate -10
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
```

Expected: no uncommitted files, no whitespace errors, no machine-local artifacts, and only scoped changes.

### Task 9: Disable obsolete machine-local Helix integrations reversibly

**Files outside Git:**
- Inspect/backup/modify: `~/.codex/config.toml`
- Inspect/backup through native CLI: `~/.gemini/settings.json`, `~/.gemini/mcp-server-enablement.json`
- Inspect only: `~/.gemini/config/mcp_config.json` and relevant Antigravity workspace config.

**Interfaces:**
- Consumes: explicit prior user approval, native host mechanisms, and current backend `/health` state.
- Produces: timestamped same-directory backups for every changed file, disabled Codex obsolete skill, persistently disabled Gemini `helix-context`, no unjustified Antigravity raw-file mutation, and rollback instructions.

- [ ] **Step 1: Re-inspect exact local targets and health before mutation**

Read only the relevant Codex `[[skills.config]]` records, Gemini MCP server names/enablement entries, and Antigravity `mcpServers` keys. Confirm the installed Gemini CLI's enablement manager still matches Google's pinned source schema before mutation; if it has changed, stop this local step and update the parser/plan rather than guessing. Probe the configured Cymatix `/health` with an explicit 10-second timeout. Do not print environment values or unrelated config content.

- [ ] **Step 2: Create same-directory timestamped backups for files that will change**

Resolve each absolute source path first and verify it is under the current user's `.codex` or `.gemini` directory. Copy only the Codex TOML and Gemini enablement file/settings file that the subsequent native operation actually changes, using a suffix like `.bak-20260827-HHmmss`. Do not stage these files.

- [ ] **Step 3: Disable the obsolete Codex skill without deleting it**

Ensure `~/.codex/config.toml` contains one exact path override, replacing the value below with the resolved absolute path to the existing legacy skill:

```toml
[[skills.config]]
path = "<absolute-path-to-legacy-skill>/SKILL.md"
enabled = false
```

Preserve all existing TOML and keep the canonical `[mcp_servers.cymatix-context]` block disabled while `/health` is unavailable. Restart Codex and verify the old skill is no longer offered.

Make the edit idempotent. Parse the pre-edit TOML with `tomllib`; resolve each `[[skills.config]].path`; and compare it with the resolved legacy `SKILL.md`. If no exact record exists, append one canonical record. If one exists, change only its `enabled` field to false. If multiple exact/conflicting records exist, use the timestamped backup, remove those duplicate blocks, and append one canonical disabled record while leaving every unrelated record byte-for-byte unchanged. Parse the result again and assert exactly one resolved-path match with `enabled is False`. If the source TOML is malformed, do not edit it.

- [ ] **Step 4: Disable Gemini's legacy server through its native command**

Run:

```powershell
gemini mcp disable helix-context
gemini mcp list
```

Expected: native command output and the verified upstream schema produce `~/.gemini/mcp-server-enablement.json` containing `{"helix-context": {"enabled": false}}`, and `helix-context` no longer connects/offers tools. If native output differs from the source-validated schema, stop rather than rewriting it by hand. Retain the server definition for rollback. Rollback command: `gemini mcp enable helix-context`.

- [ ] **Step 5: Leave Antigravity unchanged unless positive UI evidence exists**

If the canonical global/workspace `mcpServers` maps still contain no legacy entry, make no file change. If Antigravity's MCP manager visibly lists an imported legacy instance, disable it in the UI, refresh/restart, and record the UI-only rollback path; do not modify opaque application databases.

- [ ] **Step 6: Verify local completion and Git isolation**

Confirm backups exist beside every changed config, Codex's obsolete skill override is disabled, Gemini reports the legacy server disabled, Antigravity raw files were untouched absent positive evidence, and `git status --short` remains clean. Do not enable canonical Cymatix MCP while `/health` is unavailable.

### Task 10: Push only the feature branch and open the PR

**Files:**
- No new files; publish the reviewed commits only.

**Interfaces:**
- Consumes: verified clean branch `codex/cymatix-context-cross-model`, full test results, baseline exception evidence, and local completion summary.
- Produces: one push to `origin` and a pull request with scope, tests, migration safety, and known environment-only baseline notes.

- [ ] **Step 1: Load completion skills**

Read `superpowers:verification-before-completion/SKILL.md` and `superpowers:finishing-a-development-branch/SKILL.md` completely. Do not claim success from stale test output.

- [ ] **Step 2: Re-run the final verification commands required by those skills**

At minimum rerun the focused matrix, `git diff --check origin/main...HEAD`, and `git status --short`. Record exact pass/fail counts and timestamps for the PR body.

- [ ] **Step 3: Push only to the private `origin` remote**

```powershell
git push -u origin codex/cymatix-context-cross-model
```

Do not force-push and do not push to any other remote.

- [ ] **Step 4: Open the PR**

Use a title equivalent to `feat: unify cymatix-context across agent hosts`. The body must include:

- the one-skill/one-adapter/four-native-profile architecture;
- stable `cymatix` protocol namespace and six-tool lean surface;
- model/class request changes and compatibility alias boundary;
- status readiness/live distinctions and proxy/tray independence;
- active Helix cleanup plus exact retained historical/migration whitelist;
- focused/full test commands and exact outcomes;
- any reproduced baseline-only environment failures;
- machine-local disablement summarized separately, explicitly noting backups/configs are not in the PR.

- [ ] **Step 5: Return the PR URL and concise operator state**

Report the PR link, branch/commit head, test outcome, the state of Codex/Gemini/Antigravity legacy integrations, whether Cymatix health is currently available, and the rollback locations/commands without exposing secrets.

# Vendor + Host Badges Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display agent vendor + IDE/CLI host (e.g., "Claude Code + VSCode") in the launcher dashboard's Agents and Identities panels, replacing the current bare-handle ("codex") display.

**Architecture:** Two new optional columns on the `participants` table (`agent_kind`, `mcp_host`); plumbed end-to-end from MCP env vars (`HELIX_AGENT_KIND`, `HELIX_MCP_HOST`) through `_register_with_registry` → `AgentBridge` → `POST /sessions/register` → registry → SQLite, and back out through `list_participants` → collector → templates. A small pure-function module formats the pretty label (`Claude Code + VSCode`). The Jinja chip at [agents_panel.html:49-51](../../helix_context/launcher/templates/components/agents_panel.html) already exists; this plan supplies the data it needs.

**Tech Stack:** Python 3.11+, SQLite (via `genome.conn`), Pydantic v2 (schemas), FastAPI (server), Jinja2 (templates), pytest.

---

## Scope check

This is a single-subsystem change (session registry plumbing + dashboard label) — does NOT need to be split.

## File structure

**Modify:**
- [`helix_context/genome.py`](../../helix_context/genome.py) — add idempotent `ALTER TABLE` for the two new columns in `_ensure_registry_schema` (line 750+ block)
- [`helix_context/schemas.py`](../../helix_context/schemas.py) — add fields to `Participant` (line 295) and `ParticipantInfo` (line 314)
- [`helix_context/registry.py`](../../helix_context/registry.py) — accept + persist + project the two fields (`register_participant` line 96, `list_participants` line 464, `get_participant` line 513)
- [`helix_context/server.py`](../../helix_context/server.py) — accept `agent_kind` / `mcp_host` in `/sessions/register` body (line 1820)
- [`helix_context/bridge.py`](../../helix_context/bridge.py) — pass new fields through `AgentBridge.register_participant` (line 374)
- [`helix_context/mcp_server.py`](../../helix_context/mcp_server.py) — read env vars and pass to bridge in `_register_with_registry` (~line 906)
- [`helix_context/launcher/collector.py`](../../helix_context/launcher/collector.py) — populate `host_label` field on entries in `_all_agents_panel` (line 244), `_disconnected_agents_panel` (line 272), and `_participants_panel`
- [`helix_context/launcher/templates/components/participants_panel.html`](../../helix_context/launcher/templates/components/participants_panel.html) — add the host chip (the agents panel template already has it)

**Create:**
- [`helix_context/launcher/host_labels.py`](../../helix_context/launcher/host_labels.py) — pure-function pretty-label module (vendor_pretty, host_pretty, compose_label)
- [`tests/test_host_labels.py`](../../tests/test_host_labels.py) — unit tests for the pretty-label module
- [`tests/test_vendor_host_plumbing.py`](../../tests/test_vendor_host_plumbing.py) — end-to-end test: env → register → list → collector entry has host_label

**Delete:** None — purely additive.

---

## Task 1: Pretty-label module + unit tests (no dependencies)

Pure-function module first. Easiest to TDD; everything else depends on its output shape.

**Files:**
- Create: `helix_context/launcher/host_labels.py`
- Test: `tests/test_host_labels.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_host_labels.py
"""Pretty-label composition for the dashboard agent badges.

Exercises:
- known vendors → pretty form
- known hosts → pretty form (incl. "vscode" → "VS Code")
- unknown values → echoed verbatim (no silent drop)
- compose_label with both / vendor-only / host-only / neither
"""
from helix_context.launcher.host_labels import (
    vendor_pretty,
    host_pretty,
    compose_label,
)


def test_vendor_pretty_known():
    assert vendor_pretty("claude-code") == "Claude Code"
    assert vendor_pretty("claude-desktop") == "Claude Desktop"
    assert vendor_pretty("codex") == "Codex"
    assert vendor_pretty("gemini") == "Gemini"


def test_vendor_pretty_unknown_echoes_verbatim():
    assert vendor_pretty("acme-bot") == "acme-bot"


def test_vendor_pretty_none():
    assert vendor_pretty(None) is None
    assert vendor_pretty("") is None


def test_host_pretty_known():
    assert host_pretty("claude-code") == "Claude Code"
    assert host_pretty("antigravity") == "Antigravity"
    assert host_pretty("cursor") == "Cursor"
    assert host_pretty("vscode") == "VS Code"
    assert host_pretty("vscode-continue") == "VS Code (Continue)"


def test_host_pretty_unknown_echoes_verbatim():
    assert host_pretty("zed") == "zed"


def test_host_pretty_unknown_marker_returns_none():
    """The MCP server defaults HELIX_MCP_HOST to 'unknown' — we don't
    want a meaningless 'Unknown' chip cluttering the dashboard."""
    assert host_pretty("unknown") is None
    assert host_pretty(None) is None
    assert host_pretty("") is None


def test_compose_label_both():
    assert compose_label("claude-code", "vscode") == "Claude Code + VS Code"


def test_compose_label_vendor_only():
    assert compose_label("claude-code", None) == "Claude Code"


def test_compose_label_host_only():
    assert compose_label(None, "antigravity") == "Antigravity"


def test_compose_label_neither_returns_none():
    assert compose_label(None, None) is None
    assert compose_label("", "") is None


def test_compose_label_dedupes_when_vendor_equals_host():
    """Common case: HELIX_AGENT_KIND=claude-code and HELIX_MCP_HOST=claude-code.
    Render as a single chip, not 'Claude Code + Claude Code'."""
    assert compose_label("claude-code", "claude-code") == "Claude Code"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_host_labels.py -v`
Expected: ImportError or ModuleNotFoundError on `helix_context.launcher.host_labels`.

- [ ] **Step 3: Write minimal implementation**

```python
# helix_context/launcher/host_labels.py
"""Pretty-label composition for vendor + host badges on the launcher dashboard.

The session registry stores ``agent_kind`` (vendor family — "claude-code",
"codex", "gemini") and ``mcp_host`` (host capability tag — "antigravity",
"vscode", "cursor"). The dashboard renders the pair as a single chip,
e.g. "Claude Code + VS Code". Unknown values echo verbatim so a new
vendor surfaces immediately rather than being swallowed.

The literal string "unknown" is treated as missing on the host axis
because ``mcp_server.py`` defaults ``HELIX_MCP_HOST`` to "unknown" when
the host doesn't set it.
"""
from __future__ import annotations

from typing import Optional


_VENDOR_MAP = {
    "claude-code": "Claude Code",
    "claude-desktop": "Claude Desktop",
    "codex": "Codex",
    "gemini": "Gemini",
}

_HOST_MAP = {
    "claude-code": "Claude Code",
    "claude-desktop": "Claude Desktop",
    "antigravity": "Antigravity",
    "cursor": "Cursor",
    "vscode": "VS Code",
    "vscode-continue": "VS Code (Continue)",
}


def vendor_pretty(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _VENDOR_MAP.get(value, value)


def host_pretty(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if value == "unknown":
        return None
    return _HOST_MAP.get(value, value)


def compose_label(
    agent_kind: Optional[str],
    mcp_host: Optional[str],
) -> Optional[str]:
    """Combine vendor + host into a single dashboard chip label.

    Returns ``None`` when both axes are absent so the template can
    skip rendering the chip entirely.
    """
    v = vendor_pretty(agent_kind)
    h = host_pretty(mcp_host)
    if v and h and v == h:
        return v
    if v and h:
        return f"{v} + {h}"
    return v or h
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_host_labels.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/launcher/host_labels.py tests/test_host_labels.py
git commit -m "feat(launcher): add host_labels pretty-label module

Pure-function vendor+host composition for the agents panel chip.
Maps known vendors/hosts to pretty form, echoes unknowns verbatim,
treats HELIX_MCP_HOST default 'unknown' as missing."
```

---

## Task 2: Schema migration (idempotent ALTER TABLE)

Add the two columns to the `participants` table. Idempotent — re-running on a DB that already has them is a no-op.

**Files:**
- Modify: `helix_context/genome.py` (after the existing `participants` CREATE TABLE block, ~line 770)
- Test: `tests/test_registry.py` (add new test method)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_registry.py` (append a new test class or fit into existing patterns — match the file's existing style):

```python
def test_participants_table_has_agent_kind_and_mcp_host_columns(genome):
    """Schema migration adds agent_kind and mcp_host columns idempotently."""
    cur = genome.conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(participants)").fetchall()}
    assert "agent_kind" in cols, f"agent_kind missing; got {cols}"
    assert "mcp_host" in cols, f"mcp_host missing; got {cols}"


def test_schema_migration_is_idempotent(genome):
    """Re-running _ensure_registry_schema does not raise on existing columns."""
    cur = genome.conn.cursor()
    # Should not raise even though columns already exist.
    genome._ensure_registry_schema(cur)
    genome.conn.commit()
```

If `tests/test_registry.py` doesn't define a `genome` fixture, copy the fixture pattern from the top of the file. Likely already exists.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_registry.py::test_participants_table_has_agent_kind_and_mcp_host_columns -v`
Expected: FAIL — `agent_kind missing`.

- [ ] **Step 3: Write minimal implementation**

In `helix_context/genome.py`, locate the `_ensure_registry_schema` method (line 667). After the `CREATE INDEX IF NOT EXISTS idx_participants_status` statement (~line 778) and BEFORE the `agents` table block (~line 780), insert:

```python
        # Vendor + host axes for dashboard badges (added 2026-05-05).
        # `agent_kind`: vendor family — "claude-code", "codex", "gemini".
        # `mcp_host`:   host capability tag — "antigravity", "vscode", "cursor".
        # Both are nullable; pre-2026-05-05 rows simply read NULL.
        for col in ("agent_kind", "mcp_host"):
            try:
                cur.execute(f"ALTER TABLE participants ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists — idempotent
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_registry.py::test_participants_table_has_agent_kind_and_mcp_host_columns tests/test_registry.py::test_schema_migration_is_idempotent -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/genome.py tests/test_registry.py
git commit -m "feat(genome): add agent_kind+mcp_host columns to participants

Idempotent ALTER TABLE in _ensure_registry_schema. Backfills NULL on
existing rows. Plumbing for the dashboard vendor+host badge."
```

---

## Task 3: Schema models (Participant, ParticipantInfo)

Add the optional fields to the Pydantic models so they round-trip through the API and the registry projection.

**Files:**
- Modify: `helix_context/schemas.py:295-322`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_registry.py`:

```python
def test_participant_model_accepts_vendor_host_fields():
    from helix_context.schemas import Participant
    p = Participant(
        participant_id="abc",
        party_id="party",
        handle="laude",
        agent_kind="claude-code",
        mcp_host="vscode",
    )
    assert p.agent_kind == "claude-code"
    assert p.mcp_host == "vscode"


def test_participant_info_accepts_vendor_host_fields():
    from helix_context.schemas import ParticipantInfo
    p = ParticipantInfo(
        participant_id="abc",
        party_id="party",
        handle="laude",
        status="active",
        last_seen_s_ago=0.0,
        started_at=0.0,
        agent_kind="codex",
        mcp_host="cursor",
    )
    assert p.agent_kind == "codex"
    assert p.mcp_host == "cursor"


def test_participant_info_defaults_vendor_host_to_none():
    from helix_context.schemas import ParticipantInfo
    p = ParticipantInfo(
        participant_id="abc",
        party_id="party",
        handle="laude",
        status="active",
        last_seen_s_ago=0.0,
        started_at=0.0,
    )
    assert p.agent_kind is None
    assert p.mcp_host is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_registry.py::test_participant_model_accepts_vendor_host_fields -v`
Expected: FAIL — `extra_forbidden` or attribute missing.

- [ ] **Step 3: Write minimal implementation**

In `helix_context/schemas.py`, modify `Participant` (line 295):

```python
class Participant(BaseModel):
    """A live runtime actor (Claude session, sub-agent, swarm member).

    Participants belong to exactly one party. They are ephemeral — they
    come and go as sessions start and stop. Attribution of genes survives
    participant turnover via the party.
    """
    participant_id: str
    party_id: str
    handle: str
    workspace: Optional[str] = None
    pid: Optional[int] = None
    started_at: float = Field(default_factory=lambda: time.time())
    last_heartbeat: float = Field(default_factory=lambda: time.time())
    status: str = "active"
    capabilities: List[str] = Field(default_factory=list)
    metadata: Optional[dict] = None
    agent_kind: Optional[str] = None    # vendor family — "claude-code", "codex"
    mcp_host: Optional[str] = None      # host tag — "antigravity", "vscode"
```

And `ParticipantInfo` (line 314):

```python
class ParticipantInfo(BaseModel):
    """Projection used by GET /sessions — what observers see about a sibling."""
    participant_id: str
    party_id: str
    handle: str
    workspace: Optional[str] = None
    status: str
    last_seen_s_ago: float
    started_at: float
    agent_kind: Optional[str] = None
    mcp_host: Optional[str] = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_registry.py -k "participant_model or participant_info" -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/schemas.py tests/test_registry.py
git commit -m "feat(schemas): add agent_kind+mcp_host to Participant models

Optional fields, default None. Round-trip vehicle for the new
participants columns added in the prior commit."
```

---

## Task 4: Registry — accept, persist, project

Three methods change: `register_participant` (writes), `list_participants` (reads many), `get_participant` (reads one).

**Files:**
- Modify: `helix_context/registry.py:96-156` (register_participant), `464-511` (list_participants), `513-...` (get_participant)
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_registry.py`:

```python
def test_register_participant_persists_vendor_host(registry, genome):
    """register_participant stores agent_kind and mcp_host on the row."""
    p = registry.register_participant(
        party_id="party_x",
        handle="laude",
        agent_kind="claude-code",
        mcp_host="vscode",
    )
    assert p.agent_kind == "claude-code"
    assert p.mcp_host == "vscode"

    cur = genome.conn.cursor()
    row = cur.execute(
        "SELECT agent_kind, mcp_host FROM participants WHERE participant_id = ?",
        (p.participant_id,),
    ).fetchone()
    assert row[0] == "claude-code"
    assert row[1] == "vscode"


def test_register_participant_omitting_vendor_host_stores_null(registry, genome):
    """Backwards-compat: callers that don't pass the new fields get NULL."""
    p = registry.register_participant(party_id="party_y", handle="taude")
    assert p.agent_kind is None
    assert p.mcp_host is None


def test_list_participants_projects_vendor_host(registry):
    registry.register_participant(
        party_id="party_z",
        handle="laude",
        agent_kind="codex",
        mcp_host="cursor",
    )
    rows = registry.list_participants(party_id="party_z", status_filter="all")
    assert len(rows) == 1
    assert rows[0].agent_kind == "codex"
    assert rows[0].mcp_host == "cursor"


def test_get_participant_projects_vendor_host(registry):
    p = registry.register_participant(
        party_id="party_w",
        handle="laude",
        agent_kind="gemini",
        mcp_host="antigravity",
    )
    fetched = registry.get_participant(p.participant_id)
    assert fetched is not None
    assert fetched.agent_kind == "gemini"
    assert fetched.mcp_host == "antigravity"
```

If a `registry` fixture doesn't exist in this file, build one as `Registry(genome)`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_registry.py -k "vendor_host" -v`
Expected: 4 fails — `register_participant() got an unexpected keyword argument 'agent_kind'`.

- [ ] **Step 3: Write minimal implementation**

In `helix_context/registry.py`, modify `register_participant` (line 96). Add the two params, and include them in the INSERT:

```python
    def register_participant(
        self,
        party_id: str,
        handle: str,
        workspace: Optional[str] = None,
        pid: Optional[int] = None,
        capabilities: Optional[List[str]] = None,
        metadata: Optional[dict] = None,
        display_name: Optional[str] = None,
        agent_kind: Optional[str] = None,
        mcp_host: Optional[str] = None,
    ) -> Participant:
        """Register a new participant. Creates the party row on first use (trust-on-first-use).

        Returns the full Participant model with server-generated participant_id.
        """
        now = time.time()
        participant_id = _new_participant_id()
        cur = self.genome.conn.cursor()

        cur.execute(
            "INSERT OR IGNORE INTO parties "
            "(party_id, display_name, trust_domain, created_at, metadata) "
            "VALUES (?, ?, 'local', ?, NULL)",
            (party_id, display_name or party_id, now),
        )

        cur.execute(
            "INSERT INTO participants "
            "(participant_id, party_id, handle, workspace, pid, started_at, "
            " last_heartbeat, status, capabilities, metadata, "
            " agent_kind, mcp_host) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
            (
                participant_id,
                party_id,
                handle,
                workspace,
                pid,
                now,
                now,
                json_dumps(capabilities or []),
                json_dumps(metadata) if metadata else None,
                agent_kind,
                mcp_host,
            ),
        )
        self.genome.conn.commit()
        log.info(
            "Registered participant %s (handle=%s, party=%s, kind=%s, host=%s)",
            participant_id, handle, party_id, agent_kind, mcp_host,
        )

        return Participant(
            participant_id=participant_id,
            party_id=party_id,
            handle=handle,
            workspace=workspace,
            pid=pid,
            started_at=now,
            last_heartbeat=now,
            status="active",
            capabilities=capabilities or [],
            metadata=metadata,
            agent_kind=agent_kind,
            mcp_host=mcp_host,
        )
```

Modify `list_participants` (line 464). Update the SELECT and the projection:

```python
    def list_participants(
        self,
        party_id: Optional[str] = None,
        status_filter: str = "active",
        workspace_prefix: Optional[str] = None,
    ) -> List[ParticipantInfo]:
        cur = self.genome.conn.cursor()
        sql = (
            "SELECT participant_id, party_id, handle, workspace, "
            "       started_at, last_heartbeat, agent_kind, mcp_host "
            "FROM participants"
        )
        params: list = []
        conditions: list = []
        if party_id is not None:
            conditions.append("party_id = ?")
            params.append(party_id)
        if workspace_prefix is not None:
            conditions.append("workspace LIKE ?")
            params.append(workspace_prefix + "%")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY last_heartbeat DESC"

        now = time.time()
        rows = cur.execute(sql, params).fetchall()
        out: List[ParticipantInfo] = []
        for r in rows:
            live_status = _status_from_last_heartbeat(r["last_heartbeat"], now)
            if status_filter != "all" and live_status != status_filter:
                continue
            out.append(ParticipantInfo(
                participant_id=r["participant_id"],
                party_id=r["party_id"],
                handle=r["handle"],
                workspace=r["workspace"],
                status=live_status,
                last_seen_s_ago=round(now - r["last_heartbeat"], 1),
                started_at=r["started_at"],
                agent_kind=r["agent_kind"],
                mcp_host=r["mcp_host"],
            ))
        return out
```

Modify `get_participant` (line 513). Update the SELECT to include the two columns and pass them into the `Participant` construction. Locate the existing SELECT and the constructor below it; add `agent_kind, mcp_host` to both.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_registry.py -k "vendor_host" -v`
Expected: 4 passed.

Run: `python -m pytest tests/test_registry.py tests/test_bridge_registry.py -v`
Expected: all existing tests still pass (no regression).

- [ ] **Step 5: Commit**

```bash
git add helix_context/registry.py tests/test_registry.py
git commit -m "feat(registry): persist + project agent_kind/mcp_host

register_participant accepts new optional fields and writes them.
list_participants and get_participant project them through to
ParticipantInfo / Participant. Backward-compat: omitting either
field stores NULL and reads back as None."
```

---

## Task 5: Server endpoint — accept new fields

Pass `agent_kind` and `mcp_host` from request body into `registry.register_participant`.

**Files:**
- Modify: `helix_context/server.py:1820-1895` (`/sessions/register` endpoint)
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_server.py` (use the existing client fixture):

```python
def test_sessions_register_accepts_vendor_host(client):
    resp = client.post(
        "/sessions/register",
        json={
            "party_id": "party_vh",
            "handle": "laude",
            "agent_kind": "claude-code",
            "mcp_host": "vscode",
        },
    )
    assert resp.status_code == 200, resp.text
    pid = resp.json()["participant_id"]

    # Verify it landed in the registry projection
    listing = client.get("/sessions", params={"party_id": "party_vh"}).json()
    rows = listing if isinstance(listing, list) else listing.get("participants", [])
    matching = [r for r in rows if r.get("participant_id") == pid]
    assert len(matching) == 1
    assert matching[0]["agent_kind"] == "claude-code"
    assert matching[0]["mcp_host"] == "vscode"


def test_sessions_register_omitting_vendor_host_still_works(client):
    """Pre-existing clients (no agent_kind / mcp_host in body) keep working."""
    resp = client.post(
        "/sessions/register",
        json={"party_id": "party_legacy", "handle": "taude"},
    )
    assert resp.status_code == 200, resp.text
```

If `GET /sessions` doesn't already exist or has a different shape, adjust the second assertion to read directly from the DB via the test fixture's genome handle. (Check how other tests in the file verify list_participants output.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_server.py::test_sessions_register_accepts_vendor_host -v`
Expected: FAIL — fields don't surface in the listing.

- [ ] **Step 3: Write minimal implementation**

In `helix_context/server.py`, modify the `/sessions/register` endpoint (line 1820). After the existing validation block, change the `registry.register_participant(...)` call to pass the two new optional fields:

```python
        try:
            participant = registry.register_participant(
                party_id=party_id,
                handle=handle,
                workspace=data.get("workspace"),
                pid=data.get("pid"),
                capabilities=data.get("capabilities"),
                metadata=data.get("metadata"),
                display_name=data.get("display_name"),
                agent_kind=data.get("agent_kind"),
                mcp_host=data.get("mcp_host"),
            )
        except Exception as exc:
            log.warning("Session register failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"Registration failed: {exc}"},
                status_code=500,
            )
```

No new validation — the values are free-form strings and trusting the client matches the existing model (trust-on-first-use, see `registry.py:30-37`). NULL bytes are not a concern here because these fields don't go through the BM25 index.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_server.py -k "sessions_register" -v`
Expected: existing tests + 2 new pass.

- [ ] **Step 5: Commit**

```bash
git add helix_context/server.py tests/test_server.py
git commit -m "feat(server): /sessions/register accepts agent_kind+mcp_host

Optional body fields, trust-on-first-use. Pre-existing clients that
don't send them keep working unchanged."
```

---

## Task 6: AgentBridge — pass through

The HTTP client wrapper used by `_register_with_registry`.

**Files:**
- Modify: `helix_context/bridge.py:374-...` (`AgentBridge.register_participant`)
- Test: `tests/test_bridge_registry.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bridge_registry.py` (or create the file if it doesn't have a body for this case — use the existing test patterns there):

```python
def test_bridge_register_participant_sends_vendor_host(http_server, bridge):
    """AgentBridge.register_participant includes agent_kind/mcp_host in body."""
    pid = bridge.register_participant(
        party_id="party_bridge_vh",
        handle="laude",
        agent_kind="claude-code",
        mcp_host="vscode",
    )
    assert pid is not None

    # Use the bridge's own list method to verify round-trip.
    sessions = bridge.list_sessions(party_id="party_bridge_vh")
    assert len(sessions) == 1
    assert sessions[0].agent_kind == "claude-code"
    assert sessions[0].mcp_host == "vscode"
```

If `bridge.list_sessions` doesn't exist, hit the registry directly via the genome fixture.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_bridge_registry.py::test_bridge_register_participant_sends_vendor_host -v`
Expected: FAIL — unexpected keyword argument.

- [ ] **Step 3: Write minimal implementation**

In `helix_context/bridge.py`, modify `AgentBridge.register_participant` (line 374):

```python
    def register_participant(
        self,
        party_id: str,
        handle: str,
        workspace: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        display_name: Optional[str] = None,
        start_auto_heartbeat: bool = False,
        agent_kind: Optional[str] = None,
        mcp_host: Optional[str] = None,
    ) -> Optional[str]:
        body: Dict[str, Any] = {
            "party_id": party_id,
            "handle": handle,
        }
        if workspace is not None:
            body["workspace"] = workspace
        if capabilities is not None:
            body["capabilities"] = capabilities
        if display_name is not None:
            body["display_name"] = display_name
        if agent_kind is not None:
            body["agent_kind"] = agent_kind
        if mcp_host is not None:
            body["mcp_host"] = mcp_host
        try:
            body["pid"] = os.getpid()
        except Exception:
            pass

        result = self._http_post("/sessions/register", json_body=body)
        # ... (rest of the method unchanged)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_bridge_registry.py -v`
Expected: existing tests + 1 new pass.

- [ ] **Step 5: Commit**

```bash
git add helix_context/bridge.py tests/test_bridge_registry.py
git commit -m "feat(bridge): AgentBridge.register_participant forwards vendor+host

Adds optional agent_kind / mcp_host kwargs that are included in the
POST /sessions/register body when set."
```

---

## Task 7: MCP server — read env, pass to bridge

The trigger point — this is what makes a Claude Code (or Codex, Gemini, etc.) MCP session register with the right metadata.

**Files:**
- Modify: `helix_context/mcp_server.py:_register_with_registry` (~line 906)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_server.py` (look at existing tests for the env-mocking pattern):

```python
def test_register_with_registry_sends_env_vendor_host(monkeypatch, mock_bridge):
    """_register_with_registry reads HELIX_AGENT_KIND and HELIX_MCP_HOST
    from env and forwards them to AgentBridge.register_participant."""
    monkeypatch.setenv("HELIX_MCP_HANDLE", "laude")
    monkeypatch.setenv("HELIX_PARTY_ID", "swift_wing21")
    monkeypatch.setenv("HELIX_AGENT_KIND", "claude-code")
    monkeypatch.setenv("HELIX_MCP_HOST", "vscode")

    from helix_context import mcp_server
    mcp_server._register_with_registry()

    call = mock_bridge.register_participant_calls[-1]
    assert call["agent_kind"] == "claude-code"
    assert call["mcp_host"] == "vscode"
    assert call["handle"] == "laude"


def test_register_with_registry_omits_unset_env(monkeypatch, mock_bridge):
    """If HELIX_AGENT_KIND is unset, registration sends None (not 'unknown')."""
    monkeypatch.delenv("HELIX_AGENT_KIND", raising=False)
    monkeypatch.setenv("HELIX_MCP_HOST", "antigravity")
    monkeypatch.setenv("HELIX_MCP_HANDLE", "raude")
    monkeypatch.setenv("HELIX_PARTY_ID", "party_test")

    from helix_context import mcp_server
    mcp_server._register_with_registry()

    call = mock_bridge.register_participant_calls[-1]
    assert call["agent_kind"] is None
    assert call["mcp_host"] == "antigravity"
```

If a `mock_bridge` fixture doesn't exist, build it by patching `helix_context.bridge.AgentBridge` to record calls into a list. Use `monkeypatch.setattr` per the file's existing patterns.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py -k "vendor_host or env" -v`
Expected: FAIL — agent_kind not forwarded (or KeyError if mock doesn't capture it).

- [ ] **Step 3: Write minimal implementation**

In `helix_context/mcp_server.py`, modify `_register_with_registry` (~line 906). Read the two env vars and forward:

```python
def _register_with_registry() -> None:
    """Register this MCP subprocess as a session-registry participant.
    ... (existing docstring)
    """
    try:
        from helix_context.bridge import AgentBridge
    except Exception as exc:
        log.warning("Registry bridge import failed, skipping registration: %s", exc)
        return

    handle = os.environ.get("HELIX_MCP_HANDLE", f"mcp-{os.getpid()}")
    party_id = _default_party_id()
    mcp_host_env = os.environ.get("HELIX_MCP_HOST", "unknown")
    agent_kind_env = os.environ.get("HELIX_AGENT_KIND")  # no default — None means "unset"
    workspace: Optional[str]
    try:
        workspace = os.getcwd()
    except Exception:
        workspace = None

    # Capability tags retained for backwards compat with consumers that
    # parse them today; the structured agent_kind/mcp_host columns are
    # the new canonical surface.
    capabilities = ["mcp_tools", f"host:{mcp_host_env}"]

    # Normalize the literal "unknown" sentinel to None at the wire level
    # so the column doesn't get polluted with the env default.
    mcp_host = None if mcp_host_env == "unknown" else mcp_host_env

    bridge = AgentBridge(helix_base_url=HELIX_URL)
    participant_id = bridge.register_participant(
        party_id=party_id,
        handle=handle,
        workspace=workspace,
        capabilities=capabilities,
        agent_kind=agent_kind_env,
        mcp_host=mcp_host,
        start_auto_heartbeat=True,
    )
    if participant_id:
        log.info(
            "Registered as %s (party=%s, kind=%s, host=%s, pid=%d)",
            handle, party_id, agent_kind_env, mcp_host, os.getpid(),
        )
    else:
        log.warning(
            "Session registration failed (is helix running at %s?) "
            "— tool calls will still work",
            HELIX_URL,
        )
```

Also update the docstring at lines ~895-902 to add `HELIX_AGENT_KIND` to the documented env block. Match the existing format.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py -k "vendor_host or env" -v`
Expected: 2 passed.

Run full MCP test file: `python -m pytest tests/test_mcp_server.py -v`
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add helix_context/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(mcp): forward HELIX_AGENT_KIND+HELIX_MCP_HOST on register

_register_with_registry now reads the two env vars and passes them
through AgentBridge to /sessions/register. The literal 'unknown'
default for HELIX_MCP_HOST is normalized to NULL at the wire."
```

---

## Task 8: Collector — populate host_label

The dashboard's data-builder. Reads vendor + host from each participant dict and produces the `host_label` field that the templates already check.

**Files:**
- Modify: `helix_context/launcher/collector.py:244-302` (`_all_agents_panel`, `_disconnected_agents_panel`); also `_participants_panel` (find by grep)

- [ ] **Step 1: Write the failing test**

Create `tests/test_collector_host_label.py`:

```python
"""Collector populates host_label on agent panel entries.

Exercises the wire from a participant dict (with agent_kind / mcp_host
fields) through StateCollector's panel builders to the rendered entry
shape that the Jinja templates consume.
"""
from helix_context.launcher.collector import StateCollector


def _make_participant(**overrides):
    base = {
        "participant_id": "abc12345",
        "handle": "laude",
        "party_id": "party_x",
        "workspace": "F:\\Projects",
        "status": "active",
        "last_seen_s_ago": 1.0,
        "agent_kind": None,
        "mcp_host": None,
    }
    base.update(overrides)
    return base


def test_all_agents_panel_emits_host_label_when_both_set():
    collector = StateCollector(state_provider=lambda: {})
    p = _make_participant(agent_kind="claude-code", mcp_host="vscode")
    panel = collector._all_agents_panel([p])
    assert panel["entries"][0]["host_label"] == "Claude Code + VS Code"


def test_all_agents_panel_emits_host_label_vendor_only():
    collector = StateCollector(state_provider=lambda: {})
    p = _make_participant(agent_kind="codex", mcp_host=None)
    panel = collector._all_agents_panel([p])
    assert panel["entries"][0]["host_label"] == "Codex"


def test_all_agents_panel_omits_host_label_when_neither_set():
    collector = StateCollector(state_provider=lambda: {})
    p = _make_participant()
    panel = collector._all_agents_panel([p])
    # Either absent or explicitly None — both let the {% if %} skip render.
    assert not panel["entries"][0].get("host_label")


def test_disconnected_agents_panel_emits_host_label():
    collector = StateCollector(state_provider=lambda: {})
    p = _make_participant(
        status="stale",
        agent_kind="claude-code",
        mcp_host="antigravity",
    )
    panel = collector._disconnected_agents_panel([p])
    assert panel is not None
    assert panel["entries"][0]["host_label"] == "Claude Code + Antigravity"
```

If `StateCollector`'s constructor signature is different, look at `app.py` for how it's instantiated and adapt the fixture. The tests just need to call the builder methods — instantiation can be minimal.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_collector_host_label.py -v`
Expected: 4 fails — `host_label` is missing or KeyError.

- [ ] **Step 3: Write minimal implementation**

In `helix_context/launcher/collector.py`, at the top, add:

```python
from helix_context.launcher.host_labels import compose_label
```

In `_all_agents_panel` (line 244), modify the entry dict to include `host_label`:

```python
            entries.append(
                {
                    "handle": participant.get("handle"),
                    "party_id": participant.get("party_id"),
                    "workspace": participant.get("workspace"),
                    "status": participant.get("status"),
                    "last_seen_s_ago": participant["last_seen_s_ago"],
                    "participant_id": participant_id,
                    "participant_id_short": participant_id[:8],
                    "identifier": self._identity_label(participant),
                    "host_label": compose_label(
                        participant.get("agent_kind"),
                        participant.get("mcp_host"),
                    ),
                }
            )
```

Same change in `_disconnected_agents_panel` (line 272). Find `_participants_panel` (or `_identities_panel`) and apply the same field if it builds entries from participants — search for `"handle": participant` to locate it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_collector_host_label.py -v`
Expected: 4 passed.

Run: `python -m pytest tests/ -m "not live" -v`
Expected: full mock suite still green.

- [ ] **Step 5: Commit**

```bash
git add helix_context/launcher/collector.py tests/test_collector_host_label.py
git commit -m "feat(collector): emit host_label on agent panel entries

Reads agent_kind/mcp_host from participant dicts and composes the
pretty label via host_labels.compose_label. Templates' existing
{% if agent.host_label %} chip now activates."
```

---

## Task 9: Identities (participants) panel template — add chip

The Identities panel's template (used in the screenshot's middle panel) does NOT yet have a `host_label` chip. Add it so the vendor+host shows there too.

**Files:**
- Modify: `helix_context/launcher/templates/components/participants_panel.html`
- Modify: `helix_context/launcher/collector.py` — also pass `host_label` through `_participants_panel`'s entry shape (likely already done in Task 8 if you grepped correctly; if not, do it here)

- [ ] **Step 1: Skim the template to find the right insertion spot**

Read `participants_panel.html`. Locate the line that renders `{{ p.handle }}` (around line 14 per earlier investigation). The chip should sit immediately after the handle chip, mirroring `agents_panel.html:47-51`.

- [ ] **Step 2: Add the chip**

Insert after the handle chip:

```jinja
              {% if p.host_label %}
              <span class="chip chip--agent-host">{{ p.host_label }}</span>
              {% endif %}
```

Match the existing indentation and class name conventions (`chip--agent-host` already exists in [launcher.css](../../helix_context/launcher/static/launcher.css) since the agents panel uses it).

- [ ] **Step 3: Smoke test the template**

A render-time test isn't strictly necessary (the chip is a static `{% if %}`), but verify rendering doesn't break:

```bash
python -m pytest tests/ -m "not live" -k "launcher or dashboard or template" -v
```

Expected: green if any such tests exist; if not, this step is a no-op confirmation.

- [ ] **Step 4: Verify no other panels need the same treatment**

Grep for files that render participant pills:

```bash
grep -rn "p.handle\|participant.handle" helix_context/launcher/templates/
```

Expected: at most a handful — `parties_panel.html` is **party-level** (device, not vendor/host) and should NOT get the chip. The agents and participants panels are the only two that should.

- [ ] **Step 5: Commit**

```bash
git add helix_context/launcher/templates/components/participants_panel.html
git commit -m "feat(launcher): add host_label chip to identities panel

Mirrors the existing agents_panel chip. Both panels now display
'Claude Code + VS Code' style vendor+host badges when the data
is present."
```

---

## Task 10: End-to-end plumbing test

A single integration test that exercises the full chain: env-vars → bridge → endpoint → registry → collector entry. Catches regressions where any layer drops the fields.

**Files:**
- Create: `tests/test_vendor_host_plumbing.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end plumbing for vendor+host badges.

Exercises: HELIX_AGENT_KIND + HELIX_MCP_HOST env → AgentBridge →
POST /sessions/register → registry → list_participants → collector
entry. Catches regressions where any layer in the chain drops the
new fields.

Skipped under the 'live' marker since it spins up an in-memory server.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_clean_genome(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIX_GENOME_PATH", str(tmp_path / "genome.db"))
    from helix_context.server import build_app
    app = build_app()
    return app


def test_register_with_env_vendor_host_renders_in_collector(app_with_clean_genome):
    """Full plumbing: client posts to /sessions/register with
    agent_kind+mcp_host, then we read the collector's panel and
    confirm the entry has the composed host_label."""
    client = TestClient(app_with_clean_genome)

    resp = client.post(
        "/sessions/register",
        json={
            "party_id": "swift_wing21",
            "handle": "laude",
            "workspace": "F:\\Projects",
            "agent_kind": "claude-code",
            "mcp_host": "vscode",
        },
    )
    assert resp.status_code == 200, resp.text

    # Reach into the registry through the app's bridge or genome handle
    # to pull the participant dicts the collector will see.
    from helix_context.launcher.collector import StateCollector
    # Rough reconstruction — adapt to whatever StateCollector actually needs.
    # If the collector is wired through the launcher app, you may need to
    # build a state_provider closure that fetches participants from the
    # genome attached to app_with_clean_genome.
    ...
    # The crucial assertion:
    # assert collector_entry["host_label"] == "Claude Code + VS Code"
```

The full body of this test depends on how the launcher/collector is wired in tests. The skeleton above shows intent; the implementer should look at any existing launcher tests to see how `StateCollector` is exercised in test mode, OR replace the collector half with a direct check that `list_participants` projects the fields (which is already covered in Task 4 — in that case, this Task 10 is a thin smoke test that the endpoint→registry chain works under a real ASGI client).

If pure end-to-end is too much wiring, demote this test to `tests/test_vendor_host_plumbing.py` covering just: post → /sessions/register → list via bridge → assert the fields. Mark it appropriately.

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_vendor_host_plumbing.py -v`
Expected: passes (proves no layer drops the fields).

- [ ] **Step 3: Commit**

```bash
git add tests/test_vendor_host_plumbing.py
git commit -m "test: end-to-end plumbing for vendor+host badges

Verifies env → bridge → endpoint → registry → projection chain
keeps agent_kind and mcp_host intact. Single integration test
guards against future regressions in any single layer."
```

---

## Task 11: Update docs

Reflect the new fields in the routing doc and the SESSION_REGISTRY spec.

**Files:**
- Modify: `docs/clients/claude-code.md` — note that `agent_kind` and `mcp_host` are now first-class registry columns, not just env-var conventions
- Modify: `docs/architecture/SESSION_REGISTRY.md` — append a "vendor+host columns" subsection to the schema section (around the participants table description, lines 132-149)

- [ ] **Step 1: Update docs/clients/claude-code.md**

Find the "Per-host variants" table (it already exists from earlier work). Just below it, add:

```markdown
As of 2026-05-05, `HELIX_AGENT_KIND` and `HELIX_MCP_HOST` are
persisted as first-class columns on the `participants` row (not
only smuggled in via `capabilities`). The dashboard's Agents and
Identities panels render a "Claude Code + VS Code" pretty-label
chip when both are present.
```

- [ ] **Step 2: Update docs/architecture/SESSION_REGISTRY.md**

Locate the participants table schema (~line 132). Add to the column list documentation:

```markdown
- `agent_kind`    — vendor family ("claude-code", "codex", "gemini") — added 2026-05-05
- `mcp_host`      — host capability tag ("antigravity", "vscode", "cursor") — added 2026-05-05
```

Both nullable. Pre-2026-05-05 rows read as NULL.

- [ ] **Step 3: Commit**

```bash
git add docs/clients/claude-code.md docs/architecture/SESSION_REGISTRY.md
git commit -m "docs: vendor+host columns are now first-class

Reflect the new participants.agent_kind / participants.mcp_host
columns in the routing guide and the session-registry spec."
```

---

## Manual verification (post-merge)

Not a TDD step — a human-in-the-loop check that the dashboard actually shows what we want.

- [ ] Restart the helix server: `python -m uvicorn helix_context.server:app --host 127.0.0.1 --port 11437`
- [ ] Restart Claude Code with the .mcp.json from `docs/clients/claude-code.md` (env block including `HELIX_AGENT_KIND=claude-code` and `HELIX_MCP_HOST=vscode`).
- [ ] Open the launcher dashboard (`http://127.0.0.1:11437/launcher` or the tray).
- [ ] Confirm: Agents panel shows a `Claude Code + VS Code` chip next to the `laude` handle.
- [ ] Confirm: Identities panel shows the same chip.
- [ ] Confirm: Parties panel still shows just `swift_wing21` (no vendor/host chip — parties are device-level).
- [ ] Repeat with a Codex MCP session that sets `HELIX_AGENT_KIND=codex` and `HELIX_MCP_HOST=vscode` → expect `Codex + VS Code`.

---

## Out of scope (deferred follow-ups)

- Per-vendor `.mcp.json` template installer (so users don't have to hand-curate the env block) — separate plan; touches the launcher's installer.
- Backfill of pre-2026-05-05 participant rows from `capabilities` (`host:<x>`) — write-only mitigation; the data is already accessible via the env on next session start.
- Federation: agent_kind/mcp_host across remote parties — current scope is local-tier only.

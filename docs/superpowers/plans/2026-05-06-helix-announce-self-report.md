# Helix Announce: Self-Report + IDE Auto-Detect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the launcher dashboard display IDE/CLI + agent model robustly across vendors via env-var fingerprinting (adapter side) and a `helix_announce` MCP tool (agent side), with progressive disclosure on a tooltip and explicit "not announced/detected/set" placeholders for missing data.

**Architecture:** Three new nullable columns on `participants` (`ide_detected`, `ide_detection_via`, `model_id`) supplement the `agent_kind` / `mcp_host` from PR #26. A small pure-function `ide_fingerprint.py` module runs at MCP startup. A new MCP tool `helix_announce` lets the agent self-report model + override IDE. A new endpoint `POST /sessions/{participant_id}/announce` PATCHes those fields on the participant row. Templates render a CSS-only `:hover` tooltip on the existing chip with explicit placeholders for missing fields.

**Tech Stack:** Python 3.11+, SQLite (via `genome.conn`), Pydantic v2, FastAPI, Jinja2, pytest. Pure CSS for tooltip (no JS).

**Spec:** [docs/superpowers/specs/2026-05-06-helix-announce-self-report-design.md](../specs/2026-05-06-helix-announce-self-report-design.md)

**Prerequisite:** PR #26 (`feat/vendor-host-badges` — `agent_kind`/`mcp_host` columns + chip plumbing) must be merged into master first, OR this branch must branch off `feat/vendor-host-badges`. The implementation assumes PR #26's schema + collector + template wiring is in place.

---

## Scope check

Single subsystem (the participant registry's identity fields + dashboard rendering). Does NOT need decomposition.

## File structure

**Create:**
- `helix_context/launcher/ide_fingerprint.py` — env-var fingerprint chain, ~50 lines
- `helix_context/launcher/model_labels.py` — pretty-map for known `model_id` strings, ~40 lines
- `tests/test_ide_fingerprint.py`
- `tests/test_model_labels.py`
- `tests/test_helix_announce_plumbing.py` — E2E

**Modify:**
- `helix_context/genome.py` — 3 idempotent ALTER TABLE statements
- `helix_context/schemas.py` — add 3 fields to `Participant` + `ParticipantInfo`
- `helix_context/registry.py` — accept/persist/project new fields in register; new `update_announcement()` method
- `helix_context/server.py` — `/sessions/register` accepts new fields (optional); new `POST /sessions/{participant_id}/announce` endpoint
- `helix_context/bridge.py` — `register_participant` forwards new fields; new `announce()` method
- `helix_context/mcp_server.py` — `_register_with_registry` calls `detect_ide()`; expose new `helix_announce` MCP tool
- `helix_context/launcher/collector.py` — populate tooltip fields on entries
- `helix_context/launcher/templates/components/agents_panel.html` — add tooltip block
- `helix_context/launcher/templates/components/participants_panel.html` — add tooltip block
- `helix_context/launcher/static/launcher.css` — `:hover` tooltip styling
- `skills/helix/SKILL.md` — add one sentence in Workflow section about `helix_announce`
- `docs/clients/claude-code.md` — document the new tool + auto-detect; deprecate `HELIX_MCP_HOST` guidance
- `docs/architecture/SESSION_REGISTRY.md` — document new columns + `/announce` endpoint

**Delete:** None — purely additive.

---

## Task 1: IDE fingerprint module + unit tests (no dependencies)

Pure-function module first. Easiest to TDD; everything else depends on its output shape.

**Files:**
- Create: `helix_context/launcher/ide_fingerprint.py`
- Test: `tests/test_ide_fingerprint.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ide_fingerprint.py
"""Env-var fingerprint chain for detecting which IDE/CLI spawned the
MCP adapter.

Exercises:
- HELIX_MCP_HOST explicit override (highest priority, "explicit:..." via)
- VSCODE_PID present → ("vscode", "env:VSCODE_PID")
- CURSOR_TRACE_ID present → ("cursor", "env:CURSOR_TRACE_ID")
- nothing matches → (None, "no_match")
- "unknown" sentinel for HELIX_MCP_HOST falls through, doesn't trigger explicit branch
"""
import pytest

from helix_context.launcher.ide_fingerprint import detect_ide


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip every env var the chain might read so each test is isolated."""
    for key in (
        "HELIX_MCP_HOST",
        "VSCODE_PID",
        "VSCODE_IPC_HOOK",
        "CURSOR_TRACE_ID",
        "TERM_PROGRAM",
    ):
        monkeypatch.delenv(key, raising=False)


def test_explicit_helix_mcp_host_wins(monkeypatch):
    monkeypatch.setenv("HELIX_MCP_HOST", "claude-code")
    monkeypatch.setenv("VSCODE_PID", "1234")  # would otherwise win
    assert detect_ide() == ("claude-code", "explicit:HELIX_MCP_HOST")


def test_helix_mcp_host_unknown_sentinel_falls_through(monkeypatch):
    """HELIX_MCP_HOST=unknown is the legacy default — treat as unset."""
    monkeypatch.setenv("HELIX_MCP_HOST", "unknown")
    monkeypatch.setenv("VSCODE_PID", "1234")
    assert detect_ide() == ("vscode", "env:VSCODE_PID")


def test_vscode_pid_detects_vscode(monkeypatch):
    monkeypatch.setenv("VSCODE_PID", "1234")
    assert detect_ide() == ("vscode", "env:VSCODE_PID")


def test_cursor_trace_id_detects_cursor(monkeypatch):
    monkeypatch.setenv("CURSOR_TRACE_ID", "abc-123")
    assert detect_ide() == ("cursor", "env:CURSOR_TRACE_ID")


def test_vscode_priority_over_cursor(monkeypatch):
    """If both env vars are set (shouldn't happen in practice but make the
    behavior deterministic), VSCODE_PID wins because it's earlier in the chain."""
    monkeypatch.setenv("VSCODE_PID", "1234")
    monkeypatch.setenv("CURSOR_TRACE_ID", "abc-123")
    assert detect_ide() == ("vscode", "env:VSCODE_PID")


def test_no_match_returns_none(monkeypatch):
    """All env vars stripped — no signal."""
    assert detect_ide() == (None, "no_match")


def test_empty_string_env_treated_as_unset(monkeypatch):
    """An empty VSCODE_PID is not a real signal."""
    monkeypatch.setenv("VSCODE_PID", "")
    assert detect_ide() == (None, "no_match")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ide_fingerprint.py -v`
Expected: ImportError or ModuleNotFoundError on `helix_context.launcher.ide_fingerprint`.

- [ ] **Step 3: Write minimal implementation**

```python
# helix_context/launcher/ide_fingerprint.py
"""Env-var fingerprint chain for IDE/CLI detection at MCP-adapter startup.

Used by ``mcp_server._register_with_registry`` to populate the
``ide_detected`` and ``ide_detection_via`` columns on the participants
row without depending on each MCP host vendor to set ``HELIX_MCP_HOST``
correctly.

Only env vars set intentionally by the host process are trusted as
signals. No PPID walking, no terminal-program guessing, no inference.
When no signal matches we return ``(None, "no_match")`` and let the
agent self-report later via ``helix_announce``.

Priority chain (first match wins):
    1. HELIX_MCP_HOST explicit (and not the legacy "unknown" sentinel)
    2. VSCODE_PID
    3. CURSOR_TRACE_ID
    4. fallback → (None, "no_match")

Extending: add a new branch ABOVE the fallback. New branch must (a) read
a single, intentional env var that the host actually sets, and (b) return
the canonical short host id paired with ``"env:<VAR_NAME>"`` as the via.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple


def detect_ide() -> Tuple[Optional[str], str]:
    """Return ``(ide_value, detection_via)``.

    ``ide_value`` is a canonical short id (e.g. ``"vscode"``, ``"cursor"``)
    or ``None`` when no signal matched. ``detection_via`` always contains
    a string documenting how the result was reached, suitable for the
    tooltip's diagnostic line.
    """
    explicit = os.environ.get("HELIX_MCP_HOST", "").strip()
    if explicit and explicit != "unknown":
        return explicit, "explicit:HELIX_MCP_HOST"

    if os.environ.get("VSCODE_PID", "").strip():
        return "vscode", "env:VSCODE_PID"

    if os.environ.get("CURSOR_TRACE_ID", "").strip():
        return "cursor", "env:CURSOR_TRACE_ID"

    return None, "no_match"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ide_fingerprint.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/launcher/ide_fingerprint.py tests/test_ide_fingerprint.py
git commit -m "feat(launcher): add ide_fingerprint module

Env-var fingerprint chain for detecting which IDE/CLI spawned the
MCP adapter. Pure-function detect_ide() returns (ide_value, via).
Branches: explicit HELIX_MCP_HOST > VSCODE_PID > CURSOR_TRACE_ID >
no_match. No inference, no PPID walking — only intentional env vars
the host process sets."
```

---

## Task 2: Schema migration

Add 3 columns to `participants` (idempotent ALTER TABLE).

**Files:**
- Modify: `helix_context/genome.py` — `_ensure_registry_schema` method
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_registry.py`:

```python
def test_participants_table_has_announce_columns(genome):
    """Schema migration adds ide_detected, ide_detection_via, model_id columns."""
    cur = genome.conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(participants)").fetchall()}
    assert "ide_detected" in cols, f"ide_detected missing; got {cols}"
    assert "ide_detection_via" in cols, f"ide_detection_via missing; got {cols}"
    assert "model_id" in cols, f"model_id missing; got {cols}"
```

The existing `test_schema_migration_is_idempotent` from PR #26 already covers re-run safety; no new idempotency test needed.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_registry.py::test_participants_table_has_announce_columns -v`
Expected: FAIL — at least one column missing.

- [ ] **Step 3: Write minimal implementation**

In `helix_context/genome.py::_ensure_registry_schema`, locate the existing PR-#26 ALTER TABLE block for `agent_kind`/`mcp_host` (added 2026-05-05). Add a sibling block immediately after it:

```python
        # Announce columns (added 2026-05-06; design spec
        # docs/superpowers/specs/2026-05-06-helix-announce-self-report-design.md).
        # `ide_detected`:      adapter-side env-var fingerprint at register time
        #                      ("vscode", "cursor", "claude-code", or NULL on no_match).
        # `ide_detection_via`: how we figured it out — "env:VSCODE_PID",
        #                      "explicit:HELIX_MCP_HOST", "agent_override", "no_match".
        # `model_id`:          agent self-reported via helix_announce; NULL until announced.
        for col in ("ide_detected", "ide_detection_via", "model_id"):
            try:
                cur.execute(f"ALTER TABLE participants ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists — idempotent
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_registry.py -k "announce_columns or schema_migration_is_idempotent" -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/genome.py tests/test_registry.py
git commit -m "feat(genome): add ide_detected+ide_detection_via+model_id columns

Idempotent ALTER TABLE in _ensure_registry_schema. Pre-2026-05-06 rows
read NULL on all three. Plumbing for the helix_announce self-report
feature."
```

---

## Task 3: Pydantic models

Add 3 optional fields to `Participant` and `ParticipantInfo`.

**Files:**
- Modify: `helix_context/schemas.py` — `Participant` and `ParticipantInfo` classes
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_registry.py`:

```python
def test_participant_model_accepts_announce_fields():
    from helix_context.schemas import Participant
    p = Participant(
        participant_id="abc",
        party_id="party",
        handle="laude",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
        model_id="claude-opus-4-7",
    )
    assert p.ide_detected == "vscode"
    assert p.ide_detection_via == "env:VSCODE_PID"
    assert p.model_id == "claude-opus-4-7"


def test_participant_info_accepts_announce_fields():
    from helix_context.schemas import ParticipantInfo
    p = ParticipantInfo(
        participant_id="abc",
        party_id="party",
        handle="laude",
        status="active",
        last_seen_s_ago=0.0,
        started_at=0.0,
        ide_detected="cursor",
        ide_detection_via="env:CURSOR_TRACE_ID",
        model_id="gpt-5",
    )
    assert p.ide_detected == "cursor"
    assert p.ide_detection_via == "env:CURSOR_TRACE_ID"
    assert p.model_id == "gpt-5"


def test_participant_info_defaults_announce_fields_to_none():
    from helix_context.schemas import ParticipantInfo
    p = ParticipantInfo(
        participant_id="abc",
        party_id="party",
        handle="laude",
        status="active",
        last_seen_s_ago=0.0,
        started_at=0.0,
    )
    assert p.ide_detected is None
    assert p.ide_detection_via is None
    assert p.model_id is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_registry.py -k "participant_model_accepts_announce or participant_info_accepts_announce" -v`
Expected: FAIL — extra_forbidden or attribute missing.

- [ ] **Step 3: Write minimal implementation**

In `helix_context/schemas.py`, append three fields to the END of `Participant` (preserving all existing fields including the `agent_kind`/`mcp_host` from PR #26):

```python
    ide_detected: Optional[str] = None        # adapter detect at register time
    ide_detection_via: Optional[str] = None   # "env:VSCODE_PID", "agent_override", etc.
    model_id: Optional[str] = None            # agent self-reported via helix_announce
```

Same three fields appended to the END of `ParticipantInfo`:

```python
    ide_detected: Optional[str] = None
    ide_detection_via: Optional[str] = None
    model_id: Optional[str] = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_registry.py -k "announce" -v`
Expected: passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/schemas.py tests/test_registry.py
git commit -m "feat(schemas): add ide_detected+ide_detection_via+model_id

Optional fields, default None on both Participant and ParticipantInfo.
Round-trip vehicle for the new participants columns."
```

---

## Task 4: Registry — accept, persist, project, update

Two related changes: `register_participant` accepts the three new fields and writes them; new `update_announcement()` method PATCHes them on an existing row.

**Files:**
- Modify: `helix_context/registry.py` — `register_participant` signature/INSERT/return, `list_participants` SELECT/projection, `get_participant` SELECT/return, NEW `update_announcement()` method
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_registry.py`:

```python
def test_register_participant_persists_announce_fields(registry, genome):
    p = registry.register_participant(
        party_id="party_a",
        handle="laude",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
    )
    assert p.ide_detected == "vscode"
    assert p.ide_detection_via == "env:VSCODE_PID"
    assert p.model_id is None  # not yet announced

    cur = genome.conn.cursor()
    row = cur.execute(
        "SELECT ide_detected, ide_detection_via, model_id "
        "FROM participants WHERE participant_id = ?",
        (p.participant_id,),
    ).fetchone()
    assert row[0] == "vscode"
    assert row[1] == "env:VSCODE_PID"
    assert row[2] is None


def test_register_participant_omitting_announce_fields_stores_null(registry):
    p = registry.register_participant(party_id="party_b", handle="taude")
    assert p.ide_detected is None
    assert p.ide_detection_via is None
    assert p.model_id is None


def test_list_participants_projects_announce_fields(registry):
    registry.register_participant(
        party_id="party_c",
        handle="laude",
        ide_detected="cursor",
        ide_detection_via="env:CURSOR_TRACE_ID",
    )
    rows = registry.list_participants(party_id="party_c", status_filter="all")
    assert len(rows) == 1
    assert rows[0].ide_detected == "cursor"
    assert rows[0].ide_detection_via == "env:CURSOR_TRACE_ID"
    assert rows[0].model_id is None


def test_get_participant_projects_announce_fields(registry):
    p = registry.register_participant(
        party_id="party_d",
        handle="laude",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
    )
    fetched = registry.get_participant(p.participant_id)
    assert fetched is not None
    assert fetched.ide_detected == "vscode"
    assert fetched.model_id is None


def test_update_announcement_sets_model_id(registry):
    p = registry.register_participant(
        party_id="party_e",
        handle="laude",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
    )
    registry.update_announcement(
        participant_id=p.participant_id,
        model_id="claude-opus-4-7",
    )
    fetched = registry.get_participant(p.participant_id)
    assert fetched.model_id == "claude-opus-4-7"
    # IDE should be unchanged when no override is supplied
    assert fetched.ide_detected == "vscode"
    assert fetched.ide_detection_via == "env:VSCODE_PID"


def test_update_announcement_with_ide_override_sets_via_to_agent_override(registry):
    p = registry.register_participant(
        party_id="party_f",
        handle="laude",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
    )
    registry.update_announcement(
        participant_id=p.participant_id,
        model_id="gpt-5",
        ide_override="cursor",
    )
    fetched = registry.get_participant(p.participant_id)
    assert fetched.ide_detected == "cursor"
    assert fetched.ide_detection_via == "agent_override"
    assert fetched.model_id == "gpt-5"


def test_update_announcement_is_idempotent(registry):
    """Multiple calls overwrite — last write wins."""
    p = registry.register_participant(party_id="party_g", handle="laude")
    registry.update_announcement(participant_id=p.participant_id, model_id="claude-opus-4-7")
    registry.update_announcement(participant_id=p.participant_id, model_id="claude-sonnet-4-6")
    fetched = registry.get_participant(p.participant_id)
    assert fetched.model_id == "claude-sonnet-4-6"


def test_update_announcement_unknown_participant_id_raises_or_noops(registry):
    """Calling update_announcement on an unknown id should be safe."""
    # Pick the contract: either raise KeyError or no-op silently.
    # Recommended: silent no-op + log.warning, mirroring the registry's
    # other update methods (heartbeat etc).
    registry.update_announcement(
        participant_id="does-not-exist",
        model_id="claude-opus-4-7",
    )
    # No assertion on raise; if implementation chooses to raise, change
    # this test to expect the exception type. But the registry's existing
    # heartbeat() updates silently no-op on unknown ids, so this should match.
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_registry.py -k "announce_fields or update_announcement" -v`
Expected: FAIL — `register_participant() got an unexpected keyword argument 'ide_detected'` or `Registry has no attribute 'update_announcement'`.

- [ ] **Step 3: Write minimal implementation**

In `helix_context/registry.py`, modify `register_participant`. Add three new kwargs at the END of the signature (preserving all existing kwargs from PR #26):

```python
        ide_detected: Optional[str] = None,
        ide_detection_via: Optional[str] = None,
        model_id: Optional[str] = None,
```

Update the INSERT to include 3 new columns and 3 new placeholders + 3 new tuple entries at the END of the VALUES tuple. Update the returned `Participant(...)` constructor with `ide_detected=...`, `ide_detection_via=...`, `model_id=...`.

Update `list_participants` and `get_participant` similarly: add the 3 columns to SELECT, add the 3 keyword args to `ParticipantInfo(...)` / `Participant(...)`.

Add a new method `update_announcement` (placement near `heartbeat()` — they're both PATCH-style updates):

```python
    def update_announcement(
        self,
        participant_id: str,
        model_id: Optional[str] = None,
        ide_override: Optional[str] = None,
    ) -> None:
        """PATCH model_id and (optionally) ide_detected on a participant row.

        Called by the announce endpoint when an agent self-reports its
        identity. ``ide_override`` replaces ``ide_detected`` and forces
        ``ide_detection_via='agent_override'`` so the tooltip can surface
        that the IDE came from the agent rather than env detection.

        Idempotent: multiple calls overwrite (last write wins). Silently
        no-ops on unknown ``participant_id`` to match the heartbeat path
        — the agent has no useful action to take if the participant has
        already TTL'd out.
        """
        cur = self.genome.conn.cursor()
        if ide_override is not None:
            cur.execute(
                "UPDATE participants "
                "SET model_id = COALESCE(?, model_id), "
                "    ide_detected = ?, "
                "    ide_detection_via = 'agent_override' "
                "WHERE participant_id = ?",
                (model_id, ide_override, participant_id),
            )
        else:
            cur.execute(
                "UPDATE participants "
                "SET model_id = COALESCE(?, model_id) "
                "WHERE participant_id = ?",
                (model_id, participant_id),
            )
        self.genome.conn.commit()
```

**About the `COALESCE(?, model_id)` pattern (read this carefully):**

- When the bound parameter is `None` (Python passes `None` for an unset kwarg), `COALESCE` returns the column's existing value — the UPDATE is a **no-op for that field**.
- When the bound parameter is a real string (e.g. `"claude-opus-4-7"`), `COALESCE` returns the string — the column is **overwritten**.

This lets one method handle both "agent set the model" and "agent only sent ide_override, leave model alone". If you actively want to clear `model_id` back to NULL, you'd need a separate code path; this spec doesn't require that.

The `ide_override` branch does NOT use COALESCE because when `ide_override` is set, we want to unconditionally overwrite both `ide_detected` and `ide_detection_via` (the via must become `'agent_override'` exactly).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_registry.py -k "announce_fields or update_announcement" -v`
Expected: 8 passed.

Run full file: `python -m pytest tests/test_registry.py -v`
Expected: no NEW failures (pre-existing 3 from PR #26 era still expected).

- [ ] **Step 5: Commit**

```bash
git add helix_context/registry.py tests/test_registry.py
git commit -m "feat(registry): persist+project announce fields; add update_announcement

register_participant accepts ide_detected/ide_detection_via/model_id
optional kwargs and writes them. list_participants and get_participant
project them through. New update_announcement() method PATCHes model_id
and (optionally) ide_detected; ide_override forces detection_via to
'agent_override' for tooltip diagnostic. Silent no-op on unknown
participant_id, matching heartbeat() semantics."
```

---

## Task 5: Server endpoints

Two changes: `/sessions/register` accepts the three new fields in the body; new `POST /sessions/{participant_id}/announce` endpoint.

**Files:**
- Modify: `helix_context/server.py` — `/sessions/register` endpoint, NEW announce endpoint
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server.py`:

```python
def test_sessions_register_accepts_announce_fields(client):
    resp = client.post(
        "/sessions/register",
        json={
            "party_id": "party_register_announce",
            "handle": "laude",
            "ide_detected": "vscode",
            "ide_detection_via": "env:VSCODE_PID",
        },
    )
    assert resp.status_code == 200, resp.text
    pid = resp.json()["participant_id"]

    listing = client.get("/sessions", params={"party_id": "party_register_announce"}).json()
    rows = listing if isinstance(listing, list) else listing.get("participants", [])
    matching = [r for r in rows if r.get("participant_id") == pid]
    assert len(matching) == 1
    assert matching[0]["ide_detected"] == "vscode"
    assert matching[0]["ide_detection_via"] == "env:VSCODE_PID"
    assert matching[0].get("model_id") is None


def test_announce_endpoint_sets_model_id(client):
    """POST /sessions/{participant_id}/announce updates model_id."""
    reg = client.post(
        "/sessions/register",
        json={"party_id": "party_announce", "handle": "laude"},
    )
    pid = reg.json()["participant_id"]

    resp = client.post(
        f"/sessions/{pid}/announce",
        json={"model_id": "claude-opus-4-7"},
    )
    assert resp.status_code == 200, resp.text

    listing = client.get("/sessions", params={"party_id": "party_announce"}).json()
    rows = listing if isinstance(listing, list) else listing.get("participants", [])
    matching = [r for r in rows if r.get("participant_id") == pid]
    assert matching[0]["model_id"] == "claude-opus-4-7"


def test_announce_endpoint_with_ide_override_sets_agent_override_via(client):
    reg = client.post(
        "/sessions/register",
        json={
            "party_id": "party_override",
            "handle": "laude",
            "ide_detected": "vscode",
            "ide_detection_via": "env:VSCODE_PID",
        },
    )
    pid = reg.json()["participant_id"]

    resp = client.post(
        f"/sessions/{pid}/announce",
        json={"model_id": "gpt-5", "ide_override": "cursor"},
    )
    assert resp.status_code == 200, resp.text

    listing = client.get("/sessions", params={"party_id": "party_override"}).json()
    rows = listing if isinstance(listing, list) else listing.get("participants", [])
    matching = [r for r in rows if r.get("participant_id") == pid]
    assert matching[0]["ide_detected"] == "cursor"
    assert matching[0]["ide_detection_via"] == "agent_override"
    assert matching[0]["model_id"] == "gpt-5"


def test_announce_endpoint_unknown_participant_returns_200(client):
    """Silent no-op on unknown participant_id matches registry semantics
    and avoids leaking participant existence via 404 vs 200 distinction."""
    resp = client.post(
        "/sessions/does-not-exist/announce",
        json={"model_id": "claude-opus-4-7"},
    )
    assert resp.status_code == 200, resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_server.py -k "announce" -v`
Expected: FAIL — fields not surfacing or 404 from unknown route.

- [ ] **Step 3: Write minimal implementation**

In `helix_context/server.py`, modify the `/sessions/register` endpoint. Locate the call to `registry.register_participant(...)` and append three more kwargs (preserving the agent_kind/mcp_host from PR #26):

```python
                ide_detected=data.get("ide_detected"),
                ide_detection_via=data.get("ide_detection_via"),
                model_id=data.get("model_id"),
```

Add a new endpoint immediately AFTER the `/sessions/register` endpoint:

```python
@app.post("/sessions/{participant_id}/announce")
async def session_announce_endpoint(participant_id: str, request: Request):
    """Update model_id and (optionally) ide_detected on a participant.

    Called by the agent via the helix_announce MCP tool after the MCP
    adapter has registered the session. Body fields:

    - model_id: required string. Free-form, no validation.
    - ide_override: optional string. Replaces ide_detected and sets
      ide_detection_via='agent_override'.

    Silent no-op on unknown participant_id (matches heartbeat semantics
    and registry update_announcement contract).
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    model_id = data.get("model_id")
    ide_override = data.get("ide_override")
    if model_id is not None and not isinstance(model_id, str):
        raise HTTPException(status_code=400, detail="model_id must be a string")
    if ide_override is not None and not isinstance(ide_override, str):
        raise HTTPException(status_code=400, detail="ide_override must be a string")

    try:
        registry.update_announcement(
            participant_id=participant_id,
            model_id=model_id,
            ide_override=ide_override,
        )
    except Exception as exc:
        log.warning("Announce failed: %s", exc, exc_info=True)
        return JSONResponse(
            {"error": f"Announce failed: {exc}"},
            status_code=500,
        )

    return JSONResponse({"ok": True})
```

`registry` is the existing module-level registry singleton — re-use the same accessor pattern as the existing `/sessions/register` endpoint.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_server.py -k "announce" -v`
Expected: 4 passed.

Run: `python -m pytest tests/test_server.py -v`
Expected: no NEW failures.

- [ ] **Step 5: Commit**

```bash
git add helix_context/server.py tests/test_server.py
git commit -m "feat(server): /sessions/register passes through announce fields; new /announce endpoint

POST /sessions/register now forwards optional ide_detected/
ide_detection_via/model_id to registry.register_participant. New
POST /sessions/{participant_id}/announce endpoint maps to the
registry's update_announcement() PATCH for agent self-report. Silent
no-op on unknown participant_id matches heartbeat semantics."
```

---

## Task 6: AgentBridge — pass-through and announce

The HTTP-client wrapper used by `_register_with_registry` and (new) by the `helix_announce` MCP tool.

**Files:**
- Modify: `helix_context/bridge.py` — `register_participant` accepts new fields; new `announce()` method
- Test: `tests/test_bridge_registry.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bridge_registry.py`:

```python
def test_register_participant_sends_announce_fields(monkeypatch, bridge_with_capture):
    """AgentBridge.register_participant forwards ide_detected/via and model_id."""
    bridge, captured = bridge_with_capture
    bridge.register_participant(
        party_id="party_z",
        handle="laude",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
    )
    body = captured["body"]
    assert body["ide_detected"] == "vscode"
    assert body["ide_detection_via"] == "env:VSCODE_PID"
    # model_id NOT included when not passed (don't send NULL kwargs)
    assert "model_id" not in body


def test_register_participant_omits_unset_announce_fields(monkeypatch, bridge_with_capture):
    """When kwargs are None, the body omits them entirely."""
    bridge, captured = bridge_with_capture
    bridge.register_participant(party_id="party_y", handle="taude")
    body = captured["body"]
    assert "ide_detected" not in body
    assert "ide_detection_via" not in body
    assert "model_id" not in body


def test_announce_method_posts_to_announce_endpoint(monkeypatch, bridge_with_capture):
    """AgentBridge.announce(participant_id, model_id, ide_override) hits
    POST /sessions/{participant_id}/announce."""
    bridge, captured = bridge_with_capture
    # Pre-set the bridge's known participant_id so announce() can target it
    bridge._participant_id = "test-pid-123"
    bridge.announce(model_id="claude-opus-4-7", ide_override="cursor")
    assert captured["url"].endswith("/sessions/test-pid-123/announce")
    body = captured["body"]
    assert body["model_id"] == "claude-opus-4-7"
    assert body["ide_override"] == "cursor"


def test_announce_method_without_ide_override(monkeypatch, bridge_with_capture):
    bridge, captured = bridge_with_capture
    bridge._participant_id = "test-pid-456"
    bridge.announce(model_id="gpt-5")
    body = captured["body"]
    assert body["model_id"] == "gpt-5"
    assert "ide_override" not in body  # not included when None
```

If `bridge_with_capture` doesn't already exist as a fixture in this file, build one that captures the http_post payload. The existing PR-#26 test for `register_participant_sends_vendor_host` uses an inline `httpx.post` patch with a `captured` dict — copy that pattern into a fixture and yield `(bridge, captured)`. Do NOT mutate any existing fixture; the existing PR #26 tests should keep working unchanged. If you find yourself touching an existing fixture, stop and add a new one instead.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_bridge_registry.py -k "announce_fields or announce_method" -v`
Expected: FAIL — keyword args not accepted; AttributeError on `bridge.announce`.

- [ ] **Step 3: Write minimal implementation**

In `helix_context/bridge.py`, modify `AgentBridge.register_participant`. Add three new optional kwargs at the END of the signature:

```python
        ide_detected: Optional[str] = None,
        ide_detection_via: Optional[str] = None,
        model_id: Optional[str] = None,
```

Add three conditional body-building lines following the existing pattern (`if X is not None: body["X"] = X`):

```python
        if ide_detected is not None:
            body["ide_detected"] = ide_detected
        if ide_detection_via is not None:
            body["ide_detection_via"] = ide_detection_via
        if model_id is not None:
            body["model_id"] = model_id
```

Add a new method `announce` (place near `register_participant`):

```python
    def announce(
        self,
        model_id: str,
        ide_override: Optional[str] = None,
    ) -> bool:
        """POST to /sessions/{participant_id}/announce for self-report.

        Requires that ``register_participant`` has already been called
        successfully (sets ``self._participant_id``). If not yet
        registered, this is a no-op returning False — the agent shouldn't
        call announce before the adapter has registered.

        Returns True on HTTP 200, False otherwise. Failures are logged
        but non-fatal — model_id is best-effort.
        """
        if not getattr(self, "_participant_id", None):
            log.warning("announce() called before register_participant; skipping")
            return False

        body: Dict[str, Any] = {"model_id": model_id}
        if ide_override is not None:
            body["ide_override"] = ide_override

        try:
            self._http_post(
                f"/sessions/{self._participant_id}/announce",
                json_body=body,
            )
            return True
        except Exception as exc:
            log.warning("announce() failed: %s", exc)
            return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_bridge_registry.py -v`
Expected: existing tests + 4 new pass.

- [ ] **Step 5: Commit**

```bash
git add helix_context/bridge.py tests/test_bridge_registry.py
git commit -m "feat(bridge): forward announce fields; add announce() method

AgentBridge.register_participant accepts optional ide_detected/
ide_detection_via/model_id kwargs that are conditionally included
in the POST body. New announce() method POSTs to /sessions/
{participant_id}/announce for agent self-report; no-op if not yet
registered. Failures non-fatal — model_id is best-effort."
```

---

## Task 7: MCP integration — startup detect + helix_announce tool

The trigger surface — wires the adapter to call `detect_ide()` at startup, and exposes `helix_announce` as a new MCP tool the agent can call.

**Files:**
- Modify: `helix_context/mcp_server.py` — `_register_with_registry` calls `detect_ide()`; new `@mcp.tool() helix_announce(...)`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
def test_register_with_registry_calls_detect_ide(monkeypatch, mock_bridge):
    """_register_with_registry calls detect_ide() and forwards both fields."""
    monkeypatch.setenv("HELIX_MCP_HANDLE", "laude")
    monkeypatch.setenv("HELIX_PARTY_ID", "swift_wing21")
    monkeypatch.delenv("HELIX_MCP_HOST", raising=False)
    monkeypatch.setenv("VSCODE_PID", "9999")

    from helix_context import mcp_server
    mcp_server._register_with_registry()

    call = mock_bridge.register_participant_calls[-1]
    assert call["ide_detected"] == "vscode"
    assert call["ide_detection_via"] == "env:VSCODE_PID"


def test_register_with_registry_no_match_sends_none(monkeypatch, mock_bridge):
    """When fingerprint chain has no signal, ide_detected is None and via is no_match."""
    monkeypatch.setenv("HELIX_MCP_HANDLE", "laude")
    monkeypatch.setenv("HELIX_PARTY_ID", "swift_wing21")
    monkeypatch.delenv("HELIX_MCP_HOST", raising=False)
    monkeypatch.delenv("VSCODE_PID", raising=False)
    monkeypatch.delenv("CURSOR_TRACE_ID", raising=False)

    from helix_context import mcp_server
    mcp_server._register_with_registry()

    call = mock_bridge.register_participant_calls[-1]
    assert call["ide_detected"] is None
    assert call["ide_detection_via"] == "no_match"


def test_helix_announce_tool_calls_bridge_announce(monkeypatch, mock_bridge):
    """The helix_announce MCP tool delegates to AgentBridge.announce()."""
    from helix_context import mcp_server
    # Need to invoke the tool function. Tools are registered via @mcp.tool();
    # the test should access the underlying function. Use the same pattern
    # the file uses for testing other tools (often _ToolName or direct
    # function attribute on the module).
    result = mcp_server.helix_announce(
        model_id="claude-opus-4-7",
        ide_override=None,
    )
    # Confirm bridge.announce was called with the right args
    call = mock_bridge.announce_calls[-1]
    assert call["model_id"] == "claude-opus-4-7"
    assert call["ide_override"] is None
```

The `mock_bridge` fixture from PR #26 needs to grow an `announce_calls` list AND a stub `announce()` method. **The reset block at the end of the fixture must reset BOTH lists** so cross-test pollution doesn't sneak in (a missing reset would let one test's announce call leak into another's assertions).

After updating, run the existing PR #26 MCP tests (`test_register_with_registry_sends_env_vendor_host`, `test_register_with_registry_omits_unset_env`) explicitly to confirm the fixture mutation didn't break them:

```bash
python -m pytest tests/test_mcp_server.py -v
```

Expected: existing tests + 3 new pass; zero regressions.

Updated fixture:

```python
@pytest.fixture
def mock_bridge(monkeypatch):
    class _MockBridge:
        register_participant_calls = []
        announce_calls = []
        _participant_id = "mock-participant-id"

        def __init__(self, *args, **kwargs):
            pass

        def register_participant(self, **kwargs):
            type(self).register_participant_calls.append(kwargs)
            return "mock-participant-id"

        def announce(self, model_id, ide_override=None):
            type(self).announce_calls.append({
                "model_id": model_id,
                "ide_override": ide_override,
            })
            return True

    monkeypatch.setattr("helix_context.bridge.AgentBridge", _MockBridge)
    _MockBridge.register_participant_calls = []
    _MockBridge.announce_calls = []
    yield _MockBridge
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py -k "detect_ide or no_match or helix_announce" -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

In `helix_context/mcp_server.py`, modify `_register_with_registry` to call `detect_ide()` and forward results. Place the `from helix_context.launcher.ide_fingerprint import detect_ide` import inside the function (matches the existing late-binding pattern for `AgentBridge`):

```python
def _register_with_registry() -> None:
    """... (existing docstring; extend to mention IDE auto-detect)"""
    try:
        from helix_context.bridge import AgentBridge
        from helix_context.launcher.ide_fingerprint import detect_ide
    except Exception as exc:
        log.warning("Registry bridge import failed, skipping registration: %s", exc)
        return

    handle = os.environ.get("HELIX_MCP_HANDLE", f"mcp-{os.getpid()}")
    party_id = _default_party_id()
    mcp_host_env = os.environ.get("HELIX_MCP_HOST", "unknown")
    agent_kind_env = os.environ.get("HELIX_AGENT_KIND")
    workspace: Optional[str]
    try:
        workspace = os.getcwd()
    except Exception:
        workspace = None

    capabilities = ["mcp_tools", f"host:{mcp_host_env}"]
    mcp_host = None if mcp_host_env == "unknown" else mcp_host_env

    # IDE auto-detect via env-var fingerprint chain. Falls back to
    # (None, "no_match") when no signal — agent can later override via
    # helix_announce(ide_override=...).
    ide_detected, ide_detection_via = detect_ide()

    bridge = AgentBridge(helix_base_url=HELIX_URL)
    participant_id = bridge.register_participant(
        party_id=party_id,
        handle=handle,
        workspace=workspace,
        capabilities=capabilities,
        agent_kind=agent_kind_env,
        mcp_host=mcp_host,
        ide_detected=ide_detected,
        ide_detection_via=ide_detection_via,
        start_auto_heartbeat=True,
    )
    # Stash the bridge for the helix_announce tool to use later.
    if participant_id:
        global _registered_bridge
        _registered_bridge = bridge
        log.info(
            "Registered as %s (party=%s, kind=%s, host=%s, ide=%s/%s, pid=%d)",
            handle, party_id, agent_kind_env, mcp_host,
            ide_detected, ide_detection_via, os.getpid(),
        )
    else:
        log.warning(
            "Session registration failed (is helix running at %s?) "
            "— tool calls will still work",
            HELIX_URL,
        )
```

Add module-level state:

```python
# Set by _register_with_registry on success; consumed by the
# helix_announce MCP tool to PATCH the same participant row.
_registered_bridge: Optional[Any] = None
```

Add the new MCP tool. Place near other `@mcp.tool()`-decorated functions:

```python
@mcp.tool()
def helix_announce(
    model_id: str,
    ide_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Self-report the agent's model identity and (optionally) override
    the auto-detected IDE.

    Call this once per session, after your first ``helix_health`` call,
    so the dashboard can display your model in the agent badge tooltip.

    Args:
        model_id: Free-form model identifier. Examples:
            "claude-opus-4-7", "claude-sonnet-4-6", "gpt-5",
            "gemini-2-5-pro". The dashboard pretty-maps known IDs to
            display names; unknown IDs render verbatim.
        ide_override: Optional. Replaces the adapter's auto-detected
            IDE. Use only when env-var detection got it wrong.

    Returns:
        {"ok": True} on success, {"ok": False, "error": "..."} on
        failure. Failures are non-fatal — the rest of the session
        continues to work.
    """
    if _registered_bridge is None:
        return {
            "ok": False,
            "error": "Not yet registered with helix; announce skipped.",
        }
    success = _registered_bridge.announce(
        model_id=model_id,
        ide_override=ide_override,
    )
    return {"ok": success}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py -v`
Expected: existing tests + 3 new pass; no NEW regressions.

- [ ] **Step 5: Commit**

```bash
git add helix_context/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(mcp): _register_with_registry calls detect_ide; new helix_announce tool

MCP adapter now fingerprints the host IDE at register time via
ide_fingerprint.detect_ide() and forwards (ide_detected,
ide_detection_via) through AgentBridge.

New helix_announce MCP tool delegates to AgentBridge.announce() so
the agent can self-report its model_id and (optionally) override the
auto-detected IDE. The skill instructs the agent to call this once
per session after the first helix_health."
```

---

## Task 8: model_labels module + tests

Pretty-mapping for known `model_id` strings. Same pattern as `host_labels.py`.

**Files:**
- Create: `helix_context/launcher/model_labels.py`
- Test: `tests/test_model_labels.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_model_labels.py
"""Pretty-label module for model_id strings.

Same pattern as host_labels: known IDs map to canonical display form,
unknown IDs echo verbatim, None/empty returns None.
"""
from helix_context.launcher.model_labels import model_pretty


def test_known_anthropic_models():
    assert model_pretty("claude-opus-4-7") == "Claude Opus 4.7"
    assert model_pretty("claude-sonnet-4-6") == "Claude Sonnet 4.6"
    assert model_pretty("claude-haiku-4-5") == "Claude Haiku 4.5"


def test_known_anthropic_with_context_qualifier():
    assert model_pretty("claude-opus-4-7-1m") == "Claude Opus 4.7 (1M context)"


def test_known_openai_models():
    assert model_pretty("gpt-5") == "GPT-5"


def test_known_google_models():
    assert model_pretty("gemini-2-5-pro") == "Gemini 2.5 Pro"


def test_unknown_model_id_echoes_verbatim():
    """Don't fabricate a pretty form — echo what the agent reported."""
    assert model_pretty("acme-experimental-7b") == "acme-experimental-7b"


def test_none_returns_none():
    assert model_pretty(None) is None
    assert model_pretty("") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_model_labels.py -v`
Expected: ImportError.

- [ ] **Step 3: Write minimal implementation**

```python
# helix_context/launcher/model_labels.py
"""Pretty-label module for model_id strings reported via helix_announce.

Maps known model identifiers to canonical display form for the dashboard
tooltip. Unknown IDs echo verbatim — no fabrication. The map grows as
agents announce new IDs. There is no allowlist gate on what an agent
can report; the registry stores whatever the agent says, and this
module is a display-only convenience.
"""
from __future__ import annotations

from typing import Optional


_MODEL_MAP = {
    # Anthropic
    "claude-opus-4-7": "Claude Opus 4.7",
    "claude-opus-4-7-1m": "Claude Opus 4.7 (1M context)",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5": "Claude Haiku 4.5",
    # OpenAI
    "gpt-5": "GPT-5",
    # Google
    "gemini-2-5-pro": "Gemini 2.5 Pro",
}


def model_pretty(value: Optional[str]) -> Optional[str]:
    """Map a known model_id to its display form, or echo verbatim if
    unknown. None / empty input returns None."""
    if not value:
        return None
    return _MODEL_MAP.get(value, value)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_model_labels.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/launcher/model_labels.py tests/test_model_labels.py
git commit -m "feat(launcher): add model_labels pretty-map

Maps known model_id strings (claude-opus-4-7, gpt-5, gemini-2-5-pro,
etc.) to display form. Unknown IDs echo verbatim — no fabrication.
Same pattern as host_labels.py."
```

---

## Task 9: Collector — populate tooltip fields

Each panel entry now carries a tooltip-ready field bundle so the templates can render the progressive-disclosure tooltip.

**Files:**
- Modify: `helix_context/launcher/collector.py` — `_all_agents_panel`, `_disconnected_agents_panel`, `_participants_panel`
- Test: `tests/test_collector_host_label.py` (extend existing PR #26 tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_collector_host_label.py`:

```python
def test_all_agents_panel_emits_tooltip_fields_when_announced():
    """Entry has model_pretty/ide_pretty/agent_kind_pretty/ide_detection_via."""
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant(
        agent_kind="claude-code",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
        model_id="claude-opus-4-7",
    )
    panel = collector._all_agents_panel([p])
    entry = panel["entries"][0]
    tooltip = entry["tooltip"]
    assert tooltip["model_label"] == "Claude Opus 4.7"
    assert tooltip["ide_label"] == "VS Code"
    assert tooltip["agent_kind_label"] == "Claude Code"
    assert tooltip["ide_detection_via"] == "env:VSCODE_PID"


def test_all_agents_panel_emits_placeholders_when_missing():
    """Missing fields render as 'Not announced' / 'Not detected' / 'Not set'."""
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant()  # all announce fields None; agent_kind also None
    panel = collector._all_agents_panel([p])
    entry = panel["entries"][0]
    tooltip = entry["tooltip"]
    assert tooltip["model_label"] == "Not announced"
    assert tooltip["ide_label"] == "Not detected"
    assert tooltip["agent_kind_label"] == "Not set"


def test_all_agents_panel_emits_unknown_model_id_verbatim():
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant(model_id="acme-experimental-7b")
    panel = collector._all_agents_panel([p])
    assert panel["entries"][0]["tooltip"]["model_label"] == "acme-experimental-7b"


def test_disconnected_agents_panel_also_emits_tooltip():
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant(
        status="stale",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
        model_id="claude-opus-4-7",
    )
    panel = collector._disconnected_agents_panel([p])
    assert panel is not None
    tooltip = panel["entries"][0]["tooltip"]
    assert tooltip["model_label"] == "Claude Opus 4.7"
    assert tooltip["ide_label"] == "VS Code"


def test_participants_panel_also_emits_tooltip():
    collector = StateCollector(supervisor=_make_supervisor())
    p = _make_participant(
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
        model_id="gpt-5",
    )
    panel = collector._participants_panel([p])
    tooltip = panel["entries"][0]["tooltip"]
    assert tooltip["model_label"] == "GPT-5"
    assert tooltip["ide_label"] == "VS Code"
```

Update the `_make_participant` helper in this file to include the three new fields with `None` defaults (so existing PR-#26 tests still work):

```python
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
        "ide_detected": None,
        "ide_detection_via": None,
        "model_id": None,
    }
    base.update(overrides)
    return base
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_collector_host_label.py -v`
Expected: FAIL — `tooltip` key missing on entries.

- [ ] **Step 3: Write minimal implementation**

In `helix_context/launcher/collector.py`, at the top of the file add an import (alongside the existing `host_labels` import from PR #26):

```python
from helix_context.launcher.host_labels import compose_label, host_pretty, vendor_pretty
from helix_context.launcher.model_labels import model_pretty
```

Add a small private helper at module level (before `class StateCollector`) — keeps the three panel methods DRY:

```python
def _build_tooltip(participant: Dict[str, Any]) -> Dict[str, str]:
    """Compose the tooltip field bundle from a participant dict.

    Each label is either a pretty-mapped value or an explicit placeholder
    that hints at the cause:
      - model_label: "Not announced" when model_id is NULL
      - ide_label:   "Not detected" when ide_detected is NULL
      - agent_kind_label: "Not set"  when agent_kind is NULL
    The ide_detection_via line tells the operator how (or whether) the
    IDE value was obtained: "env:VSCODE_PID", "agent_override",
    "no_match", "explicit:HELIX_MCP_HOST".

    ide_label fallback chain (in order):
      1. ide_detected — populated by this change's adapter detect at
         register time, or by an agent's ide_override via helix_announce.
         Authoritative when present.
      2. mcp_host — PR #26's column. Sessions registered after PR #26 but
         before this change will have ide_detected=NULL but mcp_host set
         from HELIX_MCP_HOST env. Fall back so those sessions' chips
         still render correctly until they re-register.
      3. "Not detected" — explicit placeholder when neither column
         carries a value, so the tooltip surfaces the gap rather than
         hiding the row.

    There is no equivalent fallback chain for model_id (the column is
    new in this change; there is no prior column to fall back to).
    """
    return {
        "model_label": model_pretty(participant.get("model_id")) or "Not announced",
        "ide_label": (
            host_pretty(participant.get("ide_detected"))
            or host_pretty(participant.get("mcp_host"))   # PR #26 backward-compat fallback
            or "Not detected"
        ),
        "agent_kind_label": vendor_pretty(participant.get("agent_kind")) or "Not set",
        "ide_detection_via": participant.get("ide_detection_via") or "no_match",
    }
```

In each of `_all_agents_panel`, `_disconnected_agents_panel`, `_participants_panel`, add `"tooltip": _build_tooltip(participant)` to the entry dict (alongside the existing `host_label` from PR #26).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_collector_host_label.py -v`
Expected: all (PR-#26 + 5 new) pass.

- [ ] **Step 5: Commit**

```bash
git add helix_context/launcher/collector.py tests/test_collector_host_label.py
git commit -m "feat(collector): emit tooltip field bundle on agent panel entries

Each entry now carries tooltip = {model_label, ide_label,
agent_kind_label, ide_detection_via}. Pretty-mapped via host_labels
and model_labels; missing fields render as 'Not announced/detected/
set' explicit placeholders. ide_label falls back to PR #26 mcp_host
when ide_detected is NULL (backward-compat for sessions registered
before this change)."
```

---

## Task 10: Templates + CSS — render the tooltip

CSS-only `:hover` reveal. No JS.

**Files:**
- Modify: `helix_context/launcher/templates/components/agents_panel.html`
- Modify: `helix_context/launcher/templates/components/participants_panel.html`
- Modify: `helix_context/launcher/static/launcher.css`

- [ ] **Step 1: Locate every chip block to wrap**

The `chip--agent-host` chip from PR #26 appears in **three places** inside `agents_panel.html` — one per sub-view (active, all, historical disconnected). Find them all before editing:

```bash
grep -n "chip--agent-host" helix_context/launcher/templates/components/agents_panel.html
```

Expected output: 3 matches. The plan's wrapping change must apply to all three; missing one leaves an inconsistent dashboard.

In each match, the surrounding loop variable is `agent` (the iteration var of the outer `{% for agent in state.all_agents.entries %}`). Confirm by reading 5 lines of context above each match.

The participants panel has the same chip in **one place** (added in PR #26 Task 9):

```bash
grep -n "chip--agent-host" helix_context/launcher/templates/components/participants_panel.html
```

Expected: 1 match. The loop variable there is `p`, not `agent`.

- [ ] **Step 2: Modify agents_panel.html — wrap all three occurrences**

For each of the three matches found in Step 1, replace the chip block with a wrapped version. Pattern (loop variable is `agent`):

```jinja
{% if agent.host_label or agent.tooltip %}
<span class="chip-with-tooltip">
  {% if agent.host_label %}
    <span class="chip chip--agent-host">{{ agent.host_label }}</span>
  {% endif %}
  {% if agent.tooltip %}
    <div class="tooltip-content">
      <div class="tooltip-row"><span class="tooltip-key">Model:</span> {{ agent.tooltip.model_label }}</div>
      <div class="tooltip-row"><span class="tooltip-key">Wrapper:</span> {{ agent.tooltip.agent_kind_label }}</div>
      <div class="tooltip-row"><span class="tooltip-key">IDE:</span> {{ agent.tooltip.ide_label }}
        <span class="tooltip-via">({{ agent.tooltip.ide_detection_via }})</span>
      </div>
    </div>
  {% endif %}
</span>
{% endif %}
```

Apply the same wrapping in the **all** sub-view and the **historical disconnected** sub-view of the same template.

- [ ] **Step 3: Modify participants_panel.html**

Same wrapping treatment for the chip block added in PR #26 Task 9. Use `p.host_label` and `p.tooltip` since the loop variable is `p`.

- [ ] **Step 4: Add CSS**

Append to `helix_context/launcher/static/launcher.css`:

```css
/* Tooltip — CSS-only :hover reveal on the agent host chip. */
.chip-with-tooltip {
  position: relative;
  display: inline-block;
}

.chip-with-tooltip .tooltip-content {
  position: absolute;
  bottom: 100%;
  left: 0;
  margin-bottom: 6px;
  padding: 8px 12px;
  background: rgba(20, 20, 24, 0.96);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 4px;
  white-space: nowrap;
  pointer-events: none;
  opacity: 0;
  visibility: hidden;
  transition: opacity 120ms ease, visibility 0s linear 120ms;
  z-index: 10;
  font-size: 12px;
  line-height: 1.6;
  color: rgba(255, 255, 255, 0.9);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
}

.chip-with-tooltip:hover .tooltip-content {
  opacity: 1;
  visibility: visible;
  transition-delay: 0s;
}

.chip-with-tooltip .tooltip-row {
  display: flex;
  gap: 8px;
}

.chip-with-tooltip .tooltip-key {
  font-weight: 600;
  color: rgba(255, 255, 255, 0.6);
  min-width: 64px;
}

.chip-with-tooltip .tooltip-via {
  color: rgba(255, 255, 255, 0.4);
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, monospace;
}
```

Match the existing CSS file's style (look at how chips already in the file are styled — colors, transitions, etc.). Adjust hex/rgba values if the file's palette differs from the values above.

- [ ] **Step 5: Smoke test the templates**

Same approach as PR #26 Task 9 smoke test: render the templates with synthetic state and grep for the tooltip text.

```bash
cd f:/Projects/helix-context && python -c "
from jinja2 import Environment, FileSystemLoader, select_autoescape, ChainableUndefined
env = Environment(
    loader=FileSystemLoader('helix_context/launcher/templates'),
    autoescape=select_autoescape(['html', 'xml']),
    undefined=ChainableUndefined,
)
state = {
    'all_agents': {
        'count': 1, 'active_count': 1,
        'entries': [{
            'handle': 'laude', 'party_id': 'swift_wing21',
            'workspace': 'F:\\\\Projects', 'status': 'active',
            'last_seen_s_ago': 1.2, 'participant_id': 'abc12345-deadbeef',
            'participant_id_short': 'abc12345', 'identifier': 'swift_wing21',
            'host_label': 'Claude Code + VS Code',
            'tooltip': {
                'model_label': 'Claude Opus 4.7',
                'ide_label': 'VS Code',
                'agent_kind_label': 'Claude Code',
                'ide_detection_via': 'env:VSCODE_PID',
            },
        }],
    },
    'participants': {'count': 0, 'identity_total_count': 0, 'total_count': 0, 'entries': []},
}
html = env.get_template('components/agents_panel.html').render(state=state)
assert 'tooltip-content' in html, 'tooltip wrapper missing'
assert 'Claude Opus 4.7' in html, 'model label missing'
assert 'env:VSCODE_PID' in html, 'detection via missing'
print('PASS')
"
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add helix_context/launcher/templates/components/agents_panel.html \
        helix_context/launcher/templates/components/participants_panel.html \
        helix_context/launcher/static/launcher.css
git commit -m "feat(launcher): add :hover tooltip to agent host chip

CSS-only reveal — no JS. Tooltip shows Model / Wrapper / IDE rows
plus the ide_detection_via diagnostic. Wrapping applied in
agents_panel (active/all/disconnected sub-views) and
participants_panel for parity."
```

---

## Task 11: Skill update

One sentence in the Workflow section.

**Files:**
- Modify: `skills/helix/SKILL.md`

- [ ] **Step 1: Add the workflow item**

In `skills/helix/SKILL.md`, locate the `## Workflow` section (currently has 6 numbered items). Append item 7:

```markdown
7. After your first `helix_health` call in a session, also call
   `helix_announce(model_id=...)` once with your model identifier
   (e.g., `"claude-opus-4-7"`, `"gpt-5"`, `"gemini-2-5-pro"`) so the
   dashboard can display your model in the agent badge tooltip. If
   the IDE auto-detection got it wrong, pass `ide_override=...` to
   correct it.
```

- [ ] **Step 2: Commit**

```bash
git add skills/helix/SKILL.md
git commit -m "docs(skill): instruct agents to call helix_announce

One-line workflow addition: after first helix_health, agents call
helix_announce(model_id=...) so the dashboard tooltip can display
the model. Optional ide_override when env detect was wrong."
```

---

## Task 12: End-to-end plumbing test

Verifies the whole chain: env → adapter detect → register → announce → list → collector entry.

**Files:**
- Create: `tests/test_helix_announce_plumbing.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end plumbing for helix_announce + IDE auto-detect.

Exercises: VSCODE_PID env → adapter detect_ide → POST /sessions/register
→ POST /sessions/{participant_id}/announce → GET /sessions →
collector entry has all the right fields. Catches regressions where
any layer in the chain drops something.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_clean_genome(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIX_GENOME_PATH", str(tmp_path / "genome.db"))
    from helix_context.server import build_app  # or whatever the file's app factory is
    app = build_app()
    return app


def test_register_then_announce_round_trips_through_get_sessions(app_with_clean_genome):
    """Full plumbing: simulated MCP startup populates ide_detected via the
    body fields the adapter would have sent; agent then announces model_id;
    GET /sessions reflects both."""
    client = TestClient(app_with_clean_genome)

    # Stage 1 — adapter posts the detected IDE at register time
    reg_resp = client.post(
        "/sessions/register",
        json={
            "party_id": "swift_wing21",
            "handle": "laude",
            "workspace": "F:\\Projects",
            "ide_detected": "vscode",
            "ide_detection_via": "env:VSCODE_PID",
            "agent_kind": "claude-code",  # backward-compat from PR #26
        },
    )
    assert reg_resp.status_code == 200, reg_resp.text
    pid = reg_resp.json()["participant_id"]

    # Stage 2 — agent announces its model
    ann_resp = client.post(
        f"/sessions/{pid}/announce",
        json={"model_id": "claude-opus-4-7"},
    )
    assert ann_resp.status_code == 200, ann_resp.text

    # Stage 3 — GET projection reflects both
    listing = client.get("/sessions", params={"party_id": "swift_wing21"}).json()
    rows = listing if isinstance(listing, list) else listing.get("participants", [])
    matching = [r for r in rows if r.get("participant_id") == pid]
    assert len(matching) == 1, f"expected one matching row, got: {rows}"
    row = matching[0]
    assert row["ide_detected"] == "vscode"
    assert row["ide_detection_via"] == "env:VSCODE_PID"
    assert row["model_id"] == "claude-opus-4-7"
    assert row["agent_kind"] == "claude-code"


def test_announce_with_ide_override_changes_via_to_agent_override(app_with_clean_genome):
    """Agent override path: register with one IDE, announce with override,
    confirm ide_detection_via flips to agent_override."""
    client = TestClient(app_with_clean_genome)
    reg = client.post(
        "/sessions/register",
        json={
            "party_id": "party_override",
            "handle": "laude",
            "ide_detected": "vscode",
            "ide_detection_via": "env:VSCODE_PID",
        },
    )
    pid = reg.json()["participant_id"]

    client.post(
        f"/sessions/{pid}/announce",
        json={"model_id": "gpt-5", "ide_override": "cursor"},
    )

    listing = client.get("/sessions", params={"party_id": "party_override"}).json()
    rows = listing if isinstance(listing, list) else listing.get("participants", [])
    row = rows[0] if rows else {}
    assert row["ide_detected"] == "cursor"
    assert row["ide_detection_via"] == "agent_override"
    assert row["model_id"] == "gpt-5"
```

The fixture body shape (`build_app` vs module-level `app`) follows whatever the existing PR #26 plumbing test (`tests/test_vendor_host_plumbing.py`) used — adapt to match.

- [ ] **Step 2: Run the test**

Run: `python -m pytest tests/test_helix_announce_plumbing.py -v`
Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_helix_announce_plumbing.py
git commit -m "test: end-to-end plumbing for helix_announce + IDE auto-detect

Verifies env-detected IDE survives /sessions/register → list, plus
the announce endpoint round-trips model_id and the ide_override
flow flips ide_detection_via to 'agent_override'. Single integration
test guards against future drops in any layer."
```

---

## Task 13: Documentation update

Reflect the new fields and endpoint in the routing guide and SESSION_REGISTRY spec.

**Files:**
- Modify: `docs/clients/claude-code.md`
- Modify: `docs/architecture/SESSION_REGISTRY.md`

- [ ] **Step 1: Update docs/clients/claude-code.md**

Find the existing 2026-05-05 paragraph from PR #26 Task 11 (the one starting "As of 2026-05-05, `HELIX_AGENT_KIND` and `HELIX_MCP_HOST` are persisted as first-class columns..."). Add a new paragraph immediately after it:

```markdown
As of 2026-05-06, the MCP adapter additionally **auto-detects** the
host IDE from intentional env vars (`VSCODE_PID`, `CURSOR_TRACE_ID`)
and the agent **self-reports** its model via the new `helix_announce`
MCP tool. Together they populate three new columns on the
`participants` row — `ide_detected`, `ide_detection_via`, `model_id`
— and the dashboard renders the full identity in a tooltip on the
agent host chip. See
[`SESSION_REGISTRY.md`](../architecture/SESSION_REGISTRY.md#announce-endpoint)
for the API and [`skills/helix/SKILL.md`](../../skills/helix/SKILL.md#workflow)
for the agent contract.

The legacy `HELIX_MCP_HOST` env var is now optional — the adapter
will detect the IDE without it. Set it only when you want to override
the auto-detection (e.g., running Claude Code from a non-VS-Code
terminal but want the chip to say `"vscode"` anyway).
```

- [ ] **Step 2: Update docs/architecture/SESSION_REGISTRY.md**

Locate the participants table schema (the existing one updated in PR #26 Task 11). Add three more bullets to the column list:

```markdown
- `ide_detected`       — adapter env-var fingerprint at register time
                         ("vscode", "cursor", or NULL on no_match) — added 2026-05-06
- `ide_detection_via`  — how IDE was determined ("env:VSCODE_PID",
                         "explicit:HELIX_MCP_HOST", "agent_override",
                         "no_match") — added 2026-05-06
- `model_id`           — agent self-reported via helix_announce
                         (free-form, NULL until announced) — added 2026-05-06
```

Add a new endpoint subsection (where other endpoints are documented):

```markdown
### POST /sessions/{participant_id}/announce  *(added 2026-05-06)*

Agent self-report endpoint. Called once per session by the agent via
the `helix_announce` MCP tool, after the MCP adapter has registered
the participant.

```bash
curl -X POST http://127.0.0.1:11437/sessions/<participant_id>/announce \
  -H "Content-Type: application/json" \
  -d '{
    "model_id": "claude-opus-4-7",
    "ide_override": "cursor"
  }'
```

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `model_id` | string | yes | Free-form model identifier. No allowlist validation. |
| `ide_override` | string | no | Replaces `ide_detected` and forces `ide_detection_via='agent_override'`. |

Silent no-op on unknown `participant_id` (returns 200) — matches
heartbeat semantics. Multiple calls are idempotent (last-write-wins).
```

- [ ] **Step 3: Update the existing /sessions/register reference table**

Find the `POST /sessions/register` section in `SESSION_REGISTRY.md` (PR #26 Task 11 fix already added `agent_kind` and `mcp_host` rows). Add three more rows to the same body field table:

```markdown
| `ide_detected` | string | no | Adapter env-var fingerprint result. Sourced from `helix_context.launcher.ide_fingerprint.detect_ide()`. Added 2026-05-06. |
| `ide_detection_via` | string | no | The evidence behind `ide_detected`. Added 2026-05-06. |
| `model_id` | string | no | Optional at register time — typically supplied later via the announce endpoint. Added 2026-05-06. |
```

- [ ] **Step 4: Commit**

```bash
git add docs/clients/claude-code.md docs/architecture/SESSION_REGISTRY.md
git commit -m "docs: document helix_announce + IDE auto-detect

claude-code.md: explain how auto-detect + self-report combine to
populate the new ide_detected/ide_detection_via/model_id columns;
note that HELIX_MCP_HOST is now optional override.

SESSION_REGISTRY.md: add the three new column descriptions, the
POST /sessions/{participant_id}/announce endpoint section, and the
new optional fields on the existing /sessions/register reference."
```

---

## Manual verification (post-merge)

Not a TDD step — a human-in-the-loop check that the dashboard actually shows what we want.

- [ ] Restart the helix server with this branch.
- [ ] Restart Claude Code with the .mcp.json from `docs/clients/claude-code.md` (env block from PR #26 still works; `HELIX_MCP_HOST` is now optional).
- [ ] Open the launcher dashboard.
- [ ] Confirm: Agents panel shows the chip from PR #26.
- [ ] Hover the chip → tooltip appears with Model / Wrapper / IDE rows.
- [ ] If you've added the workflow line to your skill / asked Claude in-session, verify Model row shows the model you announced (e.g., "Claude Opus 4.7"). Otherwise it should show "Not announced".
- [ ] IDE row should show the detected IDE + the via diagnostic — for VS Code: "VS Code (env:VSCODE_PID)".
- [ ] Repeat with a Codex session (no client config change). Tooltip's IDE row should now show "VS Code (env:VSCODE_PID)" instead of "Codex" — the auto-detect worked.

---

## Out of scope (deferred follow-ups)

- Per-vendor `.mcp.json` template installer (so users don't have to
  hand-curate the env block — touch the launcher's installer).
- Backfill of pre-existing participant rows from `mcp_host` →
  `ide_detected`. Write-only mitigation; new sessions auto-populate.
- Removal of the now-redundant `mcp_host` column. Wait for an
  adoption period; do as a separate cleanup plan.
- Allowlist validation on `model_id` (would force us to chase every
  new model release).
- Real-time model-version updates if the agent switches mid-session
  (rare; current design is "last-write-wins" so a second
  `helix_announce` call already handles it).
- Federation: synchronizing `ide_detected` / `model_id` across remote
  parties.
- Antigravity / Claude Desktop fingerprint branches in
  `ide_fingerprint.py` (add as we collect telltale env vars per host).

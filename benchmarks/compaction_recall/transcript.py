"""Reconstruct compaction events from Claude Code transcripts.

A transcript is a JSONL tree, not a list: rewinds leave abandoned branches in
the file, and resumed sessions copy history into a new file. So the history
before a compaction is the ``parentUuid`` chain walked back from the boundary
(crossing earlier boundaries through ``logicalParentUuid``), and the history
after it is the set of lines descending from the boundary. Events are keyed by
the boundary's uuid so a compaction copied into several files counts once.

Thinking blocks are dropped everywhere: they are not part of the archive an
agent would search, and earlier turns' thinking is not resent to the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Attachment types whose content the model actually reads. Tool, agent and MCP
# listing deltas are omitted: they are in context but they are not memory.
_TODAY_ATTACHMENTS = {
    "file", "plan_file_reference", "invoked_skills", "edited_text_file",
    "nested_memory", "compact_file_reference", "hook_additional_context",
}
# Of those, the ones worth putting in a retrieval haystack (skill bodies are
# boilerplate that every session repeats).
_HAYSTACK_ATTACHMENTS = _TODAY_ATTACHMENTS - {"invoked_skills", "compact_file_reference"}


@dataclass
class Unit:
    uuid: str
    role: str  # user | assistant | tool_use | tool_result | attachment
    text: str
    attachment_type: str | None = None


@dataclass
class Event:
    boundary_uuid: str
    source_file: str
    trigger: str | None
    pre_tokens: int | None
    post_tokens: int | None
    timestamp: str | None
    pre: list[Unit] = field(default_factory=list)
    summary: str = ""
    preserved: list[Unit] = field(default_factory=list)
    post_attachments: list[Unit] = field(default_factory=list)
    post: list[Unit] = field(default_factory=list)

    @property
    def plan_text(self) -> str:
        return "\n".join(u.text for u in self.post_attachments
                         if u.attachment_type == "plan_file_reference")


def _flatten(value) -> str:
    """Tool inputs and results as plain text: no JSON escaping of backslashes."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("type") == "text" and "text" in value:
            return str(value["text"])
        return "\n".join(f"{k}: {_flatten(v)}" for k, v in value.items()
                         if k not in ("type", "id", "tool_use_id"))
    if isinstance(value, list):
        return "\n".join(_flatten(v) for v in value)
    return str(value)


def attachment_text(att: dict) -> str:
    t = att.get("type")
    if t == "file":
        f = (att.get("content") or {}).get("file") or {}
        return f"{f.get('filePath', att.get('filename', ''))}\n{f.get('content', '')}"
    if t == "plan_file_reference":
        return str(att.get("planContent", ""))
    if t == "invoked_skills":
        return "\n".join(f"{s.get('name', '')}\n{s.get('content', '')}"
                         for s in att.get("skills") or [])
    if t == "compact_file_reference":
        return str(att.get("filename") or att.get("displayPath") or "")
    if t in ("hook_additional_context", "nested_memory", "edited_text_file"):
        return _flatten({k: v for k, v in att.items() if k not in ("type", "hookName",
                                                                     "toolUseID", "hookEvent")})
    return ""


def line_units(d: dict) -> list[Unit]:
    """The readable units of one transcript line (possibly none)."""
    uuid = d.get("uuid", "")
    if d.get("type") == "attachment":
        att = d.get("attachment") or {}
        if att.get("type") in _TODAY_ATTACHMENTS:
            text = attachment_text(att)
            return [Unit(uuid, "attachment", text, att.get("type"))] if text else []
        return []
    if d.get("type") not in ("user", "assistant"):
        return []
    msg = d.get("message") or {}
    content = msg.get("content")
    role = d["type"]
    if isinstance(content, str):
        return [Unit(uuid, role, content)] if content.strip() else []
    out: list[Unit] = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text" and b.get("text", "").strip():
            out.append(Unit(uuid, role, b["text"]))
        elif bt == "tool_use":
            out.append(Unit(uuid, "tool_use", f"{b.get('name', '')}\n{_flatten(b.get('input'))}"))
        elif bt == "tool_result":
            text = _flatten(b.get("content"))
            if text.strip():
                out.append(Unit(uuid, "tool_result", text))
    return out


def _read_lines(path: Path) -> list[dict]:
    lines = []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                lines.append(json.loads(ln))
            except json.JSONDecodeError:
                continue  # a torn final line from a live session
    return lines


def _events_in_file(path: Path) -> list[Event]:
    lines = _read_lines(path)
    by_uuid: dict[str, dict] = {}
    for d in lines:
        u = d.get("uuid")
        if u and u not in by_uuid:
            by_uuid[u] = d

    events = []
    for idx, d in enumerate(lines):
        if d.get("subtype") != "compact_boundary" or d.get("isSidechain"):
            continue
        meta = d.get("compactMetadata") or {}
        ev = Event(boundary_uuid=d["uuid"], source_file=str(path),
                   trigger=meta.get("trigger"), pre_tokens=meta.get("preTokens"),
                   post_tokens=meta.get("postTokens"), timestamp=d.get("timestamp"))

        # Pre: walk the chain back from the boundary.
        chain: list[dict] = []
        seen: set[str] = set()
        cur = d.get("logicalParentUuid") or d.get("parentUuid")
        while cur and cur in by_uuid and cur not in seen:
            seen.add(cur)
            line = by_uuid[cur]
            chain.append(line)
            cur = line.get("parentUuid") or line.get("logicalParentUuid")
        for line in reversed(chain):
            if line.get("isSidechain") or line.get("isCompactSummary"):
                if line.get("isCompactSummary"):
                    # an earlier compaction's summary is part of the archive
                    ev.pre.append(Unit(line["uuid"], "user", _flatten(
                        (line.get("message") or {}).get("content"))))
                continue
            ev.pre.extend(line_units(line))

        preserved = (meta.get("preservedMessages") or {})
        for u in preserved.get("allUuids") or preserved.get("uuids") or []:
            if u in by_uuid:
                ev.preserved.extend(line_units(by_uuid[u]))

        # Post: descendants of the boundary, in file order, until the next one.
        # After a partial compaction the continuation hangs off the preserved
        # tail (the boundary's logical parent), not off the boundary itself.
        desc = {d["uuid"]}
        if meta.get("preservedSegment") or meta.get("preservedMessages"):
            seg = meta.get("preservedSegment") or {}
            desc |= {u for u in (seg.get("tailUuid"), d.get("logicalParentUuid")) if u}
        leading = True
        for line in lines[idx + 1:]:
            u = line.get("uuid")
            parent = line.get("parentUuid") or line.get("logicalParentUuid")
            if not u or u in desc or parent not in desc or line.get("isSidechain"):
                continue
            if line.get("subtype") == "compact_boundary":
                break
            desc.add(u)
            if line.get("isCompactSummary"):
                ev.summary = _flatten((line.get("message") or {}).get("content"))
                continue
            units = line_units(line)
            if line.get("type") == "assistant":
                leading = False
            if leading and line.get("type") == "attachment":
                # what the harness re-injects before the agent's first turn
                ev.post_attachments.extend(units)
                continue
            ev.post.extend(units)
        events.append(ev)
    return events


def haystack_units(ev: Event) -> list[Unit]:
    """Pre-compaction units an archive would hold."""
    return [u for u in ev.pre
            if u.role != "attachment" or u.attachment_type in _HAYSTACK_ATTACHMENTS]


def load_events(paths: Iterable[Path]) -> list[Event]:
    """All compaction events, deduped by boundary uuid (longest post-window wins)."""
    best: dict[str, Event] = {}
    for p in paths:
        for ev in _events_in_file(Path(p)):
            cur = best.get(ev.boundary_uuid)
            if cur is None or len(ev.post) > len(cur.post):
                best[ev.boundary_uuid] = ev
    return sorted(best.values(), key=lambda e: (e.timestamp or "", e.boundary_uuid))

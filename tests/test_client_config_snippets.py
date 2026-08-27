"""Contracts for synchronized native MCP examples in client guides."""
from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib

import pytest

from cymatix_context.integrations.host_profiles import render_mcp_snippet

ROOT = Path(__file__).resolve().parents[1]
GUIDES = {
    "claude-code": ROOT / "docs/clients/claude-code.md",
    "codex": ROOT / "docs/clients/codex.md",
    "gemini-cli": ROOT / "docs/clients/gemini-cli.md",
    "antigravity": ROOT / "docs/clients/antigravity.md",
}

NATIVE_MCP_DESTINATIONS = {
    "claude-code": (".mcp.json", "~/.claude.json"),
    "codex": (".codex/config.toml", "~/.codex/config.toml"),
}


def _generated_block(text: str, profile_id: str) -> str:
    start = f"<!-- BEGIN GENERATED MCP: {profile_id} -->"
    end = f"<!-- END GENERATED MCP: {profile_id} -->"
    return text.split(start, 1)[1].split(end, 1)[0].strip()


@pytest.mark.parametrize("profile_id", GUIDES)
def test_client_guide_mcp_block_matches_renderer(profile_id):
    text = GUIDES[profile_id].read_text(encoding="utf-8")
    assert _generated_block(text, profile_id) == render_mcp_snippet(profile_id)


@pytest.mark.parametrize("profile_id,destinations", NATIVE_MCP_DESTINATIONS.items())
def test_native_mcp_destinations_are_named(profile_id, destinations):
    """Guides name every supported workspace and user MCP config path."""
    text = GUIDES[profile_id].read_text(encoding="utf-8")
    for destination in destinations:
        assert destination in text


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

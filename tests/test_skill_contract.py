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

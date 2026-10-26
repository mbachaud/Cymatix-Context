"""CLA Assistant Lite allowlist covers the identities agent sessions commit as.

contributor-assistant/github-action@v2.6.1 names each committer by the GitHub
login the author email resolves to (falling back to the raw author name only
when the email maps to no account), and matches allowlist entries with a
case-sensitive ``===``. ``Claude <noreply@anthropic.com>`` resolves to the
login ``claude``, so ``Claude`` alone did not match and PR #529 was blocked.
"""

from pathlib import Path

import yaml

CLA_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cla.yml"


def _allowlist() -> list[str]:
    workflow = yaml.safe_load(CLA_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["contributor-signup"]["steps"]
    cla_step = next(s for s in steps if s.get("uses", "").startswith("contributor-assistant/"))
    return [p.strip() for p in cla_step["with"]["allowlist"].split(",")]


def test_claude_login_allowlisted():
    # GitHub login for noreply@anthropic.com (matched by exact, case-sensitive compare)
    assert "claude" in _allowlist()


def test_claude_author_name_allowlisted():
    # Fallback when the author email is linked to no GitHub account
    assert "Claude" in _allowlist()

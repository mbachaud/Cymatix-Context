"""Pretty-label composition for the dashboard agent badges.

Exercises:
- known vendors → pretty form
- known hosts → pretty form (incl. "vscode" → "VS Code")
- unknown values → echoed verbatim (no silent drop)
- compose_label with both / vendor-only / host-only / neither

Also absorbs `test_model_labels.py` (see the ``TestModelLabels`` section
below) — `model_labels.py` is a sibling pure-function module that follows
"the same pattern as host_labels" (its own docstring's words): known IDs
map to canonical display form, unknown IDs echo verbatim, None/empty
returns None.
"""
from helix_context.launcher.host_labels import (
    vendor_pretty,
    host_pretty,
    compose_label,
)
from helix_context.launcher.model_labels import model_pretty


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


def test_compose_label_dedupes_case_insensitive():
    """Asymmetric mapping: 'codex' vendor maps to 'Codex' via _VENDOR_MAP,
    but the same string as a host echoes verbatim ('codex'). Case-insensitive
    dedup collapses them to a single 'Codex' chip rather than 'Codex + codex'.
    Observed in the wild: Codex's MCP wrapper sends agent_kind=mcp_host=codex."""
    assert compose_label("codex", "codex") == "Codex"


class TestModelLabels:
    """Absorbed from the deleted `test_model_labels.py`.

    model_labels.py maps known model_id strings reported via helix_announce
    to a canonical display form for the dashboard tooltip; unknown IDs echo
    verbatim (no fabrication) and None/empty returns None — the same
    contract host_pretty/vendor_pretty implement above, just for models.
    """

    def test_known_anthropic_models(self):
        assert model_pretty("claude-opus-4-7") == "Claude Opus 4.7"
        assert model_pretty("claude-sonnet-4-6") == "Claude Sonnet 4.6"
        assert model_pretty("claude-haiku-4-5") == "Claude Haiku 4.5"

    def test_known_anthropic_with_context_qualifier(self):
        assert model_pretty("claude-opus-4-7-1m") == "Claude Opus 4.7 (1M context)"

    def test_known_openai_models(self):
        assert model_pretty("gpt-5") == "GPT-5"

    def test_known_google_models(self):
        assert model_pretty("gemini-2-5-pro") == "Gemini 2.5 Pro"

    def test_unknown_model_id_echoes_verbatim(self):
        """Don't fabricate a pretty form — echo what the agent reported."""
        assert model_pretty("acme-experimental-7b") == "acme-experimental-7b"

    def test_none_returns_none(self):
        assert model_pretty(None) is None
        assert model_pretty("") is None

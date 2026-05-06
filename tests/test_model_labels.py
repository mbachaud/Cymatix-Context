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

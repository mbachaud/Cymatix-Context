"""Tests for query-aware source window selection."""

from helix_context.retrieval.relevance_window import best_relevance_window


def test_best_relevance_window_finds_relevant_late_section():
    text = (
        "intro\n"
        + ("unrelated configuration prose\n" * 300)
        + "### Claim types\n"
        + "- `path_value`\n"
        + "- `config_value`\n"
        + "agent context index spec notes\n"
    )

    window = best_relevance_window(
        text,
        "claim_type allowed values helix claims layer specification",
        max_chars=800,
        overlap=100,
    )

    assert "path_value" in window
    assert "Claim types" in window


def test_best_relevance_window_falls_back_to_head_without_hits():
    text = "head marker\n" + ("body\n" * 1000)

    window = best_relevance_window(
        text,
        "nonexistent needle",
        max_chars=50,
    )

    assert window.startswith("head marker")
    assert len(window) == 50


def test_best_relevance_window_returns_full_short_text():
    text = "short claim_type path_value"

    assert best_relevance_window(text, "claim type", max_chars=500) == text

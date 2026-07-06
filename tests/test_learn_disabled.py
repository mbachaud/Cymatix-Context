"""HELIX_DISABLE_LEARN gate — read-only / ephemeral serving.

When set, /v1/chat/completions must not fire helix.learn (Stage-6 persist), so a
bench serve can't self-contaminate its corpus with echo genes (gap A2). See
docs/benchmarks/2026-07-05-sike-bedsweep-issue-resolutions.md.
"""

import pytest

from helix_context.server.helpers import _learn_disabled


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " on "])
def test_learn_disabled_truthy(monkeypatch, value):
    monkeypatch.setenv("HELIX_DISABLE_LEARN", value)
    assert _learn_disabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nope"])
def test_learn_enabled_by_default_or_falsy(monkeypatch, value):
    monkeypatch.setenv("HELIX_DISABLE_LEARN", value)
    assert _learn_disabled() is False


def test_learn_enabled_when_unset(monkeypatch):
    monkeypatch.delenv("HELIX_DISABLE_LEARN", raising=False)
    assert _learn_disabled() is False

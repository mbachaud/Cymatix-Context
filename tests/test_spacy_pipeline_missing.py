"""Fresh-install gate for the missing en_core_web_sm pipeline (#313).

Every other test that drives CpuTagger.pack() carries the
``requires_spacy_model`` skip from tests/conftest.py. That is correct for
those tests, but it means the fresh-install CI leg -- the one that runs
before the pipeline is downloaded -- skips its way to green without ever
looking at what a fresh-install user actually sees. Review of PR #348
made the point: a generic message, a dropped install command, or a
different HTTP status would all have left that leg passing.

This module is the deliberate exception. It never skips, so it runs in
the modelless leg (where the absence is real) and again in the leg that
has the pipeline (where the fixture reproduces the absence), and it pins
the two things the fix promises:

  * ``_get_nlp()`` raises OSError whose message names the download
    command, instead of letting spaCy's raw [E050] through;
  * ``POST /ingest`` answers 422 and carries that same command in the
    JSON error body, because routes_ingest.py interpolates ``str(exc)``
    into it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("spacy", reason="the tagger imports spacy at runtime via _get_nlp()")

import cymatix_context.tagger as tagger_mod
from tests.conftest import HAS_SPACY_MODEL, make_client

# The command the fix promises the operator, verbatim. It stays unpinned
# on purpose: the setup paths pin the build (en_core_web_sm-3.8.0
# --direct, see docs/SETUP.md), but the runtime error has to keep working
# on whatever spaCy minor the environment happens to carry.
DOWNLOAD_COMMAND = "python -m spacy download en_core_web_sm"

# What spaCy raises when the pipeline is not installed, quoted from the
# modelless run recorded for this branch.
_E050 = (
    "[E050] Can't find model 'en_core_web_sm'. It doesn't seem to be a "
    "Python package or a valid path to a data directory."
)


@pytest.fixture
def missing_pipeline(monkeypatch):
    """Put the process in the fresh-install state for one test.

    A modelless environment is already in it; there the fixture only
    drops the process-wide ``_nlp`` cache so the loader really runs.
    Where the pipeline IS installed, it additionally makes ``spacy.load``
    raise exactly what a missing pipeline raises, so one set of
    assertions gates both CI legs instead of quietly passing on one.
    ``monkeypatch`` restores the cache and the loader afterwards.
    """
    monkeypatch.setattr(tagger_mod, "_nlp", None)
    if HAS_SPACY_MODEL:
        import spacy

        real_load = spacy.load

        def _fail_for_the_pipeline(name, *args, **kwargs):
            if name == "en_core_web_sm":
                raise OSError(_E050)
            return real_load(name, *args, **kwargs)

        monkeypatch.setattr(spacy, "load", _fail_for_the_pipeline)


def test_tagger_error_names_the_download_command(missing_pipeline):
    """The tagger's own error, the one every other path quotes."""
    with pytest.raises(OSError) as excinfo:
        tagger_mod._get_nlp()

    message = str(excinfo.value)
    assert DOWNLOAD_COMMAND in message, (
        "the missing-pipeline error must name the install command verbatim; got: "
        + message
    )
    assert "en_core_web_sm" in message
    # Single line: routes_ingest.py drops str(exc) into a JSON error body.
    assert "\n" not in message
    # Raised with `from exc`, so the original spaCy error stays available.
    assert excinfo.value.__cause__ is not None


def test_ingest_returns_422_naming_the_download_command(missing_pipeline):
    """The response a fresh-install user actually gets from the server."""
    client = make_client()

    response = client.post("/ingest", json={
        "content": "Authentication module uses JWT refresh tokens.",
        "content_type": "text",
    })

    assert response.status_code == 422
    error = response.json()["error"]
    assert DOWNLOAD_COMMAND in error, (
        "the /ingest error body must name the install command verbatim; got: " + error
    )

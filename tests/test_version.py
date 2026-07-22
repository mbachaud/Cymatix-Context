"""``__version__`` single-source checks (v0.8.0 release-audit follow-up).

Before this, nothing at runtime reported the release version: the
FastAPI app said ``0.1.0`` at ``/docs`` and ``/health`` carried no
version field at all. ``pyproject.toml`` stays the source of truth;
these tests pin ``cymatix_context.__version__`` to it so the two can't
drift.
"""
from __future__ import annotations

import pathlib
import tomllib

import cymatix_context
from tests.conftest import make_client

_REPO = pathlib.Path(__file__).resolve().parents[1]


def test_dunder_version_matches_pyproject():
    raw = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert cymatix_context.__version__ == raw["project"]["version"]


def test_fastapi_app_reports_package_version():
    client = make_client()
    assert client.app.version == cymatix_context.__version__


def test_health_reports_package_version():
    client = make_client()
    body = client.get("/health").json()
    assert body["version"] == cymatix_context.__version__

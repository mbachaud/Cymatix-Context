"""Helix Context CLI — cold-start retrieval surface.

Entry point: ``helix`` (configured in pyproject.toml).

See ``docs/superpowers/plans/2026-05-11-helix-cli-v1.md`` for design.
"""
from __future__ import annotations

from .dispatcher import main

__all__ = ["main"]

"""
Cymatix Launcher — supervisor process + control UI.

See ``docs/LAUNCHER.md`` for the full design.

Entry point: ``cymatix-launcher`` (console script) → ``launcher.app:main``.

Dependencies (optional extras):
    pip install cymatix-context[launcher]         # browser mode
    pip install cymatix-context[launcher-native]  # + pywebview native window
"""

from __future__ import annotations

__all__ = ["app", "state", "supervisor"]

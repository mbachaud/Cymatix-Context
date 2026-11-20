"""Plugin API: the contract between cymatix-context and optional add-ons.

A plugin is any object with ``name``, ``version`` and ``requires_core``
attributes. Every hook below is optional; the loader calls a hook only if the
plugin defines it. An add-on package advertises itself through the
``cymatix.plugins`` entry-point group; the entry-point value is a zero-argument
factory that returns a plugin object.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class MenuItem:
    """One entry a plugin contributes to a host menu (e.g. the tray)."""

    id: str
    label: str
    enabled: bool = True


@dataclass(frozen=True)
class Panel:
    """A named block of content; ``render`` returns an HTML fragment."""

    id: str
    title: str
    render: Callable[[], str]


@dataclass(frozen=True)
class ServiceSpec:
    """A long-running helper process a plugin asks the host to supervise."""

    name: str
    argv: List[str]
    env: Optional[Dict[str, str]] = None


@runtime_checkable
class CymatixPlugin(Protocol):
    """Required attributes. Optional hooks (each may be absent):

    * ``cli_commands() -> dict[str, Callable[[list[str]], int]]``
    * ``services() -> list[ServiceSpec]``
    * ``routes() -> fastapi.APIRouter``
    * ``panels() -> list[Panel]``
    * ``menu() -> list[MenuItem]``
    * ``on_action(action_id: str) -> dict``
    * ``status() -> dict`` with ``level`` in ``{"ok", "warn", "error"}`` and
      ``text: str``
    """

    name: str
    version: str
    requires_core: str  # PEP 440 specifier, e.g. ">=0.12,<0.13"


__all__ = ["CymatixPlugin", "MenuItem", "Panel", "ServiceSpec"]

"""Multi-lane serving: resolve, validate, and build the environment for the
set of cymatix instances one launcher supervises.

A *lane* is one server process on its own port against its own knowledge
store. One process serves exactly one store (``/admin/swap-db`` swaps it for
every client), so "multi-DB" means one lane per store:

- ``stable`` — the primary lane, always present, synthesized from
  ``[server] port`` + ``[genome] path`` (and the launcher's
  ``--cymatix-port``). The launcher's genome switcher, collector and
  Headroom routing all act on this lane.
- ``bench`` — synthesized from the legacy ``[server] bench_*`` knobs when
  ``bench_enabled`` (or ``--bench``); a declared ``[[lanes]]`` entry named
  ``bench`` replaces it.
- anything declared in ``[[lanes]]`` — e.g. a ``staging`` lane that serves
  a different engine build from a git worktree for 1:1 comparisons.

Chats pick a lane by pointing their MCP host's ``CYMATIX_MCP_URL`` at the
lane's port.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from .config import CymatixConfig, LaneConfig

PRIMARY_LANE = "stable"
BENCH_LANE = "bench"

# Ports a lane must never take. 11440 is the encoder daemon's default and
# must stay distinct from the bench port (#376).
RESERVED_PORTS: Dict[int, str] = {
    11438: "the launcher UI",
    11440: "the encoder daemon (#376)",
    8787: "the Headroom proxy",
}

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class LaneConfigError(ValueError):
    """The resolved lane set is not safe to launch."""


def resolve_lanes(
    cfg: CymatixConfig,
    *,
    bench: bool = False,
    primary_port: Optional[int] = None,
) -> List[LaneConfig]:
    """Return every lane to supervise: primary first, then bench, then the
    declared ``[[lanes]]`` in file order.

    ``bench`` forces the legacy bench lane on (the launcher's ``--bench``);
    ``primary_port`` is the launcher's ``--cymatix-port``.
    """
    lanes = [LaneConfig(
        name=PRIMARY_LANE,
        port=primary_port if primary_port is not None else cfg.server.port,
        genome_path=cfg.genome.path,
        role=PRIMARY_LANE,
    )]
    declared = list(cfg.lanes)
    if (bench or cfg.server.bench_enabled) and not any(
        lane.name == BENCH_LANE for lane in declared
    ):
        lanes.append(LaneConfig(
            name=BENCH_LANE,
            port=cfg.server.bench_port,
            genome_path=cfg.server.bench_genome_path,
            role=BENCH_LANE,
        ))
    return lanes + declared


def validate_lanes(lanes: List[LaneConfig], base_dir: Optional[Path] = None) -> None:
    """Raise ``LaneConfigError`` listing every problem with *lanes*.

    Rules: well-formed unique names, ``stable`` only as the primary lane,
    unique ports outside ``RESERVED_PORTS``, and one knowledge-store
    directory per lane (``metrics.json`` is written next to the genome).
    """
    problems: List[str] = []
    seen_names: Dict[str, int] = {}
    seen_ports: Dict[int, str] = {}
    seen_dirs: Dict[Path, str] = {}

    for i, lane in enumerate(lanes):
        if not _NAME_RE.match(lane.name):
            problems.append(
                f"lane name {lane.name!r} is invalid (use 1-32 lowercase "
                "letters, digits, '-' or '_')"
            )
        if lane.name == PRIMARY_LANE and i != 0:
            problems.append(
                f"lane name {PRIMARY_LANE!r} is reserved for the primary lane "
                "([server] port + [genome] path); pick another name"
            )
        if lane.name in seen_names:
            problems.append(f"lane name {lane.name!r} is declared more than once")
        seen_names[lane.name] = i

        if lane.port in RESERVED_PORTS:
            problems.append(
                f"lane {lane.name!r}: port {lane.port} is reserved for "
                f"{RESERVED_PORTS[lane.port]}"
            )
        elif lane.port in seen_ports:
            problems.append(
                f"lane {lane.name!r}: port {lane.port} is already used by "
                f"lane {seen_ports[lane.port]!r}"
            )
        else:
            seen_ports[lane.port] = lane.name

        store_dir = _resolve(lane.genome_path, base_dir).parent
        if store_dir in seen_dirs:
            problems.append(
                f"lane {lane.name!r}: knowledge-store directory {store_dir} is "
                f"shared with lane {seen_dirs[store_dir]!r}; give each lane its "
                "own directory"
            )
        else:
            seen_dirs[store_dir] = lane.name

    if problems:
        raise LaneConfigError("; ".join(problems))


def lane_env(lane: LaneConfig, base_dir: Optional[Path] = None) -> Dict[str, str]:
    """Environment overlay for a lane's child process.

    Paths are made absolute against *base_dir* (default: cwd) because a lane
    with an ``engine_path`` runs with that tree as its working directory,
    where a relative path would silently resolve somewhere else.
    """
    env = {
        "CYMATIX_LANE": lane.name,
        "CYMATIX_SERVER_PORT": str(lane.port),
        "CYMATIX_GENOME_PATH": str(_resolve(lane.genome_path, base_dir)),
    }
    if lane.config_path:
        env["CYMATIX_CONFIG"] = str(_resolve(lane.config_path, base_dir))
    if lane.engine_path:
        engine = str(_resolve(lane.engine_path, base_dir))
        existing = os.environ.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(p for p in (engine, existing) if p)
    return env


def lane_cwd(lane: LaneConfig, base_dir: Optional[Path] = None) -> Optional[str]:
    """Working directory for a lane's child: its engine tree, if any."""
    if lane.engine_path:
        return str(_resolve(lane.engine_path, base_dir))
    return None


def current_lane() -> str:
    """The lane this server process serves (``CYMATIX_LANE``, set by the
    launcher); ``stable`` when run standalone."""
    return os.environ.get("CYMATIX_LANE", "").strip() or PRIMARY_LANE


def scope_default_path(configured: str, default: str, leaf: str) -> str:
    """Give a non-primary lane its own copy of a per-user directory.

    Returns *configured* unchanged for the primary lane, or when the operator
    configured a path explicitly. Otherwise (a non-primary lane still on the
    shipped *default*) returns ``~/.cymatix/lanes/<lane>/<leaf>`` so two
    lanes never share it.
    """
    lane = current_lane()
    if lane == PRIMARY_LANE or configured != default:
        return configured
    return f"~/.cymatix/lanes/{lane}/{leaf}"


def _resolve(path: str, base_dir: Optional[Path]) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = (base_dir or Path.cwd()) / p
    return p.resolve()

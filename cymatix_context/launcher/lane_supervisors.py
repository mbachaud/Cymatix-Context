"""Build one ``CymatixSupervisor`` per non-primary lane.

The primary ``stable`` lane keeps its own supervisor, built in ``main()``,
because genome switching, the state collector and Headroom routing are
wired to it. Every other lane (bench, staging, ...) gets a supervisor with
its own state file, log file and environment overlay from
``cymatix_context.lanes.lane_env``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..config import CymatixConfig, LaneConfig
from ..lanes import BENCH_LANE, PRIMARY_LANE, lane_cwd, lane_env, resolve_lanes, validate_lanes
from .state import DEFAULT_STATE_PATH, StateStore
from .supervisor import CymatixSupervisor


@dataclass
class LaneRuntime:
    config: LaneConfig
    supervisor: CymatixSupervisor


def lane_state_paths(name: str, state_dir: Path) -> "tuple[Path, Path]":
    """(state file, log file) for a lane. The bench lane keeps its pre-lanes
    file names so a bench child started by an older launcher is re-adopted."""
    if name == BENCH_LANE:
        return state_dir / "bench-state.json", state_dir / "cymatix-bench.log"
    return state_dir / f"{name}-state.json", state_dir / f"cymatix-{name}.log"


def build_lane_runtimes(
    cfg: CymatixConfig,
    *,
    host: str = "127.0.0.1",
    bench: bool = False,
    primary_port: Optional[int] = None,
    base_dir: Optional[Path] = None,
    state_dir: Optional[Path] = None,
) -> List[LaneRuntime]:
    """Resolve and validate every lane, then build supervisors for all but
    the primary. Raises ``lanes.LaneConfigError`` if the set is unsafe."""
    lanes = resolve_lanes(cfg, bench=bench, primary_port=primary_port)
    validate_lanes(lanes, base_dir=base_dir)
    state_dir = state_dir or DEFAULT_STATE_PATH.parent

    runtimes: List[LaneRuntime] = []
    for lane in lanes:
        if lane.name == PRIMARY_LANE:
            continue
        state_path, log_path = lane_state_paths(lane.name, state_dir)
        runtimes.append(LaneRuntime(
            config=lane,
            supervisor=CymatixSupervisor(
                store=StateStore(path=state_path),
                cymatix_host=host,
                cymatix_port=lane.port,
                python_executable=lane.python_executable or None,
                cymatix_log_path=log_path,
                extra_env=lane_env(lane, base_dir=base_dir),
                cwd=lane_cwd(lane, base_dir=base_dir),
            ),
        ))
    return runtimes

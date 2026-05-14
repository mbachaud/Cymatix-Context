"""Auto-sizers for parallel ingest pools (issue #92).

Two helpers:

- :func:`auto_workers` picks a count for the file-level worker pool
  used by ``build_fixture_matrix.py --parallel``. CPU-bound; leaves
  ~12.5% headroom and reserves a core for the writer process.
- :func:`auto_shard_workers` picks a count for the shard-level worker
  pool used by ``build_fixture_matrix.py --mode sharded --shard-workers``.
  VRAM-bound because each shard-worker holds its own SPLADE model on
  the GPU (~4 GB per worker).
- :func:`auto_shard_file_workers` picks the CPU-only file worker count
  inside each shard worker.

These helpers are only consulted when the user does not pass the matching
``--workers`` / ``--shard-workers`` / ``--shard-file-workers`` override.
"""

from __future__ import annotations

import math
import os

_SPLADE_VRAM_GB = 4.0
_VRAM_ROUNDING_SLACK = 0.05


def auto_workers(buffer_pct: float = 0.125) -> int:
    """Worker count for the monolithic ``--parallel`` ingest pool.

    Leaves ``buffer_pct`` CPU headroom and reserves one extra core for
    the writer process. Always returns >= 1.

    On an 8-core 5800X (the reference dev box) the default returns 6.
    """
    physical = max(1, os.cpu_count() or 4)
    reserved = max(2, math.ceil(physical * buffer_pct) + 1)
    return max(1, physical - reserved)


def _vram_worker_cap(vram_gb: float | None) -> int:
    """Return SPLADE worker cap for a VRAM amount in GB."""
    if vram_gb is None:
        return 1
    return max(
        1,
        int((max(0.0, vram_gb) / _SPLADE_VRAM_GB) + _VRAM_ROUNDING_SLACK),
    )


def auto_shard_workers(buffer_pct: float = 0.125) -> int:
    """Shard-worker count for ``--mode sharded --shard-workers``.

    Each shard-worker holds an independent SPLADE model on the GPU
    (~4 GB). The cap is the lower of total-VRAM capacity, live free-VRAM
    capacity when reported, and CPU headroom. Falls back to 1 when no GPU
    is reported.

    On a 3080 Ti (12 GB) + 5800X this returns 3.
    """
    try:
        from helix_context.hardware import get_hardware
        hw = get_hardware()
        vram_total = hw.vram_total_gb
        vram_free = getattr(hw, "vram_free_gb", None)
    except Exception:
        vram_total = None
        vram_free = None

    if vram_total is None:
        vram_cap = 1
    else:
        total_cap = _vram_worker_cap(vram_total)
        free_cap = _vram_worker_cap(vram_free) if vram_free is not None else total_cap
        vram_cap = max(1, min(total_cap, free_cap))
    cpu_cap = max(1, auto_workers(buffer_pct))
    return max(1, min(vram_cap, cpu_cap))


def auto_shard_file_workers(
    shard_workers: int,
    buffer_pct: float = 0.125,
) -> int:
    """CPU-only file workers to run inside each shard worker.

    The total file-worker budget follows :func:`auto_workers`; this helper
    splits that CPU budget across the SPLADE-owning shard workers. On the
    reference 8-core box that means 2 shard workers get 3 file workers each,
    while 3 shard workers get 2 each.
    """
    shards = max(1, int(shard_workers or 1))
    cpu_budget = max(1, auto_workers(buffer_pct))
    return max(1, cpu_budget // shards)

"""Auto-sizers for parallel ingest pools (issue #92).

Two helpers:

- :func:`auto_workers` picks a count for the file-level worker pool
  used by ``build_fixture_matrix.py --parallel``. CPU-bound; leaves
  ~12.5% headroom and reserves a core for the writer process.
- :func:`auto_shard_workers` picks a count for the shard-level worker
  pool used by ``build_fixture_matrix.py --mode sharded --shard-workers``.
  VRAM-bound because each shard-worker holds its own SPLADE model on
  the GPU (~4 GB per worker).

Both honour an explicit override at call time; these helpers are only
consulted when the user does not pass ``--workers`` / ``--shard-workers``.
"""

from __future__ import annotations

import math
import os


def auto_workers(buffer_pct: float = 0.125) -> int:
    """Worker count for the monolithic ``--parallel`` ingest pool.

    Leaves ``buffer_pct`` CPU headroom and reserves one extra core for
    the writer process. Always returns >= 1.

    On an 8-core 5800X (the reference dev box) the default returns 6.
    """
    physical = max(1, os.cpu_count() or 4)
    reserved = max(2, math.ceil(physical * buffer_pct) + 1)
    return max(1, physical - reserved)


def auto_shard_workers(buffer_pct: float = 0.125) -> int:
    """Shard-worker count for ``--mode sharded --shard-workers``.

    Each shard-worker holds an independent SPLADE model on the GPU
    (~4 GB). The cap is ``min(vram_gb // 4, auto_workers())`` so we never
    exceed CPU headroom or VRAM. Falls back to 1 when no GPU is reported.

    On a 3080 Ti (12 GB) + 5800X this returns 3.
    """
    try:
        from helix_context.hardware import get_hardware
        vram = get_hardware().vram_total_gb
    except Exception:
        vram = None

    vram_cap = max(1, int((vram or 4) // 4))  # 4 GB per SPLADE worker
    cpu_cap = max(1, auto_workers(buffer_pct))
    return max(1, min(vram_cap, cpu_cap))

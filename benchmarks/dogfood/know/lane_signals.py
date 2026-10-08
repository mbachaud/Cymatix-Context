"""Dense-free agreement signals for know/miss (issue #482).

Moved into the package as ``cymatix_context.scoring.know_lanes`` so the
served ``[know] model = "lanes"`` confidence and the bench replay/probe share
one implementation (no train/serve skew). Re-exported here for the bench
scripts that import it by this path.
"""
from __future__ import annotations

from cymatix_context.scoring.know_lanes import EVIDENCE_LANES, lane_signals

__all__ = ["EVIDENCE_LANES", "lane_signals"]

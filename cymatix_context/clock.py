"""Wall clock for scoring, pinnable for reproducible bench runs.

``CYMATIX_SCORING_EPOCH`` (seconds since epoch) fixes the "now" that
time-dependent scoring reads: the authority tier's 48 h creation-recency
boost and ``DocumentSignals.access_rate``. Unset (or unparseable) =
``time.time()``, so shipped behaviour is unchanged. Bench runners pin it to
the bed's build time so a multi-hour ladder cannot drift across a window
boundary mid-run.
"""

from __future__ import annotations

import os
import time

SCORING_EPOCH_ENV = "CYMATIX_SCORING_EPOCH"


def scoring_now() -> float:
    raw = os.environ.get(SCORING_EPOCH_ENV, "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return time.time()

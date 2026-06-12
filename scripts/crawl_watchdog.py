"""Crawl watchdog: throughput-triggered escalation ladder for dense ingest/backfill.

Issue #212. Detects the #176 WDDM-spill crawl -- a CUDA context that has
silently spilled into shared system memory and degraded to a fraction of
its early-shard throughput -- and escalates through cheap-to-terminal
recovery actions instead of letting a shard run at ~0.02 genes/s for a
projected ~66 hours (slack__eng-oncall, 2026-06-11).

Design, from two live incidents during the 2026-06-10/11 ERB 500K rebuild
on a 12 GB rig:

* NOT a wall-clock per-shard timer -- that false-positives on legitimately
  large shards and misses crawls on small ones. The trigger is the
  unambiguous signature: the per-batch genes/s EMA drops below the shard's
  OWN early-batch baseline divided by ``HELIX_BFM_CRAWL_FACTOR`` for
  ``HELIX_BFM_CRAWL_WINDOW`` consecutive batches, AND dedicated VRAM sits
  near device capacity (fraction > 0.92). A slow disk alone is not a
  crawl; a CPU-only box (no probe-able CUDA device) structurally cannot
  trip.

* Escalation ladder (``HELIX_BFM_CRAWL_ACTION=ladder``, the default):
  rung 1 -- ``gc.collect()`` + ``torch.cuda.empty_cache()`` (cheap,
  occasionally sufficient early). Rung 2, if the crawl persists for
  another full window -- terminal action: the dense BACKFILL path tears
  down the codec and reloads it on CPU for the remainder of the shard
  (``BGEM3_DEVICE=cpu`` semantics: byte-identical vectors, no VRAM
  ceiling), while the INGEST path raises the existing ``_PauseRequested``
  so the shard pauses cleanly at a batch boundary and the #183 salvage +
  file-level-resume machinery restarts it with a fresh CUDA context.
  ``HELIX_BFM_CRAWL_ACTION=cpu`` jumps straight to the terminal rung;
  ``off`` detects and logs only. The 2026-06-11 slack__eng-oncall
  incident proved ``empty_cache`` alone does not un-crawl an
  already-spilled context -- context recycle / CPU demotion is the fix.

Every watchdog log line carries the stable grep-able prefix
``[crawl-watchdog]`` and includes rate, baseline, VRAM fraction and the
action taken.

The detector is a pure class (no torch import, no clock reads) so tests
drive it with a fake feed; CUDA probing lives in the two small helpers
:func:`cuda_vram_fraction` and :func:`release_cuda_cache`, both guarded
with try/except so CPU-only boxes degrade to harmless no-ops.
"""
from __future__ import annotations

import gc
import logging
import os
import statistics

log = logging.getLogger("helix.crawl_watchdog")

#: Stable grep-able prefix for every watchdog log line.
LOG_PREFIX = "[crawl-watchdog]"

#: Rung 1: caller should run :func:`release_cuda_cache` and continue.
ACTION_EMPTY_CACHE = "empty_cache"
#: Terminal rung: backfill path reloads the codec on CPU for the rest of
#: the shard; ingest path raises ``_PauseRequested`` (process recycle).
ACTION_DEMOTE = "demote"

DEFAULT_CRAWL_WINDOW = 8
DEFAULT_CRAWL_FACTOR = 5.0
DEFAULT_VRAM_THRESHOLD = 0.92
DEFAULT_EMA_ALPHA = 0.3

_VALID_ACTIONS = ("ladder", "cpu", "off")


# -- Env knobs (read at call time so tests can monkeypatch.setenv) --------


def _env_parse(name: str, default, cast):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        log.warning(
            "%s ignoring unparseable %s=%r (using default %r)",
            LOG_PREFIX, name, raw, default,
        )
        return default


def env_crawl_window() -> int:
    """``HELIX_BFM_CRAWL_WINDOW`` -- baseline length AND consecutive-slow
    streak length, in batches. Default 8; clamped to >= 1."""
    return max(1, _env_parse("HELIX_BFM_CRAWL_WINDOW", DEFAULT_CRAWL_WINDOW, int))


def env_crawl_factor() -> float:
    """``HELIX_BFM_CRAWL_FACTOR`` -- crawl threshold = baseline / factor.
    Default 5.0; non-positive values fall back to the default."""
    val = _env_parse("HELIX_BFM_CRAWL_FACTOR", DEFAULT_CRAWL_FACTOR, float)
    return val if val > 0 else DEFAULT_CRAWL_FACTOR


def env_crawl_action() -> str:
    """``HELIX_BFM_CRAWL_ACTION`` -- ``ladder`` (default) | ``cpu`` | ``off``."""
    raw = os.environ.get("HELIX_BFM_CRAWL_ACTION", "").strip().lower()
    if not raw:
        return "ladder"
    if raw not in _VALID_ACTIONS:
        log.warning(
            "%s ignoring unknown HELIX_BFM_CRAWL_ACTION=%r (using 'ladder')",
            LOG_PREFIX, raw,
        )
        return "ladder"
    return raw


# -- Guarded CUDA helpers (no-ops on CPU-only boxes) ----------------------


def cuda_vram_fraction() -> "float | None":
    """Fraction of the current CUDA device's dedicated memory in use.

    ``max(memory_allocated, memory_reserved) / total_memory`` on the
    current device. Returns ``None`` when torch is missing, no CUDA
    device is visible, or any probe call fails -- and the detector treats
    ``None`` as "cannot trip", so CPU-only boxes never escalate.
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        dev = torch.cuda.current_device()
        total = float(torch.cuda.get_device_properties(dev).total_memory)
        if total <= 0:
            return None
        used = float(max(
            torch.cuda.memory_allocated(dev),
            torch.cuda.memory_reserved(dev),
        ))
        return used / total
    except Exception:  # noqa: BLE001 -- any probe failure means "don't trip"
        return None


def release_cuda_cache() -> bool:
    """Rung 1: ``gc.collect()`` then ``torch.cuda.empty_cache()``.

    gc first so collected tensors return their blocks to the caching
    allocator before the cache is released. Returns True iff the CUDA
    cache release actually ran (False on CPU-only / torch-less boxes,
    where only the gc pass happens).
    """
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            return True
    except Exception:  # noqa: BLE001 -- best effort; never break the build
        pass
    return False


# -- The pure detector -----------------------------------------------------


class CrawlDetector:
    """Pure throughput-crawl detector with an escalation ladder.

    Feed one observation per completed batch::

        action = detector.feed(genes, dt_seconds, vram_frac)

    Returns ``None`` (healthy / warming up / disarmed),
    :data:`ACTION_EMPTY_CACHE` (ladder rung 1) or :data:`ACTION_DEMOTE`
    (terminal rung). Pure: no clock reads, no torch imports -- callers
    supply the elapsed time and the VRAM fraction (production call sites
    use :func:`cuda_vram_fraction`, which yields ``None`` on CPU-only
    boxes, making the detector structurally unable to trip there).

    Mechanics:

    * rate tracker: EMA (``ema_alpha``, default 0.3) of per-batch genes/s;
    * baseline: median of the first ``baseline_window`` per-batch rates --
      the shard's own healthy speed, so big-slow shards don't false-trip;
    * crawl: EMA < baseline / ``factor`` AND ``vram_frac`` >
      ``vram_threshold`` for ``baseline_window`` CONSECUTIVE batches.
      Any healthy batch resets the streak (hysteresis);
    * on trip the streak re-arms, so the next rung requires another full
      window of sustained crawling;
    * ``action="off"`` logs every trip but never returns an action;
      ``action="cpu"`` returns :data:`ACTION_DEMOTE` on the first trip;
      ``action="ladder"`` returns rung 1 then rung 2. After the terminal
      rung the detector disarms (all further feeds return ``None``).
    """

    def __init__(
        self,
        baseline_window: int = DEFAULT_CRAWL_WINDOW,
        factor: float = DEFAULT_CRAWL_FACTOR,
        *,
        action: str = "ladder",
        vram_threshold: float = DEFAULT_VRAM_THRESHOLD,
        ema_alpha: float = DEFAULT_EMA_ALPHA,
        log_fn=None,
        name: str = "",
    ):
        self.baseline_window = max(1, int(baseline_window))
        self.factor = float(factor) if float(factor) > 0 else DEFAULT_CRAWL_FACTOR
        self.action = action if action in _VALID_ACTIONS else "ladder"
        self.vram_threshold = float(vram_threshold)
        self.ema_alpha = min(1.0, max(0.01, float(ema_alpha)))
        self.log_fn = log_fn if log_fn is not None else log.warning
        self.name = name

        self.baseline: "float | None" = None
        self.ema: "float | None" = None
        self.streak = 0
        self.rung = 0
        self.trips = 0
        self.batches = 0
        self.disarmed = False
        self._warmup: list[float] = []

    @classmethod
    def from_env(cls, *, log_fn=None, name: str = "") -> "CrawlDetector":
        """Build a detector from the ``HELIX_BFM_CRAWL_*`` env knobs
        (read now, not at import time, so tests can monkeypatch)."""
        return cls(
            baseline_window=env_crawl_window(),
            factor=env_crawl_factor(),
            action=env_crawl_action(),
            log_fn=log_fn,
            name=name,
        )

    # -- internals --

    def _tag(self) -> str:
        return f" name={self.name}" if self.name else ""

    def feed(self, genes: float, dt: float, vram_frac: "float | None" = None):
        """Record one batch: ``genes`` processed in ``dt`` seconds with the
        device at ``vram_frac`` (``None`` = unknown / no CUDA). Returns the
        escalation action for the caller to apply, or ``None``."""
        if self.disarmed:
            return None
        if genes <= 0 or dt <= 0:
            return None  # nothing measurable; neither counts nor resets
        rate = genes / dt
        self.batches += 1
        if self.ema is None:
            self.ema = rate
        else:
            self.ema = self.ema_alpha * rate + (1.0 - self.ema_alpha) * self.ema

        if self.baseline is None:
            self._warmup.append(rate)
            if len(self._warmup) >= self.baseline_window:
                self.baseline = statistics.median(self._warmup)
                self.log_fn(
                    f"{LOG_PREFIX} baseline established{self._tag()}: "
                    f"{self.baseline:.2f} genes/s (median of first "
                    f"{self.baseline_window} batches) "
                    f"crawl_threshold={self.baseline / self.factor:.2f} genes/s "
                    f"factor={self.factor:g} window={self.baseline_window} "
                    f"action={self.action}"
                )
            return None

        threshold = self.baseline / self.factor
        slow = self.ema < threshold
        vram_high = vram_frac is not None and vram_frac > self.vram_threshold
        if slow and vram_high:
            self.streak += 1
        else:
            self.streak = 0  # hysteresis: recovery re-arms from zero
        if self.streak < self.baseline_window:
            return None

        # Tripped: a full window of consecutive slow-and-VRAM-pinned batches.
        self.streak = 0
        self.trips += 1
        return self._escalate(rate, vram_frac)

    def _escalate(self, rate: float, vram_frac: float):
        ctx = (
            f"{LOG_PREFIX} CRAWL detected{self._tag()}: "
            f"rate={rate:.2f} genes/s ema={self.ema:.2f} genes/s "
            f"baseline={self.baseline:.2f} genes/s "
            f"threshold={self.baseline / self.factor:.2f} genes/s "
            f"vram_frac={vram_frac:.3f} window={self.baseline_window}"
        )
        if self.action == "off":
            self.log_fn(
                ctx + " action=none (HELIX_BFM_CRAWL_ACTION=off: log-only)"
            )
            return None
        if self.action == "cpu":
            self.disarmed = True
            self.log_fn(
                ctx + " action=demote (HELIX_BFM_CRAWL_ACTION=cpu: straight "
                "to terminal rung; watchdog disarmed for this shard)"
            )
            return ACTION_DEMOTE
        # ladder
        self.rung += 1
        if self.rung == 1:
            self.log_fn(
                ctx + " action=empty_cache (ladder rung 1/2: gc.collect + "
                "torch.cuda.empty_cache)"
            )
            return ACTION_EMPTY_CACHE
        self.disarmed = True
        self.log_fn(
            ctx + " action=demote (ladder rung 2/2: empty_cache was not "
            "enough -- an already-spilled context needs recycle/CPU-demote; "
            "watchdog disarmed for this shard)"
        )
        return ACTION_DEMOTE

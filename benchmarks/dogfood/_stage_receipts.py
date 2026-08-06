"""Ring-reader helper for perf receipts (2026-08 campaign, A.1 item 2).

Fetches ``GET /debug/pipeline/recent`` from a running cymatix server and
aggregates the pipeline ring into per-stage / per-signal p50/p95 blocks
that harnesses embed in their JSON receipts. This is the repo-convention
answer to "read the latency histograms" — the ring rides plain HTTP, so
no OTel collector is ever needed on a bench box.

Ring entry shape (perf slice 1):
    { request_id, stage, ms, ts }                        — every stage
    + { signals: {name: ms}, model_load_ms: {name: ms} } — express only
    + { commits: int }                                   — tail_writes only

Stdlib only, by bench convention.
"""
from __future__ import annotations

import json
import math
import urllib.request


def _percentile(vals: list[float], p: float) -> float:
    # Ceiling index, mirrored from run_latency_scaling._percentile: at
    # small n a floor index can land *below* the median (p95 of 2 samples
    # must be the max, not the min).
    s = sorted(vals)
    return s[min(len(s) - 1, math.ceil(p * (len(s) - 1)))]


def fetch_recent(base_url: str, limit: int = 128, timeout: float = 10.0) -> list[dict]:
    """Return the ring's event list from ``{base_url}/debug/pipeline/recent``."""
    url = f"{base_url.rstrip('/')}/debug/pipeline/recent?limit={int(limit)}"
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return list(payload.get("events", []))


def aggregate(entries: list[dict]) -> dict:
    """Roll ring entries up into a receipt-embeddable summary.

    Returns::

        {
          "stages":  {stage:  {n, p50_ms, p95_ms, max_ms}},
          "signals": {signal: {n, p50_ms, p95_ms, max_ms}},   # express sub-maps
          "model_load_ms": {name: ms},                        # last-seen snapshot
          "commits": {n, p50, p95, max},                      # tail_writes deltas
        }
    """
    stage_ms: dict[str, list[float]] = {}
    signal_ms: dict[str, list[float]] = {}
    model_load: dict = {}
    commit_counts: list[float] = []

    for e in entries:
        stage = e.get("stage")
        ms = e.get("ms")
        if stage is None or ms is None:
            continue
        stage_ms.setdefault(stage, []).append(float(ms))
        for name, sig_ms in (e.get("signals") or {}).items():
            signal_ms.setdefault(name, []).append(float(sig_ms))
        if e.get("model_load_ms"):
            model_load = dict(e["model_load_ms"])
        if "commits" in e:
            commit_counts.append(float(e["commits"]))

    def _block(vals: list[float]) -> dict:
        return {
            "n": len(vals),
            "p50_ms": _percentile(vals, 0.5),
            "p95_ms": _percentile(vals, 0.95),
            "max_ms": max(vals),
        }

    return {
        "stages": {k: _block(v) for k, v in stage_ms.items()},
        "signals": {k: _block(v) for k, v in signal_ms.items()},
        "model_load_ms": model_load,
        "commits": {
            "n": len(commit_counts),
            "p50": _percentile(commit_counts, 0.5) if commit_counts else 0,
            "p95": _percentile(commit_counts, 0.95) if commit_counts else 0,
            "max": max(commit_counts) if commit_counts else 0,
        },
    }

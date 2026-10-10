"""Microbench for the packet-trace hot-path cost (issue #493, Phase 1).

Measures the time the request path spends on a metadata-level trace:
``build_packet_event`` (keyed hashing of the query and every chunk) plus
``TraceWriter.submit`` (queue put). File I/O happens on the writer thread
and is not charged to the request. Prints a JSON receipt.

    python scripts/bench_trace_overhead.py [--n 2000] [--chunks 12]
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cymatix_context.telemetry import trace as tr  # noqa: E402


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
            text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _pct(sorted_us: list[float], p: float) -> float:
    return sorted_us[min(len(sorted_us) - 1, int(p * len(sorted_us)))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--chunks", type=int, default=12)
    args = ap.parse_args()

    text = "lorem ipsum dolor sit amet " * 80          # ~2 KB per chunk
    chunks = [
        {"gene_id": f"gene{i:04d}", "text": text, "rank": i + 1, "score": 1.0 / (i + 1),
         "source_kind": "code", "lane_contribs": {"fts5": 1.0, "tag": 0.5}}
        for i in range(args.chunks)
    ]
    replay = {"index_fingerprint": "x" * 32, "doc_count": 1, "config_hash": "y" * 64,
              "cymatix_version": "bench", "git_sha": None}

    with tempfile.TemporaryDirectory() as td:
        hasher = tr.KeyedHasher(Path(td))
        writer = tr.TraceWriter(Path(td), hash_chain=False, rotate_bytes=1 << 30)
        hasher.hash("warm")                                  # key load is one-time
        samples: list[float] = []
        for i in range(args.n):
            t = time.perf_counter()
            ev = tr.build_packet_event(
                packet_id=tr.new_packet_id(), pipeline_request_id="r", session_id="s",
                query=f"how does the splice step number {i} work", chunks=chunks,
                verdict={"kind": "know", "confidence": 0.9}, timing_ms={"total": 10.0},
                replay=replay, level="metadata", hasher=hasher,
            )
            writer.submit(ev)
            samples.append((time.perf_counter() - t) * 1e6)
        writer.close()

    samples.sort()
    receipt = {
        "bench": "trace_overhead_metadata",
        "git_sha": _git_sha(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "n": args.n,
        "chunks_per_event": args.chunks,
        "chunk_text_bytes": len(text),
        "level": "metadata",
        "added_latency_us": {
            "p50": round(_pct(samples, 0.50), 1),
            "p95": round(_pct(samples, 0.95), 1),
            "p99": round(_pct(samples, 0.99), 1),
            "max": round(samples[-1], 1),
        },
        "p95_under_1ms": _pct(samples, 0.95) < 1000.0,
        "not_measured": "chunk get_doc reads and index fingerprint (cached 60s) on the request path",
    }
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

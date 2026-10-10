"""End-to-end packet-trace overhead on POST /context (issue #493 acceptance).

Builds three in-process apps over identical synthetic stores (one per arm:
trace OFF, ON level=metadata, ON level=full; hash_chain on for both ON arms
so the writer-thread disk cost is included), then drives them through
FastAPI TestClient with the SAME queries, round-robin interleaved per query
(off, metadata, full, rotating start arm) after warmup. Reports per-arm
p50/p95/p99 and the paired delta (ON - OFF) per query.

Knob proof: per arm it prints the observed ``app.state.packet_tracer``
enabled/level/hash_chain/writer-alive values, and at the end the number of
lines each arm's trace file actually received.

    python scripts/bench_trace_e2e.py [--n 600] [--docs 200] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Env must not override the per-arm TraceConfig (env wins over toml).
for _k in ("CYMATIX_TRACE_ENABLED", "CYMATIX_TRACE_LEVEL", "CYMATIX_TRACE_PATH"):
    os.environ.pop(_k, None)

from cymatix_context.config import TraceConfig  # noqa: E402
from tests.conftest import make_client, make_cymatix_config, make_gene  # noqa: E402

TOPICS = ["splice", "cache", "tokenizer", "retrieval", "freshness", "session",
          "ingest", "reranker", "budget", "compaction", "telemetry", "schema"]
VERBS = ["compresses", "invalidates", "ranks", "persists", "elides", "assembles"]


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True, text=True,
            timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _pct(s: list[float], p: float) -> float:
    return s[min(len(s) - 1, int(p * len(s)))]


def _stats(xs: list[float]) -> dict:
    s = sorted(xs)
    return {"p50": round(_pct(s, .5), 1), "p95": round(_pct(s, .95), 1),
            "p99": round(_pct(s, .99), 1), "mean": round(statistics.fmean(s), 1)}


def _docs(n: int) -> list[str]:
    rng = random.Random(1234)
    out = []
    for i in range(n):
        t1, t2 = rng.sample(TOPICS, 2)
        v = rng.choice(VERBS)
        out.append(
            f"Doc {i}: the {t1} stage {v} the {t2} payload for case number {i}. "
            + (f"The {t1} module and the {t2} module interact when {v}. " * 6)
        )
    return out


def _queries(n: int) -> list[str]:
    rng = random.Random(99)
    return [f"how does the {rng.choice(TOPICS)} stage {rng.choice(VERBS)} the "
            f"{rng.choice(TOPICS)} payload" for _ in range(n)]


def build_arm(name: str, base: Path, docs: list[str], trace: TraceConfig):
    tdir = base / name / "traces"
    trace.path = str(tdir)
    c = make_client(make_cymatix_config(trace=trace))
    g = c.app.state.cymatix.genome
    for i, d in enumerate(docs):
        g.upsert_gene(make_gene(d, gene_id=f"benchgene{i:08d}"))
    return c, tdir


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--docs", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=60)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    docs = _docs(args.docs)
    queries = _queries(args.n + args.warmup)
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        specs = {
            "off": TraceConfig(enabled=False),
            "off2": TraceConfig(enabled=False),   # control: off-vs-off noise floor
            "metadata": TraceConfig(enabled=True, level="metadata", hash_chain=True),
            "full": TraceConfig(enabled=True, level="full", hash_chain=True),
        }
        arms, dirs, knob = {}, {}, {}
        for name, tc in specs.items():
            arms[name], dirs[name] = build_arm(name, base, docs, tc)
            t = arms[name].app.state.packet_tracer
            knob[name] = {"enabled": t.enabled, "level": t.level,
                          "hash_chain": t.settings["hash_chain"],
                          "writer_present": t.writer is not None}
            print(f"[knob] arm={name} observed={knob[name]}", flush=True)
        assert knob["off"]["enabled"] is False and knob["off2"]["enabled"] is False and knob["metadata"]["enabled"] and knob["full"]["enabled"]

        names = list(arms)
        body = lambda q: {"query": q, "ignore_delivered": True, "session_id": "bench"}  # noqa: E731
        for q in queries[: args.warmup]:
            for nm in names:
                r = arms[nm].post("/context", json=body(q))
                assert r.status_code == 200, r.text

        lat = {nm: [] for nm in names}
        for i, q in enumerate(queries[args.warmup:]):
            k = i % len(names)
            order = names[k:] + names[:k]              # rotate start arm
            for nm in order:
                t = time.perf_counter()
                r = arms[nm].post("/context", json=body(q))
                dt = (time.perf_counter() - t) * 1e6
                assert r.status_code == 200
                lat[nm].append(dt)

        time.sleep(1.0)  # let writer threads drain
        written = {}
        for nm in names:
            f = dirs[nm] / "packets.jsonl"
            written[nm] = sum(1 for _ in f.open(encoding="utf-8")) if f.exists() else 0
        for c in arms.values():
            c.app.state.packet_tracer.close()

    deltas = {lv: [lat[lv][i] - lat["off"][i] for i in range(args.n)]
              for lv in ("off2", "metadata", "full")}
    cfg = make_cymatix_config()
    receipt = {
        "bench": "trace_overhead_e2e_context",
        "git_sha": _git("rev-parse", "HEAD"),
        "commits_behind_master": _git("rev-list", "--count", "HEAD..master"),
        "python": platform.python_version(),
        "machine": f"{platform.platform()} / {platform.processor()}",
        "n": args.n, "warmup": args.warmup, "docs": args.docs,
        "method": "FastAPI TestClient, 3 apps (off/metadata/full), identical "
                  "seeded stores, same queries, rotating-order interleave, "
                  "hash_chain=true on ON arms, ignore_delivered=true",
        "config_dump": {
            "budget.max_genes_per_turn": cfg.budget.max_genes_per_turn,
            "genome.path": cfg.genome.path,
            "trace_arms": {k: {"enabled": v.enabled, "level": v.level,
                               "hash_chain": v.hash_chain, "sampler_ratio": v.sampler_ratio}
                           for k, v in specs.items()},
        },
        "observed_runtime_knob": knob,
        "trace_lines_written": written,
        "latency_us": {nm: _stats(lat[nm]) for nm in names},
        "paired_delta_us": {lv: _stats(deltas[lv]) for lv in deltas},
        "under_1ms": {lv: {"p50": _pct(sorted(deltas[lv]), .5) < 1000,
                           "p95": _pct(sorted(deltas[lv]), .95) < 1000}
                      for lv in deltas},
    }
    text = json.dumps(receipt, indent=2)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

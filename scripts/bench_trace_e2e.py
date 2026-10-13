"""End-to-end packet-trace overhead on POST /context (issue #493 acceptance).

Builds four in-process apps (arms: off, off2 = off-vs-off control, ON
level=metadata, ON level=full; hash_chain on for both ON arms so the
writer-thread disk cost is included), then drives them through FastAPI
TestClient with the SAME queries, round-robin interleaved per query (rotating
start arm) after warmup.

Two kinds of evidence:

1. Whole-request latency, paired per query (ON - off), with seeded bootstrap
   95% CIs on p50/p95 differences. Off2-vs-off is the noise-floor control.
2. DIRECT trace cost: the tracer's request-thread entry points
   (``sampled`` + ``emit``, which together cover chunk building, record
   build and enqueue) are wrapped with perf_counter inside this script only
   (instance-level wrappers, product code untouched). Per-request total trace
   time is reported as p50/p95/p99/max. This is the primary <1 ms evidence.

Modes:
  default      synthetic stores built per arm in a temp dir
  --genome P   all four arms open the existing store at P (read-only
               requests, CYMATIX_DISABLE_LEARN=1, session delivery off);
               requires --trace-dir. Use --queries for real queries.

    python scripts/bench_trace_e2e.py [--n 600] [--docs 200] [--out PATH]
    python scripts/bench_trace_e2e.py --genome G.db --trace-dir D \\
        --queries benchmarks/dogfood/erb/needles_resolved_v09x_full.json --out R.json
"""

from __future__ import annotations
# bench-pin: cover-index autobuild
# Bench drivers never let a background covering-index build start during a
# measured run (it perturbs latency; results are unaffected). Set before any
# store opens. Drivers with an explicit prebuild path set their own value.
import os as _bench_os
import sys as _bench_sys
_bench_os.environ["CYMATIX_COVER_INDEX_AUTOBUILD"] = "0"
print('cover-index autobuild pinned off (bench driver)', file=_bench_sys.stderr)

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
BOOT_RESAMPLES = 2000
BOOT_SEED = 493


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
            "p99": round(_pct(s, .99), 1), "max": round(s[-1], 1),
            "mean": round(statistics.fmean(s), 1)}


def _bootstrap_ci(on: list[float], off: list[float], p: float) -> dict:
    """Paired bootstrap (resample query indices) 95% CI of pXX(on)-pXX(off)."""
    n = len(on)
    rng = random.Random(BOOT_SEED)
    diffs = []
    for _ in range(BOOT_RESAMPLES):
        idx = [rng.randrange(n) for _ in range(n)]
        a = sorted(on[i] for i in idx)
        b = sorted(off[i] for i in idx)
        diffs.append(_pct(a, p) - _pct(b, p))
    diffs.sort()
    point = _pct(sorted(on), p) - _pct(sorted(off), p)
    return {"point_us": round(point, 1),
            "ci95_lo_us": round(diffs[int(0.025 * len(diffs))], 1),
            "ci95_hi_us": round(diffs[int(0.975 * len(diffs)) - 1], 1)}


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


def _synthetic_queries(n: int) -> list[str]:
    rng = random.Random(99)
    return [f"how does the {rng.choice(TOPICS)} stage {rng.choice(VERBS)} the "
            f"{rng.choice(TOPICS)} payload" for _ in range(n)]


def _load_queries(path: Path, n: int) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw["needles"] if isinstance(raw, dict) else raw
    texts = [x["query"] if isinstance(x, dict) else str(x) for x in items]
    if not texts:
        raise SystemExit(f"no queries in {path}")
    return [texts[i % len(texts)] for i in range(n)]


def instrument(tracer) -> dict:
    """Wrap request-thread entry points of *tracer* with perf_counter.

    ``sampled`` opens the timing window (it precedes chunk building in the
    route), ``emit`` closes it (record build + enqueue). A not-sampled return
    closes the window itself. Returns a mutable accumulator dict.
    """
    acc = {"t_open": None, "total_s": 0.0, "sampled_calls": 0, "emit_calls": 0}
    orig_sampled, orig_emit = tracer.sampled, tracer.emit

    def sampled():
        t = time.perf_counter()
        acc["t_open"] = t
        acc["sampled_calls"] += 1
        r = orig_sampled()
        if not r:
            acc["total_s"] += time.perf_counter() - t
            acc["t_open"] = None
        return r

    def emit(*a, **kw):
        acc["emit_calls"] += 1
        try:
            return orig_emit(*a, **kw)
        finally:
            if acc["t_open"] is not None:
                acc["total_s"] += time.perf_counter() - acc["t_open"]
                acc["t_open"] = None

    tracer.sampled, tracer.emit = sampled, emit
    return acc


def _count_docs(client) -> int | None:
    try:
        g = client.app.state.cymatix.genome
        return int(g.read_conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0])
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        print(f"[warn] doc count failed: {exc!r}", flush=True)
        return None


def build_arm(name: str, trace: TraceConfig, *, base: Path | None, docs: list[str] | None,
              genome: str | None, trace_dir: Path | None):
    tdir = (trace_dir if trace_dir is not None else base) / name / "traces"
    trace.path = str(tdir)
    if genome:
        from cymatix_context.config import load_config
        cfg = load_config()
        cfg.genome.path = genome
        cfg.trace = trace
        cfg.budget.session_delivery_enabled = False
        c = make_client(cfg)
    else:
        c = make_client(make_cymatix_config(trace=trace))
        g = c.app.state.cymatix.genome
        for i, d in enumerate(docs or []):
            g.upsert_gene(make_gene(d, gene_id=f"benchgene{i:08d}"))
    return c, tdir


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--docs", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=60)
    ap.add_argument("--out", default=None)
    ap.add_argument("--genome", default=None, help="existing store; all arms open it")
    ap.add_argument("--trace-dir", default=None, help="trace output root (required with --genome)")
    ap.add_argument("--queries", default=None, help="needles json (benchmarks/dogfood/erb)")
    args = ap.parse_args()

    if args.genome:
        if not args.trace_dir:
            ap.error("--trace-dir is required with --genome")
        if not Path(args.genome).exists():
            ap.error(f"genome not found: {args.genome}")
        os.environ["CYMATIX_DISABLE_LEARN"] = "1"
    elif args.trace_dir:
        ap.error("--trace-dir only applies with --genome")

    total_q = args.n + args.warmup
    qfile = Path(args.queries) if args.queries else None
    queries = _load_queries(qfile, total_q) if qfile else _synthetic_queries(total_q)
    docs = None if args.genome else _docs(args.docs)
    trace_root = Path(args.trace_dir) if args.trace_dir else None
    ckpt = None
    if args.out:
        ckpt = Path(args.out).with_suffix(".rounds.jsonl")
        ckpt.parent.mkdir(parents=True, exist_ok=True)
        ckpt.write_text("", encoding="utf-8")

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        specs = {
            "off": TraceConfig(enabled=False),
            "off2": TraceConfig(enabled=False),   # control: off-vs-off noise floor
            "metadata": TraceConfig(enabled=True, level="metadata", hash_chain=True),
            "full": TraceConfig(enabled=True, level="full", hash_chain=True),
        }
        arms, dirs, knob, accs = {}, {}, {}, {}
        for name, tc in specs.items():
            arms[name], dirs[name] = build_arm(
                name, tc, base=base, docs=docs, genome=args.genome, trace_dir=trace_root)
            t = arms[name].app.state.packet_tracer
            if t.enabled:
                accs[name] = instrument(t)
            knob[name] = {"enabled": t.enabled, "level": t.level,
                          "hash_chain": t.settings["hash_chain"],
                          "writer_present": t.writer is not None,
                          "trace_dir": str(dirs[name])}
            opened = arms[name].app.state.cymatix.genome.path
            print(f"[knob] arm={name} genome_path={opened} "
                  f"docs={_count_docs(arms[name])} observed={knob[name]}", flush=True)
        assert (knob["off"]["enabled"] is False and knob["off2"]["enabled"] is False
                and knob["metadata"]["enabled"] and knob["full"]["enabled"])
        learn_disabled = os.environ.get("CYMATIX_DISABLE_LEARN", "")
        print(f"[config] mode={'genome' if args.genome else 'synthetic'} "
              f"CYMATIX_DISABLE_LEARN={learn_disabled!r} read_only=true "
              f"ignore_delivered=true session_delivery_enabled="
              f"{arms['off'].app.state.cymatix.config.budget.session_delivery_enabled} "
              f"queries={qfile.name if qfile else 'synthetic'} n={args.n} warmup={args.warmup}",
              flush=True)

        names = list(arms)
        body = lambda q: {"query": q, "ignore_delivered": True, "read_only": True,  # noqa: E731
                          "session_id": "bench"}
        for wi, q in enumerate(queries[: args.warmup], 1):
            for nm in names:
                r = arms[nm].post("/context", json=body(q))
                assert r.status_code == 200, r.text
            print(f"warmup {wi}/{args.warmup}", flush=True)

        lat = {nm: [] for nm in names}
        trace_us = {nm: [] for nm in accs}
        for i, q in enumerate(queries[args.warmup:]):
            k = i % len(names)
            order = names[k:] + names[:k]              # rotate start arm
            row = {"i": i, "query": q[:80], "lat_us": {}, "trace_us": {}}
            for nm in order:
                acc = accs.get(nm)
                if acc is not None:
                    acc["total_s"], acc["t_open"] = 0.0, None
                    before = acc["emit_calls"] + acc["sampled_calls"]
                t = time.perf_counter()
                r = arms[nm].post("/context", json=body(q))
                dt = (time.perf_counter() - t) * 1e6
                assert r.status_code == 200
                lat[nm].append(dt)
                row["lat_us"][nm] = round(dt, 1)
                if acc is not None:
                    assert acc["emit_calls"] + acc["sampled_calls"] > before, \
                        f"trace wrapper captured no calls on ON arm {nm} request {i}"
                    tu = acc["total_s"] * 1e6
                    trace_us[nm].append(tu)
                    row["trace_us"][nm] = round(tu, 1)
            if ckpt is not None:
                with ckpt.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
            print(f"{i + 1}/{args.n}", flush=True)

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
        "n": args.n, "warmup": args.warmup,
        "mode": "genome" if args.genome else "synthetic",
        "cover_index_autobuild": _bench_os.environ.get("CYMATIX_COVER_INDEX_AUTOBUILD"),
        "docs": args.docs if not args.genome else None,
        "genome": args.genome,
        "queries_file": qfile.name if qfile else "synthetic",
        "queries_distinct": len(set(queries)),
        "learn_disabled": learn_disabled,
        "method": "FastAPI TestClient, 4 apps (off/off2/metadata/full), same "
                  "queries, rotating-order interleave, hash_chain=true on ON "
                  "arms, ignore_delivered=true, read_only=true; direct trace "
                  "cost = perf_counter around tracer.sampled..emit on the "
                  "request thread",
        "config_dump": {
            "budget.max_genes_per_turn": cfg.budget.max_genes_per_turn,
            "trace_arms": {k: {"enabled": v.enabled, "level": v.level,
                               "hash_chain": v.hash_chain, "sampler_ratio": v.sampler_ratio}
                           for k, v in specs.items()},
        },
        "observed_runtime_knob": knob,
        "trace_lines_written": written,
        "wrapper_call_counts": {nm: {"sampled": a["sampled_calls"], "emit": a["emit_calls"]}
                                for nm, a in accs.items()},
        "direct_trace_cost_us": {nm: _stats(v) for nm, v in trace_us.items()},
        "direct_trace_under_1ms": {nm: {"p50": _pct(sorted(v), .5) < 1000,
                                        "p95": _pct(sorted(v), .95) < 1000}
                                   for nm, v in trace_us.items()},
        "latency_us": {nm: _stats(lat[nm]) for nm in names},
        "paired_delta_us": {lv: _stats(deltas[lv]) for lv in deltas},
        "bootstrap_ci95_us": {
            lv: {"p50": _bootstrap_ci(lat[lv], lat["off"], .5),
                 "p95": _bootstrap_ci(lat[lv], lat["off"], .95)}
            for lv in ("off2", "metadata", "full")},
        "bootstrap": {"resamples": BOOT_RESAMPLES, "seed": BOOT_SEED, "paired": True},
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

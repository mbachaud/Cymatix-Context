"""run_server_scaling.py — server-mode HTTP concurrency scaling on one bed.

`run_latency_scaling.py` measured device contention: N subprocesses, each
with its own manager and its own encoder stack, hammering the same genome
file in-process. This script measures the OTHER half — the half real
integrations actually hit: one uvicorn serving the bed over HTTP, N stdlib
clients POSTing /context concurrently. That path shares one encoder stack
and funnels every build_context through the module-global 2-thread executor,
so the questions are different: does throughput plateau at c>=3, does the
event loop block while workers grind, and which pipeline stage inflates
under load.

Three instruments run per concurrency level:

* **Client workers** — one subprocess per worker (this file, ``--worker``),
  pure stdlib urllib, POSTing /context with a unique ``session_id`` per
  worker, ``ignore_delivered`` + ``read_only`` intent. Per-query wall ms and
  the rank of gold inside the returned ``agent.citations`` gene ids.
* **Event-loop canary** — a parent thread GETs
  ``/debug/pipeline/recent?limit=1`` every ``--canary-interval`` s during the
  timed window (NOT /health — on this branch /health may probe the upstream).
  Canary p95 blowing up while worker latency climbs = the loop itself is
  blocked, not just the executor queue.
* **Ring tail** — after the timed window the parent pulls the pipeline ring
  (``_stage_receipts.fetch_recent`` + ``aggregate``) for per-stage /
  per-signal p50/p95. The ring holds ~128 stage events, i.e. the last ~12
  requests, so the receipt labels it ``ring_tail_sample``, not a full-window
  distribution.

Arms:

* ``--arm read`` (default): queries only, server hot-swapped onto the REAL
  bed with ``read_only=True`` (the PR #91 fixture guard: upsert/touch/
  link/log_health are no-ops). Known residue: the /context CWoLa logger
  writes telemetry rows via ``genome.conn`` with no read_only gate, so even
  the read arm appends ``cwola_log`` rows to the served db (gene content is
  untouched). Pass ``--copy-bed`` for a strict never-touch run against a
  throwaway copy.
* ``--arm mixed``: adds one extra subprocess POSTing small synthetic strands
  to /ingest at full speed until the level ends. The mixed arm NEVER runs
  against the real bed: the genome (+ -wal/-shm) is first copied under the
  run's log dir and the server is pointed at the copy with
  ``read_only=False`` — required, because a read_only store silently no-ops
  ``upsert_doc`` while /ingest still returns gene_ids, which would fake a
  write workload. The receipt cross-checks /stats gene counts
  (``writes_verified``) so a silently-dropped write arm cannot pass.

Honesty guardrails, same spirit as the rest of the bed:

* **Fresh server per level.** Each level boots its own uvicorn (clean
  session state, fresh ring) and pays its own encoder load — which is why
  the parent sends one untimed warmup query before any worker spawns, and
  each worker sends its own untimed warmup before signaling READY.
* **Workers are barrier-synced.** READY files + a GO file, cloned from
  run_latency_scaling. The receipt records overlap_fraction; a low value
  means the "concurrent" claim is weak and the level should not be quoted.
* **Rank is recorded per query.** A server that gets fast by dropping gold
  would otherwise read as a win. Anti-fooling gates are baked INTO the
  receipt: a level is ``valid=false`` if its recall deviates >0.02 from the
  c=1 level, overlap < 0.8, any 5xx was returned, or (mixed) writes did not
  verifiably land.

Usage::

    python benchmarks/dogfood/run_server_scaling.py                 # read arm
    python benchmarks/dogfood/run_server_scaling.py --arm mixed
    python benchmarks/dogfood/run_server_scaling.py --concurrency 1,4 \\
        --limit 3 --rounds 1                                        # smoke
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "benchmarks"))
sys.path.insert(0, str(_HERE))

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CANARY_PATH = "/debug/pipeline/recent?limit=1"


# ------------------------------------------------------------------ http ----

def _http_json(method: str, url: str, body: dict | None = None,
               timeout: float = 30.0):
    """Stdlib JSON request with an explicit timeout (repo rule: no hanging
    requests). Raises urllib.error.HTTPError / URLError on failure."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _percentile(vals: list[float], p: float) -> float:
    # Ceiling index: at small n a floor index can land *below* the median
    # (p95 of 2 samples must be the max, not the min).
    s = sorted(vals)
    return s[min(len(s) - 1, math.ceil(p * (len(s) - 1)))]


# ------------------------------------------------------------ query worker ----

def worker_main(args: argparse.Namespace) -> int:
    """Runs inside each spawned client subprocess: warmup, barrier, timed
    rounds of POST /context over plain urllib. No cymatix imports — the
    server owns the encoders; this process is a thin HTTP client."""
    resolved = json.loads((REPO_ROOT / args.resolved).read_text(encoding="utf-8"))
    needles = resolved["needles"][: args.limit] if args.limit else resolved["needles"]
    gold_by_needle = {k: set(v) for k, v in json.loads(
        (REPO_ROOT / args.gold).read_text(encoding="utf-8")).items()}
    ctx_url = f"{args.base_url}/context"

    def post_query(query: str, session_id: str, timeout: float):
        # Body fields per routes_context.py: read_only intent via the
        # explicit "read_only" key (_request_read_only), session working-set
        # bypass via "ignore_delivered", CWoLa session via "session_id".
        return _http_json("POST", ctx_url, {
            "query": query,
            "session_id": session_id,
            "ignore_delivered": True,
            "read_only": True,
        }, timeout=timeout)

    # Untimed warmup. The parent already forced the encoder load, but this
    # worker's own first POST may still queue behind other warmups on the
    # 2-thread executor — hence the generous separate timeout.
    try:
        post_query(needles[0]["query"], f"{args.session_id}-warmup",
                   args.warmup_timeout)
    except Exception as exc:  # noqa: BLE001 - dead warmup = dead worker
        print(f"worker {args.worker_idx}: warmup failed: {exc!r}", file=sys.stderr)
        return 4

    sync = Path(args.sync_dir)
    (sync / f"ready_{args.worker_idx}").write_text("ready", encoding="utf-8")
    deadline = time.time() + 600
    go = sync / "go"
    while not go.exists():
        if time.time() > deadline:
            print(f"worker {args.worker_idx}: no GO within 600s", file=sys.stderr)
            return 3
        time.sleep(0.05)

    samples = []
    t_start = time.time()
    for rnd in range(args.rounds):
        for nd in needles:
            gold = gold_by_needle.get(nd["name"], set())
            q0 = time.perf_counter()
            try:
                payload = post_query(nd["query"], args.session_id,
                                     args.request_timeout)
            except urllib.error.HTTPError as exc:
                samples.append({"needle": nd["name"], "round": rnd,
                                "error": f"HTTP {exc.code}",
                                "status": int(exc.code)})
                continue
            except Exception as exc:  # noqa: BLE001 - a failing query is a datapoint
                samples.append({"needle": nd["name"], "round": rnd,
                                "error": repr(exc), "status": None})
                continue
            latency_ms = (time.perf_counter() - q0) * 1000
            # /context returns a Continue-compatible LIST envelope:
            # ``return [response]``. Expressed gene ids ride
            # response[0].agent.citations[*].gene_id, in expressed order.
            resp0 = payload[0] if isinstance(payload, list) and payload else (payload or {})
            citations = ((resp0.get("agent") or {}).get("citations") or [])
            ordered = [c.get("gene_id") for c in citations][: args.k]
            rank = next((i for i, g in enumerate(ordered, 1) if g in gold), None)
            samples.append({"needle": nd["name"], "round": rnd,
                            "latency_ms": round(latency_ms, 1), "rank": rank,
                            "status": 200})
    t_end = time.time()

    Path(args.worker_out).write_text(json.dumps({
        "worker": args.worker_idx,
        "t_start": t_start,
        "t_end": t_end,
        "samples": samples,
    }, indent=2) + "\n", encoding="utf-8")
    return 0


# ----------------------------------------------------------- ingest worker ----

def ingest_worker_main(args: argparse.Namespace) -> int:
    """Mixed-arm write pump: POST /ingest with unique synthetic strands at
    full speed until the stop file appears. Only ever pointed at the bed
    COPY (the parent enforces that)."""
    ingest_url = f"{args.base_url}/ingest"
    stop = Path(args.stop_file)
    sync = Path(args.sync_dir)

    def strand(i: int) -> str:
        u = uuid.uuid4().hex
        return (
            f"synthetic write-arm strand {u} seq {i}. This strand exists only "
            f"to exercise the /ingest write path while read queries are in "
            f"flight; marker {u} keeps every post unique so dedup cannot "
            f"collapse the write load into a no-op."
        )

    def post_strand(i: int, timeout: float):
        return _http_json("POST", ingest_url, {
            "content": strand(i),
            "content_type": "text",
            "metadata": {"source": "server_scaling_write_arm", "seq": i},
        }, timeout=timeout)

    # Untimed warmup: first ingest pays tagger/encoder ingest-side loads.
    try:
        post_strand(-1, args.warmup_timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"ingest worker: warmup failed: {exc!r}", file=sys.stderr)
        return 4

    (sync / "ready_ingest").write_text("ready", encoding="utf-8")
    deadline = time.time() + 900
    go = sync / "go"
    while not go.exists():
        if stop.exists():
            break
        if time.time() > deadline:
            print("ingest worker: no GO within 900s", file=sys.stderr)
            return 3
        time.sleep(0.05)

    posts = accepted = genes_accepted = errors = 0
    post_ms: list[float] = []
    t_start = time.time()
    i = 0
    while not stop.exists():
        q0 = time.perf_counter()
        try:
            resp = post_strand(i, args.request_timeout)
            post_ms.append((time.perf_counter() - q0) * 1000)
            posts += 1
            count = int((resp or {}).get("count", 0))
            if count >= 1:
                accepted += 1
                genes_accepted += count
        except Exception:  # noqa: BLE001 - a failing post is a datapoint
            posts += 1
            errors += 1
        i += 1
    t_end = time.time()

    Path(args.worker_out).write_text(json.dumps({
        "t_start": t_start,
        "t_end": t_end,
        "posts": posts,
        "accepted": accepted,
        "genes_accepted": genes_accepted,
        "errors": errors,
        "median_post_ms": round(statistics.median(post_ms), 1) if post_ms else None,
        "p95_post_ms": round(_percentile(post_ms, 0.95), 1) if post_ms else None,
    }, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------------------------------------------------------------- parent ----

def _canary_loop(base_url: str, interval_s: float, samples: list[float],
                 errors: list[float], stop_evt: threading.Event) -> None:
    """Event-loop-blocking detector: time a trivial GET against the ring
    endpoint. If the asyncio loop is healthy this returns in ~1-5ms no
    matter how loaded the executor is; if the loop itself blocks, the
    canary p95/max blows up."""
    url = f"{base_url}{CANARY_PATH}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    while not stop_evt.is_set():
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                resp.read()
            samples.append((time.perf_counter() - t0) * 1000)
        except Exception:  # noqa: BLE001 - a canary failure IS the signal
            errors.append(1.0)
        stop_evt.wait(interval_s)


def _copy_bed(genome: Path, dest_dir: Path) -> Path:
    """Copy the genome (+ sidecar -wal/-shm if present) into dest_dir.

    The mixed arm writes; the real bed must never be its target. Refuses
    to proceed if source and destination resolve to the same file."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dst = dest_dir / genome.name
    if dst.resolve() == genome.resolve():
        raise RuntimeError(f"bed copy would overwrite the source: {genome}")
    shutil.copy2(genome, dst)
    for suffix in ("-wal", "-shm"):
        side = genome.with_name(genome.name + suffix)
        if side.exists():
            shutil.copy2(side, dst.with_name(dst.name + suffix))
    return dst


def _run_level(concurrency: int, args: argparse.Namespace, run_dir: Path,
               fixture, warmup_query: str) -> dict:
    """Boot a fresh server, spawn `concurrency` HTTP client workers (plus
    the ingest pump on the mixed arm), aggregate samples + canary + ring."""
    from bench_orchestrator import BenchServer
    import _stage_receipts

    mixed = args.arm == "mixed"
    sync = run_dir / f"sync_c{concurrency}"
    sync.mkdir(parents=True, exist_ok=True)
    stop_file = sync / "stop"

    server = BenchServer(host="127.0.0.1", port=args.port, repo_root=REPO_ROOT,
                         log_to=run_dir / f"server_c{concurrency}.log")

    procs, outs, logs = [], [], []
    p_ing = None
    ing_out = run_dir / f"ingest_c{concurrency}.json"
    canary_samples: list[float] = []
    canary_errors: list[float] = []
    stop_evt = threading.Event()
    canary_thread = None
    ring: dict | None = None
    genes_after = None

    try:
        # Boot inside the try so a spawn that turns unhealthy still gets
        # its process tree torn down by the finally (issue #127 semantics).
        swap = server.start(fixture)
        genes_at_boot = swap.genes

        # Parent untimed warmup: the fresh server loads BGE-M3/SPLADE on
        # its first query (~15-60s). Pay that once, before any worker
        # exists, so worker warmups queue on a warm server.
        _http_json("POST", f"{server.url}/context", {
            "query": warmup_query,
            "session_id": f"scale-c{concurrency}-warmup-parent",
            "ignore_delivered": True,
            "read_only": True,
        }, timeout=args.warmup_timeout)

        for i in range(concurrency):
            out = run_dir / f"worker_c{concurrency}_{i}.json"
            log = open(run_dir / f"worker_c{concurrency}_{i}.log", "w",
                       encoding="utf-8")
            cmd = [sys.executable, str(Path(__file__).resolve()), "--worker",
                   "--worker-idx", str(i), "--worker-out", str(out),
                   "--sync-dir", str(sync), "--base-url", server.url,
                   "--session-id", f"scale-c{concurrency}-w{i}",
                   "--resolved", args.resolved, "--gold", args.gold,
                   "--k", str(args.k), "--rounds", str(args.rounds),
                   "--limit", str(args.limit),
                   "--request-timeout", str(args.request_timeout),
                   "--warmup-timeout", str(args.warmup_timeout)]
            procs.append(subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=log,
                                          stderr=log, creationflags=NO_WINDOW))
            outs.append(out)
            logs.append(log)

        if mixed:
            ing_log = open(run_dir / f"ingest_c{concurrency}.log", "w",
                           encoding="utf-8")
            cmd = [sys.executable, str(Path(__file__).resolve()),
                   "--ingest-worker", "--worker-out", str(ing_out),
                   "--sync-dir", str(sync), "--base-url", server.url,
                   "--stop-file", str(stop_file),
                   "--request-timeout", str(args.request_timeout),
                   "--warmup-timeout", str(args.warmup_timeout)]
            p_ing = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=ing_log,
                                     stderr=ing_log, creationflags=NO_WINDOW)
            logs.append(ing_log)

        # Barrier: every participant warmed up before any timed query runs.
        want = concurrency + (1 if mixed else 0)

        def _ready() -> int:
            n = sum((sync / f"ready_{i}").exists() for i in range(concurrency))
            if mixed and (sync / "ready_ingest").exists():
                n += 1
            return n

        all_procs = procs + ([p_ing] if p_ing is not None else [])
        deadline = time.time() + args.level_timeout
        while _ready() < want:
            if any(p.poll() not in (None, 0) for p in all_procs):
                raise RuntimeError("a worker died during warmup — see its .log")
            if time.time() > deadline:
                raise TimeoutError(f"workers not ready within {args.level_timeout}s")
            time.sleep(0.2)
        (sync / "go").write_text("go", encoding="utf-8")

        canary_thread = threading.Thread(
            target=_canary_loop,
            args=(server.url, args.canary_interval, canary_samples,
                  canary_errors, stop_evt),
            daemon=True)
        canary_thread.start()

        for p in procs:
            p.wait(timeout=args.level_timeout)
        stop_evt.set()
        if canary_thread is not None:
            canary_thread.join(timeout=5)

        # Ring tail: pulled right after the query window, before write-arm
        # wind-down traffic can push query stages out of the ~128-entry ring.
        try:
            entries = _stage_receipts.fetch_recent(server.url, limit=128)
            ring = _stage_receipts.aggregate(entries)
            ring["entries"] = len(entries)
        except Exception as exc:  # noqa: BLE001 - receipt records the failure
            ring = {"error": repr(exc)}

        if mixed:
            stop_file.write_text("stop", encoding="utf-8")
            if p_ing is not None:
                try:
                    p_ing.wait(timeout=180)
                except subprocess.TimeoutExpired:
                    p_ing.kill()
            try:
                stats = _http_json("GET", f"{server.url}/stats", timeout=60) or {}
                genes_after = int(stats.get("total_genes"))
            except Exception:  # noqa: BLE001
                genes_after = None
    finally:
        stop_evt.set()
        try:
            if not stop_file.exists():
                stop_file.write_text("stop", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        for p in procs + ([p_ing] if p_ing is not None else []):
            if p.poll() is None:
                p.kill()
        for log in logs:
            log.close()
        server.stop()

    workers = [json.loads(o.read_text(encoding="utf-8")) for o in outs]
    ok = [s for w in workers for s in w["samples"] if "latency_ms" in s]
    errors = [s for w in workers for s in w["samples"] if "error" in s]
    if not ok:
        raise RuntimeError(f"c={concurrency}: no successful samples ({len(errors)} errors)")
    http_5xx = sum(1 for s in errors
                   if isinstance(s.get("status"), int) and s["status"] >= 500)

    lat = [s["latency_ms"] for s in ok]
    ranks = [s["rank"] for s in ok]
    wall = max(w["t_end"] for w in workers) - min(w["t_start"] for w in workers)
    # Overlap fraction: shared timed window over the widest worker span. 1.0
    # means fully concurrent; low values mean the level barely overlapped and
    # its numbers should not be quoted as concurrency.
    overlap = ((min(w["t_end"] for w in workers) - max(w["t_start"] for w in workers))
               / wall if concurrency > 1 and wall > 0 else 1.0)

    rk = f"recall@{args.k}"
    level = {
        "concurrency": concurrency,
        "samples": len(ok),
        "errors": len(errors),
        "http_5xx": http_5xx,
        "median_latency_ms": round(statistics.median(lat), 1),
        "mean_latency_ms": round(statistics.mean(lat), 1),
        "p95_latency_ms": round(_percentile(lat, 0.95), 1),
        "max_latency_ms": round(max(lat), 1),
        "per_worker_median_ms": [
            round(statistics.median(ms), 1) if (ms := [
                s["latency_ms"] for s in w["samples"] if "latency_ms" in s
            ]) else None
            for w in workers],
        rk: round(sum(1 for r in ranks if r and r <= args.k) / len(ranks), 4),
        "wall_seconds": round(wall, 1),
        "overlap_fraction": round(max(0.0, overlap), 3),
        "throughput_qps": round(len(ok) / wall, 2) if wall > 0 else None,
        "canary": {
            "n": len(canary_samples),
            "p50_ms": round(_percentile(canary_samples, 0.5), 1) if canary_samples else None,
            "p95_ms": round(_percentile(canary_samples, 0.95), 1) if canary_samples else None,
            "max_ms": round(max(canary_samples), 1) if canary_samples else None,
            "errors": len(canary_errors),
        },
        "ring_tail_sample": ring,
        "genes_at_boot": genes_at_boot,
    }

    if mixed:
        try:
            ing = json.loads(ing_out.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            ing = {}
        elapsed = (ing.get("t_end", 0) or 0) - (ing.get("t_start", 0) or 0)
        accepted = int(ing.get("accepted", 0))
        genes_accepted = int(ing.get("genes_accepted", 0))
        level["ingest"] = {
            "posts": int(ing.get("posts", 0)),
            "accepted": accepted,
            "genes_accepted": genes_accepted,
            "errors": int(ing.get("errors", 0)),
            "median_post_ms": ing.get("median_post_ms"),
            "p95_post_ms": ing.get("p95_post_ms"),
            "elapsed_s": round(elapsed, 1),
            "posts_per_s": round(ing.get("posts", 0) / elapsed, 3) if elapsed > 0 else None,
            "ingest_genes_per_s": round(genes_accepted / elapsed, 3) if elapsed > 0 else None,
            "genes_at_boot": genes_at_boot,
            "genes_after": genes_after,
            # A read_only store silently no-ops upsert_doc while /ingest
            # still returns gene_ids — this cross-check catches that class
            # of fake write workload.
            "writes_verified": bool(
                accepted > 0 and genes_after is not None
                and genes_after > genes_at_boot
            ),
        }

    return level


def _apply_validity(levels: list[dict], rk: str) -> None:
    """Anti-fooling gates, baked into the receipt rather than left to the
    reader: recall drift vs c=1, weak overlap, any 5xx, unverified writes."""
    base = next((l for l in levels if l["concurrency"] == 1), None)
    for lvl in levels:
        reasons = []
        if base is None:
            reasons.append("no c=1 level to baseline recall against")
        elif abs(lvl[rk] - base[rk]) > 0.02:
            reasons.append(
                f"{rk} {lvl[rk]} deviates >0.02 from c=1 ({base[rk]})")
        if lvl["concurrency"] > 1 and lvl["overlap_fraction"] < 0.8:
            reasons.append(f"overlap_fraction {lvl['overlap_fraction']} < 0.8")
        if lvl.get("http_5xx", 0) > 0:
            reasons.append(f"{lvl['http_5xx']} HTTP 5xx responses")
        ing = lvl.get("ingest")
        if ing and ing.get("accepted", 0) > 0 and not ing.get("writes_verified"):
            reasons.append("ingest accepted posts but gene count did not grow")
        lvl["valid"] = not reasons
        lvl["invalid_reasons"] = reasons


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--genome",
                    default="F:/Projects/cymatix-context/genomes/dogfood/genome.db",
                    help="bed to serve; NEVER written by the read arm, "
                         "copied before the mixed arm touches it")
    ap.add_argument("--resolved", default="benchmarks/dogfood/needles_resolved.json")
    ap.add_argument("--gold",
                    default="F:/Projects/cymatix-context/benchmarks/dogfood/"
                            "gene_ids/gold_by_needle.json")
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--rounds", type=int, default=2,
                    help="timed passes over the needle set per worker (default 2)")
    ap.add_argument("--limit", type=int, default=0,
                    help="use only the first N needles (0 = all; for smoke tests)")
    ap.add_argument("--concurrency", default="1,2,4,8,16",
                    help="comma list of client counts, each level on a fresh server")
    ap.add_argument("--arm", choices=("read", "mixed"), default="read",
                    help="read = queries only vs the real bed (read_only); "
                         "mixed = adds a full-speed /ingest pump vs a bed COPY")
    ap.add_argument("--port", type=int, default=11492,
                    help="non-default port so a user server on 11437 is never hit")
    ap.add_argument("--level-timeout", type=int, default=1800)
    ap.add_argument("--canary-interval", type=float, default=0.25,
                    help="seconds between event-loop canary probes")
    ap.add_argument("--request-timeout", type=float, default=120.0,
                    help="per timed /context POST (workers queue at high c)")
    ap.add_argument("--warmup-timeout", type=float, default=300.0,
                    help="untimed first-hit timeout; covers the encoder load")
    ap.add_argument("--copy-bed", action="store_true",
                    help="serve a throwaway copy even on the read arm "
                         "(strict no-touch: /context appends cwola_log rows "
                         "even under read_only=True)")
    ap.add_argument("--out", default="benchmarks/dogfood/server_scaling.json")
    # Internal worker-mode flags (parent spawns this same file).
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--ingest-worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--worker-idx", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--worker-out", default="", help=argparse.SUPPRESS)
    ap.add_argument("--sync-dir", default="", help=argparse.SUPPRESS)
    ap.add_argument("--base-url", default="", help=argparse.SUPPRESS)
    ap.add_argument("--session-id", default="", help=argparse.SUPPRESS)
    ap.add_argument("--stop-file", default="", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker:
        return worker_main(args)
    if args.ingest_worker:
        return ingest_worker_main(args)

    from bench_orchestrator import Fixture  # parent only; workers stay stdlib

    levels_req = [int(c) for c in args.concurrency.split(",") if c.strip()]
    run_dir = REPO_ROOT / "benchmarks/dogfood/logs/server_scaling" / \
        time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    genome_path = (REPO_ROOT / args.genome).resolve()
    if not genome_path.exists():
        print(f"genome not found: {genome_path}", file=sys.stderr)
        return 2

    # Bed identity lives next to the gold file; tolerate its absence.
    summary_path = (REPO_ROOT / args.gold).parent / "summary.json"
    identity = (json.loads(summary_path.read_text(encoding="utf-8"))
                .get("identity_sha256") if summary_path.exists() else None)

    resolved = json.loads((REPO_ROOT / args.resolved).read_text(encoding="utf-8"))
    needles = resolved["needles"][: args.limit] if args.limit else resolved["needles"]
    if not needles:
        print("no needles after --limit; nothing to measure", file=sys.stderr)
        return 2
    warmup_query = needles[0]["query"]

    # The mixed arm writes, so it NEVER serves the real bed: point the
    # fixture at a copy with read_only=False (a read_only store silently
    # no-ops upsert_doc, which would fake the write workload). The read arm
    # serves the real bed read_only unless --copy-bed asks for strict
    # no-touch. CYMATIX_DISABLE_LEARN is belt-and-suspenders against the
    # /v1/chat learn path; it does not gate /context's cwola logger.
    if args.arm == "mixed" or args.copy_bed:
        served_db = _copy_bed(genome_path, run_dir / "bed_copy")
        fixture = Fixture(
            name="dogfood-scale-copy", db=str(served_db),
            read_only=(args.arm != "mixed"),
            extra_env={"CYMATIX_DISABLE_LEARN": "1"})
    else:
        served_db = genome_path
        fixture = Fixture(
            name="dogfood-scale", db=str(genome_path), read_only=True,
            extra_env={"CYMATIX_DISABLE_LEARN": "1"})

    rk = f"recall@{args.k}"
    print(f"server scaling ({args.arm} arm): concurrency {levels_req}, "
          f"rounds={args.rounds}, k={args.k}, port={args.port}, "
          f"bed {identity[:16] if identity else 'UNKNOWN'}"
          f"{'  [serving COPY]' if str(served_db) != str(genome_path) else ''}\n")

    levels = []
    for c in levels_req:
        t0 = time.time()
        lvl = _run_level(c, args, run_dir, fixture, warmup_query)
        levels.append(lvl)
        canary = lvl["canary"]
        line = (f"  c={c}: median {lvl['median_latency_ms']}ms  "
                f"p95 {lvl['p95_latency_ms']}ms  {rk} {lvl[rk]}  "
                f"overlap {lvl['overlap_fraction']}  qps {lvl['throughput_qps']}  "
                f"canary p95 {canary['p95_ms']}ms")
        if lvl.get("ingest"):
            line += f"  ingest {lvl['ingest']['ingest_genes_per_s']} genes/s"
        print(line + f"  ({time.time() - t0:.0f}s incl. boot+warmup)", flush=True)

    _apply_validity(levels, rk)

    base = next((l for l in levels if l["concurrency"] == 1), None)
    scaling = [{
        "concurrency": l["concurrency"],
        "median_latency_x": round(l["median_latency_ms"] / base["median_latency_ms"], 2),
        "p95_latency_x": round(l["p95_latency_ms"] / base["p95_latency_ms"], 2),
        "throughput_x": round(l["throughput_qps"] / base["throughput_qps"], 2)
        if base["throughput_qps"] and l["throughput_qps"] else None,
        "canary_p95_x": round(l["canary"]["p95_ms"] / base["canary"]["p95_ms"], 2)
        if base["canary"]["p95_ms"] and l["canary"]["p95_ms"] else None,
    } for l in levels] if base else []

    payload = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "genome": str(genome_path),
        "served_db": str(served_db),
        "bed_copied": str(served_db) != str(genome_path),
        "arm": args.arm,
        "bed_identity_sha256": identity,
        "k": args.k,
        "rounds": args.rounds,
        "needles": args.limit or "all",
        "port": args.port,
        "cpu_count": os.cpu_count(),
        "worker_model": "one uvicorn (BenchServer) per level, fresh process = "
                        "clean session state + fresh ring; N stdlib HTTP client "
                        "subprocesses share the server's encoders and its "
                        "module-global 2-thread executor",
        "note": "warmups untimed (parent + per worker); workers barrier-synced; "
                "ranks recorded per query as a fast-because-broken guard; "
                "validity gates baked into each level",
        "canary_endpoint": CANARY_PATH,
        "canary_interval_s": args.canary_interval,
        "ring_note": "ring_tail_sample aggregates the pipeline ring's last "
                     "~128 stage events (~ the final ~12 requests of the "
                     "level), not the full timed window",
        "read_arm_residue_note": None if str(served_db) != str(genome_path) else (
            "served the real bed read_only=True: gene content is guarded, but "
            "/context's cwola logger appends telemetry rows regardless — use "
            "--copy-bed for strict no-touch runs"),
        "levels": levels,
        "scaling_vs_c1": scaling,
        "worker_logs": str(run_dir),
    }
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if scaling:
        print("\nscaling vs c=1: " + "   ".join(
            f"c={s['concurrency']}: {s['median_latency_x']}x median, "
            f"{s['throughput_x']}x qps" for s in scaling if s["concurrency"] != 1))
    invalid = [l["concurrency"] for l in levels if not l["valid"]]
    if invalid:
        print(f"WARNING: levels {invalid} failed validity gates — "
              f"see invalid_reasons in the receipt")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

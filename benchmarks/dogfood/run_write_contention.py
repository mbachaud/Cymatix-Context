"""run_write_contention.py — read latency + integrity under concurrent writes.

The 47h tagger campaign ran a bulk ingest against the dogfood genome while
agent sessions kept querying it. Every latency number so far was measured on
a quiet store; this harness measures what concurrent *writing* does to read
latency and what breaks: 'database is locked' errors, WAL growth, torn or
lost writes. Five arms, each on a **fresh copy** of the bed (the original
genome.db is never opened writable, never even swapped into a server):

* **a — control**: BenchServer on the copy (read-only fixture), ``--readers``
  HTTP query workers, no writer. All other arms are quoted as deltas vs a.
* **b — HTTP mixed**: same readers + one subprocess POSTing ``/ingest``
  continuously. The fixture is swapped with ``read_only=False`` because the
  swap route sets a soft ``store.read_only`` flag that turns ``upsert_doc``
  into a *silent no-op returning a gene_id* — a read-only fixture here would
  fabricate lost writes by design. All writes serialize through the server
  process's ``_write_lock``.
* **c — writer-bypass (the tagger scenario)**: server serves readers on the
  copy; SEPARATELY one subprocess opens the same copy path with its own
  ``CymatixContextManager`` and calls ``manager.ingest()`` in a loop. This
  bypasses the server's in-process ``_write_lock`` entirely — that is the
  point: two OS processes contending on one SQLite file, exactly the 47h
  topology.
* **d — no server**: two direct-writer subprocesses, no readers — pure
  two-writer SQLite contention.
* **e (opt-in via --arms)**: arm-c topology, then the bypass writer is
  ``proc.kill()``ed mid-run (TerminateProcess — abrupt is the test); the
  copy is reopened with sqlite3 for ``PRAGMA integrity_check`` plus the
  full correctness block.

Correctness block after EVERY arm (hard gates; arm verdict FAIL if any is
false): ``integrity_ok`` (PRAGMA integrity_check == 'ok'),
``zero_lost_writes`` (COUNT(genes) == baseline + writer-reported successful
upserts; arm e allows committed-but-unreported rows from the kill window and
reports them), ``zero_partial_publish`` (every writer-reported gene_id has
``promoter_index`` rows AND a ``genes_fts`` row — table names read from the
DDL in ``cymatix_context/storage/ddl.py`` and re-verified against
sqlite_master at runtime — and the genes/genes_fts row-count deltas match).

Honesty guardrails, same spirit as run_latency_scaling.py:

* **Fresh copy per arm, checkpointed at baseline.** The copy's WAL is
  TRUNCATE-checkpointed before baseline counts, so WAL-size growth during
  the arm is attributable to the arm. The parent samples the copy's ``-wal``
  size every 5s; note that ``store.close()`` checkpoints, so ``final_bytes``
  is the last in-run sample, and ``after_close_bytes`` is recorded
  separately.
* **Warmup is untimed — for writers too.** The first ``ingest`` loads
  SPLADE/BGE-M3; it is logged with ``warmup: true``, excluded from the
  latency series, but *included* in the lost-write count gate.
* **Barrier-synced, overlap recorded.** Readers and writers all signal READY
  and wait for GO; the reader overlap fraction is in the receipt.
* **Rank is recorded per query.** Readers extract the expressed gene order
  from ``/context``'s ``agent.citations`` (the route returns a one-element
  list envelope); a "fast" arm that stopped retrieving gold reads as a
  recall regression, not a latency win.
* **Per-upsert latency is a SERIES, order preserved**, so the inline WAL
  checkpoint spikes at upsert #50 (PASSIVE) / #500 (TRUNCATE) are visible at
  their indices. The series includes tagger + encoder time, not just SQLite
  — the spikes ride on top. Stored series capped at 2000 points; p50/p95/max
  and top-5 outlier indices computed over the full series.
* **busy_timeout is 30s** on store connections, so every counted
  'locked'/'busy' error means ~30s of starvation, not a transient blip.
* **CWoLa logging writes ride the /context hot path** even in "read-only"
  arms (the swap flag is soft — the server's connection is read-write), so
  arm a legitimately shows nonzero WAL growth; the gene-count gate is
  unaffected (cwola_log is a separate table).
* **ring_tail_sample** embeds ``/debug/pipeline/recent`` aggregates; the
  ring holds ~128 entries, i.e. the last ~12 requests only — a tail sample,
  not the whole arm. The event-loop canary polls the same endpoint (NOT /health,
  which may probe the upstream on this branch).

Usage::

    python benchmarks/dogfood/run_write_contention.py            # arms a,b,c,d
    python benchmarks/dogfood/run_write_contention.py --arms a,c,e
    python benchmarks/dogfood/run_write_contention.py --limit 3 --rounds 1 \
        --duration 45                                            # smoke

DO NOT point --genome at a store you care about: writers mutate the copies
only, but the harness refuses to run if copy == source as a last resort.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "benchmarks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_orchestrator import BenchServer, Fixture  # noqa: E402
from _stage_receipts import fetch_recent, aggregate  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HOST = "127.0.0.1"

VALID_ARMS = ("a", "b", "c", "d", "e")
ARM_LABELS = {
    "a": "control: readers only, read-only fixture",
    "b": "HTTP mixed: readers + one /ingest writer through the server _write_lock",
    "c": "writer-bypass: readers on server + direct-open writer (tagger topology)",
    "d": "no server: two direct writers, pure SQLite two-writer contention",
    "e": "arm-c topology, bypass writer TerminateProcess'd mid-run",
}

# Timeouts (s). First hits load BGE-M3/SPLADE (~15-60s) — be generous.
READER_WARMUP_TIMEOUT = 180.0
READER_QUERY_TIMEOUT = 120.0
HTTP_INGEST_WARMUP_TIMEOUT = 300.0
HTTP_INGEST_TIMEOUT = 120.0
CANARY_TIMEOUT = 10.0
WAL_SAMPLE_INTERVAL = 5.0
CANARY_SAMPLE_INTERVAL = 2.0
SERIES_CAP = 2000


# ---------------------------------------------------------------- shared ----

def _percentile(vals: list[float], p: float) -> float:
    # Ceiling index: at small n a floor index can land *below* the median
    # (p95 of 2 samples must be the max, not the min).
    s = sorted(vals)
    return s[min(len(s) - 1, math.ceil(p * (len(s) - 1)))]


def _post_json(url: str, body: dict, timeout: float):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _await_go(sync: Path, who: str, timeout_s: float = 600.0) -> bool:
    deadline = time.time() + timeout_s
    go = sync / "go"
    while not go.exists():
        if time.time() > deadline:
            print(f"{who}: no GO within {timeout_s:.0f}s", file=sys.stderr)
            return False
        time.sleep(0.05)
    return True


# ---------------------------------------------------------------- reader ----

def reader_main(args: argparse.Namespace) -> int:
    """HTTP query worker: warmup, barrier, timed rounds against /context."""
    resolved = json.loads((REPO_ROOT / args.resolved).read_text(encoding="utf-8"))
    needles = resolved["needles"][: args.limit] if args.limit else resolved["needles"]
    gold_by_needle = {k: set(v) for k, v in json.loads(
        (REPO_ROOT / args.gold).read_text(encoding="utf-8")).items()}

    base = args.server_url.rstrip("/")
    session_id = f"wc-reader-{args.worker_idx}"

    def _query(q: str, timeout: float):
        payload = _post_json(base + "/context", {
            "query": q,
            "read_only": True,          # _request_read_only honors this field
            "ignore_delivered": True,
            "session_id": session_id,
        }, timeout=timeout)
        # /context returns a one-element list envelope: `return [response]`.
        return payload[0] if isinstance(payload, list) and payload else payload

    # Untimed warmup: server-side encoder load + first-query caches.
    _query(needles[0]["query"], READER_WARMUP_TIMEOUT)

    sync = Path(args.sync_dir)
    (sync / f"ready_r{args.worker_idx}").write_text("ready", encoding="utf-8")
    if not _await_go(sync, f"reader {args.worker_idx}"):
        return 3

    samples = []
    t_start = time.time()
    for rnd in range(args.rounds):
        for nd in needles:
            gold = gold_by_needle.get(nd["name"], set())
            q0 = time.time()
            try:
                resp = _query(nd["query"], READER_QUERY_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - a failing query is a datapoint
                samples.append({"needle": nd["name"], "round": rnd,
                                "error": repr(exc)})
                continue
            latency_ms = (time.time() - q0) * 1000
            cites = ((resp or {}).get("agent") or {}).get("citations") or []
            ordered = [c.get("gene_id") for c in cites][: args.k]
            rank = next((i for i, g in enumerate(ordered, 1) if g in gold), None)
            samples.append({"needle": nd["name"], "round": rnd,
                            "latency_ms": round(latency_ms, 1), "rank": rank,
                            "citations": len(cites)})
    t_end = time.time()

    Path(args.worker_out).write_text(json.dumps({
        "worker": args.worker_idx,
        "t_start": t_start,
        "t_end": t_end,
        "samples": samples,
    }, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------------------------------------------------------------- writer ----

def _synth_text(widx: int, i: int) -> str:
    # Unique (uuid) so content-addressed gene ids never collide; wordy enough
    # that the CPU tagger yields promoter tags (the partial-publish gate
    # checks promoter_index rows exist for every written gene).
    return (
        f"Write contention synthetic note writer {widx} upsert {i} "
        f"uid {uuid.uuid4().hex}. The tagger ingest workload records cache "
        f"invalidation notes about sqlite wal checkpoint behavior, retrieval "
        f"latency instrumentation, benchmark topology, and knowledge store "
        f"write contention while agent sessions keep querying the same "
        f"genome database file."
    )


def writer_main(args: argparse.Namespace) -> int:
    """Ingest writer: direct (own manager on the copy path) or HTTP /ingest.

    Emits one JSONL record per attempt — flushed per line so an abrupt
    TerminateProcess (arm e) loses at most the in-flight record, never the
    history. Records: {"i", "ts", "ms", "gene_ids"} on success,
    {"i", "ts", "error", "kind"} on failure (kind: busy|op_error|http|other),
    plus one leading {"warmup": true, ...} record.
    """
    stop_file = Path(args.stop_file)
    out = open(args.writer_out, "a", encoding="utf-8")

    def _emit(rec: dict) -> None:
        rec["ts"] = time.time()
        out.write(json.dumps(rec) + "\n")
        out.flush()

    if args.writer_mode == "direct":
        # The bypass topology: own CymatixContextManager on the SAME copy
        # path the server (if any) is serving. read_only deliberately NOT
        # set — KnowledgeStore defaults to a writable store, and this write
        # path never touches the server process's _write_lock.
        os.environ["CYMATIX_GENOME_PATH"] = str(args.genome_copy)
        from cymatix_context.config import load_config
        from cymatix_context.context_manager import CymatixContextManager
        cfg = load_config()
        cfg.genome.path = str(args.genome_copy)
        manager = CymatixContextManager(cfg)

        def _ingest_once(text: str, src: str) -> list:
            return manager.ingest(text, "text", {"source_id": src})
    else:  # http
        base = args.server_url.rstrip("/")

        def _ingest_once(text: str, src: str, _timeout=[HTTP_INGEST_WARMUP_TIMEOUT]) -> list:
            resp = _post_json(base + "/ingest", {
                "content": text,
                "content_type": "text",
                "metadata": {"source_id": src},
            }, timeout=_timeout[0])
            _timeout[0] = HTTP_INGEST_TIMEOUT
            return list(resp.get("gene_ids") or [])

    # Untimed warmup upsert: loads SPLADE/BGE-M3 (direct) or warms the server
    # ingest path (http). Logged so the count gate still sees its rows.
    w0 = time.time()
    try:
        gids = _ingest_once(_synth_text(args.worker_idx, -1),
                            f"wc/w{args.worker_idx}/warmup")
        _emit({"warmup": True, "i": -1, "ms": round((time.time() - w0) * 1000, 1),
               "gene_ids": gids})
    except Exception as exc:  # noqa: BLE001
        _emit({"warmup": True, "i": -1, "error": repr(exc), "kind": "other"})
        out.close()
        print(f"writer {args.worker_idx}: warmup ingest failed: {exc!r}",
              file=sys.stderr)
        return 4

    sync = Path(args.sync_dir)
    (sync / f"ready_w{args.worker_idx}").write_text("ready", encoding="utf-8")
    if not _await_go(sync, f"writer {args.worker_idx}"):
        out.close()
        return 3

    t_go = time.time()
    deadline = t_go + args.duration if args.duration > 0 else None
    i = 0
    while True:
        if stop_file.exists():
            break
        if deadline is not None and time.time() > deadline:
            break
        text = _synth_text(args.worker_idx, i)
        src = f"wc/w{args.worker_idx}/{i}"
        q0 = time.time()
        try:
            gids = _ingest_once(text, src)
            _emit({"i": i, "ms": round((time.time() - q0) * 1000, 1),
                   "gene_ids": gids})
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            kind = "busy" if ("locked" in msg or "busy" in msg) else "op_error"
            _emit({"i": i, "error": repr(exc), "kind": kind})
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            low = detail.lower()
            kind = "busy" if ("locked" in low or "busy" in low) else "http"
            _emit({"i": i, "error": f"HTTP {exc.code}: {detail[:300]}",
                   "kind": kind})
        except Exception as exc:  # noqa: BLE001 - a failing upsert is a datapoint
            _emit({"i": i, "error": repr(exc), "kind": "other"})
        i += 1

    _emit({"done": True, "i": i})
    out.close()
    return 0


# --------------------------------------------------------------- samplers ---

class _WalSampler(threading.Thread):
    """Sample os.stat(copy + '-wal').st_size every 5s; tolerate missing."""

    def __init__(self, copy: Path):
        super().__init__(daemon=True)
        self.wal = Path(str(copy) + "-wal")
        self.samples: list[int] = []
        self._stop = threading.Event()

    def _sample(self) -> None:
        try:
            self.samples.append(os.stat(self.wal).st_size)
        except OSError:
            self.samples.append(0)  # missing file == 0 bytes of WAL

    def run(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(WAL_SAMPLE_INTERVAL)

    def stop(self) -> dict:
        self._stop.set()
        self.join(timeout=WAL_SAMPLE_INTERVAL + 2)
        self._sample()  # final in-run reading
        return {
            "max_bytes": max(self.samples) if self.samples else 0,
            "final_bytes": self.samples[-1] if self.samples else 0,
            "n_samples": len(self.samples),
            "interval_s": WAL_SAMPLE_INTERVAL,
            "series_bytes": self.samples,
        }


class _CanarySampler(threading.Thread):
    """Poll GET /debug/pipeline/recent?limit=1 to expose event-loop stalls.

    NOT /health — on this branch /health may probe the upstream and pollute
    the canary with upstream latency.
    """

    def __init__(self, base_url: str):
        super().__init__(daemon=True)
        self.url = base_url.rstrip("/") + "/debug/pipeline/recent?limit=1"
        self.lat_ms: list[float] = []
        self.errors = 0
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                _get_json(self.url, CANARY_TIMEOUT)
                self.lat_ms.append(round((time.time() - t0) * 1000, 1))
            except Exception:
                self.errors += 1
            self._stop.wait(CANARY_SAMPLE_INTERVAL)

    def stop(self) -> dict:
        self._stop.set()
        self.join(timeout=CANARY_TIMEOUT + 2)
        if not self.lat_ms:
            return {"n": 0, "errors": self.errors}
        return {
            "n": len(self.lat_ms),
            "errors": self.errors,
            "p50_ms": _percentile(self.lat_ms, 0.5),
            "p95_ms": _percentile(self.lat_ms, 0.95),
            "max_ms": max(self.lat_ms),
            "interval_s": CANARY_SAMPLE_INTERVAL,
        }


# ------------------------------------------------------------- bed copies ---

def _fresh_copy(src: Path, arm_dir: Path) -> Path:
    """Copy genome.db (+ -wal/-shm sidecars if present) into the arm dir.

    The copy's WAL is TRUNCATE-checkpointed so baseline counts see all
    committed frames and the WAL-size series starts from ~0. The original is
    only ever read.
    """
    if not src.is_file():
        raise FileNotFoundError(
            f"bed genome not found: {src} — the worktree does not contain the "
            "gitignored bed; pass --genome with the absolute bed path")
    dst = arm_dir / "genome.db"
    if dst.resolve() == src.resolve():
        raise RuntimeError(f"refusing to run: copy path equals source ({src})")
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(dst) + suffix))
    con = sqlite3.connect(str(dst), timeout=30.0)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.commit()
    finally:
        con.close()
    return dst


def _cleanup_copy(copy: Path) -> None:
    for p in (copy, Path(str(copy) + "-wal"), Path(str(copy) + "-shm")):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            print(f"  warn: could not delete {p}", file=sys.stderr)


def _counts(db: Path) -> dict:
    """Read-only row counts. Verifies the DDL-derived table names at runtime
    (genes / promoter_index / genes_fts from cymatix_context/storage/ddl.py)
    instead of trusting them blind."""
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30.0)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for required in ("genes", "promoter_index", "genes_fts"):
            if required not in names:
                raise RuntimeError(
                    f"{db}: expected table {required!r} not in schema "
                    f"(sqlite_master has {sorted(names)[:12]}...) — bed/DDL drift")
        return {
            "genes": con.execute("SELECT COUNT(*) FROM genes").fetchone()[0],
            "genes_fts": con.execute("SELECT COUNT(*) FROM genes_fts").fetchone()[0],
        }
    finally:
        con.close()


# ------------------------------------------------------------ correctness ---

def _correctness_block(copy: Path, baseline: dict, reported_ids: list[str],
                       allow_unreported: bool) -> dict:
    """Post-arm integrity + zero-loss + full-publish gates (read-only)."""
    con = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True, timeout=60.0)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        final = {
            "genes": con.execute("SELECT COUNT(*) FROM genes").fetchone()[0],
            "genes_fts": con.execute("SELECT COUNT(*) FROM genes_fts").fetchone()[0],
        }
        uniq = list(dict.fromkeys(reported_ids))
        missing_gene, missing_prom, missing_fts = [], [], []
        for gid in uniq:
            if con.execute("SELECT 1 FROM genes WHERE gene_id=? LIMIT 1",
                           (gid,)).fetchone() is None:
                missing_gene.append(gid)
            if con.execute("SELECT 1 FROM promoter_index WHERE gene_id=? LIMIT 1",
                           (gid,)).fetchone() is None:
                missing_prom.append(gid)
            if con.execute("SELECT 1 FROM genes_fts WHERE gene_id=? LIMIT 1",
                           (gid,)).fetchone() is None:
                missing_fts.append(gid)
    finally:
        con.close()

    genes_delta = final["genes"] - baseline["genes"]
    fts_delta = final["genes_fts"] - baseline["genes_fts"]
    unreported = genes_delta - len(uniq)
    if allow_unreported:
        # Arm e: the killed writer may have committed an upsert whose JSONL
        # line never flushed — committed-but-unreported is NOT a lost write.
        zero_lost = unreported >= 0 and not missing_gene
    else:
        zero_lost = unreported == 0 and not missing_gene
    zero_partial = (not missing_prom and not missing_fts
                    and fts_delta == genes_delta)
    return {
        "integrity_check": integrity,
        "integrity_ok": integrity == "ok",
        "baseline_genes": baseline["genes"],
        "final_genes": final["genes"],
        "baseline_fts": baseline["genes_fts"],
        "final_fts": final["genes_fts"],
        "reported_new_genes": len(uniq),
        "genes_delta": genes_delta,
        "fts_delta": fts_delta,
        "fts_delta_matches": fts_delta == genes_delta,
        "unreported_committed": unreported,
        "missing_gene_rows": missing_gene[:20],
        "missing_promoter_rows": missing_prom[:20],
        "missing_fts_rows": missing_fts[:20],
        "missing_counts": {"gene": len(missing_gene), "promoter": len(missing_prom),
                           "fts": len(missing_fts)},
        "zero_lost_writes": zero_lost,
        "zero_partial_publish": zero_partial,
    }


# ------------------------------------------------------- worker aggregation -

def _aggregate_readers(outs: list[Path], k: int) -> dict:
    workers = [json.loads(o.read_text(encoding="utf-8")) for o in outs]
    ok = [s for w in workers for s in w["samples"] if "latency_ms" in s]
    errors = [s for w in workers for s in w["samples"] if "error" in s]
    if not ok:
        raise RuntimeError(f"no successful reader samples ({len(errors)} errors)")
    lat = [s["latency_ms"] for s in ok]
    ranks = [s["rank"] for s in ok]
    wall = max(w["t_end"] for w in workers) - min(w["t_start"] for w in workers)
    n = len(workers)
    overlap = ((min(w["t_end"] for w in workers) - max(w["t_start"] for w in workers))
               / wall if n > 1 and wall > 0 else 1.0)
    return {
        "samples": len(ok),
        "errors": len(errors),
        "error_preview": [e["error"][:200] for e in errors[:3]],
        "median_latency_ms": round(statistics.median(lat), 1),
        "mean_latency_ms": round(statistics.mean(lat), 1),
        "p95_latency_ms": round(_percentile(lat, 0.95), 1),
        "max_latency_ms": round(max(lat), 1),
        "per_worker_median_ms": [round(statistics.median(
            [s["latency_ms"] for s in w["samples"] if "latency_ms" in s]), 1)
            for w in workers if any("latency_ms" in s for s in w["samples"])],
        "k": k,
        "recall_at_k": round(sum(1 for r in ranks if r and r <= k) / len(ranks), 4),
        "median_citations": statistics.median(
            [s.get("citations", 0) for s in ok]),
        "wall_seconds": round(wall, 1),
        "overlap_fraction": round(max(0.0, overlap), 3),
        "throughput_qps": round(len(ok) / wall, 2) if wall > 0 else None,
    }


def _aggregate_writer(jsonl: Path, widx: int, mode: str, go_ts: float,
                      rc, killed: bool) -> dict:
    entries = []
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # torn final line after a kill — expected in arm e
    warmup = [e for e in entries if e.get("warmup")]
    timed = [e for e in entries if not e.get("warmup") and not e.get("done")]
    ok = [e for e in timed if "ms" in e]
    busy = [e for e in timed if e.get("kind") == "busy"]
    other = [e for e in timed if "error" in e and e.get("kind") != "busy"]
    series = [e["ms"] for e in ok]  # order preserved == upsert order
    gene_ids = [g for e in (warmup + ok) for g in (e.get("gene_ids") or [])]
    top5 = sorted(sorted(range(len(series)), key=lambda i: series[i],
                         reverse=True)[:5])
    window = (ok[-1]["ts"] - go_ts) if ok else 0.0
    genes_per_s = round(sum(len(e.get("gene_ids") or []) for e in ok) / window, 3) \
        if window > 0 else None
    out = {
        "writer": widx,
        "mode": mode,
        "upserts_ok": len(ok),
        "warmup_upserts_ok": sum(1 for e in warmup if "ms" in e),
        "busy_error_count": len(busy),
        "other_error_count": len(other),
        "error_preview": [e["error"][:200] for e in (busy + other)[:3]],
        "reported_gene_ids": len(gene_ids),
        "genes_per_s": genes_per_s,
        "active_window_s": round(window, 1),
        "rc": rc,
        "killed": killed,
    }
    if series:
        out["upsert_latency_ms"] = {
            "p50": _percentile(series, 0.5),
            "p95": _percentile(series, 0.95),
            "max": max(series),
        }
        out["upsert_latency_series_ms"] = series[:SERIES_CAP]
        out["series_truncated"] = len(series) > SERIES_CAP
        out["series_len"] = len(series)
        out["top5_outlier_indices"] = top5
    if warmup and "ms" in warmup[0]:
        out["warmup_ms"] = warmup[0]["ms"]
    return {"summary": out, "gene_ids": gene_ids}


# ------------------------------------------------------------------ arms ----

def _spawn(cmd: list[str], log_path: Path):
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=log, stderr=log,
                            creationflags=NO_WINDOW)
    return proc, log


def _run_arm(arm: str, args: argparse.Namespace, run_dir: Path) -> dict:
    arm_dir = run_dir / f"arm_{arm}"
    arm_dir.mkdir(parents=True, exist_ok=True)
    sync = arm_dir / "sync"
    sync.mkdir(exist_ok=True)
    stop_file = arm_dir / "stop"

    genome_src = REPO_ROOT / args.genome
    copy = _fresh_copy(genome_src, arm_dir)
    baseline = _counts(copy)

    with_server = arm != "d"
    n_readers = 0 if arm == "d" else args.readers
    writer_specs = {  # (count, mode)
        "a": (0, None), "b": (1, "http"), "c": (1, "direct"),
        "d": (2, "direct"), "e": (1, "direct"),
    }[arm]
    fixture_read_only = arm != "b"  # b: soft read_only makes /ingest a silent no-op
    writer_duration = args.duration
    if arm == "d" and writer_duration <= 0:
        writer_duration = 60  # no readers to bound the run; documented fallback

    srv = None
    reader_procs, reader_outs, writer_procs, writer_jsonls, logs = [], [], [], [], []
    wal_sampler = canary = None
    ring_tail = None
    kill_info = None
    self_path = str(Path(__file__).resolve())

    try:
        if with_server:
            srv = BenchServer(host=HOST, port=args.port, repo_root=REPO_ROOT,
                              log_to=arm_dir / "server.log")
            swap = srv.start(Fixture(name=f"wc-{arm}", db=str(copy),
                                     read_only=fixture_read_only))
            print(f"  server up: pid={swap.server_pid} genes={swap.genes} "
                  f"read_only={fixture_read_only}", flush=True)
            # Untimed parent warmup BEFORE any worker spawns: with a direct
            # writer in the arm, server + writer + readers cold-loading
            # encoders simultaneously starves imports (observed: the
            # transformers lazy import failing inside the server during the
            # three-process load storm). The arm measures steady-state
            # contention, not synchronized cold boot — warm first.
            for _attempt in (1, 2):
                try:
                    _post_json(f"{srv.url}/context", {
                        "query": "warmup", "session_id": f"wc-{arm}-parent-warm",
                        "ignore_delivered": True, "read_only": True,
                    }, timeout=300)
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"  parent warmup attempt {_attempt} failed: {exc!r}",
                          flush=True)
            else:
                raise RuntimeError(f"arm {arm}: server never answered warmup")

        for i in range(n_readers):
            out = arm_dir / f"reader_{i}.json"
            cmd = [sys.executable, self_path, "--reader",
                   "--worker-idx", str(i), "--worker-out", str(out),
                   "--sync-dir", str(sync), "--server-url", srv.url,
                   "--resolved", args.resolved, "--gold", args.gold,
                   "--k", str(args.k), "--rounds", str(args.rounds),
                   "--limit", str(args.limit)]
            proc, log = _spawn(cmd, arm_dir / f"reader_{i}.log")
            reader_procs.append(proc)
            reader_outs.append(out)
            logs.append(log)

        n_writers, wmode = writer_specs
        for i in range(n_writers):
            jsonl = arm_dir / f"writer_{i}.jsonl"
            cmd = [sys.executable, self_path, "--writer",
                   "--worker-idx", str(i), "--writer-out", str(jsonl),
                   "--sync-dir", str(sync), "--stop-file", str(stop_file),
                   "--genome-copy", str(copy), "--writer-mode", wmode,
                   "--duration", str(writer_duration)]
            if wmode == "http":
                cmd += ["--server-url", srv.url]
            proc, log = _spawn(cmd, arm_dir / f"writer_{i}.log")
            writer_procs.append(proc)
            writer_jsonls.append(jsonl)
            logs.append(log)

        # Barrier: every reader warmed + every writer past its warmup upsert
        # before any timed work begins.
        all_ready = ([sync / f"ready_r{i}" for i in range(n_readers)]
                     + [sync / f"ready_w{i}" for i in range(n_writers)])
        deadline = time.time() + args.level_timeout
        while not all(p.exists() for p in all_ready):
            for proc in reader_procs + writer_procs:
                if proc.poll() not in (None, 0):
                    raise RuntimeError(
                        f"arm {arm}: a worker died during warmup "
                        f"(rc={proc.returncode}) — see its .log in {arm_dir}")
            if time.time() > deadline:
                raise TimeoutError(
                    f"arm {arm}: workers not ready within {args.level_timeout}s")
            time.sleep(0.2)

        wal_sampler = _WalSampler(copy)
        wal_sampler.start()
        if with_server:
            canary = _CanarySampler(srv.url)
            canary.start()

        (sync / "go").write_text("go", encoding="utf-8")
        t_go = time.time()

        # Wait for readers (rounds-bound); arm e kills its writer mid-run.
        wait_procs = reader_procs if reader_procs else writer_procs
        while any(p.poll() is None for p in wait_procs):
            if (arm == "e" and kill_info is None
                    and time.time() - t_go >= args.kill_after
                    and writer_procs and writer_procs[0].poll() is None):
                writer_procs[0].kill()  # TerminateProcess — abrupt is the test
                kill_info = {"mode": "timer",
                             "at_s_after_go": round(time.time() - t_go, 1)}
                print(f"  arm e: writer killed {kill_info['at_s_after_go']}s "
                      f"after GO", flush=True)
            if time.time() - t_go > args.level_timeout:
                raise TimeoutError(f"arm {arm}: workers still running after "
                                   f"{args.level_timeout}s")
            time.sleep(0.5)

        if arm == "e" and kill_info is None and writer_procs:
            # Readers finished before --kill-after (smoke runs): still kill
            # abruptly rather than degrading into a clean-shutdown arm c.
            if writer_procs[0].poll() is None:
                writer_procs[0].kill()
                kill_info = {"mode": "at_readers_done",
                             "at_s_after_go": round(time.time() - t_go, 1)}
            else:
                kill_info = {"mode": "writer_exited_before_kill",
                             "at_s_after_go": None}

        # Writers stop via stop-file once readers are done (or --duration).
        stop_file.write_text("stop", encoding="utf-8")
        for proc in writer_procs:
            try:
                proc.wait(timeout=min(args.level_timeout, 300))
            except subprocess.TimeoutExpired:
                print(f"  warn: writer pid={proc.pid} ignored stop-file; killing",
                      file=sys.stderr)
                proc.kill()
                proc.wait(timeout=30)

        for proc in reader_procs:
            proc.wait(timeout=args.level_timeout)

        wal_block = wal_sampler.stop()
        wal_sampler = None
        if canary is not None:
            canary_block = canary.stop()
            canary = None
        else:
            canary_block = None

        if with_server:
            try:
                ring_tail = aggregate(fetch_recent(srv.url, limit=128))
            except Exception as exc:  # noqa: BLE001 - receipts survive ring failure
                ring_tail = {"error": repr(exc)}
    finally:
        for proc in reader_procs + writer_procs:
            if proc.poll() is None:
                proc.kill()
        for log in logs:
            log.close()
        if wal_sampler is not None:
            wal_sampler.stop()
        if canary is not None:
            canary.stop()
        if srv is not None:
            srv.stop()  # kills the whole tree (issue #127)

    # store.close() checkpoints the WAL, so record the post-close size too.
    try:
        after_close = os.stat(str(copy) + "-wal").st_size
    except OSError:
        after_close = 0
    wal_block["after_close_bytes"] = after_close

    read_latency = _aggregate_readers(reader_outs, args.k) if reader_outs else None

    writers = []
    reported_ids: list[str] = []
    n_writers, wmode = writer_specs
    for i in range(n_writers):
        killed = bool(arm == "e" and i == 0 and kill_info
                      and kill_info["mode"] != "writer_exited_before_kill")
        agg = _aggregate_writer(writer_jsonls[i], i, wmode, t_go,
                                writer_procs[i].returncode, killed)
        writers.append(agg["summary"])
        reported_ids.extend(agg["gene_ids"])

    correctness = _correctness_block(copy, baseline, reported_ids,
                                     allow_unreported=(arm == "e"))
    verdict = "PASS" if (correctness["integrity_ok"]
                         and correctness["zero_lost_writes"]
                         and correctness["zero_partial_publish"]) else "FAIL"

    if not args.keep_copies:
        _cleanup_copy(copy)

    arm_result = {
        "arm": arm,
        "label": ARM_LABELS[arm],
        "fixture_read_only": fixture_read_only if with_server else None,
        "server": with_server,
        "readers": n_readers,
        "writers_spawned": n_writers,
        "writer_mode": wmode,
        "writer_duration_s": writer_duration if n_writers else None,
        "baseline_genes": baseline["genes"],
        "read_latency": read_latency,
        "vs_control": None,  # filled by main() once arm a exists
        "writers": writers,
        "busy_error_count": sum(w["busy_error_count"] for w in writers),
        "ingest_genes_per_s": round(sum(w["genes_per_s"] or 0 for w in writers), 3)
        if writers else None,
        "wal": wal_block,
        "canary_event_loop": canary_block,
        "ring_tail_sample": ring_tail,
        "ring_tail_note": "GET /debug/pipeline/recent ring holds ~128 entries, "
                          "i.e. the last ~12 requests — a tail sample, not the arm",
        "kill": kill_info,
        "correctness": correctness,
        "verdict": verdict,
    }
    return arm_result


# ---------------------------------------------------------------- parent ----

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--genome",
                    default="F:/Projects/cymatix-context/genomes/dogfood/genome.db",
                    help="bed genome (READ ONLY — every arm runs on a fresh copy)")
    ap.add_argument("--resolved", default="benchmarks/dogfood/needles_resolved.json")
    ap.add_argument("--gold",
                    default="F:/Projects/cymatix-context/benchmarks/dogfood/"
                            "gene_ids/gold_by_needle.json")
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--rounds", type=int, default=2,
                    help="timed passes over the needle set per reader (default 2)")
    ap.add_argument("--limit", type=int, default=0,
                    help="use only the first N needles (0 = all; for smoke tests)")
    ap.add_argument("--readers", type=int, default=4,
                    help="HTTP query workers per server arm (default 4)")
    ap.add_argument("--duration", type=int, default=0,
                    help="writer run seconds; 0 = until readers finish "
                         "(arm d, which has no readers, falls back to 60s)")
    ap.add_argument("--arms", default="a,b,c,d",
                    help="comma list from a,b,c,d,e (e = kill-mid-run, opt-in)")
    ap.add_argument("--port", type=int, default=11493,
                    help="non-default port so a user server on 11437 is never hit")
    ap.add_argument("--kill-after", type=int, default=20,
                    help="arm e: seconds after GO before the writer is killed")
    ap.add_argument("--level-timeout", type=int, default=1800)
    ap.add_argument("--keep-copies", action="store_true",
                    help="keep the per-arm genome copies (~224MB each) for forensics")
    ap.add_argument("--out", default="benchmarks/dogfood/write_contention.json")
    # Internal worker-mode flags (parent spawns this same file).
    ap.add_argument("--reader", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--writer", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--worker-idx", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--worker-out", default="", help=argparse.SUPPRESS)
    ap.add_argument("--writer-out", default="", help=argparse.SUPPRESS)
    ap.add_argument("--writer-mode", default="direct", help=argparse.SUPPRESS)
    ap.add_argument("--sync-dir", default="", help=argparse.SUPPRESS)
    ap.add_argument("--server-url", default="", help=argparse.SUPPRESS)
    ap.add_argument("--stop-file", default="", help=argparse.SUPPRESS)
    ap.add_argument("--genome-copy", default="", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.reader:
        return reader_main(args)
    if args.writer:
        return writer_main(args)

    arms = [a.strip().lower() for a in args.arms.split(",") if a.strip()]
    bad = [a for a in arms if a not in VALID_ARMS]
    if bad:
        print(f"unknown arm(s) {bad}; valid: {','.join(VALID_ARMS)}",
              file=sys.stderr)
        return 2

    run_dir = REPO_ROOT / "benchmarks/dogfood/logs/write_contention" / \
        time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    summary_path = Path("F:/Projects/cymatix-context/benchmarks/dogfood/"
                        "gene_ids/summary.json")
    identity = (json.loads(summary_path.read_text(encoding="utf-8"))
                .get("identity_sha256") if summary_path.exists() else None)

    print(f"write contention: arms {arms}, readers={args.readers}, "
          f"rounds={args.rounds}, k={args.k}, port={args.port}, "
          f"bed {identity[:16] if identity else 'UNKNOWN'}\n")

    arm_results, arm_errors = [], []
    for arm in arms:
        print(f"arm {arm}: {ARM_LABELS[arm]}", flush=True)
        t0 = time.time()
        try:
            res = _run_arm(arm, args, run_dir)
        except Exception as exc:  # noqa: BLE001 - one broken arm must not eat the rest
            print(f"  arm {arm} ERROR: {exc!r}", file=sys.stderr, flush=True)
            arm_errors.append(arm)
            arm_results.append({"arm": arm, "label": ARM_LABELS[arm],
                                "verdict": "ERROR", "error": repr(exc)})
            continue
        arm_results.append(res)
        rl = res["read_latency"]
        rl_txt = (f"median {rl['median_latency_ms']}ms p95 {rl['p95_latency_ms']}ms "
                  f"r@{args.k} {rl['recall_at_k']} overlap {rl['overlap_fraction']}"
                  if rl else "no readers")
        print(f"  {rl_txt}  busy={res['busy_error_count']}  "
              f"wal_max={res['wal']['max_bytes']}  {res['verdict']}  "
              f"({time.time() - t0:.0f}s incl. copy+warmup)", flush=True)

    control = next((r for r in arm_results
                    if r.get("arm") == "a" and r.get("read_latency")), None)
    for r in arm_results:
        rl = r.get("read_latency")
        if control and rl and r["arm"] != "a":
            base = control["read_latency"]
            r["vs_control"] = {
                "median_latency_x": round(
                    rl["median_latency_ms"] / base["median_latency_ms"], 2),
                "p95_latency_x": round(
                    rl["p95_latency_ms"] / base["p95_latency_ms"], 2),
                "throughput_x": round(
                    rl["throughput_qps"] / base["throughput_qps"], 2)
                if base["throughput_qps"] and rl["throughput_qps"] else None,
            }

    payload = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "genome_source": str(REPO_ROOT / args.genome),
        "bed_identity_sha256": identity,
        "k": args.k,
        "rounds": args.rounds,
        "readers": args.readers,
        "needles": args.limit or "all",
        "duration_s": args.duration,
        "kill_after_s": args.kill_after,
        "port": args.port,
        "cpu_count": os.cpu_count(),
        "worker_model": "fresh bed copy per arm; readers = subprocess HTTP "
                        "workers on POST /context (read_only, ignore_delivered); "
                        "writers = subprocess /ingest (http) or direct-open "
                        "CymatixContextManager.ingest bypassing the server "
                        "_write_lock (the tagger topology)",
        "note": "warmups untimed (incl. writer encoder load, logged+counted); "
                "barrier-synced; ranks recorded via agent.citations as a "
                "fast-because-broken guard; per-upsert series order-preserved "
                "so WAL-checkpoint spikes at #50/#500 stay visible; busy "
                "errors imply 30s starvation (busy_timeout=30000); hard gates "
                "zero_lost_writes / zero_partial_publish / integrity_ok",
        "control_arm": "a",
        "arms": arm_results,
        "worker_logs": str(run_dir),
    }
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print("\nverdicts: " + "   ".join(
        f"{r.get('arm')}={r.get('verdict')}" for r in arm_results))
    deltas = [r for r in arm_results if r.get("vs_control")]
    if deltas:
        print("read latency vs control: " + "   ".join(
            f"{r['arm']}: {r['vs_control']['median_latency_x']}x median, "
            f"{r['vs_control']['p95_latency_x']}x p95" for r in deltas))
    print(f"\nwrote {out}")
    return 1 if arm_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

r"""Four-run SPLADE pre-filter ablation on EnterpriseRAG-Bench Onyx.

Issue #159 Wall-2 lever, PRD docs/prds/2026-05-26-splade-prefilter-dense-recall.md §5.

Variants:
  T. Today (baseline)          — splade_enabled=true,  dense_prefilter_enabled=false
  A. SPLADE off                — splade_enabled=false, dense_prefilter_enabled=false
  B. Prefilter on, escape=0    — splade_enabled=true,  dense_prefilter_enabled=true, escape=0
  C. Prefilter on, escape=250  — splade_enabled=true,  dense_prefilter_enabled=true, escape=250

Per variant: patches helix.toml to a temp file, starts the daemon against
the supplied fixture, runs the EnterpriseRAG questions through /fingerprint
with per-query wall-clock timing, captures recall@1/3/5/10 + MRR +
p50/p95/p99 latency. Writes per-variant JSON + a combined comparison table.

Usage:
  python benchmarks/ablate_dense_prefilter.py --fixture <db_path> --max-questions 100
  python benchmarks/ablate_dense_prefilter.py --fixture <db_path> --max-questions 10 --label smoke
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import httpx

import re

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_enterprise_rag import load_needles, _rel_after_sources  # noqa: E402

WORKTREE = Path(__file__).resolve().parent.parent
BASE_CONFIG = WORKTREE / "helix.toml"
HEALTH_URL = "http://127.0.0.1:11437/health"
FINGERPRINT_URL = "http://127.0.0.1:11437/fingerprint"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RESULTS_DIR = WORKTREE / "benchmarks" / "results" / "ablate_dense_prefilter"


VARIANTS: list[tuple[str, str, dict]] = [
    (
        "T",
        "Today (baseline)",
        {},  # No patch — current helix.toml
    ),
    (
        "A",
        "SPLADE off",
        {"ingestion": {"splade_enabled": False}},
    ),
    (
        "B",
        "Prefilter on, escape=0",
        {"retrieval": {
            "dense_prefilter_enabled": True,
            "dense_prefilter_escape_budget": 0,
        }},
    ),
    (
        "C",
        "Prefilter on, escape=250",
        {"retrieval": {
            "dense_prefilter_enabled": True,
            "dense_prefilter_escape_budget": 250,
        }},
    ),
]


_SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return f'"{v}"'
    raise ValueError(f"unsupported TOML value type for ablation patch: {type(v)} {v!r}")


def write_patched_toml(patch: dict[str, dict], dest: Path) -> None:
    """Apply a {section: {key: value}} patch by line-level replacement on the
    base helix.toml. Keys must already exist in the section (we only flip
    existing values for the ablation — no append semantics needed).
    """
    text = BASE_CONFIG.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    # Group: walk lines, track current section, replace matching keys.
    current_section: str | None = None
    out: list[str] = []
    pending = {sec: dict(kvs) for sec, kvs in patch.items()}
    for line in lines:
        m = _SECTION_RE.match(line.strip())
        if m:
            current_section = m.group(1)
            out.append(line)
            continue
        if current_section in pending and pending[current_section]:
            stripped = line.lstrip()
            for key in list(pending[current_section]):
                # Match "key = ..." possibly with leading whitespace and a
                # trailing comment. Be tolerant of spacing variants.
                kre = re.compile(rf"^(\s*){re.escape(key)}\s*=\s*[^#\n]*(\s*#.*)?$")
                if kre.match(line):
                    new_val = _toml_value(pending[current_section][key])
                    indent_m = re.match(r"^(\s*)", line)
                    indent = indent_m.group(1) if indent_m else ""
                    comment_m = re.search(r"(\s*#.*)$", line)
                    comment = comment_m.group(1) if comment_m else ""
                    out.append(f"{indent}{key} = {new_val}{comment}\n")
                    pending[current_section].pop(key)
                    break
            else:
                out.append(line)
            continue
        out.append(line)

    leftover = {s: ks for s, ks in pending.items() if ks}
    if leftover:
        raise RuntimeError(
            f"patch keys not found in base helix.toml: {leftover}. "
            "Add them to helix.toml first or extend the patcher."
        )
    dest.write_text("".join(out), encoding="utf-8")


def kill_existing_helix() -> None:
    try:
        subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'uvicorn helix_context._asgi' "
                "-and $_.ProcessId -ne $PID } | ForEach-Object { "
                "Stop-Process -Id $_.ProcessId -Force }",
            ],
            check=False, capture_output=True, timeout=15,
            creationflags=NO_WINDOW,
        )
    except Exception:
        pass


def wait_for_port_free(port: int = 11437, timeout_s: float = 30) -> bool:
    """After kill, the OS may take a few seconds to release the listen socket.
    Poll until nothing is listening on the port, or timeout."""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        try:
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-NetTCPConnection -State Listen -LocalPort {port} "
                 f"-ErrorAction SilentlyContinue | Measure-Object).Count"],
                capture_output=True, text=True, timeout=5,
                creationflags=NO_WINDOW,
            )
            if r.returncode == 0 and r.stdout.strip() == "0":
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def wait_for_ready(
    proc: subprocess.Popen, timeout_s: float = 240,
) -> tuple[int, dict | None]:
    """Poll /health via httpx until genes>0. Aborts early if the daemon
    process exits (poll() not None means dead).
    """
    deadline = time.perf_counter() + timeout_s
    last = None
    client = httpx.Client(timeout=8)
    try:
        while time.perf_counter() < deadline:
            if proc.poll() is not None:
                return 0, {"error": f"daemon process exited rc={proc.returncode}"}
            try:
                r = client.get(HEALTH_URL)
                if r.status_code == 200:
                    data = r.json()
                    last = data
                    if data.get("genes", 0) > 0:
                        return data["genes"], data
            except Exception:
                pass
            time.sleep(2)
    finally:
        client.close()
    return 0, last


def warmup_daemon(n: int = 2, timeout_s: float = 300.0) -> None:
    """Fire 2 throwaway /fingerprint calls so subsequent timings exclude
    first-query model warmup (BGE-M3 + SPLADE GPU load on first hit). At
    105-shard scale the first query also lazy-loads dense matrices across
    every routed shard, which can take minutes — hence the long timeout."""
    client = httpx.Client(timeout=timeout_s)
    try:
        for _ in range(n):
            try:
                client.post(FINGERPRINT_URL, json={
                    "query": "warmup query for the helix daemon",
                    "max_results": 10, "score_floor": 0.0,
                })
            except Exception:
                pass
    finally:
        client.close()


def start_helix(
    config_path: Path, fixture: Path, boot_log: Path, sharded: bool = False,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["HELIX_CONFIG"] = str(config_path)
    env["HELIX_GENOME_PATH"] = str(fixture)
    if sharded:
        env["HELIX_USE_SHARDS"] = "1"
    else:
        env.pop("HELIX_USE_SHARDS", None)
    boot_log.parent.mkdir(parents=True, exist_ok=True)
    log_fh = boot_log.open("a", encoding="utf-8")
    log_fh.write(f"\n\n=== boot at {datetime.now(timezone.utc).isoformat()} "
                 f"config={config_path.name} fixture={fixture.name} "
                 f"sharded={sharded} ===\n\n")
    log_fh.flush()
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "helix_context._asgi:app",
         "--host", "127.0.0.1", "--port", "11437"],
        cwd=str(WORKTREE), env=env,
        stdout=log_fh, stderr=subprocess.STDOUT,
        creationflags=NO_WINDOW,
    )


def run_recall_bench(
    needles: list[dict], k: int, proc: subprocess.Popen | None = None,
    query_timeout_s: float = 30,
) -> dict:
    """Hit /fingerprint per needle, record rank + per-query latency.

    Aborts cleanly if the daemon process exits mid-bench (OOM, crash). The
    remaining needles are recorded as None ranks (missed) so the metrics
    still compute, but a daemon_died flag is set so the caller can report.
    """
    ranks: list[int | None] = []
    latencies_ms: list[float] = []
    rows = []
    daemon_died_at: int | None = None
    client = httpx.Client(timeout=query_timeout_s)
    try:
        for i, n in enumerate(needles, 1):
            gold_rels = {_rel_after_sources(p) for p in n["gold_paths"]}
            gold_rels = {r for r in gold_rels if r}
            t0 = time.perf_counter()
            try:
                resp = client.post(FINGERPRINT_URL, json={
                    "query": n["question"], "max_results": k, "score_floor": 0.0,
                })
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                fps = resp.json().get("fingerprints", [])
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                print(f"    [{i}] {n['id']} ERROR {exc}")
                ranks.append(None)
                latencies_ms.append(elapsed_ms)
                # Did the daemon crash? Check immediately so we abort fast.
                if proc is not None and proc.poll() is not None:
                    daemon_died_at = i
                    print(f"    [{i}] daemon process exited rc={proc.returncode}; "
                          f"aborting variant after {i}/{len(needles)} queries")
                    break
                continue
            hit_rank = None
            for fp in fps:
                rel = _rel_after_sources(fp.get("source", "")) or ""
                if rel in gold_rels:
                    hit_rank = fp["rank"]
                    break
            ranks.append(hit_rank)
            latencies_ms.append(elapsed_ms)
            rows.append({"id": n["id"], "type": n["type"],
                         "hit_rank": hit_rank, "latency_ms": round(elapsed_ms, 1),
                         "n_returned": len(fps), "n_gold": len(gold_rels)})
            if i % 25 == 0:
                print(f"    [{i}/{len(needles)}] ranks={hit_rank} latency_ms={elapsed_ms:.0f}")
    finally:
        client.close()

    def recall_at(kk: int) -> float:
        hits = sum(1 for r in ranks if r is not None and r < kk)
        return hits / len(ranks) * 100 if ranks else 0.0

    mrr = (sum(1.0 / (r + 1) for r in ranks if r is not None) / len(ranks)
           if ranks else 0.0)
    latencies_sorted = sorted(latencies_ms)
    n_lat = len(latencies_sorted)

    def pct(p: float) -> float:
        if n_lat == 0:
            return 0.0
        idx = max(0, min(n_lat - 1, int(round(p / 100 * (n_lat - 1)))))
        return latencies_sorted[idx]

    return {
        "n_questions": len(ranks),
        "k": k,
        "recall@1": recall_at(1),
        "recall@3": recall_at(3),
        "recall@5": recall_at(5),
        "recall@10": recall_at(10),
        "mrr": mrr,
        "missed": sum(1 for r in ranks if r is None),
        "latency_ms_p50": pct(50),
        "latency_ms_p95": pct(95),
        "latency_ms_p99": pct(99),
        "latency_ms_mean": statistics.fmean(latencies_ms) if latencies_ms else 0.0,
        "daemon_died_at": daemon_died_at,
        "rows": rows,
    }


def run_variant(
    code: str, label: str, patch: dict, fixture: Path, needles: list[dict],
    k: int, run_label: str, sharded: bool, boot_timeout_s: float,
    query_timeout_s: float,
) -> dict:
    print(f"\n{'='*72}\n=== Variant {code}: {label}\n{'='*72}")
    print(f"  patch: {patch}")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".toml", delete=False, encoding="utf-8",
    ) as tmp:
        tmp_path = Path(tmp.name)
    write_patched_toml(patch, tmp_path)

    print(f"  patched config: {tmp_path}")
    kill_existing_helix()
    print(f"  waiting for port 11437 to be released ...")
    if not wait_for_port_free(11437, timeout_s=30):
        print(f"  WARN: port 11437 still listening after 30s; continuing anyway")
    time.sleep(3)

    boot_log = Path(rf"F:/tmp/helix_ablate_{run_label}_{code}.log")
    proc = start_helix(tmp_path, fixture, boot_log, sharded=sharded)
    print(f"  spawned uvicorn pid={proc.pid}; log={boot_log}; sharded={sharded}")

    print(f"  waiting for /health (up to {boot_timeout_s:.0f}s) ...")
    genes, health = wait_for_ready(proc, timeout_s=boot_timeout_s)
    if genes <= 0:
        print(f"  ERROR: daemon never reached genes>0. last_health={health}")
        try: proc.terminate()
        except Exception: pass
        tmp_path.unlink(missing_ok=True)
        return {"variant": code, "label": label, "error": "daemon-not-ready", "health": health}

    print(f"  daemon ready: genes={genes}; warming up (2 throwaway queries) ...")
    warmup_daemon(n=2)
    if proc.poll() is not None:
        print(f"  ERROR: daemon died during warmup rc={proc.returncode}")
        tmp_path.unlink(missing_ok=True)
        return {"variant": code, "label": label, "error": "daemon-died-during-warmup",
                "returncode": proc.returncode}

    print(f"  running recall bench, n={len(needles)}, k={k} ...")
    t_bench = time.perf_counter()
    metrics = run_recall_bench(needles, k, proc=proc, query_timeout_s=query_timeout_s)
    bench_wall_s = time.perf_counter() - t_bench
    metrics["variant"] = code
    metrics["label"] = label
    metrics["patch"] = patch
    metrics["bench_wall_s"] = round(bench_wall_s, 1)
    metrics["genes_loaded"] = genes

    died_msg = ""
    if metrics.get("daemon_died_at") is not None:
        died_msg = f"  *** DAEMON DIED at query {metrics['daemon_died_at']} ***"
    print(f"  recall@10={metrics['recall@10']:.1f}%  "
          f"p95={metrics['latency_ms_p95']:.0f}ms  "
          f"bench_wall={bench_wall_s:.1f}s{died_msg}")

    kill_existing_helix()
    wait_for_port_free(11437, timeout_s=30)
    tmp_path.unlink(missing_ok=True)
    time.sleep(5)
    return metrics


def print_comparison_table(results: list[dict]) -> None:
    print(f"\n{'='*88}")
    print(f"=== ABLATION RESULTS — Issue #159 Wall-2 SPLADE pre-filter")
    print(f"{'='*88}")
    print(f"{'Code':<5} {'Label':<28} {'R@1':>6} {'R@3':>6} {'R@5':>6} {'R@10':>6} "
          f"{'MRR':>6} {'p50ms':>7} {'p95ms':>7}")
    print("-" * 88)
    for r in results:
        if "error" in r:
            print(f"{r['variant']:<5} {r['label']:<28} ERROR: {r['error']}")
            continue
        print(f"{r['variant']:<5} {r['label']:<28} "
              f"{r['recall@1']:>5.1f}% {r['recall@3']:>5.1f}% "
              f"{r['recall@5']:>5.1f}% {r['recall@10']:>5.1f}% "
              f"{r['mrr']:>6.3f} {r['latency_ms_p50']:>7.0f} "
              f"{r['latency_ms_p95']:>7.0f}")

    # Deltas vs T
    by_code = {r["variant"]: r for r in results if "error" not in r}
    if "T" in by_code:
        T = by_code["T"]
        print(f"\n=== Deltas vs T (recall@10 / p95 latency) ===")
        for code in ("A", "B", "C"):
            if code in by_code:
                v = by_code[code]
                d_r10 = v["recall@10"] - T["recall@10"]
                d_p95 = v["latency_ms_p95"] - T["latency_ms_p95"]
                print(f"  {code} vs T:  recall@10  {d_r10:+.1f}pp     "
                      f"p95  {d_p95:+.0f}ms")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", required=True, type=Path)
    ap.add_argument("--max-questions", type=int, default=100)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--types", default="basic,semantic,intra_document_reasoning",
                    help="comma-separated question types")
    ap.add_argument("--label", default="full",
                    help="label suffix for result files (e.g., 'smoke', 'full')")
    ap.add_argument("--variants", default="T,A,B,C",
                    help="comma-separated variant codes to run (default all)")
    ap.add_argument("--sharded", action="store_true",
                    help="fixture is a sharded main.genome.db (sets HELIX_USE_SHARDS=1)")
    ap.add_argument("--boot-timeout", type=float, default=240,
                    help="seconds to wait for /health; bump for sharded fixtures")
    ap.add_argument("--query-timeout", type=float, default=30,
                    help="per-/fingerprint timeout in seconds")
    args = ap.parse_args()

    if not args.fixture.exists():
        print(f"ERROR: fixture not found: {args.fixture}", file=sys.stderr)
        return 2

    types = [t.strip() for t in args.types.split(",")] if args.types else None
    needles = load_needles(max_questions=args.max_questions, question_types=types)
    print(f"loaded {len(needles)} needles from EnterpriseRAG-Bench questions.jsonl")
    print(f"types: {types}")
    print(f"fixture: {args.fixture}")

    selected = set(args.variants.split(","))
    chosen = [(c, l, p) for c, l, p in VARIANTS if c in selected]
    print(f"variants: {[c for c, _, _ in chosen]}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    run_label = f"{args.label}_{ts}"

    results: list[dict] = []
    for code, label, patch in chosen:
        result = run_variant(code, label, patch, args.fixture, needles,
                             args.k, run_label, sharded=args.sharded,
                             boot_timeout_s=args.boot_timeout,
                             query_timeout_s=args.query_timeout)
        results.append(result)
        # Per-variant artifact (in case full run is interrupted)
        per_path = RESULTS_DIR / f"variant_{code}_{run_label}.json"
        per_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print_comparison_table(results)

    summary_path = RESULTS_DIR / f"ablation_{run_label}.json"
    summary_path.write_text(json.dumps({
        "fixture": str(args.fixture),
        "max_questions": args.max_questions,
        "types": types,
        "k": args.k,
        "ts": ts,
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nwritten: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""P8 shard A/B receipt driver (#222 fetch-factor, #223 coact-reserve).

Serialized cells: launch uvicorn with env knobs -> health gate -> bench_shard_recall
-> hard-stop server -> verify port free -> next cell. Canonical beds are only ever
served with HELIX_DISABLE_LEARN=1 and compact_interval=0 (read-only serving).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

REPO = "F:/Projects/helix-context/.claude/worktrees/overnight"
MAIN = "F:/Projects/helix-context"
PORT = 11438
URL = f"http://127.0.0.1:{PORT}"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

CELLS = []
for bed in ("medium", "xl"):
    CELLS += [
        (bed, f"{bed}_sharded_f2_c0", True, "2", "0"),   # base cell (defaults)
        (bed, f"{bed}_sharded_f4_c0", True, "4", "0"),   # 222 fetch receipt
        (bed, f"{bed}_sharded_f2_c2", True, "2", "2"),   # 223 coact N=2
        (bed, f"{bed}_sharded_f2_c4", True, "2", "4"),   # 223 coact N=4
        (bed, f"{bed}_unsharded_ref", False, None, None),  # blob ceiling reference
    ]


def health_up(timeout_s=5):
    try:
        urllib.request.urlopen(URL + "/health", timeout=timeout_s)
        return True
    except Exception:
        return False


def wait_health(timeout=420):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if health_up():
            return True
        time.sleep(3)
    return False


def wait_port_free(timeout=90):
    deadline = time.time() + timeout
    misses = 0
    while time.time() < deadline:
        if health_up(timeout_s=3):
            misses = 0
        else:
            misses += 1
            if misses >= 2:
                return True
        time.sleep(2)
    return False


def main():
    if health_up():
        print("ABORT: something already answers on the receipt port", flush=True)
        sys.exit(2)
    summary = {}
    for bed, label, sharded, fetch, coact in CELLS:
        env = dict(os.environ)
        env["HELIX_DISABLE_LEARN"] = "1"
        for k in ("HELIX_USE_SHARDS", "HELIX_SHARD_FETCH_FACTOR",
                  "HELIX_SHARD_COACT_RESERVE", "HELIX_OTEL_ENABLED"):
            env.pop(k, None)
        if sharded:
            env["HELIX_USE_SHARDS"] = "1"
            env["HELIX_CONFIG"] = f"F:/tmp/diag_{bed}_sharded.toml"
            env["HELIX_GENOME_PATH"] = (
                f"{MAIN}/genomes/bench/matrix-sharded/{bed}/main.genome.db")
            env["HELIX_SHARD_FETCH_FACTOR"] = fetch
            env["HELIX_SHARD_COACT_RESERVE"] = coact
        else:
            env["HELIX_CONFIG"] = f"F:/tmp/diag_{bed}_unsharded.toml"
            env["HELIX_GENOME_PATH"] = f"{MAIN}/genomes/bench/matrix/{bed}.db"
        logf = open(f"F:/tmp/overnight/p8_server_{label}.log", "w", encoding="utf-8")
        srv = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "helix_context._asgi:app",
             "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=REPO, env=env, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW)
        if not wait_health():
            print(f"[{label}] SERVER FAILED health gate", flush=True)
            srv.kill()
            srv.wait()
            logf.close()
            summary[label] = "SERVER_FAILED"
            continue
        out = f"benchmarks/results/shard_receipt_{label}.json"
        t0 = time.time()
        rc = subprocess.call(
            [sys.executable, "benchmarks/bench_shard_recall.py",
             "--needles", f"{MAIN}/benchmarks/results/shard_gold_{bed}.jsonl",
             "--helix-url", URL, "--label", label, "--out", out],
            cwd=REPO, creationflags=CREATE_NO_WINDOW)
        dt = time.time() - t0
        print(f"[{label}] bench rc={rc} in {dt:.0f}s", flush=True)
        summary[label] = f"rc={rc} {dt:.0f}s"
        srv.terminate()
        try:
            srv.wait(timeout=30)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait()
        logf.close()
        if not wait_port_free():
            print(f"ABORT after [{label}]: port never freed - refusing to "
                  f"contaminate the next cell", flush=True)
            summary["__abort__"] = f"port stuck after {label}"
            break
    print("SUMMARY " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()

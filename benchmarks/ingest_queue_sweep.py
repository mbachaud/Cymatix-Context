r"""Serial ingest queue for the chunk-width sweep on the 10K EnterpriseRAG corpus.

Waits for the in-flight width-16000 ingest to finish (by detecting its "DONE in"
log marker -- which build_enterprise_rag_batched logs AFTER genome.close(), so
the .db is guaranteed complete and consistent), then ingests width 8000, then
width 6000, each as a blocking subprocess. Serial by design: ingestion is
CPU-bound (spaCy + SPLADE + dense on CPU), so concurrency would only contend.

Launch as a background job; it sleeps cheaply until the 16000 ingest is done,
so it adds no CPU load while waiting.

Produces, alongside the existing baseline (w4000 = enterprise_rag_10k_batched.db)
and the in-flight w16000:
    enterprise_rag_10k_w8000.db
    enterprise_rag_10k_w6000.db

Per-width logs at F:/tmp/ingest_w{8000,6000}.log; queue log to stdout.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent
PY = sys.executable
BUILD = "benchmarks/build_enterprise_rag_batched.py"
IN_DIR = "F:/tmp/enterprise_rag_10k/sources"
OUT_DIR = Path("F:/Projects/helix-context/genomes/bench/matrix")

WAIT_LOG = Path("F:/tmp/ingest_w16000.log")   # the in-flight ingest to wait on
WAIT_MARKER = "DONE in"                         # logged after genome.close()
WAIT_TIMEOUT_S = 7200
WIDTHS = [8000, 6000]                           # run in this order, after 16000


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} queue: {msg}", flush=True)


def wait_for_marker(path: Path, marker: str, timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if marker in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except FileNotFoundError:
            pass
        time.sleep(30)
    return False


def run_ingest(width: int) -> int:
    out = OUT_DIR / f"enterprise_rag_10k_w{width}.db"
    log_path = Path(f"F:/tmp/ingest_w{width}.log")
    cmd = [PY, BUILD, "--in-dir", IN_DIR, "--out", str(out),
           "--batch-size", "64", "--max-chars", str(width)]
    log(f"starting w{width} -> {out.name} (log {log_path})")
    t0 = time.time()
    with log_path.open("w", encoding="utf-8") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                            cwd=str(WORKTREE)).returncode
    log(f"w{width} finished rc={rc} in {(time.time()-t0)/60:.1f} min")
    return rc


def main() -> int:
    log(f"waiting for '{WAIT_MARKER}' in {WAIT_LOG} (timeout {WAIT_TIMEOUT_S}s)...")
    if not wait_for_marker(WAIT_LOG, WAIT_MARKER, WAIT_TIMEOUT_S):
        log("TIMEOUT waiting for w16000 ingest; aborting queue")
        return 1
    log("w16000 ingest complete; proceeding with sweep")

    rcs = {}
    for w in WIDTHS:
        rcs[w] = run_ingest(w)

    bad = [w for w, rc in rcs.items() if rc != 0]
    log(f"queue complete. results={rcs}" + (f"  FAILED={bad}" if bad else "  all OK"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

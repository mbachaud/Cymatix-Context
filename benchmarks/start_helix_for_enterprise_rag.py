r"""Start helix uvicorn pointing at a given fixture, wait for /health
to report genes > 0, then exit (helix keeps running in background).

Helper for the EnterpriseRAG bench. Adapted from run_verifier_sweep.py's
``start_helix_for_fixture()``.

Usage:
  python benchmarks/start_helix_for_enterprise_rag.py \
      --fixture F:/Projects/helix-context/genomes/bench/matrix/enterprise_rag_10k.db

To stop:
  powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" |
    Where-Object { $_.CommandLine -match 'uvicorn helix_context._asgi' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


WORKTREE = Path(__file__).resolve().parent.parent
HELIX_CONFIG = WORKTREE / "helix.toml"
HEALTH_URL = "http://127.0.0.1:11437/health"
HELIX_BOOT_LOG = Path(r"F:/tmp/helix_enterprise_rag.log")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def kill_existing() -> None:
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


def wait_for_ready(timeout_s: float = 240) -> tuple[int, dict | None]:
    deadline = time.perf_counter() + timeout_s
    last = None
    while time.perf_counter() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                last = data
                genes = data.get("genes", 0)
                if genes and genes > 0:
                    return genes, data
        except Exception:
            pass
        time.sleep(2)
    return 0, last


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--fixture", required=True, type=Path,
                        help="Path to the .db genome to load")
    parser.add_argument("--sharded", action="store_true",
                        help="Pass HELIX_USE_SHARDS=1 (fixture is the main.genome.db)")
    parser.add_argument("--timeout", type=float, default=240,
                        help="Seconds to wait for genes > 0")
    args = parser.parse_args()

    if not args.fixture.exists():
        print(f"ERROR: fixture not found: {args.fixture}", file=sys.stderr)
        return 2

    print(f"=== Starting helix for {args.fixture.name} ===")
    print(f"  HELIX_CONFIG={HELIX_CONFIG}")
    print(f"  HELIX_GENOME_PATH={args.fixture}")
    print(f"  HELIX_USE_SHARDS={'1' if args.sharded else 'unset'}")

    print("kill any existing uvicorn on port 11437...")
    kill_existing()
    time.sleep(2)

    env = os.environ.copy()
    env["HELIX_CONFIG"] = str(HELIX_CONFIG)
    env["HELIX_GENOME_PATH"] = str(args.fixture)
    if args.sharded:
        env["HELIX_USE_SHARDS"] = "1"
    else:
        env.pop("HELIX_USE_SHARDS", None)

    HELIX_BOOT_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = HELIX_BOOT_LOG.open("a", encoding="utf-8")
    log_fh.write(f"\n\n=== boot {args.fixture.name} at "
                 f"{datetime.now(timezone.utc).isoformat()} ===\n\n")
    log_fh.flush()

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "helix_context._asgi:app",
         "--host", "127.0.0.1", "--port", "11437"],
        cwd=str(WORKTREE), env=env,
        stdout=log_fh, stderr=subprocess.STDOUT,
        creationflags=NO_WINDOW,
    )
    print(f"launched uvicorn pid={proc.pid}")

    print(f"waiting (up to {args.timeout}s) for /health genes > 0...")
    genes, data = wait_for_ready(args.timeout)
    if genes <= 0:
        print(f"ERROR: helix never reached genes>0 (last={data})",
              file=sys.stderr)
        try: proc.terminate()
        except Exception: pass
        return 2

    print(f"OK  pid={proc.pid}  genes={genes}  health={data}")
    print(f"helix is running. Stop with the powershell command in the docstring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

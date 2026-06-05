r"""Start helix uvicorn pointing at a given fixture, wait for /health
to report genes > 0, then exit (helix keeps running in background).

Helper for the EnterpriseRAG bench. Adapted from run_verifier_sweep.py's
``start_helix_for_fixture()``.

Usage (dev lane stays untouched; bench rides 11439):
  python benchmarks/start_helix_for_enterprise_rag.py \
      --fixture .../enterprise_rag_onyx_full_2/main.genome.db --sharded --port 11439

To stop ONLY the bench lane (port-scoped — leaves the dev daemon alive):
  powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" |
    Where-Object { $_.CommandLine -match 'uvicorn helix_context._asgi'
      -and $_.CommandLine -match '--port 11439' } |
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
DEFAULT_PORT = 11437
HELIX_BOOT_LOG = Path(r"F:/tmp/helix_enterprise_rag.log")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def kill_existing(port: int) -> None:
    """Kill ONLY the helix uvicorn bound to *this* port.

    Previously this killed every ``uvicorn helix_context._asgi`` process
    regardless of port — which murdered a coexisting dev daemon (e.g. the
    tray on 11437) whenever the bench launched. Scoping the match to
    ``--port <port>`` lets the dev lane (11437) and bench lane (11439) run
    side by side. The ``(\\s|$)`` guard stops ``11439`` matching ``114390``.
    """
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'uvicorn helix_context._asgi' "
        "-and $_.CommandLine -match '--port __PORT__(\\s|$)' "
        "-and $_.ProcessId -ne $PID } | ForEach-Object { "
        "Stop-Process -Id $_.ProcessId -Force }"
    ).replace("__PORT__", str(port))
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            check=False, capture_output=True, timeout=15,
            creationflags=NO_WINDOW,
        )
    except Exception:
        pass


def wait_for_ready(health_url: str, timeout_s: float = 240) -> tuple[int, dict | None]:
    deadline = time.perf_counter() + timeout_s
    last = None
    last_err: str | None = None
    # /health enumerates per-shard state, so on 100-shard fixtures (v2 Onyx
    # corpus) a single response takes ~6s. A 3s urllib timeout silently
    # never succeeds — bump to 30s and log the last error so an interactive
    # operator sees what's wrong instead of staring at silent retries.
    while time.perf_counter() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                last = data
                genes = data.get("genes", 0)
                if genes and genes > 0:
                    return genes, data
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    if last_err:
        print(f"  last /health error: {last_err}", file=sys.stderr)
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
    parser.add_argument("--config", type=Path, default=None,
                        help="Override HELIX_CONFIG path (default: worktree helix.toml). "
                             "Use to point at a patched config (e.g., splade_enabled=false "
                             "for v2/100-shard fixtures that won't boot under SPLADE on).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"uvicorn bind port (default {DEFAULT_PORT}). Use 11439 for "
                             "the bench lane so it coexists with the dev daemon/tray on "
                             "11437 — the kill step is now scoped to this exact port.")
    args = parser.parse_args()
    health_url = f"http://127.0.0.1:{args.port}/health"

    if not args.fixture.exists():
        print(f"ERROR: fixture not found: {args.fixture}", file=sys.stderr)
        return 2

    config_path = args.config if args.config else HELIX_CONFIG
    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        return 2

    print(f"=== Starting helix for {args.fixture.name} ===")
    print(f"  HELIX_CONFIG={config_path}")
    print(f"  HELIX_GENOME_PATH={args.fixture}")
    print(f"  HELIX_USE_SHARDS={'1' if args.sharded else 'unset'}")
    print(f"  PORT={args.port}")

    print(f"kill any existing uvicorn on port {args.port}...")
    kill_existing(args.port)
    time.sleep(2)

    env = os.environ.copy()
    env["HELIX_CONFIG"] = str(config_path)
    env["HELIX_GENOME_PATH"] = str(args.fixture)
    if args.sharded:
        env["HELIX_USE_SHARDS"] = "1"
    else:
        env.pop("HELIX_USE_SHARDS", None)
    # Force the BGE-M3 encoder to use its LOCAL cache and never phone home to
    # the HF Hub. Without this, a fresh daemon re-checks huggingface.co for the
    # model on load; at 100-shard scale (esp. pre-A1, when each shard loaded its
    # own codec) that burst of Hub checks trips the anonymous rate limit
    # (HTTP 429, 500 req / 5 min), and every subsequent query stalls on 4s+
    # retry backoffs (~408s/query observed vs ~100s real). The model is already
    # cached from any prior run. Operator can override by pre-exporting these.
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("TRANSFORMERS_OFFLINE", "1")
    # Per-lane OTel attribution: tag this as the bench lane so its telemetry
    # splits from a coexisting dev daemon in Prometheus/Tempo/Grafana. Operator
    # env wins (setdefault); instance_id always reflects the bound port. See
    # telemetry/otel.py + docs/prds/2026-06-02-otel-per-lane-attribution.md.
    env.setdefault("HELIX_OTEL_SERVICE_NAME", "helix-bench")
    env.setdefault("HELIX_LANE", "bench")
    env["HELIX_OTEL_INSTANCE_ID"] = f"127.0.0.1:{args.port}"

    HELIX_BOOT_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = HELIX_BOOT_LOG.open("a", encoding="utf-8")
    log_fh.write(f"\n\n=== boot {args.fixture.name} at "
                 f"{datetime.now(timezone.utc).isoformat()} ===\n\n")
    log_fh.flush()

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "helix_context._asgi:app",
         "--host", "127.0.0.1", "--port", str(args.port)],
        cwd=str(WORKTREE), env=env,
        stdout=log_fh, stderr=subprocess.STDOUT,
        creationflags=NO_WINDOW,
    )
    print(f"launched uvicorn pid={proc.pid}")

    print(f"waiting (up to {args.timeout}s) for /health genes > 0...")
    genes, data = wait_for_ready(health_url, args.timeout)
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

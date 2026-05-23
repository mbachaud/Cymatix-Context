r"""Run the M2-V groundedness verifier across all 5 fixtures of an M2 sweep.

For each fixture:
  1. Kill any existing helix uvicorn (port 11437)
  2. Start helix with the fixture's db loaded (HELIX_CONFIG + HELIX_GENOME_PATH,
     plus HELIX_USE_SHARDS=1 for sharded fixtures)
  3. Wait for /health to report genes > 0
  4. Run bench_groundedness_verifier on the fixture's <fixture>.jsonl
  5. Kill helix
  6. Move to next fixture

After all 5 fixtures: print a consolidated summary table.

Usage:
  python benchmarks/run_verifier_sweep.py <m2_sweep_dir>

  python benchmarks/run_verifier_sweep.py \
      F:/Projects/helix-context/benchmarks/results/injected_helix_20260521T012004Z
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


WORKTREE_ROOT = Path(r"F:\Projects\helix-context\.claude\worktrees\vibrant-easley-73d68a")
HELIX_CONFIG = WORKTREE_ROOT / "helix.toml"
MANIFEST = Path(r"F:\Projects\helix-context\genomes\bench\matrix\frozen.json")
HEALTH_URL = "http://127.0.0.1:11437/health"
HELIX_HEALTH_TIMEOUT_S = 240
HELIX_BOOT_LOG = Path(r"F:\tmp\helix_verifier_sweep.log")


def kill_existing_helix() -> None:
    """Kill any python.exe running uvicorn helix_context._asgi."""
    try:
        subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'uvicorn helix_context._asgi' "
                "-and $_.ProcessId -ne $PID } | ForEach-Object { "
                "Stop-Process -Id $_.ProcessId -Force }",
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass


def wait_for_helix(timeout_s: float = HELIX_HEALTH_TIMEOUT_S) -> int:
    """Poll /health until genes > 0 or timeout. Returns final gene count (0 if timeout)."""
    import urllib.request
    deadline = time.perf_counter() + timeout_s
    last_genes = -1
    while time.perf_counter() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                last_genes = data.get("genes", -1)
                if last_genes and last_genes > 0:
                    return last_genes
        except Exception:
            pass
        time.sleep(2)
    return last_genes


def start_helix_for_fixture(fixture_key: str, fixture: dict) -> subprocess.Popen:
    """Start helix uvicorn with the given fixture's db loaded. Returns the
    Popen handle (caller is responsible for killing it)."""
    env = os.environ.copy()
    env["HELIX_CONFIG"] = str(HELIX_CONFIG)
    env["HELIX_GENOME_PATH"] = fixture["path"]
    if fixture["mode"] == "sharded":
        env["HELIX_USE_SHARDS"] = "1"
    # Open log file in append mode so we can see boot output per fixture.
    HELIX_BOOT_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = HELIX_BOOT_LOG.open("a", encoding="utf-8")
    log_fh.write(f"\n\n=== boot fixture={fixture_key} at {datetime.now(timezone.utc).isoformat()} ===\n")
    log_fh.write(f"  path={fixture['path']}\n  mode={fixture['mode']}\n\n")
    log_fh.flush()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "helix_context._asgi:app",
            "--host", "127.0.0.1", "--port", "11437",
        ],
        cwd=str(WORKTREE_ROOT),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    return proc


def run_verifier(jsonl_path: Path, verifier_model: str, max_usd: float) -> dict:
    """Invoke bench_groundedness_verifier.py on the jsonl. Returns summary dict."""
    verifier_script = WORKTREE_ROOT / "benchmarks" / "bench_groundedness_verifier.py"
    cmd = [
        sys.executable,
        str(verifier_script),
        str(jsonl_path),
        "--verifier-model", verifier_model,
        "--max-usd", str(max_usd),
    ]
    print(f"  invoking verifier: {' '.join(cmd[:3])} ... {jsonl_path.name}", flush=True)
    proc = subprocess.run(cmd, capture_output=False, text=True)
    if proc.returncode != 0:
        return {"error": f"verifier exited {proc.returncode}"}
    # Find the latest verifier_summary_*.json in the same dir as the jsonl
    summaries = sorted(
        jsonl_path.parent.glob("verifier_summary_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not summaries:
        return {"error": "no verifier_summary_*.json produced"}
    return json.loads(summaries[0].read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("m2_sweep_dir", type=Path,
                        help="Path to the M2 sweep results dir (with per-fixture jsonl files)")
    parser.add_argument("--verifier-model", default="sonnet",
                        help="Verifier model (sonnet/haiku). Default: sonnet")
    parser.add_argument("--max-usd", type=float, default=0.10,
                        help="Per-call budget cap for verifier")
    parser.add_argument("--only", help="Comma-separated fixtures to run (default: all 5 trusted)")
    args = parser.parse_args()

    if not args.m2_sweep_dir.is_dir():
        print(f"ERROR: not a directory: {args.m2_sweep_dir}", file=sys.stderr)
        return 2

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    targets = manifest["targets"]
    fixture_keys = ["small", "medium", "large", "xl", "medium-sharded"]
    if args.only:
        wanted = [s.strip() for s in args.only.split(",")]
        fixture_keys = [k for k in fixture_keys if k in wanted]

    results_per_fixture: dict[str, dict] = {}

    for fixture_key in fixture_keys:
        jsonl_path = args.m2_sweep_dir / f"{fixture_key}.jsonl"
        if not jsonl_path.exists():
            print(f"  SKIP {fixture_key}: jsonl not found at {jsonl_path}", file=sys.stderr)
            continue

        print(f"\n{'='*72}")
        print(f"=== fixture={fixture_key} ===")
        print(f"{'='*72}")

        kill_existing_helix()
        time.sleep(2)

        fixture = targets[fixture_key]
        print(f"  starting helix: path={fixture['path']} mode={fixture['mode']}")
        proc = start_helix_for_fixture(fixture_key, fixture)
        try:
            genes = wait_for_helix()
            if genes <= 0:
                print(f"  ERROR: helix never reached genes>0 (last={genes})",
                      file=sys.stderr)
                results_per_fixture[fixture_key] = {"error": "helix failed to load"}
                continue
            print(f"  helix loaded: genes={genes} pid={proc.pid}")
            summary = run_verifier(jsonl_path, args.verifier_model, args.max_usd)
            results_per_fixture[fixture_key] = summary
        finally:
            print(f"  stopping helix pid={proc.pid}")
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try: proc.kill()
                except Exception: pass

    # Consolidated summary
    print(f"\n\n{'='*72}")
    print(f"=== M2-V SWEEP CONSOLIDATED SUMMARY ===")
    print(f"{'='*72}")
    print(f"M2 sweep dir: {args.m2_sweep_dir}")
    print(f"verifier model: {args.verifier_model}")
    print()
    print(f"{'fixture':<18} {'GR':>4} {'INSUFF':>7} {'UNSUP':>6} {'CTRD':>5} {'cost':>7}")
    total = {"GROUNDED": 0, "INSUFFICIENT_CONTEXT": 0, "UNSUPPORTED": 0, "CONTRADICTED": 0, "cost": 0.0}
    for fkey, summary in results_per_fixture.items():
        if "error" in summary:
            print(f"  {fkey:<18} ERROR: {summary['error']}")
            continue
        bc = summary.get("by_verifier_class", {})
        print(f"{fkey:<18} "
              f"{bc.get('GROUNDED',0):>4} "
              f"{bc.get('INSUFFICIENT_CONTEXT',0):>7} "
              f"{bc.get('UNSUPPORTED',0):>6} "
              f"{bc.get('CONTRADICTED',0):>5} "
              f"${summary.get('total_cost_usd',0):>6.2f}")
        for k in ("GROUNDED","INSUFFICIENT_CONTEXT","UNSUPPORTED","CONTRADICTED"):
            total[k] += bc.get(k, 0)
        total["cost"] += summary.get("total_cost_usd", 0)
    print(f"{'TOTAL':<18} "
          f"{total['GROUNDED']:>4} "
          f"{total['INSUFFICIENT_CONTEXT']:>7} "
          f"{total['UNSUPPORTED']:>6} "
          f"{total['CONTRADICTED']:>5} "
          f"${total['cost']:>6.2f}")

    # Final cleanup
    kill_existing_helix()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

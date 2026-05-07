"""helix-vault — operator CLI for the vault.

Talks to the running helix server via HTTP. Avoids shared SQLite handles
and concurrent-writer races.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

import httpx


def _api_base() -> str:
    return os.environ.get("HELIX_URL", "http://127.0.0.1:11437")


def _print(obj) -> None:
    print(json.dumps(obj, indent=2))


def _cmd_export(args) -> int:
    payload = {"full": args.full}
    with httpx.Client(timeout=300) as c:
        r = c.post(f"{_api_base()}/export/obsidian", json=payload)
    if r.status_code != 200:
        print(f"export failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print(r.json())
    return 0


def _cmd_status(args) -> int:
    with httpx.Client(timeout=10) as c:
        r = c.get(f"{_api_base()}/vault/status")
    if r.status_code != 200:
        print(f"status failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print(r.json())
    return 0


def _cmd_trace(args) -> int:
    """Manually export a trace, or list recent traces."""
    if args.last is not None:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{_api_base()}/vault/status")
        if r.status_code != 200:
            print(f"trace --last failed: {r.status_code} {r.text}", file=sys.stderr)
            return 1
        body = r.json()
        vault_root = body.get("vault_root", "(unknown)")
        print(f"Recent traces under {vault_root}/_traces/ — list with:")
        print(f"  ls -lt {vault_root}/_traces/ | head -n {args.last + 1}")
        return 0

    if not args.request_id:
        print("trace: must pass <request_id> or --last N", file=sys.stderr)
        return 2

    payload = {
        "request_id": args.request_id,
        "trigger_reason": "manual",
        "total_latency_ms": 0,
        "health_status": "aligned",
        "stage_timing_ms": {},
        "fingerprint_route": "",
        "foveated_ranks": "",
        "final_genes": [],
    }
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{_api_base()}/vault/trace", json=payload)
    if r.status_code != 200:
        print(f"trace failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print(r.json())
    return 0


def _cmd_pin(args) -> int:
    with httpx.Client(timeout=10) as c:
        r = c.post(f"{_api_base()}/vault/traces/{args.request_id}/pin")
    if r.status_code != 200:
        print(f"pin failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print(r.json())
    return 0


def _cmd_unpin(args) -> int:
    with httpx.Client(timeout=10) as c:
        r = c.post(f"{_api_base()}/vault/traces/{args.request_id}/unpin")
    if r.status_code != 200:
        print(f"unpin failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print(r.json())
    return 0


def _cmd_prune(args) -> int:
    print("prune is automatic; for manual prune the helix server must be running",
          file=sys.stderr)
    return 0


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="helix-vault", description="Helix vault operator CLI")
    sp = p.add_subparsers(dest="cmd", required=True)

    ex = sp.add_parser("export", help="Trigger snapshot export")
    ex.add_argument("--full", action="store_true", help="Full re-export (default: incremental)")
    ex.set_defaults(func=_cmd_export)

    st = sp.add_parser("status", help="Show vault state")
    st.set_defaults(func=_cmd_status)

    tr = sp.add_parser("trace", help="Manually write a trace for a request_id, or list recent traces")
    tr.add_argument("request_id", nargs="?", default=None,
                    help="The request_id to trace; omit when using --last")
    tr.add_argument("--last", type=int, default=None,
                    help="Show how to list the last N traces (file-listing pointer)")
    tr.set_defaults(func=_cmd_trace)

    pn = sp.add_parser("pin", help="Pin a trace (move to _traces-pinned/)")
    pn.add_argument("request_id")
    pn.set_defaults(func=_cmd_pin)

    up = sp.add_parser("unpin", help="Unpin a trace (move back to _traces/, reset TTL)")
    up.add_argument("request_id")
    up.set_defaults(func=_cmd_unpin)

    pr = sp.add_parser("prune", help="Note about manual prune")
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(func=_cmd_prune)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

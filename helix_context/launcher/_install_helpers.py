"""Helpers for scripts/install-native-observability.{ps1,sh}.

Both shell scripts delegate the cross-platform-tricky bits (hash verify,
download with timeout, archive extraction) to this module so the test
surface stays in Python and the shells stay thin orchestrators.

Usage from the shell:

    python -m helix_context.launcher._install_helpers verify-hash <path> <hex>

Or invoked directly from Python (for tests + the launcher's first-launch
prompt path).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("helix.launcher.install")

_HASH_CHUNK = 1 << 20  # 1 MiB


class HashMismatch(Exception):
    """Downloaded artifact's SHA256 doesn't match the pinned value."""


class HashPlaceholder(Exception):
    """The pinned hash is a `TODO_<platform>` placeholder — install refused."""


def _is_placeholder(hex_hash: str) -> bool:
    return hex_hash.startswith("TODO_") or "PLAN_NOTE_FILL" in hex_hash


def verify_hash(path: Path, expected_hex: str) -> None:
    """Raise HashMismatch / HashPlaceholder if path's SHA256 doesn't match.

    Returns None on success.
    """
    if _is_placeholder(expected_hex):
        raise HashPlaceholder(
            f"Pinned hash for this platform is a placeholder ({expected_hex!r}); "
            "fill in tools/native-otel/.versions before installing."
        )
    if len(expected_hex) != 64:
        raise HashMismatch(
            f"Pinned hash for {path.name} is malformed (got {expected_hex!r}, "
            "expected 64 hex chars)."
        )
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    actual = h.hexdigest()
    if actual.lower() != expected_hex.lower():
        raise HashMismatch(
            f"SHA256 mismatch for {path.name}: got {actual}, expected {expected_hex}"
        )


def should_skip(path: Path, expected_hex: str) -> bool:
    """Return True iff path exists AND its hash matches expected_hex.

    Used by the bootstrap to skip the download for already-installed binaries.
    Never raises — placeholder + mismatched hashes both return False.
    """
    if not path.exists() or not path.is_file():
        return False
    try:
        verify_hash(path, expected_hex)
    except (HashMismatch, HashPlaceholder, OSError):
        return False
    return True


def download_to(url: str, dest: Path, *, timeout: float = 30.0) -> None:
    """Stream a URL to dest atomically (download to .part, then rename).

    Per global preference: every urlopen call has an explicit timeout.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, tmp.open("wb") as out:
            while True:
                chunk = resp.read(_HASH_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
        tmp.replace(dest)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _cli(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="install-helpers")
    sub = p.add_subparsers(dest="cmd", required=True)
    s_v = sub.add_parser("verify-hash")
    s_v.add_argument("path")
    s_v.add_argument("expected_hex")
    s_s = sub.add_parser("should-skip")
    s_s.add_argument("path")
    s_s.add_argument("expected_hex")
    s_d = sub.add_parser("download")
    s_d.add_argument("url")
    s_d.add_argument("dest")
    s_d.add_argument("--timeout", type=float, default=30.0)
    args = p.parse_args(argv)
    try:
        if args.cmd == "verify-hash":
            verify_hash(Path(args.path), args.expected_hex)
            print("OK")
        elif args.cmd == "should-skip":
            # Silent decision predicate for the install script's
            # existing-binary check. Missing file + hash drift are both
            # EXPECTED outcomes ("please download"); they must NOT write
            # to stderr because PowerShell 5.1 with ErrorActionPreference=Stop
            # turns native-command stderr into a script-terminating error,
            # defeating the script's 2>$null redirect.
            if should_skip(Path(args.path), args.expected_hex):
                return 0
            return 1
        elif args.cmd == "download":
            download_to(args.url, Path(args.dest), timeout=args.timeout)
            print("OK")
        return 0
    except HashPlaceholder as exc:
        print(f"PLACEHOLDER: {exc}", file=sys.stderr)
        return 2
    except HashMismatch as exc:
        print(f"MISMATCH: {exc}", file=sys.stderr)
        return 3
    except (OSError, ValueError, urllib.request.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_cli())

"""Ed25519 verification of directory plugins.

A signed plugin directory looks like::

    <name>/plugin.json   manifest (keys: name, version, requires_core, entry,
                         capabilities, publisher, requires, files)
    <name>/plugin.sig    raw 64-byte Ed25519 signature over the exact bytes
                         of plugin.json
    <name>/src/...       the plugin code; ``files`` maps every file under
                         src/ (posix-style relative path) to its sha256 hex

Stale ``__pycache__`` directories under src/ are deleted before the file scan;
any other unlisted file (including a stray .pyc) is refused. A residual TOCTOU
exists between hashing and import; exploiting it requires write access to the
user's data directory.

Verification never imports or executes anything from the plugin. Every
failure is returned as ``(False, reason)``; this module does not raise on bad
input.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Dict, Optional, Sequence, Tuple

MANIFEST_NAME = "plugin.json"
SIGNATURE_NAME = "plugin.sig"
SRC_NAME = "src"

_ED25519_SIG_LEN = 64
_ED25519_KEY_LEN = 32
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400  # Windows symlinks and junctions


def _is_link(st: os.stat_result) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def is_safe_relative_key(key: object) -> bool:
    """True for a plain posix relative path with no traversal components."""
    if not isinstance(key, str) or not key:
        return False
    if "\\" in key or ":" in key or "\x00" in key:
        return False
    p = PurePosixPath(key)
    if p.is_absolute():
        return False
    return all(part not in ("", ".", "..") for part in key.split("/"))


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scan_src(src: Path) -> Tuple[Optional[Dict[str, Path]], Optional[str]]:
    """Return {posix relative path: path} of every file under src/.

    Refuses symlinks, junctions and special files anywhere under src/.
    """
    try:
        st = os.lstat(src)
    except OSError:
        return None, "missing src/"
    if _is_link(st) or not stat.S_ISDIR(st.st_mode):
        return None, "src/ is not a plain directory (symlink or file)"
    found: Dict[str, Path] = {}
    walk_errors: list = []
    for root, dirs, files in os.walk(src, followlinks=False, onerror=walk_errors.append):
        if walk_errors:  # an unlistable directory is a refusal, never a skip
            break
        root_p = Path(root)
        for name in list(dirs) + list(files):
            p = root_p / name
            rel = p.relative_to(src).as_posix()
            try:
                pst = os.lstat(p)
            except OSError as exc:
                return None, f"cannot stat {rel}: {exc!r}"
            if _is_link(pst):
                return None, f"symlink not allowed in src/: {rel}"
            if name in files:
                if not stat.S_ISREG(pst.st_mode):
                    return None, f"not a regular file: {rel}"
                found[rel] = p
    if walk_errors:
        return None, f"cannot list a directory under src/: {walk_errors[0]!r}"
    return found, None


def _purge_pycache(src: Path) -> Optional[str]:
    """Delete every real __pycache__ directory under src/ (stale bytecode is
    never trusted, and our own import may have created it). Links are left for
    the scan to refuse. Returns a refusal reason if a deletion fails."""
    import shutil

    try:
        st = os.lstat(src)
    except OSError:
        return None  # the scan reports a missing src/
    if _is_link(st) or not stat.S_ISDIR(st.st_mode):
        return None
    walk_errors: list = []
    for root, dirs, _files in os.walk(src, followlinks=False, onerror=walk_errors.append):
        if walk_errors:
            break
        for name in list(dirs):
            # os.walk's islink check misses NTFS junctions: prune every link or
            # reparse point ourselves so we never descend out of src/.
            try:
                if _is_link(os.lstat(Path(root) / name)):
                    dirs.remove(name)
                    continue
            except OSError as exc:
                return f"cannot stat {name}: {exc!r}"
            if name != "__pycache__":
                continue
            p = Path(root) / name
            dirs.remove(name)
            try:
                if _is_link(os.lstat(p)):
                    continue
                shutil.rmtree(p)
            except OSError as exc:
                return f"cannot remove stale bytecode cache {p.relative_to(src).as_posix()}: {exc!r}"
    if walk_errors:
        return f"cannot list a directory under src/: {walk_errors[0]!r}"
    return None


def verify_manifest_bytes(
    path: Path, manifest: bytes, sig: bytes, keys: Sequence[bytes]
) -> Tuple[bool, Optional[str], Optional[dict]]:
    """Verify ``manifest`` (exact bytes) + src/ hashes. Returns (ok, reason, parsed).

    ``parsed`` is the manifest decoded from the very bytes that were verified.
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False, "cryptography not installed", None

    if len(sig) != _ED25519_SIG_LEN:
        return False, "invalid signature (not 64 bytes)", None
    verified = False
    for key in keys:
        if not isinstance(key, (bytes, bytearray)) or len(key) != _ED25519_KEY_LEN:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(bytes(key)).verify(sig, manifest)
            verified = True
            break
        except (InvalidSignature, ValueError):
            continue
    if not verified:
        return False, "signature does not verify under any trusted key", None

    import json

    try:
        data = json.loads(manifest.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return False, f"plugin.json is not valid JSON: {exc}", None
    if not isinstance(data, dict):
        return False, "plugin.json is not an object", None
    files = data.get("files")
    if not isinstance(files, dict):
        return False, "plugin.json has no 'files' map", None
    for key, digest in files.items():
        if not is_safe_relative_key(key):
            return False, f"unsafe path in files: {key!r}", None
        if not (
            isinstance(digest, str)
            and len(digest) == 64
            and all(c in "0123456789abcdef" for c in digest)
        ):
            return False, f"bad sha256 for {key}", None

    err = _purge_pycache(path / SRC_NAME)
    if err is not None:
        return False, err, None
    actual, err = _scan_src(path / SRC_NAME)
    if err is not None or actual is None:
        return False, err, None
    for rel in sorted(actual):
        if rel not in files:
            return False, f"unlisted file in src/: {rel}", None
    for rel in sorted(files):
        p = actual.get(rel)
        if p is None:
            return False, f"missing file: {rel}", None
        try:
            body = p.read_bytes()  # hash exactly the bytes we read
        except OSError as exc:
            return False, f"cannot read {rel}: {exc!r}", None
        if _sha256_hex(body) != files[rel]:
            return False, f"hash mismatch: {rel}", None
    return True, None, data


def verify_plugin_dir(path: Path, keys: Sequence[bytes]) -> Tuple[bool, Optional[str]]:
    """True only if plugin.sig verifies plugin.json under one of ``keys`` AND
    every file in ``files`` matches its sha256 AND no unlisted file exists
    under src/ (symlinks and path traversal are refusals)."""
    path = Path(path)
    try:
        manifest = (path / MANIFEST_NAME).read_bytes()
    except OSError as exc:
        return False, f"cannot read plugin.json: {exc!r}"
    try:
        sig = (path / SIGNATURE_NAME).read_bytes()
    except OSError:
        return False, "unsigned"
    ok, reason, _ = verify_manifest_bytes(path, manifest, sig, keys)
    return ok, reason

"""Tests for the bootstrap script's Python helper entry-points.

Both install-native-observability.ps1 and .sh delegate hash verification
and archive extraction to a Python helper module so the platform-specific
shell wrapper stays small and the testable surface is one place.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent


def _real_sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def test_verify_hash_accepts_match(tmp_path):
    from helix_context.launcher._install_helpers import verify_hash
    f = tmp_path / "binary.bin"
    f.write_bytes(b"hello world")
    expected = _real_sha256(f)
    # Returns None on success (no raise).
    verify_hash(f, expected)


def test_verify_hash_raises_on_mismatch(tmp_path):
    from helix_context.launcher._install_helpers import (
        HashMismatch,
        verify_hash,
    )
    f = tmp_path / "binary.bin"
    f.write_bytes(b"corrupt")
    with pytest.raises(HashMismatch):
        verify_hash(f, "0" * 64)


def test_verify_hash_rejects_placeholder(tmp_path):
    """A `TODO_<platform>` placeholder must NOT be accepted as a valid hash.

    Bootstrap path: refuses to install on platforms where the row is still
    placeholder, so a Linux user running this on day 1 (before §11.6 work
    lands) gets a clean error instead of an unverified binary.
    """
    from helix_context.launcher._install_helpers import (
        HashPlaceholder,
        verify_hash,
    )
    f = tmp_path / "binary.bin"
    f.write_bytes(b"hello")
    with pytest.raises(HashPlaceholder):
        verify_hash(f, "TODO_linux_amd64")


def test_should_skip_when_existing_binary_matches(tmp_path):
    """Idempotency: present-and-correct binary returns True from
    should_skip; download is not re-run."""
    from helix_context.launcher._install_helpers import should_skip
    f = tmp_path / "binary.bin"
    f.write_bytes(b"present and correct")
    expected = _real_sha256(f)
    assert should_skip(f, expected) is True


def test_should_skip_false_when_binary_absent(tmp_path):
    from helix_context.launcher._install_helpers import should_skip
    assert should_skip(tmp_path / "nope.bin", "0" * 64) is False


def test_should_skip_false_when_hash_drifts(tmp_path):
    """Version bump → hash drift → re-download triggers."""
    from helix_context.launcher._install_helpers import should_skip
    f = tmp_path / "binary.bin"
    f.write_bytes(b"old version")
    assert should_skip(f, "0" * 64) is False


# ── Script-presence / parseability checks ───────────────────────────
# These guarantee the platform-specific wrappers exist alongside the
# Python helpers tested above. They don't try to invoke the install
# end-to-end (network + extraction), but they DO verify the shells parse
# without syntax errors so a typo doesn't slip through.

PS_SCRIPT = REPO / "scripts" / "install-native-observability.ps1"
SH_SCRIPT = REPO / "scripts" / "install-native-observability.sh"


def test_powershell_install_script_exists():
    assert PS_SCRIPT.exists(), f"missing: {PS_SCRIPT}"


def test_bash_install_script_exists():
    assert SH_SCRIPT.exists(), f"missing: {SH_SCRIPT}"


def test_bash_install_script_parses():
    """`bash -n` checks syntax without executing. Skipped if bash missing."""
    import shutil
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not on PATH")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [bash, "-n", str(SH_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=creationflags,
    )
    assert proc.returncode == 0, f"bash -n failed: {proc.stderr}"


def test_powershell_install_script_handles_targz():
    """Plan-fix carry-forward: Tempo ships .tar.gz on Windows, NOT .zip.

    The script must dispatch on archive extension rather than assuming
    every Windows artifact is a zip.
    """
    text = PS_SCRIPT.read_text(encoding="utf-8")
    assert ".zip" in text, "PowerShell script missing .zip branch"
    assert "tar" in text.lower(), "PowerShell script missing tar branch (Tempo Windows is .tar.gz)"


def test_powershell_install_script_is_ascii_only():
    """PowerShell 5.1 (the default `powershell.exe` on Windows) reads .ps1
    files without a BOM as ANSI/CP1252. UTF-8 multi-byte chars (em-dashes,
    section signs, smart quotes) become mojibake that breaks the parser
    mid-string. Pin the script to ASCII so it parses on every Windows box.

    Caught a real regression where an em-dash in a Write-Error string
    broke the install action at runtime.
    """
    script = REPO / "scripts" / "install-native-observability.ps1"
    text = script.read_text(encoding="utf-8")
    non_ascii = [
        (i + 1, c)
        for i, line in enumerate(text.splitlines())
        for c in line
        if ord(c) > 127
    ]
    assert not non_ascii, (
        f"PowerShell install script contains non-ASCII chars that will "
        f"mojibake under powershell.exe (Windows PowerShell 5.1, no BOM): "
        f"{non_ascii[:5]}. Use ASCII alternatives (-- for em-dash, "
        f"'Section' for §)."
    )


@pytest.mark.skipif(sys.platform != "win32", reason="powershell.exe only on Windows")
def test_powershell_install_script_parses_cleanly():
    """Spawn powershell.exe in parse-only mode to verify the script is
    syntactically valid. Mirrors `bash -n` for the .sh sibling. Catches
    syntax errors AND encoding-induced parse failures (e.g., the em-dash
    bug that the ASCII-only test above pins, viewed from the parser side).
    """
    script = REPO / "scripts" / "install-native-observability.ps1"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # `[scriptblock]::Create` parses without executing — equivalent of
    # bash -n. Exit 0 on parse success, non-zero on parse error.
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"$null = [scriptblock]::Create((Get-Content -Raw -Path '{script}'))",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=creationflags,
    )
    assert proc.returncode == 0, (
        f"PowerShell parse failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    )


def test_install_helpers_cli_verify_hash_returns_zero_on_match(tmp_path):
    """The CLI surface is what the shell scripts shell out to. Smoke-test
    it end-to-end so wiring drift between Python and shell is caught."""
    f = tmp_path / "binary.bin"
    f.write_bytes(b"cli round-trip")
    expected = _real_sha256(f)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [sys.executable, "-m", "helix_context.launcher._install_helpers",
         "verify-hash", str(f), expected],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=creationflags,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"


def test_install_helpers_cli_verify_hash_returns_nonzero_on_mismatch(tmp_path):
    f = tmp_path / "binary.bin"
    f.write_bytes(b"cli mismatch")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [sys.executable, "-m", "helix_context.launcher._install_helpers",
         "verify-hash", str(f), "0" * 64],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=creationflags,
    )
    assert proc.returncode != 0
    assert "MISMATCH" in proc.stderr or "ERROR" in proc.stderr

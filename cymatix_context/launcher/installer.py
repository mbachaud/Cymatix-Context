"""
Service installer — platform-aware wrapper around the deploy/ templates.

Supports three install targets:

    Linux    → systemd user unit at ~/.config/systemd/user/cymatix-launcher.service
    macOS    → launchd plist at ~/Library/LaunchAgents/io.cymatix.launcher.plist
    Windows  → prints NSSM recipe (cannot bundle NSSM due to licensing +
               download requirements; user runs the steps themselves)

On macOS, install additionally auto-migrates the pre-0.5 LaunchAgent
(com.swiftwing21.cymatix-launcher.plist): detect → launchctl unload →
remove, per the Migration section of deploy/launchd/io.cymatix.launcher.plist.

The installer only **writes the file**. It does NOT run systemctl enable,
launchctl load, or nssm install — those are side effects that deserve
explicit user consent. The installer prints the exact next command so
the user can paste it into their own shell.

Invoked via the CLI:

    cymatix-launcher install-service
    cymatix-launcher uninstall-service

Each subcommand walks through:

    1. Detect platform
    2. Locate the template file in the installed package (or repo `deploy/`)
    3. Substitute placeholders (USER, ABSOLUTE_PATH_TO_CYMATIX_LAUNCHER, etc.)
    4. Write the target file with sane perms
    5. Print the next-step command
"""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("cymatix.launcher.installer")

# Pre-0.5 releases installed the LaunchAgent under a personal reverse-DNS
# id; 0.5+ uses the neutral io.cymatix label (#207 item 7).
LEGACY_DARWIN_PLIST_NAME = "com.swiftwing21.cymatix-launcher.plist"


# ── platform detection ────────────────────────────────────────────

def current_platform() -> str:
    """Return 'linux', 'darwin', 'win32', or 'unsupported'."""
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "darwin"
    if p == "win32":
        return "win32"
    return "unsupported"


# ── template discovery ───────────────────────────────────────────

def _deploy_dir() -> Optional[Path]:
    """Locate the deploy/ directory relative to the installed package.

    The templates ship in the source tree at `<repo>/deploy/`. When
    installed via pip, the templates are not inside the wheel by
    default (deploy/ is not part of the package). Fall back to
    walking up from __file__ to find a sibling `deploy/`.
    """
    here = Path(__file__).resolve()
    for candidate in [here.parent.parent.parent, here.parent.parent.parent.parent]:
        deploy = candidate / "deploy"
        if deploy.exists() and deploy.is_dir():
            return deploy
    return None


def _find_template(platform: str) -> Optional[Path]:
    """Return the template path for the given platform, or None if missing."""
    deploy = _deploy_dir()
    if deploy is None:
        return None
    if platform == "linux":
        return deploy / "systemd" / "cymatix-launcher.service"
    if platform == "darwin":
        return deploy / "launchd" / "io.cymatix.launcher.plist"
    return None  # Windows has no installable template (see install_service below)


# ── target paths ─────────────────────────────────────────────────

def target_path(platform: str) -> Optional[Path]:
    """Return the path the service file should be written to."""
    if platform == "linux":
        return Path.home() / ".config" / "systemd" / "user" / "cymatix-launcher.service"
    if platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / "io.cymatix.launcher.plist"
    return None


def _legacy_darwin_plist_path() -> Path:
    """Path of the pre-0.5 LaunchAgent. Separate helper so tests can
    redirect it away from the real home directory."""
    return Path.home() / "Library" / "LaunchAgents" / LEGACY_DARWIN_PLIST_NAME


# ── legacy plist migration (darwin) ──────────────────────────────

def _migrate_legacy_darwin_plist(dry_run: bool = False) -> Optional[str]:
    """Auto-migrate the pre-0.5 ``com.swiftwing21`` LaunchAgent.

    Detect → ``launchctl unload`` → remove, exactly as documented in the
    Migration section of ``deploy/launchd/io.cymatix.launcher.plist``.
    Idempotent (no-op when the legacy plist is absent) and non-fatal:
    each step logs and continues on failure so a partially-migrated
    system still gets the io.cymatix plist installed.

    Returns a human-readable summary when a legacy plist was found,
    else None.
    """
    legacy = _legacy_darwin_plist_path()
    try:
        found = legacy.exists()
    except OSError:
        log.warning("Legacy plist check failed for %s", legacy, exc_info=True)
        return None
    if not found:
        return None

    if dry_run:
        return f"[dry run] Would unload + remove legacy plist {legacy}"

    log.info("Legacy LaunchAgent detected at %s — migrating", legacy)
    try:
        # check=False: an already-unloaded agent makes launchctl exit
        # non-zero, which is fine — we only need it gone from launchd.
        subprocess.run(
            ["launchctl", "unload", str(legacy)],
            check=False,
            capture_output=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        log.warning(
            "launchctl unload failed for %s — continuing with removal",
            legacy, exc_info=True,
        )
    try:
        legacy.unlink()
    except OSError:
        log.warning(
            "Could not remove legacy plist %s — continuing", legacy,
            exc_info=True,
        )
        return (
            f"Found legacy plist {legacy} but could not remove it "
            "(see launcher log); remove it by hand:\n\n"
            f"    rm {legacy}"
        )
    return f"Migrated legacy LaunchAgent: unloaded + removed {legacy}"


# ── cymatix-launcher binary discovery ──────────────────────────────

def find_launcher_binary() -> Optional[Path]:
    """Locate the cymatix-launcher console script on the current PATH."""
    path = shutil.which("cymatix-launcher")
    if path:
        return Path(path)
    # Fall back to the same Python that's running us + its Scripts/bin dir.
    py = Path(sys.executable)
    for candidate in (
        py.parent / "cymatix-launcher",
        py.parent / "cymatix-launcher.exe",
        py.parent / "Scripts" / "cymatix-launcher.exe",
    ):
        if candidate.exists():
            return candidate
    return None


# ── template substitution ────────────────────────────────────────

def _substitute(template_text: str, replacements: dict) -> str:
    out = template_text
    for key, value in replacements.items():
        out = out.replace(key, str(value))
    return out


def _linux_replacements(launcher_path: Path) -> dict:
    # The systemd template uses %h (systemd's home placeholder) so we
    # only need to swap the ExecStart path.
    return {
        "%h/.local/share/cymatix/.venv/bin/cymatix-launcher --no-browser":
            f"{launcher_path} --no-browser",
    }


def _darwin_replacements(launcher_path: Path) -> dict:
    user = getpass.getuser()
    return {
        "/usr/local/bin/cymatix-launcher": str(launcher_path),
        "/Users/USERNAME": str(Path.home()),
    }


# ── install ──────────────────────────────────────────────────────

def install_service(dry_run: bool = False, port: int = 11438) -> Tuple[bool, str]:
    """Install the service file for the current platform.

    Returns (success, message). On Windows, returns (False, instructions)
    because there is no NSSM auto-install path; the message contains the
    recipe.

    ``port`` is the launcher UI port shown in the printed next-step URL
    (threaded from the CLI's --port flag; default 11438).
    """
    platform = current_platform()
    if platform == "unsupported":
        return False, f"Unsupported platform: {sys.platform}"

    if platform == "win32":
        msg = (
            "Windows service installation is not automated — NSSM must be\n"
            "installed and the service registered manually. Recipe:\n\n"
            "    1. Install NSSM:\n"
            "         choco install nssm   (or scoop install nssm)\n\n"
            "    2. Find your cymatix-launcher.exe:\n"
            "         where cymatix-launcher\n\n"
            "    3. Register as a Windows service (run as admin):\n"
            "         nssm install CymatixLauncher\n"
            "       ... then fill in the NSSM GUI with the launcher path + --no-browser\n"
            "       See deploy/windows/README.md for the complete walkthrough.\n\n"
            "    4. Start the service:\n"
            "         nssm start CymatixLauncher"
        )
        return False, msg

    template_path = _find_template(platform)
    if template_path is None or not template_path.exists():
        return False, (
            f"Template not found for {platform}. "
            "Is this install missing the deploy/ directory? "
            "Templates ship in the source tree — try reinstalling from source."
        )

    launcher_path = find_launcher_binary()
    if launcher_path is None:
        return False, (
            "Could not locate the `cymatix-launcher` executable on PATH. "
            "Install the launcher extras first:\n\n"
            "    pip install cymatix-context[launcher]"
        )

    target = target_path(platform)
    assert target is not None  # platform check above ensures this

    # Substitute placeholders
    template_text = template_path.read_text(encoding="utf-8")
    if platform == "linux":
        content = _substitute(template_text, _linux_replacements(launcher_path))
    else:  # darwin
        content = _substitute(template_text, _darwin_replacements(launcher_path))

    # Auto-migrate the pre-0.5 com.swiftwing21 LaunchAgent before
    # installing the io.cymatix one (contract documented in
    # deploy/launchd/io.cymatix.launcher.plist).
    migration_note = None
    if platform == "darwin":
        migration_note = _migrate_legacy_darwin_plist(dry_run=dry_run)

    if dry_run:
        preview = (
            f"[dry run] Would write {target}\n"
            f"[dry run] Template: {template_path}\n"
            f"[dry run] Launcher: {launcher_path}\n"
            f"\n--- content preview ---\n{content[:800]}"
            f"{'...' if len(content) > 800 else ''}"
        )
        if migration_note:
            preview = f"{migration_note}\n{preview}"
        return True, preview

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    # Print next-step command based on platform
    if platform == "linux":
        next_steps = (
            f"Wrote {target}\n\n"
            "Next steps:\n\n"
            "    systemctl --user daemon-reload\n"
            "    systemctl --user enable --now cymatix-launcher.service\n\n"
            "Verify:\n\n"
            "    systemctl --user status cymatix-launcher.service\n"
            "    journalctl --user -u cymatix-launcher.service -f\n\n"
            f"Open http://127.0.0.1:{port}/ in your browser."
        )
    else:  # darwin
        next_steps = (
            f"Wrote {target}\n\n"
            "Next steps:\n\n"
            f"    launchctl load {target}\n\n"
            "Verify:\n\n"
            "    launchctl list | grep cymatix-launcher\n"
            "    tail -f ~/Library/Logs/cymatix-launcher.log\n\n"
            f"Open http://127.0.0.1:{port}/ in your browser."
        )

    if migration_note:
        next_steps = f"{migration_note}\n\n{next_steps}"
    return True, next_steps


# ── uninstall ────────────────────────────────────────────────────

def uninstall_service(dry_run: bool = False) -> Tuple[bool, str]:
    """Remove the service file for the current platform.

    Does NOT run the platform's disable command — the user is
    responsible for stopping the running service first. We just
    unlink the file.
    """
    platform = current_platform()
    if platform == "unsupported":
        return False, f"Unsupported platform: {sys.platform}"

    if platform == "win32":
        msg = (
            "Windows uninstall is not automated. Run:\n\n"
            "    nssm stop CymatixLauncher\n"
            "    nssm remove CymatixLauncher confirm"
        )
        return False, msg

    target = target_path(platform)
    assert target is not None

    if not target.exists():
        return False, f"Not installed — {target} does not exist"

    if dry_run:
        return True, f"[dry run] Would remove {target}"

    # Warn the user to stop the service first if applicable
    if platform == "linux":
        post = (
            f"Removed {target}\n\n"
            "If the service was running, stop and disable it:\n\n"
            "    systemctl --user disable --now cymatix-launcher.service\n"
            "    systemctl --user daemon-reload"
        )
    else:  # darwin
        post = (
            f"Removed {target}\n\n"
            "If the service was loaded, unload it:\n\n"
            f"    launchctl unload {target}\n"
            "    (it's already removed, so the unload may say 'No such process')"
        )

    target.unlink()
    return True, post

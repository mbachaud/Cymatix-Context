"""
Tests for cymatix_context.launcher.installer — platform detection,
template substitution, install + uninstall paths.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cymatix_context.launcher import installer


class TestCurrentPlatform:
    def test_returns_one_of_known_values(self):
        assert installer.current_platform() in ("linux", "darwin", "win32", "unsupported")


class TestTargetPath:
    def test_linux_target(self):
        assert installer.target_path("linux") == Path.home() / ".config" / "systemd" / "user" / "cymatix-launcher.service"

    def test_darwin_target(self):
        assert installer.target_path("darwin") == Path.home() / "Library" / "LaunchAgents" / "io.cymatix.launcher.plist"

    def test_win32_target_is_none(self):
        assert installer.target_path("win32") is None


class TestFindTemplate:
    def test_linux_template_exists(self):
        p = installer._find_template("linux")
        assert p is not None
        assert p.exists()
        assert p.name == "cymatix-launcher.service"

    def test_darwin_template_exists(self):
        p = installer._find_template("darwin")
        assert p is not None
        assert p.exists()
        assert p.name == "io.cymatix.launcher.plist"

    def test_win32_template_is_none(self):
        assert installer._find_template("win32") is None


class TestFindLauncherBinary:
    def test_returns_none_or_path(self):
        result = installer.find_launcher_binary()
        assert result is None or isinstance(result, Path)


class TestInstallWin32:
    def test_windows_install_returns_instructions(self):
        with patch("cymatix_context.launcher.installer.current_platform", return_value="win32"):
            ok, msg = installer.install_service()
        assert ok is False
        assert "NSSM" in msg
        assert "choco install nssm" in msg or "scoop install nssm" in msg

    def test_windows_uninstall_returns_instructions(self):
        with patch("cymatix_context.launcher.installer.current_platform", return_value="win32"):
            ok, msg = installer.uninstall_service()
        assert ok is False
        assert "nssm remove" in msg


class TestInstallUnsupported:
    def test_unknown_platform_errors(self):
        with patch("cymatix_context.launcher.installer.current_platform", return_value="unsupported"):
            ok, msg = installer.install_service()
        assert ok is False
        assert "Unsupported platform" in msg


class TestInstallMissingBinary:
    def test_linux_install_without_binary_errors(self):
        with patch("cymatix_context.launcher.installer.current_platform", return_value="linux"):
            with patch("cymatix_context.launcher.installer.find_launcher_binary", return_value=None):
                ok, msg = installer.install_service()
        assert ok is False
        assert "cymatix-launcher" in msg.lower()


class TestLinuxDryRun:
    def test_linux_dry_run_does_not_write_file(self, tmp_path):
        fake_target = tmp_path / "cymatix-launcher.service"
        fake_launcher = tmp_path / "cymatix-launcher"
        fake_launcher.write_text("#!/bin/bash\necho hi")
        fake_launcher.chmod(0o755)

        with patch("cymatix_context.launcher.installer.current_platform", return_value="linux"):
            with patch("cymatix_context.launcher.installer.target_path", return_value=fake_target):
                with patch("cymatix_context.launcher.installer.find_launcher_binary", return_value=fake_launcher):
                    ok, msg = installer.install_service(dry_run=True)

        assert ok is True
        assert "dry run" in msg.lower()
        assert not fake_target.exists()


class TestLinuxInstallAndUninstall:
    def test_linux_install_writes_file_and_substitutes_path(self, tmp_path):
        fake_target = tmp_path / "systemd" / "user" / "cymatix-launcher.service"
        fake_launcher = tmp_path / "cymatix-launcher"
        fake_launcher.write_text("#!/bin/bash\necho hi")

        with patch("cymatix_context.launcher.installer.current_platform", return_value="linux"):
            with patch("cymatix_context.launcher.installer.target_path", return_value=fake_target):
                with patch("cymatix_context.launcher.installer.find_launcher_binary", return_value=fake_launcher):
                    ok, msg = installer.install_service()

        assert ok is True
        assert fake_target.exists()
        content = fake_target.read_text(encoding="utf-8")
        # The launcher path should be substituted in
        assert str(fake_launcher) in content
        assert "systemctl --user" in msg

    def test_linux_uninstall_removes_file(self, tmp_path):
        fake_target = tmp_path / "systemd" / "user" / "cymatix-launcher.service"
        fake_target.parent.mkdir(parents=True, exist_ok=True)
        fake_target.write_text("stub content")

        with patch("cymatix_context.launcher.installer.current_platform", return_value="linux"):
            with patch("cymatix_context.launcher.installer.target_path", return_value=fake_target):
                ok, msg = installer.uninstall_service()

        assert ok is True
        assert not fake_target.exists()
        assert "systemctl --user disable" in msg

    def test_linux_uninstall_errors_when_not_installed(self, tmp_path):
        fake_target = tmp_path / "not-there.service"
        with patch("cymatix_context.launcher.installer.current_platform", return_value="linux"):
            with patch("cymatix_context.launcher.installer.target_path", return_value=fake_target):
                ok, msg = installer.uninstall_service()
        assert ok is False
        assert "does not exist" in msg


class TestDarwinInstall:
    def test_darwin_install_substitutes_username_and_launcher_path(self, tmp_path):
        fake_target = tmp_path / "LaunchAgents" / "io.cymatix.launcher.plist"
        fake_launcher = tmp_path / "bin" / "cymatix-launcher"
        fake_launcher.parent.mkdir(parents=True)
        fake_launcher.write_text("#!/bin/bash\necho hi")

        with patch("cymatix_context.launcher.installer.current_platform", return_value="darwin"):
            with patch("cymatix_context.launcher.installer.target_path", return_value=fake_target):
                with patch("cymatix_context.launcher.installer.find_launcher_binary", return_value=fake_launcher):
                    with patch("cymatix_context.launcher.installer._legacy_darwin_plist_path",
                               return_value=tmp_path / "LaunchAgents" / "absent-legacy.plist"):
                        ok, msg = installer.install_service()

        assert ok is True
        assert fake_target.exists()
        content = fake_target.read_text(encoding="utf-8")
        assert str(fake_launcher) in content
        # USERNAME placeholder should be replaced (with $HOME, not the literal string)
        assert "/Users/USERNAME" not in content
        assert "launchctl load" in msg


class TestDarwinLegacyMigration:
    """Pre-0.5 installs shipped com.swiftwing21.cymatix-launcher.plist.
    install_service on darwin must auto-migrate it — detect, launchctl
    unload, remove — before writing the io.cymatix plist, exactly as
    documented in deploy/launchd/io.cymatix.launcher.plist (Migration
    section). Idempotent, and non-fatal on partial failure."""

    def _install(self, tmp_path, legacy, dry_run=False):
        """Run install_service on a fake darwin with all paths tmp-rooted
        so tests never touch the real home directory."""
        fake_target = tmp_path / "LaunchAgents" / "io.cymatix.launcher.plist"
        fake_launcher = tmp_path / "bin" / "cymatix-launcher"
        fake_launcher.parent.mkdir(parents=True, exist_ok=True)
        fake_launcher.write_text("#!/bin/bash\necho hi")

        with patch("cymatix_context.launcher.installer.current_platform", return_value="darwin"):
            with patch("cymatix_context.launcher.installer.target_path", return_value=fake_target):
                with patch("cymatix_context.launcher.installer.find_launcher_binary", return_value=fake_launcher):
                    with patch("cymatix_context.launcher.installer._legacy_darwin_plist_path",
                               return_value=legacy):
                        ok, msg = installer.install_service(dry_run=dry_run)
        return ok, msg, fake_target

    def test_legacy_plist_is_unloaded_and_removed(self, tmp_path):
        legacy = tmp_path / "LaunchAgents" / "com.swiftwing21.cymatix-launcher.plist"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("<plist/>")

        with patch("cymatix_context.launcher.installer.subprocess.run") as mock_run:
            ok, msg, fake_target = self._install(tmp_path, legacy)

        assert ok is True
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == ["launchctl", "unload", str(legacy)]
        # Subprocess-safety rule: no console window flash on Windows hosts.
        assert kwargs.get("creationflags") == getattr(
            installer.subprocess, "CREATE_NO_WINDOW", 0
        )
        assert not legacy.exists(), "Legacy plist must be removed after unload"
        assert fake_target.exists(), "New io.cymatix plist must still be installed"
        assert "com.swiftwing21" in msg

    def test_migration_is_noop_when_legacy_absent(self, tmp_path):
        legacy = tmp_path / "LaunchAgents" / "com.swiftwing21.cymatix-launcher.plist"
        # Not written — the common (already-migrated / fresh) case.
        with patch("cymatix_context.launcher.installer.subprocess.run") as mock_run:
            ok, msg, fake_target = self._install(tmp_path, legacy)

        assert ok is True
        mock_run.assert_not_called()
        assert fake_target.exists()
        assert "com.swiftwing21" not in msg

    def test_migration_survives_launchctl_failure(self, tmp_path):
        """launchctl missing/erroring must not abort the migration —
        the stale plist is still removed and the install proceeds."""
        legacy = tmp_path / "LaunchAgents" / "com.swiftwing21.cymatix-launcher.plist"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("<plist/>")

        with patch("cymatix_context.launcher.installer.subprocess.run",
                   side_effect=FileNotFoundError("no launchctl")):
            ok, msg, fake_target = self._install(tmp_path, legacy)

        assert ok is True
        assert not legacy.exists()
        assert fake_target.exists()

    def test_migration_survives_unlink_failure(self, tmp_path):
        """A legacy plist we can't delete is logged, not fatal — the
        io.cymatix install must still complete."""
        legacy = tmp_path / "LaunchAgents" / "com.swiftwing21.cymatix-launcher.plist"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("<plist/>")

        real_unlink = Path.unlink

        def failing_unlink(self, *a, **kw):
            if self.name == "com.swiftwing21.cymatix-launcher.plist":
                raise OSError("file locked")
            return real_unlink(self, *a, **kw)

        with patch("cymatix_context.launcher.installer.subprocess.run"):
            with patch.object(Path, "unlink", failing_unlink):
                ok, msg, fake_target = self._install(tmp_path, legacy)

        assert ok is True
        assert legacy.exists()  # removal failed, but install went through
        assert fake_target.exists()

    def test_dry_run_previews_migration_without_side_effects(self, tmp_path):
        legacy = tmp_path / "LaunchAgents" / "com.swiftwing21.cymatix-launcher.plist"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("<plist/>")

        with patch("cymatix_context.launcher.installer.subprocess.run") as mock_run:
            ok, msg, fake_target = self._install(tmp_path, legacy, dry_run=True)

        assert ok is True
        mock_run.assert_not_called()
        assert legacy.exists()
        assert not fake_target.exists()
        assert "dry run" in msg.lower()
        assert "com.swiftwing21" in msg


class TestCLIIntegration:
    def test_install_service_command_dispatches(self, capsys):
        from cymatix_context.launcher import app as app_mod
        with patch("cymatix_context.launcher.installer.install_service",
                   return_value=(True, "ok message")):
            rc = app_mod.main(["install-service"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "ok message" in captured.out

    def test_uninstall_service_command_dispatches(self, capsys):
        from cymatix_context.launcher import app as app_mod
        with patch("cymatix_context.launcher.installer.uninstall_service",
                   return_value=(True, "removed")):
            rc = app_mod.main(["uninstall-service"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "removed" in captured.out

    def test_install_service_failure_returns_nonzero(self, capsys):
        from cymatix_context.launcher import app as app_mod
        with patch("cymatix_context.launcher.installer.install_service",
                   return_value=(False, "error")):
            rc = app_mod.main(["install-service"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "error" in captured.out

    def test_dry_run_flag_passed_through(self):
        from cymatix_context.launcher import app as app_mod
        with patch("cymatix_context.launcher.installer.install_service",
                   return_value=(True, "dry")) as mock_install:
            app_mod.main(["install-service", "--dry-run"])
        mock_install.assert_called_once_with(dry_run=True, port=11438)

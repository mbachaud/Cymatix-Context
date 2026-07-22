"""Cached launcher update checks against PyPI."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Optional

import httpx

log = logging.getLogger("helix.launcher.update_check")


def _parse_version(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for raw in value.replace("-", ".").split("."):
        digits = ""
        for char in raw:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer_version(latest: str, current: str) -> bool:
    latest_parts = _parse_version(latest)
    current_parts = _parse_version(current)
    if not latest_parts or not current_parts:
        return False
    width = max(len(latest_parts), len(current_parts))
    latest_padded = latest_parts + (0,) * (width - len(latest_parts))
    current_padded = current_parts + (0,) * (width - len(current_parts))
    return latest_padded > current_padded


def installed_version(package_name: str = "cymatix-context") -> str:
    try:
        return metadata.version(package_name)
    except metadata.PackageNotFoundError:
        return "unknown"


@dataclass
class UpdateInfo:
    current_version: str
    latest_version: Optional[str] = None
    update_available: bool = False
    checked_at: Optional[float] = None
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "checked_at": self.checked_at,
            "error": self.error,
        }


class UpdateChecker:
    """TTL cache around PyPI's package JSON endpoint."""

    def __init__(
        self,
        package_name: str = "cymatix-context",
        *,
        ttl_s: float = 6 * 60 * 60,
        timeout_s: float = 3.0,
        enabled: Optional[bool] = None,
    ) -> None:
        self.package_name = package_name
        self.ttl_s = ttl_s
        self.timeout_s = timeout_s
        self.enabled = (
            enabled
            if enabled is not None
            else os.environ.get("HELIX_LAUNCHER_UPDATE_CHECK", "1").strip().lower()
            not in {"0", "false", "no", "off"}
        )
        self._cached: Optional[UpdateInfo] = None

    def check(self, *, force: bool = False) -> UpdateInfo:
        current = installed_version(self.package_name)
        now = time.time()
        if not self.enabled:
            return UpdateInfo(current_version=current)
        if (
            not force
            and self._cached is not None
            and self._cached.checked_at is not None
            and now - self._cached.checked_at < self.ttl_s
        ):
            return self._cached
        try:
            resp = httpx.get(
                f"https://pypi.org/pypi/{self.package_name}/json",
                timeout=self.timeout_s,
            )
            resp.raise_for_status()
            latest = str(resp.json().get("info", {}).get("version") or "").strip()
            info = UpdateInfo(
                current_version=current,
                latest_version=latest or None,
                update_available=bool(latest and is_newer_version(latest, current)),
                checked_at=now,
            )
        except Exception as exc:
            log.debug("Launcher update check failed", exc_info=True)
            info = UpdateInfo(current_version=current, checked_at=now, error=str(exc))
        self._cached = info
        return info

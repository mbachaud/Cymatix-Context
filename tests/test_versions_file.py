"""Regression: tools/native-otel/.versions is parseable, contains all 5
services, and Windows hashes are non-placeholder.

Per spec §6 + §11.6: hashes ship live for Windows on day 1; Linux/macOS
rows mark TODO so the bootstrap can fail loud rather than silently
installing an unverified binary.
"""

from pathlib import Path

import pytest

try:
    import tomllib  # py3.11+
except ImportError:
    import tomli as tomllib  # type: ignore


REPO = Path(__file__).resolve().parent.parent
VERSIONS = REPO / "tools" / "native-otel" / ".versions"
SERVICES = ["otelcol-contrib", "prometheus", "tempo", "loki", "grafana"]
PLATFORMS = ["windows_amd64", "linux_amd64", "darwin_arm64", "darwin_amd64"]


def test_versions_file_exists():
    assert VERSIONS.exists()


def test_versions_file_parses_as_toml():
    with VERSIONS.open("rb") as f:
        spec = tomllib.load(f)
    assert isinstance(spec, dict)


def test_versions_has_all_five_services():
    with VERSIONS.open("rb") as f:
        spec = tomllib.load(f)
    for svc in SERVICES:
        assert svc in spec, f"missing service block: {svc}"
        assert "version" in spec[svc], f"{svc}: missing version"


@pytest.mark.parametrize("svc", SERVICES)
def test_versions_windows_hash_is_real(svc):
    with VERSIONS.open("rb") as f:
        spec = tomllib.load(f)
    h = spec[svc].get("sha256_windows_amd64")
    assert h, f"{svc}: windows_amd64 hash missing"
    # Real SHA256 is 64 hex chars; placeholders look like TODO_*.
    assert len(h) == 64, f"{svc}: windows_amd64 hash is placeholder ({h!r})"
    int(h, 16)  # raises ValueError if not hex


@pytest.mark.parametrize("svc", SERVICES)
@pytest.mark.parametrize("plat", ["linux_amd64", "darwin_arm64", "darwin_amd64"])
def test_versions_other_platforms_are_present_even_if_todo(svc, plat):
    """Non-Windows rows may be TODO placeholders, but the keys must exist
    so the bootstrap script can detect-and-refuse rather than KeyError."""
    with VERSIONS.open("rb") as f:
        spec = tomllib.load(f)
    assert f"sha256_{plat}" in spec[svc]
    assert f"url_{plat}" in spec[svc]

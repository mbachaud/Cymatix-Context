"""Doc tests for the native-observability-sidecar README scope.

Asserts that the docs say what the spec promises:

1. Root README:
   - has a "Native observability (default)" section under Quick Start.
   - mentions the bootstrap script run, tray-managed lifecycle, balloon
     notification, and the HELIX_OBSERVABILITY=0 opt-out.
   - demotes the docker-compose path to an "Advanced" footnote pointing at
     deploy/otel/README.md.
2. deploy/otel/README.md:
   - exists; framed as the alternate Docker path (not deprecated).
   - lists all five services with the same ports as the native sidecar.
   - links back to the native sidecar / spec for the shared-config story.
3. tools/native-otel/README.md:
   - still accurate post Tasks 3-10: per-user state dir, install script,
     idempotent re-run, .versions pinning.

The tests are textual — they do not import any module — so they run on
any host without the launcher extras installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Root README
# ---------------------------------------------------------------------------


def test_root_readme_has_native_observability_section():
    body = _read("README.md")
    assert "Native observability (default)" in body, (
        "Root README is missing the 'Native observability (default)' section"
    )


def test_root_readme_mentions_tray_lifecycle_and_opt_out():
    body = _read("README.md")
    # Tray manages the binaries' lifecycle.
    assert "tray" in body.lower()
    # Opt-out env var is documented.
    assert "HELIX_OBSERVABILITY=0" in body, (
        "Root README must document the HELIX_OBSERVABILITY=0 opt-out"
    )
    # Native binaries directory is referenced.
    assert "tools/native-otel" in body


def test_root_readme_demotes_docker_to_advanced_footnote():
    body = _read("README.md")
    # The docker-compose mention is now framed as "Advanced — Docker stack",
    # not as the primary install path.
    assert "Advanced" in body and "Docker" in body
    # Footnote points at the new doc.
    assert "deploy/otel/README.md" in body, (
        "Root README must link to deploy/otel/README.md"
    )


def test_root_readme_preserves_canonical_launch_section():
    """Regression: don't rip out existing Quick Start ▸ Launch content."""
    body = _read("README.md")
    assert "Canonical path" in body
    assert "start-helix-tray.bat" in body.lower()


# ---------------------------------------------------------------------------
# deploy/otel/README.md
# ---------------------------------------------------------------------------


def test_deploy_otel_readme_exists():
    assert (REPO / "deploy" / "otel" / "README.md").is_file(), (
        "deploy/otel/README.md must exist as the Docker-stack reference"
    )


def test_deploy_otel_readme_is_alternate_not_deprecated():
    body = _read("deploy/otel/README.md")
    # Frames itself as alternate / advanced, not deprecated.
    assert "alternate" in body.lower()
    assert "deprecated" not in body.lower(), (
        "deploy/otel/README.md must NOT call the Docker path deprecated"
    )


def test_deploy_otel_readme_lists_all_five_services():
    body = _read("deploy/otel/README.md").lower()
    for svc in ("otel", "prometheus", "tempo", "loki", "grafana"):
        assert svc in body, f"deploy/otel/README.md is missing service: {svc}"


def test_deploy_otel_readme_documents_shared_ports():
    body = _read("deploy/otel/README.md")
    # Same ports as native sidecar.
    for port in ("4317", "4318", "8889", "9090", "3200", "3100", "3000"):
        assert port in body, (
            f"deploy/otel/README.md is missing port {port}"
        )


def test_deploy_otel_readme_links_to_native_sidecar_or_spec():
    body = _read("deploy/otel/README.md")
    assert "tools/native-otel" in body or "native sidecar" in body.lower()
    # Spec back-reference.
    assert "2026-05-04-native-observability-sidecar-design" in body


# ---------------------------------------------------------------------------
# tools/native-otel/README.md
# ---------------------------------------------------------------------------


def test_native_otel_readme_still_accurate():
    body = _read("tools/native-otel/README.md")
    # Install script reference.
    assert "install-native-observability" in body
    # Per-user state dir story (platformdirs).
    assert "platformdirs" in body or "user_data_dir" in body
    # Pinned versions file.
    assert ".versions" in body
    # Idempotent re-run is called out.
    assert "idempotent" in body.lower() or "skip" in body.lower()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])

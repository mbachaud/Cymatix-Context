"""OKF fixture manifest — a fresh clone must contain every fixture file.

The repo .gitignore carries patterns that match at any depth
(``temp_*.md``, ``logs/``) and has silently untracked fixture payloads
before (benchmarks/results/). MANIFEST.json pins the exact per-bundle
file lists; this test fails loudly if a file goes missing on disk OR
is present but untracked by git (which would make it missing on the
NEXT fresh clone).
"""

import json
import subprocess
from pathlib import Path

import pytest

OKF_FIXTURES = Path(__file__).parent / "fixtures" / "okf"


@pytest.fixture(scope="module")
def manifest():
    return json.loads(
        (OKF_FIXTURES / "MANIFEST.json").read_text(encoding="utf-8")
    )


def test_support_files_present():
    for name in ("MANIFEST.json", "NOTICE", "README.md", "SPEC-ee67a5ca.md"):
        assert (OKF_FIXTURES / name).is_file(), f"missing fixture file: {name}"


def test_bundles_match_manifest_on_disk(manifest):
    assert set(manifest) == {"crypto_bitcoin", "ga4", "type_only", "degraded"}
    for bundle, spec in manifest.items():
        root = OKF_FIXTURES / bundle
        assert root.is_dir(), f"missing bundle dir: {bundle}"
        on_disk = sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file()
        )
        assert on_disk == sorted(spec["files"]), bundle
        assert len(on_disk) == spec["file_count"], bundle


def test_fixture_files_are_git_tracked(manifest):
    """Untracked-but-present files pass the disk check locally and then
    vanish on a fresh clone — catch that before push."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--", "tests/fixtures/okf"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=OKF_FIXTURES.parents[2],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git unavailable")
    if proc.returncode != 0:
        pytest.skip("not a git checkout")

    tracked = {
        line.strip().replace("\\", "/")
        for line in proc.stdout.splitlines()
        if line.strip()
    }
    missing = []
    for bundle, spec in manifest.items():
        for rel in spec["files"]:
            path = f"tests/fixtures/okf/{bundle}/{rel}"
            if path not in tracked:
                missing.append(path)
    assert missing == [], f"fixture files present but untracked: {missing}"


def test_vendored_bundle_files_are_utf8(manifest):
    for bundle in ("crypto_bitcoin", "ga4"):
        root = OKF_FIXTURES / bundle
        for rel in manifest[bundle]["files"]:
            if not rel.endswith(".md"):
                continue
            (root / rel).read_text(encoding="utf-8")  # strict — raises on bad bytes

"""Regression tests for doc references embedded in vault output.

If anyone moves operator-facing docs again (cf. PR #100), the generated
vault README and the operator-facing docs must keep their pointers in sync.
These tests fail loudly when a referenced local doc no longer exists.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from helix_context.config import HelixConfig, VaultConfig, VaultTracesConfig
from helix_context.genome import Genome
from helix_context.vault import VaultManager


REPO_ROOT = Path(__file__).resolve().parents[1]


# Match backtick-quoted paths inside the README that look like local docs:
#   `docs/foo/bar.md`
#   `docs/foo/bar.py`
# Stop at the closing backtick.
_BACKTICK_LOCAL_PATH = re.compile(r"`((?:docs|helix_context|scripts|tests)/[^\s`]+\.(?:md|py|toml))`")

# Match markdown link targets that point at local files (skip http/https/mailto).
_MD_LINK_TARGET = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _extract_local_paths(text: str) -> list[str]:
    """Pull out every plausible local-doc path mentioned in `text`."""
    paths: list[str] = []
    paths.extend(_BACKTICK_LOCAL_PATH.findall(text))
    for target in _MD_LINK_TARGET.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        # Strip anchor fragments + query strings.
        clean = target.split("#", 1)[0].split("?", 1)[0]
        if not clean:
            continue
        paths.append(clean)
    return paths


@pytest.fixture
def vault_manager(tmp_path: Path):
    cfg = HelixConfig()
    cfg.vault = VaultConfig(
        enabled=True,
        path=str(tmp_path / "vault"),
        party_id="",
        fan_out_threshold=5000,
        redact_body=False,
        stale_threshold=0.5,
        traces=VaultTracesConfig(
            enabled=True,
            retention_hours=48,
            max_retention_hours_hard=720,
            max_count=10000,
            rollup_enabled=True,
            rollup_shard="hour",
            prune_interval_minutes=60,
            trigger_only=False,
        ),
    )
    genome = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
    manager = VaultManager(config=cfg, genome=genome)
    manager.start()
    yield manager, Path(cfg.vault.path)
    manager.stop()
    genome.close()


def test_generated_readme_local_doc_paths_resolve(vault_manager):
    """Every local-doc path referenced by the generated vault README must exist.

    Regression for issue #100: PR #37 shipped a README pointing at
    `docs/superpowers/specs/...` which later moved to `docs/archive/...`,
    leaving operators with broken pointers. This test guards against the
    same class of drift on any future doc move.
    """
    _, vault_root = vault_manager
    readme_path = vault_root / "README.md"
    assert readme_path.exists(), "vault README was not generated on start"

    readme_text = readme_path.read_text(encoding="utf-8")
    local_paths = _extract_local_paths(readme_text)

    # Sanity: we should at least be picking up the operator-status pointer.
    assert local_paths, (
        "no local-doc paths extracted from generated README — did the README "
        "format change? If so, update _extract_local_paths in this test."
    )

    missing = [
        p for p in local_paths
        if not (REPO_ROOT / p).exists()
    ]
    assert not missing, (
        f"vault README references local docs that do not exist on disk: {missing}. "
        f"Update helix_context/vault/__init__.py._write_readme or restore the "
        f"missing file."
    )


def test_claude_code_doc_local_paths_resolve():
    """Same guard applied to the Claude Code operator doc.

    `docs/clients/claude-code.md` is the primary operator-facing intro and
    must not contain broken local pointers either.
    """
    doc = REPO_ROOT / "docs" / "clients" / "claude-code.md"
    text = doc.read_text(encoding="utf-8")
    local_paths = _extract_local_paths(text)

    missing: list[str] = []
    for raw in local_paths:
        # The doc lives at docs/clients/claude-code.md, so relative paths
        # like ../ops/foo.md should resolve from the doc's directory.
        candidate = (doc.parent / raw).resolve() if raw.startswith((".", "/")) \
            else (REPO_ROOT / raw)
        # Also accept absolute-from-repo paths verbatim.
        if not candidate.exists() and not (REPO_ROOT / raw).exists():
            # Try resolving from doc's directory as a fallback.
            alt = (doc.parent / raw).resolve()
            if not alt.exists():
                missing.append(raw)
    assert not missing, (
        f"docs/clients/claude-code.md references local paths that do not "
        f"exist: {missing}. Update the doc or restore the missing files."
    )

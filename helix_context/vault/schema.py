"""Frontmatter shape, filename derivation, and path safety helpers.

The frontmatter is the load-bearing surface for vault interop.
Computed fields are helix-authoritative (read-only in the vault).
Authored fields are operator-editable starting in v1.1; in v1 they are
rendered as cosmetic placeholders for forward-compat.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# ── Field classification ────────────────────────────────────────────────

COMPUTED_FIELDS = frozenset({
    "gene_id",
    "chromatin",
    "domains",
    "content_type",
    "source_id",
    "source_lines",
    "content_sha256",
    "last_seen",
    "last_seen_ts",
    "live_truth_score",
    "co_activation_partners",
    "party_id",
    "participant_handle",
})

AUTHORED_FIELDS = frozenset({
    "operator_notes",
    "operator_tags",
    "pinned",
    "quarantine_reason",
    "supersedes",
    "contradicts",
    "implements",
    "documented_by",
    "tests",
})

assert COMPUTED_FIELDS.isdisjoint(AUTHORED_FIELDS), \
    "field classification overlap — must be disjoint"


def authored_placeholders() -> dict:
    """Default values for cosmetic authored fields in v1.

    These render every export. v1.1 will populate them from
    gene_attribution.notes via the validator; in v1 they're forward-compat.
    """
    return {
        "operator_notes": "",
        "operator_tags": [],
        "pinned": False,
        "quarantine_reason": None,
        "supersedes": [],
        "contradicts": [],
        "implements": [],
        "documented_by": [],
        "tests": [],
    }


# ── Filename derivation ─────────────────────────────────────────────────

_SHORT_ID_LEN = 6


def derive_gene_filename(source_id: str, gene_id: str) -> str:
    """Derive a vault-side filename from source path + gene_id.

    Pattern: <source_stem>-<short_id>.md
    """
    stem = Path(source_id).stem if Path(source_id).suffix else Path(source_id).name
    short = gene_id[:_SHORT_ID_LEN]
    return f"{stem}-{short}.md"


def derive_gene_relpath(*, domain: Optional[str], source_id: str, gene_id: str) -> str:
    """Vault-relative path for a gene: genes/<domain>/<filename>.

    If domain is None or empty, falls back to genes/_orphan/.
    """
    sub = domain if domain else "_orphan"
    return f"genes/{sub}/{derive_gene_filename(source_id, gene_id)}"


# ── Path safety ──────────────────────────────────────────────────────────

def safe_resolve_under(vault_root: Path, candidate: Path) -> Path:
    """Resolve `candidate` and assert it lives under vault_root.

    Raises ValueError if the candidate would escape the vault root.
    """
    root = Path(vault_root).resolve()
    target = Path(candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"{candidate} resolves outside vault root {root}")
    return target

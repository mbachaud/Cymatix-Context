"""Regression tests for #146 — `<GENE src=...>` source-prefix preservation.

The renderer in ``HelixContextManager.build_context`` shortens
``gene.source_id`` into the ``src=...`` attribute of each `<GENE>` block.
Before the fix, the shortener:

  1. Looked for a ``Projects`` segment and sliced after it, OR
  2. Fell back to the last 3 path components.

The (2) fallback dropped the canonical ``<source_type>/`` prefix for any
path layered as ``<root>/sources/<source_type>/<sub>/<file>`` whose
``<sub>`` was a directory (i.e., > 3 segments below the boundary). That
hit ~30% of confluence paths in the enterprise_rag_* fixtures and any
similarly-deep gmail/jira/github tree, and propagated as truncated
citations into the answerer prompt and downstream lookups.

The fix prefers a ``sources`` marker over both prior branches so the
source-type prefix (``confluence/``, ``github/``, etc.) is always
preserved verbatim in `<GENE src=...>`.
"""

from __future__ import annotations

import pytest

from helix_context.config import BudgetConfig
from helix_context.context_manager import HelixContextManager
from tests.conftest import MockCompressorBackend, make_gene, make_helix_config


@pytest.fixture
def manager():
    """Manager with mock backend + in-memory genome.

    The ribosome backend is mocked so build_context does not try to call a
    real LLM; the path-shortening branch we exercise sits in the splice
    loop, which runs unconditionally once candidates are selected.
    """
    cfg = make_helix_config(
        budget=BudgetConfig(max_genes_per_turn=4, splice_aggressiveness=0.5),
    )
    mgr = HelixContextManager(cfg)
    mgr.ribosome.backend = MockCompressorBackend()
    yield mgr
    mgr.close()


def _seed_with_source(mgr, source_id: str, *, gene_id: str = "src_prefix_test_0001"):
    """Seed exactly one gene with a controlled ``source_id``.

    The single-gene seeding keeps the splice loop deterministic — the
    rendered `<GENE>` tag is the only one in the expressed context.
    """
    g = make_gene(
        "Document about confluence audit logging retention",
        domains=["confluence", "audit"],
        entities=["confluence", "audit", "retention"],
        gene_id=gene_id,
    )
    g.source_id = source_id
    mgr.genome.upsert_gene(g)
    return g


def _render_src_for(mgr, source_id: str) -> str:
    """Drive build_context with a query that matches the seeded gene and
    return the rendered ``<GENE src=...>`` attribute body (or empty
    string if no src= was emitted)."""
    _seed_with_source(mgr, source_id)
    win = mgr.build_context("confluence audit retention")
    body = win.expressed_context
    # Extract the src="..." attribute from the first <GENE ...> tag.
    import re
    m = re.search(r'<GENE\b[^>]*\bsrc="([^"]*)"', body)
    return m.group(1) if m else ""


# ── #146 regression cases ────────────────────────────────────────────


def test_confluence_path_preserves_source_type_prefix(manager):
    """A canonical ``F:/tmp/.../sources/confluence/<sub>/<file>`` path
    must render with ``confluence/`` retained — the prior fallback
    sliced to ``parts[-3:]`` and stripped it."""
    src = (
        r"F:\tmp\enterprise_rag_10k\sources\confluence\architecture-and-standards"
        r"\decision-records\adr-015-admission-control.json"
    )
    out = _render_src_for(manager, src)
    assert out == (
        "confluence/architecture-and-standards/decision-records/"
        "adr-015-admission-control.json"
    ), out


def test_gmail_path_does_not_get_literal_sources_prefix(manager):
    """The bug also produced ``sources/gmail/...`` (literal ``sources/``
    prefix on a path that was already only 3 segments deep below
    ``sources/``). After the fix, the ``sources`` segment itself is
    stripped while the ``gmail/`` source-type prefix is retained."""
    src = (
        r"F:\tmp\enterprise_rag_10k\sources\gmail"
        r"\20250610-private-upgrade-audit-log-requirements.json"
    )
    out = _render_src_for(manager, src)
    assert out == "gmail/20250610-private-upgrade-audit-log-requirements.json", out


def test_eng_private_deployments_path_keeps_source_type(manager):
    """``eng-private-deployments`` is one of the source-type subdirs
    listed in the bug report. The deep-path case must keep it."""
    src = (
        r"F:\tmp\enterprise_rag_10k\sources\eng-private-deployments"
        r"\deployment-guides\private-audit-log-export-config.json"
    )
    out = _render_src_for(manager, src)
    assert out == (
        "eng-private-deployments/deployment-guides/"
        "private-audit-log-export-config.json"
    ), out


def test_company_misc_path_keeps_source_type(manager):
    """``company/misc/...`` was reported stripped to just
    ``company/misc/<file>``; after the fix it is fully preserved."""
    src = (
        r"F:\tmp\enterprise_rag_10k\sources\company\misc"
        r"\coverage-gap-census-lite-oct-2031.json"
    )
    out = _render_src_for(manager, src)
    assert out == "company/misc/coverage-gap-census-lite-oct-2031.json", out


# ── Defense-in-depth: existing behaviors must not regress ────────────


def test_projects_path_still_short_circuits_to_projects_index(manager):
    """``F:/Projects/<repo>/<sub>/<file>`` is the dev-laptop layout.
    Without a ``sources`` segment the shortener still slices after the
    ``Projects`` segment exactly as before."""
    src = r"F:\Projects\helix-context\helix_context\context_manager.py"
    out = _render_src_for(manager, src)
    assert out == "helix-context/helix_context/context_manager.py", out


def test_projects_with_nested_sources_prefers_sources_anchor(manager):
    """When BOTH ``Projects`` and ``sources`` segments are present (e.g.
    the upstream ``F:/Projects/EnterpriseRAG-Bench-main/generated_data/
    sources/...`` layout described in the corpus matrix), the ``sources``
    anchor takes precedence so the source-type prefix is preserved."""
    src = (
        r"F:\Projects\EnterpriseRAG-Bench-main\generated_data\sources\confluence"
        r"\architecture-and-standards\decision-records\adr-015.json"
    )
    out = _render_src_for(manager, src)
    assert out == (
        "confluence/architecture-and-standards/decision-records/adr-015.json"
    ), out


def test_short_non_sources_path_falls_back_to_parts3(manager):
    """A path with neither ``sources`` nor ``Projects`` still falls back
    to the legacy ``parts[-3:]`` behavior — the fallback is only wrong
    for the deep-``sources`` case, which the new branch catches first.

    The path is constructed to include the retrieval-side query tokens
    (``confluence``, ``audit``) so the synthetic gene clears the
    promoter-match floor and a `<GENE>` tag is actually emitted —
    otherwise build_context short-circuits to a `<helix:no_match>`
    token before the shortener ever runs.
    """
    src = "/var/log/confluence/audit/output.json"
    out = _render_src_for(manager, src)
    assert out == "confluence/audit/output.json", out


def test_underscore_prefixed_source_id_emits_no_src_attr(manager):
    """Sentinel / synthetic source_ids that start with ``_`` (e.g.
    ``_inline``, ``_replication``) are explicitly excluded from the
    shortener and must not emit ``src=`` at all."""
    src = "_inline_synthetic"
    out = _render_src_for(manager, src)
    assert out == "", out

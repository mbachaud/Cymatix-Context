"""Ratchets for issue #219 slice 4 ("knob-docs single source of truth").

``docs/config-reference.md`` drifted from ``helix_context/config.py`` more
than once (PRs #261 / #262 both had to hand-patch stale knob tables after
adding fields to the config dataclasses). This module is the guard rail:

1. ``test_generated_regions_are_in_sync_with_config_py`` /
   ``test_doc_is_byte_identical_to_a_fresh_regenerate`` regenerate every
   ``<!-- BEGIN GENERATED: config-tables:NAME -->`` region in
   ``docs/config-reference.md`` in-memory from the *current*
   ``helix_context/config.py`` (via ``scripts/gen_config_reference.py``)
   and assert the committed file already reflects that output. A config.py
   field add/remove/rename/re-comment that isn't followed by
   ``python scripts/gen_config_reference.py`` fails here.
2. ``test_gen_config_reference_output_is_deterministic`` re-runs the
   generator twice in the same process and asserts identical output
   (byte-stable / idempotent regeneration, per the issue #219 slice-4
   requirement).
3. ``test_claude_md_config_table_keys_exist`` parses CLAUDE.md's
   ``[section] | key settings`` config table and asserts every key it
   mentions is a real field on the corresponding ``config.py`` dataclass.
   Parsing is intentionally lenient (see ``extract_candidate_keys``) —
   missing a key hidden in free prose is fine; flagging a key that
   doesn't exist is the point.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"


def _load_generator():
    """Import scripts/gen_config_reference.py the way this repo's tests do
    for other scripts/ modules (see tests/test_dehardcode_207.py)."""
    scripts_dir = str(REPO_ROOT / "scripts")
    sys.path.insert(0, scripts_dir)
    try:
        import gen_config_reference  # type: ignore

        return gen_config_reference
    finally:
        sys.path.remove(scripts_dir)


gen = _load_generator()


# ---------------------------------------------------------------------------
# 1 & 2. Generated-region sync + determinism ratchets
# ---------------------------------------------------------------------------


def test_generated_regions_are_in_sync_with_config_py():
    module = gen.load_config_module()
    source = gen.CONFIG_PY_PATH.read_text(encoding="utf-8")
    fresh_tables = gen.generate_all_tables(module, source)

    doc_text = gen.DOC_PATH.read_text(encoding="utf-8")
    on_disk_regions = gen.extract_generated_regions(doc_text)

    assert set(on_disk_regions) == set(fresh_tables), (
        "docs/config-reference.md's <!-- BEGIN GENERATED: ... --> region "
        "names don't match scripts/gen_config_reference.py's "
        "SECTION_TO_CLASS / SUBTABLE_MARKERS registry -- a marker was "
        "added/removed in the doc, or a section was added/removed from "
        "the registry, without updating the other side.\n"
        f"doc regions:  {sorted(on_disk_regions)}\n"
        f"generator:    {sorted(fresh_tables)}"
    )

    stale = sorted(
        name for name, fresh in fresh_tables.items() if on_disk_regions[name] != fresh
    )
    assert not stale, (
        "docs/config-reference.md is stale relative to helix_context/config.py "
        f"in generated region(s): {stale}\n"
        "Run `python scripts/gen_config_reference.py` and commit the result."
    )


def test_doc_is_byte_identical_to_a_fresh_regenerate():
    """End-to-end: regenerate the whole doc and diff against what's committed."""
    fresh_doc_text = gen.regenerate_doc_text()
    current_doc_text = gen.DOC_PATH.read_text(encoding="utf-8")
    assert fresh_doc_text == current_doc_text, (
        "docs/config-reference.md does not match a fresh run of "
        "`python scripts/gen_config_reference.py` -- regenerate and commit."
    )


def test_gen_config_reference_output_is_deterministic():
    """Re-running the generator must be byte-stable (stable field ordering,
    no dict/set iteration nondeterminism) -- issue #219 slice-4 requirement."""
    module = gen.load_config_module()
    source = gen.CONFIG_PY_PATH.read_text(encoding="utf-8")

    tables_a = gen.generate_all_tables(module, source)
    tables_b = gen.generate_all_tables(module, source)
    assert tables_a == tables_b

    doc_text = gen.DOC_PATH.read_text(encoding="utf-8")
    applied_a = gen.apply_generated_regions(doc_text, tables_a)
    applied_b = gen.apply_generated_regions(doc_text, tables_b)
    assert applied_a == applied_b

    # Applying again to already-regenerated text must be a no-op (idempotent).
    applied_twice = gen.apply_generated_regions(applied_a, tables_a)
    assert applied_twice == applied_a


def test_every_helix_config_section_has_a_registry_entry():
    """Every field on HelixConfig (i.e. every real [section]) must be
    accounted for in SECTION_TO_CLASS, so a brand-new config section can
    never silently ship without a generated table."""
    module = gen.load_config_module()
    helix_config_fields = {f.name for f in dataclasses.fields(module.HelixConfig)}
    # synonym_map has no fixed field set (free-form Dict[str, List[str]])
    # and is intentionally not table-generated.
    accounted = set(gen.SECTION_TO_CLASS) | {"synonym_map"}
    missing = helix_config_fields - accounted
    assert not missing, (
        f"HelixConfig field(s) {sorted(missing)} have no entry in "
        "gen_config_reference.SECTION_TO_CLASS -- add one (or None if the "
        "section is intentionally not a generated table)."
    )


# ---------------------------------------------------------------------------
# 3. CLAUDE.md config-table key-existence ratchet
# ---------------------------------------------------------------------------

_ROW_RE = re.compile(r"^\|\s*`\[([a-zA-Z_]+)\]`\s*\|(.*)\|\s*$")
_BACKTICK_BARE_IDENT_RE = re.compile(r"`([a-z_][a-z0-9_]*)`")
_LEADING_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*")


def parse_claude_md_config_rows(text: str) -> List[Tuple[str, str]]:
    """Extract (section, cell_text) pairs from CLAUDE.md's Configuration table.

    Locates the table by its header row (`| Section | Key settings |`) so
    we don't accidentally scan unrelated tables (package structure, HTTP
    endpoints, etc.) elsewhere in the file.
    """
    rows: List[Tuple[str, str]] = []
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if not in_table:
            if stripped.startswith("| Section") and "Key settings" in stripped:
                in_table = True
            continue
        if stripped.startswith("|---"):
            continue
        m = _ROW_RE.match(stripped)
        if m:
            rows.append((m.group(1), m.group(2).strip()))
            continue
        # First non-matching, non-empty-table-row line ends the table.
        break
    return rows


def _top_level_comma_split(text: str) -> List[str]:
    """Split on commas that are not nested inside ( ) or [ ] groups."""
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in text:
        if ch in "([":
            depth += 1
            current.append(ch)
        elif ch in ")]":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def extract_candidate_keys(cell_text: str) -> Set[str]:
    """Best-effort extraction of config key names from a CLAUDE.md cell.

    Lenient by design (false negatives -- missed keys -- are fine; false
    positives that would fail the ratchet on prose are not):

    - Any backticked bare lowercase identifier anywhere in the cell (e.g.
      `` `enabled` ``) -- this is the "backticked key names" convention
      the task asked for.
    - The leading identifier of each top-level comma-separated segment
      (parens/brackets don't count as top-level commas), e.g. in
      "model, backend (`"ollama"` / ...), timeout" the segments are
      "model", "backend (...)", "timeout" and the leading identifiers are
      model / backend / timeout. Segments that start with capitalized
      prose ("Rule-based query classifier: ...") are skipped because the
      identifier regex requires a lowercase start.
    """
    candidates: Set[str] = set()
    for m in _BACKTICK_BARE_IDENT_RE.finditer(cell_text):
        candidates.add(m.group(1))
    for segment in _top_level_comma_split(cell_text):
        segment = segment.strip().lstrip("`").strip()
        m = _LEADING_IDENT_RE.match(segment)
        if m:
            candidates.add(m.group(0))
    return candidates


def check_claude_md_config_table(gen_module) -> Tuple[List[str], List[str]]:
    """Return (failures, skipped) for every row of CLAUDE.md's config table.

    failures: "[section] key" strings for keys that don't exist as a field
    on the section's config.py dataclass -- these fail the test.
    skipped: sections with no corresponding dataclass (documented
    exceptions like mem_sync/synonyms, or an unrecognized section name) --
    informational only, never fails the test.
    """
    claude_text = CLAUDE_MD.read_text(encoding="utf-8")
    rows = parse_claude_md_config_rows(claude_text)
    assert rows, "Could not locate CLAUDE.md's `| Section | Key settings |` config table"

    module = gen_module.load_config_module()
    failures: List[str] = []
    skipped: List[str] = []

    for section, cell in rows:
        if section not in gen_module.SECTION_TO_CLASS:
            skipped.append(f"[{section}] (unrecognized section)")
            continue
        class_name = gen_module.SECTION_TO_CLASS[section]
        if class_name is None:
            skipped.append(f"[{section}] (no config.py dataclass -- documented exception)")
            continue
        cls = getattr(module, class_name)
        field_names = {f.name for f in dataclasses.fields(cls)}
        for key in sorted(extract_candidate_keys(cell)):
            if key not in field_names:
                failures.append(f"[{section}] {key}")

    return failures, skipped


def test_claude_md_config_table_keys_exist():
    failures, _skipped = check_claude_md_config_table(gen)
    assert not failures, (
        "CLAUDE.md's `[section] | Key settings` config table mentions "
        f"key(s) that don't exist on the matching config.py dataclass: "
        f"{failures}. Fix the key name in CLAUDE.md, or add the field to "
        "helix_context/config.py if this is genuinely a new knob."
    )

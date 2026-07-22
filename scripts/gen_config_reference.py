"""Generate the per-section knob tables in docs/config-reference.md.

Slice 4 of epic #219 ("knob-docs single source of truth"). PRs #261/#262
both had to hand-patch stale tables in docs/config-reference.md after
adding fields to cymatix_context/config.py — this script is the fix: it
introspects the config dataclasses (via ``dataclasses.fields()``, the
same reflection ``load_config()`` itself relies on for ``_warn_unknown``)
and regenerates the "Key / Type / Default / Description" table for each
``[section]``, so the tables can never silently drift from the code
again.

Descriptions are harvested from the comments living in
``cymatix_context/config.py`` next to each field:

1. A trailing ``# ...`` comment on the field's own line (or its last
   physical line, for multi-line ``field(...)`` defaults) wins first.
2. Otherwise, the contiguous block of ``#``-only comment lines
   immediately above the field is used.
3. Otherwise the description is left empty — harvesting is best-effort,
   not a requirement (see the module CLAUDE.md / issue #219 slice-4
   description).

Nested dataclass fields (a field whose resolved default is itself a
dataclass instance, e.g. ``VaultConfig.traces``, or whose annotation is
``Dict[str, SomeDataclass]``, e.g. ``AbstainConfig.per_class``) are
TOML sub-tables, not scalar keys — they are excluded from their
parent's table and rendered as their own named sub-table instead (see
``SUBTABLE_MARKERS``).

Usage::

    python scripts/gen_config_reference.py           # regenerate docs/config-reference.md in place
    python scripts/gen_config_reference.py --check    # exit 1 if the doc is stale (no write)
    python scripts/gen_config_reference.py --print NAME   # print one generated table to stdout

No third-party dependencies — stdlib only (ast, dataclasses, importlib,
tokenize).
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import importlib.util
import re
import sys
import tokenize
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY_PATH = REPO_ROOT / "cymatix_context" / "config.py"
DOC_PATH = REPO_ROOT / "docs" / "config-reference.md"

MARKER_PREFIX = "config-tables:"
BEGIN_RE = re.compile(
    r"<!-- BEGIN GENERATED: " + re.escape(MARKER_PREFIX) + r"([A-Za-z0-9_.]+) -->"
)
END_MARKER = "<!-- END GENERATED -->"

# ---------------------------------------------------------------------------
# [section] -> dataclass name, mirroring load_config()'s raw["section"]
# lookups / HelixConfig's field declarations. ``None`` marks a section that
# is documented in helix.toml but intentionally NOT a config.py dataclass
# (consumed by a standalone script instead) — no table is generated for it.
# ---------------------------------------------------------------------------
SECTION_TO_CLASS: Dict[str, Optional[str]] = {
    "ribosome": "RibosomeConfig",
    "hardware": "Hardware",
    "budget": "BudgetConfig",
    "session": "SessionConfig",
    "genome": "GenomeConfig",
    "server": "ServerConfig",
    "telemetry": "TelemetryConfig",
    "headroom": "HeadroomConfig",
    "ingestion": "IngestionConfig",
    "context": "ContextConfig",
    "cymatics": "CymaticsConfig",
    "classifier": "ClassifierConfig",
    "retrieval": "RetrievalConfig",
    "plr": "PLRConfig",
    "know": "KnowConfig",
    "abstain": "AbstainConfig",
    "vault": "VaultConfig",
    "mem_sync": None,  # scripts/run_mem_sync.py reads this section itself
    "synonyms": None,  # free-form Dict[str, List[str]] -> HelixConfig.synonym_map
}

# Extra named sub-tables for nested-dataclass fields, keyed by the marker
# name used in docs/config-reference.md.
SUBTABLE_MARKERS: Dict[str, str] = {
    "abstain.subtable": "AbstainClassFloors",
    "vault.traces": "VaultTracesConfig",
}

TABLE_HEADER = "| Key | Type | Default | Description |\n|---|---|---|---|"


def load_config_module() -> ModuleType:
    """Load cymatix_context/config.py as a standalone module (no package deps)."""
    spec = importlib.util.spec_from_file_location(
        "_gen_config_reference_config_module", CONFIG_PY_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load spec for {CONFIG_PY_PATH}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses._process_class needs the module registered in sys.modules
    # (it resolves ClassVar/InitVar via sys.modules[cls.__module__]).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Comment harvesting
# ---------------------------------------------------------------------------


def _standalone_comment_lines(source: str) -> Dict[int, str]:
    """Return {line_no: comment_text} for lines that are ONLY a comment."""
    lines_with_code: set = set()
    comments: Dict[int, str] = {}
    tokens = tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__)
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            comments[tok.start[0]] = tok.string
        elif tok.type not in (
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENCODING,
            tokenize.ENDMARKER,
        ):
            lines_with_code.add(tok.start[0])
    return {ln: txt for ln, txt in comments.items() if ln not in lines_with_code}


def _trailing_comments(source: str) -> Dict[int, str]:
    """Return {line_no: comment_text} for a trailing comment on a code line."""
    lines_with_code: set = set()
    comments: Dict[int, str] = {}
    tokens = tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__)
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            comments[tok.start[0]] = tok.string
        elif tok.type not in (
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENCODING,
            tokenize.ENDMARKER,
        ):
            lines_with_code.add(tok.start[0])
    return {ln: txt for ln, txt in comments.items() if ln in lines_with_code}


def _clean_comment(text: str) -> str:
    text = text.strip()
    if text.startswith("#"):
        text = text[1:]
    return text.strip()


def harvest_descriptions(source: str) -> Dict[str, Dict[str, str]]:
    """Map ``{class_name: {field_name: description}}`` from config.py source.

    Trailing comments win; otherwise the contiguous comment block directly
    above the field is used; otherwise the description is "".
    """
    standalone = _standalone_comment_lines(source)
    trailing = _trailing_comments(source)
    lines = source.splitlines()

    tree = ast.parse(source)
    result: Dict[str, Dict[str, str]] = {}

    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        is_dataclass = any(
            (isinstance(dec, ast.Name) and dec.id == "dataclass")
            or (isinstance(dec, ast.Attribute) and dec.attr == "dataclass")
            for dec in node.decorator_list
        )
        if not is_dataclass:
            continue
        field_descriptions: Dict[str, str] = {}
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(
                stmt.target, ast.Name
            ):
                continue
            field_name = stmt.target.id
            end_line = stmt.end_lineno or stmt.lineno
            desc = ""
            if end_line in trailing:
                desc = _clean_comment(trailing[end_line])
            else:
                collected: List[str] = []
                probe = stmt.lineno - 1
                while probe >= 1 and probe in standalone:
                    collected.append(_clean_comment(standalone[probe]))
                    probe -= 1
                if collected:
                    collected.reverse()
                    desc = " ".join(c for c in collected if c)
            field_descriptions[field_name] = desc
        result[node.name] = field_descriptions

    return result


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------


def _format_inner(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    if value is None:
        return "None"
    return str(value)


def format_default(value: Any) -> str:
    if isinstance(value, bool):
        return "`true`" if value else "`false`"
    if isinstance(value, str):
        return f'`"{value}"`'
    if value is None:
        return "`None`"
    if isinstance(value, (list, tuple)):
        if not value:
            return "`[]`"
        return "`[" + ", ".join(_format_inner(v) for v in value) + "]`"
    if isinstance(value, dict):
        if not value:
            return "`{}`"
        inner = ", ".join(f"{k} = {_format_inner(v)}" for k, v in value.items())
        return "`{" + inner + "}`"
    return f"`{value!r}`"


def resolve_default(field: "dataclasses.Field") -> Any:
    if field.default is not dataclasses.MISSING:
        return field.default
    if field.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return field.default_factory()
    return dataclasses.MISSING


_DICT_OF_DATACLASS_RE = re.compile(r"^Dict\[str,\s*([A-Za-z_][A-Za-z0-9_]*)\]$")


def _is_dict_of_dataclass(type_str: str, module: ModuleType) -> bool:
    m = _DICT_OF_DATACLASS_RE.match(type_str.strip())
    if not m:
        return False
    inner = getattr(module, m.group(1), None)
    return isinstance(inner, type) and dataclasses.is_dataclass(inner)


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def build_table(
    module: ModuleType,
    class_name: str,
    descriptions: Dict[str, Dict[str, str]],
) -> str:
    cls = getattr(module, class_name)
    rows: List[Tuple[str, str, str, str]] = []
    for f in dataclasses.fields(cls):
        default = resolve_default(f)
        if dataclasses.is_dataclass(default) and not isinstance(default, type):
            continue  # nested single sub-table — rendered separately
        if _is_dict_of_dataclass(f.type, module):
            continue  # nested keyed sub-table — rendered separately
        default_repr = (
            "*(required)*" if default is dataclasses.MISSING else format_default(default)
        )
        desc = descriptions.get(class_name, {}).get(f.name, "")
        rows.append((f.name, f.type, default_repr, _escape_cell(desc)))

    lines = [TABLE_HEADER]
    for name, type_str, default_repr, desc in rows:
        lines.append(f"| `{name}` | `{type_str}` | {default_repr} | {desc} |")
    return "\n".join(lines)


def generate_all_tables(module: ModuleType, source: str) -> Dict[str, str]:
    """Return ``{marker_name: generated_table_markdown}`` for every marker."""
    descriptions = harvest_descriptions(source)
    tables: Dict[str, str] = {}
    for section, class_name in SECTION_TO_CLASS.items():
        if class_name is None:
            continue
        tables[section] = build_table(module, class_name, descriptions)
    for marker_name, class_name in SUBTABLE_MARKERS.items():
        tables[marker_name] = build_table(module, class_name, descriptions)
    return tables


# ---------------------------------------------------------------------------
# Doc region substitution
# ---------------------------------------------------------------------------


def apply_generated_regions(doc_text: str, tables: Dict[str, str]) -> str:
    """Replace the content between each BEGIN/END marker pair in-place.

    Raises ValueError if a marker references a name with no generated table,
    or if a BEGIN marker has no matching END marker — this is a hard failure
    by design so an editing mistake in the doc can never silently ship an
    empty / stale region.
    """
    out: List[str] = []
    pos = 0
    seen: set = set()
    for m in BEGIN_RE.finditer(doc_text):
        name = m.group(1)
        if name not in tables:
            raise ValueError(
                f"docs/config-reference.md references unknown generated "
                f"region '{name}' with no matching table in "
                f"gen_config_reference.SECTION_TO_CLASS / SUBTABLE_MARKERS"
            )
        end_idx = doc_text.find(END_MARKER, m.end())
        if end_idx == -1:
            raise ValueError(f"No matching '{END_MARKER}' for region '{name}'")
        out.append(doc_text[pos : m.end()])
        out.append("\n" + tables[name] + "\n")
        pos = end_idx
        seen.add(name)
    out.append(doc_text[pos:])
    return "".join(out)


def extract_generated_regions(doc_text: str) -> Dict[str, str]:
    """Return ``{marker_name: current_region_body}`` as currently on disk."""
    regions: Dict[str, str] = {}
    for m in BEGIN_RE.finditer(doc_text):
        name = m.group(1)
        end_idx = doc_text.find(END_MARKER, m.end())
        if end_idx == -1:
            raise ValueError(f"No matching '{END_MARKER}' for region '{name}'")
        regions[name] = doc_text[m.end() : end_idx].strip("\n")
    return regions


def regenerate_doc_text() -> str:
    module = load_config_module()
    source = CONFIG_PY_PATH.read_text(encoding="utf-8")
    tables = generate_all_tables(module, source)
    doc_text = DOC_PATH.read_text(encoding="utf-8")
    return apply_generated_regions(doc_text, tables)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if docs/config-reference.md is stale; do not write.",
    )
    parser.add_argument(
        "--print",
        dest="print_name",
        metavar="NAME",
        help="Print one generated region's table to stdout and exit.",
    )
    args = parser.parse_args(argv)

    if args.print_name:
        module = load_config_module()
        source = CONFIG_PY_PATH.read_text(encoding="utf-8")
        tables = generate_all_tables(module, source)
        if args.print_name not in tables:
            print(f"Unknown region: {args.print_name}", file=sys.stderr)
            return 2
        print(tables[args.print_name])
        return 0

    new_text = regenerate_doc_text()
    current_text = DOC_PATH.read_text(encoding="utf-8")

    if args.check:
        if new_text != current_text:
            print(
                f"{DOC_PATH} is stale relative to cymatix_context/config.py — "
                f"run `python scripts/gen_config_reference.py` to refresh.",
                file=sys.stderr,
            )
            return 1
        print(f"{DOC_PATH} is up to date.")
        return 0

    if new_text != current_text:
        DOC_PATH.write_text(new_text, encoding="utf-8")
        print(f"Regenerated {DOC_PATH}")
    else:
        print(f"{DOC_PATH} already up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

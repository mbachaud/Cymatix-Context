"""OKF bundle reader — walk a directory, parse concepts, capture links.

Spec facts this module encodes (OKF v0.1 @ ee67a5ca):

- A bundle is a plain directory tree of UTF-8 markdown files (§3).
  Tarball/zip ingestion was cut by the council (Amendment 9) — callers
  unpack archives themselves.
- ``index.md`` and ``log.md`` are reserved at ANY level and are never
  concept documents (§3.1). The bundle-root ``index.md`` MAY carry a
  frontmatter block solely to declare ``okf_version`` (§11); index
  files otherwise contain no frontmatter (§6).
- Concept ID = bundle-relative POSIX path with the ``.md`` suffix
  removed (§2).
- Conformance requires exactly ONE frontmatter field: a non-empty
  ``type`` (§9). The upstream reference implementation validates four
  fields; that is deliberately NOT copied here (council Amendment 6).

Degradation policy — this is HELIX'S OWN policy, not the spec's (§9
covers missing optional fields / unknown types / broken links, and is
silent on missing or unparseable frontmatter and empty ``type``):

- Missing frontmatter block        → ingest as generic document, warn.
- Unparseable / non-mapping YAML   → strip the delimited block if its
  boundaries are recognizable (it is metadata, however broken), ingest
  the rest as a generic document, warn.
- Empty or non-string ``type``     → generic document, warn; the other
  frontmatter fields are still honored.
- Non-UTF-8 file                   → skip the file (the spec requires
  UTF-8 and content cannot be trusted), warn, bundle continues.

Nothing is fatal at bundle level; every warning carries the file path.

Determinism note: bodies are newline-normalized to LF (and a leading
BOM stripped) before hashing or ingestion, so a bundle checked out with
CRLF translation on Windows produces the same canonical digest as an
LF checkout on Linux.
"""

from __future__ import annotations

import datetime as _dt
import logging
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("helix.okf")

RESERVED_FILENAMES = frozenset({"index.md", "log.md"})

# Frontmatter core fields with dedicated mappings; everything else is a
# producer extension and flows into key_values (scalars) and the stored
# frontmatter dict (everything).
_CORE_FIELDS = frozenset({"type", "title", "description", "tags"})

_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
# Standard inline markdown link. Images (![alt](src)) are excluded via
# the lookbehind — an embedded image is not a concept relationship.
_LINK_RE = re.compile(r"(?<!\!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# URL scheme prefix (http:, https:, mailto:, ...) — external citation,
# not a bundle cross-link.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True)
class OkfLink:
    """One captured concept→concept link (already normalized)."""

    target_concept_id: str
    link_text: str


@dataclass(frozen=True)
class OkfConcept:
    """One parsed concept document."""

    concept_id: str                 # bundle-relative POSIX path minus .md
    source_path: str                # bundle-relative POSIX path with .md
    body: str                       # frontmatter-stripped, LF-normalized
    raw_type: Optional[str]         # frontmatter `type` verbatim (None = degraded)
    title: Optional[str]
    description: Optional[str]
    tags: Tuple[str, ...]           # frontmatter `tags`
    key_values: Tuple[str, ...]     # "key=value" for scalar extension fields
    frontmatter: Dict[str, Any]     # full parsed frontmatter, JSON-safe
    links: Tuple[OkfLink, ...]      # captured cross-links, document order


@dataclass
class OkfBundle:
    root: Path
    bundle_id: str
    okf_version: Optional[str]
    concepts: List[OkfConcept]      # sorted by concept_id
    warnings: List[str] = field(default_factory=list)
    skipped_files: List[str] = field(default_factory=list)


def _normalize_text(raw: str) -> str:
    """Strip a leading BOM and normalize newlines to LF."""
    if raw.startswith("﻿"):
        raw = raw[1:]
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def split_frontmatter(text: str) -> Tuple[Optional[str], str, Optional[str]]:
    """Split *text* into (frontmatter_block, body, boundary_error).

    Returns the raw YAML block (without delimiters) or None when the
    file has no recognizable frontmatter. ``boundary_error`` is set when
    an opening ``---`` exists but no closing delimiter was found.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text, None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            block = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:]).lstrip("\n")
            return block, body, None
    return None, text, "unterminated frontmatter block (no closing ---)"


def _json_safe(value: Any) -> Any:
    """Coerce YAML-native values (datetimes, dates) to JSON-safe types."""
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def parse_frontmatter_block(block: str) -> Optional[Dict[str, Any]]:
    """Parse a YAML frontmatter block into a JSON-safe dict, or None."""
    import yaml  # core dependency (declared in pyproject; also used by vault/)

    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(k): _json_safe(v) for k, v in parsed.items()}


def _extract_link_lines(body: str) -> List[str]:
    """Body lines with fenced code blocks (``` / ~~~) blanked out."""
    out: List[str] = []
    fence: Optional[str] = None
    for line in body.split("\n"):
        m = _FENCE_RE.match(line)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            out.append("")
            continue
        out.append("" if fence is not None else line)
    return out


def normalize_link_target(raw_target: str, source_path: str) -> Optional[str]:
    """Normalize a markdown link target to a concept ID, or None.

    Only ``.md``-suffixed, non-external, non-reserved targets are
    concept links (spec §5 examples link WITH ``.md``; concept IDs strip
    it). Absolute ``/...`` targets are bundle-relative; relative targets
    resolve against the source concept's directory.
    """
    target = raw_target.strip().strip("<>")
    target = target.split("#", 1)[0]
    if not target or _SCHEME_RE.match(target):
        return None
    if not target.lower().endswith(".md"):
        return None
    if posixpath.basename(target).lower() in RESERVED_FILENAMES:
        return None
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(source_path), target)
        )
    # A target that escapes the bundle root cannot resolve to a concept;
    # keep it (dangling) so the link is still recorded losslessly.
    return resolved[: -len(".md")] if resolved.lower().endswith(".md") else resolved


def extract_links(body: str, source_path: str) -> List[OkfLink]:
    """Capture concept→concept links from *body*, in document order."""
    links: List[OkfLink] = []
    for line in _extract_link_lines(body):
        for m in _LINK_RE.finditer(line):
            target = normalize_link_target(m.group(2), source_path)
            if target is not None:
                links.append(OkfLink(target_concept_id=target, link_text=m.group(1)))
    return links


def _scalar_key_values(frontmatter: Dict[str, Any]) -> List[str]:
    """"key=value" strings for scalar non-core frontmatter fields."""
    out: List[str] = []
    for key, value in frontmatter.items():
        if key in _CORE_FIELDS or value is None:
            continue
        if isinstance(value, bool):
            out.append(f"{key}={'true' if value else 'false'}")
        elif isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                out.append(f"{key}={text}")
    return out


def _coerce_tags(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(v).strip() for v in value if str(v).strip())


def _coerce_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_okf_version(root: Path, warnings: List[str]) -> Optional[str]:
    """Parse the bundle-root index.md frontmatter solely for okf_version.

    Spec §6: index files carry no frontmatter; §11 carves out the single
    exception — a bundle-root index.md MAY declare ``okf_version``.
    """
    index_path = root / "index.md"
    if not index_path.is_file():
        return None
    try:
        text = _normalize_text(index_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, OSError) as exc:
        warnings.append(f"index.md: unreadable ({exc}); okf_version unknown")
        return None
    block, _body, _err = split_frontmatter(text)
    if block is None:
        return None
    parsed = parse_frontmatter_block(block)
    if not parsed:
        return None
    return _coerce_optional_str(parsed.get("okf_version"))


def _iter_concept_files(root: Path) -> List[Path]:
    """All non-reserved .md files, deterministic order, dot-dirs skipped."""
    files: List[Path] = []
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue  # .git and friends — bundles are often git repos (§3)
        if path.name.lower() in RESERVED_FILENAMES:
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def read_bundle(root: Path | str, bundle_id: Optional[str] = None) -> OkfBundle:
    """Read an OKF bundle from a plain directory.

    Never raises for content problems — degraded files are ingested as
    generic documents (or skipped, for non-UTF-8) with a logged warning;
    see the module docstring for the full policy. Raises ``ValueError``
    only when *root* is not a directory.
    """
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"OKF bundle root is not a directory: {root}")
    resolved_bundle_id = bundle_id or root.resolve().name

    warnings: List[str] = []
    skipped: List[str] = []
    okf_version = _read_okf_version(root, warnings)

    concepts: List[OkfConcept] = []
    for path in _iter_concept_files(root):
        source_path = path.relative_to(root).as_posix()
        concept_id = source_path[: -len(".md")]
        try:
            text = _normalize_text(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            msg = f"{source_path}: not valid UTF-8 (spec §4 requires UTF-8); file skipped"
            log.warning("OKF bundle %s: %s", resolved_bundle_id, msg)
            warnings.append(msg)
            skipped.append(source_path)
            continue

        block, body, boundary_err = split_frontmatter(text)
        frontmatter: Dict[str, Any] = {}
        if boundary_err is not None:
            msg = f"{source_path}: {boundary_err}; ingesting whole file as generic document"
            log.warning("OKF bundle %s: %s", resolved_bundle_id, msg)
            warnings.append(msg)
        elif block is None:
            msg = f"{source_path}: missing frontmatter; ingesting as generic document"
            log.warning("OKF bundle %s: %s", resolved_bundle_id, msg)
            warnings.append(msg)
        else:
            parsed = parse_frontmatter_block(block)
            if parsed is None:
                msg = (
                    f"{source_path}: unparseable frontmatter YAML; "
                    "ingesting body as generic document"
                )
                log.warning("OKF bundle %s: %s", resolved_bundle_id, msg)
                warnings.append(msg)
            else:
                frontmatter = parsed

        raw_type = _coerce_optional_str(frontmatter.get("type"))
        if frontmatter and raw_type is None:
            msg = (
                f"{source_path}: missing or empty required `type` field "
                "(spec §9); ingesting as generic document"
            )
            log.warning("OKF bundle %s: %s", resolved_bundle_id, msg)
            warnings.append(msg)

        concepts.append(
            OkfConcept(
                concept_id=concept_id,
                source_path=source_path,
                body=body,
                raw_type=raw_type,
                title=_coerce_optional_str(frontmatter.get("title")),
                description=_coerce_optional_str(frontmatter.get("description")),
                tags=_coerce_tags(frontmatter.get("tags")),
                key_values=tuple(_scalar_key_values(frontmatter)),
                frontmatter=frontmatter,
                links=tuple(extract_links(body, source_path)),
            )
        )

    return OkfBundle(
        root=root,
        bundle_id=resolved_bundle_id,
        okf_version=okf_version,
        concepts=concepts,
        warnings=warnings,
        skipped_files=skipped,
    )

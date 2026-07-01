"""build_shard_gold.py -- Gold-needle generator for shard × dense recall A/B.

Auto-generates a needle set from project source trees WITHOUT leaking
path/filename/symbol tokens into the natural-language query.  This is the
prerequisite for bench_shard_recall.py.

DESIGN
------
For each project root we walk source files (.py, .js, .ts, .md), skipping
vendor/node_modules/.git/__pycache__ directories.  We extract salient units:

  * Python/JS/TS: functions and classes that have a docstring or leading
    block comment.  The QUERY is built from that docstring/comment prose.
  * Markdown: H1/H2 sections with at least one non-heading prose line.
    The QUERY is built from the prose body of the section.

Scrubbing (no-leak guard)
--------------------------
The query is scrubbed of:
  1. Every component of the file's path (directory names, filename stem,
     extension, and the project root name itself).
  2. The symbol name (function/class name, section title).
  3. Common path separators / camelCase / snake_case splits thereof.

This prevents the lexical retriever from trivially winning by echoing the
filename back in the query.

CROSS-PROJECT class
--------------------
~20 % of needles are tagged type="cross": they are drawn from one project
but phrased with generic prose (no project-name token) so the retriever must
pick the right project among all ingested projects.

OUTPUT SCHEMA (JSONL, one JSON object per line)
-----------------------------------------------
{
  "id":          "<uuid4-hex>",      // unique row id
  "project":     "<project-name>",   // directory name of the project
  "type":        "within"|"cross",   // within-project vs cross-project
  "file_type":   "code"|"doc",       // source file category
  "question":    "...",              // natural-language query (scrubbed)
  "gold_paths":  ["project/rel/path/to/file.py"],  // project-relative forward-slash
  "gold_symbols":["ClassName.method_name"],        // [] for doc sections
  "gold_lines":  [start_1based, end_1based]        // inclusive
}

CLI
---
python benchmarks/build_shard_gold.py \\
    --project-roots F:/Projects/BookKeeper,F:/Projects/helix-context \\
    --per-project 20 \\
    --out benchmarks/results/shard_gold.jsonl \\
    --seed 42
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import random
import re
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PROJECT_ROOTS = [
    "F:/Projects/BookKeeper",
    "F:/Projects/CosmicTasha",
    "F:/Projects/Education",
    "F:/Projects/helix-context",
    "F:/Projects/MaxExpressKit",
    "F:/Projects/two-brain-audit",
]

SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", "vendor", ".venv", "venv",
    "env", ".env", "dist", "build", ".cache", ".mypy_cache", ".pytest_cache",
    ".tox", "eggs", ".eggs", "*.egg-info", "site-packages", "htmlcov",
    ".ruff_cache", "target",  # Rust/Java
}

SOURCE_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx"}
DOC_EXTS = {".md"}
ALL_EXTS = SOURCE_EXTS | DOC_EXTS

# Minimum prose characters to accept a docstring/section body as a query source.
MIN_PROSE_CHARS = 60
# Maximum prose characters to use (truncate for readability).
MAX_PROSE_CHARS = 500
# Cross-project ratio: ~20 % of needles.
CROSS_RATIO = 0.20


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------

def _rel_forward(root: Path, path: Path) -> str:
    """Return forward-slash path relative to root, e.g. 'src/foo/bar.py'."""
    return path.relative_to(root).as_posix()


def _path_tokens(root: Path, path: Path) -> set[str]:
    """Return all individual tokens that could leak path/filename info.

    Includes every path component, the project root name, stem, camelCase /
    snake_case sub-words, and the extension.
    """
    tokens: set[str] = set()

    # project root directory name
    tokens.add(root.name.lower())

    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path

    for part in rel.parts:
        tokens.add(part.lower())
        stem = Path(part).stem.lower()
        tokens.add(stem)
        # split camelCase and snake_case sub-tokens
        sub = re.sub(r"([A-Z])", r"_\1", stem).lower()
        for t in re.split(r"[_\-\.]+", sub):
            if t:
                tokens.add(t)

    # extension without dot
    ext = path.suffix.lstrip(".")
    if ext:
        tokens.add(ext)

    # Remove trivial 1-2 char tokens and common words that give no info
    boring = {"py", "js", "ts", "md", "tsx", "jsx", "src", "lib", "app",
               "test", "tests", "docs", "doc", "index", "main", "init",
               "readme", "setup", "config", "utils", "util", "helpers",
               "helper", "api", "the", "a", "an", "of", "in", "on", "at"}
    tokens -= boring
    return {t for t in tokens if len(t) > 2}


def _scrub(text: str, forbidden: set[str]) -> str:
    """Remove exact-word occurrences of forbidden tokens from text.

    Uses whole-word regex replacement (case-insensitive) so partial matches
    (e.g. 'bookkeeper' in 'bookkeeper_route') are still caught.
    """
    for tok in sorted(forbidden, key=len, reverse=True):
        if not tok:
            continue
        # Escape and replace as whole word (word boundary aware)
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(tok) + r"(?![A-Za-z0-9_])"
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    # Collapse whitespace
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# File walkers
# ---------------------------------------------------------------------------

def _should_skip(path: Path) -> bool:
    """Return True if any directory component should be skipped."""
    for part in path.parts:
        if part in SKIP_DIRS or part.endswith(".egg-info"):
            return True
    return False


def walk_source_files(root: Path) -> Iterator[Path]:
    """Yield source + doc files under root, respecting skip rules."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place so os.walk doesn't descend into them.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not d.endswith(".egg-info")]
        p = Path(dirpath)
        if _should_skip(p):
            continue
        for fname in filenames:
            fp = p / fname
            if fp.suffix in ALL_EXTS and not _should_skip(fp):
                yield fp


# ---------------------------------------------------------------------------
# Python extractor
# ---------------------------------------------------------------------------

def _first_docstring(node: ast.AST) -> str | None:
    """Return the docstring of a function/class node, or None."""
    if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body):
        return None
    first = node.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        val = first.value.value
        if isinstance(val, str) and len(val.strip()) >= MIN_PROSE_CHARS:
            return val.strip()
    return None


def extract_python_units(path: Path) -> list[dict]:
    """Extract functions/classes with docstrings from a Python file."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    lines = source.splitlines()
    units = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        doc = _first_docstring(node)
        if not doc:
            continue
        symbol = node.name
        start = node.lineno  # 1-based
        end = getattr(node, "end_lineno", start)
        units.append({
            "symbol": symbol,
            "doc": doc,
            "start": start,
            "end": end,
        })

    return units


# ---------------------------------------------------------------------------
# JS/TS extractor (regex-based, no full parse)
# ---------------------------------------------------------------------------

_JS_FUNC_RE = re.compile(
    r"/\*\*\s*(.*?)\*/\s*"
    r"(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|"
    r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(?)",
    re.DOTALL,
)
_JS_CLASS_RE = re.compile(
    r"/\*\*\s*(.*?)\*/\s*(?:export\s+)?class\s+(\w+)",
    re.DOTALL,
)


def _clean_jsdoc(raw: str) -> str:
    """Strip JSDoc * prefixes and @param/@returns lines."""
    lines = []
    for line in raw.splitlines():
        line = re.sub(r"^\s*\*\s?", "", line)
        if re.match(r"@\w+", line.strip()):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def extract_js_units(path: Path) -> list[dict]:
    """Extract JSDoc-documented functions/classes from JS/TS files."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    units = []
    lines = source.splitlines()

    for pattern in (_JS_FUNC_RE, _JS_CLASS_RE):
        for m in pattern.finditer(source):
            raw_doc = m.group(1)
            doc = _clean_jsdoc(raw_doc)
            if len(doc) < MIN_PROSE_CHARS:
                continue
            # Pick symbol name from whichever capture group matched.
            symbol = next((g for g in m.groups()[1:] if g), "anonymous")
            # Approximate line number from match start.
            start = source[:m.start()].count("\n") + 1
            end = source[:m.end()].count("\n") + 1
            units.append({
                "symbol": symbol,
                "doc": doc,
                "start": start,
                "end": end,
            })

    return units


# ---------------------------------------------------------------------------
# Markdown extractor
# ---------------------------------------------------------------------------

_MD_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")


def extract_md_sections(path: Path) -> list[dict]:
    """Extract H1/H2/H3 sections with prose bodies from Markdown."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    lines = source.splitlines()
    sections = []
    i = 0
    while i < len(lines):
        m = _MD_HEADING_RE.match(lines[i])
        if m:
            title = m.group(2).strip()
            start = i + 1  # 1-based
            body_lines = []
            i += 1
            while i < len(lines) and not _MD_HEADING_RE.match(lines[i]):
                body_lines.append(lines[i])
                i += 1
            body = "\n".join(body_lines).strip()
            # Strip code fences from body for prose extraction.
            body_no_code = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
            body_no_code = re.sub(r"`[^`]+`", "", body_no_code)
            body_no_code = body_no_code.strip()
            if len(body_no_code) >= MIN_PROSE_CHARS:
                sections.append({
                    "symbol": title,
                    "doc": body_no_code,
                    "start": start,
                    "end": i,  # 1-based, end of section
                })
        else:
            i += 1

    return sections


# ---------------------------------------------------------------------------
# Unit extractor dispatcher
# ---------------------------------------------------------------------------

def extract_units(path: Path) -> list[dict]:
    """Extract salient units (with docstrings/prose) from any supported file."""
    ext = path.suffix.lower()
    if ext == ".py":
        return extract_python_units(path)
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        return extract_js_units(path)
    elif ext == ".md":
        return extract_md_sections(path)
    return []


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------

def _build_query(doc: str, forbidden: set[str]) -> str | None:
    """Build and scrub a natural-language query from docstring/prose.

    Returns None if the scrubbed result is too short to be useful.
    """
    # Take first paragraph (up to MAX_PROSE_CHARS) and normalise whitespace.
    prose = textwrap.dedent(doc)
    # Use the first non-empty paragraph that is not badge/image-heavy.
    # A paragraph where >50% of lines start with "![" is a badge block —
    # skip it and try the next one.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", prose) if p.strip()]
    if not paragraphs:
        return None
    text = ""
    for para in paragraphs:
        lines = [l.strip() for l in para.splitlines() if l.strip()]
        badge_lines = sum(1 for l in lines if l.startswith("![") or l.startswith("[!["))
        if lines and badge_lines / len(lines) > 0.4:
            continue  # skip badge paragraph, try next
        text = para[:MAX_PROSE_CHARS]
        break
    if not text:
        return None
    # Strip Markdown inline image syntax first (before link stripping).
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    # Strip Markdown inline formatting.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)

    # Scrub forbidden tokens.
    scrubbed = _scrub(text, forbidden)

    # Require at least 40 chars after scrubbing.
    if len(scrubbed) < 40:
        return None

    # Convert to a question form if it looks like a declarative sentence.
    # We prepend "What does this code do?" only for very terse snippets;
    # for longer prose we use the prose directly (it already describes behaviour).
    first_word = scrubbed.split()[0].lower() if scrubbed.split() else ""
    question_starters = {"what", "how", "why", "when", "where", "which", "who",
                         "does", "is", "are", "can", "should", "will"}
    if first_word not in question_starters:
        # Trim to first sentence if possible.
        m = re.search(r"[.!?]", scrubbed)
        if m and m.start() > 40:
            scrubbed = scrubbed[: m.start() + 1]
        # Don't wrap — leave as declarative description; it's a valid query form.

    return scrubbed.strip()


# ---------------------------------------------------------------------------
# Needle builder for one project
# ---------------------------------------------------------------------------

def build_needles_for_project(
    root: Path,
    n: int,
    rng: random.Random,
) -> list[dict]:
    """Walk a project root and generate up to n needle candidates."""
    if not root.exists():
        return []

    candidates: list[dict] = []

    for fpath in walk_source_files(root):
        units = extract_units(fpath)
        if not units:
            continue

        forbidden = _path_tokens(root, fpath)
        file_type = "doc" if fpath.suffix == ".md" else "code"
        rel = _rel_forward(root, fpath)

        for unit in units:
            forbidden_unit = forbidden | {unit["symbol"].lower()}
            # Also add camelCase splits of the symbol.
            sym_parts = re.sub(r"([A-Z])", r"_\1", unit["symbol"]).lower()
            for t in re.split(r"[_\-\. ]+", sym_parts):
                if len(t) > 2:
                    forbidden_unit.add(t)

            query = _build_query(unit["doc"], forbidden_unit)
            if not query:
                continue

            candidates.append({
                "_root": root,
                "_fpath": fpath,
                "project": root.name,
                "file_type": file_type,
                "question": query,
                "gold_paths": ["{}/{}".format(root.name, rel)],
                "gold_symbols": [unit["symbol"]] if file_type == "code" else [],
                "gold_lines": [unit["start"], unit["end"]],
            })

    if not candidates:
        return []

    rng.shuffle(candidates)
    return candidates[:n]


# ---------------------------------------------------------------------------
# Cross-project needle synthesis
# ---------------------------------------------------------------------------

def make_cross_needles(
    within_needles: list[dict],
    n_cross: int,
    rng: random.Random,
) -> list[dict]:
    """Select n_cross within-needles and re-tag them as type='cross'.

    We pick from different projects to ensure variety.  The query is already
    scrubbed of project-name tokens, so the same row works as a cross-project
    needle.
    """
    # Group by project.
    by_project: dict[str, list[dict]] = {}
    for nd in within_needles:
        by_project.setdefault(nd["project"], []).append(nd)

    pool: list[dict] = []
    project_names = sorted(by_project.keys())
    # Round-robin across projects to ensure variety.
    idx = 0
    while len(pool) < n_cross * 3 and any(by_project.values()):
        pname = project_names[idx % len(project_names)]
        if by_project.get(pname):
            pool.append(by_project[pname].pop(rng.randint(0, len(by_project[pname]) - 1)))
        idx += 1
        if idx > n_cross * 10:
            break

    rng.shuffle(pool)
    cross = []
    for nd in pool[:n_cross]:
        c = dict(nd)
        c["type"] = "cross"
        cross.append(c)
    return cross


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Generate a gold-needle set for shard × dense recall A/B benchmarks. "
            "Queries are scrubbed of filename/symbol/path tokens to prevent lexical "
            "short-circuiting.  Output is JSONL with one needle per line."
        )
    )
    ap.add_argument(
        "--project-roots",
        default=",".join(DEFAULT_PROJECT_ROOTS),
        help=(
            "Comma-separated list of project root directories.  Missing roots are "
            "skipped gracefully.  Default: the 6 medium-corpus projects."
        ),
    )
    ap.add_argument(
        "--per-project",
        type=int,
        default=20,
        dest="per_project",
        help="Target number of needles per project (default: 20).",
    )
    ap.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "results" / "shard_gold.jsonl"),
        help="Output JSONL path (default: benchmarks/results/shard_gold.jsonl).",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for determinism (default: 42).",
    )
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)

    project_roots = [
        Path(p.strip()) for p in args.project_roots.split(",") if p.strip()
    ]

    all_within: list[dict] = []
    summary_rows: list[dict] = []

    for root in project_roots:
        if not root.exists():
            print(
                "[build_shard_gold] SKIP {} (not found on this machine)".format(root),
                file=sys.stderr,
            )
            summary_rows.append({"project": root.name, "status": "missing", "n": 0})
            continue

        print("[build_shard_gold] scanning {} ...".format(root), flush=True)
        needles = build_needles_for_project(root, args.per_project, rng)

        for nd in needles:
            nd["id"] = uuid.uuid4().hex
            nd["type"] = "within"
            # Remove internal _root/_fpath keys.
            nd.pop("_root", None)
            nd.pop("_fpath", None)

        all_within.extend(needles)
        n_code = sum(1 for n in needles if n["file_type"] == "code")
        n_doc = sum(1 for n in needles if n["file_type"] == "doc")
        summary_rows.append({
            "project": root.name,
            "status": "ok",
            "n": len(needles),
            "n_code": n_code,
            "n_doc": n_doc,
        })
        print(
            "  -> {} needles ({} code, {} doc)".format(
                len(needles), n_code, n_doc
            ),
            flush=True,
        )

    if not all_within:
        print(
            "ERROR: no needles generated — are the project roots accessible?",
            file=sys.stderr,
        )
        return 1

    # Build cross-project needles (~20 % of total).
    n_total_within = len(all_within)
    n_cross = max(1, round(n_total_within * CROSS_RATIO / (1.0 - CROSS_RATIO)))
    cross_needles = make_cross_needles(list(all_within), n_cross, rng)

    all_needles = all_within + cross_needles
    rng.shuffle(all_needles)

    # Write JSONL.
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for nd in all_needles:
            fh.write(json.dumps(nd, ensure_ascii=False) + "\n")

    # Summary.
    n_within = sum(1 for n in all_needles if n["type"] == "within")
    n_cross_final = sum(1 for n in all_needles if n["type"] == "cross")
    n_code = sum(1 for n in all_needles if n["file_type"] == "code")
    n_doc = sum(1 for n in all_needles if n["file_type"] == "doc")

    print()
    print("=" * 60)
    print("  Gold needle summary")
    print("=" * 60)
    print("  {:>8}  total needles".format(len(all_needles)))
    print("  {:>8}  within-project (type=within)".format(n_within))
    print("  {:>8}  cross-project  (type=cross)".format(n_cross_final))
    print("  {:>8}  code units (functions/classes)".format(n_code))
    print("  {:>8}  doc units (Markdown sections)".format(n_doc))
    print()
    print("  Per-project breakdown:")
    for row in summary_rows:
        if row["status"] == "missing":
            print("    {:30s}  MISSING".format(row["project"]))
        else:
            print(
                "    {:30s}  {:3d} needles "
                "({} code / {} doc)".format(
                    row["project"],
                    row["n"],
                    row.get("n_code", 0),
                    row.get("n_doc", 0),
                )
            )
    print()
    print("  -> {}".format(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

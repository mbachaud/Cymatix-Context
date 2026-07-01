"""build_gold_from_genome.py -- Gold-needle generator from a BLOB genome.db.

Sibling of ``build_shard_gold.py``, but instead of walking project source
*trees* it samples salient genes directly out of a single, already-ingested
**blob** ``genome.db`` (the non-sharded knowledge store). This lets us build a
recall needle-set for any corpus we have a genome for, without needing the
original files on disk.

DESIGN
------
1. Read every gene's ``source_id`` (the on-disk path the document came from)
   and ``content`` from the ``genes`` table (read-only, ``mode=ro``).
2. Bucket genes by *source-prefix* -- the top path segment of ``source_id``.
   That top segment is treated as the gene's "project" / shard label
   (e.g. ``F:\\tmp\\enterprise_rag_500k\\sources\\github\\...`` -> ``github``
   once the drive / mount prefix is peeled). See ``source_prefix``.
3. For each prefix, sample ``--per-source`` genes whose content is
   "substantial" (>= ``--min-content-chars`` after whitespace collapse).
4. Form a natural-language QUESTION from a salient sentence / heading /
   docstring line of the content, SCRUBBED of the gene's own filename stem,
   directory tokens, and symbol-like tokens -- the same no-leak guard idea as
   ``build_shard_gold.py`` so the lexical retriever can't win by echoing the
   path back.
5. ``gold_paths = [that gene's source_id]`` (the recall target).

~20 % of needles are tagged ``type="cross"`` (drawn from the within pool but
re-tagged so the retriever must pick the right source among all sources). The
rest are ``type="within"``.

OUTPUT SCHEMA (JSONL -- EXACTLY what bench_shard_recall.py consumes)
-------------------------------------------------------------------
``bench_shard_recall.py`` reads, per row:
    nd["question"]            -- the query string (required)
    nd.get("type", "within")  -- "within" | "cross"
    nd.get("gold_paths", [])  -- list of path substrings (bidirectional match)
    nd.get("id")              -- row id (echoed into per-needle results)
    nd.get("project")         -- echoed into per-needle results
    nd.get("file_type")       -- echoed into per-needle results

So each emitted row carries:
    {
      "id":         "<uuid4-hex>",
      "project":    "<source-prefix>",
      "type":       "within" | "cross",
      "file_type":  "code" | "doc",
      "question":   "...",                 // scrubbed natural-language query
      "gold_paths": ["<gene.source_id>"],  // the recall target
      "gene_id":    "<gene_id>",           // provenance (ignored by the bench)
      "source_prefix": "<prefix>"          // provenance (ignored by the bench)
    }

CLI
---
python benchmarks/build_gold_from_genome.py \\
    --genome genomes/main/genome.db \\
    --per-source 20 \\
    --out benchmarks/results/blob_gold.jsonl \\
    --seed 42 \\
    --min-content-chars 200
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from collections import defaultdict
from pathlib import PurePosixPath, PureWindowsPath
from typing import Iterable

# ~20 % of needles are cross-project (matches build_shard_gold.py).
CROSS_RATIO = 0.20

# File extensions we treat as "doc" rather than "code".
_DOC_EXTS = {".md", ".txt", ".rst", ".adoc"}

# Tokens that carry no retrieval signal -- never count them as a "leak".
_BORING_TOKENS = {
    "py", "js", "ts", "md", "txt", "json", "tsx", "jsx", "src", "lib", "app",
    "test", "tests", "docs", "doc", "index", "main", "init", "readme", "setup",
    "config", "utils", "util", "helpers", "helper", "api", "the", "a", "an",
    "of", "in", "on", "at", "to", "and", "or", "is", "are", "for", "with",
    "sources", "source", "tmp", "data",
}


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _split_path(source_id: str) -> list[str]:
    """Split a Windows-or-POSIX path into its components, drive peeled."""
    if not source_id:
        return []
    s = source_id.replace("\\", "/")
    # Peel a Windows drive prefix (e.g. "F:/").
    s = re.sub(r"^[A-Za-z]:/", "", s)
    parts = [p for p in s.split("/") if p and p not in (".", "..")]
    return parts


def source_prefix(source_id: str) -> str:
    """Return the top path segment of ``source_id`` -- the gene's "project".

    We peel structural wrapper segments (``tmp``, ``sources``, mount/scratch
    dirs) so the prefix lands on the meaningful corpus name. For

        F:\\tmp\\enterprise_rag_500k\\sources\\github\\pr-1.json

    we want ``github`` (the real per-shard source), not ``tmp``. Heuristic:
    walk components left-to-right, skipping boring wrapper tokens, and return
    the first "meaningful" segment; if the next-to-last meaningful directory
    looks like a corpus container (``sources``/``source``), prefer the segment
    *after* it.
    """
    parts = _split_path(source_id)
    if not parts:
        return "_unknown"
    # Drop the filename (last component) when there's a directory to use.
    dirs = parts[:-1] if len(parts) > 1 else parts
    # If a "sources"/"source" container appears, the segment right after it is
    # the canonical shard/source label.
    low = [d.lower() for d in dirs]
    for container in ("sources", "source"):
        if container in low:
            i = low.index(container)
            if i + 1 < len(dirs):
                return dirs[i + 1]
    # Otherwise: first non-boring directory segment.
    for d in dirs:
        if d.lower() not in _BORING_TOKENS:
            return d
    return dirs[0]


def _path_leak_tokens(source_id: str) -> set[str]:
    """All tokens from the path that must NOT appear in the question.

    Mirrors build_shard_gold._path_tokens: every component, its stem, and the
    camelCase / snake_case sub-words thereof, plus the extension.
    """
    tokens: set[str] = set()
    for part in _split_path(source_id):
        tokens.add(part.lower())
        stem = re.sub(r"\.[^.]+$", "", part).lower()  # strip extension
        tokens.add(stem)
        sub = re.sub(r"([A-Z])", r"_\1", stem).lower()
        for t in re.split(r"[_\-\.\s]+", sub):
            if t:
                tokens.add(t)
    # Extension of the final component.
    last = _split_path(source_id)[-1] if _split_path(source_id) else ""
    m = re.search(r"\.([A-Za-z0-9]+)$", last)
    if m:
        tokens.add(m.group(1).lower())
    tokens -= _BORING_TOKENS
    return {t for t in tokens if len(t) > 2}


def _symbol_like_tokens(text: str) -> set[str]:
    """Symbol-ish tokens in *text* (snake_case, camelCase, dotted.paths).

    These are stripped from a question because they tend to be code symbols
    that echo the gene's identity rather than describe its behaviour.
    """
    out: set[str] = set()
    # dotted.module.paths and snake_case_words and camelCase identifiers
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", text):
        out.add(m.group(0).lower())
    for m in re.finditer(r"\b[a-z]+(?:_[a-z0-9]+)+\b", text):
        out.add(m.group(0).lower())
    for m in re.finditer(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b", text):  # camelCase
        out.add(m.group(0).lower())
    return out


def _scrub(text: str, forbidden: set[str]) -> str:
    """Whole-word, case-insensitive removal of forbidden tokens."""
    for tok in sorted(forbidden, key=len, reverse=True):
        if not tok:
            continue
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(tok) + r"(?![A-Za-z0-9_])"
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Question extraction
# ---------------------------------------------------------------------------

def _salient_sentences(content: str) -> list[str]:
    """Yield candidate salient lines/sentences from raw content, best first.

    Strategy: prefer Markdown headings and the first prose sentence; fall back
    to the longest plain sentence. Code fences and obvious noise are stripped.
    """
    cands: list[str] = []

    # Strip fenced code blocks for prose extraction.
    prose = re.sub(r"```.*?```", " ", content, flags=re.DOTALL)
    prose = re.sub(r"`[^`]+`", " ", prose)
    # Strip HTML/XML tags so markup-heavy corpora (rendered docs) yield clean
    # prose questions rather than tag soup.
    prose = re.sub(r"<[^>]+>", " ", prose)

    # Markdown headings (drop the leading #'s).
    for m in re.finditer(r"(?m)^#{1,4}\s+(.+)$", content):
        h = m.group(1).strip()
        if len(h) >= 12:
            cands.append(h)

    # Sentences from the prose body.
    flat = re.sub(r"\s+", " ", prose).strip()
    for sent in re.split(r"(?<=[.!?])\s+", flat):
        sent = sent.strip()
        if 20 <= len(sent) <= 400:
            cands.append(sent)

    # Longest single line as a final fallback.
    for line in content.splitlines():
        line = line.strip()
        if 20 <= len(line) <= 400:
            cands.append(line)

    # De-dup, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        key = c.lower()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def build_question(content: str, source_id: str) -> str | None:
    """Build a scrubbed natural-language question, or None if not usable."""
    forbidden = _path_leak_tokens(source_id)
    for cand in _salient_sentences(content):
        # Also forbid symbol-like tokens that appear in the candidate.
        forbidden_cand = forbidden | _symbol_like_tokens(cand)
        # Strip markdown inline formatting.
        text = re.sub(r"!\[.*?\]\(.*?\)", "", cand)
        text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
        # Strip any residual HTML/XML tags (heading + line fallbacks use raw
        # content, which may still carry markup).
        text = re.sub(r"<[^>]+>", " ", text)
        # Strip *bold* / ~strike~ emphasis. Deliberately do NOT touch '_': it
        # is a word char inside snake_case identifiers, and stripping it would
        # fuse tokens (frobnicator_service -> frobnicatorservice) past the
        # whole-word scrub below, defeating the no-leak guard.
        text = re.sub(r"[*~]{1,3}(.+?)[*~]{1,3}", r"\1", text)
        scrubbed = _scrub(text, forbidden_cand)
        if len(scrubbed) >= 40:
            return scrubbed
    return None


def _file_type(source_id: str) -> str:
    last = _split_path(source_id)[-1] if _split_path(source_id) else ""
    m = re.search(r"(\.[A-Za-z0-9]+)$", last)
    ext = m.group(1).lower() if m else ""
    return "doc" if ext in _DOC_EXTS else "code"


# ---------------------------------------------------------------------------
# Genome reader
# ---------------------------------------------------------------------------

def _open_ro(genome_path: str) -> sqlite3.Connection:
    """Open the genome strictly read-only (mode=ro)."""
    if not os.path.exists(genome_path):
        raise FileNotFoundError(genome_path)
    uri = "file:{}?mode=ro".format(os.path.abspath(genome_path).replace("\\", "/"))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def iter_genes(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    """Yield (gene_id, source_id, content) rows that have a source + content."""
    cur = conn.execute(
        "SELECT gene_id, source_id, content FROM genes "
        "WHERE source_id IS NOT NULL AND source_id != '' "
        "AND content IS NOT NULL AND content != ''"
    )
    for row in cur:
        yield row


def _substantial(content: str, min_chars: int) -> bool:
    return len(re.sub(r"\s+", " ", content or "").strip()) >= min_chars


# ---------------------------------------------------------------------------
# Needle builder
# ---------------------------------------------------------------------------

def build_needles(
    genome_path: str,
    per_source: int,
    seed: int,
    min_content_chars: int,
) -> tuple[list[dict], dict[str, int]]:
    """Build within-needles from a blob genome. Returns (needles, per_src_n)."""
    import random

    rng = random.Random(seed)
    conn = _open_ro(genome_path)
    try:
        by_prefix: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in iter_genes(conn):
            if not _substantial(row["content"], min_content_chars):
                continue
            by_prefix[source_prefix(row["source_id"])].append(row)
    finally:
        conn.close()

    needles: list[dict] = []
    per_src_n: dict[str, int] = {}

    for prefix in sorted(by_prefix.keys()):
        rows = list(by_prefix[prefix])
        rng.shuffle(rows)
        taken = 0
        for row in rows:
            if taken >= per_source:
                break
            question = build_question(row["content"], row["source_id"])
            if not question:
                continue
            needles.append({
                "id": uuid.uuid4().hex,
                "project": prefix,
                "type": "within",
                "file_type": _file_type(row["source_id"]),
                "question": question,
                "gold_paths": [row["source_id"]],
                "gene_id": row["gene_id"],
                "source_prefix": prefix,
            })
            taken += 1
        per_src_n[prefix] = taken

    return needles, per_src_n


def make_cross_needles(
    within: list[dict],
    n_cross: int,
    seed: int,
) -> list[dict]:
    """Re-tag a round-robin selection of within-needles as type='cross'."""
    import random

    rng = random.Random(seed + 1)
    by_project: dict[str, list[dict]] = defaultdict(list)
    for nd in within:
        by_project[nd["project"]].append(nd)
    project_names = sorted(by_project.keys())

    pool: list[dict] = []
    idx = 0
    guard = 0
    while len(pool) < n_cross and any(by_project.values()):
        pname = project_names[idx % len(project_names)]
        bucket = by_project.get(pname)
        if bucket:
            pool.append(bucket.pop(rng.randrange(len(bucket))))
        idx += 1
        guard += 1
        if guard > n_cross * 50 + 100:
            break

    cross: list[dict] = []
    for nd in pool[:n_cross]:
        c = dict(nd)
        c["id"] = uuid.uuid4().hex  # fresh id so cross row is distinct
        c["type"] = "cross"
        cross.append(c)
    return cross


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Generate gold recall needles directly from a single blob "
            "genome.db. Questions are scrubbed of the gene's own path / "
            "filename / symbol tokens (no leak). Output JSONL is consumed "
            "by bench_shard_recall.py."
        )
    )
    ap.add_argument("--genome", required=True, help="Path to a blob genome.db.")
    ap.add_argument(
        "--per-source", type=int, default=20, dest="per_source",
        help="Target needles per source-prefix (default 20).",
    )
    ap.add_argument("--out", required=True, help="Output JSONL path.")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed (default 42).")
    ap.add_argument(
        "--min-content-chars", type=int, default=200, dest="min_content_chars",
        help="Minimum collapsed content length to sample a gene (default 200).",
    )
    args = ap.parse_args(argv)

    within, per_src_n = build_needles(
        genome_path=args.genome,
        per_source=args.per_source,
        seed=args.seed,
        min_content_chars=args.min_content_chars,
    )

    if not within:
        print(
            "ERROR: no needles generated from {} -- empty genome, or no genes "
            "passed the --min-content-chars / no-leak filters.".format(args.genome),
            file=sys.stderr,
        )
        return 1

    n_within = len(within)
    n_cross = max(1, round(n_within * CROSS_RATIO / (1.0 - CROSS_RATIO)))
    cross = make_cross_needles(within, n_cross, args.seed)

    all_needles = within + cross
    import random
    random.Random(args.seed + 2).shuffle(all_needles)

    out_path = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for nd in all_needles:
            fh.write(json.dumps(nd, ensure_ascii=False) + "\n")

    n_cross_final = sum(1 for n in all_needles if n["type"] == "cross")
    n_code = sum(1 for n in all_needles if n["file_type"] == "code")
    n_doc = sum(1 for n in all_needles if n["file_type"] == "doc")

    print("=" * 60)
    print("  Gold needles from blob genome")
    print("=" * 60)
    print("  genome:        {}".format(args.genome))
    print("  {:>8}  total needles".format(len(all_needles)))
    print("  {:>8}  within-source (type=within)".format(n_within))
    print("  {:>8}  cross-source  (type=cross)".format(n_cross_final))
    print("  {:>8}  code / {:<8} doc".format(n_code, n_doc))
    print()
    print("  Per-source breakdown:")
    for prefix in sorted(per_src_n.keys()):
        print("    {:30s}  {:3d} needles".format(prefix, per_src_n[prefix]))
    print()
    print("  -> {}".format(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

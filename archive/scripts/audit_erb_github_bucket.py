"""ERB github-bucket audit (council plan 2026-07-01, step 4).

Decides the lingering question "is a semantic/ERB re-test worth it after the
code-path improvements?" with data instead of opinion. The code path only
fires for ``content_type == "code"``; ERB is prose except (possibly) the
``sources/github`` subtree, and the fixtures were built pre-cAST. This audit
measures whether that exception is material.

Decision rule (from docs/research/2026-07-01-next-bench-wave-consensus.md):
  gold share in code-typed github files > 10%  → schedule a one-shard
  re-ingest A/B (re-chunk github source post-#224+cAST, re-run only
  github-bucket questions);
  otherwise → close the semantic re-test question; the encoder re-embed
  (GB10-gated) remains the only ERB-semantic lever.

Usage (rig, no GPU, seconds):
  python scripts/audit_erb_github_bucket.py \
      --erb-root F:/Projects/EnterpriseRAG-Bench-main \
      [--genome genomes/bench/enterprise_rag_onyx_full_2/main.db]

Stdlib only.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys
from pathlib import Path

try:  # single source of truth when run from the repo
    from cymatix_context.cli.cmd_ingest import _CODE_EXTENSIONS as CODE_EXTS
except Exception:  # fallback copy (#224 list)
    CODE_EXTS = frozenset({
        ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java", ".c",
        ".h", ".cc", ".cpp", ".hpp", ".hh", ".cs", ".rb", ".php", ".lua",
        ".scala", ".kt", ".kts", ".swift", ".sh", ".bash", ".sql", ".m",
        ".mm",
    })


def census_extensions(github_root: Path) -> tuple[collections.Counter, int, int]:
    """Extension histogram under sources/github + code-typed file count."""
    exts: collections.Counter = collections.Counter()
    total = code = 0
    for p in github_root.rglob("*"):
        if not p.is_file():
            continue
        total += 1
        suf = p.suffix.lower()
        exts[suf or "<none>"] += 1
        if suf in CODE_EXTS:
            code += 1
    return exts, total, code


def load_uuid_index(gen_root: Path) -> dict[str, str]:
    """uuid -> path map; tolerant of value shapes."""
    idx_path = gen_root / "uuid_index.json"
    if not idx_path.exists():
        return {}
    raw = json.loads(idx_path.read_text(encoding="utf-8", errors="replace"))
    out: dict[str, str] = {}
    for k, v in raw.items() if isinstance(raw, dict) else []:
        if isinstance(v, str):
            out[k] = v
        elif isinstance(v, dict):
            for key in ("path", "file", "source", "relative_path"):
                if isinstance(v.get(key), str):
                    out[k] = v[key]
                    break
    return out


def gold_share(gen_root: Path, uuid_index: dict[str, str]) -> tuple[int, int, int]:
    """(total gold ids, gold in github bucket, gold in github with code ext)."""
    total = in_gh = in_gh_code = 0
    for qfile in ("questions.jsonl", "extra_questions.jsonl"):
        qpath = gen_root / qfile
        if not qpath.exists():
            continue
        for line in qpath.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                q = json.loads(line)
            except json.JSONDecodeError:
                continue
            for doc_id in q.get("expected_doc_ids") or []:
                total += 1
                path = uuid_index.get(str(doc_id), str(doc_id))
                norm = str(path).replace("\\", "/").lower()
                if "sources/github" in norm or "/github/" in norm:
                    in_gh += 1
                    if Path(norm).suffix.lower() in CODE_EXTS:
                        in_gh_code += 1
    return total, in_gh, in_gh_code


def genome_census(db_path: Path) -> None:
    """How the fixture actually stored github-source genes."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        n_total = conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
        n_gh = conn.execute(
            "SELECT COUNT(*) FROM genes WHERE LOWER(source_id) LIKE '%github%'"
        ).fetchone()[0]
        print(f"genome: {n_total} genes, {n_gh} with github-ish source_id "
              f"({100.0 * n_gh / max(n_total, 1):.1f}%)")
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--erb-root", default=os.environ.get(
        "ERB_ROOT", "F:/Projects/EnterpriseRAG-Bench-main"))
    ap.add_argument("--genome", default=None,
                    help="optional fixture db for a stored-source census")
    args = ap.parse_args()

    gen_root = Path(args.erb_root) / "generated_data"
    github_root = gen_root / "sources" / "github"
    if not github_root.is_dir():
        print(f"ERROR: {github_root} not found", file=sys.stderr)
        return 2

    exts, total, code = census_extensions(github_root)
    print(f"sources/github: {total} files, {code} with code extensions "
          f"({100.0 * code / max(total, 1):.1f}%)")
    for ext, n in exts.most_common(12):
        marker = "  <- code-typed (#224)" if ext in CODE_EXTS else ""
        print(f"  {ext:>8}  {n}{marker}")

    uuid_index = load_uuid_index(gen_root)
    g_total, g_gh, g_gh_code = gold_share(gen_root, uuid_index)
    if g_total:
        print(f"\ngold: {g_total} expected_doc_ids | github bucket {g_gh} "
              f"({100.0 * g_gh / g_total:.1f}%) | code-typed within github "
              f"{g_gh_code} ({100.0 * g_gh_code / g_total:.1f}% of all gold)")
        verdict = ("SCHEDULE one-shard re-ingest A/B (gold share above 10%)"
                   if g_gh_code / g_total > 0.10 else
                   "CLOSE the semantic re-test question (code-typed gold share <= 10%)")
        print(f"verdict: {verdict}")
    else:
        print("\ngold: no questions.jsonl found — run with --erb-root pointing "
              "at the upstream checkout")

    if args.genome:
        genome_census(Path(args.genome))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Fetch MULocBench repo snapshots + emit corpus and needle set.

Semantic-portfolio lane (user-approved 2026-08-30). Source dataset:
hf:somethingone/MULocBench (``all_issues_with_pr_commit_comment_all_project_
0922_only_Python.json`` — 842 GitHub issues across 45 Python projects, gold
locations at file/class/function level pinned to a per-issue base_commit).

Haystack strategy (recorded because it bounds validity): 842 issues span 804
distinct (repo, base_commit) pairs — per-issue snapshots are infeasible for a
knowledge-store bench. We pin ONE snapshot per repo (the repo's most frequent
base_commit; ties broken by first occurrence) fetched as a GitHub tarball
(codeload.github.com/<org>/<repo>/tar.gz/<sha>), and keep every issue with at
least one gold file present in that snapshot. Per-needle meta records whether
the issue's own base_commit == the snapshot (``exact_commit``) — grade the
exact subset separately when citing strict benchmark numbers; the drifted
majority measures file-level localization against a near-neighbor tree
(file identity is commit-stable for 'modified' gold, content may drift).

Corpus: allowlisted text/code files only, extracted to
``<corpus-root>/<org>__<repo>/<relpath>``. The builder's 50..200,000-byte
ingest gate applies downstream; gold files outside it are recorded as
``gold_size_gated`` and their issues keep only in-gate gold.

Outputs:
  <bench-dir>/needles_muloc.json        {"needles": [{name, query,
                                        question_type: "muloc_file_loc"}]}
  <bench-dir>/gold_paths_muloc.json     needle -> [corpus-relative paths]
  <bench-dir>/needles_muloc_meta.json   per-needle repo/commit/drift detail +
                                        snapshot table + drop ledger

Usage:
  python scripts/build_mulocbench_corpus.py fetch    # download tarballs
  python scripts/build_mulocbench_corpus.py emit     # extract + emit
  python scripts/build_mulocbench_corpus.py all
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import tarfile
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

log = logging.getLogger("build_mulocbench_corpus")

DATASET = Path(r"F:/Projects/mulocbench/hf_dataset/all_issues_with_pr_commit_comment_all_project_0922_only_Python.json")
TARBALLS = Path(r"F:/Projects/mulocbench/tarballs")
CORPUS = Path(r"F:/Projects/mulocbench/corpus")
BENCH = Path("benchmarks/dogfood/muloc")

ALLOW_EXT = {
    ".py", ".pyx", ".pyi", ".rst", ".md", ".txt", ".yml", ".yaml", ".json",
    ".toml", ".ini", ".cfg", ".sh", ".ts", ".tsx", ".js", ".h", ".c",
}
QUERY_BODY_CHARS = 2000
MAX_JSON_BYTES = 200_000  # mirrors the builder gate; skips giant data files at extract time


def load_issues() -> list[dict]:
    return json.loads(DATASET.read_text(encoding="utf-8"))


def choose_snapshots(issues: list[dict]) -> dict[str, tuple[str, str]]:
    """repo_name -> (org, chosen base_commit)."""
    counts: dict[str, Counter] = defaultdict(Counter)
    first_seen: dict[tuple[str, str], int] = {}
    org_of: dict[str, str] = {}
    for i, x in enumerate(issues):
        r, c = x["repo_name"], x["base_commit"]
        counts[r][c] += 1
        first_seen.setdefault((r, c), i)
        org_of[r] = x["organization"]
    chosen = {}
    for r, cnt in counts.items():
        best = sorted(cnt.items(), key=lambda kv: (-kv[1], first_seen[(r, kv[0])]))[0][0]
        chosen[r] = (org_of[r], best)
    return chosen


def fetch(chosen: dict[str, tuple[str, str]]) -> None:
    TARBALLS.mkdir(parents=True, exist_ok=True)
    for repo, (org, sha) in sorted(chosen.items()):
        out = TARBALLS / f"{org}__{repo}__{sha[:12]}.tar.gz"
        if out.exists() and out.stat().st_size > 0:
            log.info("have %s", out.name)
            continue
        url = f"https://codeload.github.com/{org}/{repo}/tar.gz/{sha}"
        for attempt in range(3):
            try:
                log.info("fetch %s/%s@%s (attempt %d)", org, repo, sha[:12], attempt + 1)
                with urllib.request.urlopen(url, timeout=120) as resp:
                    data = resp.read()
                out.write_bytes(data)
                log.info("  %s: %.1f MB", out.name, len(data) / 1e6)
                break
            except Exception as exc:  # noqa: BLE001 — retried, then surfaced below
                log.warning("  fetch failed: %s", exc)
                time.sleep(5 * (attempt + 1))
        else:
            log.error("GIVING UP on %s/%s@%s", org, repo, sha[:12])


def extract(chosen: dict[str, tuple[str, str]]) -> dict[str, set[str]]:
    """Extract allowlisted members; return repo -> set of corpus-relative paths."""
    CORPUS.mkdir(parents=True, exist_ok=True)
    present: dict[str, set[str]] = defaultdict(set)
    for repo, (org, sha) in sorted(chosen.items()):
        tb = TARBALLS / f"{org}__{repo}__{sha[:12]}.tar.gz"
        if not tb.exists():
            log.error("missing tarball for %s/%s — skipped", org, repo)
            continue
        root = CORPUS / f"{org}__{repo}"
        if root.exists() and any(root.iterdir()):
            for f in root.rglob("*"):
                if f.is_file():
                    present[repo].add(f.relative_to(CORPUS).as_posix())
            log.info("have extracted %s (%d files)", root.name, len(present[repo]))
            continue
        n = 0
        with tarfile.open(tb, "r:gz") as tf:
            for m in tf:
                if not m.isfile():
                    continue
                parts = Path(m.name).parts
                if len(parts) < 2 or ".." in parts:
                    continue
                rel = Path(*parts[1:])  # strip the <repo>-<sha>/ prefix
                if rel.suffix.lower() not in ALLOW_EXT:
                    continue
                if rel.suffix.lower() == ".json" and m.size > MAX_JSON_BYTES:
                    continue
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                fh = tf.extractfile(m)
                if fh is None:
                    continue
                dest.write_bytes(fh.read())
                present[repo].add((Path(root.name) / rel).as_posix())
                n += 1
        log.info("extracted %s: %d files", root.name, n)
    return present


def emit(issues: list[dict], chosen: dict[str, tuple[str, str]],
         present: dict[str, set[str]]) -> None:
    BENCH.mkdir(parents=True, exist_ok=True)
    needles, gold, meta = [], {}, {}
    drops = {"no_gold_paths": [], "gold_absent_from_snapshot": [], "no_snapshot": []}
    for i, x in enumerate(issues):
        name = f"muloc_{i:04d}"
        repo = x["repo_name"]
        if repo not in chosen or repo not in present:
            drops["no_snapshot"].append(name)
            continue
        org, sha = chosen[repo]
        gold_rel_all = []
        for f in (x.get("file_loc") or {}).get("files") or []:
            p = f.get("path")
            if p:
                gold_rel_all.append((Path(f"{org}__{repo}") / p).as_posix())
        if not gold_rel_all:
            drops["no_gold_paths"].append(name)
            continue
        gold_present = [p for p in gold_rel_all if p in present[repo]]
        if not gold_present:
            drops["gold_absent_from_snapshot"].append(name)
            continue
        body = (x.get("body") or "").strip()
        query = (x.get("title") or "").strip() + "\n\n" + body[:QUERY_BODY_CHARS]
        needles.append({"name": name, "query": query,
                        "question_type": "muloc_file_loc"})
        gold[name] = gold_present
        meta[name] = {
            "repo": f"{org}/{repo}",
            "snapshot_commit": sha,
            "issue_base_commit": x["base_commit"],
            "exact_commit": x["base_commit"] == sha,
            "issue_url": x.get("iss_html_url"),
            "gold_total": len(gold_rel_all),
            "gold_present": len(gold_present),
            "body_truncated": len(body) > QUERY_BODY_CHARS,
        }

    (BENCH / "needles_muloc.json").write_text(
        json.dumps({"needles": needles}, indent=1, ensure_ascii=False), encoding="utf-8")
    (BENCH / "gold_paths_muloc.json").write_text(
        json.dumps(gold, indent=1), encoding="utf-8")
    exact = sum(1 for m in meta.values() if m["exact_commit"])
    (BENCH / "needles_muloc_meta.json").write_text(
        json.dumps({
            "source": "hf:somethingone/MULocBench only_Python (842 issues, 45 repos)",
            "snapshot_strategy": "one snapshot per repo = most frequent base_commit (ties: first occurrence); per-needle exact_commit marks strict-validity subset",
            "snapshots": {r: {"org": o, "commit": c} for r, (o, c) in sorted(chosen.items())},
            "n_needles": len(needles), "n_exact_commit": exact,
            "drops": {k: v for k, v in drops.items()},
            "per_needle": meta,
        }, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("needles: %d (exact-commit subset: %d); drops: %s",
             len(needles), exact, {k: len(v) for k, v in drops.items()})


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stage", choices=["fetch", "emit", "all"])
    args = ap.parse_args()
    issues = load_issues()
    chosen = choose_snapshots(issues)
    log.info("%d issues, %d repos", len(issues), len(chosen))
    if args.stage in ("fetch", "all"):
        fetch(chosen)
    if args.stage in ("emit", "all"):
        present = extract(chosen)
        emit(issues, chosen, present)
    return 0


if __name__ == "__main__":
    sys.exit(main())

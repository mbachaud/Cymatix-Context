"""Fetch SWE-bench Verified repo snapshots + emit corpus and needle set.

Source: hf:princeton-nlp/SWE-bench_Verified (500 human-validated instances,
12 Python repos). Task here is issue -> file localization: the query is the
issue's ``problem_statement`` and gold is the set of files the gold ``patch``
touches (``diff --git a/<path>`` headers; ``test_patch`` files are not gold).

Haystack strategy (same family as ``scripts/build_mulocbench_corpus.py``, and
recorded because it bounds validity): 500 instances span 499 distinct
(repo, base_commit) pairs, so per-instance snapshots are infeasible for a
knowledge-store bench. We pin ONE snapshot per repo — the base_commit of the
instance at the repo's median ``created_at`` (minimises drift in both
directions) — fetched as a GitHub tarball, and keep every instance with at
least one gold file present in that snapshot. Per-needle meta records
``exact_commit`` (instance base_commit == snapshot; at most one or two per
repo here, so cite the drifted-majority numbers as near-neighbour-tree file
localization, not as the paper's setting).

Corpus: allowlisted text/code files only, extracted to
``<corpus-root>/<org>__<repo>/<relpath>``; the builder's 50..200,000-byte
ingest gate applies downstream.

Outputs (``benchmarks/dogfood/swebench``):
  needles_swebench.json       {"needles": [{name, query, question_type}]}
  gold_paths_swebench.json    needle -> [corpus-relative paths]
  needles_swebench_meta.json  per-needle repo/commit/drift/difficulty +
                              snapshot table + drop ledger

Usage:
  python scripts/build_swebench_corpus.py fetch
  python scripts/build_swebench_corpus.py emit
  python scripts/build_swebench_corpus.py all
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import tarfile
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

log = logging.getLogger("build_swebench_corpus")

DATASET = Path(r"F:/Projects/bench_pulls/SWE-bench_Verified/data/test-00000-of-00001.parquet")
TARBALLS = Path(r"F:/Projects/swebench/tarballs")
CORPUS = Path(r"F:/Projects/swebench/corpus")
BENCH = Path("benchmarks/dogfood/swebench")

ALLOW_EXT = {
    ".py", ".pyx", ".pyi", ".rst", ".md", ".txt", ".yml", ".yaml", ".json",
    ".toml", ".ini", ".cfg", ".sh", ".ts", ".tsx", ".js", ".h", ".c",
}
QUERY_CHARS = 2000            # MULoc parity: title + body truncated to 2,000 chars
MAX_JSON_BYTES = 200_000

_DIFF_HDR = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)


def load_instances() -> list[dict]:
    return pq.read_table(DATASET).to_pylist()


def gold_files(patch: str) -> list[str]:
    return sorted({m.group(2) for m in _DIFF_HDR.finditer(patch or "")})


def choose_snapshots(rows: list[dict]) -> dict[str, dict]:
    """repo -> {org, name, commit, created_at, n}: median-created_at instance's base_commit."""
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_repo[r["repo"]].append(r)
    chosen = {}
    for repo, rs in by_repo.items():
        rs = sorted(rs, key=lambda r: r["created_at"])
        mid = rs[len(rs) // 2]
        org, name = repo.split("/", 1)
        chosen[repo] = {"org": org, "name": name, "commit": mid["base_commit"],
                        "created_at": mid["created_at"], "n": len(rs),
                        "span": [rs[0]["created_at"], rs[-1]["created_at"]]}
    return chosen


def fetch(chosen: dict[str, dict]) -> None:
    TARBALLS.mkdir(parents=True, exist_ok=True)
    for repo, c in sorted(chosen.items()):
        out = TARBALLS / f"{c['org']}__{c['name']}__{c['commit'][:12]}.tar.gz"
        if out.exists() and out.stat().st_size > 0:
            log.info("have %s", out.name)
            continue
        url = f"https://codeload.github.com/{c['org']}/{c['name']}/tar.gz/{c['commit']}"
        for attempt in range(3):
            try:
                log.info("fetch %s@%s (attempt %d)", repo, c["commit"][:12], attempt + 1)
                with urllib.request.urlopen(url, timeout=300) as resp:
                    data = resp.read()
                out.write_bytes(data)
                log.info("  %s: %.1f MB", out.name, len(data) / 1e6)
                break
            except Exception as exc:  # noqa: BLE001 — retried, then surfaced below
                log.warning("  fetch failed: %s", exc)
                time.sleep(5 * (attempt + 1))
        else:
            log.error("GIVING UP on %s@%s", repo, c["commit"][:12])


def extract(chosen: dict[str, dict]) -> dict[str, set[str]]:
    CORPUS.mkdir(parents=True, exist_ok=True)
    present: dict[str, set[str]] = defaultdict(set)
    for repo, c in sorted(chosen.items()):
        tb = TARBALLS / f"{c['org']}__{c['name']}__{c['commit'][:12]}.tar.gz"
        if not tb.exists():
            log.error("missing tarball for %s — skipped", repo)
            continue
        root = CORPUS / f"{c['org']}__{c['name']}"
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
                rel = Path(*parts[1:])
                if rel.suffix.lower() not in ALLOW_EXT:
                    continue
                if rel.suffix.lower() == ".json" and m.size > MAX_JSON_BYTES:
                    continue
                fh = tf.extractfile(m)
                if fh is None:
                    continue
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(fh.read())
                present[repo].add((Path(root.name) / rel).as_posix())
                n += 1
        log.info("extracted %s: %d files", root.name, n)
    return present


def emit(rows: list[dict], chosen: dict[str, dict], present: dict[str, set[str]]) -> None:
    BENCH.mkdir(parents=True, exist_ok=True)
    needles, gold, meta = [], {}, {}
    drops = {"no_gold_paths": [], "gold_absent_from_snapshot": [], "no_snapshot": []}
    for r in rows:
        name = f"swe_{r['instance_id']}"
        repo = r["repo"]
        if repo not in chosen or repo not in present:
            drops["no_snapshot"].append(name)
            continue
        c = chosen[repo]
        prefix = f"{c['org']}__{c['name']}"
        gold_all = [f"{prefix}/{p}" for p in gold_files(r["patch"])]
        if not gold_all:
            drops["no_gold_paths"].append(name)
            continue
        gold_present = [p for p in gold_all if p in present[repo]]
        if not gold_present:
            drops["gold_absent_from_snapshot"].append(name)
            continue
        text = (r.get("problem_statement") or "").strip()
        needles.append({"name": name, "query": text[:QUERY_CHARS],
                        "question_type": "swebench_file_loc"})
        gold[name] = gold_present
        meta[name] = {
            "repo": repo, "snapshot_commit": c["commit"], "instance_base_commit": r["base_commit"],
            "exact_commit": r["base_commit"] == c["commit"], "created_at": r["created_at"],
            "difficulty": r.get("difficulty"), "gold_total": len(gold_all),
            "gold_present": len(gold_present), "query_truncated": len(text) > QUERY_CHARS,
        }
    exact = sum(1 for m in meta.values() if m["exact_commit"])
    (BENCH / "needles_swebench.json").write_text(
        json.dumps({"needles": needles}, indent=1, ensure_ascii=False), encoding="utf-8")
    (BENCH / "gold_paths_swebench.json").write_text(json.dumps(gold, indent=1), encoding="utf-8")
    (BENCH / "needles_swebench_meta.json").write_text(json.dumps({
        "source": "hf:princeton-nlp/SWE-bench_Verified (500 instances, 12 repos)",
        "snapshot_strategy": "one snapshot per repo = base_commit of the instance at the repo's "
                             "median created_at; per-needle exact_commit marks the strict-validity subset",
        "snapshots": chosen, "n_needles": len(needles), "n_exact_commit": exact,
        "query": f"problem_statement[:{QUERY_CHARS}]",
        "drops": drops, "per_needle": meta,
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("needles: %d (exact-commit subset: %d); drops: %s",
             len(needles), exact, {k: len(v) for k, v in drops.items()})


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stage", choices=["fetch", "emit", "all"])
    args = ap.parse_args()
    rows = load_instances()
    chosen = choose_snapshots(rows)
    if args.stage in ("fetch", "all"):
        fetch(chosen)
    if args.stage in ("emit", "all"):
        present = extract(chosen)
        emit(rows, chosen, present)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""ContextBench Step-0 arm-D: dump tasks (checkout worktrees) for the smoke gold set.

Run with the cb-step0 venv (has contextbench via PYTHONPATH, pyarrow).
Emits F:/tmp/cb_tasks_smoke.json = list of
  {instance_id, repo, repo_url, base_commit, problem_statement, worktree_dir}
Worktrees are already cloned/warm; checkout() returns the existing worktree fast.
Any checkout returning None is skipped and recorded (printed at the end).
Read-only on contextbench source.
"""
import json
import os
import sys

GOLD = "F:/Projects/helix-context/benchmarks/contextbench/gold_smoke_4repo.parquet"
CACHE = "F:/Projects/_cache/cb_repos"
OUT = "F:/tmp/cb_tasks_smoke.json"
CONTEXTBENCH_SRC = os.environ.get("CONTEXTBENCH_SRC", "F:/Projects/contextbench-src")

os.environ["CONTEXTBENCH_TMP_ROOT"] = "F:/Projects/_cache/cb_wt"
os.environ["GIT_LFS_SKIP_SMUDGE"] = "1"
sys.path.insert(0, CONTEXTBENCH_SRC)

from contextbench.core import checkout  # noqa: E402
import pyarrow.dataset as ds  # noqa: E402


def main():
    rows = ds.dataset(GOLD, format="parquet").to_table().to_pylist()
    print(f"loaded {len(rows)} gold rows from {GOLD}", file=sys.stderr)

    tasks = []
    checkout_errors = {}
    for i, r in enumerate(rows):
        iid = r["instance_id"]
        repo_url = r.get("repo_url")
        base_commit = r.get("base_commit")
        try:
            gc = json.loads(r["gold_context"]) if r.get("gold_context") else []
        except Exception:  # noqa: BLE001
            gc = []
        gold_files = sorted({(e.get("file") or "").replace("\\", "/").lstrip("/")
                             for e in gc if e.get("file")})
        print(f"[checkout {i+1}/{len(rows)}] {iid} {r.get('repo')}@{(base_commit or '')[:10]}",
              file=sys.stderr)
        try:
            wt = checkout(repo_url, base_commit, CACHE)
        except Exception as e:  # noqa: BLE001
            wt = None
            checkout_errors[iid] = repr(e)
        if not wt or not os.path.isdir(wt):
            checkout_errors[iid] = checkout_errors.get(iid, "checkout_returned_none")
            print(f"  CHECKOUT FAILED: {checkout_errors[iid]}", file=sys.stderr)
            continue
        tasks.append({
            "instance_id": iid,
            "repo": r.get("repo"),
            "repo_url": repo_url,
            "base_commit": base_commit,
            "problem_statement": r.get("problem_statement") or "",
            "worktree_dir": wt.replace("\\", "/"),
            "gold_files": gold_files,
        })
        print(f"  ok -> {wt}", file=sys.stderr)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2)
    print(f"\nwrote {len(tasks)} tasks -> {OUT}", file=sys.stderr)
    if checkout_errors:
        print(f"checkout_errors ({len(checkout_errors)}): {json.dumps(checkout_errors, indent=2)}",
              file=sys.stderr)
    else:
        print("checkout_errors: none", file=sys.stderr)


if __name__ == "__main__":
    main()

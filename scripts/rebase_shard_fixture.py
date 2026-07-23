"""rebase_shard_fixture.py — relocate a sharded knowledge-store fixture set
to a new host ("needle adapter", cc-exchange spark-erb-receipts 0007/0008,
unblocks the #275 shard A/B cell).

The problem this solves
-----------------------
A sharded fixture is a coordinator ``main.genome.db`` plus shard ``.db``
files. The coordinator's ``shards.path`` column records each shard's location
as an ABSOLUTE path on the box the fixture was built on. Copy the fixture to
another host and every recorded path is foreign: ``ShardRouter._open_shard``
either finds nothing or — worse — a per-shard ``Genome`` silently CREATES an
empty db at the foreign path (mkdir + fresh schema), so every arm returns
0.000 delivery / 0.000 pool with rc=0 and no error anywhere. A "does the code
path execute" pre-check passes; serving is dead.

What it does
------------
1. **Discover** — open the coordinator read-only, enumerate its recorded
   shard paths (all rows, healthy or not). A coordinator with zero shard
   rows hard-fails: that is the empty-coordinator signature, usually meaning
   the real ``main.genome.db`` did not travel with the fixture (an empty one
   was auto-created at a wrong path).
2. **Rewrite** — match every recorded path against the ``*.db`` files
   actually present under the fixture root by longest path-component suffix
   (case-insensitive, separator-agnostic — POSIX-recorded paths match
   Windows trees and vice versa). Rewrites are absolute paths under the new
   root: the router hands ``shards.path`` verbatim to ``sqlite3.connect``,
   so relative paths would resolve against the process CWD and break the
   moment the server starts from a different directory. Missing or ambiguous
   matches hard-fail with a per-shard diagnostic. ``--dry-run`` prints the
   plan and touches nothing.
3. **Serve verification** — reopen the coordinator through the real
   ``ShardRouter`` path, open every healthy shard (refusing to open a path
   whose file is absent, so nothing is ever auto-created), and print the
   gene-rows-served count per shard. Zero total rows, or any shard that
   fails to open, is a hard fail. This asserts SERVING, not execution.
4. **Smoke gate** — run N probe queries (default 3, auto-derived from the
   fixture's own fingerprint terms, or supply ``--probe``) through
   ``ShardedGenomeAdapter.query_docs`` — the exact serving path benches
   use. Per arm, every probe must return nonzero delivery AND nonzero
   candidate pool or the tool exits nonzero. Arms: ``lexical`` always;
   ``fused`` (per-shard FTS+dense fusion) only when torch imports and at
   least one shard has ``embedding_dense_v2`` vectors — absent that, the
   gate degrades to lexical-only with an explicit warning, never silently.

Usage
-----
    # Rebase a fixture copied to this box, then verify + gate:
    python scripts/rebase_shard_fixture.py /path/to/fixture-root

    # See the rewrite plan without touching anything:
    python scripts/rebase_shard_fixture.py /path/to/fixture-root --dry-run

    # Pre-run gate only (no rewrite) — use before every bench run:
    python scripts/rebase_shard_fixture.py /path/to/fixture-root --verify-only

The fixture root is the directory holding ``main.genome.db`` (a direct path
to the coordinator db also works). Ship ``main.genome.db`` together with its
``-wal``/``-shm`` siblings, or checkpoint before copying
(``PRAGMA wal_checkpoint(TRUNCATE)``) — a coordinator copied without its WAL
can be missing recent rows.

Exit codes
----------
    0  rewrite (if any) applied, serving verified, smoke gate green
    2  coordinator missing or unreadable
    3  coordinator has zero shard rows (empty-coordinator signature)
    4  one or more recorded shards have no / ambiguous file under the root
    5  serve verification failed (shard missing/unopenable, or zero gene
       rows served)
    6  smoke gate failed (an arm returned zero delivery or zero pool)

Known limits
------------
- ``source_index.repo_root`` / ``source_id`` provenance strings are NOT
  rewritten (metadata only — nothing on the serving path reads them to
  open files). Citations keep origin-host paths.
- The tool relocates; it does not repair. A shard that is itself empty or
  schema-incompatible fails verification rather than being rebuilt.
- Dense probes need the dense stack importable AND vectors present in the
  shards (``embedding_dense_v2`` backfilled); otherwise the gate is
  lexical-only and says so.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rebase.shard_fixture")

EXIT_OK = 0
EXIT_COORDINATOR_INVALID = 2
EXIT_NO_SHARD_ROWS = 3
EXIT_UNMATCHED_SHARDS = 4
EXIT_SERVE_FAILED = 5
EXIT_SMOKE_FAILED = 6

_COORDINATOR_NAMES = ("main.genome.db", "main.db")
_DB_SUFFIXES = (".db",)
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


# ── Discovery ────────────────────────────────────────────────────────


def find_coordinator(target: Path) -> Optional[Path]:
    """Resolve the coordinator db from a fixture root dir or a direct path."""
    if target.is_file():
        return target
    if target.is_dir():
        for name in _COORDINATOR_NAMES:
            cand = target / name
            if cand.is_file():
                return cand
    return None


def discover_shard_files(root: Path, coordinator: Path) -> list[Path]:
    """All ``*.db`` files under ``root`` except the coordinator + sidecars."""
    out: list[Path] = []
    coord = coordinator.resolve()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        name = p.name.lower()
        if not name.endswith(_DB_SUFFIXES):
            continue
        if any(name.endswith(f".db{s}") for s in _SIDECAR_SUFFIXES):
            continue
        if p.resolve() == coord:
            continue
        out.append(p)
    return out


def _components(path_str: str) -> list[str]:
    """Lower-cased path components, separator- and drive-agnostic."""
    norm = path_str.replace("\\", "/")
    parts = [c for c in norm.split("/") if c and c != "."]
    # Drop a bare drive component ("F:" / "f:") so origin-drive prefixes
    # never block a suffix match.
    return [c.lower() for c in parts if not (len(c) == 2 and c[1] == ":")]


def _suffix_overlap(recorded: list[str], candidate: list[str]) -> int:
    """Number of trailing path components the two share."""
    n = 0
    for a, b in zip(reversed(recorded), reversed(candidate)):
        if a != b:
            break
        n += 1
    return n


@dataclass
class RewritePlan:
    shard_name: str
    old_path: str
    new_path: Optional[str]          # None => unmatched
    matched_components: int = 0
    error: Optional[str] = None      # set when unmatched/ambiguous


def plan_rewrites(
    rows: list[sqlite3.Row],
    discovered: list[Path],
) -> list[RewritePlan]:
    """Match each recorded shard path to a discovered file by longest
    component-suffix. At minimum the filename must match; ties between two
    discovered files are ambiguous and hard-fail rather than guess."""
    disc = [(p, _components(str(p))) for p in discovered]
    plans: list[RewritePlan] = []
    for row in rows:
        rec = _components(row["path"])
        best: list[tuple[int, Path]] = []
        for p, comps in disc:
            n = _suffix_overlap(rec, comps)
            if n >= 1:  # filename itself must match
                best.append((n, p))
        if not best:
            plans.append(RewritePlan(
                shard_name=row["shard_name"], old_path=row["path"],
                new_path=None,
                error="no file under the fixture root matches "
                      f"basename {rec[-1] if rec else '?'!r}",
            ))
            continue
        best.sort(key=lambda t: (-t[0], str(t[1]).lower()))
        top_n = best[0][0]
        tied = [p for n, p in best if n == top_n]
        if len(tied) > 1:
            plans.append(RewritePlan(
                shard_name=row["shard_name"], old_path=row["path"],
                new_path=None,
                error="ambiguous — equally-good matches: "
                      + ", ".join(str(p) for p in tied),
            ))
            continue
        plans.append(RewritePlan(
            shard_name=row["shard_name"], old_path=row["path"],
            new_path=str(tied[0].resolve()), matched_components=top_n,
        ))
    return plans


# ── Rewrite ──────────────────────────────────────────────────────────


def apply_rewrites(coordinator: Path, plans: list[RewritePlan]) -> int:
    """UPDATE shards.path in one transaction. Returns rows changed."""
    import time
    conn = sqlite3.connect(str(coordinator), timeout=30)
    try:
        now = time.time()
        with conn:  # single transaction
            changed = 0
            for plan in plans:
                if plan.new_path is None or plan.new_path == plan.old_path:
                    continue
                conn.execute(
                    "UPDATE shards SET path = ?, updated_at = ? "
                    "WHERE shard_name = ?",
                    (plan.new_path, now, plan.shard_name),
                )
                changed += 1
        return changed
    finally:
        conn.close()


# ── Serve verification ───────────────────────────────────────────────


def serve_verification(coordinator: Path) -> tuple[int, list[str]]:
    """Open every healthy shard through the real ShardRouter and count the
    gene rows it can serve. Returns (total_rows, failures).

    A recorded path whose file does not exist is a failure — it is NOT
    opened, because a per-shard Genome would auto-create an empty db there
    and manufacture exactly the silent-zero state this tool exists to kill.
    """
    from cymatix_context.shard_router import ShardRouter

    router = ShardRouter(main_path=str(coordinator))
    failures: list[str] = []
    total = 0
    try:
        names = router.known_shards()
        if not names:
            return 0, ["no healthy shards registered in the coordinator"]
        for name in names:
            row = router.main_conn.execute(
                "SELECT path FROM shards WHERE shard_name = ?", (name,)
            ).fetchone()
            path = row["path"] if row else None
            if not path or not os.path.isfile(path):
                failures.append(
                    f"shard {name!r}: file missing at recorded path {path!r}"
                )
                continue
            try:
                shard = router._open_shard(name)
                n = int(shard.conn.execute(
                    "SELECT COUNT(*) FROM genes"
                ).fetchone()[0])
            except Exception as exc:
                failures.append(f"shard {name!r}: open/count failed: {exc}")
                continue
            print(f"  shard {name:<24} {n:>9} gene rows served  ({path})")
            if n == 0:
                failures.append(f"shard {name!r}: opens but serves 0 gene rows")
            total += n
    finally:
        router.close()
    return total, failures


# ── Smoke gate ───────────────────────────────────────────────────────


def derive_probes(coordinator: Path, count: int) -> list[str]:
    """Pick probe terms from the fixture's own fingerprint domains/entities.

    Most-frequent first, terms > 2 chars (mirrors the router's IDF-term
    filter), deterministic. The gate then requires these to actually
    retrieve — a term the fixture itself tagged MUST hit if serving works.
    """
    conn = sqlite3.connect(f"file:{coordinator.as_posix()}?mode=ro", uri=True)
    freq: dict[str, int] = {}
    try:
        rows = conn.execute(
            "SELECT domains, entities FROM fingerprint_index LIMIT 20000"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    for domains_json, entities_json in rows:
        for raw in (domains_json, entities_json):
            if not raw:
                continue
            try:
                terms = json.loads(raw) or []
            except Exception:
                continue
            for t in terms:
                t = str(t).strip().lower()
                if len(t) > 2:
                    freq[t] = freq.get(t, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    return [t for t, _n in ranked[:count]]


def _dense_available(coordinator: Path) -> tuple[bool, str]:
    """(usable, reason). Usable = torch imports AND some shard has
    embedding_dense_v2 vectors."""
    try:
        import torch  # noqa: F401
    except Exception as exc:
        return False, f"torch unavailable ({exc.__class__.__name__})"
    conn = sqlite3.connect(f"file:{coordinator.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT path FROM shards WHERE health='ok'").fetchall()
    finally:
        conn.close()
    for (path,) in rows:
        if not os.path.isfile(path):
            continue
        sconn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        try:
            n = sconn.execute(
                "SELECT COUNT(*) FROM genes "
                "WHERE embedding_dense_v2 IS NOT NULL LIMIT 1"
            ).fetchone()[0]
            if int(n) > 0:
                return True, "torch + embedding_dense_v2 present"
        except sqlite3.OperationalError:
            continue
        finally:
            sconn.close()
    return False, "no shard has embedding_dense_v2 vectors (run the backfill)"


def smoke_gate(
    coordinator: Path,
    probes: list[str],
    max_genes: int,
    no_dense: bool,
) -> tuple[bool, list[str]]:
    """Run the probes through the real sharded serving path, per arm.

    Gate contract (spark-erb-receipts 0008 §3): EVERY probe in EVERY
    attempted arm must return nonzero delivery AND nonzero candidate pool.
    Returns (passed, report_lines).
    """
    from cymatix_context.sharding import ShardedGenomeAdapter

    arms: list[tuple[str, dict]] = [("lexical", {})]
    if no_dense:
        log.warning("smoke gate: --no-dense set; fused arm skipped by request")
    else:
        ok, reason = _dense_available(coordinator)
        if ok:
            arms.append(("fused", {"dense_embedding_enabled": True}))
        else:
            log.warning(
                "smoke gate DEGRADED to lexical-only: %s — the fused arm "
                "was NOT exercised", reason,
            )

    lines: list[str] = []
    passed = True
    for arm_name, kwargs in arms:
        adapter = ShardedGenomeAdapter(main_path=str(coordinator), **kwargs)
        try:
            for term in probes:
                try:
                    genes = adapter.query_docs(
                        domains=[term], entities=[],
                        max_genes=max_genes, read_only=True,
                    )
                except Exception as exc:
                    passed = False
                    lines.append(
                        f"  [{arm_name}] probe {term!r}: EXCEPTION {exc!r}"
                    )
                    continue
                delivery = len(genes)
                pool = len(adapter.last_query_scores)
                verdict = "ok" if (delivery > 0 and pool > 0) else "FAIL"
                if verdict == "FAIL":
                    passed = False
                lines.append(
                    f"  [{arm_name}] probe {term!r}: delivery={delivery} "
                    f"pool={pool}  {verdict}"
                )
        finally:
            adapter.close()
    return passed, lines


# ── Main ─────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Relocate a sharded fixture set (coordinator + shards) "
                    "to this host: rewrite coordinator shard paths, verify "
                    "serving, run the per-arm smoke gate.",
    )
    ap.add_argument(
        "fixture_root",
        help="Fixture root dir containing main.genome.db (or a direct path "
             "to the coordinator db).",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print the rewrite plan and exit without touching anything.",
    )
    ap.add_argument(
        "--verify-only", action="store_true",
        help="Skip the rewrite; run serve verification + smoke gate against "
             "the coordinator as-is. Use as a pre-run gate before benches.",
    )
    ap.add_argument(
        "--probe", action="append", default=None, metavar="TERM",
        help="Probe query term for the smoke gate (repeatable). Default: "
             "auto-derived from the fixture's fingerprint terms.",
    )
    ap.add_argument(
        "--probe-count", type=int, default=3,
        help="Number of auto-derived probes (default 3).",
    )
    ap.add_argument(
        "--max-genes", type=int, default=8,
        help="max_genes per probe query (default 8).",
    )
    ap.add_argument(
        "--no-dense", action="store_true",
        help="Skip the fused (dense) arm even when the dense stack is present.",
    )
    args = ap.parse_args(argv)

    target = Path(args.fixture_root)
    coordinator = find_coordinator(target)
    if coordinator is None:
        print(
            f"ERROR: no coordinator db found at {target} "
            f"(looked for {' / '.join(_COORDINATOR_NAMES)})",
            file=sys.stderr,
        )
        return EXIT_COORDINATOR_INVALID
    root = coordinator.parent
    print(f"[rebase] coordinator: {coordinator}")

    # Read the registry read-only — a missing/foreign coordinator must not
    # be auto-created by this step.
    try:
        conn = sqlite3.connect(f"file:{coordinator.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT shard_name, category, path, health FROM shards "
            "ORDER BY shard_name"
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print(f"ERROR: cannot read coordinator: {exc}", file=sys.stderr)
        return EXIT_COORDINATOR_INVALID

    if not rows:
        print(
            "ERROR: coordinator has ZERO shard rows. This is the "
            "empty-coordinator signature: the real main.genome.db likely "
            "did not travel with the fixture (an empty one may have been "
            "auto-created at a wrong path). Re-copy the fixture including "
            "main.genome.db and its -wal/-shm siblings, or checkpoint "
            "before copying.",
            file=sys.stderr,
        )
        return EXIT_NO_SHARD_ROWS

    if not args.verify_only:
        discovered = discover_shard_files(root, coordinator)
        print(f"[rebase] {len(rows)} shard row(s) registered, "
              f"{len(discovered)} candidate db file(s) under {root}")
        plans = plan_rewrites(rows, discovered)

        bad = [p for p in plans if p.error]
        for plan in plans:
            if plan.error:
                print(f"  {plan.shard_name:<24} UNMATCHED: {plan.error}")
            elif plan.new_path == plan.old_path:
                print(f"  {plan.shard_name:<24} unchanged: {plan.old_path}")
            else:
                print(f"  {plan.shard_name:<24} {plan.old_path}")
                print(f"  {'':<24} -> {plan.new_path} "
                      f"(matched {plan.matched_components} trailing components)")
        if bad:
            print(
                f"ERROR: {len(bad)} shard(s) could not be matched under "
                f"{root}; nothing was rewritten.",
                file=sys.stderr,
            )
            return EXIT_UNMATCHED_SHARDS

        if args.dry_run:
            print("[rebase] --dry-run: no changes applied.")
            return EXIT_OK

        changed = apply_rewrites(coordinator, plans)
        print(f"[rebase] rewrote {changed} shard path(s).")

    # ── Serve verification (assert SERVING, not execution) ──────────
    print("[verify] opening every healthy shard through ShardRouter:")
    total, failures = serve_verification(coordinator)
    print(f"[verify] total gene rows served: {total}")
    if failures or total == 0:
        for f in failures:
            print(f"  FAIL: {f}", file=sys.stderr)
        print(
            "ERROR: serve verification failed — the coordinator does not "
            "serve gene rows on this box. A bench run against this fixture "
            "would return all-zero metrics with rc=0.",
            file=sys.stderr,
        )
        return EXIT_SERVE_FAILED

    # ── Smoke gate ──────────────────────────────────────────────────
    probes = args.probe if args.probe else derive_probes(coordinator, args.probe_count)
    if not probes:
        print(
            "ERROR: no probe terms derivable from fingerprint_index and "
            "none supplied via --probe.",
            file=sys.stderr,
        )
        return EXIT_SMOKE_FAILED
    print(f"[gate] SMOKE GATE: probes={probes} max_genes={args.max_genes}")
    passed, lines = smoke_gate(
        coordinator, probes, args.max_genes, args.no_dense,
    )
    for line in lines:
        print(line)
    if not passed:
        print(
            "ERROR: SMOKE GATE FAILED — at least one probe returned zero "
            "delivery or zero pool. Do not run benches against this fixture.",
            file=sys.stderr,
        )
        return EXIT_SMOKE_FAILED
    print("[gate] SMOKE GATE PASSED: nonzero delivery AND nonzero pool "
          "on every probe in every attempted arm.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

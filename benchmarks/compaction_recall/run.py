"""Compaction-recall bench runner (stage 1: retrieval-level, no model calls).

Usage::

    python -m benchmarks.compaction_recall.run --out-dir F:/tmp/compaction-recall/<run-id>
    python -m benchmarks.compaction_recall.run --dry-run          # needle census only
    python -m benchmarks.compaction_recall.run --arms bm25,cymatix --limit-events 2

Writes into ``--out-dir`` (keep it outside the repo: per-needle rows quote
transcript text):

* ``needles.jsonl``             — the needle set (event, class, key, query, qualifier)
* ``results/<arm>/<event>.jsonl`` — per-needle hit / hedge / delivered tokens;
  one file per (arm, event), so a rerun skips finished units
* ``receipt.json``              — aggregates + identity (commit, behind count,
  transcript sha256s, needle-set sha256, arm descriptions incl. the cymatix
  config as loaded at runtime). Safe to commit: no transcript text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from . import arms as arms_mod
from .needles import Needle, contains, extract_needles, hedge_near
from .transcript import Event, haystack_units, load_events

REPO_ROOT = Path(__file__).resolve().parents[2]
RETRIEVAL_ARMS = ("bm25", "dense", "cymatix")
TEXT_ARMS = tuple(arms_mod.TEXT_ARMS)


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                              timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                              ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def discover(globs: list[str]) -> list[Path]:
    out = []
    for g in globs:
        base = Path(g).expanduser()
        for p in sorted(base.parent.glob(base.name)) if "*" in base.name else [base]:
            for q in (sorted(p.glob("*.jsonl")) if p.is_dir() else [p]):
                with open(q, encoding="utf-8", errors="replace") as fh:
                    if '"subtype":"compact_boundary"' in fh.read():
                        out.append(q)
    return sorted(set(out))


def event_chunks(ev: Event) -> list[str]:
    chunks: list[str] = []
    for u in haystack_units(ev):
        chunks.extend(arms_mod.chunk_text(f"[{u.role}]\n{u.text}"))
    return chunks


def _needle_row(ev: Event, n: Needle) -> dict:
    return {"event": ev.boundary_uuid, "cls": n.cls, "key": n.key, "pre_units": n.pre_units,
            "use_index": n.use_index, "use_role": n.use_role,
            "seen_post_before_use": n.seen_post_before_use, "qualifier": n.qualifier,
            "query": n.query}


def _score(text: str, n: Needle) -> dict:
    hit = contains(text, n.cls, n.key)
    row = {"hit": hit, "tokens": len(text) // 4}
    if n.qualifier:
        row["hedge"] = bool(hit and hedge_near(text, n.cls, n.key))
    return row


class _Lock:
    def __init__(self, out_dir: Path) -> None:
        self.path = out_dir / "RUNNING.lock"

    def __enter__(self):
        if self.path.exists():
            pid = self.path.read_text(encoding="utf-8").strip()
            raise SystemExit(f"{self.path} exists (pid {pid}); a runner may still be writing. "
                             "Check the process, then delete the lock to resume.")
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def run_text_arms(events, needles_by_event, out_dir: Path) -> None:
    for arm in TEXT_ARMS:
        d = out_dir / "results" / arm
        d.mkdir(parents=True, exist_ok=True)
        fn = arms_mod.TEXT_ARMS[arm]
        for ev in events:
            text = fn(ev)
            rows = []
            for n in needles_by_event[ev.boundary_uuid]:
                r = _score(text, n) if text else {"hit": None, "tokens": 0}
                rows.append({"cls": n.cls, "key": n.key, **r, "available": bool(text)})
            (d / f"{ev.boundary_uuid}.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def make_arm(name: str, args):
    if name == "bm25":
        return arms_mod.BM25Arm()
    if name == "dense":
        return arms_mod.DenseArm()
    if name == "cymatix":
        return arms_mod.CymatixArm(config_path=args.cymatix_config,
                                   root=str(Path(args.out_dir) / "genomes"))
    raise ValueError(name)


def run_retrieval_arm(name, events, needles_by_event, args, out_dir: Path) -> dict:
    d = out_dir / "results" / name
    d.mkdir(parents=True, exist_ok=True)
    arm = make_arm(name, args)
    loaded: list[str] = []          # chunk sequence currently in the arm
    stats = {"events": 0, "ingest_s": 0.0, "query_s": 0.0, "queries": 0}
    try:
        for ev in events:
            target = d / f"{ev.boundary_uuid}.jsonl"
            ns = needles_by_event[ev.boundary_uuid]
            if target.exists() or not ns:
                continue
            chunks = event_chunks(ev)
            t0 = time.time()
            if loaded and chunks[:len(loaded)] == loaded:
                arm.add(chunks[len(loaded):])      # same chain, later boundary
            else:
                arm.reset()
                arm.add(chunks)
            loaded = chunks
            stats["ingest_s"] += time.time() - t0
            t0 = time.time()
            rows = []
            for n in ns:
                got = arm.query(n.query, args.k)
                rows.append({"cls": n.cls, "key": n.key, **_score(got.text, n),
                             "n_chunks": got.n_chunks, "haystack_chunks": arm.size,
                             "available": True})
            stats["query_s"] += time.time() - t0
            stats["queries"] += len(ns)
            stats["events"] += 1
            tmp = target.with_suffix(".part")
            tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            tmp.replace(target)
            print(f"  [{name}] {ev.boundary_uuid[:8]} chunks={arm.size} needles={len(ns)} "
                  f"hit={sum(r['hit'] for r in rows)}", flush=True)
        stats["describe"] = arm.describe() if hasattr(arm, "describe") else {}
    finally:
        if hasattr(arm, "close"):
            arm.close()
    return stats


def aggregate(events, needles_by_event, arm_names, out_dir: Path) -> dict:
    rows: dict[str, dict[tuple, dict]] = defaultdict(dict)
    for arm in arm_names:
        for ev in events:
            p = out_dir / "results" / arm / f"{ev.boundary_uuid}.jsonl"
            if not p.exists():
                continue
            for r in map(json.loads, p.read_text(encoding="utf-8").splitlines()):
                rows[arm][(ev.boundary_uuid, r["cls"], r["key"])] = r

    all_needles = [(ev.boundary_uuid, n) for ev in events for n in needles_by_event[ev.boundary_uuid]]
    keys = [(e, n.cls, n.key) for e, n in all_needles]
    quals = {(e, n.cls, n.key) for e, n in all_needles if n.qualifier}

    def rate(arm, subset, field="hit"):
        got = [rows[arm].get(k) for k in subset]
        got = [g for g in got if g is not None and g.get("available")]
        if not got:
            return None
        return {"n": len(got), "rate": round(sum(bool(g.get(field)) for g in got) / len(got), 4)}

    def union_rate(a, b, subset):
        both = [(rows[a].get(k), rows[b].get(k)) for k in subset]
        both = [(x, y) for x, y in both if x and y and x.get("available") and y.get("available")]
        if not both:
            return None
        return {"n": len(both), "rate": round(sum(bool(x["hit"] or y["hit"]) for x, y in both)
                                               / len(both), 4)}

    by_cls = defaultdict(list)
    for k in keys:
        by_cls[k[1]].append(k)
    # Where the detail came from before the agent wrote it again: a tool result
    # (re-read a file / re-ran a command), the user, or nowhere visible.
    by_origin = defaultdict(list)
    for e, n in all_needles:
        origin = {"tool_result": "refetched", "user": "user_supplied"}.get(
            n.seen_post_before_use or "", "unprompted")
        by_origin[origin].append((e, n.cls, n.key))

    out = {"needles": len(keys), "events_with_needles": sum(1 for ev in events
                                                            if needles_by_event[ev.boundary_uuid]),
           "by_class_counts": {c: len(v) for c, v in sorted(by_cls.items())},
           "by_origin_counts": {o: len(v) for o, v in sorted(by_origin.items())},
           "qualifier_needles": len(quals), "arms": {}}
    for arm in arm_names:
        if not rows[arm]:
            continue
        a = {"hit": rate(arm, keys),
             "by_class": {c: rate(arm, v) for c, v in sorted(by_cls.items())},
             "by_origin": {o: rate(arm, v) for o, v in sorted(by_origin.items())},
             "hedge_kept_on_qualifier_needles": rate(arm, sorted(quals), "hedge"),
             "hit_on_qualifier_needles": rate(arm, sorted(quals)),
             "median_tokens": statistics.median([r["tokens"] for r in rows[arm].values()
                                                 if r.get("available")] or [0])}
        # macro average over events, so one long session cannot carry the number
        per_ev = []
        for ev in events:
            sub = [k for k in keys if k[0] == ev.boundary_uuid]
            r = rate(arm, sub)
            if r:
                per_ev.append(r["rate"])
        a["hit_macro_over_events"] = round(statistics.mean(per_ev), 4) if per_ev else None
        if arm != "today" and rows["today"]:
            a["today_union"] = union_rate("today", arm, keys)
            missed = [k for k in keys if rows["today"].get(k) and not rows["today"][k]["hit"]]
            a["recovers_today_misses"] = rate(arm, missed)
        out["arms"][arm] = a

    # paired cymatix vs bm25 (the "is it more than BM25" question)
    if rows["cymatix"] and rows["bm25"]:
        shared = [k for k in keys if k in rows["cymatix"] and k in rows["bm25"]]
        wins = sum(1 for k in shared if rows["cymatix"][k]["hit"] and not rows["bm25"][k]["hit"])
        losses = sum(1 for k in shared if rows["bm25"][k]["hit"] and not rows["cymatix"][k]["hit"])
        out["paired_cymatix_vs_bm25"] = {"n": len(shared), "cymatix_only": wins,
                                         "bm25_only": losses}
    if rows["cymatix"] and rows["dense"]:
        shared = [k for k in keys if k in rows["cymatix"] and k in rows["dense"]]
        out["paired_cymatix_vs_dense"] = {
            "n": len(shared),
            "cymatix_only": sum(1 for k in shared if rows["cymatix"][k]["hit"]
                                and not rows["dense"][k]["hit"]),
            "dense_only": sum(1 for k in shared if rows["dense"][k]["hit"]
                              and not rows["cymatix"][k]["hit"])}
    return out


def transcript_identity(files) -> list[dict]:
    """Receipt identity for the input transcripts: size and sha256 only.

    A transcript's folder is named after the local project it came from, so
    the receipt carries a positional label instead of the real name. The
    sha256 is what identifies the file.
    """
    return [{"name": f"transcript-{i:02d}.jsonl", "bytes": p.stat().st_size,
             "sha256": _sha256(p)} for i, p in enumerate(files, 1)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transcripts", action="append",
                    help="file, directory or glob of *.jsonl (default ~/.claude/projects/*)")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--arms", default="today,summary,plan,handoff,bm25,dense,cymatix")
    ap.add_argument("--k", type=int, default=12, help="chunks per query (12 = shipped max_genes)")
    ap.add_argument("--limit-events", type=int, default=0)
    ap.add_argument("--cymatix-config", default=None,
                    help="cymatix.toml for the cymatix arm (default: load_config() resolution)")
    ap.add_argument("--dry-run", action="store_true", help="needle census only, no retrieval")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    files = discover(args.transcripts or [str(Path.home() / ".claude" / "projects" / "*")])
    # Hash before parsing: live sessions keep appending to their files.
    transcript_ids = transcript_identity(files)
    events = load_events(files)
    if args.limit_events:
        events = events[: args.limit_events]
    needles_by_event = {ev.boundary_uuid: extract_needles(ev) for ev in events}
    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]

    census = []
    for ev in events:
        ns = needles_by_event[ev.boundary_uuid]
        chunks = event_chunks(ev)
        census.append({
            "event": ev.boundary_uuid, "file": Path(ev.source_file).parent.name[:48],
            "trigger": ev.trigger, "pre_tokens": ev.pre_tokens, "post_tokens": ev.post_tokens,
            "haystack_chunks": len(chunks), "haystack_tokens": sum(map(len, chunks)) // 4,
            "today_tokens": len(arms_mod.today_text(ev)) // 4,
            "plan": bool(ev.plan_text), "handoff": bool(arms_mod.handoff_text(ev)),
            "post_units": len(ev.post), "needles": len(ns),
            "by_class": {c: sum(1 for n in ns if n.cls == c)
                         for c in ("sha", "path", "ref", "number", "ident")},
            "qualifier": sum(1 for n in ns if n.qualifier)})
    print(f"{len(files)} transcripts, {len(events)} unique compaction events, "
          f"{sum(c['needles'] for c in census)} needles")
    for c in census:
        print(f"  {c['event'][:8]} {c['file'][:40]:40s} {c['trigger'] or '?':6s} "
              f"hay={c['haystack_chunks']:6d}ch/{c['haystack_tokens']:8d}tok "
              f"today={c['today_tokens']:6d}tok needles={c['needles']:4d} {c['by_class']} "
              f"q={c['qualifier']} plan={int(c['plan'])} handoff={int(c['handoff'])}")
    if args.dry_run:
        return 0

    out_dir = Path(args.out_dir or REPO_ROOT / "benchmarks" / "results" /
                   f"compaction_recall_{time.strftime('%Y%m%dT%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    with _Lock(out_dir):
        needle_rows = [_needle_row(ev, n) for ev in events for n in needles_by_event[ev.boundary_uuid]]
        blob = "".join(json.dumps(r, sort_keys=True) + "\n" for r in needle_rows)
        (out_dir / "needles.jsonl").write_text(blob, encoding="utf-8")
        needle_sha = hashlib.sha256(blob.encode("utf-8")).hexdigest()

        t_start = time.time()
        run_text_arms(events, needles_by_event, out_dir)
        arm_stats = {}
        for name in arm_names:
            if name in RETRIEVAL_ARMS:
                print(f"[{name}] running", flush=True)
                arm_stats[name] = run_retrieval_arm(name, events, needles_by_event, args, out_dir)

        receipt = {
            "bench": "compaction_recall stage 1 (retrieval-level, no model calls)",
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "commit": _git("rev-parse", "HEAD"),
            "dirty": bool(_git("status", "--porcelain")),
            "behind_origin_master": _git("rev-list", "--count", "HEAD..origin/master"),
            "python": platform.python_version(),
            "pythonhashseed": os.environ.get("PYTHONHASHSEED"),
            "k": args.k, "chunk_chars": arms_mod.CHUNK_CHARS,
            "transcripts": transcript_ids,
            "needle_set_sha256": needle_sha,
            "census": [{k: v for k, v in c.items() if k != "file"} for c in census],
            "arm_stats": arm_stats,
            "seconds": round(time.time() - t_start, 1),
            "results": aggregate(events, needles_by_event,
                                 [*TEXT_ARMS, *[a for a in arm_names if a in RETRIEVAL_ARMS]],
                                 out_dir),
        }
        (out_dir / "receipt.json").write_text(json.dumps(receipt, indent=2, default=str),
                                              encoding="utf-8")
        res = receipt["results"]
        print(f"\nneedles={res['needles']} qualifier={res['qualifier_needles']}")
        for arm, a in res["arms"].items():
            hit = a["hit"] or {}
            extra = ""
            if "recovers_today_misses" in a and a["recovers_today_misses"]:
                extra = (f" today∪={a['today_union']['rate']:.3f} "
                         f"recovers_today_misses={a['recovers_today_misses']['rate']:.3f}")
            hedge = a["hedge_kept_on_qualifier_needles"]
            print(f"  {arm:8s} hit={hit.get('rate')} (n={hit.get('n')}) "
                  f"macro={a['hit_macro_over_events']} tok~{a['median_tokens']}"
                  f" hedge={hedge['rate'] if hedge else None}{extra}")
        for k in ("paired_cymatix_vs_bm25", "paired_cymatix_vs_dense"):
            if k in res:
                print(f"  {k}: {res[k]}")
        print(f"\nwrote {out_dir / 'receipt.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""`cymatix compare` — run the same queries against two lanes, write a receipt.

The 1:1 check for engine changes: serve the candidate build on a staging
lane (``genome_source = "snapshot:stable"`` so both lanes read the same
store), then

    cymatix compare --lanes stable,staging --queries needles.txt --out receipt.json

Each query goes to both lanes' ``/context/packet`` with
``ignore_delivered`` (session elision off). The receipt records, per
query, the delivered gene ids of each lane and their difference, and,
per lane, ``/admin/config-dump`` — the config actually loaded, the engine
commit and the store identity — so the numbers carry their own evidence.
Queries are matched across lanes by their text, never by position or name.

Retrieval tie-breaks can depend on Python's hash seed; start both lanes
with the same ``PYTHONHASHSEED`` before reading a small diff as a change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from . import output
from .dispatcher import invoked_prog

_TIMEOUT_S = 60.0


def load_queries(path: Path) -> List[str]:
    """Query texts from .txt (one per line, # comments), .jsonl or .json
    (strings or objects with "question" / "query")."""
    text = path.read_text(encoding="utf-8")

    def _q(item) -> Optional[str]:
        if isinstance(item, str):
            return item.strip() or None
        if isinstance(item, dict):
            value = item.get("question") or item.get("query")
            return str(value).strip() if value else None
        return None

    suffix = path.suffix.lower()
    if suffix == ".json":
        items = json.loads(text)
    elif suffix == ".jsonl":
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        items = [line for line in text.splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
    return [q for q in (_q(i) for i in items) if q]


def _headers(token: Optional[str], has_body: bool) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"} if has_body else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _post_json(url: str, body: dict, token: Optional[str] = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=_headers(token, True), method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, token: Optional[str] = None) -> dict:
    req = urllib.request.Request(url, headers=_headers(token, False), method="GET")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _delivered(packet: dict) -> List[str]:
    ids: List[str] = []
    for bucket in ("verified", "stale_risk"):
        for item in packet.get(bucket) or []:
            gid = item.get("gene_id")
            if gid and gid not in ids:
                ids.append(gid)
    return ids


def _know(packet: dict) -> Dict[str, object]:
    know = packet.get("know")
    if isinstance(know, dict):
        return {"found": True, "confidence": know.get("confidence")}
    miss = packet.get("miss")
    return {"found": False, "reason": miss.get("reason") if isinstance(miss, dict) else None}


def _diff(query: str, a: List[str], b: List[str], know_a: dict, know_b: dict) -> dict:
    union = set(a) | set(b)
    return {
        "query": query,
        "a": a,
        "b": b,
        "identical": a == b,
        "same_set": set(a) == set(b),
        "top1_same": (a[:1] == b[:1]),
        "jaccard": (len(set(a) & set(b)) / len(union)) if union else 1.0,
        "only_a": [g for g in a if g not in b],
        "only_b": [g for g in b if g not in a],
        "know_a": know_a,
        "know_b": know_b,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{invoked_prog()} compare",
        description="Run the same queries against two lanes and write a diff receipt.",
    )
    p.add_argument("--lanes", required=True,
                   help="Two lane names, comma-separated (see `cymatix mcp lanes`).")
    p.add_argument("--queries", required=True, type=Path,
                   help="Query file: .txt (one per line), .jsonl or .json.")
    p.add_argument("--max-docs", type=int, default=8,
                   help="max_genes per packet (default 8, the /context/packet default).")
    p.add_argument("--out", type=Path, default=None,
                   help="Receipt path (default: compare-<a>-vs-<b>-<timestamp>.json).")
    p.add_argument("--token", default=os.environ.get("CYMATIX_ADMIN_TOKEN"),
                   help="Admin token for /admin/config-dump (default: $CYMATIX_ADMIN_TOKEN).")
    return p


def run(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    names = [n.strip() for n in args.lanes.split(",") if n.strip()]
    if len(names) != 2 or names[0] == names[1]:
        output.eprint("--lanes takes exactly two different lane names, e.g. stable,staging")
        return output.EXIT_BAD_ARGS

    from ..config import load_config
    from ..lanes import resolve_lanes
    lanes = {lane.name: lane for lane in resolve_lanes(load_config())}
    unknown = [n for n in names if n not in lanes]
    if unknown:
        output.eprint(f"unknown lane(s): {', '.join(unknown)}; known: {', '.join(lanes)}")
        return output.EXIT_BAD_ARGS

    try:
        queries = load_queries(args.queries)
    except (OSError, ValueError) as exc:
        output.eprint(f"cannot read queries from {args.queries}: {exc}")
        return output.EXIT_ERROR
    if not queries:
        output.eprint(f"no queries in {args.queries}")
        return output.EXIT_BAD_ARGS

    urls = {n: f"http://127.0.0.1:{lanes[n].port}" for n in names}
    lane_info = []
    for n in names:
        try:
            dump = _get_json(urls[n] + "/admin/config-dump", args.token)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            output.eprint(f"lane {n} ({urls[n]}) did not answer /admin/config-dump: {exc}")
            return output.EXIT_ERROR
        lane_info.append({"name": n, "url": urls[n], "config_dump": dump})

    results = []
    for q in queries:
        packets = []
        for n in names:
            try:
                packets.append(_post_json(urls[n] + "/context/packet", {
                    "query": q, "max_genes": args.max_docs, "ignore_delivered": True,
                }, args.token))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                output.eprint(f"lane {n} failed on query {q!r}: {exc}")
                return output.EXIT_ERROR
        results.append(_diff(q, _delivered(packets[0]), _delivered(packets[1]),
                             _know(packets[0]), _know(packets[1])))

    n = len(results)
    receipt = {
        "kind": "cymatix-compare",
        "created_at": time.time(),
        "lanes": lane_info,
        "queries_file": str(args.queries.resolve()),
        "queries_sha256": hashlib.sha256(
            "\n".join(queries).encode("utf-8")).hexdigest(),
        "max_docs": args.max_docs,
        "summary": {
            "n": n,
            "identical": sum(r["identical"] for r in results),
            "same_set": sum(r["same_set"] for r in results),
            "top1_same": sum(r["top1_same"] for r in results),
            "know_agree": sum(r["know_a"]["found"] == r["know_b"]["found"] for r in results),
            "mean_jaccard": sum(r["jaccard"] for r in results) / n,
        },
        "results": results,
    }
    out = args.out or Path(
        f"compare-{names[0]}-vs-{names[1]}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    s = receipt["summary"]
    print(f"{names[0]} vs {names[1]}: {n} queries, {s['identical']} identical, "
          f"{s['same_set']} same set, {s['top1_same']} same top-1, "
          f"mean jaccard {s['mean_jaccard']:.3f}, know agreement {s['know_agree']}/{n}")
    print(f"receipt: {out}")
    return output.EXIT_OK

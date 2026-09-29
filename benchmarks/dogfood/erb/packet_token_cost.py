"""Tokenizer-counted per-packet cost of saved ERB packet captures (read-only).

Each ``--arm NAME=DIR`` points at a directory of ``qst_*.json`` captures that
carry the delivered ``context`` string (the ``<expressed_context>`` block the
answer model saw) and its ``decoder`` prompt. Counts use tiktoken's
``o200k_base`` and ``cl100k_base`` encodings; the ``chars`` field is the
string length. Nothing is retrieved or re-rendered.

    python benchmarks/dogfood/erb/packet_token_cost.py \
        --arm default_compressed_12=<dir> --arm fulltext_12=<dir> --out receipt.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
from pathlib import Path

import tiktoken

ENCODINGS = ("o200k_base", "cl100k_base")


def percentile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def summarize(values):
    return {"mean": round(statistics.fmean(values), 1), "median": statistics.median(values),
            "p90": percentile(values, 0.9), "min": min(values), "max": max(values),
            "total": sum(values)}


def measure(directory: Path, encoders) -> dict:
    files = sorted(directory.glob("qst_*.json"))
    if not files:
        raise ValueError(f"No qst_*.json captures in {directory}")
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    listing = hashlib.sha256()
    for path in files:
        listing.update(path.name.encode("utf-8") + hashlib.sha256(path.read_bytes()).digest())
    result = {"dir": str(directory), "n": len(rows), "files_sha256": listing.hexdigest(),
              "chars": summarize([len(row["context"]) for row in rows]),
              "delivered_ids": summarize([len(row["delivered_ids"]) for row in rows])}
    if any("added_ids" in row for row in rows):
        result["added_ids"] = summarize([len(row.get("added_ids", [])) for row in rows])
    for name, encoder in encoders.items():
        result["tokens_" + name] = summarize(
            [len(encoder.encode(row["context"], disallowed_special=())) for row in rows])
        result["decoder_tokens_" + name] = summarize(
            [len(encoder.encode(row.get("decoder", ""), disallowed_special=())) for row in rows])
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--arm", action="append", required=True, metavar="NAME=DIR")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.out.exists():
        raise SystemExit(f"Refusing to overwrite {args.out}")
    encoders = {name: tiktoken.get_encoding(name) for name in ENCODINGS}
    arms = {}
    for spec in args.arm:
        name, _, directory = spec.partition("=")
        arms[name] = measure(Path(directory), encoders)
    code_sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=30, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    receipt = {"tool": "benchmarks/dogfood/erb/packet_token_cost.py", "code_sha": code_sha,
               "python": sys.version.split()[0], "tiktoken": tiktoken.__version__,
               "encodings": list(ENCODINGS), "arms": arms}
    args.out.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    for name, arm in arms.items():
        tokens = arm["tokens_o200k_base"]
        print(f"{name}: n={arm['n']} o200k mean={tokens['mean']} median={tokens['median']} p90={tokens['p90']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Merge CSV files, resolving their schemas and performing an external sort."""

import argparse
import csv
import datetime as dt
import heapq
import json
import math
import os
import pickle
import sys
import tempfile
from pathlib import Path


TYPES = {"string", "int", "float", "bool", "date", "timestamp"}
TYPE_PRIORITY = {"timestamp": 0, "date": 1, "bool": 2, "int": 3, "float": 4, "string": 5}


def reader_for(path, quotechar, escapechar):
    f = open(path, "r", encoding="utf-8", newline="")
    return f, csv.reader(f, delimiter=",", quotechar=quotechar,
                         doublequote=escapechar is None, escapechar=escapechar)


def parse_value(text, typ):
    if text == "":
        return None
    if typ == "string":
        return text
    if typ == "int":
        # bool-like and decimal values are deliberately not ints.
        if text.strip() != text or not text or text[0] in "+-" and len(text) == 1:
            raise ValueError("invalid integer")
        return int(text, 10)
    if typ == "float":
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("non-finite float")
        return value
    if typ == "bool":
        v = text.strip().lower()
        if v in {"1", "true", "t", "yes", "y"}: return True
        if v in {"0", "false", "f", "no", "n"}: return False
        raise ValueError("invalid boolean")
    if typ == "date":
        value = dt.date.fromisoformat(text)
        if value.isoformat() != text:
            raise ValueError("date is not YYYY-MM-DD")
        return value
    if typ == "timestamp":
        # Keep date-only ISO values in the date type during inference.
        if "T" not in text and "t" not in text and " " not in text:
            raise ValueError("timestamp requires a time component")
        raw = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        value = dt.datetime.fromisoformat(raw)
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)
    raise ValueError("unknown type")


def classify(text):
    if text == "": return None
    # Timestamp before date; bool before numeric; priority then controls overlaps.
    candidates = []
    for typ in ("timestamp", "date", "bool", "int", "float"):
        try: parse_value(text, typ); candidates.append(typ)
        except (ValueError, OverflowError): pass
    return min(candidates, key=lambda x: TYPE_PRIORITY[x]) if candidates else "string"


def candidates(text):
    result = []
    for typ in ("timestamp", "date", "bool", "int", "float"):
        try: parse_value(text, typ); result.append(typ)
        except (ValueError, OverflowError): pass
    return set(result) or {"string"}


def canonical(value, typ):
    if value is None: return ""
    if typ == "string": return value
    if typ == "int": return str(value)
    if typ == "float": return repr(value)
    if typ == "bool": return "true" if value else "false"
    if typ == "date": return value.isoformat()
    if typ == "timestamp": return value.isoformat(timespec="microseconds").replace("+00:00", "Z").replace(".000000Z", "Z")


def load_schema(path):
    with open(path, encoding="utf-8") as f: data = json.load(f)
    cols = data.get("columns")
    if not isinstance(cols, list) or not cols: raise ValueError("schema columns must be a non-empty array")
    result = []
    seen = set()
    for col in cols:
        if not isinstance(col, dict) or not isinstance(col.get("name"), str) or col.get("type") not in TYPES:
            raise ValueError("each schema column needs a name and valid type")
        if col["name"] in seen: raise ValueError("duplicate schema column: " + col["name"])
        seen.add(col["name"]); result.append((col["name"], col["type"]))
    return result


def discover(inputs, quotechar, escapechar, mode):
    names = set(); evidence = {}; loose_possible = {}
    for path in inputs:
        f, rd = reader_for(path, quotechar, escapechar)
        try:
            header = next(rd)
            if len(set(header)) != len(header): raise ValueError(f"duplicate header in {path}")
            names.update(header)
            for row in rd:
                if len(row) != len(header): raise ValueError(f"row has wrong number of fields in {path}")
                for name, value in zip(header, row):
                    if value != "":
                        evidence.setdefault(name, set()).add(classify(value))
                        if mode == "loose":
                            possible = candidates(value)
                            loose_possible[name] = (loose_possible.get(name, set(TYPES)) & possible)
        finally: f.close()
    schema = []
    for name in sorted(names):
        types = evidence.get(name, set())
        # Strict requires one type everywhere; loose chooses the most specific type
        # that parses every observed value (the evidence sets are sufficient).
        if mode == "loose" and loose_possible.get(name):
            typ = min(loose_possible[name], key=lambda x: TYPE_PRIORITY[x])
        elif len(types) == 1: typ = next(iter(types))
        elif not types: typ = "string"
        else: typ = "string"
        schema.append((name, typ))
    return schema


def sortable(value, typ, descending):
    null = value is None
    nullrank = 1 if (null and descending) or (not null and not descending) else 0
    if null: return (nullrank, 0)
    if typ in ("int", "float"): v = -value if descending else value
    elif typ == "bool": v = -(1 if value else 0) if descending else (1 if value else 0)
    elif typ == "date": v = -value.toordinal() if descending else value.toordinal()
    elif typ == "timestamp":
        epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        micros = int((value - epoch).total_seconds() * 1000000)
        v = -micros if descending else micros
    else:
        b = value.encode("utf-8")
        v = bytes(255 - x for x in b) if descending else b
    return (nullrank, v)


def run(args):
    if not args.inputs: raise ValueError("at least one input is required")
    if len(args.csv_quotechar) != 1: raise ValueError("quotechar must be one character")
    if args.csv_escapechar is not None and len(args.csv_escapechar) != 1: raise ValueError("escapechar must be one character")
    schema = load_schema(args.schema) if args.schema else discover(args.inputs, args.csv_quotechar, args.csv_escapechar, args.infer)
    by_name = dict(schema)
    keys = args.key.split(",") if args.key else []
    if not keys or any(k not in by_name for k in keys): raise ValueError("every key must be present in the resolved schema")
    key_types = [by_name[k] for k in keys]
    limit = max(1, args.memory_limit_mb or 64) * 1024 * 1024
    chunk_limit = max(1024 * 1024, limit // 2)

    with tempfile.TemporaryDirectory(dir=args.temp_dir) as td:
        runs = []; chunk = []; chunk_bytes = 0; sequence = 0
        def flush():
            nonlocal chunk, chunk_bytes
            if not chunk: return
            chunk.sort(key=lambda x: (x[0], x[1]))
            name = os.path.join(td, f"run-{len(runs):08d}.bin")
            with open(name, "wb") as out:
                for entry in chunk: pickle.dump(entry, out, protocol=pickle.HIGHEST_PROTOCOL)
            runs.append(name); chunk = []; chunk_bytes = 0

        for path in args.inputs:
            f, rd = reader_for(path, args.csv_quotechar, args.csv_escapechar)
            try:
                header = next(rd)
                if len(set(header)) != len(header): raise ValueError(f"duplicate header in {path}")
                positions = {n: i for i, n in enumerate(header)}
                for raw in rd:
                    if len(raw) != len(header): raise ValueError(f"row has wrong number of fields in {path}")
                    rendered = []; parsed = {}
                    for name, typ in schema:
                        original = raw[positions[name]] if name in positions else ""
                        failed = False
                        try: value = parse_value(original, typ)
                        except (ValueError, OverflowError) as exc:
                            if args.on_type_error == "fail": raise ValueError(f"{path}: column {name}: {exc}")
                            value = None if args.on_type_error == "coerce-null" else original
                            failed = True
                        # keep-string remains a string in output, but sorts as text for this cell.
                        actual_type = "string" if failed else typ
                        rendered.append(args.csv_null_literal if value is None else canonical(value, actual_type)); parsed[name] = value
                    sk = tuple(sortable(parsed[k], t if not (args.on_type_error == "keep-string" and isinstance(parsed[k], str)) else "string", args.desc) for k, t in zip(keys, key_types))
                    entry = (sk, sequence, rendered); sequence += 1
                    chunk.append(entry); chunk_bytes += len(pickle.dumps(entry, protocol=pickle.HIGHEST_PROTOCOL))
                    if chunk_bytes >= chunk_limit: flush()
            finally: f.close()
        flush()

        def iterator(name):
            with open(name, "rb") as f:
                while True:
                    try: yield pickle.load(f)
                    except EOFError: return
        streams = [iterator(n) for n in runs]
        heap = []
        for i, stream in enumerate(streams):
            try:
                item = next(stream); heapq.heappush(heap, (item[0], item[1], i, item[2]))
            except StopIteration: pass
        out = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8", newline="")
        try:
            writer = csv.writer(out, delimiter=",", quotechar='"', doublequote=True, lineterminator="\n")
            writer.writerow([n for n, _ in schema])
            while heap:
                _, _, i, row = heapq.heappop(heap); writer.writerow(row)
                try:
                    item = next(streams[i]); heapq.heappush(heap, (item[0], item[1], i, item[2]))
                except StopIteration: pass
        finally:
            if out is not sys.stdout: out.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True); p.add_argument("--key", required=True)
    p.add_argument("--desc", action="store_true"); p.add_argument("--schema")
    p.add_argument("--infer", choices=["strict", "loose"], default="strict")
    p.add_argument("--on-type-error", choices=["coerce-null", "fail", "keep-string"], default="coerce-null")
    p.add_argument("--memory-limit-mb", type=int); p.add_argument("--temp-dir")
    p.add_argument("--csv-quotechar", default='"'); p.add_argument("--csv-escapechar")
    p.add_argument("--csv-null-literal", default=""); p.add_argument("inputs", nargs="+")
    args = p.parse_args()
    try: run(args)
    except (OSError, csv.Error, json.JSONDecodeError, ValueError) as e:
        print(f"merge_files.py: {e}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__": sys.exit(main())

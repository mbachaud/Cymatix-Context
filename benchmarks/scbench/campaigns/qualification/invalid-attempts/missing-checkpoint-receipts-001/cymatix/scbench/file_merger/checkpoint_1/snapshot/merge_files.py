#!/usr/bin/env python3
"""Merge CSV files, resolve/cast their schemas, and externally sort the rows."""
import argparse
import csv
import datetime as dt
import functools
import heapq
import json
import os
import pickle
import shutil
import sys
import tempfile
from pathlib import Path

TYPES = {"string", "int", "float", "bool", "date", "timestamp"}
TYPE_ORDER = {"string": 0, "float": 1, "int": 2, "bool": 3, "date": 4, "timestamp": 5}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--desc", action="store_true")
    p.add_argument("--schema")
    p.add_argument("--infer", choices=("strict", "loose"), default="strict")
    p.add_argument("--on-type-error", choices=("coerce-null", "fail", "keep-string"), default="coerce-null")
    p.add_argument("--memory-limit-mb", type=int, default=128)
    p.add_argument("--temp-dir")
    p.add_argument("--csv-quotechar", default='"')
    p.add_argument("--csv-escapechar")
    p.add_argument("--csv-null-literal", default="")
    p.add_argument("inputs", nargs="+")
    a = p.parse_args()
    if a.memory_limit_mb <= 0 or len(a.csv_quotechar) != 1 or (a.csv_escapechar is not None and len(a.csv_escapechar) != 1):
        p.error("memory limit must be positive and CSV quote/escape characters must be one character")
    return a


def reader(path, args):
    f = open(path, "r", encoding="utf-8", newline="")
    return f, csv.reader(f, delimiter=",", quotechar=args.csv_quotechar,
                          escapechar=args.csv_escapechar, strict=True)


def is_null(s, args):
    return s == "" or (args.csv_null_literal != "" and s == args.csv_null_literal)


def parse_value(s, typ):
    if typ == "string": return s
    if typ == "int":
        # Reject values such as 1.0 and whitespace-surrounded values.
        if not s or s.strip() != s or (s[0] in "+-" and not s[1:].isdigit()) or (s[0] not in "+-" and not s.isdigit()):
            raise ValueError("invalid integer")
        return int(s)
    if typ == "float":
        x = float(s)
        if x != x or x in (float("inf"), float("-inf")): raise ValueError("non-finite float")
        return x
    if typ == "bool":
        v = s.strip().lower()
        if v in ("true", "t", "yes", "y", "1"): return True
        if v in ("false", "f", "no", "n", "0"): return False
        raise ValueError("invalid boolean")
    if typ == "date":
        return dt.date.fromisoformat(s)
    if typ == "timestamp":
        v = s.strip()
        if v.endswith("Z"): v = v[:-1] + "+00:00"
        x = dt.datetime.fromisoformat(v)
        if x.tzinfo is None: x = x.replace(tzinfo=dt.timezone.utc)
        return x.astimezone(dt.timezone.utc)
    raise ValueError("unknown type")


def classify(s):
    if s == "": return None
    # A date-only ISO value is a date, not a timestamp.
    candidates = ("timestamp", "date", "bool", "int", "float")
    if "T" not in s and "t" not in s and " " not in s:
        candidates = ("date", "bool", "int", "float")
    for typ in candidates:
        try: parse_value(s, typ); return typ
        except (ValueError, OverflowError): pass
    return "string"


def infer_schema(inputs, args):
    names = set()
    per_file = {}
    all_types = {}
    for path in inputs:
        f, r = reader(path, args)
        try:
            header = next(r)
            names.update(header)
            seen = {n: set() for n in header}
            for row in r:
                for i, value in enumerate(row[:len(header)]):
                    if not is_null(value, args): seen[header[i]].add(classify(value))
            per_file[path] = seen
            for n, ts in seen.items(): all_types.setdefault(n, set()).update(ts)
        finally: f.close()
    schema = []
    for n in sorted(names):
        if args.infer == "strict":
            file_types = [next(iter(x[n])) if len(x[n]) == 1 else "string"
                          for x in per_file.values() if x.get(n)]
            typ = file_types[0] if file_types and all(x == file_types[0] for x in file_types) else "string"
        else:
            ts = all_types.get(n, set())
            if not ts: typ = "string"
            elif "string" in ts: typ = "string"
            elif all(t in ("int",) for t in ts): typ = "int"
            elif all(t in ("int", "float") for t in ts): typ = "float"
            elif len(ts) == 1: typ = next(iter(ts))
            else: typ = "string"
        schema.append((n, typ))
    return schema


def load_schema(path):
    with open(path, encoding="utf-8") as f: obj = json.load(f)
    cols = obj.get("columns") if isinstance(obj, dict) else None
    if not isinstance(cols, list) or not cols: raise ValueError("schema must contain a non-empty columns list")
    result = []
    names = set()
    for c in cols:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or c.get("type") not in TYPES:
            raise ValueError("each schema column needs a valid name and type")
        if c["name"] in names: raise ValueError("duplicate schema column: " + c["name"])
        names.add(c["name"]); result.append((c["name"], c["type"]))
    return result


def output_value(value, typ):
    if typ == "string": return value
    if typ == "bool": return "true" if value else "false"
    if typ == "date": return value.isoformat()
    if typ == "timestamp": return value.strftime("%Y-%m-%dT%H:%M:%S") + (".%06d" % value.microsecond if value.microsecond else "") + "Z"
    return str(value)


def cast(s, typ, args, col, seq):
    if is_null(s, args): return None, args.csv_null_literal
    try: v = parse_value(s, typ); return v, output_value(v, typ)
    except (ValueError, OverflowError, TypeError) as e:
        if args.on_type_error == "fail": raise ValueError(f"row {seq}, column {col}: {e}: {s!r}")
        if args.on_type_error == "keep-string": return s, s
        return None, args.csv_null_literal


def compare_rows(a, b, desc):
    # a/b are (typed key tuple, appearance sequence, output cells)
    for x, y in zip(a[0], b[0]):
        xn, xv = x is None, x
        yn, yv = y is None, y
        if xn != yn:
            # Nulls are lower in ascending order, but explicitly last in descending.
            return (1 if desc else -1) if xn else (-1 if desc else 1)
        if xn: continue
        if xv < yv: return 1 if desc else -1
        if xv > yv: return -1 if desc else 1
    return (a[1] > b[1]) - (a[1] < b[1])


class HeapItem:
    def __init__(self, row, run, fh): self.row, self.run, self.fh = row, run, fh
    def __lt__(self, other): return compare_rows(self.row, other.row, self._desc) < 0


def sortable_value(value, typ):
    """Keep malformed keep-string values comparable with successfully cast values."""
    if value is None: return None
    if typ == "string" or isinstance(value, (str, int, float, bool, dt.date, dt.datetime)):
        # A string is valid for a string target; for other targets it is the
        # marker used by keep-string and sorts after successfully cast values.
        if isinstance(value, str) and typ != "string": return (1, value)
        return (0, value)
    return (0, value)


def main():
    args = parse_args()
    keys = [x for x in args.key.split(",") if x]
    if not keys: raise ValueError("--key must contain at least one column")
    schema = load_schema(args.schema) if args.schema else infer_schema(args.inputs, args)
    positions = {n: i for i, (n, _) in enumerate(schema)}
    missing = [k for k in keys if k not in positions]
    if missing: raise ValueError("key column not in resolved schema: " + ", ".join(missing))
    types = [t for _, t in schema]
    limit = max(1024 * 1024, args.memory_limit_mb * 1024 * 1024 // 3)
    temp_root = tempfile.mkdtemp(prefix="csv-merge-", dir=args.temp_dir)
    runs = []; seq = 0; chunk = []; chunk_bytes = 0
    try:
        for path in args.inputs:
            f, r = reader(path, args)
            try:
                header = next(r); index = {n: i for i, n in enumerate(header)}
                for row in r:
                    typed = [None] * len(schema); cells = [args.csv_null_literal] * len(schema)
                    for i, (name, typ) in enumerate(schema):
                        raw = row[index[name]] if name in index and index[name] < len(row) else ""
                        typed[i], cells[i] = cast(raw, typ, args, name, seq)
                    keyvals = tuple(sortable_value(typed[positions[k]], types[positions[k]]) for k in keys)
                    item = (keyvals, seq, cells); chunk.append(item)
                    chunk_bytes += sum(len(x) for x in cells) + 64; seq += 1
                    if chunk_bytes >= limit:
                        chunk.sort(key=functools.cmp_to_key(lambda a,b: compare_rows(a,b,args.desc)))
                        rp = os.path.join(temp_root, f"run-{len(runs)}.bin")
                        with open(rp, "wb") as out:
                            for x in chunk: pickle.dump(x, out, pickle.HIGHEST_PROTOCOL)
                        runs.append(rp); chunk.clear(); chunk_bytes = 0
            finally: f.close()
        if chunk:
            chunk.sort(key=functools.cmp_to_key(lambda a,b: compare_rows(a,b,args.desc)))
            rp = os.path.join(temp_root, f"run-{len(runs)}.bin")
            with open(rp, "wb") as out:
                for x in chunk: pickle.dump(x, out, pickle.HIGHEST_PROTOCOL)
            runs.append(rp)
        out = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8", newline="")
        try:
            w = csv.writer(out, delimiter=",", quotechar='"', lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
            w.writerow([n for n, _ in schema])
            if runs:
                fhs = [open(p, "rb") for p in runs]; heap = []
                HeapItem._desc = args.desc
                for i, fh in enumerate(fhs):
                    try: heapq.heappush(heap, HeapItem(pickle.load(fh), i, fh))
                    except EOFError: pass
                while heap:
                    h = heapq.heappop(heap); w.writerow(h.row[2])
                    try: h.row = pickle.load(h.fh); heapq.heappush(heap, h)
                    except EOFError: pass
                for fh in fhs: fh.close()
        finally:
            if out is not sys.stdout: out.close()
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try: sys.exit(main())
    except Exception as e:
        print(f"merge_files.py: {e}", file=sys.stderr); sys.exit(1)

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
import re
import sys
import tempfile

TYPES = ("string", "float", "int", "bool", "date", "timestamp")
PRIORITY = {"string": 0, "float": 1, "int": 2, "bool": 3, "date": 4, "timestamp": 5}
INT_RE = re.compile(r"^[+-]?\d+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_timestamp(s):
    x = s.strip()
    if x.endswith(("Z", "z")):
        x = x[:-1] + "+00:00"
    value = dt.datetime.fromisoformat(x)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def can_parse(s, typ):
    try:
        if typ == "string": return True
        if typ == "int": int(s.strip()); return bool(INT_RE.match(s.strip()))
        if typ == "float": float(s.strip()); return True
        if typ == "bool": return s.strip().lower() in {"true", "false", "yes", "no", "y", "n", "t", "f", "1", "0"}
        if typ == "date": dt.date.fromisoformat(s.strip()); return bool(DATE_RE.match(s.strip()))
        if typ == "timestamp": parse_timestamp(s); return not bool(DATE_RE.match(s.strip()))
    except (ValueError, OverflowError):
        return False
    return False


def possible_types(s):
    # Keep 1/0 as integers during inference; they remain valid boolean casts.
    result = {"float"} if can_parse(s, "float") else set()
    if can_parse(s, "int"): result.add("int")
    if can_parse(s, "bool") and s.strip().lower() not in {"1", "0"}:
        result.add("bool")
    if can_parse(s, "date"): result.add("date")
    if can_parse(s, "timestamp"): result.add("timestamp")
    return result or {"string"}


def infer_schema(paths, mode, quotechar, escapechar):
    names = set()
    stats = {}                 # column -> list of per-file candidate intersections
    for path in paths:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f, delimiter=",", quotechar=quotechar,
                                escapechar=escapechar, doublequote=True)
            try: header = next(reader)
            except StopIteration: header = []
            names.update(header)
            positions = {n: i for i, n in enumerate(header)}
            local = {n: None for n in header}
            for row in reader:
                for n, i in positions.items():
                    if i < len(row) and row[i] != "":
                        p = possible_types(row[i])
                        local[n] = p if local[n] is None else local[n] & p
            for n in header:
                stats.setdefault(n, []).append(local[n])
    result = []
    for n in sorted(names):
        observations = [x for x in stats.get(n, []) if x]
        if not observations:
            typ = "string"
        elif mode == "strict":
            choices = [max(x, key=lambda z: PRIORITY[z]) if x else "string" for x in observations]
            typ = choices[0] if all(x == choices[0] for x in choices) else "string"
        else:
            common = set.intersection(*observations)
            typ = max(common, key=lambda z: PRIORITY[z]) if common else "string"
        result.append((n, typ))
    return result


def cast(value, typ, null_literal, policy, path, row_number, column):
    if value == "": return None
    try:
        if typ == "string": return value
        if typ == "int":
            if not INT_RE.match(value.strip()): raise ValueError()
            return str(int(value.strip()))
        if typ == "float":
            number = float(value.strip())
            if number != number or number in (float("inf"), float("-inf")): raise ValueError()
            return str(number)
        if typ == "bool":
            low = value.strip().lower()
            if low in {"true", "yes", "y", "t", "1"}: return "true"
            if low in {"false", "no", "n", "f", "0"}: return "false"
            raise ValueError()
        if typ == "date":
            return dt.date.fromisoformat(value.strip()).isoformat()
        if typ == "timestamp":
            x = parse_timestamp(value)
            if x.microsecond:
                return x.isoformat(timespec="microseconds").replace("+00:00", "Z")
            return x.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError):
        if policy == "fail":
            raise ValueError(f"{path}:{row_number}: cannot cast column {column!r} value {value!r} to {typ}")
        if policy == "keep-string": return value
        return None
    return value


def sort_atom(value, typ):
    if value is None: return None
    try:
        if typ == "int": return int(value)
        if typ == "float": return float(value)
        if typ == "bool": return value == "true"
        if typ == "date": return dt.date.fromisoformat(value)
        if typ == "timestamp": return parse_timestamp(value)
    except (ValueError, OverflowError):
        pass
    return value


def compare_values(a, b):
    if a is None: return 0 if b is None else -1
    if b is None: return 1
    try:
        return (a > b) - (a < b)
    except TypeError:
        return (str(a) > str(b)) - (str(a) < str(b))


class SortItem:
    __slots__ = ("key", "seq", "values", "desc")
    def __init__(self, key, seq, values, desc): self.key, self.seq, self.values, self.desc = key, seq, values, desc
    def __lt__(self, other):
        for a, b in zip(self.key, other.key):
            c = compare_values(a, b)
            if c:
                # Null ordering is deliberately not reversed.
                if a is None or b is None: return c < 0
                return c > 0 if self.desc else c < 0
        return self.seq < other.seq


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--desc", action="store_true")
    ap.add_argument("--schema")
    ap.add_argument("--infer", choices=("strict", "loose"), default="strict")
    ap.add_argument("--on-type-error", choices=("coerce-null", "fail", "keep-string"), default="coerce-null")
    ap.add_argument("--memory-limit-mb", type=int, default=128)
    ap.add_argument("--temp-dir")
    ap.add_argument("--csv-quotechar", default='"')
    ap.add_argument("--csv-escapechar")
    ap.add_argument("--csv-null-literal", default="")
    ap.add_argument("inputs", nargs="+")
    args = ap.parse_args(argv)
    if len(args.csv_quotechar) != 1 or (args.csv_escapechar is not None and len(args.csv_escapechar) != 1):
        ap.error("CSV quote and escape characters must each be one character")
    if args.memory_limit_mb <= 0: ap.error("--memory-limit-mb must be positive")
    keys = [x for x in args.key.split(",") if x]
    if not keys: ap.error("--key requires at least one column")
    if args.schema:
        with open(args.schema, encoding="utf-8") as f: spec = json.load(f)
        schema = [(x["name"], x["type"]) for x in spec["columns"]]
        if any(t not in TYPES for _, t in schema) or len({n for n, _ in schema}) != len(schema):
            raise ValueError("invalid schema types or duplicate column names")
    else:
        schema = infer_schema(args.inputs, args.infer, args.csv_quotechar, args.csv_escapechar)
    col_index = {n: i for i, (n, _) in enumerate(schema)}
    missing_keys = [k for k in keys if k not in col_index]
    if missing_keys: raise ValueError("key column(s) not present in resolved schema: " + ", ".join(missing_keys))

    # Leave ample room for Python object overhead, CSV buffers, and the merge heap.
    chunk_limit = max(1024, args.memory_limit_mb * 1024 * 1024 // 16)
    temp = tempfile.TemporaryDirectory(dir=args.temp_dir)
    chunks, pending, pending_size, seq = [], [], 0, 0
    types = dict(schema)
    try:
        def spill():
            nonlocal pending, pending_size
            pending.sort(key=functools.cmp_to_key(lambda a, b: -1 if SortItem(a[0], a[1], a[2], args.desc) < SortItem(b[0], b[1], b[2], args.desc) else (1 if SortItem(b[0], b[1], b[2], args.desc) < SortItem(a[0], a[1], a[2], args.desc) else 0)))
            name = os.path.join(temp.name, f"chunk-{len(chunks)}.bin")
            with open(name, "wb") as f:
                p = pickle.Pickler(f, protocol=pickle.HIGHEST_PROTOCOL)
                for item in pending: p.dump(item)
            chunks.append(name); pending, pending_size = [], 0

        for path in args.inputs:
            with open(path, "r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f, delimiter=",", quotechar=args.csv_quotechar,
                                    escapechar=args.csv_escapechar, doublequote=True)
                try: header = next(reader)
                except StopIteration: continue
                positions = {n: i for i, n in enumerate(header)}
                for rn, row in enumerate(reader, 2):
                    values = []
                    for n, typ in schema:
                        raw = row[positions[n]] if n in positions and positions[n] < len(row) else ""
                        values.append(cast(raw, typ, args.csv_null_literal, args.on_type_error, path, rn, n))
                    key = tuple(sort_atom(values[col_index[k]], types[k]) for k in keys)
                    pending.append((key, seq, values)); seq += 1
                    pending_size += sum(len(x) if isinstance(x, str) else 8 for x in values) + 80
                    if pending_size >= chunk_limit: spill()
        if pending: spill()

        streams = [open(x, "rb") for x in chunks]
        loaders = [pickle.Unpickler(f) for f in streams]
        heap = []
        for i, loader in enumerate(loaders):
            try:
                key, number, values = loader.load()
                heapq.heappush(heap, SortItem(key, number, (i, values), args.desc))
            except EOFError: pass
        out = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8", newline="")
        try:
            writer = csv.writer(out, delimiter=",", quotechar='"', escapechar=None, doublequote=True, lineterminator="\n")
            writer.writerow([n for n, _ in schema])
            while heap:
                item = heapq.heappop(heap); i, values = item.values
                writer.writerow([args.csv_null_literal if x is None else x for x in values])
                try:
                    key, number, vals = loaders[i].load()
                    heapq.heappush(heap, SortItem(key, number, (i, vals), args.desc))
                except EOFError: pass
        finally:
            if out is not sys.stdout: out.close()
            for f in streams: f.close()
    finally:
        temp.cleanup()
    return 0


if __name__ == "__main__":
    try: sys.exit(main())
    except Exception as exc:
        print(f"merge_files.py: {exc}", file=sys.stderr)
        sys.exit(1)

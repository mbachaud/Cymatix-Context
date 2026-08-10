#!/usr/bin/env python3
"""Merge CSV files, resolve their schemas, and externally sort their rows."""

import argparse
import csv
import datetime as dt
import functools
import heapq
import json
import os
import pickle
import sys
import tempfile
from pathlib import Path


TYPES = {"string", "int", "float", "bool", "date", "timestamp"}
PRIORITY = ["timestamp", "date", "bool", "int", "float", "string"]
BOOLS = {"true": True, "false": False, "yes": True, "no": False,
         "t": True, "f": False, "y": True, "n": False, "1": True, "0": False}


def parse_bool(value):
    v = value.strip().lower()
    if v not in BOOLS:
        raise ValueError("invalid boolean")
    return BOOLS[v]


def parse_date(value):
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError("invalid date")
    return dt.date.fromisoformat(value)


def parse_timestamp(value):
    s = value.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    # A date without a time is a date, not a timestamp for inference.
    if "T" not in s and "t" not in s and " " not in s:
        raise ValueError("timestamp requires a time")
    x = dt.datetime.fromisoformat(s)
    if x.tzinfo is None:
        x = x.replace(tzinfo=dt.timezone.utc)
    return x.astimezone(dt.timezone.utc)


def classify(value):
    """Return the most specific type represented by one non-null value."""
    for typ, fn in (("timestamp", parse_timestamp), ("date", parse_date),
                    ("bool", parse_bool)):
        try:
            fn(value)
            return typ
        except (ValueError, TypeError):
            pass
    try:
        int(value.strip())
        return "int"
    except ValueError:
        pass
    try:
        float(value.strip())
        return "float"
    except ValueError:
        return "string"


def compatible(a, b):
    if a == b:
        return a
    if {a, b} == {"int", "float"}:
        return "float"
    return "string"


def reader_for(path, quotechar, escapechar):
    f = open(path, "r", encoding="utf-8", newline="")
    return f, csv.reader(f, delimiter=",", quotechar=quotechar,
                          escapechar=escapechar, doublequote=(escapechar is None))


def inspect_inputs(paths, quotechar, escapechar, loose):
    names = set()
    per_file = []
    for path in paths:
        f, reader = reader_for(path, quotechar, escapechar)
        try:
            header = next(reader)
            names.update(header)
            states = {}
            for row in reader:
                for i, value in enumerate(row[:len(header)]):
                    if not value:
                        continue
                    name = header[i]
                    if loose:
                        possible = states.setdefault(name, set(PRIORITY))
                        states[name] = {typ for typ in possible if _can_cast(value, typ)}
                    else:
                        typ = classify(value)
                        states[name] = compatible(states.get(name, typ), typ)
            per_file.append(states)
        finally:
            f.close()
    columns = sorted(names)
    resolved = []
    for name in columns:
        if loose:
            possible = set(PRIORITY)
            for state in per_file:
                if name in state:
                    possible &= state[name]
            chosen = next((typ for typ in PRIORITY if typ in possible), "string")
        else:
            observed = [state[name] for state in per_file if name in state]
            chosen = observed[0] if observed else "string"
            for typ in observed[1:]:
                chosen = compatible(chosen, typ)
        resolved.append((name, chosen))
    return resolved


def load_schema(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    columns = data.get("columns") if isinstance(data, dict) else None
    if not isinstance(columns, list):
        raise ValueError("schema must contain a columns array")
    result = []
    seen = set()
    for item in columns:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("each schema column needs a name and type")
        name, typ = item["name"], item.get("type")
        if name in seen or typ not in TYPES:
            raise ValueError("invalid or duplicate schema column")
        seen.add(name)
        result.append((name, typ))
    return result


def cast(value, typ):
    if typ == "string":
        return value
    if typ == "int":
        return int(value.strip())
    if typ == "float":
        return float(value.strip())
    if typ == "bool":
        return parse_bool(value)
    if typ == "date":
        return parse_date(value.strip())
    if typ == "timestamp":
        return parse_timestamp(value)
    raise ValueError("unknown type")


def _can_cast(value, typ):
    try:
        cast(value, typ)
        return True
    except (ValueError, OverflowError):
        return False


def output_value(value, typ):
    if typ == "bool":
        return "true" if value else "false"
    if typ == "date":
        return value.isoformat()
    if typ == "timestamp":
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z").replace(".000000Z", "Z")
    return str(value)


def key_value(value, typ):
    if value is None:
        return None
    if typ == "string":
        return value
    if typ == "date":
        return value.toordinal()
    if typ == "timestamp":
        return value.timestamp()
    if typ == "bool":
        return int(value)
    return value


def compare_records(a, b, desc):
    ka, kb = a[0], b[0]
    for x, y in zip(ka, kb):
        if x is None or y is None:
            if x is None and y is None:
                continue
            # Null is always lower in the logical ordering; descending reverses
            # only non-null comparisons, so null is last when descending.
            result = -1 if x is None else 1
        else:
            try:
                if x < y:
                    result = -1
                elif x > y:
                    result = 1
                else:
                    continue
            except TypeError:
                # keep-string may intentionally leave a bad typed key as text.
                # Give mixed representations a deterministic, total ordering.
                left, right = (type(x).__name__, str(x)), (type(y).__name__, str(y))
                result = -1 if left < right else (1 if left > right else 0)
                if result == 0:
                    continue
        return -result if desc else result
    return (a[1] > b[1]) - (a[1] < b[1])


class HeapItem:
    def __init__(self, record, run, desc):
        self.record, self.run, self.desc = record, run, desc
    def __lt__(self, other):
        return compare_records(self.record, other.record, self.desc) < 0


def make_runs(paths, schema, index, args, temp):
    runs = []
    rows, used = [], 0
    limit = max(1, args.memory_limit_mb or 64) * 1024 * 1024 // 3
    seq = 0

    def flush():
        nonlocal rows, used
        if not rows:
            return
        rows.sort(key=functools.cmp_to_key(lambda a, b: compare_records(a, b, args.desc)))
        fd, runpath = tempfile.mkstemp(prefix="merge-", suffix=".run", dir=temp)
        with os.fdopen(fd, "wb") as out:
            for record in rows:
                pickle.dump(record, out, protocol=pickle.HIGHEST_PROTOCOL)
        runs.append(runpath)
        rows, used = [], 0

    for path in paths:
        f, reader = reader_for(path, args.csv_quotechar, args.csv_escapechar)
        try:
            header = next(reader)
            positions = {name: i for i, name in enumerate(header)}
            for line, row in enumerate(reader, 2):
                values, keys = [], []
                for name, typ in schema:
                    raw = row[positions[name]] if name in positions and positions[name] < len(row) else ""
                    if raw == "":
                        value, rendered = None, args.csv_null_literal
                    else:
                        try:
                            value = cast(raw, typ)
                            rendered = output_value(value, typ)
                        except (ValueError, OverflowError) as exc:
                            if args.on_type_error == "fail":
                                raise ValueError(f"{path}:{line}:{name}: {exc}")
                            value, rendered = (None, args.csv_null_literal) if args.on_type_error == "coerce-null" else (raw, raw)
                    values.append(rendered)
                    if name in index:
                        keys.append(key_value(value, typ) if value is not None else None)
                record = (tuple(keys), seq, values)
                rows.append(record)
                seq += 1
                used += sum(len(x) for x in values) + 128
                if used >= limit:
                    flush()
        finally:
            f.close()
    flush()
    return runs


def merge_runs(runs, output, header, desc):
    files, heap = [], []
    try:
        for n, path in enumerate(runs):
            f = open(path, "rb")
            files.append(f)
            try:
                record = pickle.load(f)
                heapq.heappush(heap, HeapItem(record, n, desc))
            except EOFError:
                pass
        writer = csv.writer(output, delimiter=",", quotechar='"', doublequote=True,
                            lineterminator="\n")
        writer.writerow(header)
        while heap:
            item = heapq.heappop(heap)
            writer.writerow(item.record[2])
            try:
                nxt = pickle.load(files[item.run])
                heapq.heappush(heap, HeapItem(nxt, item.run, desc))
            except EOFError:
                pass
    finally:
        for f in files:
            f.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--desc", action="store_true")
    p.add_argument("--schema")
    p.add_argument("--infer", choices=("strict", "loose"), default="strict")
    p.add_argument("--on-type-error", choices=("coerce-null", "fail", "keep-string"), default="coerce-null")
    p.add_argument("--memory-limit-mb", type=int)
    p.add_argument("--temp-dir")
    p.add_argument("--csv-quotechar", default='"')
    p.add_argument("--csv-escapechar")
    p.add_argument("--csv-null-literal", default="")
    p.add_argument("inputs", nargs="+")
    args = p.parse_args(argv)
    if len(args.csv_quotechar) != 1 or (args.csv_escapechar is not None and len(args.csv_escapechar) != 1):
        p.error("quote and escape characters must each be one character")
    if args.memory_limit_mb is not None and args.memory_limit_mb < 1:
        p.error("memory limit must be positive")
    keys = [x.strip() for x in args.key.split(",") if x.strip()]
    if not keys:
        p.error("at least one key is required")
    try:
        schema = load_schema(args.schema) if args.schema else inspect_inputs(args.inputs, args.csv_quotechar, args.csv_escapechar, args.infer == "loose")
        schema_names = [x[0] for x in schema]
        if any(k not in schema_names for k in keys):
            raise ValueError("key column is not present in resolved schema")
        index = {name: i for i, name in enumerate(keys)}
        with tempfile.TemporaryDirectory(dir=args.temp_dir) as temp:
            runs = make_runs(args.inputs, schema, index, args, temp)
            if args.output == "-":
                merge_runs(runs, sys.stdout, schema_names, args.desc)
            else:
                with open(args.output, "w", encoding="utf-8", newline="") as out:
                    merge_runs(runs, out, schema_names, args.desc)
    except (OSError, csv.Error, json.JSONDecodeError, ValueError, StopIteration) as exc:
        print(f"merge_files.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

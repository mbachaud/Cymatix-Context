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
from decimal import Decimal, InvalidOperation


TYPES = {"string", "int", "float", "bool", "date", "timestamp"}
TYPE_PRIORITY = {"timestamp": 6, "date": 5, "bool": 4, "int": 3, "float": 2, "string": 1}


def parse_timestamp(value):
    s = value.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    # ISO 8601 permits a space as the separator too.
    parsed = dt.datetime.fromisoformat(s)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def parse_date(value):
    return dt.date.fromisoformat(value.strip())


def parse_bool(value):
    v = value.strip().lower()
    if v in {"true", "t", "yes", "y"}:
        return True
    if v in {"false", "f", "no", "n"}:
        return False
    if v == "1":
        return True
    if v == "0":
        return False
    raise ValueError("not a boolean")


def parse_int(value):
    s = value.strip()
    # int("1.0") is rightly rejected; bool is not accepted as an int here.
    if not s or any(c in s.lower() for c in (".", "e")):
        raise ValueError("not an integer")
    return int(s)


def parse_float(value):
    s = value.strip()
    if not s:
        raise ValueError("not a float")
    return float(s)


def try_type(value, typ):
    try:
        if typ == "int": parse_int(value)
        elif typ == "float": parse_float(value)
        elif typ == "bool": parse_bool(value)
        elif typ == "date": parse_date(value)
        elif typ == "timestamp": parse_timestamp(value)
        elif typ == "string": return True
        else: return False
        return True
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        return False


def classify(value):
    # Timestamp must precede date because a timestamp is not a date-only value.
    for typ in ("timestamp", "date", "bool", "int", "float"):
        if try_type(value, typ):
            return typ
    return "string"


def inferred_schema(paths, mode, dialect):
    names = set()
    kinds = {}
    candidates = {}
    for path in paths:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f, **dialect)
            try: header = next(reader)
            except StopIteration: raise ValueError(f"empty CSV input: {path}")
            names.update(header)
            for row in reader:
                for i, value in enumerate(row[:len(header)]):
                    if value != "":
                        name = header[i]
                        kinds.setdefault(name, set()).add(classify(value))
                        active = candidates.setdefault(name, {"timestamp", "date", "bool", "int", "float"})
                        active.intersection_update({t for t in active if try_type(value, t)})

    result = []
    for name in sorted(names):
        if name not in kinds:
            typ = "string"
        elif mode == "strict":
            found = kinds[name]
            typ = next(iter(found)) if len(found) == 1 else "string"
        else:
            # Pick the most specific type that accepts every observed value.
            typ = "string"
            for candidate in ("timestamp", "date", "bool", "int", "float"):
                if candidate in candidates[name]:
                    typ = candidate
                    break
        result.append((name, typ))
    return result


def load_schema(path):
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    columns = obj.get("columns") if isinstance(obj, dict) else None
    if not isinstance(columns, list) or not columns:
        raise ValueError("schema must contain a non-empty columns array")
    result, seen = [], set()
    for col in columns:
        if not isinstance(col, dict) or not isinstance(col.get("name"), str) or col.get("type") not in TYPES:
            raise ValueError("each schema column must have a name and a valid type")
        if col["name"] in seen:
            raise ValueError(f"duplicate schema column: {col['name']}")
        seen.add(col["name"])
        result.append((col["name"], col["type"]))
    return result


def cast(value, typ, on_error, null_literal, column, path, rownum):
    if value == "":
        return null_literal, True
    try:
        if typ == "string": out = value
        elif typ == "int": out = str(parse_int(value))
        elif typ == "float":
            number = parse_float(value)
            if number != number or number in (float("inf"), float("-inf")):
                raise ValueError("non-finite float")
            out = repr(number)
        elif typ == "bool": out = "true" if parse_bool(value) else "false"
        elif typ == "date": out = parse_date(value).isoformat()
        elif typ == "timestamp": out = parse_timestamp(value).isoformat().replace("+00:00", "Z")
        return out, False
    except Exception as exc:
        if on_error == "coerce-null": return null_literal, True
        if on_error == "keep-string": return value, False
        raise ValueError(f"{path}:{rownum}: invalid {typ} in column {column!r}: {exc}")


def compare_values(a, b, descending):
    an, bn = a is None, b is None
    if an or bn:
        if an and bn: return 0
        # Null is the lowest value; reversing the complete ordering puts it
        # last for descending output.
        result = -1 if an else 1
        return -result if descending else result
    try:
        c = (a > b) - (a < b)
    except TypeError:
        # Can occur when keep-string preserves a malformed value in a typed key.
        c = (str(a) > str(b)) - (str(a) < str(b))
    return -c if descending else c


def record_compare(a, b, key_indexes, descending):
    for index in key_indexes:
        c = compare_values(a[0][index], b[0][index], descending)
        if c: return c
    return (a[1] > b[1]) - (a[1] < b[1])


class HeapItem:
    def __init__(self, record, run, compare): self.record, self.run, self.compare = record, run, compare
    def __lt__(self, other): return self.compare(self.record, other.record) < 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--desc", action="store_true")
    p.add_argument("--schema")
    p.add_argument("--infer", choices=("strict", "loose"), default="strict")
    p.add_argument("--on-type-error", choices=("coerce-null", "fail", "keep-string"), default="coerce-null")
    p.add_argument("--memory-limit-mb", type=int, default=64)
    p.add_argument("--temp-dir")
    p.add_argument("--csv-quotechar", default='"')
    p.add_argument("--csv-escapechar")
    p.add_argument("--csv-null-literal", default="")
    p.add_argument("inputs", nargs="+")
    args = p.parse_args(argv)
    if args.memory_limit_mb <= 0: p.error("--memory-limit-mb must be positive")
    if len(args.csv_quotechar) != 1 or (args.csv_escapechar is not None and len(args.csv_escapechar) != 1):
        p.error("quotechar and escapechar must each be one character")
    dialect = {"delimiter": ",", "quotechar": args.csv_quotechar,
               "doublequote": args.csv_escapechar is None, "escapechar": args.csv_escapechar}
    try:
        schema = load_schema(args.schema) if args.schema else inferred_schema(args.inputs, args.infer, dialect)
        names = [n for n, _ in schema]
        keys = [x for x in args.key.split(",") if x]
        if not keys or any(k not in names for k in keys):
            raise ValueError("every --key column must be present in the resolved schema")
        key_indexes = [names.index(k) for k in keys]
        schema_types = dict(schema)
        compare = functools.partial(record_compare, key_indexes=key_indexes, descending=args.desc)
        temp_root = tempfile.mkdtemp(prefix="csv-merge-", dir=args.temp_dir)
        runs, records, sequence = [], [], 0
        # A conservative record-count cap keeps Python object overhead bounded.
        cap = max(1, args.memory_limit_mb * 1024 * 1024 // max(256, 64 * len(schema)))
        def spill():
            if not records: return
            records.sort(key=functools.cmp_to_key(compare))
            runpath = os.path.join(temp_root, f"run-{len(runs):06d}.bin")
            with open(runpath, "wb") as f:
                for rec in records: pickle.dump(rec, f, protocol=4)
            runs.append(runpath); records.clear()

        for path in args.inputs:
            with open(path, "r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f, **dialect)
                try: header = next(reader)
                except StopIteration: raise ValueError(f"empty CSV input: {path}")
                positions = {n: i for i, n in enumerate(header)}
                for rownum, row in enumerate(reader, 2):
                    values, nulls = [], []
                    for name, typ in schema:
                        value = row[positions[name]] if name in positions and positions[name] < len(row) else ""
                        out, is_null = cast(value, typ, args.on_type_error, args.csv_null_literal, name, path, rownum)
                        values.append(out); nulls.append(is_null)
                    # Keep typed keys for comparisons, while output remains strings.
                    typed_keys = [None] * len(schema)
                    for i in key_indexes:
                        if nulls[i]: typed_keys[i] = None; continue
                        typ = schema[i][1]; value = values[i]
                        try:
                            if typ == "int": typed_keys[i] = int(value)
                            elif typ == "float": typed_keys[i] = float(value)
                            elif typ == "bool": typed_keys[i] = value == "true"
                            elif typ == "date": typed_keys[i] = parse_date(value)
                            elif typ == "timestamp": typed_keys[i] = parse_timestamp(value)
                            else: typed_keys[i] = value
                        except (ValueError, TypeError):
                            # keep-string retains the original text, including for keys.
                            typed_keys[i] = value
                    records.append((typed_keys, sequence, values)); sequence += 1
                    if len(records) >= cap: spill()
        spill()
        out_f = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8", newline="")
        try:
            writer = csv.writer(out_f, delimiter=",", quotechar='"', doublequote=True, lineterminator="\n")
            writer.writerow(names)
            if not runs:
                return 0
            handles = [open(path, "rb") for path in runs]
            heap = []
            def read_one(i):
                try: return pickle.load(handles[i])
                except EOFError: return None
            for i in range(len(handles)):
                rec = read_one(i)
                if rec is not None: heapq.heappush(heap, HeapItem(rec, i, compare))
            while heap:
                item = heapq.heappop(heap)
                writer.writerow(item.record[2])
                rec = read_one(item.run)
                if rec is not None: heapq.heappush(heap, HeapItem(rec, item.run, compare))
            for h in handles: h.close()
        finally:
            if out_f is not sys.stdout: out_f.close()
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"merge_files.py: {exc}", file=sys.stderr)
        return 1
    finally:
        if 'temp_root' in locals() and os.path.isdir(temp_root): shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

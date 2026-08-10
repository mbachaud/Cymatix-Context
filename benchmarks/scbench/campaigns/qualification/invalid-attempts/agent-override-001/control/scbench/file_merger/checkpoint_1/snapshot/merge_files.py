#!/usr/bin/env python3
"""Merge CSV files, resolve their schemas, and externally sort their rows."""

import argparse
import csv
import datetime as dt
import functools
import heapq
import json
import math
import os
import pickle
import re
import sys
import tempfile
from pathlib import Path


TYPES = {"string", "int", "float", "bool", "date", "timestamp"}
TYPE_PRIORITY = {"timestamp": 6, "date": 5, "bool": 4, "int": 3, "float": 2, "string": 1}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DataError(Exception):
    pass


def bool_value(s):
    x = s.strip().lower()
    if x in {"true", "t", "yes", "y", "1"}:
        return True
    if x in {"false", "f", "no", "n", "0"}:
        return False
    raise ValueError("not a boolean")


def parse_date(s):
    if not DATE_RE.fullmatch(s):
        raise ValueError("not an ISO date")
    return dt.date.fromisoformat(s)


def parse_timestamp(s):
    text = s.strip()
    if "T" not in text and "t" not in text and " " not in text:
        raise ValueError("not an ISO timestamp")
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def classify(value):
    """Return the narrowest useful type for one non-empty cell."""
    for name, parser in (("timestamp", parse_timestamp), ("date", parse_date),
                         ("bool", bool_value)):
        try:
            parser(value)
            return name
        except (ValueError, TypeError, OverflowError):
            pass
    try:
        int(value.strip())
        return "int"
    except ValueError:
        pass
    try:
        n = float(value.strip())
        if math.isfinite(n):
            return "float"
    except ValueError:
        pass
    return "string"


def combined_type(types, loose=False):
    if not types:
        return "string"
    if loose:
        if all(x == "int" for x in types):
            return "int"
        if all(x in {"int", "float"} for x in types):
            return "float"
        if all(x == "bool" for x in types):
            return "bool"
        if all(x == "date" for x in types):
            return "date"
        if all(x == "timestamp" for x in types):
            return "timestamp"
        return "string"
    # A mixture of integer and floating-point observations is one numeric type.
    if set(types) <= {"int", "float"}:
        return "int" if set(types) == {"int"} else "float"
    return types[0] if all(x == types[0] for x in types) else "string"


def read_schema(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise DataError(f"cannot read schema {path}: {e}")
    columns = doc.get("columns") if isinstance(doc, dict) else None
    if not isinstance(columns, list) or not columns:
        raise DataError("schema must contain a non-empty 'columns' array")
    result = []
    seen = set()
    for item in columns:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or item.get("type") not in TYPES:
            raise DataError("each schema column needs a string name and a valid type")
        name = item["name"]
        if name in seen:
            raise DataError(f"duplicate schema column: {name}")
        seen.add(name)
        result.append((name, item["type"]))
    return result


def scan_headers(paths, dialect, loose=False):
    headers = []
    seen = set()
    per_file_types = []
    for path in paths:
        try:
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.reader(f, **dialect)
                header = next(reader, None)
                if header is None:
                    raise DataError(f"empty CSV input: {path}")
                if len(set(header)) != len(header):
                    raise DataError(f"duplicate column in header: {path}")
                for name in header:
                    if name not in seen:
                        seen.add(name)
                        headers.append(name)
                types = {name: [] for name in header}
                for row in reader:
                    for i, name in enumerate(header):
                        if i < len(row) and row[i] != "":
                            types[name].append(classify(row[i]))
                per_file_types.append(types)
        except (OSError, UnicodeError, csv.Error) as e:
            raise DataError(f"cannot read {path}: {e}")
    names = sorted(headers)
    resolved = []
    for name in names:
        if loose:
            observed = [t for ft in per_file_types for t in ft.get(name, [])]
            resolved.append((name, combined_type(observed, loose=True)))
        else:
            file_types = [combined_type(ft[name]) for ft in per_file_types if ft.get(name)]
            resolved.append((name, file_types[0] if file_types and all(t == file_types[0] for t in file_types) else "string"))
    return resolved


def cast(raw, typ):
    if raw == "":
        return None
    if typ == "string":
        return raw
    if typ == "int":
        return int(raw.strip())
    if typ == "float":
        value = float(raw.strip())
        if not math.isfinite(value):
            raise ValueError("non-finite float")
        return value
    if typ == "bool":
        return bool_value(raw)
    if typ == "date":
        return parse_date(raw.strip())
    if typ == "timestamp":
        return parse_timestamp(raw)
    raise ValueError(f"unknown type {typ}")


def format_value(value, typ):
    if value is None:
        return None
    if typ == "bool":
        return "true" if value else "false"
    if typ == "date":
        return value.isoformat()
    if typ == "timestamp":
        rendered = value.isoformat(timespec="microseconds")
        if "." in rendered:
            base, _ = rendered.rsplit("+00:00", 1)
            rendered = base.rstrip("0").rstrip(".") + "Z"
        else:
            rendered = rendered.replace("+00:00", "Z")
        return rendered
    return str(value)


class SortKey:
    __slots__ = ("parts",)

    def __init__(self, parts):
        self.parts = parts

    def __lt__(self, other):
        for (anull, aval, adesc), (bnull, bval, _) in zip(self.parts, other.parts):
            if anull != bnull:
                # Nulls first ascending and last descending.
                return anull > bnull if not adesc else anull < bnull
            if anull:
                continue
            if aval != bval:
                try:
                    return aval > bval if adesc else aval < bval
                except TypeError:
                    left, right = (type(aval).__name__, str(aval)), (type(bval).__name__, str(bval))
                    return left > right if adesc else left < right
        return False


def make_key(values, key_indexes, desc):
    return SortKey([(values[i] is None, values[i], desc) for i in key_indexes])


def merge(paths, schema, key_names, args, dialect, temp_root):
    indexes = {name: i for i, (name, _) in enumerate(schema)}
    try:
        key_indexes = [indexes[k] for k in key_names]
    except KeyError as e:
        raise DataError(f"key column is not present in resolved schema: {e.args[0]}")
    types = [typ for _, typ in schema]
    limit = max(1, args.memory_limit_mb or 64) * 1024 * 1024
    runs, chunk, chunk_size, sequence = [], [], 0, 0

    def flush():
        nonlocal chunk, chunk_size
        if not chunk:
            return
        chunk.sort(key=lambda x: (x[0], x[1]))
        fd, name = tempfile.mkstemp(prefix="csvrun-", suffix=".bin", dir=temp_root)
        with os.fdopen(fd, "wb") as f:
            for item in chunk:
                pickle.dump(item, f, protocol=pickle.HIGHEST_PROTOCOL)
        runs.append(name)
        chunk, chunk_size = [], 0

    for path in paths:
        try:
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.reader(f, **dialect)
                header = next(reader, None)
                if header is None:
                    raise DataError(f"empty CSV input: {path}")
                positions = {name: i for i, name in enumerate(header)}
                for row in reader:
                    vals = []
                    for name, typ in schema:
                        raw = row[positions[name]] if name in positions and positions[name] < len(row) else ""
                        try:
                            vals.append(cast(raw, typ))
                        except (ValueError, TypeError, OverflowError) as e:
                            if args.on_type_error == "fail":
                                raise DataError(f"{path}: row {reader.line_num}, column {name}: {e}")
                            vals.append(raw if args.on_type_error == "keep-string" else None)
                    item = (make_key(vals, key_indexes, args.desc), sequence, vals)
                    sequence += 1
                    chunk.append(item)
                    chunk_size += sum(len(str(v)) for v in vals) + 128
                    if chunk_size >= limit:
                        flush()
        except (OSError, UnicodeError, csv.Error) as e:
            raise DataError(f"cannot read {path}: {e}")
    flush()

    def output_stream():
        if args.output == "-":
            return sys.stdout, False
        return open(args.output, "w", newline="", encoding="utf-8"), True

    streams = []
    try:
        for name in runs:
            f = open(name, "rb")
            streams.append(f)
        heap = []
        for run_no, f in enumerate(streams):
            item = pickle.load(f)
            heapq.heappush(heap, (item[0], item[1], run_no, item[2]))
        out, close_out = output_stream()
        try:
            writer = csv.writer(out, delimiter=",", quotechar='"', doublequote=True,
                                lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
            writer.writerow([name for name, _ in schema])
            while heap:
                _, _, run_no, vals = heapq.heappop(heap)
                writer.writerow([format_value(v, typ) if v is not None else args.csv_null_literal
                                 for v, (_, typ) in zip(vals, schema)])
                try:
                    item = pickle.load(streams[run_no])
                    heapq.heappush(heap, (item[0], item[1], run_no, item[2]))
                except EOFError:
                    pass
        finally:
            if close_out:
                out.close()
    finally:
        for f in streams:
            f.close()
        for name in runs:
            try:
                os.unlink(name)
            except OSError:
                pass


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
    if args.memory_limit_mb is not None and args.memory_limit_mb <= 0:
        p.error("--memory-limit-mb must be positive")
    if len(args.csv_quotechar) != 1 or (args.csv_escapechar is not None and len(args.csv_escapechar) != 1):
        p.error("CSV quote and escape characters must each be one character")
    output_abs = os.path.abspath(args.output) if args.output != "-" else None
    if output_abs and any(output_abs == os.path.abspath(x) for x in args.inputs):
        p.error("output path must not be one of the input paths")
    dialect = {"delimiter": ",", "quotechar": args.csv_quotechar,
               "escapechar": args.csv_escapechar, "doublequote": args.csv_escapechar is None}
    try:
        schema = read_schema(args.schema) if args.schema else scan_headers(args.inputs, dialect, args.infer == "loose")
        keys = [x for x in args.key.split(",") if x]
        if not keys:
            raise DataError("--key must name at least one column")
        temp_parent = args.temp_dir or None
        with tempfile.TemporaryDirectory(prefix="csv-merge-", dir=temp_parent) as temp_root:
            merge(args.inputs, schema, keys, args, dialect, temp_root)
    except (DataError, OSError, pickle.PickleError) as e:
        print(f"merge_files.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

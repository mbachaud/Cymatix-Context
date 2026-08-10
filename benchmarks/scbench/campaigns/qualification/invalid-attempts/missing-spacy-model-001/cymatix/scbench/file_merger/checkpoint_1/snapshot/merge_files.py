#!/usr/bin/env python3
"""Merge CSV files, resolve their schemas, and perform a stable external sort."""

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
from pathlib import Path


TYPES = ("string", "int", "float", "bool", "date", "timestamp")
TYPE_PRIORITY = {"string": 0, "float": 1, "int": 2, "bool": 3, "date": 4, "timestamp": 5}
INT_RE = re.compile(r"^[+-]?\d+$")


def timestamp_value(value):
    s = value.strip()
    if not re.search(r"[Tt ]", s):
        raise ValueError("not a timestamp")
    z = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    parsed = dt.datetime.fromisoformat(z)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def parse_as(value, typ):
    s = value.strip()
    if typ == "string":
        return value
    if typ == "int":
        if not INT_RE.fullmatch(s):
            raise ValueError("invalid int")
        return int(s)
    if typ == "float":
        return float(s)
    if typ == "bool":
        low = s.lower()
        if low in ("1", "true", "yes", "y", "t"):
            return True
        if low in ("0", "false", "no", "n", "f"):
            return False
        raise ValueError("invalid bool")
    if typ == "date":
        return dt.date.fromisoformat(s)
    if typ == "timestamp":
        return timestamp_value(s)
    raise ValueError("unknown type")


def format_value(value, typ):
    if typ == "string":
        return value
    if typ == "int":
        return str(value)
    if typ == "float":
        return str(value)
    if typ == "bool":
        return "true" if value else "false"
    if typ == "date":
        return value.isoformat()
    if typ == "timestamp":
        text = value.isoformat(timespec="auto").replace("+00:00", "Z")
        if "." in text:
            head, tail = text.split(".", 1)
            suffix = "Z" if tail.endswith("Z") else ""
            digits = tail[:-1] if suffix else tail
            text = head + ("." + digits.rstrip("0") if digits.rstrip("0") else "") + suffix
        return text
    return str(value)


def value_kind(value):
    """Classify one non-null value, using the most specific valid type."""
    for typ in ("timestamp", "date", "bool", "int", "float"):
        try:
            parse_as(value, typ)
            return typ
        except (ValueError, OverflowError):
            pass
    return "string"


def kind_for_values(values):
    for typ in sorted(TYPES[1:], key=lambda x: TYPE_PRIORITY[x], reverse=True):
        try:
            for value in values:
                parse_as(value, typ)
            return typ
        except (ValueError, OverflowError):
            continue
    return "string"


def infer_schema(paths, infer_mode, quotechar, escapechar):
    names = set()
    per_file = []
    overall = {}
    candidates0 = set(TYPES[1:])
    for path in paths:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f, delimiter=",", quotechar=quotechar,
                                escapechar=escapechar, doublequote=escapechar is None)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError(f"input is missing a header: {path}")
            if len(set(header)) != len(header):
                raise ValueError(f"duplicate column in header: {path}")
            names.update(header)
            observed = {name: set(candidates0) for name in header}
            seen = {name: False for name in header}
            for row in reader:
                for i, name in enumerate(header):
                    if i < len(row) and row[i] != "":
                        seen[name] = True
                        for typ in tuple(observed[name]):
                            try:
                                parse_as(row[i], typ)
                            except (ValueError, OverflowError):
                                observed[name].discard(typ)
            per_file.append((observed, seen))
            for name in header:
                if seen[name]:
                    if name not in overall:
                        overall[name] = set(observed[name])
                    else:
                        overall[name].intersection_update(observed[name])

    result = []
    for name in sorted(names):
        files_seen = []
        for observed, seen in per_file:
            if seen.get(name, False):
                possible = observed[name]
                files_seen.append(max(possible, key=lambda x: TYPE_PRIORITY[x]) if possible else "string")
        if not files_seen:
            typ = "string"
        elif infer_mode == "strict":
            typ = files_seen[0] if len(set(files_seen)) == 1 else "string"
        else:
            possible = overall.get(name, set())
            typ = max(possible, key=lambda x: TYPE_PRIORITY[x]) if possible else "string"
        result.append((name, typ))
    return result


def read_schema(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    columns = data.get("columns") if isinstance(data, dict) else None
    if not isinstance(columns, list) or not columns:
        raise ValueError("schema must contain a non-empty columns array")
    result = []
    seen = set()
    for col in columns:
        if not isinstance(col, dict) or not isinstance(col.get("name"), str) or col.get("type") not in TYPES:
            raise ValueError("each schema column needs a name and valid type")
        if col["name"] in seen:
            raise ValueError(f"duplicate schema column: {col['name']}")
        seen.add(col["name"])
        result.append((col["name"], col["type"]))
    return result


def compare_records(a, b, key_indexes, descending):
    for idx in key_indexes:
        av, bv = a[0][idx], b[0][idx]
        if av is None and bv is None:
            continue
        if av is None:
            return -1
        if bv is None:
            return 1
        if type(av) is not type(bv) and not (isinstance(av, (int, float)) and isinstance(bv, (int, float))):
            av, bv = str(av), str(bv)
        if av < bv:
            return 1 if descending else -1
        if av > bv:
            return -1 if descending else 1
    return (a[2] > b[2]) - (a[2] < b[2])


class HeapItem:
    def __init__(self, record, source, key_indexes, descending):
        self.record, self.source = record, source
        self.key_indexes, self.descending = key_indexes, descending

    def __lt__(self, other):
        return compare_records(self.record, other.record, self.key_indexes, self.descending) < 0


def merge(args):
    if not args.inputs:
        raise ValueError("at least one input CSV is required")
    if not args.key:
        raise ValueError("--key requires at least one column")
    schema = read_schema(args.schema) if args.schema else infer_schema(
        args.inputs, args.infer, args.csv_quotechar, args.csv_escapechar)
    schema_names = [x[0] for x in schema]
    indexes = {name: i for i, name in enumerate(schema_names)}
    keys = [x.strip() for x in args.key.split(",")]
    if any(not x for x in keys):
        raise ValueError("--key contains an empty column name")
    missing_keys = [x for x in keys if x not in indexes]
    if missing_keys:
        raise ValueError("key column not in resolved schema: " + ", ".join(missing_keys))
    key_indexes = [indexes[x] for x in keys]
    type_by_name = dict(schema)
    null_literal = args.csv_null_literal
    # A conservative row budget accounts for Python object/list overhead while
    # the serialized-size check handles large cells more accurately.
    byte_limit = max(1, args.memory_limit_mb) * 1024 * 1024
    chunks = []
    sequence = 0
    with tempfile.TemporaryDirectory(dir=args.temp_dir) as temp_dir:
        chunk = []
        chunk_bytes = 0
        for path in args.inputs:
            with open(path, "r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f, delimiter=",", quotechar=args.csv_quotechar,
                                    escapechar=args.csv_escapechar,
                                    doublequote=args.csv_escapechar is None)
                try:
                    header = next(reader)
                except StopIteration:
                    raise ValueError(f"input is missing a header: {path}")
                if len(set(header)) != len(header):
                    raise ValueError(f"duplicate column in header: {path}")
                locations = {name: i for i, name in enumerate(header)}
                for row in reader:
                    typed, rendered = [], []
                    for name, typ in schema:
                        raw = row[locations[name]] if name in locations and locations[name] < len(row) else ""
                        if raw == "":
                            typed.append(None); rendered.append(null_literal); continue
                        try:
                            parsed = parse_as(raw, typ)
                            typed.append(parsed); rendered.append(format_value(parsed, typ))
                        except (ValueError, OverflowError) as exc:
                            if args.on_type_error == "fail":
                                raise ValueError(f"{path}: column {name}: cannot cast {raw!r} to {typ}: {exc}")
                            if args.on_type_error == "keep-string":
                                typed.append(raw); rendered.append(raw)
                            else:
                                typed.append(None); rendered.append(null_literal)
                    rec = (tuple(typed), tuple(rendered), sequence)
                    sequence += 1
                    chunk.append(rec)
                    chunk_bytes += sum(len(x.encode("utf-8")) for x in rendered) + 256
                    if chunk_bytes >= byte_limit:
                        chunk.sort(key=functools.cmp_to_key(lambda a, b: compare_records(a, b, key_indexes, args.desc)))
                        chunk_path = os.path.join(temp_dir, f"chunk-{len(chunks):08d}.bin")
                        with open(chunk_path, "wb") as out:
                            pickle.dump(chunk, out, protocol=pickle.HIGHEST_PROTOCOL)
                        chunks.append(chunk_path); chunk = []; chunk_bytes = 0
        if chunk:
            chunk.sort(key=functools.cmp_to_key(lambda a, b: compare_records(a, b, key_indexes, args.desc)))
            chunk_path = os.path.join(temp_dir, f"chunk-{len(chunks):08d}.bin")
            with open(chunk_path, "wb") as out:
                pickle.dump(chunk, out, protocol=pickle.HIGHEST_PROTOCOL)
            chunks.append(chunk_path)

        streams = [open(p, "rb") for p in chunks]
        try:
            heap = []
            for i, stream in enumerate(streams):
                records = pickle.load(stream)
                if records:
                    heapq.heappush(heap, HeapItem(records[0], (i, records, 0), key_indexes, args.desc))
            out_stream = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8", newline="")
            try:
                writer = csv.writer(out_stream, delimiter=",", quotechar='"', doublequote=True, lineterminator="\n")
                writer.writerow(schema_names)
                while heap:
                    item = heapq.heappop(heap)
                    rec = item.record
                    writer.writerow(rec[1])
                    i, records, pos = item.source
                    pos += 1
                    if pos < len(records):
                        heapq.heappush(heap, HeapItem(records[pos], (i, records, pos), key_indexes, args.desc))
            finally:
                if out_stream is not sys.stdout:
                    out_stream.close()
        finally:
            for stream in streams:
                stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--desc", action="store_true")
    parser.add_argument("--schema")
    parser.add_argument("--infer", choices=("strict", "loose"), default="strict")
    parser.add_argument("--on-type-error", choices=("coerce-null", "fail", "keep-string"), default="coerce-null")
    parser.add_argument("--memory-limit-mb", type=int, default=64)
    parser.add_argument("--temp-dir")
    parser.add_argument("--csv-quotechar", default='"')
    parser.add_argument("--csv-escapechar")
    parser.add_argument("--csv-null-literal", default="")
    parser.add_argument("inputs", nargs="+")
    args = parser.parse_args()
    if len(args.csv_quotechar) != 1 or (args.csv_escapechar is not None and len(args.csv_escapechar) != 1):
        parser.error("CSV quote and escape characters must each be one character")
    if args.memory_limit_mb < 1:
        parser.error("--memory-limit-mb must be positive")
    try:
        merge(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"merge_files.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Merge CSV files, resolve their schemas, and externally sort the result."""

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


TYPES = {"string", "int", "float", "bool", "date", "timestamp"}
TYPE_PRIORITY = {"timestamp": 6, "date": 5, "bool": 4, "int": 3, "float": 2, "string": 1}
BOOLS = {"true": True, "false": False, "yes": True, "no": False, "1": True, "0": False}


def parse_timestamp(value):
    s = value.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        value_dt = dt.datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if value_dt.tzinfo is None:
        value_dt = value_dt.replace(tzinfo=dt.timezone.utc)
    return value_dt.astimezone(dt.timezone.utc)


def parse_value(value, typ):
    if value == "":
        return None
    if typ == "string":
        return value
    if typ == "int":
        # Avoid accepting strings such as 1.0 as integers.
        if not value.strip() or value.strip() != value or not value.lstrip("+-").isdigit():
            raise ValueError("invalid integer")
        return int(value)
    if typ == "float":
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("non-finite float")
        return result
    if typ == "bool":
        try:
            return BOOLS[value.strip().lower()]
        except KeyError as exc:
            raise ValueError("invalid boolean") from exc
    if typ == "date":
        result = dt.date.fromisoformat(value)
        if len(value) != 10 or result.isoformat() != value:
            raise ValueError("invalid ISO date")
        return result
    if typ == "timestamp":
        return parse_timestamp(value)
    raise ValueError("unknown type")


def format_value(value, typ):
    if value is None:
        return None
    if typ == "string":
        return str(value)
    if typ == "int":
        return str(value)
    if typ == "float":
        return repr(value)
    if typ == "bool":
        return "true" if value else "false"
    if typ == "date":
        return value.isoformat()
    if typ == "timestamp":
        return value.isoformat().replace("+00:00", "Z")
    raise ValueError("unknown type")


def csv_reader(path, quotechar, escapechar):
    # A missing escapechar means RFC-style quote doubling.
    kwargs = {"delimiter": ",", "quotechar": quotechar, "lineterminator": "\n"}
    if escapechar is None:
        kwargs["doublequote"] = True
    else:
        kwargs.update(escapechar=escapechar, doublequote=False)
    stream = sys.stdin if path == "-" else open(path, "r", encoding="utf-8", newline="")
    return stream, csv.reader(stream, **kwargs)


def read_header(path, quotechar, escapechar):
    stream, reader = csv_reader(path, quotechar, escapechar)
    try:
        header = next(reader)
    finally:
        if stream is not sys.stdin:
            stream.close()
    if not header:
        raise ValueError(f"{path}: missing header row")
    return header


def infer_type(values):
    nonnull = [v for v in values if v != ""]
    if not nonnull:
        return "string"
    candidates = ["timestamp", "date", "bool", "int", "float"]
    valid = [t for t in candidates if all(_parses(v, t) for v in nonnull)]
    return max(valid, key=lambda t: TYPE_PRIORITY[t]) if valid else "string"


def _parses(value, typ):
    try:
        parse_value(value, typ)
        return True
    except (ValueError, OverflowError):
        return False


def resolve_schema(args):
    if args.schema:
        with open(args.schema, encoding="utf-8") as f:
            data = json.load(f)
        columns = data.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValueError("schema must contain a non-empty columns list")
        result = []
        names = set()
        for item in columns:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or item.get("type") not in TYPES:
                raise ValueError("each schema column needs a name and a valid type")
            if item["name"] in names:
                raise ValueError(f"duplicate schema column: {item['name']}")
            names.add(item["name"])
            result.append((item["name"], item["type"]))
        return result

    headers = [read_header(p, args.csv_quotechar, args.csv_escapechar) for p in args.inputs]
    names = sorted({name for header in headers for name in header})
    if args.infer == "loose":
        observations = {name: [] for name in names}
        for path in args.inputs:
            stream, reader = csv_reader(path, args.csv_quotechar, args.csv_escapechar)
            try:
                header = next(reader)
                for row in reader:
                    for i, name in enumerate(header):
                        if name in observations:
                            observations[name].append(row[i] if i < len(row) else "")
            finally:
                if stream is not sys.stdin:
                    stream.close()
        return [(name, infer_type(observations[name])) for name in names]

    # Strict inference first infers each file independently, then rejects
    # columns whose per-file types disagree.
    per_file = {name: [] for name in names}
    for path in args.inputs:
        stream, reader = csv_reader(path, args.csv_quotechar, args.csv_escapechar)
        try:
            header = next(reader)
            values = {name: [] for name in header}
            for row in reader:
                for i, name in enumerate(header):
                    values[name].append(row[i] if i < len(row) else "")
            for name in header:
                # A file with no observed non-null value contributes no type
                # evidence to strict inference.
                if any(value != "" for value in values[name]):
                    per_file[name].append(infer_type(values[name]))
        finally:
            if stream is not sys.stdin:
                stream.close()
    result = []
    for name in names:
        types = set(per_file[name])
        result.append((name, types.pop() if len(types) == 1 else "string") if types else (name, "string"))
    return result


def compare_atoms(a, b, direction):
    if a is None or b is None:
        if a is None and b is None:
            return 0
        # Null is intrinsically less; direction reverses non-null ordering.
        return (-1 if a is None else 1) * direction
    try:
        if a < b:
            return -1 * direction
        if a > b:
            return 1 * direction
    except TypeError:
        # keep-string can deliberately mix a failed cast with valid values.
        # Give that otherwise-invalid comparison a deterministic ordering.
        left, right = str(a), str(b)
        if left < right:
            return -1 * direction
        if left > right:
            return 1 * direction
    return 0


def cast_row(raw, positions, schema, on_error, null_literal, source, line_number):
    output, keys = [], []
    for name, typ in schema:
        original = raw[positions[name]] if name in positions and positions[name] < len(raw) else ""
        try:
            parsed = parse_value(original, typ)
            output.append(null_literal if parsed is None else format_value(parsed, typ))
            keys.append(parsed)
        except (ValueError, OverflowError) as exc:
            if on_error == "fail":
                raise ValueError(f"{source}:{line_number}: cannot cast {name!r} to {typ}: {original!r} ({exc})")
            if on_error == "keep-string":
                output.append(original)
                keys.append(original if original != "" else None)
            else:
                output.append(null_literal)
                keys.append(None)
    return keys, output


def write_chunk(records, temp_dir, direction):
    records.sort()
    fd, path = tempfile.mkstemp(prefix="merge-", suffix=".bin", dir=temp_dir)
    with os.fdopen(fd, "wb") as f:
        for record in records:
            pickle.dump((record.keys, record.row, record.sequence), f, protocol=pickle.HIGHEST_PROTOCOL)
    records.clear()
    return path


class HeapItem:
    __slots__ = ("keys", "row", "sequence", "source", "direction")
    def __init__(self, keys, row, sequence, source, direction):
        self.keys, self.row, self.sequence, self.source, self.direction = keys, row, sequence, source, direction
    def __lt__(self, other):
        for a, b in zip(self.keys, other.keys):
            c = compare_atoms(a, b, self.direction)
            if c:
                return c < 0
        return self.sequence < other.sequence


def merge_chunks(paths, writer, direction):
    files = [open(p, "rb") for p in paths]
    heap = []
    def advance(i):
        try:
            keys, row, sequence = pickle.load(files[i])
            heapq.heappush(heap, HeapItem(keys, row, sequence, i, direction))
        except EOFError:
            pass
    try:
        for i in range(len(files)):
            advance(i)
        while heap:
            item = heapq.heappop(heap)
            writer.writerow(item.row)
            advance(item.source)
    finally:
        for f in files:
            f.close()


def run(args):
    schema = resolve_schema(args)
    schema_names = {name for name, _ in schema}
    keys = [k.strip() for k in args.key.split(",") if k.strip()]
    if not keys or any(k not in schema_names for k in keys):
        missing = [k for k in keys if k not in schema_names]
        raise ValueError("key column is missing from resolved schema: " + ", ".join(missing or keys))
    direction = -1 if args.desc else 1
    # Keep chunks comfortably below the requested cap; pickle and Python
    # object overhead mean the raw limit is not a safe chunk size.
    chunk_limit = max(64 * 1024, args.memory_limit_mb * 1024 * 1024 // 3)
    chunks, records, estimated, sequence = [], [], 0, 0
    temp_paths = []
    try:
        for path in args.inputs:
            stream, reader = csv_reader(path, args.csv_quotechar, args.csv_escapechar)
            try:
                header = next(reader)
                positions = {name: i for i, name in enumerate(header)}
                for line_number, raw in enumerate(reader, 2):
                    all_values, output = cast_row(raw, positions, schema, args.on_type_error, args.csv_null_literal, path, line_number)
                    key_values = [all_values[next(i for i, (name, _) in enumerate(schema) if name == key)] for key in keys]
                    record = HeapItem(key_values, output, sequence, -1, direction)
                    records.append(record)
                    estimated += sum(len(x) for x in output) + 128
                    sequence += 1
                    if estimated >= chunk_limit:
                        chunks.append(write_chunk(records, args.temp_dir, direction))
                        temp_paths.append(chunks[-1])
                        estimated = 0
            finally:
                if stream is not sys.stdin:
                    stream.close()
        if records:
            chunks.append(write_chunk(records, args.temp_dir, direction))
            temp_paths.append(chunks[-1])

        output_stream = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8", newline="")
        try:
            writer = csv.writer(output_stream, delimiter=",", quotechar='"', doublequote=True, lineterminator="\n")
            writer.writerow([name for name, _ in schema])
            if chunks:
                merge_chunks(chunks, writer, direction)
        finally:
            if output_stream is not sys.stdout:
                output_stream.close()
    finally:
        for path in temp_paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--desc", action="store_true")
    parser.add_argument("--schema")
    parser.add_argument("--infer", choices=["strict", "loose"], default="strict")
    parser.add_argument("--on-type-error", choices=["coerce-null", "fail", "keep-string"], default="coerce-null")
    parser.add_argument("--memory-limit-mb", type=int, default=64)
    parser.add_argument("--temp-dir")
    parser.add_argument("--csv-quotechar", default='"')
    parser.add_argument("--csv-escapechar")
    parser.add_argument("--csv-null-literal", default="")
    parser.add_argument("inputs", nargs="+")
    args = parser.parse_args()
    if args.memory_limit_mb <= 0:
        parser.error("--memory-limit-mb must be positive")
    if len(args.csv_quotechar) != 1 or (args.csv_escapechar is not None and len(args.csv_escapechar) != 1):
        parser.error("CSV quote and escape characters must be exactly one character")
    try:
        run(args)
    except (OSError, ValueError, json.JSONDecodeError, csv.Error) as exc:
        print(f"merge_files.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

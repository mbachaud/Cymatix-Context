#!/usr/bin/env python3
"""Merge CSV files, resolve their schema, and perform an external stable sort."""

import argparse
import csv
import datetime as dt
import heapq
import json
import os
import pickle
import sys
import tempfile
from pathlib import Path


TYPES = ("string", "float", "int", "bool", "date", "timestamp")
TYPE_PRIORITY = {"string": 0, "float": 1, "int": 2, "bool": 3, "date": 4, "timestamp": 5}
TRUE_VALUES = {"1", "true", "t", "yes", "y"}
FALSE_VALUES = {"0", "false", "f", "no", "n"}


def parse_timestamp(value):
    s = value.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(s)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def parse_value(value, kind):
    if kind == "string":
        return value
    if kind == "int":
        return int(value.strip())
    if kind == "float":
        result = float(value.strip())
        if result != result or result in (float("inf"), float("-inf")):
            raise ValueError("non-finite float")
        return result
    if kind == "bool":
        lowered = value.strip().lower()
        if lowered in TRUE_VALUES:
            return True
        if lowered in FALSE_VALUES:
            return False
        raise ValueError("invalid boolean")
    if kind == "date":
        parsed = dt.date.fromisoformat(value.strip())
        return parsed
    if kind == "timestamp":
        return parse_timestamp(value)
    raise ValueError("unknown type")


def classify(value):
    for kind in ("timestamp", "date", "bool", "int", "float"):
        try:
            parse_value(value, kind)
            return kind
        except (TypeError, ValueError, OverflowError):
            pass
    return "string"


class Reverse:
    """A pickleable reverse-order wrapper for values used in heap keys."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return self.value > other.value

    def __eq__(self, other):
        return isinstance(other, Reverse) and self.value == other.value

    def __hash__(self):
        return hash(self.value)


def is_null(value, null_literal):
    return value == "" or value == null_literal


def csv_reader(path, quotechar, escapechar):
    stream = open(path, "r", encoding="utf-8", newline="")
    reader = csv.reader(stream, delimiter=",", quotechar=quotechar,
                        escapechar=escapechar, doublequote=(escapechar is None))
    return stream, reader


def read_headers(paths, quotechar, escapechar):
    headers = []
    for path in paths:
        stream, reader = csv_reader(path, quotechar, escapechar)
        try:
            header = next(reader)
            if len(set(header)) != len(header):
                raise ValueError(f"duplicate column in {path}")
            headers.append(header)
        except StopIteration:
            raise ValueError(f"empty CSV file: {path}")
        finally:
            stream.close()
    return headers


def resolve_schema(paths, schema_path, infer_mode, quotechar, escapechar, null_literal):
    headers = read_headers(paths, quotechar, escapechar)
    if schema_path:
        with open(schema_path, encoding="utf-8") as f:
            data = json.load(f)
        columns = data.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValueError("schema must contain a non-empty columns list")
        result = []
        names = set()
        for item in columns:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or item.get("type") not in TYPES:
                raise ValueError("invalid schema column; valid types are string, int, float, bool, date, timestamp")
            if item["name"] in names:
                raise ValueError("duplicate column in schema")
            names.add(item["name"])
            result.append((item["name"], item["type"]))
        return result

    all_names = sorted({name for header in headers for name in header})
    evidence = {name: [] for name in all_names}
    for path in paths:
        stream, reader = csv_reader(path, quotechar, escapechar)
        header = next(reader)
        positions = {name: i for i, name in enumerate(header)}
        per_file = {name: [] for name in all_names}
        for row in reader:
            for name, pos in positions.items():
                if pos < len(row) and not is_null(row[pos], null_literal):
                    per_file[name].append(classify(row[pos]))
        for name, kinds in per_file.items():
            if kinds:
                evidence[name].append(kinds)
        stream.close()

    result = []
    for name in all_names:
        files = evidence[name]
        if not files:
            kind = "string"
        elif infer_mode == "strict":
            file_types = [max(set(values), key=lambda x: TYPE_PRIORITY[x]) if len(set(values)) == 1 else "string" for values in files]
            kind = file_types[0] if all(x == file_types[0] for x in file_types) else "string"
        else:
            values = [kind for file_values in files for kind in file_values]
            kind = max(set(values), key=lambda x: TYPE_PRIORITY[x])
            # A type is loose-compatible only when every observed value parses as it.
            # Re-read values below through the normal cast path to disambiguate candidates.
            stream_values = []
            for path, header in zip(paths, headers):
                stream, reader = csv_reader(path, quotechar, escapechar)
                next(reader)
                pos = header.index(name) if name in header else None
                if pos is not None:
                    for row in reader:
                        if pos < len(row) and not is_null(row[pos], null_literal):
                            stream_values.append(row[pos])
                stream.close()
            for candidate in ("timestamp", "date", "bool", "int", "float"):
                try:
                    for value in stream_values:
                        parse_value(value, candidate)
                    kind = candidate
                    break
                except (TypeError, ValueError, OverflowError):
                    continue
        result.append((name, kind))
    return result


def cast_cell(raw, kind, null_literal, on_error, column, source, line):
    if raw is None or is_null(raw, null_literal):
        return None
    try:
        return parse_value(raw, kind)
    except (TypeError, ValueError, OverflowError) as exc:
        if on_error == "fail":
            raise ValueError(f"{source}:{line}: cannot cast column {column!r} to {kind}: {exc}")
        if on_error == "keep-string":
            return raw
        return None


def output_value(value, kind, null_literal):
    if value is None:
        return null_literal
    if kind == "bool":
        return "true" if value else "false"
    if kind == "timestamp":
        return value.strftime("%Y-%m-%dT%H:%M:%S") + (f".{value.microsecond:06d}".rstrip("0") if value.microsecond else "") + "Z"
    return str(value)


def make_key(key_values, desc):
    result = []
    for value, kind in key_values:
        if value is None:
            result.append((1 if desc else 0, None))
        else:
            # keep-string may produce a string in a column otherwise containing
            # parsed values.  The type tag keeps those values comparable and
            # makes the fallback ordering deterministic.
            expected = not (kind != "string" and isinstance(value, str))
            sortable = (0 if expected else 1, value)
            result.append((0 if desc else 1, Reverse(sortable) if desc else sortable))
    return tuple(result)


def merge(paths, output, schema, keys, desc, args, temp_root):
    schema_index = {name: (i, kind) for i, (name, kind) in enumerate(schema)}
    key_indexes = [(schema_index[k][0], schema_index[k][1]) for k in keys]
    chunk_budget = max(1, args.memory_limit_mb * 1024 * 1024 * 3 // 5)
    runs = []
    records = []
    record_bytes = 0
    sequence = 0

    def flush():
        nonlocal records, record_bytes
        if not records:
            return
        records.sort(key=lambda record: (record[0], record[1]))
        run_path = os.path.join(temp_root, f"run-{len(runs):06d}.pkl")
        with open(run_path, "wb") as f:
            for record in records:
                pickle.dump(record, f, protocol=4)
        runs.append(run_path)
        records, record_bytes = [], 0

    for path in paths:
        stream, reader = csv_reader(path, args.csv_quotechar, args.csv_escapechar)
        header = next(reader)
        positions = {name: i for i, name in enumerate(header)}
        for line, row in enumerate(reader, 2):
            values = []
            for name, kind in schema:
                pos = positions.get(name)
                raw = row[pos] if pos is not None and pos < len(row) else None
                values.append(cast_cell(raw, kind, args.csv_null_literal, args.on_type_error, name, path, line))
            rendered = tuple(output_value(v, kind, args.csv_null_literal) for v, (_, kind) in zip(values, schema))
            key = make_key([(values[i], kind) for i, kind in key_indexes], desc)
            record = (key, sequence, rendered)
            records.append(record)
            record_bytes += len(pickle.dumps(record, protocol=4)) + 64
            sequence += 1
            if record_bytes >= chunk_budget:
                flush()
        stream.close()
    flush()

    streams = []
    heap = []
    try:
        for run_number, run_path in enumerate(runs):
            stream = open(run_path, "rb")
            streams.append(stream)
            try:
                record = pickle.load(stream)
                heapq.heappush(heap, (record[0], record[1], run_number, record[2]))
            except EOFError:
                pass
        for record in heap_sorted(heap, streams):
            output.writerow(record)
    finally:
        for stream in streams:
            stream.close()


def heap_sorted(heap, streams):
    while heap:
        _, _, run_number, rendered = heapq.heappop(heap)
        yield rendered
        try:
            record = pickle.load(streams[run_number])
            heapq.heappush(heap, (record[0], record[1], run_number, record[2]))
        except EOFError:
            pass


def build_parser():
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
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.memory_limit_mb <= 0 or len(args.csv_quotechar) != 1 or (args.csv_escapechar is not None and len(args.csv_escapechar) != 1):
        raise ValueError("memory-limit-mb must be positive; quote and escape characters must be one character")
    paths = [str(Path(p)) for p in args.inputs]
    schema = resolve_schema(paths, args.schema, args.infer, args.csv_quotechar, args.csv_escapechar, args.csv_null_literal)
    keys = [key.strip() for key in args.key.split(",") if key.strip()]
    if not keys or len(set(keys)) != len(keys):
        raise ValueError("key must contain one or more distinct column names")
    missing = [key for key in keys if key not in dict(schema)]
    if missing:
        raise ValueError("key column(s) not present in resolved schema: " + ", ".join(missing))

    with tempfile.TemporaryDirectory(prefix="csv-merge-", dir=args.temp_dir) as temp_root:
        if args.output == "-":
            output_stream = sys.stdout
            close_output = False
        else:
            output_stream = open(args.output, "w", encoding="utf-8", newline="")
            close_output = True
        try:
            writer = csv.writer(output_stream, delimiter=",", quotechar='"', escapechar=None,
                                doublequote=True, lineterminator="\n")
            writer.writerow([name for name, _ in schema])
            merge(paths, writer, schema, keys, args.desc, args, temp_root)
        finally:
            if close_output:
                output_stream.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError, csv.Error) as exc:
        print(f"merge_files.py: {exc}", file=sys.stderr)
        sys.exit(1)

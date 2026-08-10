#!/usr/bin/env python3
"""Merge CSV files, resolve their schema, and perform a stable external sort."""
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

TYPES = ("string", "int", "float", "bool", "date", "timestamp")
PRIORITY = {"timestamp": 6, "date": 5, "bool": 4, "int": 3, "float": 2, "string": 1}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_value(text, typ):
    if text == "":
        return None
    if typ == "string":
        return text
    if typ == "int":
        if not re.fullmatch(r"[+-]?\d+", text):
            raise ValueError("not an integer")
        return int(text)
    if typ == "float":
        value = float(text)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite float")
        return value
    if typ == "bool":
        lowered = text.strip().lower()
        if lowered in ("true", "t", "yes", "y", "on", "1"):
            return True
        if lowered in ("false", "f", "no", "n", "off", "0"):
            return False
        raise ValueError("not a boolean")
    if typ == "date":
        if not ISO_DATE.fullmatch(text):
            raise ValueError("not an ISO date")
        return dt.date.fromisoformat(text)
    if typ == "timestamp":
        if "T" not in text and "t" not in text and " " not in text:
            raise ValueError("not an ISO timestamp")
        value = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    raise ValueError("unknown type")


def render(value, typ, null_literal):
    if value is None:
        return null_literal
    if typ == "bool":
        return "true" if value else "false"
    if typ == "date":
        return value.isoformat()
    if typ == "timestamp":
        return value.isoformat(timespec="auto").replace("+00:00", "Z")
    return str(value)


def possible_type(text):
    for typ in ("timestamp", "date", "bool", "int", "float"):
        try:
            parse_value(text, typ)
            return typ
        except (ValueError, OverflowError):
            pass
    return "string"


def read_header(path, dialect):
    with open(path, "r", encoding="utf-8", newline="") as f:
        row = next(csv.reader(f, **dialect), None)
        if row is None:
            raise ValueError(f"{path}: missing header row")
        return row


def infer_schema(paths, dialect, mode):
    headers = [read_header(path, dialect) for path in paths]
    names = sorted({name for header in headers for name in header})
    observations = {name: [] for name in names}
    for path, header in zip(paths, headers):
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f, fieldnames=header, **dialect)
            next(reader, None)
            local = {name: [] for name in header}
            for row in reader:
                for name in header:
                    if row.get(name, "") != "":
                        local[name].append(row[name])
            for name in header:
                if mode == "strict" and local[name]:
                    kinds = {possible_type(v) for v in local[name]}
                    observations[name].append(next(iter(kinds)) if len(kinds) == 1 else "string")
                elif mode == "strict":
                    observations[name].append(None)
                elif local[name]:
                    observations[name].extend(local[name])
    result = []
    for name in names:
        values = observations[name]
        if mode == "strict":
            kinds = {v for v in values if v is not None}
            typ = next(iter(kinds)) if len(kinds) == 1 else "string"
        else:
            nonempty = values
            if not nonempty:
                result.append((name, "string"))
                continue
            candidates = sorted(TYPES[1:], key=lambda x: -PRIORITY[x])
            typ = "string"
            for candidate in candidates:
                try:
                    for value in nonempty:
                        parse_value(value, candidate)
                    typ = candidate
                    break
                except (ValueError, OverflowError):
                    continue
        result.append((name, typ))
    return result


class SortKey:
    def __init__(self, record, desc):
        self.record, self.desc = record, desc
    def __lt__(self, other):
        return compare_records(self.record, other.record, self.desc) < 0


def compare_records(a, b, desc):
    ka, kb = a[0], b[0]
    for x, y in zip(ka, kb):
        if x is None and y is None: continue
        if x is None: return 1 if desc else -1
        if y is None: return -1 if desc else 1
        if x != y:
            return (-1 if x < y else 1) * (-1 if desc else 1)
    return (a[1] > b[1]) - (a[1] < b[1])


def load_schema(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    columns = data.get("columns")
    if not isinstance(columns, list) or not columns:
        raise ValueError("schema must contain a non-empty columns array")
    result = []
    seen = set()
    for col in columns:
        if not isinstance(col, dict) or not isinstance(col.get("name"), str) or col.get("type") not in TYPES:
            raise ValueError("invalid schema column")
        if col["name"] in seen: raise ValueError("duplicate schema column")
        seen.add(col["name"]); result.append((col["name"], col["type"]))
    return result


def write_chunk(path, records):
    with open(path, "wb") as out:
        for record in records:
            pickle.dump(record, out, protocol=4)


def chunk_records(path):
    with open(path, "rb") as source:
        while True:
            try:
                yield pickle.load(source)
            except EOFError:
                return


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--desc", action="store_true")
    ap.add_argument("--schema")
    ap.add_argument("--infer", choices=("strict", "loose"), default="strict")
    ap.add_argument("--on-type-error", choices=("coerce-null", "fail", "keep-string"), default="coerce-null")
    ap.add_argument("--memory-limit-mb", type=int, default=64)
    ap.add_argument("--temp-dir")
    ap.add_argument("--csv-quotechar", default='"')
    ap.add_argument("--csv-escapechar")
    ap.add_argument("--csv-null-literal", default="")
    ap.add_argument("inputs", nargs="+")
    args = ap.parse_args(argv)
    if args.memory_limit_mb <= 0 or len(args.csv_quotechar) != 1 or (args.csv_escapechar and len(args.csv_escapechar) != 1):
        ap.error("invalid memory limit or CSV character")
    dialect = {"delimiter": ",", "quotechar": args.csv_quotechar}
    if args.csv_escapechar:
        dialect.update(escapechar=args.csv_escapechar, doublequote=False)
    try:
        schema = load_schema(args.schema) if args.schema else infer_schema(args.inputs, dialect, args.infer)
        names = [x[0] for x in schema]
        key_names = [x for x in args.key.split(",") if x]
        if not key_names or any(x not in names for x in key_names):
            raise ValueError("every sort key must be present in the resolved schema")
        type_by_name = dict(schema)
        key_indices = [names.index(x) for x in key_names]
        temp_context = tempfile.TemporaryDirectory(dir=args.temp_dir)
        chunks = []
        sequence = 0
        budget = max(1024 * 1024, args.memory_limit_mb * 1024 * 1024 // 2)
        try:
            records = []
            estimate = 0
            for path in args.inputs:
                with open(path, "r", encoding="utf-8", newline="") as f:
                    reader = csv.DictReader(f, **dialect)
                    header = reader.fieldnames or []
                    for row_number, row in enumerate(reader, 2):
                        fields = []
                        for name, typ in schema:
                            raw = row.get(name, "")
                            try: value = parse_value(raw, typ)
                            except (ValueError, OverflowError) as exc:
                                if args.on_type_error == "fail":
                                    raise ValueError(f"{path}:{row_number}:{name}: {exc}")
                                value = raw if args.on_type_error == "keep-string" else None
                            fields.append(render(value, typ, args.csv_null_literal))
                        typed = []
                        for i, (name, typ) in enumerate(schema):
                            try: typed.append(parse_value(row.get(name, ""), typ))
                            except (ValueError, OverflowError): typed.append(row.get(name, "") if args.on_type_error == "keep-string" else None)
                        record = (tuple(typed[i] for i in key_indices), sequence, fields)
                        records.append(record); sequence += 1
                        estimate += sum(len(x) for x in fields) + 128
                        if estimate >= budget:
                            records.sort(key=functools.cmp_to_key(lambda a,b: compare_records(a,b,args.desc)))
                            fn = os.path.join(temp_context.name, f"chunk-{len(chunks)}.bin")
                            write_chunk(fn, records)
                            chunks.append(fn); records = []; estimate = 0
            if records:
                records.sort(key=functools.cmp_to_key(lambda a,b: compare_records(a,b,args.desc)))
                fn = os.path.join(temp_context.name, f"chunk-{len(chunks)}.bin")
                write_chunk(fn, records)
                chunks.append(fn)
            readers = [chunk_records(fn) for fn in chunks]
            heap = []
            for idx, it in enumerate(readers):
                try: rec = next(it); heapq.heappush(heap, (SortKey(rec, args.desc), idx, rec))
                except StopIteration: pass
            output = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8", newline="")
            try:
                writer = csv.writer(output, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
                writer.writerow(names)
                while heap:
                    _, idx, rec = heapq.heappop(heap); writer.writerow(rec[2])
                    try:
                        nxt = next(readers[idx]); heapq.heappush(heap, (SortKey(nxt, args.desc), idx, nxt))
                    except StopIteration: pass
            finally:
                if output is not sys.stdout: output.close()
        finally:
            temp_context.cleanup()
    except (OSError, ValueError, json.JSONDecodeError, csv.Error) as exc:
        print(f"merge_files.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

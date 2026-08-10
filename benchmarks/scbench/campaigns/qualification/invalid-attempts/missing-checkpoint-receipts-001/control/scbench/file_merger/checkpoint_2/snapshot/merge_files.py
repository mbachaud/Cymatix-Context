#!/usr/bin/env python3
"""Merge heterogeneous tabular files into one sorted CSV."""
import argparse
import csv
import datetime as dt
import functools
import gzip
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
INT_MIN, INT_MAX = -(1 << 63), (1 << 63) - 1

class FormatError(ValueError): pass
class NestedError(ValueError): pass

def parse_bool(value):
    v = str(value).strip().lower()
    if v not in BOOLS: raise ValueError("invalid boolean")
    return BOOLS[v]

def parse_date(value):
    s = str(value).strip()
    if len(s) != 10 or s[4] != "-" or s[7] != "-": raise ValueError("invalid date")
    return dt.date.fromisoformat(s)

def parse_timestamp(value):
    s = str(value).strip()
    if s.endswith(("Z", "z")): s = s[:-1] + "+00:00"
    if "T" not in s and "t" not in s and " " not in s: raise ValueError("timestamp requires a time")
    x = dt.datetime.fromisoformat(s)
    if x.tzinfo is None: x = x.replace(tzinfo=dt.timezone.utc)
    return x.astimezone(dt.timezone.utc)

def classify(value):
    if isinstance(value, bool): return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int" if INT_MIN <= value <= INT_MAX else "float"
    if isinstance(value, float): return "float"
    for typ, fn in (("timestamp", parse_timestamp), ("date", parse_date), ("bool", parse_bool)):
        try: fn(value); return typ
        except (ValueError, TypeError, OverflowError): pass
    try:
        integer = int(str(value).strip())
        return "int" if INT_MIN <= integer <= INT_MAX else "float"
    except (ValueError, TypeError): pass
    try: float(str(value).strip()); return "float"
    except (ValueError, TypeError): return "string"

def compatible(a, b):
    if a == b: return a
    if {a, b} == {"int", "float"}: return "float"
    return "string"

def cast(value, typ):
    if value is None: return None
    if typ == "string": return str(value)
    if typ == "int":
        if isinstance(value, bool): raise ValueError("invalid integer")
        result = int(str(value).strip())
        if not INT_MIN <= result <= INT_MAX: raise ValueError("integer out of range")
        return result
    if typ == "float": return float(value) if not isinstance(value, str) else float(value.strip())
    if typ == "bool": return parse_bool(value)
    if typ == "date": return parse_date(value)
    if typ == "timestamp": return parse_timestamp(value)
    raise ValueError("unknown type")

def can_cast(value, typ):
    try: cast(value, typ); return True
    except (ValueError, TypeError, OverflowError): return False

def output_value(value, typ):
    if typ == "bool": return "true" if value else "false"
    if typ == "date": return value.isoformat()
    if typ == "timestamp": return value.isoformat(timespec="microseconds").replace("+00:00", "Z").replace(".000000Z", "Z")
    return str(value)

def key_value(value, typ):
    if value is None: return None
    if typ == "date": return value.toordinal()
    if typ == "timestamp": return value.timestamp()
    if typ == "bool": return int(value)
    return value

def compare_records(a, b, desc):
    for x, y in zip(a[0], b[0]):
        if x is None or y is None:
            result = 0 if x is None and y is None else (-1 if x is None else 1)
        else:
            try: result = (x > y) - (x < y)
            except TypeError:
                lx, ly = (type(x).__name__, str(x)), (type(y).__name__, str(y))
                result = (lx > ly) - (lx < ly)
        if result: return -result if desc else result
    return (a[1] > b[1]) - (a[1] < b[1])

class HeapItem:
    def __init__(self, record, run, desc): self.record, self.run, self.desc = record, run, desc
    def __lt__(self, other): return compare_records(self.record, other.record, self.desc) < 0

def compression_for(path, requested):
    is_gz = path.lower().endswith(".gz")
    if requested == "gzip" and not is_gz: raise FormatError(f"compression mismatch: {path}")
    if requested == "none" and is_gz: raise FormatError(f"compression mismatch: {path}")
    return requested == "gzip" or (requested == "auto" and is_gz)

def detect_format(path, requested):
    base = path[:-3] if path.lower().endswith(".gz") else path
    ext = Path(base).suffix.lower()
    by_ext = {".csv":"csv", ".tsv":"tsv", ".jsonl":"jsonl", ".ndjson":"jsonl", ".parquet":"parquet"}
    if requested != "auto": return requested
    if ext in by_ext: return by_ext[ext]
    try:
        with open(path, "rb") as f: magic = f.read(4)
    except OSError: raise
    if magic == b"PAR1": return "parquet"
    raise FormatError(f"cannot detect input format: {path}")

def text_rows(path, fmt, args):
    gz = compression_for(path, args.compression)
    opener = gzip.open if gz else open
    try: f = opener(path, "rt", encoding="utf-8", newline="")
    except (OSError, UnicodeError) as e: raise FormatError(str(e))
    try:
        if fmt in ("csv", "tsv"):
            kwargs = dict(delimiter="," if fmt == "csv" else "\t", lineterminator="\n", strict=True)
            if fmt == "csv": kwargs.update(quotechar=args.csv_quotechar, escapechar=args.csv_escapechar, doublequote=args.csv_escapechar is None)
            else: kwargs.update(quoting=csv.QUOTE_NONE)
            reader = csv.reader(f, **kwargs)
            try: header = next(reader)
            except StopIteration: raise FormatError(f"empty input: {path}")
            if not header or len(set(header)) != len(header): raise FormatError(f"invalid header: {path}")
            yield header, None
            for line, row in enumerate(reader, 2):
                if len(row) != len(header): raise FormatError(f"{path}:{line}: wrong number of fields")
                yield dict(zip(header, row)), line
        else:
            header = None
            for line, raw in enumerate(f, 1):
                if not raw.strip(): continue
                try: obj = json.loads(raw)
                except json.JSONDecodeError as e: raise FormatError(f"{path}:{line}: {e.msg}")
                if not isinstance(obj, dict) or any(isinstance(v, (dict, list)) for v in obj.values()): raise NestedError(f"{path}:{line}: nested JSON value")
                if header is None: header = list(obj); yield header, None
                yield obj, line
            if header is None: raise FormatError(f"empty input: {path}")
    except (csv.Error, UnicodeError) as e: raise FormatError(f"{path}: {e}")
    finally: f.close()

def parquet_rows(path, args):
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError: raise FormatError("Parquet support requires pyarrow")
    gz = compression_for(path, args.compression)
    source = path
    tmp = None
    if gz:
        fd, tmp = tempfile.mkstemp(suffix=".parquet", dir=args.temp_dir)
        with os.fdopen(fd, "wb") as out, gzip.open(path, "rb") as inp:
            while True:
                block = inp.read(1024 * 1024)
                if not block: break
                out.write(block)
        source = tmp
    try:
        pf = pq.ParquetFile(source)
        schema = pf.schema_arrow
        if any(pa.types.is_nested(f.type) or pa.types.is_list(f.type) or pa.types.is_struct(f.type) or pa.types.is_map(f.type) for f in schema):
            raise NestedError(f"{path}: nested Parquet field")
        header = schema.names
        yield header, None
        # Read one physical row group at a time.  This keeps peak memory tied to
        # the source's row-group size rather than to the complete file.
        for group in range(pf.num_row_groups):
            table = pf.read_row_group(group)
            for row in table.to_pylist(): yield row, None
    except NestedError:
        raise
    except (OSError, ValueError, pa.ArrowException) as e: raise FormatError(f"{path}: {e}")
    finally:
        if tmp:
            try: os.unlink(tmp)
            except OSError: pass

def rows_for(path, args):
    fmt = detect_format(path, args.input_format)
    if fmt == "parquet": return parquet_rows(path, args)
    return text_rows(path, fmt, args)

def load_schema(path):
    with open(path, encoding="utf-8") as f: data = json.load(f)
    columns = data.get("columns") if isinstance(data, dict) else None
    if not isinstance(columns, list): raise ValueError("schema must contain a columns array")
    result, seen = [], set()
    for item in columns:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or item.get("type") not in TYPES: raise ValueError("each schema column needs a name and type")
        if item["name"] in seen: raise ValueError("invalid or duplicate schema column")
        seen.add(item["name"]); result.append((item["name"], item["type"]))
    return result

def inspect_inputs(paths, args):
    names, file_states = set(), []
    for path in paths:
        states = {}
        for header, _ in rows_for(path, args):
            names.update(header)
            if not states: states = {n: set() for n in header}
            else:
                for n in header: states.setdefault(n, set())
            if isinstance(header, list): continue
            for name, value in header.items():
                if value is None or value == "": continue
                states[name].add(classify(value))
        file_states.append(states)
    result = []
    for name in sorted(names):
        observations = [s[name] for s in file_states if s.get(name)]
        if args.infer == "loose":
            possible = set(PRIORITY)
            for obs in observations:
                possible &= {t for t in possible if all(can_cast(_sample_for_type({kind}, t), t) for kind in obs)}
            typ = next((t for t in PRIORITY if t in possible), "string")
        elif args.schema_strategy == "consensus":
            votes = [_file_type(obs) for obs in observations]
            typ = max(set(votes), key=lambda t: (votes.count(t), -PRIORITY.index(t))) if votes else "string"
        elif args.schema_strategy == "union":
            typ = next((t for t in PRIORITY if all(all(can_cast(_sample_for_type({kind}, t), t) for kind in obs) for obs in observations)), "string")
        else:
            typ = next((t for t in PRIORITY if any(t in obs for obs in observations)), "string")
            for obs in observations:
                for t in obs: typ = compatible(typ, t)
        result.append((name, typ))
    return result

def _sample_for_type(obs, typ):
    # Loose inference is based on the set of classes observed; this representative
    # keeps the compatibility check deterministic without retaining all input rows.
    return {"timestamp":"2000-01-01T00:00:00Z", "date":"2000-01-01", "bool":"true", "int":"1", "float":"1.5", "string":"x"}[next((x for x in PRIORITY if x in obs), "string")]

def _file_type(observed):
    """Resolve the type supported by one file before cross-file voting."""
    typ = next((t for t in PRIORITY if t in observed), "string")
    for other in observed:
        typ = compatible(typ, other)
    return typ

def make_runs(paths, schema, keys, args, temp):
    runs, rows, used, seq = [], [], 0, 0
    limit = max(1, args.memory_limit_mb or 64) * 1024 * 1024 // 3
    def flush():
        nonlocal rows, used
        if not rows: return
        rows.sort(key=functools.cmp_to_key(lambda a,b: compare_records(a,b,args.desc)))
        fd, name = tempfile.mkstemp(prefix="merge-", suffix=".run", dir=temp)
        with os.fdopen(fd, "wb") as f:
            for row in rows: pickle.dump(row, f, protocol=pickle.HIGHEST_PROTOCOL)
        runs.append(name); rows, used = [], 0
    for path in paths:
        for header, line in rows_for(path, args):
            if isinstance(header, list): positions = {n:i for i,n in enumerate(header)}; continue
            values, keyvals = [], []
            for name, typ in schema:
                raw = header.get(name) if isinstance(header, dict) else None
                if raw is None or raw == "": value, rendered = None, args.csv_null_literal
                else:
                    try: value, rendered = cast(raw, typ), None; rendered = output_value(value, typ)
                    except (ValueError, TypeError, OverflowError) as e:
                        if args.on_type_error == "fail": raise ValueError(f"{path}:{line}:{name}: {e}")
                        value, rendered = (None, args.csv_null_literal) if args.on_type_error == "coerce-null" else (str(raw), str(raw))
                values.append(rendered)
                if name in keys: keyvals.append(key_value(value, typ))
            rows.append((tuple(keyvals), seq, values)); seq += 1
            used += sum(len(x) for x in values) + 128
            if used >= limit: flush()
    flush(); return runs

def merge_runs(runs, out, schema, args):
    files, heap = [], []
    try:
        for n, path in enumerate(runs):
            f = open(path, "rb"); files.append(f)
            try: heapq.heappush(heap, HeapItem(pickle.load(f), n, args.desc))
            except EOFError: pass
        writer = csv.writer(out, delimiter=",", quotechar=args.csv_quotechar, escapechar=args.csv_escapechar, doublequote=args.csv_escapechar is None, lineterminator="\n")
        writer.writerow([n for n,_ in schema])
        while heap:
            item = heapq.heappop(heap); writer.writerow(item.record[2])
            try: heapq.heappush(heap, HeapItem(pickle.load(files[item.run]), item.run, args.desc))
            except EOFError: pass
    finally:
        for f in files: f.close()
        for path in runs:
            try: os.unlink(path)
            except OSError: pass

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True); p.add_argument("--key", required=True); p.add_argument("--desc", action="store_true")
    p.add_argument("--schema"); p.add_argument("--infer", choices=("strict","loose"), default="strict")
    p.add_argument("--schema-strategy", choices=("authoritative","consensus","union"), default="authoritative")
    p.add_argument("--on-type-error", choices=("coerce-null","fail","keep-string"), default="coerce-null")
    p.add_argument("--memory-limit-mb", type=int); p.add_argument("--temp-dir"); p.add_argument("--csv-quotechar", default='"'); p.add_argument("--csv-escapechar"); p.add_argument("--csv-null-literal", default="")
    p.add_argument("--input-format", choices=("auto","csv","tsv","jsonl","parquet"), default="auto"); p.add_argument("--compression", choices=("auto","none","gzip"), default="auto"); p.add_argument("--parquet-row-group-bytes", type=int)
    p.add_argument("inputs", nargs="+"); args = p.parse_args(argv)
    if len(args.csv_quotechar) != 1 or (args.csv_escapechar is not None and len(args.csv_escapechar) != 1): p.error("quote and escape characters must each be one character")
    if args.memory_limit_mb is not None and args.memory_limit_mb < 1: p.error("memory limit must be positive")
    if args.parquet_row_group_bytes is not None and args.parquet_row_group_bytes < 1: p.error("row group bytes must be positive")
    keys = [x.strip() for x in args.key.split(",") if x.strip()]
    if not keys: p.error("at least one key is required")
    try:
        schema = load_schema(args.schema) if args.schema else inspect_inputs(args.inputs, args)
        if any(k not in dict(schema) for k in keys): return _error(3, "key column is not present in resolved schema")
        with tempfile.TemporaryDirectory(dir=args.temp_dir) as temp:
            runs = make_runs(args.inputs, schema, set(keys), args, temp)
            if args.output == "-": merge_runs(runs, sys.stdout, schema, args)
            else:
                target = Path(args.output); fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent or Path('.')))
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="") as out: merge_runs(runs, out, schema, args)
                    os.replace(tmp, target)
                except Exception:
                    try: os.unlink(tmp)
                    except OSError: pass
                    raise
    except NestedError as e: return _error(6, e)
    except FormatError as e: return _error(5 if "detect input format" not in str(e) else 2, e)
    except (OSError, csv.Error, json.JSONDecodeError, ValueError, StopIteration) as e: return _error(1, e)
    return 0

def _error(code, message):
    print(f"merge_files.py: {message}", file=sys.stderr); return code

if __name__ == "__main__": raise SystemExit(main())

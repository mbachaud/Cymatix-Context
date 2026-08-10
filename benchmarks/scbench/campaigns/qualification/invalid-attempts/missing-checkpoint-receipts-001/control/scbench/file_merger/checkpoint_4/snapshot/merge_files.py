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
import shutil
import io
from pathlib import Path

TYPES = {"string", "int", "float", "bool", "date", "timestamp"}
PRIORITY = ["timestamp", "date", "bool", "int", "float", "string"]
BOOLS = {"true": True, "false": False, "yes": True, "no": False,
         "t": True, "f": False, "y": True, "n": False, "1": True, "0": False}
INT_MIN, INT_MAX = -(1 << 63), (1 << 63) - 1

class FormatError(ValueError): pass
class NestedError(ValueError): pass
class SchemaError(ValueError): pass
class KeyErrorPath(ValueError): pass
class CastFailure(ValueError):
    def __init__(self, value, typ, path):
        self.value, self.typ, self.path = value, typ, path
        super().__init__(f'cannot cast "{value}" to {type_name(typ)} in field "{path}"')

def type_name(typ):
    if isinstance(typ, str): return typ
    if typ.get("json"): return "json"
    if "array" in typ: return f"array<{type_name(typ['array'])}>"
    if "map" in typ: return f"map<string,{type_name(typ['map'])}>"
    return "struct"

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
    if isinstance(typ, dict):
        return json.dumps(to_json_value(value, typ), ensure_ascii=False, separators=(",", ":"))
    if typ == "bool": return "true" if value else "false"
    if typ == "date": return value.isoformat()
    if typ == "timestamp": return value.isoformat(timespec="microseconds").replace("+00:00", "Z").replace(".000000Z", "Z")
    return str(value)

def to_json_value(value, typ):
    if value is None: return None
    if isinstance(typ, str):
        if typ == "bool": return value if isinstance(value, bool) else value
        if typ == "date": return value.isoformat() if isinstance(value, dt.date) else value
        if typ == "timestamp": return output_value(value, typ) if isinstance(value, dt.datetime) else value
        return value
    if typ.get("json"):
        if isinstance(value, dict): return {k: to_json_value(value[k], {"json": True}) for k in sorted(value)}
        if isinstance(value, list): return [to_json_value(x, {"json": True}) for x in value]
        return value
    if "array" in typ:
        return [to_json_value(x, typ["array"]) for x in value] if isinstance(value, list) else value
    if "map" in typ:
        return {k: to_json_value(value[k], typ["map"]) for k in sorted(value)} if isinstance(value, dict) else value
    if not isinstance(value, dict): return value
    return {name: to_json_value(value.get(name), child) for name, child in typ["struct"]}

def parse_type(value, aliases, stack=()):
    if isinstance(value, str):
        raw = value.strip(); low = raw.lower()
        if low in aliases:
            if low in stack: raise SchemaError("type alias cycle: " + " -> ".join(stack + (low,)))
            return parse_type(aliases[low], aliases, stack + (low,))
        if low in TYPES: return low
        if low == "json": return {"json": True}
        for prefix, kind in (("array<", "array"), ("list<", "array"), ("map<", "map")):
            if low.startswith(prefix) and low.endswith(">"):
                inner = raw[len(prefix):-1]
                if kind == "map":
                    comma = inner.find(",")
                    if comma < 0 or inner[:comma].strip().lower() != "string": raise SchemaError("maps require string keys")
                    return {"map": parse_type(inner[comma+1:], aliases, stack)}
                return {"array": parse_type(inner, aliases, stack)}
        raise SchemaError(f"unknown type: {raw}")
    if isinstance(value, dict):
        if set(value) == {"struct"}:
            fields = value["struct"].get("fields") if isinstance(value["struct"], dict) else None
            if not isinstance(fields, list): raise SchemaError("struct needs a fields array")
            result, seen = [], set()
            for field in fields:
                if not isinstance(field, dict) or not isinstance(field.get("name"), str) or field["name"] in seen:
                    raise SchemaError("invalid or duplicate struct field")
                seen.add(field["name"]); result.append((field["name"], parse_type(field.get("type"), aliases)))
            return {"struct": result}
        if set(value) == {"array"} and isinstance(value["array"], dict) and "element" in value["array"]:
            return {"array": parse_type(value["array"]["element"], aliases)}
        if set(value) == {"map"} and isinstance(value["map"], dict) and "value" in value["map"]:
            if str(value["map"].get("key", "")).lower() != "string": raise SchemaError("maps require string keys")
            return {"map": parse_type(value["map"]["value"], aliases)}
    raise SchemaError("invalid type declaration")

def cast_nested(value, typ, path, mode):
    if value is None: return None
    if isinstance(typ, str):
        if isinstance(value, (dict, list)):
            if mode == "coerce-null": return None
            if mode == "keep-string": return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            raise CastFailure(value, typ, path)
        try: return cast(value, typ)
        except (ValueError, TypeError, OverflowError):
            if mode == "coerce-null": return None
            if mode == "keep-string": return value if isinstance(value, str) else str(value)
            raise CastFailure(value, typ, path)
    if typ.get("json"): return value
    if "struct" in typ:
        if not isinstance(value, dict):
            if mode == "coerce-null": return None
            if mode == "keep-string": return value if isinstance(value, str) else str(value)
            raise CastFailure(value, typ, path)
        return {name: cast_nested(value.get(name), child, f"{path}.{name}" if path else name, mode)
                for name, child in typ["struct"]}
    if "array" in typ:
        if not isinstance(value, list):
            if mode == "coerce-null": return None
            if mode == "keep-string": return value if isinstance(value, str) else str(value)
            raise CastFailure(value, typ, path)
        return [cast_nested(x, typ["array"], f"{path}.{i}", mode) for i, x in enumerate(value)]
    if not isinstance(value, dict):
        if mode == "coerce-null": return None
        if mode == "keep-string": return value if isinstance(value, str) else str(value)
        raise CastFailure(value, typ, path)
    return {str(k): cast_nested(v, typ["map"], f'{path}["{k}"]', mode) for k, v in value.items()}

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
                if not isinstance(obj, dict): raise FormatError(f"{path}:{line}: JSON object required")
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
        header = [field.name for field in schema]
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

BUILTIN_ALIASES = {"integer":"int", "long":"int", "double":"float", "number":"float",
                   "boolean":"bool", "datetime":"timestamp", "timestamptz":"timestamp",
                   "text":"string", "varchar":"string"}

def load_aliases(path):
    aliases = dict(BUILTIN_ALIASES)
    if path:
        with open(path, encoding="utf-8") as f: data = json.load(f)
        extra = data.get("aliases") if isinstance(data, dict) else None
        if not isinstance(extra, dict): raise SchemaError("alias file must contain an aliases object")
        aliases.update({str(k).lower(): v for k, v in extra.items()})
    # Resolve every alias now, making cycles and unknown aliases deterministic.
    for name in list(aliases): parse_type(name, aliases)
    return aliases

def load_schema(path, alias_path=None):
    with open(path, encoding="utf-8") as f: data = json.load(f)
    columns = data.get("columns") if isinstance(data, dict) else None
    if not isinstance(columns, list): raise SchemaError("schema must contain a columns array")
    aliases = load_aliases(alias_path)
    result, seen = [], set()
    for item in columns:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str): raise SchemaError("each schema column needs a name and type")
        if item["name"] in seen: raise SchemaError("invalid or duplicate schema column")
        seen.add(item["name"]); result.append((item["name"], parse_type(item.get("type"), aliases)))
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
                if isinstance(value, (dict, list)):
                    raise NestedError("nested structure requires provided --schema")
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

def parse_path(path):
    import re
    tokens = []
    pos = 0
    while pos < len(path):
        if path[pos] == '.': pos += 1; continue
        if path[pos] == '[':
            end = path.find(']', pos)
            if end < 0: raise KeyErrorPath(f"invalid field path: {path}")
            inside = path[pos+1:end]
            if len(inside) >= 2 and inside[0] == '"' and inside[-1] == '"': tokens.append(("map", inside[1:-1]))
            elif inside.isdigit(): tokens.append(("index", int(inside)))
            else: raise KeyErrorPath(f"invalid field path: {path}")
            pos = end + 1; continue
        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+", path[pos:])
        if not match: raise KeyErrorPath(f"invalid field path: {path}")
        token = match.group(0); tokens.append(("index" if token.isdigit() else "field", int(token) if token.isdigit() else token)); pos += len(token)
    if not tokens: raise KeyErrorPath(f"invalid field path: {path}")
    return tokens

def resolve_path(schema_map, path):
    typ = None
    tokens = parse_path(path)
    for kind, token in tokens:
        if typ is None:
            if kind != "field" or token not in schema_map: raise KeyErrorPath(f'key column "{path}" is not present')
            typ = schema_map[token]
        elif isinstance(typ, dict) and "struct" in typ and kind == "field":
            fields = dict(typ["struct"])
            if token not in fields: raise KeyErrorPath(f'key column "{path}" is not present')
            typ = fields[token]
        elif isinstance(typ, dict) and "array" in typ and kind == "index": typ = typ["array"]
        elif isinstance(typ, dict) and "map" in typ and kind == "map": typ = typ["map"]
        else: raise KeyErrorPath(f'key column "{path}" does not resolve to a primitive')
    if not isinstance(typ, str): raise KeyErrorPath(f'key column "{path}" does not resolve to a primitive')
    return typ

def extract_path(values, path):
    value = values
    for kind, token in parse_path(path):
        if value is None: return None
        if kind == "field":
            if not isinstance(value, dict): return None
            value = value.get(token)
        elif kind == "index":
            if not isinstance(value, list) or token >= len(value): return None
            value = value[token]
        else:
            if not isinstance(value, dict): return None
            value = value.get(token)
    return value

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

def make_runs(paths, schema, keys, partitions, args, temp):
    runs, rows, used, seq = [], [], 0, 0
    limit = max(1, args.memory_limit_mb or 64) * 1024 * 1024 // 3
    schema_map = dict(schema)
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
            values, casted, keyvals, partvals = [], {}, [], {}
            for name, typ in schema:
                raw = header.get(name) if isinstance(header, dict) else None
                if raw is None or raw == "": value, rendered = None, args.csv_null_literal
                else:
                    source = raw
                    if isinstance(typ, dict) and isinstance(raw, str):
                        try: source = json.loads(raw)
                        except json.JSONDecodeError:
                            if args.on_type_error == "fail": raise ValueError(f'ERR 4 cannot cast "{raw}" to {type_name(typ)} in field "{name}" (file={path} line={line})')
                            source = None if args.on_type_error == "coerce-null" else raw
                    try:
                        value = cast_nested(source, typ, name, args.on_type_error)
                        rendered = args.csv_null_literal if value is None else output_value(value, typ)
                    except CastFailure as e:
                        if isinstance(typ, str): raise ValueError(f"{path}:{line}:{name}: {e}")
                        raise ValueError(f'ERR 4 {e} (file={path} line={line})')
                    except (ValueError, TypeError, OverflowError) as e:
                        if args.on_type_error == "fail": raise ValueError(f'ERR 4 cannot cast "{raw}" to {type_name(typ)} in field "{name}" (file={path} line={line})')
                        value, rendered = (None, args.csv_null_literal) if args.on_type_error == "coerce-null" else (str(raw), str(raw))
                values.append(rendered)
                casted[name] = value
            for field_path in keys:
                typ = resolve_path(schema_map, field_path)
                keyvals.append(key_value(extract_path(casted, field_path), typ))
            for field_path in partitions:
                typ = resolve_path(schema_map, field_path)
                value = extract_path(casted, field_path)
                partvals[field_path] = (field_path, value, None if value is None else output_value(value, typ))
            rows.append((tuple(keyvals), seq, values,
                         tuple(partvals[name] for name in partitions))); seq += 1
            used += sum(len(x) for x in values) + 128
            if used >= limit: flush()
    flush(); return runs

def iter_runs(runs, args):
    files, heap = [], []
    try:
        for n, path in enumerate(runs):
            f = open(path, "rb"); files.append(f)
            try: heapq.heappush(heap, HeapItem(pickle.load(f), n, args.desc))
            except EOFError: pass
        while heap:
            item = heapq.heappop(heap)
            yield item.record
            try: heapq.heappush(heap, HeapItem(pickle.load(files[item.run]), item.run, args.desc))
            except EOFError: pass
    finally:
        for f in files: f.close()
        for path in runs:
            try: os.unlink(path)
            except OSError: pass

def csv_line(row, args):
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=",", quotechar=args.csv_quotechar,
                        escapechar=args.csv_escapechar,
                        doublequote=args.csv_escapechar is None,
                        lineterminator="\n")
    writer.writerow(row)
    return buf.getvalue()

def write_single(runs, out, schema, args):
    out.write(csv_line([n for n, _ in schema], args))
    for record in iter_runs(runs, args):
        out.write(csv_line(record[2], args))

def partition_component(value, rendered):
    if value is None:
        return "_null"
    # Percent-encode UTF-8 bytes, retaining only the explicitly permitted
    # ASCII characters.  This also makes slash and spaces unambiguous.
    raw = str(rendered if rendered is not None else value).encode("utf-8")
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
    return "".join(chr(b) if b in safe else f"%{b:02X}" for b in raw)

class ShardWriter:
    def __init__(self, root, schema, args, partition_names):
        self.root, self.schema, self.args = Path(root), schema, args
        self.partition_names = partition_names
        self.states = {}
        self.header = csv_line([n for n, _ in schema], args)
        self.header_bytes = len(self.header.encode("utf-8"))

    def _state(self, key):
        if key not in self.states:
            directory = self.root
            for name, value, rendered in key:
                directory /= f"{name}={partition_component(value, rendered)}"
            directory.mkdir(parents=True, exist_ok=True)
            self.states[key] = {"directory": directory, "part": 0,
                                "rows": 0, "bytes": 0}
        return self.states[key]

    def write(self, record):
        key = tuple(record[3]) if self.partition_names else ()
        state = self._state(key)
        line = csv_line(record[2], self.args)
        line_bytes = len(line.encode("utf-8"))
        max_rows = self.args.max_rows_per_file
        max_bytes = self.args.max_bytes_per_file
        if state["rows"] and ((max_rows is not None and state["rows"] >= max_rows) or
                               (max_bytes is not None and state["bytes"] + line_bytes > max_bytes)):
            state["part"] += 1
            state["rows"], state["bytes"] = 0, 0
        path = state["directory"] / f"part-{state['part']:05d}.csv"
        mode = "ab" if state["rows"] else "wb"
        with open(path, mode) as out:
            if not state["rows"]:
                out.write(self.header.encode("utf-8"))
                state["bytes"] = self.header_bytes
            out.write(line.encode("utf-8"))
        state["rows"] += 1
        state["bytes"] += line_bytes

def write_partitioned(runs, root, schema, args, partition_names):
    writer = ShardWriter(root, schema, args, partition_names)
    for record in iter_runs(runs, args):
        writer.write(record)

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True); p.add_argument("--key", required=True); p.add_argument("--desc", action="store_true")
    p.add_argument("--partition-by")
    p.add_argument("--type-alias-file")
    p.add_argument("--max-rows-per-file", type=int)
    p.add_argument("--max-bytes-per-file", type=int)
    p.add_argument("--schema"); p.add_argument("--infer", choices=("strict","loose"), default="strict")
    p.add_argument("--schema-strategy", choices=("authoritative","consensus","union"), default="authoritative")
    p.add_argument("--on-type-error", choices=("coerce-null","fail","keep-string"), default="coerce-null")
    p.add_argument("--memory-limit-mb", type=int); p.add_argument("--temp-dir"); p.add_argument("--csv-quotechar", default='"'); p.add_argument("--csv-escapechar"); p.add_argument("--csv-null-literal", default="")
    p.add_argument("--input-format", choices=("auto","csv","tsv","jsonl","parquet"), default="auto"); p.add_argument("--compression", choices=("auto","none","gzip"), default="auto"); p.add_argument("--parquet-row-group-bytes", type=int)
    p.add_argument("inputs", nargs="+"); args = p.parse_args(argv)
    if len(args.csv_quotechar) != 1 or (args.csv_escapechar is not None and len(args.csv_escapechar) != 1): p.error("quote and escape characters must each be one character")
    if args.memory_limit_mb is not None and args.memory_limit_mb < 1: p.error("memory limit must be positive")
    if args.parquet_row_group_bytes is not None and args.parquet_row_group_bytes < 1: p.error("row group bytes must be positive")
    if args.max_rows_per_file is not None and args.max_rows_per_file < 1: p.error("max rows per file must be positive")
    if args.max_bytes_per_file is not None and args.max_bytes_per_file < 1: p.error("max bytes per file must be positive")
    keys = [x.strip() for x in args.key.split(",") if x.strip()]
    if not keys: p.error("at least one key is required")
    partitions = ([x.strip() for x in args.partition_by.split(",") if x.strip()]
                  if args.partition_by is not None else [])
    if args.partition_by is not None and not partitions: p.error("at least one partition column is required")
    if len(set(partitions)) != len(partitions): p.error("partition columns must be unique")
    directory_mode = bool(partitions or args.max_rows_per_file is not None or args.max_bytes_per_file is not None)
    if directory_mode and args.output == "-": p.error("partitioned output requires a directory path")
    try:
        schema = load_schema(args.schema, args.type_alias_file) if args.schema else inspect_inputs(args.inputs, args)
        schema_names = dict(schema)
        for field_path in keys + partitions:
            try: resolve_path(schema_names, field_path)
            except KeyErrorPath as e: return _error(3, e)
        with tempfile.TemporaryDirectory(dir=args.temp_dir) as temp:
            runs = make_runs(args.inputs, schema, keys, partitions, args, temp)
            if not directory_mode and args.output == "-":
                write_single(runs, sys.stdout, schema, args)
            elif not directory_mode:
                target = Path(args.output); fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent or Path('.')))
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="") as out: write_single(runs, out, schema, args)
                    os.replace(tmp, target)
                except Exception:
                    try: os.unlink(tmp)
                    except OSError: pass
                    raise
            else:
                target = Path(args.output)
                target.parent.mkdir(parents=True, exist_ok=True)
                tmpdir = tempfile.mkdtemp(prefix=f".{target.name}.", dir=str(target.parent))
                try:
                    write_partitioned(runs, tmpdir, schema, args, partitions)
                    if target.exists():
                        # Keep replacement recoverable if the final rename
                        # fails; normal test/output paths are non-existent.
                        backup = tempfile.mkdtemp(prefix=f".{target.name}.old-", dir=str(target.parent))
                        os.rmdir(backup)
                        os.replace(target, backup)
                        try:
                            os.replace(tmpdir, target)
                        except Exception:
                            os.replace(backup, target)
                            raise
                        shutil.rmtree(backup)
                    else:
                        os.replace(tmpdir, target)
                    tmpdir = None
                finally:
                    if tmpdir is not None:
                        shutil.rmtree(tmpdir, ignore_errors=True)
    except NestedError as e: return _error(6, e)
    except SchemaError as e: return _error(2, e)
    except FormatError as e: return _error(5 if "detect input format" not in str(e) else 2, e)
    except (OSError, csv.Error, json.JSONDecodeError, ValueError, StopIteration) as e:
        message = str(e)
        return _error(4 if message.startswith("ERR 4 ") else 1, message)
    return 0

def _error(code, message):
    print(f"merge_files.py: {message}", file=sys.stderr); return code

if __name__ == "__main__": raise SystemExit(main())

#!/usr/bin/env python3
"""Merge CSV, TSV, JSONL and Parquet files into one sorted CSV."""
import argparse, csv, datetime as dt, functools, gzip, heapq, io, json, os, pickle, re, shutil, sqlite3, sys, tempfile

TYPES = ("string", "int", "float", "bool", "date", "timestamp")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RANK = {"string": 0, "bool": 1, "int": 2, "float": 3, "date": 4, "timestamp": 5}
BUILTIN_ALIASES = {"integer":"int", "long":"int", "double":"float", "number":"float",
                   "boolean":"bool", "datetime":"timestamp", "timestamptz":"timestamp",
                   "text":"string", "varchar":"string"}

class MergeError(Exception):
    def __init__(self, message, code=1):
        self.code, self.message = code, message

class InputRow(dict):
    def __init__(self, *args, line=None, **kwargs):
        super().__init__(*args, **kwargs); self.line=line

class CastError(ValueError):
    def __init__(self, raw, typ, path): self.raw,self.typ,self.path=raw,typ,path

def parse_value(value, typ):
    if value is None or value == "": return None
    if typ == "string": return value if isinstance(value, str) else str(value)
    if typ == "int":
        if isinstance(value, bool): raise ValueError("not an integer")
        if isinstance(value, int): return value
        if isinstance(value, float) and value.is_integer(): return int(value)
        if not isinstance(value, str) or not re.fullmatch(r"[+-]?\d+", value): raise ValueError("not an integer")
        return int(value)
    if typ == "float":
        x = float(value)
        if x != x or x in (float("inf"), float("-inf")): raise ValueError("non-finite float")
        return x
    if typ == "bool":
        if isinstance(value, bool): return value
        s = str(value).strip().lower()
        if s in ("true","t","yes","y","on","1"): return True
        if s in ("false","f","no","n","off","0"): return False
        raise ValueError("not a boolean")
    if typ == "date":
        if isinstance(value, dt.datetime): return value.date()
        if isinstance(value, dt.date): return value
        if not isinstance(value, str) or not ISO_DATE.fullmatch(value): raise ValueError("not an ISO date")
        return dt.date.fromisoformat(value)
    if typ == "timestamp":
        if isinstance(value, dt.datetime): x = value
        else:
            s = str(value); s = s[:-1] + "+00:00" if s.endswith(("Z","z")) else s
            if "T" not in s and "t" not in s and " " not in s: raise ValueError("not an ISO timestamp")
            x = dt.datetime.fromisoformat(s)
        return (x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)
    raise ValueError("unknown type")

def type_kind(t):
    return t if isinstance(t, str) else t["kind"]

def is_primitive(t):
    return isinstance(t, str) and t in TYPES

def type_label(t):
    if isinstance(t, str): return t
    k = t["kind"]
    if k == "array": return "array<%s>" % type_label(t["element"])
    if k == "map": return "map<string,%s>" % type_label(t["value"])
    if k == "json": return "json"
    return "struct"

def alias_resolver(alias_data):
    aliases = dict(BUILTIN_ALIASES)
    if alias_data:
        try:
            with open(alias_data, encoding="utf-8") as f: data = json.load(f)
        except (OSError, json.JSONDecodeError) as e: raise MergeError(str(e), 2)
        if not isinstance(data, dict) or not isinstance(data.get("aliases"), dict): raise MergeError("invalid type alias file", 2)
        aliases.update({str(k).lower(): str(v).lower() for k,v in data["aliases"].items()})
    resolving = set(); done = {}
    def resolve_name(name):
        name = name.lower()
        if name in done: return done[name]
        if name in resolving: raise MergeError("type alias cycle", 2)
        resolving.add(name)
        target = aliases.get(name, name)
        # Container aliases may be expressed with whitespace and nested syntax.
        out = parse_type(target, resolve_name)
        resolving.remove(name); done[name] = out
        return out
    def parse_type(spec, resolve=resolve_name):
        if isinstance(spec, str):
            s = spec.strip().lower()
            if s in TYPES: return s
            if s == "json": return {"kind":"json"}
            m = re.fullmatch(r"list\s*<(.+)>", s)
            if m: return {"kind":"array", "element":parse_type(m.group(1))}
            if s in aliases:
                return resolve_name(s)
            raise MergeError("unknown type: " + spec, 2)
        if not isinstance(spec, dict) or len(spec) != 1: raise MergeError("invalid schema type", 2)
        if "struct" in spec:
            x=spec["struct"]; fs=x.get("fields") if isinstance(x,dict) else None
            if not isinstance(fs,list): raise MergeError("invalid struct type",2)
            seen=set(); fields=[]
            for f in fs:
                if not isinstance(f,dict) or not isinstance(f.get("name"),str) or f["name"] in seen: raise MergeError("invalid struct type",2)
                seen.add(f["name"]); fields.append((f["name"],parse_type(f.get("type"))))
            return {"kind":"struct","fields":fields}
        if "array" in spec:
            x=spec["array"]; e=x.get("element") if isinstance(x,dict) else None
            return {"kind":"array","element":parse_type(e)}
        if "map" in spec:
            x=spec["map"]
            if not isinstance(x,dict) or str(x.get("key")).lower() != "string": raise MergeError("map keys must be string",2)
            return {"kind":"map","value":parse_type(x.get("value"))}
        raise MergeError("invalid schema type",2)
    for _alias in list(aliases): resolve_name(_alias)
    return parse_type, resolve_name

def kind(value):
    if value is None or value == "": return None
    if isinstance(value, bool): return "bool"
    if isinstance(value, int): return "int"
    if isinstance(value, float): return "float"
    for t in ("int", "float", "bool", "timestamp", "date"):
        try: parse_value(value, t); return t
        except (ValueError, TypeError, OverflowError): pass
    return "string"

def observed_kind(value, fmt):
    if fmt in ("jsonl", "parquet") and isinstance(value, str): return "string"
    return kind(value)

def contains_nested(value):
    return isinstance(value, (dict,list)) or (isinstance(value, tuple) and any(contains_nested(x) for x in value))

def render(value, typ, null):
    if value is None: return null
    if typ == "bool": return "true" if value else "false"
    if typ == "date": return value.isoformat()
    if typ == "timestamp": return value.isoformat(timespec="auto").replace("+00:00", "Z")
    return str(value)

def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

def normalize_json_value(value):
    if isinstance(value, dict): return {k:normalize_json_value(value[k]) for k in sorted(value)}
    if isinstance(value, list): return [normalize_json_value(x) for x in value]
    return value

def cast_nested(value, typ, path, csv_input=False, on_error="fail"):
    if value is None or value == "": return None
    if is_primitive(typ):
        if not csv_input and isinstance(value, (dict,list)): raise ValueError("nested value for flat type")
        return parse_value(value, typ)
    k=typ["kind"]
    if k == "json":
        if csv_input and isinstance(value, str): return json.loads(value)
        return normalize_json_value(value)
    if csv_input and isinstance(value, str): value=json.loads(value)
    if k == "struct":
        if not isinstance(value, dict): raise ValueError("not an object")
        out={}
        for n,t in typ["fields"]:
            try: out[n]=cast_nested(value.get(n),t,path+"."+n,False,on_error)
            except CastError: raise
            except (ValueError,TypeError,OverflowError,json.JSONDecodeError) as e:
                if on_error == "fail": raise CastError(value.get(n),t,path+"."+n) from e
                out[n]=None if on_error == "coerce-null" else value.get(n)
        return out
    if k == "array":
        if not isinstance(value, list): raise ValueError("not an array")
        out=[]
        for x in value:
            try: out.append(cast_nested(x,typ["element"],path+"[]",False,on_error))
            except CastError: raise
            except (ValueError,TypeError,OverflowError,json.JSONDecodeError) as e:
                if on_error == "fail": raise CastError(x,typ["element"],path+"[]") from e
                out.append(None if on_error == "coerce-null" else x)
        return out
    if k == "map":
        if not isinstance(value, dict): raise ValueError("not an object")
        out={}
        for n in sorted(value):
            x=value[n]
            try: out[str(n)]=cast_nested(x,typ["value"],path+'["'+str(n)+'"]',False,on_error)
            except CastError: raise
            except (ValueError,TypeError,OverflowError,json.JSONDecodeError) as e:
                if on_error == "fail": raise CastError(x,typ["value"],path+'["'+str(n)+'"]') from e
                out[str(n)]=None if on_error == "coerce-null" else x
        return out
    raise ValueError("unknown type")

def render_cell(value, typ, null_literal, keep=False):
    if isinstance(typ, str): return render(value, typ, null_literal)
    if value is None: return null_literal
    if keep and isinstance(value, str): return value
    return canonical_json(value)

PATH_TOKEN = re.compile(r'([^\.\[\]]+)|\["((?:[^"\\]|\\.)*)"\]|\[([0-9]+)\]')
def path_tokens(path):
    tokens=[]; pos=0
    while pos < len(path):
        if path[pos] == '.': pos += 1; continue
        m=PATH_TOKEN.match(path,pos)
        if not m: raise MergeError(f'ERR 3 key column "{path}" does not resolve to a primitive',3)
        tokens.append(m.group(1) if m.group(1) is not None else (json.loads('"'+m.group(2)+'"') if m.group(2) is not None else int(m.group(3))))
        pos=m.end()
    return tokens

def resolve_path_type(schema_types, path):
    toks=path_tokens(path)
    if not toks or not isinstance(toks[0],str) or toks[0] not in schema_types: raise MergeError(f'ERR 3 key column "{path}" does not resolve to a primitive',3)
    t=schema_types[toks[0]]
    for tok in toks[1:]:
        if isinstance(t,str) or t.get("kind") == "json": raise MergeError(f'key column "{path}" does not resolve to a primitive',3)
        k=t["kind"]
        if k == "struct":
            matches={n:x for n,x in t["fields"]}
            if not isinstance(tok,str) or tok not in matches: raise MergeError(f'key column "{path}" does not resolve to a primitive',3)
            t=matches[tok]
        elif k == "array":
            if not isinstance(tok,int) or tok < 0: raise MergeError(f'key column "{path}" does not resolve to a primitive',3)
            t=t["element"]
        elif k == "map":
            if not isinstance(tok,str): raise MergeError(f'key column "{path}" does not resolve to a primitive',3)
            t=t["value"]
    if not is_primitive(t): raise MergeError(f'key column "{path}" does not resolve to a primitive',3)
    return toks,t

def path_value(values, tokens):
    try:
        v=values[tokens[0]]
        for tok in tokens[1:]:
            if v is None: return None
            if isinstance(tok,int):
                if not isinstance(v,list) or tok >= len(v): return None
            else:
                if isinstance(v,dict):
                    if tok not in v: return None
                else: return None
            v=v[tok]
        return v
    except (KeyError, IndexError, TypeError): return None

def open_input(path, compression):
    gz = path.lower().endswith(".gz")
    if compression == "gzip" and not gz: raise MergeError(f"{path}: compression mismatch", 5)
    if compression == "none" and gz: raise MergeError(f"{path}: compression mismatch", 5)
    return gzip.open(path, "rt", encoding="utf-8", newline="") if (compression == "gzip" or compression == "auto" and gz) else open(path, "r", encoding="utf-8", newline="")

def _gzip_magic_parquet(path):
    try:
        with gzip.open(path, "rb") as f: return f.read(4) == b"PAR1"
    except OSError: return False

def detect(path, forced, compression):
    gz = path.lower().endswith(".gz")
    if compression == "gzip" and not gz: raise MergeError(f"{path}: compression mismatch", 5)
    if compression == "none" and gz: raise MergeError(f"{path}: compression mismatch", 5)
    base = path[:-3] if path.lower().endswith(".gz") else path
    ext = os.path.splitext(base)[1].lower()
    byext = {".csv":"csv", ".tsv":"tsv", ".jsonl":"jsonl", ".ndjson":"jsonl", ".parquet":"parquet"}.get(ext)
    if forced != "auto": return forced
    try:
        with open(path, "rb") as f: magic = f.read(4)
    except OSError as e: raise MergeError(str(e), 2)
    if magic == b"PAR1" or (path.lower().endswith(".gz") and _gzip_magic_parquet(path)): return "parquet"
    if byext: return byext
    raise MergeError(f"{path}: cannot detect input format", 2)

def csv_rows(path, fmt, compression):
    dialect = dict(delimiter="\t" if fmt == "tsv" else ",", quoting=csv.QUOTE_NONE, strict=True) if fmt == "tsv" else dict(delimiter=",", quotechar='"', strict=True)
    with open_input(path, compression) as f:
        reader = csv.reader(f, **dialect)
        header = next(reader, None)
        if not header: raise MergeError(f"{path}: missing header row", 5)
        if len(set(header)) != len(header): raise MergeError(f"{path}: duplicate header", 5)
        yield header, None
        for n, row in enumerate(reader, 2):
            if len(row) != len(header): raise MergeError(f"{path}:{n}: wrong number of fields", 5)
            yield header, InputRow(zip(header, row), line=n)

def jsonl_rows(path, compression, allow_nested=False):
    header = None
    with open_input(path, compression) as f:
        for n, line in enumerate(f, 1):
            if not line.strip(): continue
            try: obj = json.loads(line)
            except json.JSONDecodeError as e: raise MergeError(f"{path}:{n}: invalid JSON: {e.msg}", 5)
            if not isinstance(obj, dict): raise MergeError(f"{path}:{n}: JSON row must be an object", 5)
            if not allow_nested and any(isinstance(v, (dict,list)) for v in obj.values()):
                raise MergeError("ERR 6 nested structure requires provided --schema", 6)
            if header is None:
                header = list(obj)
                yield header, None
            for k in obj:
                if k not in header: header.append(k)
            yield header, InputRow(obj, line=n)
    if header is None: raise MergeError(f"{path}: missing header row", 5)

def parquet_rows(path, compression):
    try: import pyarrow.parquet as pq
    except ImportError: raise MergeError("Parquet support requires pyarrow", 1)
    temp = None
    source = path
    if compression == "gzip" or (compression == "auto" and path.lower().endswith(".gz")):
        try:
            fd, temp = tempfile.mkstemp(suffix=".parquet"); os.close(fd)
            with gzip.open(path, "rb") as src, open(temp, "wb") as dst:
                while True:
                    block = src.read(1024 * 1024)
                    if not block: break
                    dst.write(block)
            source = temp
        except OSError as e: raise MergeError(str(e), 5)
    try: pf = pq.ParquetFile(source)
    except Exception as e:
        if temp: os.unlink(temp)
        raise MergeError(f"{path}: {e}", 5)
    schema = pf.schema_arrow
    header = schema.names
    yield header, None
    try:
        for batch in pf.iter_batches():
            cols = batch.to_pydict()
            for i in range(batch.num_rows): yield header, {n: cols[n][i] for n in header}
    finally:
        if temp: os.unlink(temp)

def rows(path, fmt, compression, allow_nested=False):
    if fmt in ("csv", "tsv"): return csv_rows(path, fmt, compression)
    if fmt == "jsonl": return jsonl_rows(path, compression, allow_nested)
    return parquet_rows(path, compression)

def schema_file(path, alias_file=None):
    try:
        with open(path, encoding="utf-8") as f: data = json.load(f)
    except (OSError, json.JSONDecodeError) as e: raise MergeError(str(e), 2)
    cols = data.get("columns") if isinstance(data, dict) else None
    if not isinstance(cols, list) or not cols: raise MergeError("schema must contain a non-empty columns array", 2)
    parse_type, _ = alias_resolver(alias_file)
    out=[]; seen=set()
    for c in cols:
        if not isinstance(c,dict) or not isinstance(c.get("name"),str) or c["name"] in seen: raise MergeError("invalid schema", 2)
        seen.add(c["name"]); out.append((c["name"],parse_type(c.get("type"))))
    return out

def _compatible(src, dst): return src == dst or (src == "int" and dst == "float")

def resolve(infos, mode, strategy):
    names = sorted({n for _, h, _ in infos for n in h}); obs = {n:[] for n in names}
    typed = {n:[] for n in names}
    # Parquet carries the strongest schema, followed by JSONL; CSV/TSV are raw.
    source_rank = {"csv":0, "tsv":0, "jsonl":1, "parquet":2}
    for fmt, h, values in infos:
        for n in h:
            # Keep only observed kinds: inference does not need to retain all cells.
            ks = [v for v in values.get(n, set()) if v]
            if not ks: continue
            if fmt in ("jsonl", "parquet"):
                typed[n].append((source_rank[fmt], ks))
            if mode == "strict": obs[n].append(next(iter(set(ks))) if len(set(ks)) == 1 else "string")
            else: obs[n].extend(ks)
    out=[]
    for n in names:
        ks=[x for x in obs[n] if x]
        if strategy == "authoritative" and typed[n]:
            best = max(x[0] for x in typed[n])
            selected = [k for rank, values in typed[n] if rank == best for k in values]
            t = selected[0] if mode == "loose" else (selected[0] if len(set(selected)) == 1 else "string")
        elif not ks: t="string"
        elif strategy == "consensus": t=max(set(ks), key=lambda x:(ks.count(x), -RANK[x]))
        elif strategy == "union":
            t="string"
            for candidate in ("bool","int","float","date","timestamp"):
                if all(_compatible(k,candidate) for k in ks): t=candidate; break
        else: t=max(ks, key=lambda x:RANK[x])
        out.append((n,t))
    return out

class SortKey:
    def __init__(self, r, d): self.r,self.d=r,d
    def __lt__(self, o): return compare(self.r,o.r,self.d)<0

def compare(a,b,desc):
    for x,y in zip(a[0],b[0]):
        if x is None and y is None: continue
        if x is None: return 1 if desc else -1
        if y is None: return -1 if desc else 1
        if x != y: return (-1 if x < y else 1) * (-1 if desc else 1)
    return (a[1]>b[1])-(a[1]<b[1])

def write_chunk(path, records):
    with open(path,"wb") as f:
        for r in records: pickle.dump(r,f,protocol=4)
def chunk_records(path):
    with open(path,"rb") as f:
        while True:
            try: yield pickle.load(f)
            except EOFError: return

def csv_bytes(row, args):
    """Render one output record exactly as csv.writer would, as UTF-8 bytes."""
    s = io.StringIO(newline="")
    w = csv.writer(s, delimiter=",", quotechar=args.csv_quotechar,
                   escapechar=args.csv_escapechar,
                   doublequote=not bool(args.csv_escapechar),
                   lineterminator="\n")
    w.writerow(row)
    return s.getvalue().encode("utf-8")

def encoded_partition_value(value, typ, null_literal):
    if value is None:
        return "_null"
    text = render(value, typ, null_literal)
    out = []
    for b in text.encode("utf-8"):
        c = chr(b)
        if (b >= ord("A") and b <= ord("Z")) or (b >= ord("a") and b <= ord("z")) or (b >= ord("0") and b <= ord("9")) or c in "._-":
            out.append(c)
        else:
            out.append("%%%02X" % b)
    return "".join(out)

def merged_records(chunks, desc):
    its = [chunk_records(x) for x in chunks]
    heap = []
    for i, it in enumerate(its):
        try:
            x = next(it)
            heapq.heappush(heap, (SortKey(x, desc), i, x))
        except StopIteration:
            pass
    while heap:
        _, i, x = heapq.heappop(heap)
        yield x
        try:
            y = next(its[i])
            heapq.heappush(heap, (SortKey(y, desc), i, y))
        except StopIteration:
            pass

def make_output_dir(path):
    parent = os.path.dirname(os.path.abspath(path)) or "."
    if os.path.exists(path) and not os.path.isdir(path):
        raise MergeError(f"{path}: output is not a directory", 2)
    return tempfile.mkdtemp(prefix="." + os.path.basename(os.path.abspath(path)) + ".tmp-", dir=parent)

def commit_output_dir(temp_path, output):
    old = None
    if os.path.exists(output):
        old = tempfile.mkdtemp(prefix="." + os.path.basename(os.path.abspath(output)) + ".old-", dir=os.path.dirname(os.path.abspath(output)) or ".")
        os.rmdir(old)
        os.rename(output, old)
    try:
        os.rename(temp_path, output)
    except Exception:
        if old and not os.path.exists(output):
            os.rename(old, output)
        raise
    if old:
        shutil.rmtree(old)

def emit_output(records, names, types, args, temp_root, partition_specs=None):
    partition_names = [x for x in args.partition_by.split(",") if x] if args.partition_by else []
    partitioned = bool(partition_names)
    header = csv_bytes(names, args)
    if not partitioned and args.max_rows_per_file is None and args.max_bytes_per_file is None:
        if args.output == "-":
            sys.stdout.buffer.write(header)
            for _, _, rendered, _ in records: sys.stdout.buffer.write(csv_bytes(rendered, args))
            return
        parent = os.path.dirname(os.path.abspath(args.output)) or "."
        fd, path = tempfile.mkstemp(prefix=".merge-", dir=parent); os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(header)
                for _, _, rendered, _ in records: f.write(csv_bytes(rendered, args))
            os.replace(path, args.output)
        except Exception:
            if os.path.exists(path): os.unlink(path)
            raise
        return

    # A disk-backed state table avoids retaining one writer/row buffer per partition.
    db = None
    db_path = None
    global_part = global_count = global_size = 0
    if partitioned:
        db_path = os.path.join(temp_root, ".partition-state.sqlite")
        db = sqlite3.connect(db_path)
        db.execute("create table state (key text primary key, rel text, part integer, rows integer, bytes integer)")
        db.commit()
    current = None
    try:
        for sort_values, _, rendered, all_values in records:
            if partitioned:
                # Use typed values, not their textual representation, for layout.
                pvals = tuple(path_value(all_values, toks) for toks, _ in partition_specs)
                rel = os.path.join(*(n + "=" + encoded_partition_value(v, typ, args.csv_null_literal) for n, v, (_, typ) in zip(partition_names, pvals, partition_specs)))
                key = json.dumps(pvals, default=str, ensure_ascii=False, separators=(",", ":"))
                row = db.execute("select rel,part,rows,bytes from state where key=?", (key,)).fetchone()
                if row is None:
                    rel, part, count, size = rel, 0, 0, 0
                else:
                    rel, part, count, size = row
                directory = os.path.join(temp_root, rel)
            else:
                rel, part, count, size = "", global_part, global_count, global_size
                directory = temp_root
            line = csv_bytes(rendered, args)
            limit = args.max_bytes_per_file
            row_limit = args.max_rows_per_file
            if count and ((row_limit is not None and count >= row_limit) or (limit is not None and size + len(line) > limit)):
                part += 1; count = 0; size = 0
            os.makedirs(directory, exist_ok=True)
            filename = os.path.join(directory, "part-%05d.csv" % part)
            if count == 0:
                with open(filename, "wb") as f: f.write(header)
                size = len(header)
            with open(filename, "ab") as f: f.write(line)
            count += 1; size += len(line)
            if partitioned:
                db.execute("insert or replace into state values (?,?,?,?,?)", (key, rel, part, count, size))
                db.commit()
            else:
                global_part, global_count, global_size = part, count, size
        if db:
            db.close(); db = None
        if db_path and os.path.exists(db_path): os.unlink(db_path)
    finally:
        if db: db.close()
        if db_path and os.path.exists(db_path): os.unlink(db_path)

def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument("--output",required=True); ap.add_argument("--key",required=True); ap.add_argument("--desc",action="store_true")
    ap.add_argument("--partition-by")
    ap.add_argument("--max-rows-per-file", type=int)
    ap.add_argument("--max-bytes-per-file", type=int)
    ap.add_argument("--schema"); ap.add_argument("--type-alias-file")
    ap.add_argument("--infer",choices=("strict","loose"),default="strict")
    ap.add_argument("--schema-strategy",choices=("authoritative","consensus","union"),default="authoritative")
    ap.add_argument("--on-type-error",choices=("coerce-null","fail","keep-string"),default="coerce-null")
    ap.add_argument("--memory-limit-mb",type=int,default=64); ap.add_argument("--temp-dir")
    ap.add_argument("--csv-quotechar",default='"'); ap.add_argument("--csv-escapechar"); ap.add_argument("--csv-null-literal",default="")
    ap.add_argument("--input-format",choices=("auto","csv","tsv","jsonl","parquet"),default="auto")
    ap.add_argument("--compression",choices=("auto","none","gzip"),default="auto"); ap.add_argument("--parquet-row-group-bytes",type=int,default=0); ap.add_argument("inputs",nargs="+")
    try: args=ap.parse_args(argv)
    except SystemExit as e: return e.code
    partitioned = bool([x for x in args.partition_by.split(",") if x]) if args.partition_by is not None else False
    sharded = partitioned or args.max_rows_per_file is not None or args.max_bytes_per_file is not None
    if (args.memory_limit_mb<=0 or len(args.csv_quotechar)!=1 or
        (args.csv_escapechar and len(args.csv_escapechar)!=1) or
        (args.max_rows_per_file is not None and args.max_rows_per_file <= 0) or
        (args.max_bytes_per_file is not None and args.max_bytes_per_file <= 0) or
        (sharded and args.output == "-") or
        (args.partition_by is not None and not all(x for x in args.partition_by.split(",")))):
        print("merge_files.py: invalid arguments",file=sys.stderr); return 2
    try:
        specs=[(p,detect(p,args.input_format,args.compression)) for p in args.inputs]
        schema=schema_file(args.schema, args.type_alias_file) if args.schema else None
        infos=[]
        for p,f in specs:
            vals={}; header=None
            for h,r in rows(p,f,args.compression, bool(schema)):
                header=h
                if r is not None:
                    if not schema and any(contains_nested(v) for v in r.values()):
                        raise MergeError("ERR 6 nested structure requires provided --schema",6)
                    for n, v in r.items():
                        if v is not None and v != "": vals.setdefault(n,set()).add(observed_kind(v, f))
            infos.append((f,header,vals))
        schema=schema or resolve(infos,args.infer,args.schema_strategy)
        names=[n for n,_ in schema]; types=dict(schema); keys=[x for x in args.key.split(",") if x]
        if not keys: raise MergeError("every sort key must be present in the resolved schema",3)
        key_specs=[resolve_path_type(types,k) for k in keys]
        partition_names = [x for x in args.partition_by.split(",") if x] if args.partition_by else []
        partition_specs=[resolve_path_type(types,k) for k in partition_names]
        budget=max(1<<20,args.memory_limit_mb*1024*1024//2)
        td=tempfile.TemporaryDirectory(dir=args.temp_dir); chunks=[]; records=[]; estimate=0; seq=0
        try:
            for p,f in specs:
                for _,r in rows(p,f,args.compression, True):
                    if r is None: continue
                    vals=[]; rendered=[]
                    for n,t in schema:
                        raw=r.get(n)
                        kept=False
                        try:
                            v=cast_nested(raw,t,n, f in ("csv","tsv"), args.on_type_error)
                        except json.JSONDecodeError as e:
                            if args.on_type_error == "fail": raise MergeError(f'ERR 5 invalid JSON in field "{n}" (file={p} line={getattr(r,"line","?")})',5)
                            v=raw if args.on_type_error=="keep-string" else None
                        except CastError as e:
                            if args.on_type_error=="fail": raise MergeError(f'ERR 4 cannot cast "{e.raw}" to {type_label(e.typ)} in field "{e.path}" (file={p} line={getattr(r,"line","?")})',4)
                            v=raw if args.on_type_error=="keep-string" else None; kept=args.on_type_error=="keep-string"
                        except (ValueError,TypeError,OverflowError) as e:
                            if args.on_type_error=="fail": raise MergeError(f'ERR 4 cannot cast "{raw}" to {type_label(t)} in field "{n}" (file={p} line={getattr(r,"line","?")})',4)
                            v=raw if args.on_type_error=="keep-string" else None; kept=args.on_type_error=="keep-string"
                        vals.append(v); rendered.append(render_cell(v,t,args.csv_null_literal,kept))
                    value_map=dict(zip(names,vals))
                    key_values=tuple(path_value(value_map,toks) for toks,_ in key_specs)
                    records.append((key_values,seq,rendered,value_map)); seq+=1; estimate+=sum(len(x) for x in rendered)+128
                    if estimate>=budget:
                        records.sort(key=functools.cmp_to_key(lambda a,b:compare(a,b,args.desc))); fn=os.path.join(td.name,f"chunk-{len(chunks)}"); write_chunk(fn,records); chunks.append(fn); records=[]; estimate=0
            if records:
                records.sort(key=functools.cmp_to_key(lambda a,b:compare(a,b,args.desc))); fn=os.path.join(td.name,f"chunk-{len(chunks)}"); write_chunk(fn,records); chunks.append(fn)
            if sharded:
                out_temp = make_output_dir(args.output)
                try:
                    emit_output(merged_records(chunks, args.desc), names, types, args, out_temp, partition_specs)
                    commit_output_dir(out_temp, args.output)
                    out_temp = None
                finally:
                    if out_temp and os.path.exists(out_temp): shutil.rmtree(out_temp)
            else:
                emit_output(merged_records(chunks, args.desc), names, types, args, td.name)
        finally: td.cleanup()
    except MergeError as e:
        print(f"merge_files.py: {e.message}",file=sys.stderr); return e.code
    except (OSError,csv.Error,ValueError,sqlite3.Error) as e:
        print(f"merge_files.py: {e}",file=sys.stderr); return 5
    return 0

if __name__=="__main__": sys.exit(main())

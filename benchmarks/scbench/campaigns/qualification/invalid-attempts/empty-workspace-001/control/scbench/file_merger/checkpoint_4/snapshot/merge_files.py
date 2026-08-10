#!/usr/bin/env python3
"""Merge heterogeneous tabular files into one sorted CSV."""
import argparse, csv, datetime as dt, functools, gzip, heapq, io, json, os, pickle, re, sys, tempfile, shutil

TYPES = ("string", "float", "int", "bool", "date", "timestamp")
PRIORITY = {"string": 0, "float": 1, "int": 2, "bool": 3, "date": 4, "timestamp": 5}
INT_RE = re.compile(r"^[+-]?\d+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

class MergeError(Exception):
    def __init__(self, message, code=1): self.code = code; super().__init__(message)
class InputError(MergeError):
    def __init__(self, message): super().__init__(message, 5)
class NestedError(MergeError):
    def __init__(self, message): super().__init__(message, 6)

class CastError(MergeError):
    def __init__(self, message): super().__init__(message, 4)

BUILTIN_ALIASES = {"integer":"int", "long":"int", "double":"float", "number":"float",
                   "boolean":"bool", "datetime":"timestamp", "timestamptz":"timestamp",
                   "text":"string", "varchar":"string", "json":"json"}

def type_label(t):
    if isinstance(t, str): return t
    if "array" in t: return "array<%s>" % type_label(t["array"])
    if "map" in t: return "map<string,%s>" % type_label(t["map"])
    if "json" in t: return "json"
    return "struct"

def parse_type_aliases(data):
    aliases = dict(BUILTIN_ALIASES)
    if data:
        aliases.update({str(k).lower(): str(v).lower() for k,v in data.get("aliases", {}).items()})
    resolving, done = set(), {}
    def expand_name(name):
        name=name.lower().strip()
        if name in done: return done[name]
        if name in resolving: raise MergeError("type alias cycle", 2)
        if name not in aliases: return name
        resolving.add(name)
        val=expand_expr(aliases[name])
        resolving.remove(name); done[name]=val
        return val
    def split_top(s, sep):
        depth=0
        for i,c in enumerate(s):
            if c == '<': depth += 1
            elif c == '>': depth -= 1
            elif c == sep and depth == 0: return s[:i],s[i+1:]
        return None
    def expand_expr(expr):
        s=expr.lower().strip()
        if s.startswith("list<") and s.endswith(">"): return {"array":expand_expr(s[5:-1])}
        if s.startswith("array<") and s.endswith(">"):
            return {"array":expand_expr(s[6:-1])}
        if s.startswith("map<") and s.endswith(">"):
            pair=split_top(s[4:-1], ',')
            if not pair or pair[0].strip() != "string": raise MergeError("map keys must be string", 2)
            return {"map":expand_expr(pair[1])}
        if s == "json": return {"json":True}
        if s in TYPES: return s
        if s in aliases: return expand_name(s)
        raise MergeError("unknown type %r" % expr, 2)
    for _name in list(aliases): expand_name(_name)
    return expand_expr, aliases

def parse_schema(spec, alias_data=None):
    if not isinstance(spec, dict) or not isinstance(spec.get("columns"), list): raise ValueError("schema must contain columns")
    expand, _ = parse_type_aliases(alias_data)
    def typ(x):
        if isinstance(x, str): return expand(x)
        if not isinstance(x, dict) or len(x) != 1: raise ValueError("invalid nested type")
        if "struct" in x:
            fields=x["struct"].get("fields") if isinstance(x["struct"],dict) else None
            if not isinstance(fields,list): raise ValueError("struct must contain fields")
            out=[]; seen=set()
            for f in fields:
                if not isinstance(f,dict) or "name" not in f or "type" not in f or f["name"] in seen: raise ValueError("invalid struct field")
                seen.add(f["name"]); out.append((f["name"],typ(f["type"])))
            return {"struct":out}
        if "array" in x:
            a=x["array"]; e=a.get("element") if isinstance(a,dict) else None
            if e is None: raise ValueError("array must contain element")
            return {"array":typ(e)}
        if "map" in x:
            m=x["map"]
            if not isinstance(m,dict) or str(m.get("key","")).lower() != "string": raise ValueError("map keys must be string")
            return {"map":typ(m.get("value"))}
        raise ValueError("invalid nested type")
    result=[(x["name"],typ(x["type"])) for x in spec["columns"]]
    if len({n for n,_ in result}) != len(result): raise ValueError("duplicate column names")
    return result

def is_nested(t): return isinstance(t,dict)
def is_primitive(t): return isinstance(t,str) and t in TYPES

def parse_timestamp(s):
    x = s.strip()
    if x.endswith(("Z", "z")): x = x[:-1] + "+00:00"
    value = dt.datetime.fromisoformat(x)
    if value.tzinfo is None: value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)

def can_parse(s, typ):
    try:
        x = s.strip()
        if typ == "string": return True
        if typ == "int": return bool(INT_RE.fullmatch(x))
        if typ == "float": return bool(x) and float(x) == float(x) and abs(float(x)) != float("inf")
        if typ == "bool": return x.lower() in {"true","false","yes","no","y","n","t","f","1","0"}
        if typ == "date": return bool(DATE_RE.fullmatch(x)) and bool(dt.date.fromisoformat(x))
        if typ == "timestamp": parse_timestamp(x); return not bool(DATE_RE.fullmatch(x))
    except (ValueError, OverflowError): return False
    return False

def possible_types(value):
    if value is None: return set()
    if isinstance(value, bool): return {"bool"}
    if isinstance(value, int) and not isinstance(value, bool):
        return ({"int", "float"} if -(1 << 63) <= value <= (1 << 63) - 1 else {"float"})
    if isinstance(value, float): return {"float"}
    if isinstance(value, str):
        result = {"float"} if can_parse(value, "float") else set()
        if can_parse(value, "int"): result.add("int")
        if can_parse(value, "bool") and value.strip().lower() not in {"1", "0"}: result.add("bool")
        if can_parse(value, "date"): result.add("date")
        if can_parse(value, "timestamp"): result.add("timestamp")
        return result or {"string"}
    return {"string"}

def inferred_type(candidates, mode):
    if not candidates: return "string"
    common = set.intersection(*candidates)
    if mode == "strict":
        # A file's observed values must agree; numeric int/float remains numeric.
        return max(common or {"string"}, key=lambda x: PRIORITY[x])
    return max(common or {"string"}, key=lambda x: PRIORITY[x])

def parquet_type(field, allow_nested=False):
    import pyarrow.types as pt
    t = field.type
    if pt.is_struct(t):
        if not allow_nested: raise NestedError("nested structure requires provided --schema")
        return {"struct":[(f.name, parquet_type(f, True)) for f in t]}
    if pt.is_list(t):
        if not allow_nested: raise NestedError("nested structure requires provided --schema")
        return {"array":parquet_type(t.value_field, True)}
    if pt.is_map(t):
        if not allow_nested: raise NestedError("nested structure requires provided --schema")
        if not pt.is_string(t.key_type): raise InputError(f"map key is not string in {field.name}")
        return {"map":parquet_type(t.item_field, True)}
    if pt.is_boolean(t): return "bool"
    if pt.is_integer(t): return "int"
    if pt.is_floating(t) or pt.is_decimal(t): return "float"
    if pt.is_date(t): return "date"
    if pt.is_timestamp(t): return "timestamp"
    return "string"

def detect(path, forced_format, compression):
    ext = path.lower()
    gz_ext = ext.endswith(".gz")
    if compression == "gzip" and not gz_ext:
        # Forced compression is permitted without a conventional suffix.
        is_gz = True
    else: is_gz = gz_ext if compression == "auto" else False if compression == "none" else True
    try:
        with open(path, "rb") as f: magic = f.read(4)
    except OSError as e: raise InputError(str(e))
    is_gzip_magic = magic[:2] == b"\x1f\x8b"
    if compression == "none" and is_gzip_magic: raise InputError(f"gzip input with --compression=none: {path}")
    if compression in ("auto", "gzip") and not is_gz and is_gzip_magic: raise InputError(f"gzip mismatch: {path}")
    base = ext[:-3] if gz_ext else ext
    by_ext = {".csv":"csv", ".tsv":"tsv", ".jsonl":"jsonl", ".ndjson":"jsonl", ".parquet":"parquet"}.get(os.path.splitext(base)[1])
    fmt = forced_format if forced_format != "auto" else by_ext
    if fmt is None and magic == b"PAR1": fmt = "parquet"
    if fmt is None: raise MergeError(f"cannot detect input format: {path}", 2)
    if fmt == "parquet" and magic != b"PAR1" and not is_gz: raise InputError(f"not a Parquet file: {path}")
    if forced_format != "auto" and magic == b"PAR1" and forced_format != "parquet": raise InputError(f"input format mismatch: {path}")
    return fmt, is_gz

def open_text(path, gz):
    try: return gzip.open(path, "rt", encoding="utf-8", newline="") if gz else open(path, "r", encoding="utf-8", newline="")
    except OSError as e: raise InputError(str(e))

def parquet_source(path, gz):
    if not gz: return path, None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
        with gzip.open(path, "rb") as src: shutil.copyfileobj(src, tmp)
        tmp.close()
        return tmp.name, tmp.name
    except (OSError, EOFError) as e:
        try: tmp.close()
        except Exception: pass
        try: os.unlink(tmp.name)
        except Exception: pass
        raise InputError(f"invalid gzip input {path}: {e}")

def read_file(path, fmt, gz, quotechar, escapechar, allow_nested=False):
    """Yield (header, row-number, dict) and validate flat source structure."""
    if fmt == "parquet":
        try: import pyarrow.parquet as pq
        except ImportError: raise MergeError("Parquet support requires pyarrow; install requirements.txt")
        source, cleanup = parquet_source(path, gz)
        try:
            pf = pq.ParquetFile(source)
            header = [f.name for f in pf.schema_arrow]
            for rg in range(pf.num_row_groups):
                for rn, row in enumerate(pf.read_row_group(rg).to_pylist(), 2 + rg):
                    if not allow_nested and any(isinstance(v, (dict,list,tuple)) for v in row.values()): raise NestedError("nested structure requires provided --schema")
                    yield header, rn, row
        except NestedError: raise
        except Exception as e: raise InputError(f"cannot read Parquet {path}: {e}")
        finally:
            if cleanup:
                try: os.unlink(cleanup)
                except OSError: pass
        return
    f = open_text(path, gz)
    try:
        if fmt in ("csv", "tsv"):
            reader = csv.reader(f, delimiter="," if fmt == "csv" else "\t", quotechar=quotechar if fmt == "csv" else '\0', escapechar=escapechar, doublequote=True)
            try: header = next(reader)
            except StopIteration: return
            if len(header) != len(set(header)): raise InputError(f"duplicate header columns in {path}")
            for rn, row in enumerate(reader, 2):
                if fmt == "tsv" and any("\t" in x for x in row): raise InputError(f"literal tab in TSV field {path}:{rn}")
                if len(row) > len(header): raise InputError(f"too many fields in {path}:{rn}")
                yield header, rn, {n: row[i] if i < len(row) else "" for i,n in enumerate(header)}
        else:
            header = None
            for rn, line in enumerate(f, 1):
                if not line.strip(): continue
                try: obj = json.loads(line)
                except Exception as e: raise InputError(f"invalid JSON at {path}:{rn}: {e}")
                if not isinstance(obj, dict): raise InputError(f"JSONL line is not an object at {path}:{rn}")
                if not allow_nested and any(isinstance(v, (dict,list)) for v in obj.values()): raise NestedError("nested structure requires provided --schema")
                if header is None: header = list(obj)
                yield (list(obj), rn, obj)
    except (csv.Error, UnicodeError, OSError) as e: raise InputError(f"invalid delimited input {path}: {e}")
    finally: f.close()

def json_or_parquet_value(v):
    if v is None: return None
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, int): return str(v)
    if isinstance(v, float): return repr(v)
    if isinstance(v, str): return v
    if isinstance(v, dt.datetime): return v.isoformat().replace("+00:00", "Z")
    if isinstance(v, dt.date): return v.isoformat()
    return str(v)

def _json_text(v):
    return json.dumps(v, ensure_ascii=False, separators=(",",":"), allow_nan=False)

def cast(value, typ, null_literal, policy, file_path, row, column, nested=False, field_path=None, json_cell=False):
    field_path = field_path or column
    if value is None or (value == "" and isinstance(value,str) and not nested): return None
    if isinstance(typ,dict):
        if typ.get("json"):
            if isinstance(value,str) and not nested and json_cell:
                try: value=json.loads(value)
                except Exception: return _cast_failure(value, typ, policy, file_path, row, field_path)
            return value
        if "struct" in typ:
            if isinstance(value,str) and not nested and json_cell:
                try: value=json.loads(value)
                except Exception: return _cast_failure(value,typ,policy,file_path,row,field_path)
            if not isinstance(value,dict): return _cast_failure(value,typ,policy,file_path,row,field_path)
            return {n:cast(value.get(n),t,null_literal,policy,file_path,row,column,nested=True,field_path=field_path+"."+n) for n,t in typ["struct"]}
        if "array" in typ:
            if isinstance(value,str) and not nested and json_cell:
                try: value=json.loads(value)
                except Exception: return _cast_failure(value,typ,policy,file_path,row,field_path)
            if not isinstance(value,list): return _cast_failure(value,typ,policy,file_path,row,field_path)
            return [cast(v,typ["array"],null_literal,policy,file_path,row,column,nested=True,field_path=f"{field_path}.{i}") for i,v in enumerate(value)]
        if "map" in typ:
            if isinstance(value,str) and not nested:
                try: value=json.loads(value)
                except Exception: return _cast_failure(value,typ,policy,file_path,row,field_path)
            if not isinstance(value,dict): return _cast_failure(value,typ,policy,file_path,row,field_path)
            return {str(k):cast(value[k],typ["map"],null_literal,policy,file_path,row,column,nested=True,field_path=f'{field_path}["{k}"]') for k in sorted(value)}
    if isinstance(value,(dict,list,tuple)):
        return _cast_failure(value,typ,policy,file_path,row,field_path)
    raw=json_or_parquet_value(value)
    try:
        if typ == "string": return raw if not nested else raw
        if typ == "int":
            if not INT_RE.fullmatch(raw.strip()): raise ValueError()
            out=int(raw.strip())
            return out if nested else str(out)
        if typ == "float":
            x=float(raw.strip())
            if x != x or abs(x)==float("inf"): raise ValueError()
            return x if nested else str(x)
        if typ == "bool":
            x=raw.strip().lower()
            if x in {"true","yes","y","t","1"}: return True if nested else "true"
            if x in {"false","no","n","f","0"}: return False if nested else "false"
            raise ValueError()
        if typ == "date":
            out=dt.date.fromisoformat(raw.strip()).isoformat(); return out
        if typ == "timestamp":
            x=parse_timestamp(raw); out=(x.isoformat(timespec="microseconds") if x.microsecond else x.strftime("%Y-%m-%dT%H:%M:%SZ")).replace("+00:00", "Z"); return out
    except (ValueError, OverflowError, TypeError):
        return _cast_failure(raw,typ,policy,file_path,row,field_path)
    return raw

def _cast_failure(raw, typ, policy, file_path, row, field_path):
    if policy == "fail": raise CastError(f'cannot cast "{raw}" to {type_label(typ)} in field "{field_path}" (file={file_path} line={row})')
    if policy == "keep-string": return json_or_parquet_value(raw)
    return None

def render_value(v, typ):
    if v is None: return None
    if not isinstance(typ,dict): return v
    if typ.get("json"): return _json_text(v)
    return _json_text(v)

def sort_atom(value, typ):
    if value is None: return None
    try:
        return {"int":int, "float":float, "date":dt.date.fromisoformat, "timestamp":parse_timestamp}.get(typ, lambda x: x)(value)
    except (ValueError, OverflowError): return value

def cmp_item(a,b,desc):
    for x,y in zip(a[0],b[0]):
        if x is None or y is None: c=0 if x is None and y is None else (-1 if x is None else 1)
        else:
            try: c=(x>y)-(x<y)
            except TypeError: c=(str(x)>str(y))-(str(x)<str(y))
        if c: return c if x is None or y is None or not desc else -c
    return (a[1]>b[1])-(a[1]<b[1])

class SortItem:
    def __init__(self,item,desc,stream): self.item=item; self.desc=desc; self.stream=stream
    def __lt__(self,other): return cmp_item(self.item,other.item,self.desc)<0

def csv_line(values, args):
    """Return one output record, including its configured line ending."""
    f = io.StringIO(newline="")
    w = csv.writer(f, quotechar=args.csv_quotechar, escapechar=args.csv_escapechar,
                   doublequote=args.csv_escapechar is None, lineterminator="\n")
    w.writerow(values)
    return f.getvalue()

def partition_value(value):
    if value is None: return "_null"
    # Percent-encode bytes, rather than Unicode code points, as required by
    # Hive-style paths.  The permitted characters are deliberately narrower
    # than urllib.quote's default set.
    raw = str(value).encode("utf-8")
    return "".join(chr(b) if (b >= ord("A") and b <= ord("Z")) or
                    (b >= ord("a") and b <= ord("z")) or
                    (b >= ord("0") and b <= ord("9")) or b in b"._-"
                    else "%%%02X" % b for b in raw)

class ShardWriter:
    def __init__(self, root, schema, args, partition_cols=()):
        self.root, self.schema, self.args = root, schema, args
        self.partition_cols = partition_cols
        self.states = {}
        self.header = csv_line([n for n, _ in schema], args).encode("utf-8")

    def _state(self, directory):
        if directory not in self.states:
            os.makedirs(directory, exist_ok=True)
            self.states[directory] = [0, 0, 0]  # part number, rows, bytes
        return self.states[directory]

    def write(self, vals):
        if self.partition_cols:
            parts=[]
            for name,toks,typ,index in self.partition_cols:
                value=vals[index]
                if is_nested(self.schema[index][1]) and isinstance(value,str):
                    try: value=json.loads(value)
                    except Exception: value=None
                parts.append(f"{name}={partition_value(get_path(value,toks))}")
            directory = os.path.join(self.root, *parts)
        else:
            directory = self.root
        state = self._state(directory)
        rendered = [self.args.csv_null_literal if v is None else v for v in vals]
        row = csv_line(rendered, self.args).encode("utf-8")
        header = self.header
        max_rows = self.args.max_rows_per_file
        max_bytes = self.args.max_bytes_per_file
        needs_cut = state[1] and ((max_rows is not None and state[1] >= max_rows) or
                                  (max_bytes is not None and state[2] + len(row) > max_bytes))
        if needs_cut:
            state[0] += 1
            state[1] = state[2] = 0
        path = os.path.join(directory, f"part-{state[0]:05d}.csv")
        if state[1] == 0:
            with open(path, "wb") as f:
                f.write(header)
            state[2] = len(header)
        with open(path, "ab") as f:
            f.write(row)
        state[1] += 1
        state[2] += len(row)

    def ensure_global_header(self):
        # A sharded, non-partitioned output is still a valid CSV when there
        # are no data rows, and follows the same header rule as the old mode.
        if not self.partition_cols and not self.states:
            self._state(self.root)
            path = os.path.join(self.root, "part-00000.csv")
            with open(path, "wb") as f:
                f.write(self.header)

def parse_path(path):
    toks=[]; i=0
    m=re.match(r"[A-Za-z_][A-Za-z0-9_]*",path)
    if not m: raise MergeError("invalid field path: "+path,3)
    toks.append(m.group()); i=m.end()
    while i<len(path):
        if path[i]=='.':
            m=re.match(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+",path[i+1:])
            if not m: raise MergeError("invalid field path: "+path,3)
            toks.append(("index",int(m.group())) if m.group().isdigit() else m.group()); i+=m.end()+1
        elif path[i]=='[':
            j=path.find(']',i+1)
            if j<0: raise MergeError("invalid field path: "+path,3)
            x=path[i+1:j]
            if len(x)>=2 and x[0] in "\"'" and x[-1]==x[0]: toks.append(("map",x[1:-1]))
            elif x.isdigit(): toks.append(("index",int(x)))
            else: raise MergeError("invalid field path: "+path,3)
            i=j+1
        else: raise MergeError("invalid field path: "+path,3)
    return toks

def resolve_type(root, toks, path):
    t=root
    for tok in toks[1:]:
        if isinstance(t,dict) and "struct" in t:
            name=tok if isinstance(tok,str) else None
            fields=dict(t["struct"])
            if name not in fields: raise MergeError(f'key column "{path}" does not resolve to a primitive',3)
            t=fields[name]
        elif isinstance(t,dict) and "array" in t and isinstance(tok,tuple) and tok[0]=="index": t=t["array"]
        elif isinstance(t,dict) and "map" in t and isinstance(tok,tuple) and tok[0]=="map": t=t["map"]
        else: raise MergeError(f'key column "{path}" does not resolve to a primitive',3)
    if not is_primitive(t): raise MergeError(f'key column "{path}" does not resolve to a primitive',3)
    return t

def get_path(value,toks):
    for tok in toks[1:]:
        if value is None: return None
        if isinstance(tok,str):
            if not isinstance(value,dict) or tok not in value: return None
            value=value[tok]
        elif tok[0]=="index":
            if not isinstance(value,list) or tok[1]>=len(value): return None
            value=value[tok[1]]
        else:
            if not isinstance(value,dict) or tok[1] not in value: return None
            value=value[tok[1]]
    return value

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output",required=True); ap.add_argument("--key",required=True); ap.add_argument("--desc",action="store_true")
    ap.add_argument("--schema"); ap.add_argument("--infer",choices=("strict","loose"),default="strict")
    ap.add_argument("--type-alias-file")
    ap.add_argument("--schema-strategy",choices=("authoritative","consensus","union"),default="authoritative")
    ap.add_argument("--on-type-error",choices=("coerce-null","fail","keep-string"),default="coerce-null")
    ap.add_argument("--memory-limit-mb",type=int,default=128); ap.add_argument("--temp-dir")
    ap.add_argument("--csv-quotechar",default='"'); ap.add_argument("--csv-escapechar"); ap.add_argument("--csv-null-literal",default="")
    ap.add_argument("--input-format",choices=("auto","csv","tsv","jsonl","parquet"),default="auto"); ap.add_argument("--compression",choices=("auto","none","gzip"),default="auto")
    ap.add_argument("--parquet-row-group-bytes",type=int,default=0); ap.add_argument("inputs",nargs="+")
    ap.add_argument("--partition-by")
    ap.add_argument("--max-rows-per-file",type=int)
    ap.add_argument("--max-bytes-per-file",type=int)
    args=ap.parse_args(argv)
    if len(args.csv_quotechar)!=1 or (args.csv_escapechar is not None and len(args.csv_escapechar)!=1): ap.error("CSV quote and escape characters must each be one character")
    if args.memory_limit_mb<=0: ap.error("--memory-limit-mb must be positive")
    if args.max_rows_per_file is not None and args.max_rows_per_file <= 0: ap.error("--max-rows-per-file must be positive")
    if args.max_bytes_per_file is not None and args.max_bytes_per_file <= 0: ap.error("--max-bytes-per-file must be positive")
    partition_names=[x for x in args.partition_by.split(",") if x] if args.partition_by is not None else []
    if args.partition_by is not None and not partition_names: ap.error("--partition-by requires at least one column")
    sharded=bool(args.partition_by is not None or args.max_rows_per_file is not None or args.max_bytes_per_file is not None)
    if sharded and args.output == "-": ap.error("partitioned output requires --output to be a directory")
    keys=[x for x in args.key.split(",") if x]
    if not keys: ap.error("--key requires at least one column")
    alias_data=None
    if args.type_alias_file:
        try:
            with open(args.type_alias_file,encoding="utf-8") as f: alias_data=json.load(f)
        except Exception as e: raise MergeError(f"invalid type alias file: {e}",2)
    provided_schema=None
    if args.schema:
        try:
            with open(args.schema,encoding="utf-8") as f: provided_schema=parse_schema(json.load(f),alias_data)
        except MergeError: raise
        except Exception as e: raise MergeError(f"invalid schema: {e}",2)
    infos=[(p,*detect(p,args.input_format,args.compression)) for p in args.inputs]
    observations={}; names=set(); file_types=[]
    for path,fmt,gz in infos:
        local={}; header_seen=False
        if fmt=="parquet":
            try:
                import pyarrow.parquet as pq
                source, cleanup = parquet_source(path, gz)
                fields=list(pq.ParquetFile(source).schema_arrow)
                header=[f.name for f in fields]; local={f.name:{parquet_type(f, bool(provided_schema))} for f in fields}
                for n in header:
                    observations.setdefault(n,[]).append(([local[n]] if is_primitive(local[n]) else [{"string"}],fmt))
                names.update(header); file_types.append((path,fmt,gz,local))
                if cleanup: os.unlink(cleanup)
                continue
            except NestedError: raise
            except Exception as e: raise InputError(f"cannot inspect Parquet {path}: {e}")
        for header,rn,row in read_file(path,fmt,gz,args.csv_quotechar,args.csv_escapechar,bool(provided_schema)):
            if not header_seen:
                header_seen=True; names.update(header)
                for n in header: local[n]=[]
            for n,v in row.items():
                if n not in local: local[n]=[]; names.add(n)
                p=possible_types(v)
                if p: local[n].append(p)
        for n in local: observations.setdefault(n,[]).append((local[n] or [{"string"}],fmt))
        file_types.append((path,fmt,gz,local))
    if provided_schema:
        schema=provided_schema
    else:
        schema=[]
        for n in sorted(names):
            obs=observations.get(n,[])
            choices=[inferred_type(x[0],args.infer) for x in obs]
            if args.schema_strategy=="consensus":
                typ=max(set(choices),key=lambda t:(choices.count(t),PRIORITY[t])) if choices else "string"
            elif args.schema_strategy=="union":
                common=set.intersection(*(set(x[0]) for x in obs)) if obs else set()
                typ=max(common,key=lambda t:PRIORITY[t]) if common else "string"
            else: typ=max(choices,key=lambda t:PRIORITY[t]) if choices else "string"
            schema.append((n,typ))
    col_index={n:i for i,(n,_) in enumerate(schema)}
    key_specs=[]
    for k in keys:
        toks=parse_path(k)
        if toks[0] not in col_index: raise MergeError("key column(s) not present in resolved schema: "+toks[0],3)
        key_specs.append((k,toks,resolve_type(schema[col_index[toks[0]]][1],toks,k)))
    part_specs=[]
    for p in partition_names:
        toks=parse_path(p)
        if toks[0] not in col_index: raise MergeError("partition column(s) not present in resolved schema: "+toks[0],3)
        part_specs.append((p,toks,resolve_type(schema[col_index[toks[0]]][1],toks,p)))
    if len(set(partition_names)) != len(partition_names): raise MergeError("duplicate partition columns",3)
    limit=max(1024,args.memory_limit_mb*1024*1024//16); temp=tempfile.TemporaryDirectory(dir=args.temp_dir); chunks=[]; pending=[]; size=0; seq=0
    atomic=None; backup=None
    try:
        def spill():
            nonlocal pending,size
            pending.sort(key=functools.cmp_to_key(lambda a,b:cmp_item(a,b,args.desc)))
            name=os.path.join(temp.name,f"chunk-{len(chunks)}.bin")
            with open(name,"wb") as f:
                for x in pending: pickle.dump(x,f,pickle.HIGHEST_PROTOCOL)
            chunks.append(name); pending=[]; size=0
        for path,fmt,gz,_ in file_types:
            for header,rn,row in read_file(path,fmt,gz,args.csv_quotechar,args.csv_escapechar,bool(provided_schema)):
                rawvals=[]; vals=[]
                for n,typ in schema:
                    v=cast(row.get(n),typ,args.csv_null_literal,args.on_type_error,path,rn,n,json_cell=fmt in ("csv","tsv"))
                    rawvals.append(v)
                    vals.append(render_value(v,typ) if is_nested(typ) else v)
                key=tuple(sort_atom(get_path(rawvals[col_index[t[1][0]]],t[1]),t[2]) for t in key_specs)
                item=(key,seq,vals); pending.append(item); seq+=1; size+=sum(len(x) if isinstance(x,str) else 8 for x in vals)+80
                if size>=limit: spill()
        if pending: spill()
        streams=[open(x,"rb") for x in chunks]; heap=[]
        for i,f in enumerate(streams):
            try: heapq.heappush(heap,SortItem(pickle.load(f),args.desc,i))
            except EOFError: pass
        if not sharded:
            if args.output=="-": out=sys.stdout
            else:
                fd,atomic=tempfile.mkstemp(prefix=".merge-",dir=os.path.dirname(os.path.abspath(args.output)) or ".",text=True); out=os.fdopen(fd,"w",encoding="utf-8",newline="")
            try:
                w=csv.writer(out,quotechar=args.csv_quotechar,escapechar=args.csv_escapechar,doublequote=args.csv_escapechar is None,lineterminator="\n")
                w.writerow([n for n,_ in schema])
                while heap:
                    si=heapq.heappop(heap); key,num,vals=si.item
                    w.writerow([args.csv_null_literal if v is None else v for v in vals])
                    try: heapq.heappush(heap,SortItem(pickle.load(streams[si.stream]),args.desc,si.stream))
                    except EOFError: pass
            finally:
                if out is not sys.stdout: out.close()
                for f in streams: f.close()
            if atomic: os.replace(atomic,args.output); atomic=None
        else:
            parent=os.path.dirname(os.path.abspath(args.output)) or "."
            os.makedirs(parent, exist_ok=True)
            atomic=tempfile.mkdtemp(prefix="." + os.path.basename(os.path.abspath(args.output)) + ".tmp-", dir=parent)
            writer=ShardWriter(atomic, schema, args,
                               [(p,toks,typ,col_index[toks[0]]) for p,toks,typ in part_specs])
            try:
                while heap:
                    si=heapq.heappop(heap); key,num,vals=si.item
                    writer.write(vals)
                    try: heapq.heappush(heap,SortItem(pickle.load(streams[si.stream]),args.desc,si.stream))
                    except EOFError: pass
                writer.ensure_global_header()
                for f in streams: f.close()
                # Replace an existing destination only after the complete
                # tree has been written.  Keep a rollback name until commit.
                if os.path.lexists(args.output):
                    backup=args.output + ".old-" + next(tempfile._get_candidate_names())
                    os.replace(args.output, backup)
                os.replace(atomic,args.output); atomic=None
                if backup:
                    if os.path.isdir(backup) and not os.path.islink(backup): shutil.rmtree(backup)
                    else: os.unlink(backup)
                    backup=None
            except Exception:
                for f in streams:
                    try: f.close()
                    except Exception: pass
                if backup and not os.path.lexists(args.output):
                    os.replace(backup,args.output); backup=None
                raise
    finally:
        if atomic and os.path.exists(atomic):
            if os.path.isdir(atomic): shutil.rmtree(atomic)
            else: os.unlink(atomic)
        if backup and os.path.lexists(backup):
            if os.path.isdir(backup) and not os.path.islink(backup): shutil.rmtree(backup)
            else: os.unlink(backup)
        temp.cleanup()
    return 0

if __name__=="__main__":
    try: sys.exit(main())
    except MergeError as e: print(f"merge_files.py: {e}",file=sys.stderr); sys.exit(e.code)
    except Exception as e: print(f"merge_files.py: {e}",file=sys.stderr); sys.exit(1)

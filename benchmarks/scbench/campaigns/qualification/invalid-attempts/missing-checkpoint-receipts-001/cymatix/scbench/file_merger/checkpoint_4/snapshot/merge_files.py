#!/usr/bin/env python3
"""Merge heterogeneous tabular files into one stably sorted CSV."""
import argparse, csv, datetime as dt, functools, gzip, heapq, io, itertools, json, os, pickle, re, shutil, sys, tempfile
from urllib.parse import quote

TYPES = {"string", "int", "float", "bool", "date", "timestamp"}
BUILTIN_ALIASES = {"integer":"int", "long":"int", "double":"float", "number":"float", "boolean":"bool",
                   "datetime":"timestamp", "timestamptz":"timestamp", "text":"string", "varchar":"string"}

class InputError(Exception): pass
class KeyErrorInput(Exception): pass
class NestedError(Exception): pass
class CastError(Exception): pass

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True); p.add_argument("--key", required=True)
    p.add_argument("--desc", action="store_true"); p.add_argument("--schema")
    p.add_argument("--type-alias-file")
    p.add_argument("--infer", choices=("strict", "loose"), default="strict")
    p.add_argument("--schema-strategy", choices=("authoritative", "consensus", "union"), default="authoritative")
    p.add_argument("--on-type-error", choices=("coerce-null", "fail", "keep-string"), default="coerce-null")
    p.add_argument("--memory-limit-mb", type=int, default=128); p.add_argument("--temp-dir")
    p.add_argument("--csv-quotechar", default='"'); p.add_argument("--csv-escapechar")
    p.add_argument("--csv-null-literal", default="")
    p.add_argument("--input-format", choices=("auto", "csv", "tsv", "jsonl", "parquet"), default="auto")
    p.add_argument("--compression", choices=("auto", "none", "gzip"), default="auto")
    p.add_argument("--parquet-row-group-bytes", type=int, default=0)
    p.add_argument("--partition-by")
    p.add_argument("--max-rows-per-file", type=int)
    p.add_argument("--max-bytes-per-file", type=int)
    p.add_argument("inputs", nargs="+")
    a = p.parse_args()
    if a.memory_limit_mb <= 0 or len(a.csv_quotechar) != 1 or (a.csv_escapechar and len(a.csv_escapechar) != 1): p.error("memory limit must be positive and CSV quote/escape characters must be one character")
    if a.parquet_row_group_bytes < 0: p.error("parquet row group bytes must not be negative")
    if a.max_rows_per_file is not None and a.max_rows_per_file <= 0: p.error("max rows per file must be positive")
    if a.max_bytes_per_file is not None and a.max_bytes_per_file <= 0: p.error("max bytes per file must be positive")
    if (a.partition_by or a.max_rows_per_file is not None or a.max_bytes_per_file is not None) and a.output == "-":
        p.error("partitioned output requires a directory output path")
    return a

def compression(path, args):
    actual = path.lower().endswith(".gz")
    wanted = actual if args.compression == "auto" else args.compression == "gzip"
    if args.compression != "auto" and actual != wanted: raise InputError("compression mismatch")
    return wanted

def fmt(path, args):
    gz = compression(path, args); base = path[:-3] if gz else path; ext = os.path.splitext(base)[1].lower()
    byext = {".csv":"csv", ".tsv":"tsv", ".jsonl":"jsonl", ".ndjson":"jsonl", ".parquet":"parquet"}
    if args.input_format != "auto": return args.input_format, gz
    if ext in byext: return byext[ext], gz
    try:
        with (gzip.open(path, "rb") if gz else open(path, "rb")) as f: magic = f.read(4)
    except OSError as e: raise InputError(str(e))
    if magic == b"PAR1": return "parquet", gz
    raise InputError("cannot detect input format: " + path)

def open_text(path, gz):
    try: return gzip.open(path, "rt", encoding="utf-8", newline="") if gz else open(path, "r", encoding="utf-8", newline="")
    except (OSError, UnicodeError) as e: raise InputError(str(e))

def parse_value(v, typ):
    if typ == "string": return str(v)
    if typ == "int":
        if isinstance(v, bool): raise ValueError("invalid integer")
        if isinstance(v, int): return v
        s = str(v)
        if not s or s.strip() != s or (s[0] in "+-" and not s[1:].isdigit()) or (s[0] not in "+-" and not s.isdigit()): raise ValueError("invalid integer")
        return int(s)
    if typ == "float":
        x = float(v)
        if not (x == x and abs(x) != float("inf")): raise ValueError("non-finite float")
        return x
    if typ == "bool":
        if isinstance(v, bool): return v
        s = str(v).strip().lower()
        if s in ("true","t","yes","y","1"): return True
        if s in ("false","f","no","n","0"): return False
        raise ValueError("invalid boolean")
    if typ == "date": return v if isinstance(v, dt.date) and not isinstance(v, dt.datetime) else dt.date.fromisoformat(str(v))
    if typ == "timestamp":
        if isinstance(v, dt.datetime): x = v
        else:
            s = str(v).strip(); x = dt.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
        return (x.replace(tzinfo=dt.timezone.utc) if x.tzinfo is None else x.astimezone(dt.timezone.utc))
    raise ValueError("unknown type")

def classify(v):
    if v is None: return None
    if isinstance(v, bool): return "bool"
    if isinstance(v, int): return "int" if -(1 << 63) <= v <= (1 << 63) - 1 else "float"
    if isinstance(v, float): return "float"
    s = str(v)
    for t in (("timestamp","date","bool","int","float") if ("T" in s or "t" in s or " " in s) else ("date","bool","int","float")):
        try: parse_value(s, t); return t
        except (ValueError, OverflowError, TypeError): pass
    return "string"

def is_null(v, args): return v is None or (isinstance(v, str) and (v == "" or (args.csv_null_literal and v == args.csv_null_literal)))

def parquet_rows(path, args):
    try: import pyarrow.parquet as pq
    except ImportError: raise InputError("Parquet input requires pyarrow")
    try: pf = pq.ParquetFile(path)
    except Exception as e: raise InputError(str(e))
    if not args.schema:
        for field in pf.schema_arrow:
            if getattr(field.type, "is_nested", False) or str(field.type).startswith(("list<", "map<", "struct<")):
                raise NestedError("nested structure requires provided --schema")
    for i in range(pf.num_row_groups):
        tab = pf.read_row_group(i)
        for row in tab.to_pylist():
            yield row

def rows_for(path, args):
    kind, gz = fmt(path, args)
    if kind == "parquet":
        if gz: raise InputError("gzip-compressed Parquet is not supported")
        it = parquet_rows(path,args)
        try: first = next(it)
        except StopIteration: return [], iter(())
        names = list(first.keys())
        if len(names) != len(set(names)): raise InputError("duplicate header column")
        return names, itertools.chain((first,), it)
    f = open_text(path, gz)
    if kind in ("csv", "tsv"):
        delim = "," if kind == "csv" else "\t"
        r = csv.reader(f, delimiter=delim, quotechar=args.csv_quotechar if kind == "csv" else "\0", escapechar=args.csv_escapechar, strict=True)
        try: header = next(r)
        except (StopIteration, csv.Error) as e: f.close(); raise InputError("invalid or missing header") from e
        if not header or len(header) != len(set(header)): f.close(); raise InputError("invalid or duplicate header")
        def gen():
            try:
                for row in r:
                    if len(row) != len(header): raise InputError("row has wrong number of fields")
                    yield dict(zip(header, row))
            except csv.Error as e: raise InputError(str(e))
            finally: f.close()
        return header, gen()
    def genjson():
        try:
            for line in f:
                if not line.strip(): continue
                try: obj = json.loads(line)
                except (ValueError, UnicodeError) as e: raise InputError("invalid JSON Lines input") from e
                if not isinstance(obj, dict): raise InputError("each JSONL line must be an object")
                yield obj
        finally: f.close()
    it = genjson()
    try: first = next(it)
    except StopIteration: return [], iter(())
    names = list(first); return names, itertools.chain((first,), it)

def load_aliases(path):
    aliases = dict(BUILTIN_ALIASES)
    if not path: return aliases
    try:
        with open(path, encoding="utf-8") as f: obj=json.load(f)
    except (OSError, ValueError, TypeError) as e: raise InputError(str(e))
    vals=obj.get("aliases") if isinstance(obj,dict) else None
    if not isinstance(vals,dict): raise InputError("alias file must contain an aliases object")
    aliases.update({str(k).lower(): v for k,v in vals.items()})
    state={}; resolved={}
    def one(name):
        name=name.lower()
        if name in resolved:return resolved[name]
        if state.get(name)==1: raise InputError("type alias cycle")
        state[name]=1
        target=aliases.get(name, name)
        if not isinstance(target,str): raise InputError("alias target must be a string: "+name)
        # Generic aliases are resolved by parse_type, while scalar aliases are transitive here.
        m=re.fullmatch(r"(list|array)\s*<(.+)>", target, re.I)
        if m: out=target
        else: out=one(target) if target.lower() in aliases else target.lower()
        state[name]=2; resolved[name]=out; return out
    for n in aliases: one(n)
    return resolved

def load_schema(path, aliases=None):
    try:
        if isinstance(path, str) and os.path.isfile(path):
            with open(path, encoding="utf-8") as f: obj = json.load(f)
        else:
            obj = json.loads(path)
    except (OSError, ValueError, TypeError) as e: raise InputError(str(e))
    cols = obj.get("columns") if isinstance(obj, dict) else None
    if not isinstance(cols, list) or not cols: raise InputError("schema must contain a non-empty columns list")
    aliases=aliases or dict(BUILTIN_ALIASES)
    out=[]; seen=set()
    def typ(x):
        if isinstance(x,str):
            raw=x.strip().lower(); raw=aliases.get(raw,raw)
            if raw == "json": return {"json":True}
            m=re.fullmatch(r"(?:array|list)\s*<(.+)>",raw,re.I)
            if m:return {"array":typ(m.group(1))}
            if raw in TYPES:return raw
            raise InputError("unknown schema type: "+x)
        if not isinstance(x,dict) or len(x)!=1: raise InputError("invalid nested schema type")
        if "struct" in x:
            spec=x["struct"]; fields=spec.get("fields") if isinstance(spec,dict) else None
            if not isinstance(fields,list): raise InputError("struct must contain fields")
            fs=[]; names=set()
            for f in fields:
                if not isinstance(f,dict) or not isinstance(f.get("name"),str) or f["name"] in names: raise InputError("invalid or duplicate struct field")
                names.add(f["name"]); fs.append((f["name"],typ(f.get("type"))))
            return {"struct":fs}
        if "array" in x:
            a=x["array"]
            if not isinstance(a,dict) or "element" not in a: raise InputError("array must contain element")
            return {"array":typ(a["element"])}
        if "map" in x:
            m=x["map"]
            if not isinstance(m,dict) or str(m.get("key","")).lower()!="string": raise InputError("map keys must be string")
            return {"map":typ(m.get("value"))}
        raise InputError("invalid nested schema type")
    for c in cols:
        if not isinstance(c,dict) or not isinstance(c.get("name"),str): raise InputError("each schema column needs a valid name and type")
        if c["name"] in seen: raise InputError("duplicate schema column: " + c["name"])
        seen.add(c["name"]); out.append((c["name"],typ(c.get("type"))))
    return out

def resolve(infos, args):
    names=set(); evidence={}
    for names_i, types_i, rank in infos:
        names.update(names_i)
        for n, ts in types_i.items():
            if ts: evidence.setdefault(n, []).append((next(iter(ts)) if len(ts)==1 else "string", rank))
    def common(ts):
        if not ts: return "string"
        if "string" in ts: return "string"
        if all(x in ("int","float") for x in ts): return "float" if "float" in ts else "int"
        if len(set(ts)) == 1: return ts[0]
        return "string"
    out=[]
    for n in sorted(names):
        ev=evidence.get(n,[]); ts=[x for x,_ in ev]
        if args.infer == "strict" and ts and len(set(ts)) == 1: typ=ts[0]
        elif args.schema_strategy == "consensus" and ts:
            typ=max(set(ts), key=lambda x:(ts.count(x), -["string","float","int","bool","date","timestamp"].index(x)))
        elif args.schema_strategy == "union": typ=common(ts)
        elif ev: typ=max(enumerate(ev), key=lambda z:(z[1][1], -z[0]))[1][0]
        else: typ="string"
        out.append((n,typ))
    return out

def output_value(v, typ):
    if typ=="string": return v
    if typ=="bool": return "true" if v else "false"
    if typ in ("date","timestamp"): return v.isoformat().replace("+00:00","Z") if typ=="timestamp" else v.isoformat()
    return str(v)

def type_name(t):
    if isinstance(t,str): return t
    if "json" in t:return "json"
    if "array" in t:return "array"
    if "map" in t:return "map"
    return "struct"

def json_value(v):
    if isinstance(v, dt.datetime): return output_value(v,"timestamp")
    if isinstance(v, dt.date): return v.isoformat()
    if isinstance(v, tuple): return [json_value(x) for x in v]
    if isinstance(v, list): return [json_value(x) for x in v]
    if isinstance(v, dict): return {k:json_value(v[k]) for k in sorted(v)}
    return v

def serialize_nested(v, typ):
    if v is None:return None
    if isinstance(typ,dict) and "struct" in typ and not isinstance(v,dict): return v
    if isinstance(typ,dict) and "array" in typ and not isinstance(v,list): return v
    if isinstance(typ,dict) and "map" in typ and not isinstance(v,dict): return v
    if isinstance(typ,dict) and typ.get("json"):
        return json_value(v)
    if isinstance(typ,dict) and "struct" in typ:
        return {n:serialize_nested(v.get(n),t) for n,t in typ["struct"]}
    if isinstance(typ,dict) and "array" in typ:
        return [serialize_nested(x,typ["array"]) for x in v]
    if isinstance(typ,dict) and "map" in typ:
        return {k:serialize_nested(v[k],typ["map"]) for k in sorted(v)}
    return json_value(v)

def nested_output(v, typ):
    if v is None:return None
    return json.dumps(serialize_nested(v,typ), ensure_ascii=False, separators=(",",":"), allow_nan=False)

def cast(v, typ, args, col, seq, source="", line=None):
    if is_null(v,args): return None, args.csv_null_literal
    try: x=parse_value(v,typ); return x,output_value(x,typ)
    except (ValueError,OverflowError,TypeError) as e:
        if args.on_type_error=="fail": raise InputError(f"row {seq}, column {col}: {e}: {v!r}")
        if args.on_type_error=="keep-string": return str(v),str(v)
        return None,args.csv_null_literal

def cast_nested(v, typ, args, path, seq, source, line):
    if is_null(v,args): return None
    if isinstance(typ,dict) and typ.get("json"):
        if isinstance(v,str):
            try:return json.loads(v)
            except (ValueError,TypeError):
                if args.on_type_error=="coerce-null": return None
                if args.on_type_error=="keep-string": return v
                raise CastError(f'cannot cast "{v}" to json in field "{path}" (file={source} line={line})')
        return v
    if isinstance(typ,str):
        try:return parse_value(v,typ)
        except (ValueError,OverflowError,TypeError):
            if args.on_type_error=="coerce-null": return None
            if args.on_type_error=="keep-string": return str(v)
            raise CastError(f'cannot cast "{v}" to {typ} in field "{path}" (file={source} line={line})')
    try:
        if "struct" in typ:
            if not isinstance(v,dict): raise ValueError("expected object")
            return {n:cast_nested(v.get(n),t,args,path+"."+n,seq,source,line) for n,t in typ["struct"]}
        if "array" in typ:
            if not isinstance(v,list): raise ValueError("expected array")
            return [cast_nested(x,typ["array"],args,path+"[]",seq,source,line) for x in v]
        if "map" in typ:
            if not isinstance(v,dict) or any(not isinstance(k,str) for k in v): raise ValueError("expected string-keyed object")
            return {k:cast_nested(v[k],typ["map"],args,path+'["'+k+'"]',seq,source,line) for k in sorted(v)}
    except CastError: raise
    except (ValueError,OverflowError,TypeError):
        if args.on_type_error=="coerce-null": return None
        if args.on_type_error=="keep-string": return str(v)
        raise CastError(f'cannot cast "{v}" to {type_name(typ)} in field "{path}" (file={source} line={line})')
    raise ValueError("invalid type")

def cast_any(v, typ, args, path, seq, source, line, csv_literal=False):
    if isinstance(typ,str):
        if isinstance(v,(dict,list,tuple)):
            if args.on_type_error=="coerce-null": return None,args.csv_null_literal
            if args.on_type_error=="keep-string": return str(v),str(v)
            raise CastError(f'cannot cast "{v}" to {typ} in field "{path}" (file={source} line={line})')
        x,_=cast(v,typ,args,path,seq,source,line); return x, (args.csv_null_literal if x is None else output_value(x,typ))
    if csv_literal and isinstance(v,str) and not is_null(v,args):
        try:v=json.loads(v)
        except (ValueError,TypeError):
            if args.on_type_error=="fail": raise CastError(f'cannot cast "{v}" to {type_name(typ)} in field "{path}" (file={source} line={line})')
            if args.on_type_error=="keep-string": return str(v),str(v)
            return None,args.csv_null_literal
    x=cast_nested(v,typ,args,path,seq,source,line)
    return x,args.csv_null_literal if x is None else nested_output(x,typ)

def path_parts(path):
    out=[]; i=0
    m=re.match(r"[A-Za-z_][\w]*",path)
    if not m: raise KeyErrorInput("invalid field path: "+path)
    out.append(m.group()); i=m.end()
    while i<len(path):
        if path[i]=='.':
            m=re.match(r"\.([A-Za-z_][\w]*)|\.(\d+)",path[i:])
            if not m: raise KeyErrorInput("invalid field path: "+path)
            out.append(int(m.group(2)) if m.group(2) else m.group(1)); i+=len(m.group())
        elif path[i]=='[':
            m=re.match(r'\["([^"\\]*)"\]|\[(\d+)\]',path[i:])
            if not m: raise KeyErrorInput("invalid field path: "+path)
            out.append(int(m.group(2)) if m.group(2) is not None else m.group(1)); i+=len(m.group())
        else: raise KeyErrorInput("invalid field path: "+path)
    return out

def resolve_path(schema, path):
    parts=path_parts(path); root=parts.pop(0); names=dict(schema); t=names.get(root)
    if t is None: raise KeyErrorInput("key column not in resolved schema: "+path)
    for p in parts:
        if isinstance(t,dict) and "struct" in t and isinstance(p,str): t=dict(t["struct"]).get(p)
        elif isinstance(t,dict) and "array" in t and isinstance(p,int): t=t["array"]
        elif isinstance(t,dict) and "map" in t and isinstance(p,str): t=t["map"]
        else: t=None
        if t is None: raise KeyErrorInput(f'key column "{path}" does not resolve to a primitive')
    if not isinstance(t,str): raise KeyErrorInput(f'key column "{path}" does not resolve to a primitive')
    return root, parts, t

def lookup(v, parts):
    for p in parts:
        if v is None:return None
        if isinstance(p,int):
            if not isinstance(v,list) or p>=len(v):return None
        else:
            if not isinstance(v,dict) or p not in v:return None
        v=v[p]
    return v

def cmp(a,b,desc):
    for x,y in zip(a[0],b[0]):
        if x is None or y is None:
            if x is y: continue
            return (1 if desc else -1) if x is None else (-1 if desc else 1)
        if x!=y: return (-1 if x<y else 1) * (1 if not desc else -1)
    return (a[1]>b[1])-(a[1]<b[1])
class H:
    desc=False
    def __init__(self,row,fh): self.row,self.fh=row,fh
    def __lt__(self,o): return cmp(self.row,o.row,self.desc)<0

def main():
    args=parse_args(); keys=[x for x in args.key.split(",") if x]
    if not keys: raise KeyErrorInput("--key must contain at least one column")
    aliases=load_aliases(args.type_alias_file)
    infos=[]
    for path in args.inputs:
        kind, _ = fmt(path, args)
        header,it=rows_for(path,args); ts={n:set() for n in header}
        try:
            for row in it:
                for n,v in row.items():
                    ts.setdefault(n,set())
                    if isinstance(v,(dict,list,tuple)) and not args.schema: raise NestedError("nested structure requires provided --schema")
                    if not is_null(v,args): ts[n].add(classify(v))
        except (InputError,NestedError): raise
        infos.append((header,ts,{"csv":0,"tsv":0,"jsonl":1,"parquet":2}[kind]))
    schema=load_schema(args.schema,aliases) if args.schema else resolve(infos,args)
    key_specs=[]
    for k in keys:
        root,parts,typ=resolve_path(schema,k); key_specs.append((k,root,parts,typ))
    partitions=[x for x in (args.partition_by or "").split(",") if x]
    part_specs=[]
    for k in partitions:
        root,parts,typ=resolve_path(schema,k); part_specs.append((k,root,parts,typ))
    pos={n:i for i,(n,_) in enumerate(schema)}
    limit=max(1024*1024,args.memory_limit_mb*1024*1024//3); root=tempfile.mkdtemp(prefix="csv-merge-",dir=args.temp_dir); runs=[]; seq=0; chunk=[]; size=0
    def flush():
        nonlocal chunk,size
        if not chunk:return
        chunk.sort(key=functools.cmp_to_key(lambda a,b:cmp(a,b,args.desc))); p=os.path.join(root,f"run-{len(runs)}.bin")
        with open(p,"wb") as f:
            for x in chunk: pickle.dump(x,f,pickle.HIGHEST_PROTOCOL)
        runs.append(p);chunk=[];size=0
    try:
        for path in args.inputs:
            kind,_=fmt(path,args)
            header,it=rows_for(path,args); ix=set(header)
            for row in it:
                vals=[]; cells=[]
                for n,t in schema:
                    v=row.get(n) if n in ix else None
                    x,s=cast_any(v,t,args,n,seq,path,seq,kind in ("csv","tsv")); vals.append(x);cells.append(s)
                chunk.append((tuple(lookup(vals[pos[r]],parts) for _,r,parts,_ in key_specs),seq,cells,tuple(vals),tuple(lookup(vals[pos[r]],parts) for _,r,parts,_ in part_specs)));size+=sum(len(x) for x in cells)+64;seq+=1
                if size>=limit:flush()
        flush()
        field_names=[n for n,_ in schema]
        def csv_line(values):
            s=io.StringIO(newline="")
            csv.writer(s,quotechar=args.csv_quotechar,escapechar=args.csv_escapechar,
                       doublequote=args.csv_escapechar is None,lineterminator="\n").writerow(values)
            return s.getvalue()

        header_line=csv_line(field_names)
        partitioned=bool(partitions)
        sharded=args.max_rows_per_file is not None or args.max_bytes_per_file is not None
        directory_mode=partitioned or sharded
        temp_output=None; output_file=None; writers={}; replaced_backup=None

        class Shard:
            def __init__(self, path):
                self.path=path; self.index=0; self.fh=None; self.current_name=None; self.rows=0; self.bytes=0
            def open(self):
                os.makedirs(self.path, exist_ok=True)
                if self.current_name is None:
                    self.current_name=os.path.join(self.path, f"part-{self.index:05d}.csv")
                    self.index += 1
                    self.fh=open(self.current_name,"w",encoding="utf-8",newline="")
                    self.fh.write(header_line); self.rows=0; self.bytes=len(header_line.encode("utf-8"))
                else:
                    self.fh=open(self.current_name,"a",encoding="utf-8",newline="")
            def write(self, values):
                line=csv_line(values); n=len(line.encode("utf-8"))
                if self.fh is None: self.open()
                if self.rows and ((args.max_rows_per_file is not None and self.rows >= args.max_rows_per_file) or
                                  (args.max_bytes_per_file is not None and self.bytes+n > args.max_bytes_per_file)):
                    self.fh.close(); self.fh=None; self.current_name=None; self.open()
                self.fh.write(line); self.rows += 1; self.bytes += n
            def close(self):
                if self.fh is not None: self.fh.close(); self.fh=None

        try:
            if directory_mode:
                destination=os.path.abspath(args.output); parent=os.path.dirname(destination) or os.curdir
                os.makedirs(parent, exist_ok=True)
                temp_output=tempfile.mkdtemp(prefix="."+os.path.basename(destination)+".tmp-",dir=parent)
            else:
                if args.output=="-":
                    output_file=sys.stdout; temp_output=None
                else:
                    temp_output=args.output+".tmp-"+str(os.getpid())
                    output_file=open(temp_output,"w",encoding="utf-8",newline="")
                output_file.write(header_line)

            H.desc=args.desc; fhs=[open(p,"rb") for p in runs]; heap=[]
            for f in fhs:
                try: heapq.heappush(heap,H(pickle.load(f),f))
                except EOFError: pass
            while heap:
                h=heapq.heappop(heap); row=h.row; cells=row[2]
                if directory_mode:
                    if partitions:
                        vals=row[4]; segments=[]
                        for i,(name,_,_,typ) in enumerate(part_specs):
                            value=vals[i]
                            text_value=args.csv_null_literal if value is None else output_value(value,typ)
                            if value is None: text_value="_null"
                            segments.append(name+"="+quote(str(text_value),safe="A-Za-z0-9._-"))
                        key_path=os.path.join(temp_output,*segments)
                    else: key_path=temp_output
                    shard=writers.get(key_path)
                    if shard is None: shard=writers.setdefault(key_path,Shard(key_path))
                    shard.write(cells)
                    # Partition cardinality can be large; do not retain one OS
                    # file descriptor per partition.
                    shard.close()
                else:
                    output_file.write(csv_line(cells))
                try: h.row=pickle.load(h.fh); heapq.heappush(heap,h)
                except EOFError: pass
            for f in fhs: f.close()
            for shard in writers.values(): shard.close()
            if directory_mode:
                if os.path.lexists(destination):
                    replaced_backup=tempfile.mkdtemp(prefix="."+os.path.basename(destination)+".old-",dir=parent)
                    os.rmdir(replaced_backup); os.replace(destination,replaced_backup)
                os.replace(temp_output,destination); temp_output=None
                if replaced_backup: shutil.rmtree(replaced_backup)
            else:
                if output_file is not sys.stdout:
                    output_file.close(); output_file=None; os.replace(temp_output,args.output); temp_output=None
                else: output_file=None
        except Exception:
            for shard in writers.values(): shard.close()
            if output_file is not None and output_file is not sys.stdout: output_file.close()
            if replaced_backup and not os.path.lexists(os.path.abspath(args.output)):
                os.replace(replaced_backup,os.path.abspath(args.output)); replaced_backup=None
            raise
        finally:
            if temp_output:
                if directory_mode: shutil.rmtree(temp_output,ignore_errors=True)
                else:
                    try: os.unlink(temp_output)
                    except OSError: pass
            if replaced_backup: shutil.rmtree(replaced_backup,ignore_errors=True)
    finally:shutil.rmtree(root,ignore_errors=True)

if __name__=="__main__":
    try: main()
    except KeyErrorInput as e: print("merge_files.py: "+str(e),file=sys.stderr);sys.exit(3)
    except NestedError as e: print("merge_files.py: "+str(e),file=sys.stderr);sys.exit(6)
    except CastError as e: print("merge_files.py: ERR 4 "+str(e),file=sys.stderr);sys.exit(4)
    except (InputError, csv.Error, OSError, UnicodeError, ValueError) as e: print("merge_files.py: "+str(e),file=sys.stderr);sys.exit(5)
    except Exception as e: print("merge_files.py: "+str(e),file=sys.stderr);sys.exit(1)

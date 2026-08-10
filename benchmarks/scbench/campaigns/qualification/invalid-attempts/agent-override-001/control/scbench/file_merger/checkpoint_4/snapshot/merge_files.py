#!/usr/bin/env python3
"""Merge heterogeneous delimited, JSON Lines, and Parquet files."""
import argparse, csv, datetime as dt, heapq, io, json, math, os, pickle, re, shutil, sys, tempfile, gzip
from urllib.parse import quote

TYPES = {"string", "int", "float", "bool", "date", "timestamp"}
BUILTIN_ALIASES = {"integer":"int", "long":"int", "double":"float", "number":"float",
                    "boolean":"bool", "datetime":"timestamp", "timestamptz":"timestamp",
                    "text":"string", "varchar":"string"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

class DataError(Exception):
    def __init__(self, message, code=5): self.code, self.message = code, message; super().__init__(message)

def bool_value(v):
    x = str(v).strip().lower()
    if x in {"true", "t", "yes", "y", "1"}: return True
    if x in {"false", "f", "no", "n", "0"}: return False
    raise ValueError("not a boolean")
def parse_date(v):
    if not DATE_RE.fullmatch(str(v)): raise ValueError("not an ISO date")
    return dt.date.fromisoformat(str(v))
def parse_timestamp(v):
    s = str(v).strip()
    if not any(x in s for x in ("T", "t", " ")): raise ValueError("not an ISO timestamp")
    x = dt.datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
    return (x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)
def classify(v):
    if v is None: return None
    if isinstance(v, bool): return "bool"
    if isinstance(v, int) and not isinstance(v, bool): return "int"
    if isinstance(v, float): return "float"
    s = str(v)
    for t, fn in (("timestamp", parse_timestamp), ("date", parse_date), ("bool", bool_value)):
        try: fn(s); return t
        except (ValueError, TypeError, OverflowError): pass
    try: int(s.strip()); return "int"
    except ValueError: pass
    try:
        if math.isfinite(float(s.strip())): return "float"
    except ValueError: pass
    return "string"

def combined(types, loose=False):
    types = [x for x in types if x]
    if not types: return "string"
    if loose and len(set(types)) > 1:
        if set(types) <= {"int", "float"}: return "float"
        return "string"
    if set(types) <= {"int", "float"}: return "int" if set(types)=={"int"} else "float"
    return types[0] if len(set(types)) == 1 else "string"

def read_aliases(spec):
    aliases = dict(BUILTIN_ALIASES)
    if not spec: return aliases
    try:
        try:
            with open(spec, encoding="utf-8") as f: doc=json.load(f)
        except (OSError, UnicodeError): doc=json.loads(spec)
        extra=doc.get("aliases") if isinstance(doc,dict) else None
        if not isinstance(extra,dict): raise ValueError("alias file must contain an 'aliases' object")
        aliases.update({str(k).lower():v for k,v in extra.items()})
    except Exception as e: raise DataError(f"cannot read type aliases {spec}: {e}",2)
    return aliases

def read_schema(spec, alias_spec=None):
    try:
        try:
            with open(spec, encoding="utf-8") as f: doc = json.load(f)
        except (OSError, UnicodeError): doc = json.loads(spec)
    except (OSError, UnicodeError, json.JSONDecodeError) as e: raise DataError(f"cannot read schema {spec}: {e}", 2)
    cols = doc.get("columns") if isinstance(doc, dict) else None
    if not isinstance(cols, list) or not cols: raise DataError("schema must contain a non-empty 'columns' array", 2)
    aliases=read_aliases(alias_spec); resolving=set(); resolved={}
    def type_of(x):
        if isinstance(x,str):
            s=x.strip().lower()
            m=re.fullmatch(r"(array|list)\s*<(.+)>",s)
            if m: return {"array":type_of(m.group(2))}
            m=re.fullmatch(r"map\s*<\s*string\s*,\s*(.+)>\s*",s)
            if m: return {"map":type_of(m.group(1))}
            if s=="json": return {"json":True}
            if s in TYPES:return s
            if s in aliases:
                if s in resolving: raise DataError(f"type alias cycle involving {s}",2)
                if s not in resolved:
                    resolving.add(s); resolved[s]=type_of(aliases[s]); resolving.remove(s)
                return resolved[s]
            raise DataError(f"unknown type: {x}",2)
        if isinstance(x,dict):
            if set(x)=={"struct"}:
                fs=x["struct"].get("fields") if isinstance(x["struct"],dict) else None
                if not isinstance(fs,list): raise DataError("struct type needs a fields array",2)
                out=[]; names=set()
                for f in fs:
                    if not isinstance(f,dict) or not isinstance(f.get("name"),str): raise DataError("struct fields need names and types",2)
                    if f["name"] in names: raise DataError(f"duplicate struct field: {f['name']}",2)
                    names.add(f["name"]); out.append((f["name"],type_of(f.get("type"))))
                return {"struct":out}
            if set(x)=={"array"} and isinstance(x["array"],dict) and "element" in x["array"]:
                return {"array":type_of(x["array"]["element"])}
            if set(x)=={"map"} and isinstance(x["map"],dict) and str(x["map"].get("key","string")).lower()=="string" and "value" in x["map"]:
                return {"map":type_of(x["map"]["value"])}
        raise DataError("invalid schema type",2)
    # Validate the complete alias graph up front so cycles and bad targets are
    # reported deterministically even when an alias is not used by a column.
    for alias_name in aliases:
        type_of(alias_name)
    out=[]; seen=set()
    for c in cols:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str): raise DataError("each schema column needs a string name and a valid type", 2)
        if c["name"] in seen: raise DataError(f"duplicate schema column: {c['name']}", 2)
        seen.add(c["name"]); out.append((c["name"], type_of(c.get("type"))))
    return out

def detect(path, forced, compression):
    name = path.lower(); gz = name.endswith(".gz")
    if compression == "gzip" and not gz: raise DataError(f"compression mismatch: {path}")
    if compression == "none" and gz: raise DataError(f"compression mismatch: {path}")
    base = name[:-3] if gz else name
    ext = {".csv":"csv", ".tsv":"tsv", ".jsonl":"jsonl", ".ndjson":"jsonl", ".parquet":"parquet"}.get(os.path.splitext(base)[1])
    try:
        with (gzip.open(path, "rb") if gz else open(path, "rb")) as f: magic=f.read(4)
    except OSError as e: raise DataError(f"cannot read {path}: {e}")
    if forced != "auto":
        fmt=forced
    elif ext: fmt=ext
    elif magic == b"PAR1": fmt="parquet"
    else: raise DataError(f"cannot detect input format: {path}", 2)
    if forced == "auto" and fmt == "parquet" and magic != b"PAR1": raise DataError(f"input format mismatch: {path}")
    return fmt, gz

def flat_json(obj, path, line, allow_nested=False):
    if not isinstance(obj, dict) or (not allow_nested and any(isinstance(v, (dict,list)) for v in obj.values())):
        raise DataError(f"{path}: line {line}: JSON object must be flat", 6)
    return obj

def open_text(path, gz):
    try: return gzip.open(path, "rt", encoding="utf-8", newline="") if gz else open(path, newline="", encoding="utf-8")
    except OSError as e: raise DataError(f"cannot read {path}: {e}")

def source_info(path, fmt, gz, dialect, loose, allow_nested=False):
    names=[]; observations={}; rows_factory=None
    if fmt in {"csv","tsv"}:
        d=dict(dialect); d.update(delimiter="\t" if fmt=="tsv" else ",", quoting=csv.QUOTE_NONE if fmt=="tsv" else csv.QUOTE_MINIMAL)
        if fmt=="tsv": d.update(quotechar='\0', escapechar='\\')
        def scan():
            with open_text(path,gz) as f:
                try: r=csv.reader(f, **d); h=next(r,None)
                except (UnicodeError,csv.Error) as e: raise DataError(f"cannot read {path}: {e}")
                if h is None: raise DataError(f"empty input: {path}")
                if len(set(h))!=len(h): raise DataError(f"duplicate column in header: {path}")
                for row in r:
                    if fmt=="tsv" and any("\t" in x for x in row): raise DataError(f"literal tab in TSV field: {path}")
                    if fmt=="tsv" and len(row)!=len(h): raise DataError(f"invalid TSV field count: {path}")
                    yield row, r.line_num
        with open_text(path,gz) as f:
            try: h=next(csv.reader(f, **d),None)
            except (UnicodeError,csv.Error) as e: raise DataError(f"cannot read {path}: {e}")
        if h is None: raise DataError(f"empty input: {path}")
        if len(set(h))!=len(h): raise DataError(f"duplicate column in header: {path}")
        names=list(h); observations={n:set() for n in h}
        for row,_ in scan():
            for i,n in enumerate(h):
                if i<len(row) and row[i]!="": observations[n].add(classify(row[i]))
        def rows():
            # Reopen for the actual pass; the first pass is intentionally streaming.
            for row,line in scan(): yield dict((n,row[i] if i<len(row) else "") for i,n in enumerate(h)), line
        rows_factory=rows
    elif fmt=="jsonl":
        def scanj():
            with open_text(path,gz) as f:
                for no,line in enumerate(f,1):
                    if not line.strip(): continue
                    try: obj=json.loads(line)
                    except (ValueError,UnicodeError) as e: raise DataError(f"{path}: line {no}: invalid JSON: {e}")
                    yield flat_json(obj,path,no,allow_nested), no
        for obj,_ in scanj():
            for n,v in obj.items():
                if n not in observations: observations[n]=set(); names.append(n)
                if v is not None: observations[n].add(classify(v))
        def rows(): yield from scanj()
        rows_factory=rows
    else:
        try: import pyarrow as pa; import pyarrow.parquet as pq
        except ImportError: raise DataError("Parquet input requires pyarrow", 5)
        if gz: raise DataError(f"gzip Parquet is unsupported: {path}")
        try:
            pf=pq.ParquetFile(path); names=pf.schema_arrow.names
            for field in pf.schema_arrow:
                if pa.types.is_nested(field.type) and not allow_nested: raise DataError(f"nested Parquet field: {field.name}", 6)
                observations[field.name]={parquet_type(field.type)}
        except DataError: raise
        except Exception as e: raise DataError(f"cannot read {path}: {e}")
        def rows():
            try:
                for batch in pf.iter_batches():
                    for obj in batch.to_pylist(): yield obj, 0
            except Exception as e: raise DataError(f"cannot read {path}: {e}")
        rows_factory=rows
    return names, observations, rows_factory

def parquet_type(t):
    s=str(t)
    if "bool" in s: return "bool"
    if "int" in s: return "int"
    if "float" in s or "double" in s or "decimal" in s: return "float"
    if "date" in s: return "date"
    if "timestamp" in s: return "timestamp"
    return "string"

def cast(v, typ, nested_error=None):
    if v is None or v=="": return None
    if isinstance(typ,dict):
        if "json" in typ:
            return v
        if isinstance(v,str):
            try: v=json.loads(v)
            except Exception: raise ValueError(f'invalid JSON literal {v!r}')
        if "struct" in typ:
            if not isinstance(v,dict): raise ValueError("expected object")
            out={}
            for n,t in typ["struct"]:
                try: out[n]=cast(v.get(n),t,nested_error)
                except (ValueError,TypeError,OverflowError):
                    if nested_error=="keep-string": out[n]=v.get(n)
                    elif nested_error=="coerce-null": out[n]=None
                    else: raise
            return out
        if "array" in typ:
            if not isinstance(v,list): raise ValueError("expected array")
            out=[]
            for x in v:
                try: out.append(cast(x,typ["array"],nested_error))
                except (ValueError,TypeError,OverflowError):
                    if nested_error=="keep-string": out.append(x)
                    elif nested_error=="coerce-null": out.append(None)
                    else: raise
            return out
        if "map" in typ:
            if not isinstance(v,dict): raise ValueError("expected object map")
            out={}
            for k,x in v.items():
                try: out[str(k)]=cast(x,typ["map"],nested_error)
                except (ValueError,TypeError,OverflowError):
                    if nested_error=="keep-string": out[str(k)]=x
                    elif nested_error=="coerce-null": out[str(k)]=None
                    else: raise
            return out
        raise ValueError("unknown nested type")
    if isinstance(v,(dict,list)): raise ValueError("expected primitive")
    if typ=="string": return str(v)
    if typ=="int": return int(v)
    if typ=="float":
        x=float(v)
        if not math.isfinite(x): raise ValueError("non-finite float")
        return x
    if typ=="bool": return v if isinstance(v,bool) else bool_value(v)
    if typ=="date": return v if isinstance(v,dt.date) and not isinstance(v,dt.datetime) else parse_date(v)
    if typ=="timestamp": return v if isinstance(v,dt.datetime) else parse_timestamp(v)
    raise ValueError("unknown type")
def render(v,t):
    if v is None:return None
    if isinstance(t,dict):
        if "json" in t: return json.dumps(v,separators=(",",":"),ensure_ascii=False)
        def clean(x, typ):
            if x is None:return None
            if isinstance(typ,str):
                if typ in {"date","timestamp"}: return render(x,typ)
                if typ=="bool": return bool(x)
                return x
            if "json" in typ:return x
            if "struct" in typ:return {n:clean(x.get(n),q) for n,q in typ["struct"]} if isinstance(x,dict) else x
            if "array" in typ:return [clean(y,typ["array"]) for y in x] if isinstance(x,list) else x
            if "map" in typ:return {k:clean(x[k],typ["map"]) for k in sorted(x)} if isinstance(x,dict) else x
        return json.dumps(clean(v,t),separators=(",",":"),ensure_ascii=False)
    if t=="bool":return "true" if v else "false"
    if t in {"date","timestamp"}:
        if t=="date": return v.isoformat() if isinstance(v,dt.date) else str(v)
        if not isinstance(v,dt.datetime): return str(v)
        s=v.isoformat(timespec="microseconds").replace("+00:00","Z")
        if "." in s: s=s.rstrip("0").rstrip(".")+"Z"
        return s
    return str(v)

def type_name(t):
    if isinstance(t,str): return t
    if "json" in t:return "json"
    if "array" in t:return "array"
    if "map" in t:return "map"
    return "struct"

PATH_TOKEN=re.compile(r'([^\.\[\]]+)|\[\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|(\d+))\s*\]')
def path_tokens(path):
    out=[]; pos=0
    for m in PATH_TOKEN.finditer(path):
        if m.start()!=pos and not (path[pos:m.start()]=="."): raise DataError(f"invalid field path: {path}",3)
        if m.group(1): out.append(m.group(1))
        elif m.group(4) is not None: out.append(int(m.group(4)))
        else: out.append(m.group(2) if m.group(2) is not None else m.group(3))
        pos=m.end()
    if pos!=len(path) or not out: raise DataError(f"invalid field path: {path}",3)
    return out

def path_type(schema, path):
    toks=path_tokens(path); root={n:t for n,t in schema}; cur=None
    for i,tok in enumerate(toks):
        if i==0:
            if not isinstance(tok,str) or tok not in root: raise DataError(f"key column \"{path}\" is not present in resolved schema",3)
            cur=root[tok]; continue
        if isinstance(cur,dict) and "struct" in cur:
            found=dict(cur["struct"])
            if not isinstance(tok,str) or tok not in found: raise DataError(f"key column \"{path}\" does not resolve to a primitive",3)
            cur=found[tok]
        elif isinstance(cur,dict) and "array" in cur and ((isinstance(tok,int)) or (isinstance(tok,str) and tok.isdigit())):
            toks[i]=int(tok); cur=cur["array"]
        elif isinstance(cur,dict) and "map" in cur and isinstance(tok,str): cur=cur["map"]
        else: raise DataError(f"key column \"{path}\" does not resolve to a primitive",3)
    if not isinstance(cur,str): raise DataError(f"key column \"{path}\" does not resolve to a primitive",3)
    return toks,cur

def path_value(obj,toks):
    cur=obj
    for tok in toks:
        if cur is None:return None
        if isinstance(tok,int):
            if not isinstance(cur,list) or tok>=len(cur):return None
            cur=cur[tok]
        elif isinstance(cur,dict): cur=cur.get(tok)
        else:return None
    return cur

def column_path_value(vals, column_index, toks):
    return vals[column_index] if len(toks)==1 else path_value(vals[column_index],toks[1:])

class K:
    def __init__(self,p):self.p=p
    def __lt__(self,o):
        for (an,a,d),(bn,b,_) in zip(self.p,o.p):
            if an!=bn:return an > bn if not d else an < bn
            if not an and a!=b:
                try: return a>b if d else a<b
                except TypeError:
                    return str(a)>str(b) if d else str(a)<str(b)
        return False
def key(vals, idx, desc): return K([(vals[i] is None, vals[i], desc) for i in idx])

def encoded_partition_value(value, typ):
    """Render a resolved value and escape it for one Hive path component."""
    return quote(render(value, typ) if value is not None else "_null", safe="A-Za-z0-9._-")

def csv_bytes(values, schema, args):
    """Serialize exactly one CSV record, returning its on-disk UTF-8 bytes."""
    s = io.StringIO(newline="")
    csv.writer(s, delimiter=",", quotechar=args.csv_quotechar,
               escapechar=args.csv_escapechar,
               doublequote=args.csv_escapechar is None,
               lineterminator="\n").writerow(values)
    return s.getvalue().encode("utf-8")

def atomic_replace_directory(temp_output, output):
    """Publish a completed directory while retaining the old one on failure."""
    if not os.path.exists(output):
        os.replace(temp_output, output)
        return
    if not os.path.isdir(output):
        raise OSError(f"output path is not a directory: {output}")
    parent, name = os.path.dirname(os.path.abspath(output)), os.path.basename(output)
    backup = tempfile.mkdtemp(prefix=f".{name}.old-", dir=parent)
    os.rmdir(backup)
    os.replace(output, backup)
    try:
        os.replace(temp_output, output)
    except Exception:
        os.replace(backup, output)
        raise
    shutil.rmtree(backup)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",required=True); p.add_argument("--key",required=True)
    p.add_argument("--partition-by"); p.add_argument("--max-rows-per-file",type=int)
    p.add_argument("--max-bytes-per-file",type=int); p.add_argument("--desc",action="store_true")
    p.add_argument("--schema"); p.add_argument("--type-alias-file"); p.add_argument("--infer",choices=("strict","loose"),default="strict")
    p.add_argument("--schema-strategy",choices=("authoritative","consensus","union"),default="authoritative")
    p.add_argument("--on-type-error",choices=("coerce-null","fail","keep-string"),default="coerce-null")
    p.add_argument("--memory-limit-mb",type=int); p.add_argument("--temp-dir")
    p.add_argument("--csv-quotechar",default='"'); p.add_argument("--csv-escapechar")
    p.add_argument("--csv-null-literal",default="")
    p.add_argument("--input-format",choices=("auto","csv","tsv","jsonl","parquet"),default="auto")
    p.add_argument("--compression",choices=("auto","none","gzip"),default="auto")
    p.add_argument("--parquet-row-group-bytes",type=int); p.add_argument("inputs",nargs="+"); a=p.parse_args(argv)
    if a.memory_limit_mb is not None and a.memory_limit_mb<=0:p.error("--memory-limit-mb must be positive")
    if a.max_rows_per_file is not None and a.max_rows_per_file<=0:p.error("--max-rows-per-file must be positive")
    if a.max_bytes_per_file is not None and a.max_bytes_per_file<=0:p.error("--max-bytes-per-file must be positive")
    if len(a.csv_quotechar)!=1 or (a.csv_escapechar is not None and len(a.csv_escapechar)!=1):p.error("CSV quote and escape characters must each be one character")
    if a.parquet_row_group_bytes is not None and a.parquet_row_group_bytes<=0:p.error("--parquet-row-group-bytes must be positive")
    partition_cols=[x.strip() for x in (a.partition_by or "").split(",") if x.strip()]
    partitioned=bool(partition_cols or a.max_rows_per_file is not None or a.max_bytes_per_file is not None)
    if partitioned and a.output=="-": p.error("partitioned output requires a directory path")
    if a.output!="-" and os.path.abspath(a.output) in {os.path.abspath(x) for x in a.inputs}:p.error("output path must not be one of the input paths")
    dialect={"quotechar":a.csv_quotechar,"escapechar":a.csv_escapechar,"doublequote":a.csv_escapechar is None}
    try:
        schema=read_schema(a.schema,a.type_alias_file) if a.schema else None
        infos=[]
        for path in a.inputs:
            fmt,gz=detect(path,a.input_format,a.compression); infos.append((path,fmt,gz,source_info(path,fmt,gz,dialect,a.infer=="loose",schema is not None)))
        if schema is None:
            names=sorted({n for _,_,_,(ns,obs,rf) in infos for n in ns}); schema=[]
            for n in names:
                per=[combined(obs[n],a.infer=="loose") for _,_,_,(_,obs,_) in infos if n in obs and obs[n]]
                if not per: typ="string"
                elif a.schema_strategy=="consensus": typ=max(set(per),key=lambda x:(per.count(x),x))
                elif a.schema_strategy=="union": typ=combined(per,False)
                else: typ=next((x for x in per if x!="string"),"string")
                schema.append((n,typ))
        keys=[x.strip() for x in a.key.split(",") if x.strip()]
        if not keys: raise DataError("--key must name at least one column",3)
        if len(set(keys)) != len(keys): raise DataError("duplicate key column",3)
        if len(set(partition_cols)) != len(partition_cols): raise DataError("duplicate partition column",3)
        idx={n:i for i,(n,_) in enumerate(schema)}
        key_specs=[path_type(schema,k) for k in keys]
        part_specs=[path_type(schema,k) for k in partition_cols]
        lim=max(1,a.memory_limit_mb or 64)*1024*1024; runs=[]; chunk=[]; size=0; seq=0
        temp_parent=a.temp_dir
        with tempfile.TemporaryDirectory(prefix="csv-merge-",dir=temp_parent) as td:
            def flush():
                nonlocal chunk,size
                if not chunk:return
                chunk.sort(key=lambda x:(x[0],x[1])); fd,n=tempfile.mkstemp(prefix="csvrun-",dir=td)
                with os.fdopen(fd,"wb") as f:
                    for x in chunk:pickle.dump(x,f,pickle.HIGHEST_PROTOCOL)
                runs.append(n);chunk=[];size=0
            for path,fmt,gz,(_,_,rf) in infos:
                for obj,line in rf():
                    vals=[]
                    for n,t in schema:
                        raw=obj.get(n) if isinstance(obj,dict) else None
                        if isinstance(t,dict) and "json" in t and fmt in {"csv","tsv"} and raw not in (None,""):
                            try: raw=json.loads(raw)
                            except Exception as e:
                                if a.on_type_error=="fail": raise DataError(f'cannot cast "{raw}" to json in field "{n}" (file={path} line={line})',4)
                                raw = raw if a.on_type_error=="keep-string" else None
                        try:v=cast(raw,t,a.on_type_error)
                        except (ValueError,TypeError,OverflowError) as e:
                            if a.on_type_error=="fail":raise DataError(f'cannot cast "{obj.get(n) if isinstance(obj,dict) else None}" to {type_name(t)} in field "{n}" (file={path} line={line})',4)
                            v=raw if a.on_type_error=="keep-string" else None
                        vals.append(v)
                    kvals=[column_path_value(vals,idx[spec[0][0]],spec[0]) for spec in key_specs]
                    chunk.append((key(kvals,list(range(len(kvals))),a.desc),seq,vals));seq+=1;size+=sum(len(str(x)) for x in vals)+128
                    if size>=lim:flush()
            flush(); streams=[open(n,"rb") for n in runs]; heap=[]
            output_tmp=None
            try:
                for i,f in enumerate(streams):
                    try:x=pickle.load(f);heapq.heappush(heap,(x[0],x[1],i,x[2]))
                    except EOFError:pass
                if not partitioned:
                    if a.output=="-":
                        out=sys.stdout; close=False; tmp=None
                    else:
                        fd,tmp=tempfile.mkstemp(prefix=".merge-",dir=os.path.dirname(os.path.abspath(a.output)) or ".")
                        out=os.fdopen(fd,"w",newline="",encoding="utf-8"); close=True
                    try:
                        w=csv.writer(out,delimiter=",",quotechar=a.csv_quotechar,escapechar=a.csv_escapechar,doublequote=a.csv_escapechar is None,lineterminator="\n")
                        w.writerow([n for n,_ in schema])
                        while heap:
                            _,_,i,vals=heapq.heappop(heap)
                            w.writerow([render(v,t) if v is not None else a.csv_null_literal for v,(_,t) in zip(vals,schema)])
                            try:x=pickle.load(streams[i]);heapq.heappush(heap,(x[0],x[1],i,x[2]))
                            except EOFError:pass
                        if close: out.close(); os.replace(tmp,a.output)
                    except Exception:
                        if close:
                            out.close()
                            try: os.unlink(tmp)
                            except OSError: pass
                        raise
                else:
                    parent=os.path.dirname(os.path.abspath(a.output)) or "."
                    os.makedirs(parent,exist_ok=True)
                    output_tmp=tempfile.mkdtemp(prefix=f".{os.path.basename(os.path.abspath(a.output))}.tmp-",dir=parent)
                    states={}; header=csv_bytes([n for n,_ in schema],schema,a)
                    pidx=[idx[spec[0][0]] for spec in part_specs]
                    def state_for(part):
                        state=states.get(part)
                        if state is not None:return state
                        directory=output_tmp
                        for col,value in zip(partition_cols,part):
                            directory=os.path.join(directory,col+"="+encoded_partition_value(value,part_specs[partition_cols.index(col)][1]))
                        os.makedirs(directory,exist_ok=True)
                        state=[0,0,0,None,directory]
                        states[part]=state
                        return state
                    def next_file(state):
                        if state[3] is not None: state[3].close()
                        path=os.path.join(state[4],f"part-{state[0]:05d}.csv")
                        state[0]+=1; state[3]=open(path,"wb"); state[1]=len(header); state[2]=0; state[3].write(header)
                    while heap:
                        _,_,i,vals=heapq.heappop(heap)
                        part=tuple(column_path_value(vals,j,spec[0]) for j,spec in zip(pidx,part_specs)); state=state_for(part)
                        row=csv_bytes([render(v,t) if v is not None else a.csv_null_literal for v,(_,t) in zip(vals,schema)],schema,a)
                        if state[3] is None: next_file(state)
                        if state[2] and ((a.max_rows_per_file is not None and state[2]>=a.max_rows_per_file) or (a.max_bytes_per_file is not None and state[1]+len(row)>a.max_bytes_per_file)):
                            next_file(state)
                        state[3].write(row); state[1]+=len(row); state[2]+=1
                        try:x=pickle.load(streams[i]);heapq.heappush(heap,(x[0],x[1],i,x[2]))
                        except EOFError:pass
                    for state in states.values():
                        if state[3] is not None: state[3].close()
                    atomic_replace_directory(output_tmp,a.output); output_tmp=None
            finally:
                for f in streams:f.close()
                if output_tmp is not None: shutil.rmtree(output_tmp,ignore_errors=True)
    except DataError as e: print(f"merge_files.py: {e.message}",file=sys.stderr); return e.code
    except (OSError,pickle.PickleError) as e: print(f"merge_files.py: {e}",file=sys.stderr); return 5
    return 0
if __name__=="__main__":sys.exit(main())

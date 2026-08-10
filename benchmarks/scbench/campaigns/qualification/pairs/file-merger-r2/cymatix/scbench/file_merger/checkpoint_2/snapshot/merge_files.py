#!/usr/bin/env python3
"""Merge heterogeneous tabular files into one sorted CSV."""
import argparse, csv, datetime as dt, gzip, heapq, json, math, os, pickle, sys, tempfile

TYPES = {"string", "int", "float", "bool", "date", "timestamp"}
PRIORITY = {"timestamp": 0, "date": 1, "bool": 2, "int": 3, "float": 4, "string": 5}

class MergeError(Exception):
    def __init__(self, message, code=1): self.code, self.message = code, message

def parse_value(value, typ):
    if value is None or value == "": return None
    if typ == "string": return value if isinstance(value, str) else str(value)
    if typ == "int":
        if isinstance(value, bool): raise ValueError("invalid integer")
        if isinstance(value, int): return value
        if isinstance(value, float) and value.is_integer(): return int(value)
        s = str(value)
        if s.strip() != s or not s or (s[0] in "+-" and len(s) == 1): raise ValueError("invalid integer")
        return int(s, 10)
    if typ == "float":
        x = float(value)
        if not math.isfinite(x): raise ValueError("non-finite float")
        return x
    if typ == "bool":
        if isinstance(value, bool): return value
        s = str(value).strip().lower()
        if s in {"1", "true", "t", "yes", "y"}: return True
        if s in {"0", "false", "f", "no", "n"}: return False
        raise ValueError("invalid boolean")
    s = str(value)
    if typ == "date":
        x = dt.date.fromisoformat(s)
        if x.isoformat() != s: raise ValueError("date is not YYYY-MM-DD")
        return x
    if typ == "timestamp":
        if "T" not in s and "t" not in s and " " not in s: raise ValueError("timestamp requires a time component")
        x = dt.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s)
        return (x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)
    raise ValueError("unknown type")

def candidates(v):
    if v is None or v == "": return set()
    out = {"string"}
    for t in ("timestamp", "date", "bool", "int", "float"):
        try: parse_value(v, t); out.add(t)
        except (ValueError, OverflowError, TypeError): pass
    return out

def classify(v):
    c = candidates(v) - {"string"}
    return min(c, key=lambda x: PRIORITY[x]) if c else "string"

def typed_kind(v, fmt):
    """Return the type implied by a typed source, preserving JSON strings."""
    if fmt not in ("jsonl", "parquet"):
        return classify(v)
    if v is None: return None
    if isinstance(v, bool): return "bool"
    if isinstance(v, int): return "int"
    if isinstance(v, float): return "float"
    if isinstance(v, dt.datetime): return "timestamp"
    if isinstance(v, dt.date): return "date"
    return "string"

def type_candidates(v, kind):
    if kind is None: return set()
    if kind == "string": return {"string"}
    if kind == "int": return {"int", "float"}
    if kind == "float": return {"float"}
    if kind == "bool": return {"bool"}
    return {kind}

def canonical(v, typ):
    if v is None: return ""
    if typ == "string": return v if isinstance(v, str) else str(v)
    if typ == "int": return str(v)
    if typ == "float": return repr(v)
    if typ == "bool": return "true" if v else "false"
    if typ == "date": return v.isoformat()
    return v.isoformat(timespec="microseconds").replace("+00:00", "Z").replace(".000000Z", "Z")

def detect(path, requested, compression):
    name = path.lower()
    gz_name = name.endswith(".gz")
    gz = gz_name if compression == "auto" else compression == "gzip"
    try:
        with open(path, "rb") as f: magic = f.read(4)
    except OSError as e: raise MergeError(str(e), 1)
    if compression == "gzip" and magic[:2] != b"\x1f\x8b": raise MergeError(f"compression mismatch: {path}", 5)
    if compression == "none" and magic[:2] == b"\x1f\x8b": raise MergeError(f"compression mismatch: {path}", 5)
    if compression == "auto" and gz_name and magic[:2] != b"\x1f\x8b": raise MergeError(f"compression mismatch: {path}", 5)
    base = name[:-3] if gz_name else name
    ext = {".csv":"csv", ".tsv":"tsv", ".jsonl":"jsonl", ".ndjson":"jsonl", ".parquet":"parquet"}.get(os.path.splitext(base)[1])
    fmt = requested if requested != "auto" else ext
    if fmt is None and magic == b"PAR1": fmt = "parquet"
    if fmt is None: raise MergeError(f"cannot detect input format: {path}", 2)
    if requested != "auto" and requested == "parquet" and magic != b"PAR1":
        raise MergeError(f"input format mismatch: {path}", 5)
    if requested == "parquet" and gz: raise MergeError(f"compression mismatch: {path}", 5)
    return fmt, gz

def load_schema(path):
    with open(path, encoding="utf-8") as f: data = json.load(f)
    cols = data.get("columns")
    if not isinstance(cols, list) or not cols: raise MergeError("schema columns must be a non-empty array")
    result=[]; seen=set()
    for c in cols:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or c.get("type") not in TYPES: raise MergeError("each schema column needs a name and valid type")
        if c["name"] in seen: raise MergeError("duplicate schema column: " + c["name"])
        seen.add(c["name"]); result.append((c["name"], c["type"]))
    return result

def open_text(path, gz):
    return gzip.open(path, "rt", encoding="utf-8", newline="") if gz else open(path, "r", encoding="utf-8", newline="")

def parquet_rows(path):
    try:
        import pyarrow.parquet as pq
        import pyarrow.types as patypes
        pf = pq.ParquetFile(path)
        names = pf.schema_arrow.names
        for field in pf.schema_arrow:
            if patypes.is_nested(field.type) or str(field.type).startswith(("list", "struct", "map")): raise MergeError(f"nested Parquet field: {field.name}", 6)
        for batch in pf.iter_batches():
            for row in batch.to_pylist(): yield names, row
    except ImportError: raise MergeError("Parquet support requires pyarrow", 1)
    except MergeError: raise
    except Exception as e: raise MergeError(str(e), 1)

def records(path, fmt, gz, args):
    if fmt == "parquet":
        for names, row in parquet_rows(path): yield names, row
        return
    f = open_text(path, gz)
    try:
        if fmt in ("csv", "tsv"):
            delimiter = "," if fmt == "csv" else "\t"
            rd = csv.reader(f, delimiter=delimiter, quotechar=args.csv_quotechar if fmt == "csv" else '\0', quoting=csv.QUOTE_MINIMAL if fmt == "csv" else csv.QUOTE_NONE, escapechar=args.csv_escapechar)
            try: header = next(rd)
            except StopIteration: raise MergeError(f"missing header in {path}", 5)
            if len(set(header)) != len(header): raise MergeError(f"duplicate header in {path}", 5)
            for row in rd:
                if len(row) != len(header): raise MergeError(f"row has wrong number of fields in {path}", 5)
                yield header, dict(zip(header, row))
        else:
            header = None
            for lineno, line in enumerate(f, 1):
                if not line.strip(): continue
                try: obj = json.loads(line)
                except json.JSONDecodeError as e: raise MergeError(f"{path}:{lineno}: {e}", 5)
                if not isinstance(obj, dict) or any(isinstance(v, (dict,list)) for v in obj.values()): raise MergeError(f"nested JSONL value in {path}:{lineno}", 6)
                names = list(obj)
                if header is None: header = names
                yield names, obj
    finally: f.close()

def discover(inputs, args):
    names=set(); files=[]; observations={}; observed_types={}; supports={}; authoritative={}
    for path in inputs:
        fmt,gz=detect(path,args.input_format,args.compression); files.append((path,fmt,gz)); filetypes={}
        for header, row in records(path,fmt,gz,args):
            names.update(header)
            for n,v in row.items():
                if v is not None and v != "":
                    kind = typed_kind(v, fmt)
                    possible = type_candidates(v, kind)
                    observations[n] = possible if n not in observations else observations[n] & possible
                    if kind: observed_types.setdefault(n, set()).add(kind); filetypes.setdefault(n,set()).add(kind)
        for n,t in filetypes.items(): supports.setdefault(n,[]).append(t)
        if fmt in ("jsonl", "parquet"):
            for n,t in filetypes.items():
                if t: authoritative.setdefault(n, []).append((0 if fmt == "parquet" else 1, t))
    schema=[]
    for n in sorted(names):
        common=observations.get(n, {"string"})
        if args.infer == "loose":
            typ=min(common or {"string"},key=lambda x: PRIORITY[x])
        else:
            typed=observed_types.get(n, set())
            typ=next(iter(typed)) if len(typed)==1 and common else "string"
        if args.schema_strategy == "authoritative" and authoritative.get(n):
            _, choices = min(authoritative[n], key=lambda x: x[0])
            typ=min(choices, key=lambda x: PRIORITY[x])
        elif args.schema_strategy == "consensus" and supports.get(n):
            counts={t:sum(t in s for s in supports[n]) for t in TYPES}; best=max(counts.values())
            if best: typ=min((t for t,c in counts.items() if c==best),key=lambda x: PRIORITY[x])
        elif args.schema_strategy == "union" and n in observations:
            typ=min(common,key=lambda x: PRIORITY[x]) if common else "string"
        schema.append((n,typ))
    return schema,files

def sortable(v,t,desc):
    if v is None: return (1 if not desc else 0, 0)
    if t in ("int","float"): x=-v if desc else v
    elif t=="bool": x=-(int(v)) if desc else int(v)
    elif t=="date": x=-v.toordinal() if desc else v.toordinal()
    elif t=="timestamp": x=-int(v.timestamp()*1000000) if desc else int(v.timestamp()*1000000)
    else:
        b=v.encode(); x=bytes(255-z for z in b) if desc else b
    return (1 if desc else 0,x)

def run(args):
    if len(args.csv_quotechar)!=1 or (args.csv_escapechar is not None and len(args.csv_escapechar)!=1): raise MergeError("CSV quote and escape characters must be one character")
    if args.schema: schema=load_schema(args.schema); files=[(p,*detect(p,args.input_format,args.compression)) for p in args.inputs]
    else: schema,files=discover(args.inputs,args)
    by=dict(schema); keys=args.key.split(",") if args.key else []
    if not keys or any(k not in by for k in keys): raise MergeError("every key must be present in the resolved schema",3)
    limit=max(1,args.memory_limit_mb or 64)*1024*1024; chunk_limit=max(1024*1024,limit//2)
    target=args.output
    with tempfile.TemporaryDirectory(dir=args.temp_dir) as td:
        runs=[]; chunk=[]; used=0; seq=0
        def flush():
            nonlocal chunk,used
            if not chunk:return
            chunk.sort(key=lambda x:(x[0],x[1])); fn=os.path.join(td,f"run-{len(runs):08d}")
            with open(fn,"wb") as f:
                for x in chunk: pickle.dump(x,f,pickle.HIGHEST_PROTOCOL)
            runs.append(fn); chunk=[]; used=0
        for path,fmt,gz in files:
            for header,row in records(path,fmt,gz,args):
                rendered=[]; parsed={}
                for n,t in schema:
                    raw=row.get(n)
                    try: val=parse_value(raw,t); actual=t
                    except (ValueError,OverflowError,TypeError) as e:
                        if args.on_type_error=="fail": raise MergeError(f"{path}: column {n}: {e}")
                        val=None if args.on_type_error=="coerce-null" else raw; actual="string"
                    parsed[n]=val; rendered.append(args.csv_null_literal if val is None else canonical(val,actual))
                sk=tuple(sortable(parsed[k],by[k] if not isinstance(parsed[k],str) else "string",args.desc) for k in keys)
                ent=(sk,seq,rendered); seq+=1; chunk.append(ent); used+=len(pickle.dumps(ent,protocol=pickle.HIGHEST_PROTOCOL))
                if used>=chunk_limit: flush()
        flush(); streams=[]
        def it(fn):
            with open(fn,"rb") as f:
                while True:
                    try: yield pickle.load(f)
                    except EOFError:return
        heap=[]
        for i,fn in enumerate(runs):
            s=it(fn); streams.append(s)
            try: x=next(s); heapq.heappush(heap,(x[0],x[1],i,x[2]))
            except StopIteration: pass
        if target=="-": out=sys.stdout; close=False
        else:
            parent=os.path.dirname(os.path.abspath(target)) or "."; fd,tmp=tempfile.mkstemp(prefix=".merge-",dir=parent); os.close(fd); out=open(tmp,"w",encoding="utf-8",newline=""); close=True
        try:
            w=csv.writer(out,quotechar=args.csv_quotechar,escapechar=args.csv_escapechar,doublequote=args.csv_escapechar is None,lineterminator="\n")
            w.writerow([n for n,_ in schema])
            while heap:
                _,_,i,row=heapq.heappop(heap); w.writerow(row)
                try: x=next(streams[i]); heapq.heappush(heap,(x[0],x[1],i,x[2]))
                except StopIteration: pass
        finally:
            if close: out.close()
        if target!="-": os.replace(tmp,target)

def main():
    p=argparse.ArgumentParser(); p.add_argument("--output",required=True); p.add_argument("--key",required=True); p.add_argument("--desc",action="store_true"); p.add_argument("--schema"); p.add_argument("--infer",choices=["strict","loose"],default="strict"); p.add_argument("--schema-strategy",choices=["authoritative","consensus","union"],default="authoritative"); p.add_argument("--on-type-error",choices=["coerce-null","fail","keep-string"],default="coerce-null"); p.add_argument("--memory-limit-mb",type=int); p.add_argument("--temp-dir"); p.add_argument("--csv-quotechar",default='"'); p.add_argument("--csv-escapechar"); p.add_argument("--csv-null-literal",default=""); p.add_argument("--input-format",choices=["auto","csv","tsv","jsonl","parquet"],default="auto"); p.add_argument("--compression",choices=["auto","none","gzip"],default="auto"); p.add_argument("--parquet-row-group-bytes",type=int,default=0); p.add_argument("inputs",nargs="+")
    try: run(p.parse_args()); return 0
    except MergeError as e: print(f"merge_files.py: {e.message}",file=sys.stderr); return e.code
    except (OSError,csv.Error,json.JSONDecodeError,ValueError) as e: print(f"merge_files.py: {e}",file=sys.stderr); return 1
if __name__=="__main__": sys.exit(main())

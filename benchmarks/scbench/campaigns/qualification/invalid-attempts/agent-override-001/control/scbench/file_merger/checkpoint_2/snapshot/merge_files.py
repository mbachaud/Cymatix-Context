#!/usr/bin/env python3
"""Merge heterogeneous delimited, JSON Lines, and Parquet files."""
import argparse, csv, datetime as dt, heapq, json, math, os, pickle, re, sys, tempfile, gzip

TYPES = {"string", "int", "float", "bool", "date", "timestamp"}
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

def read_schema(spec):
    try:
        try:
            with open(spec, encoding="utf-8") as f: doc = json.load(f)
        except (OSError, UnicodeError): doc = json.loads(spec)
    except (OSError, UnicodeError, json.JSONDecodeError) as e: raise DataError(f"cannot read schema {spec}: {e}", 2)
    cols = doc.get("columns") if isinstance(doc, dict) else None
    if not isinstance(cols, list) or not cols: raise DataError("schema must contain a non-empty 'columns' array", 2)
    out=[]; seen=set()
    for c in cols:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or c.get("type") not in TYPES:
            raise DataError("each schema column needs a string name and a valid type", 2)
        if c["name"] in seen: raise DataError(f"duplicate schema column: {c['name']}", 2)
        seen.add(c["name"]); out.append((c["name"], c["type"]))
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

def flat_json(obj, path, line):
    if not isinstance(obj, dict) or any(isinstance(v, (dict,list)) for v in obj.values()):
        raise DataError(f"{path}: line {line}: JSON object must be flat", 6)
    return obj

def open_text(path, gz):
    try: return gzip.open(path, "rt", encoding="utf-8", newline="") if gz else open(path, newline="", encoding="utf-8")
    except OSError as e: raise DataError(f"cannot read {path}: {e}")

def source_info(path, fmt, gz, dialect, loose):
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
        names=list(h); observations={n:[] for n in h}
        for row,_ in scan():
            for i,n in enumerate(h):
                if i<len(row) and row[i]!="": observations[n].append(classify(row[i]))
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
                    yield flat_json(obj,path,no), no
        for obj,_ in scanj():
            for n,v in obj.items():
                if n not in observations: observations[n]=[]; names.append(n)
                if v is not None: observations[n].append(classify(v))
        def rows(): yield from scanj()
        rows_factory=rows
    else:
        try: import pyarrow as pa; import pyarrow.parquet as pq
        except ImportError: raise DataError("Parquet input requires pyarrow", 5)
        if gz: raise DataError(f"gzip Parquet is unsupported: {path}")
        try:
            pf=pq.ParquetFile(path); names=pf.schema_arrow.names
            for field in pf.schema_arrow:
                if pa.types.is_nested(field.type): raise DataError(f"nested Parquet field: {field.name}", 6)
                observations[field.name]=[parquet_type(field.type)]
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

def cast(v, typ):
    if v is None or v=="": return None
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
    if t=="bool":return "true" if v else "false"
    if t in {"date","timestamp"}:
        if t=="date": return v.isoformat() if isinstance(v,dt.date) else str(v)
        if not isinstance(v,dt.datetime): return str(v)
        s=v.isoformat(timespec="microseconds").replace("+00:00","Z")
        if "." in s: s=s.rstrip("0").rstrip(".")+"Z"
        return s
    return str(v)

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

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--output",required=True); p.add_argument("--key",required=True); p.add_argument("--desc",action="store_true"); p.add_argument("--schema"); p.add_argument("--infer",choices=("strict","loose"),default="strict"); p.add_argument("--schema-strategy",choices=("authoritative","consensus","union"),default="authoritative"); p.add_argument("--on-type-error",choices=("coerce-null","fail","keep-string"),default="coerce-null"); p.add_argument("--memory-limit-mb",type=int); p.add_argument("--temp-dir"); p.add_argument("--csv-quotechar",default='"'); p.add_argument("--csv-escapechar"); p.add_argument("--csv-null-literal",default=""); p.add_argument("--input-format",choices=("auto","csv","tsv","jsonl","parquet"),default="auto"); p.add_argument("--compression",choices=("auto","none","gzip"),default="auto"); p.add_argument("--parquet-row-group-bytes",type=int); p.add_argument("inputs",nargs="+"); a=p.parse_args(argv)
    if a.memory_limit_mb is not None and a.memory_limit_mb<=0:p.error("--memory-limit-mb must be positive")
    if len(a.csv_quotechar)!=1 or (a.csv_escapechar is not None and len(a.csv_escapechar)!=1):p.error("CSV quote and escape characters must each be one character")
    if a.parquet_row_group_bytes is not None and a.parquet_row_group_bytes<=0:p.error("--parquet-row-group-bytes must be positive")
    if a.output!="-" and os.path.abspath(a.output) in {os.path.abspath(x) for x in a.inputs}:p.error("output path must not be one of the input paths")
    dialect={"quotechar":a.csv_quotechar,"escapechar":a.csv_escapechar,"doublequote":a.csv_escapechar is None}
    try:
        infos=[]
        for path in a.inputs:
            fmt,gz=detect(path,a.input_format,a.compression); infos.append((path,fmt,gz,source_info(path,fmt,gz,dialect,a.infer=="loose")))
        if a.schema: schema=read_schema(a.schema)
        else:
            names=sorted({n for _,_,_,(ns,obs,rf) in infos for n in ns}); schema=[]
            for n in names:
                per=[combined(obs[n],a.infer=="loose") for _,_,_,(_,obs,_) in infos if n in obs and obs[n]]
                if not per: typ="string"
                elif a.schema_strategy=="consensus": typ=max(set(per),key=lambda x:(per.count(x),x))
                elif a.schema_strategy=="union": typ=combined(per,False)
                else: typ=next((x for x in per if x!="string"),"string")
                schema.append((n,typ))
        keys=[x for x in a.key.split(",") if x]
        if not keys: raise DataError("--key must name at least one column",3)
        idx={n:i for i,(n,_) in enumerate(schema)}
        if any(k not in idx for k in keys): raise DataError("key column is not present in resolved schema",3)
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
                        try:v=cast(obj.get(n) if isinstance(obj,dict) else None,t)
                        except (ValueError,TypeError,OverflowError) as e:
                            if a.on_type_error=="fail":raise DataError(f"{path}: row {line}, column {n}: {e}")
                            v=obj.get(n) if a.on_type_error=="keep-string" and isinstance(obj,dict) else None
                        vals.append(v)
                    chunk.append((key(vals,[idx[k] for k in keys],a.desc),seq,vals));seq+=1;size+=sum(len(str(x)) for x in vals)+128
                    if size>=lim:flush()
            flush(); streams=[open(n,"rb") for n in runs]; heap=[]
            try:
                for i,f in enumerate(streams):
                    try:x=pickle.load(f);heapq.heappush(heap,(x[0],x[1],i,x[2]))
                    except EOFError:pass
                if a.output=="-": out=sys.stdout; close=False
                else:
                    fd,tmp=tempfile.mkstemp(prefix=".merge-",dir=os.path.dirname(os.path.abspath(a.output)) or ".");out=os.fdopen(fd,"w",newline="",encoding="utf-8");close=True
                try:
                    w=csv.writer(out,delimiter=",",quotechar=a.csv_quotechar,escapechar=a.csv_escapechar,doublequote=a.csv_escapechar is None,lineterminator="\n")
                    w.writerow([n for n,_ in schema])
                    while heap:
                        _,_,i,vals=heapq.heappop(heap);w.writerow([render(v,t) if v is not None else a.csv_null_literal for v,(_,t) in zip(vals,schema)])
                        try:x=pickle.load(streams[i]);heapq.heappush(heap,(x[0],x[1],i,x[2]))
                        except EOFError:pass
                    if close: out.close();os.replace(tmp,a.output)
                except Exception:
                    if close: out.close();
                    if close:
                        try:os.unlink(tmp)
                        except OSError:pass
                    raise
            finally:
                for f in streams:f.close()
    except DataError as e: print(f"merge_files.py: {e.message}",file=sys.stderr); return e.code
    except (OSError,pickle.PickleError) as e: print(f"merge_files.py: {e}",file=sys.stderr); return 5
    return 0
if __name__=="__main__":sys.exit(main())

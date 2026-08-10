#!/usr/bin/env python3
"""Merge CSV, TSV, JSONL and Parquet files into one sorted CSV."""
import argparse, csv, datetime as dt, functools, gzip, heapq, json, os, pickle, re, sys, tempfile

TYPES = ("string", "int", "float", "bool", "date", "timestamp")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RANK = {"string": 0, "bool": 1, "int": 2, "float": 3, "date": 4, "timestamp": 5}

class MergeError(Exception):
    def __init__(self, message, code=1):
        self.code, self.message = code, message

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

def render(value, typ, null):
    if value is None: return null
    if typ == "bool": return "true" if value else "false"
    if typ == "date": return value.isoformat()
    if typ == "timestamp": return value.isoformat(timespec="auto").replace("+00:00", "Z")
    return str(value)

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
            yield header, dict(zip(header, row))

def jsonl_rows(path, compression):
    header = None
    with open_input(path, compression) as f:
        for n, line in enumerate(f, 1):
            if not line.strip(): continue
            try: obj = json.loads(line)
            except json.JSONDecodeError as e: raise MergeError(f"{path}:{n}: invalid JSON: {e.msg}", 5)
            if not isinstance(obj, dict) or any(isinstance(v, (dict,list)) for v in obj.values()):
                raise MergeError(f"{path}:{n}: nested JSON values are not supported", 6)
            if header is None:
                header = list(obj)
                yield header, None
            for k in obj:
                if k not in header: header.append(k)
            yield header, obj
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
    allowed = ("bool","int8","int16","int32","int64","uint8","uint16","uint32","uint64","float","double","string","large_string","date32[day]","date64[ms]")
    for field in schema:
        if not (str(field.type) in allowed or "timestamp" in str(field.type)):
            if temp: os.unlink(temp)
            raise MergeError(f"{path}: nested Parquet field {field.name}", 6)
    header = schema.names
    yield header, None
    try:
        for batch in pf.iter_batches():
            cols = batch.to_pydict()
            for i in range(batch.num_rows): yield header, {n: cols[n][i] for n in header}
    finally:
        if temp: os.unlink(temp)

def rows(path, fmt, compression):
    if fmt in ("csv", "tsv"): return csv_rows(path, fmt, compression)
    if fmt == "jsonl": return jsonl_rows(path, compression)
    return parquet_rows(path, compression)

def schema_file(path):
    try:
        with open(path, encoding="utf-8") as f: data = json.load(f)
    except (OSError, json.JSONDecodeError) as e: raise MergeError(str(e), 2)
    cols = data.get("columns") if isinstance(data, dict) else None
    if not isinstance(cols, list) or not cols: raise MergeError("schema must contain a non-empty columns array", 2)
    out=[]; seen=set()
    for c in cols:
        if not isinstance(c,dict) or not isinstance(c.get("name"),str) or c.get("type") not in TYPES or c["name"] in seen: raise MergeError("invalid schema", 2)
        seen.add(c["name"]); out.append((c["name"],c["type"]))
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

def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument("--output",required=True); ap.add_argument("--key",required=True); ap.add_argument("--desc",action="store_true")
    ap.add_argument("--schema"); ap.add_argument("--infer",choices=("strict","loose"),default="strict")
    ap.add_argument("--schema-strategy",choices=("authoritative","consensus","union"),default="authoritative")
    ap.add_argument("--on-type-error",choices=("coerce-null","fail","keep-string"),default="coerce-null")
    ap.add_argument("--memory-limit-mb",type=int,default=64); ap.add_argument("--temp-dir")
    ap.add_argument("--csv-quotechar",default='"'); ap.add_argument("--csv-escapechar"); ap.add_argument("--csv-null-literal",default="")
    ap.add_argument("--input-format",choices=("auto","csv","tsv","jsonl","parquet"),default="auto")
    ap.add_argument("--compression",choices=("auto","none","gzip"),default="auto"); ap.add_argument("--parquet-row-group-bytes",type=int,default=0); ap.add_argument("inputs",nargs="+")
    try: args=ap.parse_args(argv)
    except SystemExit as e: return e.code
    if args.memory_limit_mb<=0 or len(args.csv_quotechar)!=1 or (args.csv_escapechar and len(args.csv_escapechar)!=1):
        print("merge_files.py: invalid arguments",file=sys.stderr); return 2
    try:
        specs=[(p,detect(p,args.input_format,args.compression)) for p in args.inputs]; infos=[]
        for p,f in specs:
            vals={}; header=None
            for h,r in rows(p,f,args.compression):
                header=h
                if r is not None:
                    for n, v in r.items():
                        if v is not None and v != "": vals.setdefault(n,set()).add(observed_kind(v, f))
            infos.append((f,header,vals))
        schema=schema_file(args.schema) if args.schema else resolve(infos,args.infer,args.schema_strategy)
        names=[n for n,_ in schema]; types=dict(schema); keys=[x for x in args.key.split(",") if x]
        if not keys or any(k not in types for k in keys): raise MergeError("every sort key must be present in the resolved schema",3)
        kidx=[names.index(k) for k in keys]; budget=max(1<<20,args.memory_limit_mb*1024*1024//2)
        td=tempfile.TemporaryDirectory(dir=args.temp_dir); chunks=[]; records=[]; estimate=0; seq=0
        try:
            for p,f in specs:
                for _,r in rows(p,f,args.compression):
                    if r is None: continue
                    vals=[]; rendered=[]
                    for n,t in schema:
                        raw=r.get(n)
                        try: v=parse_value(raw,t)
                        except (ValueError,TypeError,OverflowError) as e:
                            if args.on_type_error=="fail": raise MergeError(f"{p}:{n}: {e}",1)
                            v=raw if args.on_type_error=="keep-string" else None
                        vals.append(v); rendered.append(str(v) if args.on_type_error=="keep-string" and v is raw and v is not None else render(v,t,args.csv_null_literal))
                    records.append((tuple(vals[i] for i in kidx),seq,rendered)); seq+=1; estimate+=sum(len(x) for x in rendered)+128
                    if estimate>=budget:
                        records.sort(key=functools.cmp_to_key(lambda a,b:compare(a,b,args.desc))); fn=os.path.join(td.name,f"chunk-{len(chunks)}"); write_chunk(fn,records); chunks.append(fn); records=[]; estimate=0
            if records:
                records.sort(key=functools.cmp_to_key(lambda a,b:compare(a,b,args.desc))); fn=os.path.join(td.name,f"chunk-{len(chunks)}"); write_chunk(fn,records); chunks.append(fn)
            target=sys.stdout; temp_path=None
            if args.output!="-":
                parent=os.path.dirname(os.path.abspath(args.output)) or "."; fd,temp_path=tempfile.mkstemp(prefix=".merge-",dir=parent); os.close(fd); target=open(temp_path,"w",encoding="utf-8",newline="")
            try:
                w=csv.writer(target,delimiter=",",quotechar=args.csv_quotechar,escapechar=args.csv_escapechar,doublequote=not bool(args.csv_escapechar),lineterminator="\n"); w.writerow(names)
                its=[chunk_records(x) for x in chunks]; heap=[]
                for i,it in enumerate(its):
                    try: x=next(it); heapq.heappush(heap,(SortKey(x,args.desc),i,x))
                    except StopIteration: pass
                while heap:
                    _,i,x=heapq.heappop(heap); w.writerow(x[2])
                    try: y=next(its[i]); heapq.heappush(heap,(SortKey(y,args.desc),i,y))
                    except StopIteration: pass
            finally:
                if target is not sys.stdout: target.close()
            if temp_path: os.replace(temp_path,args.output)
        finally: td.cleanup()
    except MergeError as e:
        print(f"merge_files.py: {e.message}",file=sys.stderr); return e.code
    except (OSError,csv.Error) as e:
        print(f"merge_files.py: {e}",file=sys.stderr); return 5
    return 0

if __name__=="__main__": sys.exit(main())

#!/usr/bin/env python3
"""Merge heterogeneous tabular files into one sorted CSV."""
import argparse, csv, datetime as dt, functools, gzip, heapq, json, os, pickle, re, sys, tempfile, shutil

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

def parquet_type(field):
    import pyarrow.types as pt
    t = field.type
    if pt.is_nested(t) or pt.is_struct(t) or pt.is_list(t) or pt.is_map(t): raise NestedError(f"nested Parquet field {field.name!r} is not supported")
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

def read_file(path, fmt, gz, quotechar, escapechar):
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
                    if any(isinstance(v, (dict,list,tuple)) for v in row.values()): raise NestedError(f"nested Parquet value in {path}:{rn}")
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
                if any(isinstance(v, (dict,list)) for v in obj.values()): raise NestedError(f"nested JSON value at {path}:{rn}")
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

def cast(value, typ, null_literal, policy, path, row, column):
    if value is None or value == "": return None
    raw = json_or_parquet_value(value)
    try:
        if typ == "string": return raw
        if typ == "int":
            if not INT_RE.fullmatch(raw.strip()): raise ValueError()
            return str(int(raw.strip()))
        if typ == "float":
            x=float(raw.strip())
            if x != x or abs(x)==float("inf"): raise ValueError()
            return str(x)
        if typ == "bool":
            x=raw.strip().lower()
            if x in {"true","yes","y","t","1"}: return "true"
            if x in {"false","no","n","f","0"}: return "false"
            raise ValueError()
        if typ == "date": return dt.date.fromisoformat(raw.strip()).isoformat()
        if typ == "timestamp":
            x=parse_timestamp(raw); return (x.isoformat(timespec="microseconds") if x.microsecond else x.strftime("%Y-%m-%dT%H:%M:%SZ")).replace("+00:00", "Z")
    except (ValueError, OverflowError):
        if policy == "fail": raise MergeError(f"{path}:{row}: cannot cast column {column!r} value {raw!r} to {typ}")
        return raw if policy == "keep-string" else None
    return raw

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

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output",required=True); ap.add_argument("--key",required=True); ap.add_argument("--desc",action="store_true")
    ap.add_argument("--schema"); ap.add_argument("--infer",choices=("strict","loose"),default="strict")
    ap.add_argument("--schema-strategy",choices=("authoritative","consensus","union"),default="authoritative")
    ap.add_argument("--on-type-error",choices=("coerce-null","fail","keep-string"),default="coerce-null")
    ap.add_argument("--memory-limit-mb",type=int,default=128); ap.add_argument("--temp-dir")
    ap.add_argument("--csv-quotechar",default='"'); ap.add_argument("--csv-escapechar"); ap.add_argument("--csv-null-literal",default="")
    ap.add_argument("--input-format",choices=("auto","csv","tsv","jsonl","parquet"),default="auto"); ap.add_argument("--compression",choices=("auto","none","gzip"),default="auto")
    ap.add_argument("--parquet-row-group-bytes",type=int,default=0); ap.add_argument("inputs",nargs="+")
    args=ap.parse_args(argv)
    if len(args.csv_quotechar)!=1 or (args.csv_escapechar is not None and len(args.csv_escapechar)!=1): ap.error("CSV quote and escape characters must each be one character")
    if args.memory_limit_mb<=0: ap.error("--memory-limit-mb must be positive")
    keys=[x for x in args.key.split(",") if x]
    if not keys: ap.error("--key requires at least one column")
    infos=[(p,*detect(p,args.input_format,args.compression)) for p in args.inputs]
    observations={}; names=set(); file_types=[]
    for path,fmt,gz in infos:
        local={}; header_seen=False
        if fmt=="parquet":
            try:
                import pyarrow.parquet as pq
                source, cleanup = parquet_source(path, gz)
                fields=list(pq.ParquetFile(source).schema_arrow)
                header=[f.name for f in fields]; local={f.name:{parquet_type(f)} for f in fields}
                for n in header: observations.setdefault(n,[]).append(([local[n]],fmt))
                names.update(header); file_types.append((path,fmt,gz,local))
                if cleanup: os.unlink(cleanup)
                continue
            except NestedError: raise
            except Exception as e: raise InputError(f"cannot inspect Parquet {path}: {e}")
        for header,rn,row in read_file(path,fmt,gz,args.csv_quotechar,args.csv_escapechar):
            if not header_seen:
                header_seen=True; names.update(header)
                for n in header: local[n]=[]
            for n,v in row.items():
                if n not in local: local[n]=[]; names.add(n)
                p=possible_types(v)
                if p: local[n].append(p)
        for n in local: observations.setdefault(n,[]).append((local[n] or [{"string"}],fmt))
        file_types.append((path,fmt,gz,local))
    if args.schema:
        try:
            with open(args.schema,encoding="utf-8") as f: spec=json.load(f)
            schema=[(x["name"],x["type"]) for x in spec["columns"]]
        except Exception as e: raise MergeError(f"invalid schema: {e}")
        if any(t not in TYPES for _,t in schema) or len({n for n,_ in schema})!=len(schema): raise MergeError("invalid schema types or duplicate column names")
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
    missing=[k for k in keys if k not in col_index]
    if missing: raise MergeError("key column(s) not present in resolved schema: "+", ".join(missing),3)
    limit=max(1024,args.memory_limit_mb*1024*1024//16); temp=tempfile.TemporaryDirectory(dir=args.temp_dir); chunks=[]; pending=[]; size=0; seq=0
    try:
        def spill():
            nonlocal pending,size
            pending.sort(key=functools.cmp_to_key(lambda a,b:cmp_item(a,b,args.desc)))
            name=os.path.join(temp.name,f"chunk-{len(chunks)}.bin")
            with open(name,"wb") as f:
                for x in pending: pickle.dump(x,f,pickle.HIGHEST_PROTOCOL)
            chunks.append(name); pending=[]; size=0
        for path,fmt,gz,_ in file_types:
            for header,rn,row in read_file(path,fmt,gz,args.csv_quotechar,args.csv_escapechar):
                vals=[cast(row.get(n),typ,args.csv_null_literal,args.on_type_error,path,rn,n) for n,typ in schema]
                key=tuple(sort_atom(vals[col_index[k]],dict(schema)[k]) for k in keys)
                item=(key,seq,vals); pending.append(item); seq+=1; size+=sum(len(x) if isinstance(x,str) else 8 for x in vals)+80
                if size>=limit: spill()
        if pending: spill()
        streams=[open(x,"rb") for x in chunks]; heap=[]
        for i,f in enumerate(streams):
            try: heapq.heappush(heap,SortItem(pickle.load(f),args.desc,i))
            except EOFError: pass
        atomic=None
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
    finally:
        if atomic and os.path.exists(atomic): os.unlink(atomic)
        temp.cleanup()
    return 0

if __name__=="__main__":
    try: sys.exit(main())
    except MergeError as e: print(f"merge_files.py: {e}",file=sys.stderr); sys.exit(e.code)
    except Exception as e: print(f"merge_files.py: {e}",file=sys.stderr); sys.exit(1)

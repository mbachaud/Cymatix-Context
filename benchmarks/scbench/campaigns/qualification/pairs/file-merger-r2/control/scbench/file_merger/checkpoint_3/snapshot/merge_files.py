#!/usr/bin/env python3
"""Merge CSV, TSV, JSONL and Parquet files using an external stable sort."""
import argparse, csv, datetime as dt, functools, gzip, heapq, io, json, math, os, pickle, shutil, sys, tempfile

TYPES = {"string", "int", "float", "bool", "date", "timestamp"}
INFER_ORDER = ("timestamp", "date", "bool", "int", "float", "string")

class MergeError(Exception):
    def __init__(self, message, code=1): super().__init__(message); self.code = code

def parse_timestamp(v):
    s = str(v).strip(); s = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    x = dt.datetime.fromisoformat(s)
    if x.tzinfo is None: x = x.replace(tzinfo=dt.timezone.utc)
    return x.astimezone(dt.timezone.utc)
def parse_date(v): return dt.date.fromisoformat(str(v).strip())
def parse_bool(v):
    s = str(v).strip().lower()
    if s in {"true","t","yes","y","1"}: return True
    if s in {"false","f","no","n","0"}: return False
    raise ValueError("not a boolean")
def parse_int(v):
    s = str(v).strip()
    if not s or any(c in s.lower() for c in (".","e")): raise ValueError("not an integer")
    return int(s)
def parse_float(v):
    x = float(str(v).strip())
    if not math.isfinite(x): raise ValueError("non-finite float")
    return x
def accepts(v, typ):
    if v is None or v == "": return True
    try:
        if typ == "string": return True
        if typ == "int": parse_int(v)
        elif typ == "float": parse_float(v)
        elif typ == "bool": parse_bool(v)
        elif typ == "date": parse_date(v)
        elif typ == "timestamp": parse_timestamp(v)
        return True
    except (ValueError, TypeError, OverflowError): return False
def classify(v):
    if isinstance(v,bool): return "bool"
    if isinstance(v,int) and not isinstance(v,bool): return "int"
    if isinstance(v,float): return "float"
    for t in INFER_ORDER:
        if accepts(v, t): return t
    return "string"

def load_schema(path):
    try:
        with open(path, encoding="utf-8") as f: obj = json.load(f)
    except (OSError, json.JSONDecodeError) as e: raise MergeError(str(e))
    cols = obj.get("columns") if isinstance(obj, dict) else None
    if not isinstance(cols, list) or not cols: raise MergeError("schema must contain a non-empty columns array")
    out=[]; seen=set()
    for c in cols:
        if not isinstance(c,dict) or not isinstance(c.get("name"),str) or c.get("type") not in TYPES:
            raise MergeError("each schema column must have a name and a valid type")
        if c["name"] in seen: raise MergeError("duplicate schema column: " + c["name"])
        seen.add(c["name"]); out.append((c["name"],c["type"]))
    return out

def detect(path, requested, compression):
    low = path.lower()
    gz_suffix = low.endswith(".gz")
    ext = low[:-3] if gz_suffix else low
    byext = {".csv":"csv", ".tsv":"tsv", ".jsonl":"jsonl", ".ndjson":"jsonl", ".parquet":"parquet"}
    kind = requested if requested != "auto" else byext.get(os.path.splitext(ext)[1])
    try:
        with open(path,"rb") as f: magic=f.read(4)
    except OSError as e: raise MergeError(str(e))
    if kind is None and magic == b"PAR1": kind="parquet"
    if kind is None: raise MergeError(f"cannot detect input format: {path}", 2)
    if kind == "parquet" and gz_suffix: raise MergeError(f"gzip is not supported for Parquet: {path}", 5)
    use_gz = gz_suffix if compression == "auto" else compression == "gzip"
    if compression == "gzip" and kind == "parquet": raise MergeError("compression mismatch",5)
    if compression == "none" and gz_suffix: raise MergeError(f"compression mismatch: {path}",5)
    if compression == "gzip" and not gz_suffix:
        # Forced gzip is valid even without a suffix; validate the stream when read.
        use_gz=True
    if requested != "auto" and requested != kind and magic == b"PAR1":
        raise MergeError(f"input format mismatch: {path}",5)
    return kind, use_gz

def open_text(path, gz):
    try: return gzip.open(path,"rt",encoding="utf-8",newline="") if gz else open(path,"r",encoding="utf-8",newline="")
    except OSError as e: raise MergeError(str(e))

def json_value(v):
    if v is None or isinstance(v,(str,int,float,bool)): return v
    raise MergeError("nested JSON values are not supported",6)
def source_rows(path, kind, gz, dialect):
    if kind in ("csv","tsv"):
        f=open_text(path,gz); reader=csv.reader(f, delimiter=("\t" if kind=="tsv" else ","), quotechar=dialect["quotechar"] if kind=="csv" else '\0', quoting=(csv.QUOTE_MINIMAL if kind=="csv" else csv.QUOTE_NONE), doublequote=dialect["doublequote"], escapechar=dialect["escapechar"])
        try:
            try: header=next(reader)
            except StopIteration: raise MergeError(f"empty input: {path}")
            if len(set(header)) != len(header): raise MergeError(f"duplicate header in {path}",5)
            yield header, None, 1
            for n,row in enumerate(reader,2):
                if kind=="tsv" and any("\t" in x for x in row): raise MergeError(f"literal tab in TSV: {path}:{n}",5)
                yield header, row, n
        except csv.Error as e: raise MergeError(f"{path}: {e}",5)
        finally: f.close()
    elif kind=="jsonl":
        f=open_text(path,gz); header=None
        try:
            for n,line in enumerate(f,1):
                if not line.strip(): continue
                try: obj=json.loads(line)
                except json.JSONDecodeError as e: raise MergeError(f"{path}:{n}: invalid JSON: {e}",5)
                if not isinstance(obj,dict): raise MergeError(f"{path}:{n}: JSON line is not an object",6)
                obj={k:json_value(v) for k,v in obj.items()}
                if header is None: header=list(obj); yield header,None,n
                yield header,obj,n
            if header is None: raise MergeError(f"empty input: {path}")
        finally: f.close()
    else:
        try:
            import pyarrow.parquet as pq
            import pyarrow.types as patypes
        except ImportError: raise MergeError("Parquet support requires pyarrow")
        try: pf=pq.ParquetFile(path)
        except Exception as e: raise MergeError(f"{path}: {e}",5)
        fields=[]
        for field in pf.schema_arrow:
            if patypes.is_nested(field.type) or patypes.is_dictionary(field.type):
                raise MergeError(f"nested Parquet field {field.name}",6)
            fields.append(field.name)
        yield fields,None,1
        for batch in pf.iter_batches():
            data=batch.to_pydict()
            for i in range(batch.num_rows): yield fields,{k:data[k][i] for k in fields},i+2

def cast(v, typ, on_error, null, col, path, row):
    if v is None or v=="": return null,True
    try:
        if typ=="string": out=str(v)
        elif typ=="int": out=str(parse_int(v))
        elif typ=="float": out=repr(parse_float(v))
        elif typ=="bool": out="true" if parse_bool(v) else "false"
        elif typ=="date": out=parse_date(v).isoformat()
        else: out=parse_timestamp(v).isoformat().replace("+00:00","Z")
        return out,False
    except Exception as e:
        if on_error=="coerce-null": return null,True
        if on_error=="keep-string": return str(v),False
        raise MergeError(f"{path}:{row}: invalid {typ} in column {col!r}: {e}")

def infer_schema(infos, mode, strategy):
    names=set(); observations={}
    for info in infos:
        names.update(info["header"])
        for n, vals in info["values"].items(): observations.setdefault(n,[]).append((info["typed"],vals))
    result=[]
    for n in sorted(names):
        files=observations.get(n,[])
        if not files: result.append((n,"string")); continue
        supports={t:sum(all(accepts(v,t) for v in vals) for typed,vals in files) for t in TYPES}
        if strategy=="consensus":
            # A string vote is only counted for a file whose non-null values
            # are not consistently representable by a more specific type.
            votes={t:0 for t in TYPES}
            for _, vals in files:
                concrete=[t for t in INFER_ORDER[:-1] if vals and all(accepts(v,t) for v in vals)]
                votes[concrete[0] if concrete else "string"] += 1
            mx=max(votes.values()); typ=next(t for t in INFER_ORDER if votes[t]==mx)
        elif strategy=="union":
            typ=next((t for t in INFER_ORDER if supports[t]==len(files)),"string")
        else:
            # Typed formats outrank textual sources; among those choose the most specific common type.
            typed_files=[x for x in files if x[0]] or files
            typ=next((t for t in INFER_ORDER if sum(all(accepts(v,t) for v in vs) for _,vs in typed_files)==len(typed_files)),"string")
        if mode=="strict":
            kinds={classify(v) for _,vs in files for v in vs if v not in (None,"")}
            if len(kinds)>1: typ="string"
        result.append((n,typ))
    return result

def typed_key(v, typ):
    if v is None: return None
    try:
        if typ=="int": return int(v)
        if typ=="float": return float(v)
        if typ=="bool": return v=="true" if isinstance(v,str) else bool(v)
        if typ=="date": return parse_date(v)
        if typ=="timestamp": return parse_timestamp(v)
    except Exception: pass
    return v
def compare(a,b,indexes,desc):
    for i in indexes:
        x,y=a[0][i],b[0][i]
        if x is None or y is None:
            c=0 if x is None and y is None else (-1 if x is None else 1)
        else:
            try: c=(x>y)-(x<y)
            except TypeError: c=(str(x)>str(y))-(str(x)<str(y))
        if c: return -c if desc else c
    return (a[1]>b[1])-(a[1]<b[1])
class HeapItem:
    def __init__(self, record, run, cmp): self.record,self.run,self.cmp=record,run,cmp
    def __lt__(self,o): return self.cmp(self.record,o.record)<0

def csv_bytes(values, dialect):
    """Serialize one CSV record exactly as it will be written to disk."""
    s=io.StringIO(newline="")
    w=csv.writer(s, delimiter=",", quotechar=dialect["quotechar"],
                 doublequote=dialect["doublequote"], escapechar=dialect["escapechar"],
                 lineterminator="\n")
    w.writerow(values)
    return s.getvalue().encode("utf-8")

def partition_value(value, isnull):
    if isnull: return "_null"
    allowed=b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
    return "".join(chr(b) if b in allowed else f"%{b:02X}" for b in str(value).encode("utf-8"))

class PartitionWriter:
    def __init__(self, root, names, schema, partition_indexes, dialect, max_rows, max_bytes):
        self.root=root; self.names=names; self.schema=schema
        self.partition_indexes=partition_indexes; self.dialect=dialect
        self.max_rows=max_rows; self.max_bytes=max_bytes
        self.states={}
        self.header=csv_bytes(names,dialect)

    def _state(self, values, nulls):
        key=tuple(partition_value(values[i],nulls[i]) for i in self.partition_indexes)
        state=self.states.get(key)
        if state is None:
            directory=self.root
            for i,segment in zip(self.partition_indexes,key):
                directory=os.path.join(directory,self.names[i]+"="+segment)
            os.makedirs(directory,exist_ok=True)
            state={"directory":directory,"part":-1,"file":None,"rows":0,"size":0}
            self.states[key]=state
        return state

    def _open_part(self,state):
        if state["file"] is not None: state["file"].close()
        state["part"]+=1
        path=os.path.join(state["directory"],f"part-{state['part']:05d}.csv")
        f=open(path,"wb")
        f.write(self.header)
        state["file"]=f; state["rows"]=0; state["size"]=len(self.header)

    def write(self, values, nulls):
        state=self._state(values,nulls)
        row=csv_bytes(values,self.dialect)
        need_new=(state["file"] is None or
                  (self.max_rows is not None and state["rows"] >= self.max_rows) or
                  (self.max_bytes is not None and state["rows"] > 0 and
                   state["size"]+len(row)>self.max_bytes))
        if need_new: self._open_part(state)
        state["file"].write(row); state["rows"]+=1; state["size"]+=len(row)

    def close(self):
        for state in self.states.values():
            if state["file"] is not None: state["file"].close(); state["file"]=None

def atomic_directory_commit(tempdir,target):
    parent=os.path.dirname(os.path.abspath(target)) or "."
    if os.path.exists(target) or os.path.islink(target):
        if os.path.islink(target) or not os.path.isdir(target):
            raise MergeError(f"output is not a directory: {target}")
        backup=tempfile.mkdtemp(prefix="."+os.path.basename(target)+"-old-",dir=parent)
        os.rmdir(backup)
        try:
            os.rename(target,backup); os.rename(tempdir,target)
        except Exception:
            if os.path.exists(target) and os.path.abspath(target)==os.path.abspath(tempdir):
                pass
            if os.path.exists(backup) and not os.path.exists(target): os.rename(backup,target)
            raise
        shutil.rmtree(backup,ignore_errors=True)
    else:
        os.rename(tempdir,target)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",required=True); p.add_argument("--key",required=True); p.add_argument("--desc",action="store_true")
    p.add_argument("--partition-by")
    p.add_argument("--max-rows-per-file",type=int)
    p.add_argument("--max-bytes-per-file",type=int)
    p.add_argument("--schema"); p.add_argument("--infer",choices=("strict","loose"),default="strict")
    p.add_argument("--schema-strategy",choices=("authoritative","consensus","union"),default="authoritative")
    p.add_argument("--on-type-error",choices=("coerce-null","fail","keep-string"),default="coerce-null")
    p.add_argument("--memory-limit-mb",type=int,default=64); p.add_argument("--temp-dir")
    p.add_argument("--csv-quotechar",default='"'); p.add_argument("--csv-escapechar"); p.add_argument("--csv-null-literal",default="")
    p.add_argument("--input-format",choices=("auto","csv","tsv","jsonl","parquet"),default="auto")
    p.add_argument("--compression",choices=("auto","none","gzip"),default="auto")
    p.add_argument("--parquet-row-group-bytes",type=int,default=0); p.add_argument("inputs",nargs="+")
    args=p.parse_args(argv)
    if args.memory_limit_mb<=0: p.error("--memory-limit-mb must be positive")
    if args.max_rows_per_file is not None and args.max_rows_per_file<=0: p.error("--max-rows-per-file must be positive")
    if args.max_bytes_per_file is not None and args.max_bytes_per_file<=0: p.error("--max-bytes-per-file must be positive")
    if len(args.csv_quotechar)!=1 or (args.csv_escapechar is not None and len(args.csv_escapechar)!=1): p.error("quotechar and escapechar must each be one character")
    dialect={"quotechar":args.csv_quotechar,"escapechar":args.csv_escapechar,"doublequote":args.csv_escapechar is None}
    temp_root=None
    try:
        sources=[(path,*detect(path,args.input_format,args.compression)) for path in args.inputs]
        infos=[]
        # Inference is intentionally bounded per file: values are samples, while the second pass streams all rows.
        if args.schema:
            schema=load_schema(args.schema)
        else:
            for path,kind,gz in sources:
                it=source_rows(path,kind,gz,dialect); header=None; vals={}
                for h,row,_ in it:
                    if header is None: header=h; continue
                    if kind in ("jsonl","parquet"): pairs=row.items()
                    else: pairs=((h[i],row[i]) for i in range(min(len(h),len(row))))
                    for n,v in pairs:
                        a=vals.setdefault(n,[])
                        if len(a)<10000: a.append(v)
                infos.append({"header":sorted(set(header or []).union(vals)),"values":vals,"typed":kind in ("jsonl","parquet")})
            schema=infer_schema(infos,args.infer,args.schema_strategy)
        names=[n for n,_ in schema]; positions_schema={n:i for i,n in enumerate(names)}
        keys=[k for k in args.key.split(",") if k]
        if not keys or any(k not in positions_schema for k in keys): raise MergeError("every --key column must be present in the resolved schema",3)
        partitions=[k for k in (args.partition_by.split(",") if args.partition_by is not None else []) if k]
        if args.partition_by is not None and (not partitions or any(k not in positions_schema for k in partitions)):
            raise MergeError("every --partition-by column must be present in the resolved schema",3)
        directory_mode=args.partition_by is not None or args.max_rows_per_file is not None or args.max_bytes_per_file is not None
        if directory_mode and args.output=="-": raise MergeError("--output must be a directory when partitioning or sharding",2)
        indexes=[positions_schema[k] for k in keys]; cmp=functools.partial(compare,indexes=indexes,desc=args.desc)
        temp_root=tempfile.mkdtemp(prefix="csv-merge-",dir=args.temp_dir)
        runs=[]; records=[]; seq=0
        cap=max(1,args.memory_limit_mb*1024*1024//max(256,64*len(schema)))
        def spill():
            if not records:return
            records.sort(key=functools.cmp_to_key(cmp)); fn=os.path.join(temp_root,f"run-{len(runs):06d}.bin")
            with open(fn,"wb") as f:
                for r in records: pickle.dump(r,f,protocol=4)
            runs.append(fn); records.clear()
        for path,kind,gz in sources:
            it=source_rows(path,kind,gz,dialect); header=None; pos={}
            for h,row,rownum in it:
                if header is None: header=h; pos={x:i for i,x in enumerate(header)}; continue
                values=[]; nulls=[]
                for n,t in schema:
                    if kind in ("jsonl","parquet"): v=row.get(n) if n in row else None
                    else: v=row[pos[n]] if n in pos and pos[n]<len(row) else None
                    out,isnull=cast(v,t,args.on_type_error,args.csv_null_literal,n,path,rownum); values.append(out); nulls.append(isnull)
                tk=[None]*len(schema)
                for i in indexes: tk[i]=None if nulls[i] else typed_key(values[i],schema[i][1])
                records.append((tk,seq,values,nulls)); seq+=1
                if len(records)>=cap: spill()
        spill()
        handles=[open(fn,"rb") for fn in runs]; heap=[]
        def one(i):
            try:return pickle.load(handles[i])
            except EOFError:return None
        for i in range(len(handles)):
            r=one(i)
            if r is not None: heapq.heappush(heap,HeapItem(r,i,cmp))
        if not directory_mode:
            target=args.output
            if target=="-": out=sys.stdout; close=False; tmpname=None
            else:
                parent=os.path.dirname(os.path.abspath(target)) or "."
                os.makedirs(parent,exist_ok=True)
                fd,tmpname=tempfile.mkstemp(prefix=".merge-",dir=parent); os.close(fd)
                out=open(tmpname,"w",encoding="utf-8",newline=""); close=True
            try:
                writer=csv.writer(out,delimiter=",",quotechar=args.csv_quotechar,doublequote=args.csv_escapechar is None,escapechar=args.csv_escapechar,lineterminator="\n")
                writer.writerow(names)
                while heap:
                    x=heapq.heappop(heap); writer.writerow(x.record[2]); r=one(x.run)
                    if r is not None: heapq.heappush(heap,HeapItem(r,x.run,cmp))
            finally:
                if close: out.close()
            if tmpname: os.replace(tmpname,target)
        else:
            target=os.path.abspath(args.output); parent=os.path.dirname(target) or "."
            os.makedirs(parent,exist_ok=True)
            outdir=tempfile.mkdtemp(prefix="."+os.path.basename(target)+"-tmp-",dir=parent)
            pw=PartitionWriter(outdir,names,schema,[positions_schema[k] for k in partitions],dialect,args.max_rows_per_file,args.max_bytes_per_file)
            try:
                while heap:
                    x=heapq.heappop(heap); pw.write(x.record[2],x.record[3]); r=one(x.run)
                    if r is not None: heapq.heappush(heap,HeapItem(r,x.run,cmp))
            finally: pw.close()
            atomic_directory_commit(outdir,target); outdir=None
        for h in handles:h.close()
        return 0
    except MergeError as e:
        print(f"merge_files.py: {e}",file=sys.stderr); return e.code
    except gzip.BadGzipFile as e:
        print(f"merge_files.py: {e}",file=sys.stderr); return 5
    except (OSError,ValueError,TypeError) as e:
        print(f"merge_files.py: {e}",file=sys.stderr); return 1
    finally:
        if 'tmpname' in locals() and tmpname and os.path.exists(tmpname):
            try: os.unlink(tmpname)
            except OSError: pass
        if 'outdir' in locals() and outdir and os.path.isdir(outdir): shutil.rmtree(outdir,ignore_errors=True)
        if temp_root and os.path.isdir(temp_root): shutil.rmtree(temp_root,ignore_errors=True)

if __name__=="__main__": sys.exit(main())

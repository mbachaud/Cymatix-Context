#!/usr/bin/env python3
"""Merge heterogeneous tabular files into one stably sorted CSV."""
import argparse, csv, datetime as dt, functools, gzip, heapq, itertools, json, os, pickle, shutil, sys, tempfile

TYPES = {"string", "int", "float", "bool", "date", "timestamp"}

class InputError(Exception): pass
class KeyErrorInput(Exception): pass
class NestedError(Exception): pass

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True); p.add_argument("--key", required=True)
    p.add_argument("--desc", action="store_true"); p.add_argument("--schema")
    p.add_argument("--infer", choices=("strict", "loose"), default="strict")
    p.add_argument("--schema-strategy", choices=("authoritative", "consensus", "union"), default="authoritative")
    p.add_argument("--on-type-error", choices=("coerce-null", "fail", "keep-string"), default="coerce-null")
    p.add_argument("--memory-limit-mb", type=int, default=128); p.add_argument("--temp-dir")
    p.add_argument("--csv-quotechar", default='"'); p.add_argument("--csv-escapechar")
    p.add_argument("--csv-null-literal", default="")
    p.add_argument("--input-format", choices=("auto", "csv", "tsv", "jsonl", "parquet"), default="auto")
    p.add_argument("--compression", choices=("auto", "none", "gzip"), default="auto")
    p.add_argument("--parquet-row-group-bytes", type=int, default=0)
    p.add_argument("inputs", nargs="+")
    a = p.parse_args()
    if a.memory_limit_mb <= 0 or len(a.csv_quotechar) != 1 or (a.csv_escapechar and len(a.csv_escapechar) != 1): p.error("memory limit must be positive and CSV quote/escape characters must be one character")
    if a.parquet_row_group_bytes < 0: p.error("parquet row group bytes must not be negative")
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

def parquet_rows(path):
    try: import pyarrow.parquet as pq
    except ImportError: raise InputError("Parquet input requires pyarrow")
    try: pf = pq.ParquetFile(path)
    except Exception as e: raise InputError(str(e))
    for field in pf.schema_arrow:
        if getattr(field.type, "is_nested", False) or str(field.type).startswith(("list<", "map<", "struct<")):
            raise NestedError("nested Parquet field: " + field.name)
    for i in range(pf.num_row_groups):
        tab = pf.read_row_group(i)
        for row in tab.to_pylist():
            for k, v in row.items():
                if isinstance(v, (dict, list, tuple)): raise NestedError("nested Parquet field: " + k)
            yield row

def rows_for(path, args):
    kind, gz = fmt(path, args)
    if kind == "parquet":
        if gz: raise InputError("gzip-compressed Parquet is not supported")
        it = parquet_rows(path)
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
                if any(isinstance(v, (dict,list)) for v in obj.values()): raise NestedError("nested JSONL value")
                yield obj
        finally: f.close()
    it = genjson()
    try: first = next(it)
    except StopIteration: return [], iter(())
    names = list(first); return names, itertools.chain((first,), it)

def load_schema(path):
    try:
        with open(path, encoding="utf-8") as f: obj = json.load(f)
    except (OSError, ValueError) as e: raise InputError(str(e))
    cols = obj.get("columns") if isinstance(obj, dict) else None
    if not isinstance(cols, list) or not cols: raise InputError("schema must contain a non-empty columns list")
    out=[]; seen=set()
    for c in cols:
        if not isinstance(c,dict) or not isinstance(c.get("name"),str) or c.get("type") not in TYPES: raise InputError("each schema column needs a valid name and type")
        if c["name"] in seen: raise InputError("duplicate schema column: " + c["name"])
        seen.add(c["name"]); out.append((c["name"],c["type"]))
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

def cast(v, typ, args, col, seq):
    if is_null(v,args): return None, args.csv_null_literal
    try: x=parse_value(v,typ); return x,output_value(x,typ)
    except (ValueError,OverflowError,TypeError) as e:
        if args.on_type_error=="fail": raise InputError(f"row {seq}, column {col}: {e}: {v!r}")
        if args.on_type_error=="keep-string": return str(v),str(v)
        return None,args.csv_null_literal

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
    infos=[]
    for path in args.inputs:
        kind, _ = fmt(path, args)
        header,it=rows_for(path,args); ts={n:set() for n in header}
        try:
            for row in it:
                for n,v in row.items():
                    ts.setdefault(n,set())
                    if not is_null(v,args): ts[n].add(classify(v))
        except (InputError,NestedError): raise
        infos.append((header,ts,{"csv":0,"tsv":0,"jsonl":1,"parquet":2}[kind]))
    schema=load_schema(args.schema) if args.schema else resolve(infos,args)
    pos={n:i for i,(n,_) in enumerate(schema)}; missing=[k for k in keys if k not in pos]
    if missing: raise KeyErrorInput("key column not in resolved schema: "+", ".join(missing))
    types=[t for _,t in schema]; limit=max(1024*1024,args.memory_limit_mb*1024*1024//3); root=tempfile.mkdtemp(prefix="csv-merge-",dir=args.temp_dir); runs=[]; seq=0; chunk=[]; size=0
    def flush():
        nonlocal chunk,size
        if not chunk:return
        chunk.sort(key=functools.cmp_to_key(lambda a,b:cmp(a,b,args.desc))); p=os.path.join(root,f"run-{len(runs)}.bin")
        with open(p,"wb") as f:
            for x in chunk: pickle.dump(x,f,pickle.HIGHEST_PROTOCOL)
        runs.append(p);chunk=[];size=0
    try:
        for path in args.inputs:
            header,it=rows_for(path,args); ix=set(header)
            for row in it:
                vals=[]; cells=[]
                for n,t in schema:
                    v=row.get(n) if n in ix else None; x,s=cast(v,t,args,n,seq); vals.append(x);cells.append(s)
                chunk.append((tuple(vals[pos[k]] for k in keys),seq,cells));size+=sum(len(x) for x in cells)+64;seq+=1
                if size>=limit:flush()
        flush()
        out=sys.stdout if args.output=="-" else open(args.output+".tmp-"+str(os.getpid()),"w",encoding="utf-8",newline="")
        try:
            w=csv.writer(out,quotechar=args.csv_quotechar,escapechar=args.csv_escapechar,doublequote=args.csv_escapechar is None,lineterminator="\n")
            w.writerow([n for n,_ in schema]); H.desc=args.desc; fhs=[open(p,"rb") for p in runs]; heap=[]
            for f in fhs:
                try:heapq.heappush(heap,H(pickle.load(f),f))
                except EOFError:pass
            while heap:
                h=heapq.heappop(heap);w.writerow(h.row[2])
                try:h.row=pickle.load(h.fh);heapq.heappush(heap,h)
                except EOFError:pass
            for f in fhs:f.close()
        finally:
            if out is not sys.stdout:
                out.close();os.replace(out.name,args.output)
    finally:shutil.rmtree(root,ignore_errors=True)

if __name__=="__main__":
    try: main()
    except KeyErrorInput as e: print("merge_files.py: "+str(e),file=sys.stderr);sys.exit(3)
    except NestedError as e: print("merge_files.py: "+str(e),file=sys.stderr);sys.exit(6)
    except (InputError, csv.Error, OSError, UnicodeError, ValueError) as e: print("merge_files.py: "+str(e),file=sys.stderr);sys.exit(5)
    except Exception as e: print("merge_files.py: "+str(e),file=sys.stderr);sys.exit(1)

import sys
from pypdf import PdfReader
src=sys.argv[1]; out=sys.argv[2]
r=PdfReader(src)
t=[]
for i,p in enumerate(r.pages):
    t.append(f"\n===PAGE {i+1}===\n"+ (p.extract_text() or ""))
open(out,"w",encoding="utf-8").write("".join(t))
print(out, len("".join(t)))

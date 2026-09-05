import sys, io
from pypdf import PdfReader
r = PdfReader(sys.argv[1])
with io.open(sys.argv[2],"w",encoding="utf-8") as f:
    for i,p in enumerate(r.pages):
        f.write(f"\n===== PAGE {i+1} =====\n")
        try:
            f.write(p.extract_text() or "")
        except Exception as e:
            f.write(f"[err {e}]")

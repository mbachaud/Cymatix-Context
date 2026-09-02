import sys, urllib.request, io, os
from pypdf import PdfReader
src = sys.argv[1]
if src.startswith("http"):
    req = urllib.request.Request(src, headers={"User-Agent":"Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=45).read()
    f = io.BytesIO(data)
else:
    f = open(src, "rb")
r = PdfReader(f)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
print("PAGES:", len(r.pages))
for i, p in enumerate(r.pages):
    try:
        t = p.extract_text() or ""
    except Exception as e:
        t = f"[err {e}]"
    print(f"\n===== PAGE {i+1} =====\n{t}")

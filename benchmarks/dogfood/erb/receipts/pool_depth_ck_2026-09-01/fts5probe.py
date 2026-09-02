import sqlite3
db = sqlite3.connect(":memory:")
print("sqlite version:", sqlite3.sqlite_version)
c = db.cursor()
c.execute("CREATE VIRTUAL TABLE email USING fts5(sender, title, body)")
rows = [
    ("alice@enron.com", "Gas nomination", "the body mentions gas once"),
    ("bob@enron.com", "unrelated", "gas gas gas gas gas gas in body only"),
]
c.executemany("INSERT INTO email VALUES (?,?,?)", rows)

print("\n-- default (no weights) --")
for r in c.execute("SELECT sender, bm25(email) FROM email WHERE email MATCH 'gas' ORDER BY bm25(email)"):
    print(r)

print("\n-- weights (10 sender, 5 title) => trailing body defaults to 1.0 --")
for r in c.execute("SELECT sender, bm25(email,10.0,5.0) FROM email WHERE email MATCH 'gas' ORDER BY bm25(email,10.0,5.0)"):
    print(r)

print("\n-- explicit 3rd weight 1.0 must equal the 2-arg form --")
for r in c.execute("SELECT sender, bm25(email,10.0,5.0), bm25(email,10.0,5.0,1.0) FROM email WHERE email MATCH 'gas'"):
    print(r)

print("\n-- too many trailing args ignored? --")
try:
    for r in c.execute("SELECT bm25(email,10.0,5.0,1.0,99.0) FROM email WHERE email MATCH 'gas'"):
        print(r)
except Exception as e:
    print("ERR:", e)

print("\n-- column filters --")
for q in ["title : gas", "{title body} : gas", "- title : gas", "- {title body} : gas"]:
    try:
        got = list(c.execute("SELECT sender FROM email WHERE email MATCH ?", (q,)))
        print(repr(q), "->", got)
    except Exception as e:
        print(repr(q), "-> ERR:", e)

print("\n-- ALTER TABLE ADD COLUMN on an existing FTS5 table --")
try:
    c.execute("ALTER TABLE email ADD COLUMN filename")
    print("ADD COLUMN: OK")
except Exception as e:
    print("ADD COLUMN: ERR:", type(e).__name__, e)

print("\n-- does bm25 length-normalize per FIELD or per ROW? --")
db2 = sqlite3.connect(":memory:")
d = db2.cursor()
d.execute("CREATE VIRTUAL TABLE t USING fts5(title, body)")
# same title content; wildly different body length -> if per-field norm, title-only score unchanged
d.execute("INSERT INTO t VALUES ('gas nomination', 'x')")
d.execute("INSERT INTO t VALUES ('gas nomination', ?)", (" ".join(["filler"]*500),))
for r in d.execute("SELECT rowid, bm25(t,1.0,0.0) FROM t WHERE t MATCH 'title : gas'"):
    print("title-only score, body weight 0:", r)

print("\n-- detail=none: column filters --")
db3 = sqlite3.connect(":memory:")
e3 = db3.cursor()
e3.execute("CREATE VIRTUAL TABLE n USING fts5(a, b, detail=none)")
e3.execute("INSERT INTO n VALUES ('gas','oil')")
try:
    print(list(e3.execute("SELECT rowid FROM n WHERE n MATCH 'a : gas'")))
except Exception as ex:
    print("detail=none column filter ERR:", ex)
try:
    print("detail=none bm25:", list(e3.execute("SELECT bm25(n,10.0,1.0) FROM n WHERE n MATCH 'gas'")))
except Exception as ex:
    print("detail=none bm25 ERR:", ex)

print("\n-- columnsize=0 + bm25 weights --")
db4 = sqlite3.connect(":memory:")
e4 = db4.cursor()
e4.execute("CREATE VIRTUAL TABLE cz USING fts5(a, b, columnsize=0)")
e4.execute("INSERT INTO cz VALUES ('gas','oil')")
try:
    print("columnsize=0 bm25:", list(e4.execute("SELECT bm25(cz,10.0,1.0) FROM cz WHERE cz MATCH 'gas'")))
except Exception as ex:
    print("columnsize=0 bm25 ERR:", ex)

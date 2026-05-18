import sqlite3, json
db = r'C:\Users\wvini\AppData\Local\Aether\runtime\aether.sqlite3'
con = sqlite3.connect(db); con.row_factory = sqlite3.Row
r = con.execute("select * from nodes where status='validated' order by score desc limit 1").fetchone()
print("FIELDS:")
for k in r.keys():
    v = r[k]
    if k == 'metrics_json' and v:
        d = json.loads(v)
        print(f"{k}:")
        print(f"  keys: {list(d.keys())}")
        bp = d.get('best_probe')
        if isinstance(bp, dict):
            print(f"  best_probe keys: {list(bp.keys())}")
            print(f"  best_probe params: {bp.get('params')}")
            print(f"  best_probe asset: {bp.get('asset')}")
            print(f"  best_probe engine: {bp.get('engine')}")
            print(f"  best_probe sharpe: {bp.get('sharpe')}")
        s = d.get('summary')
        if isinstance(s, dict):
            print(f"  summary keys: {list(s.keys())}")
            print(f"  summary asset: {s.get('asset')}")
            print(f"  summary name: {s.get('name')}")
    elif k == 'tags_json':
        print(f"{k}: {v}")
    else:
        print(f"{k}: {str(v)[:120]}")

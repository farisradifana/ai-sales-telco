"""Independent stdlib SQLite validator over generated CSVs (not a PostgreSQL substitute)."""
import argparse
import csv
import hashlib
import json
import sqlite3
from pathlib import Path
from .db import ORDER


def validate(directory):
    directory = Path(directory)
    m = json.loads((directory/'manifest.json').read_text())
    db = sqlite3.connect(':memory:')
    checks = []

    def check(name, ok):
        if not ok: raise AssertionError(name)
        checks.append(name)

    for filename, spec in m['files'].items():
        check('sha256:'+filename, hashlib.sha256((directory/filename).read_bytes()).hexdigest()==spec['sha256'])
    for name in ORDER:
        with (directory/(name+'.csv')).open(encoding='utf-8',newline='') as f:
            reader = csv.reader(f)
            header = next(reader)
            db.execute('CREATE TABLE '+name+' ('+','.join('"'+c+'" TEXT' for c in header)+')')
            db.executemany('INSERT INTO '+name+' VALUES ('+','.join('?' for _ in header)+')',reader)
        check('count:'+name, db.execute('SELECT count(*) FROM '+name).fetchone()[0]==m['counts'][name])
        if 'id' in header:
            check('unique id:'+name,db.execute('SELECT count(*)=count(DISTINCT id) FROM '+name).fetchone()[0]==1)
    count = lambda q: db.execute(q).fetchone()[0]
    check('exact commercial count',count('SELECT (SELECT count(*) FROM sell_in)+(SELECT count(*) FROM sell_out)')==m['rows'])
    for table in ('sell_in','sell_out'):
        check('keys/assignment:'+table,count(f'''SELECT count(*) FROM {table} t LEFT JOIN assignment a ON t.assignment_id=a.id
        LEFT JOIN product p ON p.id=t.product_id LEFT JOIN outlet o ON o.id=t.outlet_id
        WHERE a.id IS NULL OR p.id IS NULL OR o.id IS NULL OR a.outlet_id<>t.outlet_id OR a.sales_id<>t.sales_id
        OR substr(t.occurred_at,1,10)<a.valid_from OR substr(t.occurred_at,1,10)>=a.valid_to''')==0)
        check('quantity/price:'+table,count(f'''SELECT count(*) FROM {table} t JOIN product p ON p.id=t.product_id
        WHERE CAST(quantity AS REAL)<=0 OR CAST(unit_price AS REAL)<0 OR CAST(discount_idr AS REAL)<0
        OR CAST(quantity AS REAL)*CAST(unit_price AS REAL)<CAST(discount_idr AS REAL)
        OR (p.stock_unit='PCS' AND CAST(quantity AS REAL)<>CAST(quantity AS INTEGER))''')==0)
        check('unique external key:'+table,count(f'SELECT count(*)=count(DISTINCT external_key) FROM {table}')==1)
        check('date range:'+table,count(f"SELECT count(*) FROM {table} WHERE substr(occurred_at,1,10)<'2025-01-01' OR substr(occurred_at,1,10)>'2026-09-30'")==0)
    if m['rows']==500000:
        check('all 21 months',count('SELECT count(DISTINCT substr(occurred_at,1,7)) FROM sell_out')==21)
        check('date endpoints',db.execute('SELECT min(substr(occurred_at,1,10)),max(substr(occurred_at,1,10)) FROM sell_out').fetchone()==('2025-01-01','2026-09-30'))
    for child,key,parent in [('regency','province_id','province'),('district','regency_id','regency'),('outlet','district_id','district'),('sales','district_id','district')]:
        check('hierarchy:'+child,count(f'SELECT count(*) FROM {child} c LEFT JOIN {parent} p ON c.{key}=p.id WHERE p.id IS NULL')==0)
    # Administrative codes nest by prefix (35 -> 35.xx -> 35.xx.yy), independent of the generator's id mapping.
    check('region code nesting',count('''SELECT count(*) FROM district d JOIN regency r ON r.id=d.regency_id JOIN province p ON p.id=r.province_id
      WHERE substr(d.code,1,length(r.code)+1)<>r.code||'.' OR substr(r.code,1,length(p.code)+1)<>p.code||'.' ''')==0)
    check('outlets have no coordinates (fictional outlet, real region)',count("SELECT count(*) FROM outlet WHERE latitude<>'' OR longitude<>''")==0)
    # Independent ledger reconstruction from source transactions.
    db.executescript('''CREATE TABLE ledger AS
      SELECT account_id,product_id,occurred_at,CAST(quantity AS REAL) delta FROM supply
      UNION ALL SELECT sales_id,product_id,occurred_at,-CAST(quantity AS REAL) FROM sell_in
      UNION ALL SELECT CAST(1000+CAST(outlet_id AS INTEGER) AS TEXT),product_id,occurred_at,CAST(quantity AS REAL) FROM sell_in
      UNION ALL SELECT CAST(1000+CAST(outlet_id AS INTEGER) AS TEXT),product_id,occurred_at,-CAST(quantity AS REAL) FROM sell_out;
      CREATE INDEX lp ON ledger(account_id,product_id,occurred_at);''')
    check('ledger account/product FK',count('SELECT count(*) FROM ledger l LEFT JOIN stock_account a ON a.id=l.account_id LEFT JOIN product p ON p.id=l.product_id WHERE a.id IS NULL OR p.id IS NULL')==0)
    # Aggregate simultaneous timestamps before cumulative sum: no arbitrary same-time ordering.
    check('chronological stock nonnegative',count('''WITH d AS (SELECT account_id,product_id,occurred_at,sum(delta) delta FROM ledger GROUP BY 1,2,3),
      b AS(SELECT sum(delta) OVER(PARTITION BY account_id,product_id ORDER BY occurred_at) q FROM d) SELECT count(*) FROM b WHERE q<0''')==0)
    check('closing reconciliation zero (fixture)',count('SELECT count(*) FROM (SELECT sum(delta) q FROM ledger GROUP BY account_id,product_id HAVING q<>0)')==0)
    check('no real customer numbers',count("SELECT count(*) FROM sell_out WHERE customer_token NOT LIKE 'SYN-CUSTOMER-%'")==0)
    result = {'status':'passed','engine':'Python stdlib SQLite independent CSV checks','commercial_rows':m['rows'],
              'checks_passed':len(checks),'checks':checks,'seed':m['seed']}
    db.close()
    return result


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('directory')
    ap.add_argument('--report')
    a=ap.parse_args()
    result=validate(a.directory)
    payload=json.dumps(result,indent=2)+'\n'
    if a.report: Path(a.report).write_text(payload,encoding='utf-8')
    print(payload)

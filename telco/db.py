"""Checksum migrations and streamed COPY loader for real PostgreSQL (psycopg 3)."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORDER = ['province','regency','district','sales','outlet','product','assignment','stock_account',
         'supply','sell_in','sell_out','sales_target','outlet_day','ingestion_watermark']


def connect():
    import psycopg
    return psycopg.connect(os.environ['DATABASE_URL'])


def migrate(conn):
    # Serialize concurrent deployment runners in the same transaction. The lock is taken BEFORE creating the
    # bookkeeping table so two first-time runners cannot race on CREATE TABLE IF NOT EXISTS.
    conn.execute('SELECT pg_advisory_xact_lock(718204061)')
    conn.execute('CREATE TABLE IF NOT EXISTS public.telco_migration(version text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())')
    for path in sorted((ROOT/'sql').glob('[0-9][0-9][0-9]_*.sql')):
        sql = path.read_text(encoding='utf-8')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        row = conn.execute('SELECT sha256 FROM public.telco_migration WHERE version=%s', (path.name,)).fetchone()
        if row:
            if row[0] != digest:
                raise ValueError('Applied migration checksum changed: '+path.name)
            continue
        conn.execute(sql)
        conn.execute('INSERT INTO public.telco_migration(version,sha256) VALUES(%s,%s)', (path.name,digest))


def load(conn, directory):
    from psycopg import sql
    directory = Path(directory)
    manifest = json.loads((directory/'manifest.json').read_text())
    for filename, spec in manifest['files'].items():
        if hashlib.sha256((directory/filename).read_bytes()).hexdigest() != spec['sha256']:
            raise ValueError('Checksum mismatch: '+filename)
    conn.execute('SELECT pg_advisory_xact_lock(718204062)')
    if conn.execute('SELECT EXISTS(SELECT 1 FROM telco.sell_in) OR EXISTS(SELECT 1 FROM telco.supply) OR EXISTS(SELECT 1 FROM telco.province)').fetchone()[0]:
        raise ValueError('Load requires empty schema; refusing duplicate/replacement load')
    for name in ORDER:
        path = directory/(name+'.csv')
        with path.open(encoding='utf-8',newline='') as f:
            columns = next(csv.reader(f))
        query = sql.SQL('COPY telco.{} ({}) FROM STDIN WITH (FORMAT CSV, HEADER TRUE)').format(
            sql.Identifier(name), sql.SQL(',').join(map(sql.Identifier,columns)))
        with path.open('rb') as f, conn.cursor() as cur, cur.copy(query) as cp:
            while block := f.read(1024*1024):
                cp.write(block)
    actual = conn.execute('SELECT (SELECT count(*) FROM telco.sell_in)+(SELECT count(*) FROM telco.sell_out)').fetchone()[0]
    if actual != manifest['rows']:
        raise ValueError('Row-count mismatch')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action',choices=['migrate','load','validate'])
    ap.add_argument('--data',default='data/full')
    a = ap.parse_args()
    with connect() as conn:
        if a.action=='migrate': migrate(conn)
        elif a.action=='load': load(conn,a.data)
        else: conn.execute((ROOT/'sql/validate.sql').read_text())
    print(a.action+': OK')


if __name__=='__main__': main()

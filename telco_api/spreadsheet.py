"""Spreadsheet = recap/export or validated import staging. Never a stock source of truth; never writes stock_balance."""
import csv
import hashlib
import io
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from psycopg.types.json import Jsonb

from . import authz, errors, transactions
from .money import dec, money_str, stringify_money
from .schemas import DraftRequest, SaleLine
from .security import Principal, load_principal

EXPORT_VERSION = 'export-sales-v1'
IMPORT_COLUMNS = ['row_key', 'occurred_at', 'outlet_code', 'sku', 'quantity', 'unit_price', 'discount_idr', 'customer_token']


def export_sales_csv(db, p: Principal, scope: str, entity_id: int, channel: str, start: date, end: date) -> tuple[str, dict]:
    if (end - start).days <= 0 or (end - start).days > 366:
        raise errors.bad_request('INVALID_PERIOD', 'Rentang export 1–366 hari (akhir eksklusif).')

    def q(conn):
        authz.require(conn, p, scope, entity_id)
        wm = conn.execute('SELECT complete_through,batch_key FROM telco.ingestion_watermark WHERE source=%s', (channel,)).fetchone()
        rows = conn.execute('''SELECT s.day,o.code,o.name,p.sku,p.stock_unit,sum(s.quantity),sum(s.revenue_idr),sum(s.net_lines)
            FROM telco.v_daily_sales s JOIN telco.outlet o ON o.id=s.outlet_id JOIN telco.product p ON p.id=s.product_id
            JOIN telco.district dd ON dd.id=o.district_id JOIN telco.regency rr ON rr.id=dd.regency_id
            WHERE s.channel=%s AND s.day>=%s AND s.day<%s AND ((%s='outlet' AND o.id=%s) OR (%s='district' AND o.district_id=%s)
              OR (%s='regency' AND dd.regency_id=%s) OR (%s='province' AND rr.province_id=%s))
            GROUP BY 1,2,3,4,5 ORDER BY 1,2,4''', (channel, start, end, scope, entity_id, scope, entity_id, scope, entity_id,
                                                  scope, entity_id)).fetchall()
        names = {'outlet': "SELECT code||' · '||name FROM telco.outlet WHERE id=%s",
                 'district': "SELECT 'Kecamatan '||name||' ('||code||')' FROM telco.district WHERE id=%s",
                 'regency': "SELECT name||' ('||code||')' FROM telco.regency WHERE id=%s",
                 'province': "SELECT 'Provinsi '||name FROM telco.province WHERE id=%s"}
        scope_name = conn.execute(names[scope], (entity_id,)).fetchone()[0]
        return wm, rows, scope_name

    wm, rows, scope_name = db.read(q)
    total = sum((dec(r[6]) for r in rows), Decimal(0))
    qty_by_unit: dict[str, Decimal] = {}
    for r in rows:
        qty_by_unit[r[4]] = qty_by_unit.get(r[4], Decimal(0)) + dec(r[5])
    meta = {'generated_at': datetime.now(timezone.utc).isoformat(), 'export_version': EXPORT_VERSION, 'principal': p.principal_id,
            'scope': scope, 'entity_id': entity_id, 'channel': channel, 'period_start': start.isoformat(),
            'period_end_exclusive': end.isoformat(), 'timezone': 'Asia/Jakarta',
            'watermark': str(wm[0]) if wm else None, 'source_batch_key': wm[1] if wm else None,
            'revenue_unit': 'IDR net (tanpa pajak)', 'total_revenue_idr': money_str(total),
            'quantity_pcs': str(qty_by_unit.get('PCS', Decimal(0))), 'quantity_idr': str(qty_by_unit.get('IDR', Decimal(0))),
            'scope_name': scope_name, 'rows': len(rows),
            'note': 'Rekap read-only; bukan sumber kebenaran stok. Quantity PCS dan IDR tidak dijumlahkan.'}
    buf = io.StringIO()
    for k, v in meta.items():
        buf.write(f'# {k}: {v}\n')
    w = csv.writer(buf, lineterminator='\n')
    w.writerow(['day_wib', 'outlet_code', 'outlet_name', 'sku', 'stock_unit', 'quantity', 'revenue_idr', 'net_lines'])
    for r in rows:
        w.writerow([r[0].isoformat(), r[1], r[2], r[3], r[4], str(r[5]), money_str(r[6]), r[7]])
    w.writerow(['TOTAL', '', '', '', '', '', money_str(total), ''])
    return buf.getvalue(), meta


def stage_import(db, s, p: Principal, filename: str, content: bytes, correlation_id: str) -> dict:
    """Stage a sell-out recap CSV. Idempotent per file hash. Validation never posts anything."""
    authz.require_role(p, 'admin')
    sha = hashlib.sha256(content).hexdigest()
    existing = db.write(lambda conn: conn.execute('SELECT id,status,preview FROM telco.import_file WHERE sha256=%s', (sha,)).fetchone())
    if existing:
        return {'import_id': str(existing[0]), 'status': existing[1], 'preview': existing[2], 'idempotent_replay': True}
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise errors.bad_request('INVALID_FILE', 'File harus CSV UTF-8.')
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != IMPORT_COLUMNS:
        raise errors.bad_request('INVALID_FILE', 'Header CSV harus: ' + ','.join(IMPORT_COLUMNS))
    rows = list(reader)
    if not rows or len(rows) > 5000:
        raise errors.bad_request('INVALID_FILE', 'Jumlah baris 1–5000.')

    fid = uuid.uuid4()

    def tx(conn):
        staged, valid = [], 0
        totals: dict = {'revenue_idr': Decimal(0), 'quantity_by_unit': {}, 'invalid_rows': 0, 'duplicate_rows': 0}
        need: dict[tuple[int, int], Decimal] = {}
        for i, raw in enumerate(rows, 1):
            errs, norm = [], None
            try:
                occurred = datetime.fromisoformat(raw['occurred_at'])
                if occurred.tzinfo is None:
                    errs.append('occurred_at wajib memiliki zona waktu')
                o = conn.execute('SELECT id FROM telco.outlet WHERE code=%s', (raw['outlet_code'],)).fetchone()
                pr = conn.execute('SELECT id,stock_unit FROM telco.product WHERE sku=%s', (raw['sku'],)).fetchone()
                if not o:
                    errs.append('outlet_code tidak dikenal')
                if not pr:
                    errs.append('sku tidak dikenal')
                qty = int(raw['quantity'])
                if qty <= 0 or str(qty) != raw['quantity'].strip():
                    errs.append('quantity harus bilangan bulat positif')
                price, disc = Decimal(raw['unit_price']), Decimal(raw['discount_idr'] or '0')
                if not raw['customer_token'].startswith('SYN-'):
                    errs.append('customer_token harus SYN-*')
                if not errs:
                    # Reuse the exact domain validation as the posting sales principal of that outlet/date.
                    a = conn.execute('''SELECT a.sales_id,ap.principal_id FROM telco.assignment a
                        LEFT JOIN telco.app_principal ap ON ap.sales_id=a.sales_id AND ap.active
                        WHERE a.outlet_id=%s AND a.valid_from<=%s AND a.valid_to>%s''',
                                     (o[0], occurred.date(), occurred.date())).fetchone()
                    if not a or not a[1]:
                        errs.append('tidak ada sales aktif yang ditugaskan')
                    else:
                        sp = load_principal(conn, a[1], 'import')
                        d = DraftRequest(channel='sell_out', outlet_id=o[0], occurred_at=occurred, customer_token=raw['customer_token'],
                                         lines=[SaleLine(product_id=pr[0], quantity=qty, unit_price=money_str(price) if price % 1 else str(int(price)),
                                                         discount_idr=str(disc))])
                        try:
                            with conn.transaction():
                                n = _normalize_as(conn, s, sp, d)
                            norm = stringify_money({**n, 'posting_principal': a[1]})
                            totals['revenue_idr'] += n['lines'][0]['net_revenue_idr']
                            u = pr[1]
                            totals['quantity_by_unit'][u] = totals['quantity_by_unit'].get(u, Decimal(0)) + qty
                            key = (o[0], pr[0])
                            need[key] = need.get(key, Decimal(0)) + qty
                        except errors.AppError as exc:
                            errs.append(f'{exc.code}: {exc.message}')
            except (ValueError, InvalidOperation, KeyError) as exc:
                errs.append(f'format: {type(exc).__name__}')
            ext = f"IMP-{sha[:12]}-{raw.get('row_key') or i}"
            dup = conn.execute("SELECT 1 FROM telco.posting_request WHERE channel='sell_out' AND external_key=%s", (ext + '-1',)).fetchone()
            status = 'duplicate' if dup else ('invalid' if errs else 'staged')
            if status == 'staged':
                valid += 1
            elif status == 'invalid':
                totals['invalid_rows'] += 1
            else:
                totals['duplicate_rows'] += 1
            staged.append((fid, i, Jsonb(raw), Jsonb(norm) if norm else None, Jsonb(errs), ext, status))
        stock_check = []
        for (oid, pid), q in need.items():
            acct = conn.execute('SELECT id FROM telco.stock_account WHERE outlet_id=%s', (oid,)).fetchone()[0]
            avail = transactions._available(conn, acct, pid)
            stock_check.append({'outlet_id': oid, 'product_id': pid, 'required': str(q), 'available_now': str(avail),
                                'sufficient': avail >= q})
        preview = stringify_money({'file_sha256': sha, 'rows': len(rows), 'valid_rows': valid, **totals,
                                   'stock_check': stock_check, 'channel': 'sell_out',
                                   'note': 'Preview saja. Posting hanya setelah konfirmasi admin; stok tidak ditimpa.'})
        conn.execute('''INSERT INTO telco.import_file(id,sha256,filename,kind,uploaded_by,status,row_count,valid_rows,preview)
            VALUES(%s,%s,%s,'sell_out',%s,%s,%s,%s,%s)''',
                     (fid, sha, filename[:200], p.principal_id, 'staged' if valid else 'rejected', len(rows), valid, Jsonb(preview)))
        with conn.cursor() as cur:
            cur.executemany('''INSERT INTO telco.import_row(file_id,row_no,raw,normalized,errors,external_key,status)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (external_key) DO NOTHING''', staged)
        return {'import_id': str(fid), 'status': 'staged' if valid else 'rejected', 'preview': preview}

    return db.write(tx)


def _normalize_as(conn, s, sp, d):
    return transactions.normalize_draft(conn, s, sp, d)


def confirm_import(db, s, p: Principal, import_id: str, correlation_id: str) -> dict:
    authz.require_role(p, 'admin')

    def tx(conn):
        f = conn.execute('SELECT status,uploaded_by FROM telco.import_file WHERE id=%s FOR UPDATE', (import_id,)).fetchone()
        if not f:
            raise errors.not_found('Import tidak ditemukan.')
        if f[0] == 'posted':
            return {'import_id': import_id, 'status': 'posted', 'idempotent_replay': True}
        if f[0] != 'staged':
            raise errors.conflict('IMPORT_NOT_STAGED', f'Import berstatus {f[0]}.')
        posted = 0
        for row_no, norm, ext in conn.execute('''SELECT row_no,normalized,external_key FROM telco.import_row
            WHERE file_id=%s AND status='staged' ORDER BY row_no''', (import_id,)).fetchall():
            sp = load_principal(conn, norm['posting_principal'], 'import')
            n = transactions._rehydrate_lines(norm)
            res = transactions.post_normalized(conn, s, sp, n, ext)
            conn.execute("UPDATE telco.import_row SET status='posted',transaction_id=%s WHERE file_id=%s AND row_no=%s",
                         (res[0]['transaction_id'], import_id, row_no))
            posted += 1
        conn.execute("UPDATE telco.import_file SET status='posted',confirmed_by=%s,confirmed_at=now() WHERE id=%s", (p.principal_id, import_id))
        return {'import_id': import_id, 'status': 'posted', 'posted_rows': posted}

    return db.write(tx)

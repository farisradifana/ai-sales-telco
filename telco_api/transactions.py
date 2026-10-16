"""Domain writes. All posting goes through INSERT into immutable event tables; triggers write the ledger and balance
atomically in the same transaction. sales_id/assignment_id are derived server-side from the principal + date."""
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from psycopg import errors as pgerr
from psycopg.types.json import Jsonb

from . import authz, errors
from .money import canonical_json, dec, money_str, rupiah, stringify_money
from .periods import WIB
from .schemas import CorrectionRequest, DirectSale, DraftRequest
from .security import Principal

CENT = Decimal('0.01')


def payload_hash(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ validation
def _outlet(conn, outlet_id=None, code=None):
    if outlet_id is not None:
        r = conn.execute('SELECT id,code,name,opened_on,closed_on FROM telco.outlet WHERE id=%s', (outlet_id,)).fetchone()
    else:
        r = conn.execute('SELECT id,code,name,opened_on,closed_on FROM telco.outlet WHERE code=%s', (code,)).fetchone()
    return r


def check_posting_window(conn, s, channel: str, occurred_at: datetime, *, correction: bool = False) -> None:
    now = _now()
    if occurred_at > now + timedelta(seconds=s.future_skew_seconds):
        raise errors.unprocessable('FUTURE_TIMESTAMP', 'Waktu transaksi di masa depan.')
    wm = conn.execute('SELECT complete_through FROM telco.ingestion_watermark WHERE source=%s', (channel,)).fetchone()
    local_day = occurred_at.astimezone(WIB).date()
    if wm and local_day <= wm[0]:
        raise errors.unprocessable('BACKDATE_REJECTED', 'Periode sudah ditutup oleh watermark ingestion; tidak bisa diposting.',
                                   closed_through=str(wm[0]))
    if not correction and occurred_at < now - timedelta(hours=s.posting_grace_hours):
        raise errors.unprocessable('BACKDATE_REJECTED', f'Transaksi lebih lama dari {s.posting_grace_hours} jam; gunakan jalur koreksi supervisor.')


def normalize_draft(conn, s, p: Principal, req: DraftRequest) -> dict:
    """Validate everything that can be validated before locks: role, scope, assignment, policy, units."""
    if p.role != 'sales' or p.sales_id is None:
        raise errors.role_denied('Hanya akun sales yang dapat menginput penjualan.')
    o = _outlet(conn, req.outlet_id, req.outlet_code)
    if not o:
        raise errors.denied()  # do not reveal whether the outlet exists
    authz.require(conn, p, 'outlet', o[0])
    occurred_at = (req.occurred_at or _now()).astimezone(timezone.utc)
    check_posting_window(conn, s, req.channel, occurred_at)
    day = occurred_at.astimezone(WIB).date()
    if not (o[3] <= day and (o[4] is None or day < o[4])):
        raise errors.unprocessable('OUTLET_CLOSED', 'Outlet tidak buka pada tanggal transaksi.')
    a = conn.execute('''SELECT id,sales_id FROM telco.assignment WHERE outlet_id=%s AND valid_from<=%s AND valid_to>%s''',
                     (o[0], day, day)).fetchone()
    if not a or a[1] != p.sales_id:
        raise errors.unprocessable('INVALID_ASSIGNMENT', 'Outlet tidak ditugaskan ke Anda pada tanggal transaksi.')
    lines, seen = [], set()
    for i, ln in enumerate(req.lines, 1):
        pr = conn.execute('SELECT id,sku,name,stock_unit,category FROM telco.product WHERE ' +
                          ('id=%s' if ln.product_id else 'sku=%s'), (ln.product_id or ln.sku,)).fetchone()
        if not pr:
            raise errors.bad_request('UNKNOWN_PRODUCT', f'Produk baris {i} tidak dikenal.')
        if pr[0] in seen:
            raise errors.bad_request('DUPLICATE_LINE', f'Produk {pr[1]} muncul lebih dari sekali; gabungkan kuantitasnya.')
        seen.add(pr[0])
        pol = conn.execute('''SELECT unit_price,max_discount_pct FROM telco.price_policy WHERE product_id=%s AND channel=%s
            AND valid_from<=%s AND valid_to>%s''', (pr[0], req.channel, day, day)).fetchall()
        if len(pol) != 1:
            raise errors.unprocessable('PRICE_POLICY_MISSING', f'Kebijakan harga {pr[1]} untuk {req.channel} belum tersedia.')
        price, max_pct = dec(pol[0][0]), dec(pol[0][1])
        if ln.unit_price is not None and dec(ln.unit_price) != price:
            raise errors.unprocessable('PRICE_POLICY_VIOLATION', f'Harga {pr[1]} harus {money_str(price)} per unit sesuai kebijakan.')
        qty = Decimal(ln.quantity)
        gross = (qty * price).quantize(CENT, rounding=ROUND_HALF_UP)
        disc = dec(ln.discount_idr)
        if disc > (gross * max_pct / 100).quantize(CENT, rounding=ROUND_HALF_UP):
            raise errors.unprocessable('PRICE_POLICY_VIOLATION', f'Diskon {pr[1]} melebihi batas {max_pct}%.')
        lines.append({'line': i, 'product_id': pr[0], 'sku': pr[1], 'product': pr[2], 'stock_unit': pr[3], 'category': pr[4],
                      'quantity': qty, 'unit_price': price, 'discount_idr': disc, 'net_revenue_idr': gross - disc})
    return {'channel': req.channel, 'outlet_id': o[0], 'outlet_code': o[1], 'outlet_name': o[2], 'sales_id': p.sales_id,
            'assignment_id': a[0], 'occurred_at': occurred_at.isoformat(), 'customer_token': req.customer_token, 'lines': lines}


def summary_text(n: dict, code: str, ttl_min: int) -> str:
    ch = 'SELL-IN' if n['channel'] == 'sell_in' else 'SELL-OUT'
    t = datetime.fromisoformat(n['occurred_at']).astimezone(WIB)
    out = [f"Konfirmasi {ch} {n['outlet_code']} ({n['outlet_name']}), {t:%d-%m-%Y %H:%M} WIB:"]
    pcs, saldo, total = Decimal(0), Decimal(0), Decimal(0)
    for ln in n['lines']:
        q = dec(ln['quantity'])
        unit = 'pcs' if ln['stock_unit'] == 'PCS' else 'nominal saldo'
        qty = f"{int(q):,}".replace(',', '.')
        disc = f" diskon {rupiah(ln['discount_idr'])}" if dec(ln['discount_idr']) > 0 else ''
        out.append(f"{ln['line']}. {ln['sku']} x{qty} {unit} @{rupiah(ln['unit_price'])}{disc} = {rupiah(ln['net_revenue_idr'])}")
        total += dec(ln['net_revenue_idr'])
        if ln['stock_unit'] == 'PCS':
            pcs += q
        else:
            saldo += q
    out.append(f"Total omzet {rupiah(total)} (fisik {int(pcs)} pcs; saldo {rupiah(saldo)} nominal — satuan tidak dijumlahkan).")
    out.append(f"Balas YA {code} untuk posting atau BATAL {code} untuk membatalkan. Berlaku {ttl_min} menit.")
    return '\n'.join(out)


def _lock_accounts(conn, n: dict) -> dict:
    outlet_acct = conn.execute('SELECT id FROM telco.stock_account WHERE outlet_id=%s', (n['outlet_id'],)).fetchone()[0]
    ids = [outlet_acct]
    sales_acct = None
    if n['channel'] == 'sell_in':
        sales_acct = conn.execute('SELECT id FROM telco.stock_account WHERE sales_id=%s', (n['sales_id'],)).fetchone()[0]
        ids.append(sales_acct)
    # Deterministic lock order across ALL lines prevents multi-SKU deadlocks; whole tx retried on 40P01.
    conn.execute('SELECT telco.lock_stock_accounts(%s)', (sorted(ids),))
    return {'outlet': outlet_acct, 'sales': sales_acct, 'ids': sorted(ids)}


def _ledger_order_ok(conn, accts: list[int], product_id: int, occurred_at: datetime) -> bool:
    last = conn.execute('SELECT max(occurred_at) FROM telco.stock_ledger WHERE account_id=ANY(%s) AND product_id=%s',
                        (accts, product_id)).fetchone()[0]
    return last is None or occurred_at >= last


def _available(conn, acct: int, product_id: int) -> Decimal:
    r = conn.execute('SELECT quantity FROM telco.stock_balance WHERE account_id=%s AND product_id=%s', (acct, product_id)).fetchone()
    return dec(r[0]) if r else Decimal(0)


def _existing(conn, channel: str, key: str, h: str):
    r = conn.execute('SELECT payload_hash,result FROM telco.posting_request WHERE channel=%s AND external_key=%s',
                     (channel, key)).fetchone()
    if r is None:
        return None
    if r[0] != h:
        raise errors.conflict('IDEMPOTENCY_CONFLICT', 'external_key sudah dipakai dengan payload berbeda.')
    return r[1]


def post_normalized(conn, s, p: Principal, n: dict, key_prefix: str, *, correction: CorrectionRequest | None = None) -> list[dict]:
    """Post every line of a normalized draft in the CURRENT transaction (caller commits once)."""
    occurred_at = datetime.fromisoformat(n['occurred_at'])
    check_posting_window(conn, s, n['channel'], occurred_at, correction=correction is not None)
    accts = _lock_accounts(conn, n)
    results = []
    for ln in n['lines']:
        key = f"{key_prefix}-{ln['line']}" if correction is None else correction.external_key
        line_payload = {k: n[k] for k in ('channel', 'outlet_id', 'sales_id', 'assignment_id', 'occurred_at', 'customer_token')}
        line_payload.update({k: ln[k] for k in ('product_id', 'quantity', 'unit_price', 'discount_idr')})
        h = payload_hash(line_payload)
        prior = _existing(conn, n['channel'], key, h)
        if prior is not None:
            results.append({**prior, 'idempotent_replay': True})
            continue
        if correction is None and not _ledger_order_ok(conn, accts['ids'], ln['product_id'], occurred_at):
            raise errors.unprocessable('BACKDATE_REJECTED', 'Ada mutasi stok yang lebih baru; transaksi mundur harus lewat jalur koreksi.')
        src = accts['sales'] if n['channel'] == 'sell_in' else accts['outlet']
        if _available(conn, src, ln['product_id']) < dec(ln['quantity']):
            raise errors.unprocessable('INSUFFICIENT_STOCK', f"Stok {ln['sku']} tidak cukup "
                                       f"({'stok sales' if n['channel'] == 'sell_in' else 'stok outlet'}).",
                                       sku=ln['sku'], available=str(_available(conn, src, ln['product_id'])))
        seq = 'telco.sell_in_api_id' if n['channel'] == 'sell_in' else 'telco.sell_out_api_id'
        table = 'sell_in' if n['channel'] == 'sell_in' else 'sell_out'
        cols = 'id,external_key,occurred_at,outlet_id,sales_id,assignment_id,product_id,quantity,unit_price,discount_idr'
        vals = [key, occurred_at, n['outlet_id'], n['sales_id'], n['assignment_id'], ln['product_id'], ln['quantity'],
                ln['unit_price'], ln['discount_idr']]
        if table == 'sell_out':
            cols += ',customer_token'
            vals.append(n.get('customer_token') or f"SYN-API-{key[-24:].upper()}")
        try:
            with conn.transaction():  # savepoint: map constraint errors precisely
                tx_id, net = conn.execute(f'INSERT INTO telco.{table}({cols}) VALUES(nextval(%s),{",".join(["%s"] * len(vals))}) '
                                          'RETURNING id,net_revenue', [seq, *vals]).fetchone()
        except pgerr.CheckViolation as exc:
            if 'stock_balance' in str(exc):
                raise errors.unprocessable('INSUFFICIENT_STOCK', f"Stok {ln['sku']} tidak cukup.", sku=ln['sku'])
            raise errors.unprocessable('CONSTRAINT_VIOLATION', 'Transaksi melanggar aturan data.')
        except pgerr.RaiseException as exc:
            msg = str(exc).split('\n')[0]
            code = 'INVALID_ASSIGNMENT' if 'assignment' in msg else 'OUTLET_CLOSED' if 'outlet' in msg else 'POSTING_REJECTED'
            raise errors.unprocessable(code, 'Transaksi ditolak aturan posting: ' + msg)
        res = {'channel': n['channel'], 'transaction_id': tx_id, 'external_key': key, 'sku': ln['sku'],
               'quantity': str(ln['quantity']), 'stock_unit': ln['stock_unit'], 'net_revenue_idr': str(net)}
        if correction is not None:
            for acct in accts['ids']:
                mrb = conn.execute('SELECT telco.min_running_balance(%s,%s)', (acct, ln['product_id'])).fetchone()[0]
                if dec(mrb) < 0:
                    raise errors.unprocessable('HISTORICAL_STOCK_NEGATIVE', 'Koreksi mundur membuat stok historis negatif; ditolak.')
            conn.execute('''INSERT INTO telco.correction_audit(channel,external_key,occurred_at,approved_by,reason,min_running_balance)
                VALUES(%s,%s,%s,%s,%s,%s)''', (n['channel'], key, occurred_at, p.principal_id, correction.reason,
                                              min(dec(conn.execute('SELECT telco.min_running_balance(%s,%s)', (a, ln['product_id'])).fetchone()[0])
                                                  for a in accts['ids'])))
        conn.execute('INSERT INTO telco.posting_request(channel,external_key,payload_hash,principal_id,transaction_id,result) '
                     'VALUES(%s,%s,%s,%s,%s,%s)', (n['channel'], key, h, p.principal_id, tx_id, Jsonb(res)))
        results.append(res)
    return results


# ------------------------------------------------------------------ drafts (WhatsApp / dashboard)
def create_draft(db, s, p: Principal, req: DraftRequest, correlation_id: str, source_ref: str | None = None) -> dict:
    def tx(conn):
        n = normalize_draft(conn, s, p, req)
        # Advisory stock check for a clear message before the user confirms (re-checked under lock at posting).
        acct_sql = ('SELECT id FROM telco.stock_account WHERE sales_id=%s', n['sales_id']) if n['channel'] == 'sell_in' \
            else ('SELECT id FROM telco.stock_account WHERE outlet_id=%s', n['outlet_id'])
        acct = conn.execute(acct_sql[0], (acct_sql[1],)).fetchone()[0]
        short = []
        for ln in n['lines']:
            avail = _available(conn, acct, ln['product_id'])
            if avail < dec(ln['quantity']):
                short.append({'sku': ln['sku'], 'requested': str(ln['quantity']), 'available': str(avail), 'unit': ln['stock_unit']})
        if short:
            raise errors.unprocessable('INSUFFICIENT_STOCK', 'Stok tidak cukup untuk: ' +
                                       ', '.join(f"{x['sku']} (diminta {x['requested']}, tersedia {x['available']})" for x in short),
                                       lines=short)
        aid = uuid.uuid4()
        code = aid.hex[:6].upper()
        n['confirm_code'] = code
        text = summary_text(n, code, s.draft_ttl_minutes)
        h = payload_hash(n)
        conn.execute('''INSERT INTO telco.pending_action(id,principal_id,kind,payload,payload_hash,summary,confirm_role,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s,'self',now()+make_interval(mins=>%s))''',
                     (aid, p.principal_id, n['channel'], Jsonb(stringify_money(n)), h, text, s.draft_ttl_minutes))
        conn.execute('INSERT INTO telco.action_audit(action_id,actor,event,event_ref,payload_hash,correlation_id) VALUES(%s,%s,%s,%s,%s,%s)',
                     (aid, p.principal_id, 'draft_created', source_ref, h, correlation_id))
        return {'draft_id': str(aid), 'confirm_code': code, 'summary': text, 'status': 'pending',
                'expires_in_minutes': s.draft_ttl_minutes, 'payload_hash': h}
    return db.write(tx)


def _rehydrate_lines(n: dict) -> dict:
    n = dict(n)
    n['lines'] = [{**ln, 'quantity': dec(ln['quantity']), 'unit_price': dec(ln['unit_price']), 'discount_idr': dec(ln['discount_idr']),
                   'net_revenue_idr': dec(ln['net_revenue_idr'])} for ln in n['lines']]
    return n


def find_draft_by_code(conn, p: Principal, code: str):
    rows = conn.execute('''SELECT id FROM telco.pending_action WHERE principal_id=%s AND kind IN ('sell_in','sell_out')
        AND payload->>'confirm_code'=%s ORDER BY created_at DESC LIMIT 2''', (p.principal_id, code.upper())).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def confirm_draft(db, s, p: Principal, draft_id, confirmation_event_id: str, correlation_id: str, on_posted=None) -> dict:
    def tx(conn):
        r = conn.execute('''SELECT id,principal_id,kind,payload,status,expires_at>now(),result,confirm_role FROM telco.pending_action
            WHERE id=%s FOR UPDATE''', (draft_id,)).fetchone()
        if not r or r[1] != p.principal_id or r[2] not in ('sell_in', 'sell_out'):
            raise errors.not_found('Draft tidak ditemukan.')
        if r[4] == 'posted':
            return {**r[6], 'idempotent_replay': True}
        if r[4] != 'pending':
            raise errors.conflict('ACTION_NOT_PENDING', f'Draft berstatus {r[4]}.', status=r[4])
        if not r[5]:
            conn.execute("UPDATE telco.pending_action SET status='expired' WHERE id=%s", (draft_id,))
            return {'status': 'expired'}
        authz.require(conn, p, 'outlet', r[3]['outlet_id'])  # grant may have been revoked since the draft
        n = _rehydrate_lines(r[3])
        lines = post_normalized(conn, s, p, n, f"DRF-{uuid.UUID(str(draft_id)).hex[:16]}")
        result = {'status': 'posted', 'draft_id': str(draft_id), 'lines': lines,
                  'total_net_revenue_idr': money_str(sum(dec(x['net_revenue_idr']) for x in lines))}
        ch = hashlib.sha256(confirmation_event_id.encode()).hexdigest()
        conn.execute('''UPDATE telco.pending_action SET status='posted',confirmed_by=%s,confirmation_event_id=%s,
            confirmation_payload_hash=%s,confirmed_at=now(),result=%s WHERE id=%s''',
                     (p.principal_id, confirmation_event_id, ch, Jsonb(result), draft_id))
        conn.execute('INSERT INTO telco.action_audit(action_id,actor,event,event_ref,payload_hash,correlation_id) VALUES(%s,%s,%s,%s,%s,%s)',
                     (draft_id, p.principal_id, 'confirmed_posted', confirmation_event_id, ch, correlation_id))
        if on_posted:
            on_posted(conn, result)  # e.g. outbox reply in the SAME commit
        return result

    try:
        res = db.write(tx)
    except errors.AppError as exc:
        if exc.code in ('INSUFFICIENT_STOCK', 'BACKDATE_REJECTED', 'INVALID_ASSIGNMENT', 'OUTLET_CLOSED', 'SCOPE_DENIED'):
            # Terminal for this draft: mark rejected so retries do not loop on the same failure.
            def rej(conn):
                conn.execute("UPDATE telco.pending_action SET status='rejected',result=%s WHERE id=%s AND status='pending'",
                             (Jsonb({'status': 'rejected', 'code': exc.code, 'message': exc.message}), draft_id))
                conn.execute('INSERT INTO telco.action_audit(action_id,actor,event,event_ref,correlation_id) VALUES(%s,%s,%s,%s,%s)',
                             (draft_id, p.principal_id, 'rejected:' + exc.code, confirmation_event_id, correlation_id))
            db.write(rej)
        raise
    if res.get('status') == 'expired':
        raise errors.gone('DRAFT_EXPIRED', 'Draft kedaluwarsa; kirim ulang data penjualan.')
    return res


def cancel_draft(db, p: Principal, draft_id, event_ref: str, correlation_id: str) -> dict:
    def tx(conn):
        r = conn.execute("UPDATE telco.pending_action SET status='cancelled' WHERE id=%s AND principal_id=%s AND status='pending' RETURNING id",
                         (draft_id, p.principal_id)).fetchone()
        if not r:
            raise errors.conflict('ACTION_NOT_PENDING', 'Draft tidak dalam status pending.')
        conn.execute('INSERT INTO telco.action_audit(action_id,actor,event,event_ref,correlation_id) VALUES(%s,%s,%s,%s,%s)',
                     (draft_id, p.principal_id, 'cancelled', event_ref, correlation_id))
        return {'status': 'cancelled', 'draft_id': str(draft_id)}
    return db.write(tx)


def post_direct(db, s, p: Principal, channel: str, req: DirectSale, correlation_id: str) -> dict:
    """Programmatic posting with a caller-held confirmation id; same validation/locks/idempotency as drafts."""
    from .schemas import SaleLine

    def tx(conn):
        d = DraftRequest(channel=channel, outlet_id=req.outlet_id, occurred_at=req.occurred_at, customer_token=req.customer_token,
                         lines=[SaleLine(product_id=req.product_id, quantity=req.quantity, unit_price=req.unit_price,
                                         discount_idr=req.discount_idr)])
        n = normalize_draft(conn, s, p, d)
        n['lines'][0]['line'] = 1
        res = post_normalized(conn, s, p, n, req.external_key)
        # The single line uses "<external_key>-1"; expose the caller key too.
        return {'status': 'posted', 'client_confirmation_id': req.client_confirmation_id, 'lines': res}
    return db.write(tx)


def post_correction(db, s, p: Principal, req: CorrectionRequest, correlation_id: str) -> dict:
    """Supervisor-only backdated path: locks, inserts, proves chronological balance >= 0, audits; else rolls back."""
    authz.require_supervisor(p)
    from .schemas import SaleLine

    def tx(conn):
        authz.require(conn, p, 'outlet', req.outlet_id)
        day = req.occurred_at.astimezone(WIB).date()
        a = conn.execute('SELECT id,sales_id FROM telco.assignment WHERE outlet_id=%s AND valid_from<=%s AND valid_to>%s',
                         (req.outlet_id, day, day)).fetchone()
        if not a:
            raise errors.unprocessable('INVALID_ASSIGNMENT', 'Tidak ada penugasan valid pada tanggal tersebut.')
        sales_p = Principal('correction', 'sales', a[1], 'correction', 'jwt')
        d = DraftRequest(channel=req.channel, outlet_id=req.outlet_id, occurred_at=req.occurred_at, customer_token=req.customer_token,
                         lines=[SaleLine(product_id=req.product_id, quantity=req.quantity, unit_price=req.unit_price,
                                         discount_idr=req.discount_idr)])
        n = _normalize_for_correction(conn, s, sales_p, d, a[0])
        return {'status': 'posted', 'correction': True,
                'lines': post_normalized(conn, s, p, n, req.external_key, correction=req)}
    return db.write(tx)


def _normalize_for_correction(conn, s, sales_p, d, assignment_id):
    # Same policy checks as normal drafts except the grace-window/ledger-order rule (audited instead).
    o = _outlet(conn, d.outlet_id)
    ln = d.lines[0]
    pr = conn.execute('SELECT id,sku,name,stock_unit,category FROM telco.product WHERE id=%s', (ln.product_id,)).fetchone()
    if not pr:
        raise errors.bad_request('UNKNOWN_PRODUCT', 'Produk tidak dikenal.')
    day = d.occurred_at.astimezone(WIB).date()
    pol = conn.execute('SELECT unit_price,max_discount_pct FROM telco.price_policy WHERE product_id=%s AND channel=%s AND valid_from<=%s AND valid_to>%s',
                       (pr[0], d.channel, day, day)).fetchall()
    if len(pol) != 1 or dec(ln.unit_price) != dec(pol[0][0]):
        raise errors.unprocessable('PRICE_POLICY_VIOLATION', 'Harga tidak sesuai kebijakan pada tanggal koreksi.')
    qty, price, disc = Decimal(ln.quantity), dec(ln.unit_price), dec(ln.discount_idr)
    gross = (qty * price).quantize(CENT, rounding=ROUND_HALF_UP)
    if disc > (gross * dec(pol[0][1]) / 100).quantize(CENT):
        raise errors.unprocessable('PRICE_POLICY_VIOLATION', 'Diskon melebihi kebijakan.')
    return {'channel': d.channel, 'outlet_id': o[0], 'outlet_code': o[1], 'outlet_name': o[2], 'sales_id': sales_p.sales_id,
            'assignment_id': assignment_id, 'occurred_at': d.occurred_at.astimezone(timezone.utc).isoformat(),
            'customer_token': d.customer_token,
            'lines': [{'line': 1, 'product_id': pr[0], 'sku': pr[1], 'product': pr[2], 'stock_unit': pr[3], 'category': pr[4],
                       'quantity': qty, 'unit_price': price, 'discount_idr': disc, 'net_revenue_idr': gross - disc}]}


# ------------------------------------------------------------------ supervisor-confirmed actions
def request_reversal(db, s, p: Principal, req, correlation_id: str) -> dict:
    def tx(conn):
        table = 'sell_in' if req.channel == 'sell_in' else 'sell_out'
        t = conn.execute(f'SELECT id,outlet_id,product_id,quantity,net_revenue,occurred_at FROM telco.{table} WHERE id=%s',
                         (req.transaction_id,)).fetchone()
        if not t or not authz.can_access(conn, p, 'outlet', t[1]):
            raise errors.not_found('Transaksi tidak ditemukan.')
        if conn.execute('SELECT 1 FROM telco.reversal WHERE channel=%s AND transaction_id=%s', (req.channel, t[0])).fetchone():
            raise errors.conflict('ALREADY_REVERSED', 'Transaksi sudah direversal.')
        payload = {'channel': req.channel, 'transaction_id': t[0], 'outlet_id': t[1], 'reason': req.reason,
                   'original': {'product_id': t[2], 'quantity': str(t[3]), 'net_revenue_idr': str(t[4]), 'occurred_at': t[5].isoformat()},
                   'replacement': req.replacement.model_dump(mode='json') if req.replacement else None, 'requested_by': p.principal_id}
        aid = uuid.uuid4()
        text = (f"Permintaan reversal penuh {req.channel} #{t[0]} (omzet {rupiah(t[4])}) — alasan: {req.reason}. "
                f"{'Dengan transaksi pengganti. ' if req.replacement else ''}Butuh persetujuan supervisor.")
        h = payload_hash(payload)
        conn.execute('''INSERT INTO telco.pending_action(id,principal_id,kind,payload,payload_hash,summary,confirm_role,expires_at)
            VALUES(%s,%s,'reversal',%s,%s,%s,'supervisor',now()+interval '72 hours')''', (aid, p.principal_id, Jsonb(payload), h, text))
        conn.execute('INSERT INTO telco.action_audit(action_id,actor,event,payload_hash,correlation_id) VALUES(%s,%s,%s,%s,%s)',
                     (aid, p.principal_id, 'reversal_requested', h, correlation_id))
        return {'action_id': str(aid), 'status': 'pending', 'summary': text, 'confirm_role': 'supervisor'}
    return db.write(tx)


def request_transfer(db, s, p: Principal, req, correlation_id: str) -> dict:
    if p.role != 'sales':
        raise errors.role_denied('Transfer diajukan oleh sales pemegang stok.')

    def tx(conn):
        if req.to_sales_id == p.sales_id:
            raise errors.bad_request('INVALID_INPUT', 'Tujuan transfer sama dengan pengirim.')
        pr = conn.execute('SELECT id,sku,stock_unit FROM telco.product WHERE id=%s', (req.product_id,)).fetchone()
        to = conn.execute('SELECT id FROM telco.stock_account WHERE sales_id=%s', (req.to_sales_id,)).fetchone()
        if not pr or not to:
            raise errors.not_found('Produk atau penerima tidak ditemukan.')
        payload = {'from_sales_id': p.sales_id, 'to_sales_id': req.to_sales_id, 'product_id': pr[0], 'sku': pr[1],
                   'stock_unit': pr[2], 'quantity': str(req.quantity), 'reason': req.reason, 'requested_by': p.principal_id}
        aid = uuid.uuid4()
        text = f"Permintaan transfer stok {pr[1]} x{req.quantity} {pr[2]} dari sales {p.sales_id} ke sales {req.to_sales_id}. Butuh persetujuan supervisor."
        h = payload_hash(payload)
        conn.execute('''INSERT INTO telco.pending_action(id,principal_id,kind,payload,payload_hash,summary,confirm_role,expires_at)
            VALUES(%s,%s,'transfer',%s,%s,%s,'supervisor',now()+interval '72 hours')''', (aid, p.principal_id, Jsonb(payload), h, text))
        conn.execute('INSERT INTO telco.action_audit(action_id,actor,event,payload_hash,correlation_id) VALUES(%s,%s,%s,%s,%s)',
                     (aid, p.principal_id, 'transfer_requested', h, correlation_id))
        return {'action_id': str(aid), 'status': 'pending', 'summary': text, 'confirm_role': 'supervisor'}
    return db.write(tx)


def list_actions(db, p: Principal) -> list[dict]:
    def q(conn):
        rows = conn.execute('''SELECT id,principal_id,kind,summary,status,created_at,expires_at,payload FROM telco.pending_action
            WHERE status='pending' AND expires_at>now() AND (principal_id=%s OR confirm_role='supervisor') ORDER BY created_at DESC LIMIT 100''',
                            (p.principal_id,)).fetchall()
        out = []
        for r in rows:
            mine = r[1] == p.principal_id
            if not mine:
                if not p.is_supervisor or not _supervisor_can(conn, p, r[2], r[7]):
                    continue
            out.append({'action_id': str(r[0]), 'requested_by': r[1], 'kind': r[2], 'summary': r[3], 'status': r[4],
                        'created_at': r[5].isoformat(), 'expires_at': r[6].isoformat(), 'can_decide': (not mine) and p.is_supervisor})
        return out
    return db.write(q)


def _supervisor_can(conn, p: Principal, kind: str, payload: dict) -> bool:
    if kind == 'reversal':
        return authz.can_access(conn, p, 'outlet', payload['outlet_id'])
    if kind == 'transfer':
        return authz.sales_visible(conn, p, payload['from_sales_id']) and authz.sales_visible(conn, p, payload['to_sales_id'])
    return False


def decide_action(db, s, p: Principal, action_id, decision: str, note: str, confirmation_event_id: str, correlation_id: str) -> dict:
    authz.require_supervisor(p)

    def tx(conn):
        r = conn.execute('''SELECT id,principal_id,kind,payload,status,expires_at>now(),result FROM telco.pending_action
            WHERE id=%s AND confirm_role='supervisor' FOR UPDATE''', (action_id,)).fetchone()
        if not r or not _supervisor_can(conn, p, r[2], r[3]):
            raise errors.not_found('Aksi tidak ditemukan.')
        if r[1] == p.principal_id:
            raise errors.role_denied('Pemohon tidak boleh menyetujui aksinya sendiri (four-eyes).')
        if r[4] == 'posted':
            return {**r[6], 'idempotent_replay': True}
        if r[4] != 'pending' or not r[5]:
            raise errors.conflict('ACTION_NOT_PENDING', 'Aksi tidak lagi pending.')
        ch = hashlib.sha256(confirmation_event_id.encode()).hexdigest()
        if decision == 'reject':
            res = {'status': 'rejected', 'note': note}
            conn.execute('''UPDATE telco.pending_action SET status='rejected',confirmed_by=%s,confirmation_event_id=%s,
                confirmation_payload_hash=%s,confirmed_at=now(),result=%s WHERE id=%s''', (p.principal_id, confirmation_event_id, ch, Jsonb(res), action_id))
        elif r[2] == 'reversal':
            res = _post_reversal(conn, s, p, r[0], r[1], r[3])
        else:
            res = _post_transfer(conn, p, r[0], r[3])
        if decision == 'approve':
            conn.execute('''UPDATE telco.pending_action SET status='posted',confirmed_by=%s,confirmation_event_id=%s,
                confirmation_payload_hash=%s,confirmed_at=now(),result=%s WHERE id=%s''', (p.principal_id, confirmation_event_id, ch, Jsonb(res), action_id))
        conn.execute('INSERT INTO telco.action_audit(action_id,actor,event,event_ref,payload_hash,correlation_id) VALUES(%s,%s,%s,%s,%s,%s)',
                     (action_id, p.principal_id, decision, confirmation_event_id, ch, correlation_id))
        return res
    return db.write(tx)


def _post_reversal(conn, s, sup: Principal, action_id, requester_id: str, payload: dict) -> dict:
    key = f"REV-{uuid.UUID(str(action_id)).hex[:16]}"
    h = payload_hash({'channel': payload['channel'], 'transaction_id': payload['transaction_id']})
    prior = _existing(conn, 'reversal', key, h)
    if prior is not None:
        return prior
    try:
        with conn.transaction():
            rid = conn.execute('INSERT INTO telco.reversal(channel,transaction_id,external_key,occurred_at,reason) VALUES(%s,%s,%s,now(),%s) RETURNING id',
                               (payload['channel'], payload['transaction_id'], key, payload['reason'])).fetchone()[0]
    except pgerr.CheckViolation:
        raise errors.unprocessable('INSUFFICIENT_STOCK', 'Stok outlet tidak cukup untuk mengembalikan sell-in ini.')
    except pgerr.UniqueViolation:
        raise errors.conflict('ALREADY_REVERSED', 'Transaksi sudah direversal.')
    res = {'status': 'posted', 'reversal_id': rid, 'external_key': key, 'channel': payload['channel'],
           'transaction_id': payload['transaction_id']}
    conn.execute('INSERT INTO telco.posting_request(channel,external_key,payload_hash,principal_id,transaction_id,result) VALUES(%s,%s,%s,%s,%s,%s)',
                 ('reversal', key, h, sup.principal_id, rid, Jsonb(res)))
    if payload.get('replacement'):
        from .security import load_principal
        requester = load_principal(conn, requester_id, 'jwt')
        n = normalize_draft(conn, s, requester, DraftRequest.model_validate(payload['replacement']))
        res['replacement'] = post_normalized(conn, s, requester, n, f"RPL-{uuid.UUID(str(action_id)).hex[:16]}")
    return res


def _post_transfer(conn, sup: Principal, action_id, payload: dict) -> dict:
    key = f"TRF-{uuid.UUID(str(action_id)).hex[:16]}"
    h = payload_hash(payload)
    prior = _existing(conn, 'transfer', key, h)
    if prior is not None:
        return prior
    fa = conn.execute('SELECT id FROM telco.stock_account WHERE sales_id=%s', (payload['from_sales_id'],)).fetchone()[0]
    ta = conn.execute('SELECT id FROM telco.stock_account WHERE sales_id=%s', (payload['to_sales_id'],)).fetchone()[0]
    try:
        with conn.transaction():
            tid = conn.execute('''INSERT INTO telco.stock_transfer(external_key,occurred_at,from_account,to_account,product_id,quantity,reason,approved_by)
                VALUES(%s,now(),%s,%s,%s,%s,%s,%s) RETURNING id''',
                               (key, fa, ta, payload['product_id'], Decimal(payload['quantity']), payload['reason'], sup.principal_id)).fetchone()[0]
    except pgerr.CheckViolation:
        raise errors.unprocessable('INSUFFICIENT_STOCK', 'Stok pengirim tidak cukup untuk transfer.')
    res = {'status': 'posted', 'transfer_id': tid, 'external_key': key}
    conn.execute('INSERT INTO telco.posting_request(channel,external_key,payload_hash,principal_id,transaction_id,result) VALUES(%s,%s,%s,%s,%s,%s)',
                 ('transfer', key, h, sup.principal_id, tid, Jsonb(res)))
    return res

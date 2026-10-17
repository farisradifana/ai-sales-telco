"""Fixed-format WhatsApp reports (no model, no chat quota): "sell in", "sell out", "peringkat outlet|kecamatan|kabupaten",
"stok saya", "menu". Scope = the principal's grants (default-deny); manager levels see the accumulation of their sales.

Period: the month of the latest COMPLETE data (ingestion watermark), i.e. the whole month when the watermark is a month end,
else month-to-date up to the watermark. Growth compares that period with the same span of the 1st/2nd/3rd previous month
(calendar months). Sell-in and sell-out are never summed. Every report is stored as an evidence snapshot (audit trail).
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from . import errors, evidence, periods
from .charts import short_rp
from .money import dec, rupiah, stringify_money
from .security import Principal

WIB = timezone(timedelta(hours=7))
LEVEL_LABEL = {'sales': 'Sales', 'territory_manager': 'Territory Manager', 'head_of_branch': 'Head of Branch',
               'head_of_sales': 'Head of Sales', 'head_of_region': 'Head of Region'}
DISTRICT_LEVELS = ('territory_manager', 'head_of_branch', 'head_of_sales', 'head_of_region')
REGENCY_LEVELS = ('head_of_sales', 'head_of_region')
LIST_LIMIT = 10      # top N in rankings
TAIL_LIMIT = 5       # bottom N when the list is longer than LIST_LIMIT + TAIL_LIMIT
CHANNEL_LABEL = {'sell_in': 'Sell-in', 'sell_out': 'Sell-out'}


# ------------------------------------------------------------------ scope + periods
def profile(conn, p: Principal) -> dict:
    r = conn.execute('''SELECT o.id,o.level,o.title FROM telco.app_principal a LEFT JOIN telco.org_position o ON o.id=a.org_position_id
        WHERE a.principal_id=%s''', (p.principal_id,)).fetchone()
    return {'position_id': r[0], 'level': r[1], 'title': r[2] or p.display_name} if r else {'position_id': None, 'level': None,
                                                                                            'title': p.display_name}


def scope_outlets(conn, p: Principal) -> list[int]:
    """Outlets visible to the principal: district grants + outlet grants (same rule as telco.can_access)."""
    return [r[0] for r in conn.execute('''SELECT o.id FROM telco.outlet o WHERE EXISTS(SELECT 1 FROM telco.principal_grant g
        WHERE g.principal_id=%s AND ((g.scope_type='district' AND g.entity_id=o.district_id) OR (g.scope_type='outlet' AND g.entity_id=o.id)))
        ORDER BY o.id''', (p.principal_id,)).fetchall()]


def scope_sales(conn, p: Principal) -> list[int]:
    """Sales whose results roll up to the principal: own account for sales, every sales of a granted kecamatan otherwise."""
    if p.role == 'sales':
        return [p.sales_id] if p.sales_id else []
    return [r[0] for r in conn.execute('''SELECT s.id FROM telco.sales s JOIN telco.principal_grant g ON g.scope_type='district'
        AND g.entity_id=s.district_id AND g.principal_id=%s ORDER BY s.id''', (p.principal_id,)).fetchall()]


def _month_shift(d: date, k: int) -> date:
    m = d.month - 1 - k
    return date(d.year + m // 12, m % 12 + 1, 1)


def windows(conn, months_back: int = 3) -> list[dict]:
    wm = min(r[0] for r in conn.execute("SELECT complete_through FROM telco.ingestion_watermark WHERE source IN ('sell_in','sell_out')"))
    p0, p1 = wm.replace(day=1), wm + timedelta(days=1)
    full = p1 == periods.month_bounds(p0)[1]  # watermark on a month end: compare whole calendar months
    span = p1 - p0
    out = []
    for k in range(months_back + 1):
        a = _month_shift(p0, k)
        b = periods.month_bounds(a)[1] if full else min(a + span, periods.month_bounds(a)[1])
        out.append({'k': k, 'start': a, 'end': b, 'label': periods.label(a, b)})
    return out


def _bounds(ws):
    lo = min(w['start'] for w in ws)
    hi = max(w['end'] for w in ws)
    return (datetime.combine(lo, datetime.min.time(), WIB), datetime.combine(hi, datetime.min.time(), WIB))


def _bucket_sql(ws) -> str:
    return ' '.join(f"WHEN d>='{w['start']}' AND d<'{w['end']}' THEN {w['k']}" for w in ws)


def _growth(cur: Decimal, old: Decimal):
    return ((cur - old) / old).quantize(Decimal('0.0001')) if old > 0 else None


def growth_txt(g, cur=None) -> str:
    if g is None:
        return 'baru' if cur is not None and dec(cur) > 0 else '–'
    v = (dec(g) * 100).quantize(Decimal('0.1'))
    return (f"+{v}%" if v > 0 else f"{v}%").replace('.', ',').replace('-', '−')


def _store(db, p, tool, params, payload, corr):
    payload = {'tool': tool, 'query_version': f'{tool}-v1', **payload}
    return stringify_money(db.write(lambda conn: evidence.store(conn, p, tool, params, payload, corr)))


def _periods_payload(ws):
    return {'current': periods.period_obj(ws[0]['start'], ws[0]['end']), 'current_label': ws[0]['label'],
            'comparisons': [{'months_back': w['k'], 'label': w['label'], **periods.period_obj(w['start'], w['end'])} for w in ws[1:]],
            'data_complete_through': str(ws[0]['end'] - timedelta(days=1))}


def _require_level(prof, levels, what):
    if prof['level'] not in levels:
        allowed = ', '.join(LEVEL_LABEL[x] for x in levels)
        raise errors.AppError(403, 'LEVEL_DENIED', f'"{what}" hanya untuk {allowed}.')


# ------------------------------------------------------------------ data
def _by_product(conn, ws, outlets, sales, channel):
    """Per product revenue/quantity per window. Sell-in is attributed to the sales, sell-out to the outlet."""
    lo, hi = _bounds(ws)
    col, ids = ('sales_id', sales) if channel == 'sell_in' else ('outlet_id', outlets)
    rows = conn.execute(f'''WITH ev AS (SELECT product_id,quantity,net_revenue,(occurred_at AT TIME ZONE 'Asia/Jakarta')::date d
          FROM telco.v_commercial_event WHERE channel=%s AND occurred_at>=%s AND occurred_at<%s AND {col}=ANY(%s))
        SELECT CASE {_bucket_sql(ws)} END k,product_id,sum(quantity),sum(net_revenue) FROM ev GROUP BY 1,2''',
                        (channel, lo, hi, ids)).fetchall()
    out = {}
    for k, pid, q, r in rows:
        if k is not None:
            out.setdefault(pid, {})[k] = (dec(q), dec(r))
    return out


def _products(conn):
    return conn.execute('SELECT id,sku,name,stock_unit FROM telco.product ORDER BY category DESC,id').fetchall()


def _grouped(conn, ws, outlets, key_sql, join_sql):
    """Revenue per (group, channel, window) over the principal's outlets; group = outlet/district/regency."""
    lo, hi = _bounds(ws)
    rows = conn.execute(f'''WITH ev AS (SELECT e.channel,e.outlet_id,e.net_revenue,(e.occurred_at AT TIME ZONE 'Asia/Jakarta')::date d
          FROM telco.v_commercial_event e WHERE e.occurred_at>=%s AND e.occurred_at<%s AND e.outlet_id=ANY(%s))
        SELECT {key_sql} g,ev.channel,CASE {_bucket_sql(ws)} END k,sum(ev.net_revenue)
        FROM ev JOIN telco.outlet o ON o.id=ev.outlet_id {join_sql} GROUP BY 1,2,3''', (lo, hi, outlets)).fetchall()
    out = {}
    for g, ch, k, r in rows:
        if k is not None:
            out.setdefault(g, {}).setdefault(ch, {})[k] = dec(r)
    return out


def _metrics(series: dict, ws) -> dict:
    out = {}
    for ch in ('sell_in', 'sell_out'):
        s = series.get(ch, {})
        cur = s.get(0, Decimal(0))
        hist = [s.get(w['k'], Decimal(0)) for w in ws[1:]]
        # delta_idr is stored so a narrative may quote the change in rupiah (the verifier only accepts evidence numbers).
        out[ch] = {'revenue_idr': cur, 'history_idr': hist, 'delta_idr': [cur - h for h in hist],
                   'growth': [_growth(cur, h) for h in hist]}
    return out


# ------------------------------------------------------------------ reports
def sell_in(db, p: Principal, corr: str, now: datetime | None = None) -> dict:
    today = periods.today_wib(now)

    def q(conn):
        prof = profile(conn, p)
        ws = windows(conn, 1)
        sales, outlets = scope_sales(conn, p), scope_outlets(conn, p)
        if not sales:
            raise errors.denied('Tidak ada sales dalam cakupan akun Anda.')
        cur = ws[0]
        tgt = dec(conn.execute('''SELECT coalesce(sum(revenue_idr),0) FROM telco.sales_target WHERE channel='sell_in'
            AND sales_id=ANY(%s) AND target_date>=%s AND target_date<%s''', (sales, cur['start'], cur['end'])).fetchone()[0])
        prod = _by_product(conn, ws, outlets, sales, 'sell_in')
        products = [{'sku': s, 'product': n, 'stock_unit': u, 'quantity': prod.get(i, {}).get(0, (Decimal(0), Decimal(0)))[0],
                     'revenue_idr': prod.get(i, {}).get(0, (Decimal(0), Decimal(0)))[1],
                     'previous_revenue_idr': prod.get(i, {}).get(1, (0, Decimal(0)))[1],
                     'delta_idr': prod.get(i, {}).get(0, (0, Decimal(0)))[1] - prod.get(i, {}).get(1, (0, Decimal(0)))[1],
                     'growth': _growth(prod.get(i, {}).get(0, (0, Decimal(0)))[1], prod.get(i, {}).get(1, (0, Decimal(0)))[1])}
                    for i, s, n, u in _products(conn)]
        actual = sum((x['revenue_idr'] for x in products), Decimal(0))
        prev = sum((v.get(1, (0, Decimal(0)))[1] for v in prod.values()), Decimal(0))
        # Today, live (not yet behind the watermark): target vs posted sell-in.
        t_today = dec(conn.execute('''SELECT coalesce(sum(revenue_idr),0) FROM telco.sales_target WHERE channel='sell_in'
            AND sales_id=ANY(%s) AND target_date=%s''', (sales, today)).fetchone()[0])
        a_today = dec(conn.execute('''SELECT coalesce(sum(net_revenue),0) FROM telco.v_commercial_event WHERE channel='sell_in'
            AND sales_id=ANY(%s) AND occurred_at>=%s AND occurred_at<%s''',
                                   (sales, datetime.combine(today, datetime.min.time(), WIB),
                                    datetime.combine(today + timedelta(days=1), datetime.min.time(), WIB))).fetchone()[0])
        # Managers: progress of each direct report (accumulated from the sales under it).
        children = []
        if prof['position_id'] and prof['level'] != 'sales':
            for cid, title in conn.execute('SELECT id,title FROM telco.org_position WHERE parent_id=%s ORDER BY code', (prof['position_id'],)):
                ss = [r[0] for r in conn.execute('''SELECT s.id FROM telco.sales s JOIN telco.v_org_coverage c ON c.district_id=s.district_id
                    WHERE c.position_id=%s''', (cid,))]
                ss = [x for x in ss if x in set(sales)]
                ct = dec(conn.execute('''SELECT coalesce(sum(revenue_idr),0) FROM telco.sales_target WHERE channel='sell_in' AND sales_id=ANY(%s)
                    AND target_date>=%s AND target_date<%s''', (ss, cur['start'], cur['end'])).fetchone()[0])
                if ct == 0:
                    continue  # no active sales/targets under this position
                lo, hi = _bounds([cur])
                ca = dec(conn.execute('''SELECT coalesce(sum(net_revenue),0) FROM telco.v_commercial_event WHERE channel='sell_in'
                    AND sales_id=ANY(%s) AND occurred_at>=%s AND occurred_at<%s''', (ss, lo, hi)).fetchone()[0])
                children.append({'title': title, 'target_idr': ct, 'actual_idr': ca, 'gap_idr': max(Decimal(0), ct - ca),
                                 'achievement': (ca / ct).quantize(Decimal('0.0001'))})
            children.sort(key=lambda c: c['achievement'])
        return prof, ws, sales, tgt, products, actual, prev, t_today, a_today, children

    prof, ws, sales, tgt, products, actual, prev, t_today, a_today, children = db.read(q)
    payload = {'scope_title': prof['title'], 'level': prof['level'], 'channel': 'sell_in', **_periods_payload(ws),
               'sales_count': len(sales), 'target_idr': tgt, 'actual_idr': actual,
               'achievement': (actual / tgt).quantize(Decimal('0.0001')) if tgt else None, 'gap_idr': max(Decimal(0), tgt - actual),
               'previous_month_idr': prev, 'delta_vs_previous_idr': actual - prev, 'growth_vs_previous': _growth(actual, prev),
               'products': products,
               'today': {'date': str(today), 'target_idr': t_today, 'actual_idr': a_today}, 'children': children}
    return _store(db, p, 'report_sell_in', {'period': payload['current']}, payload, corr)


def sell_out(db, p: Principal, corr: str) -> dict:
    def q(conn):
        prof = profile(conn, p)
        ws = windows(conn, 1)
        outlets = scope_outlets(conn, p)
        if not outlets:
            raise errors.denied('Tidak ada outlet dalam cakupan akun Anda.')
        prod = _by_product(conn, ws, outlets, [], 'sell_out')
        products = []
        for i, s, n, u in _products(conn):
            c, o = prod.get(i, {}).get(0, (Decimal(0), Decimal(0))), prod.get(i, {}).get(1, (Decimal(0), Decimal(0)))
            products.append({'sku': s, 'product': n, 'stock_unit': u, 'quantity': c[0], 'revenue_idr': c[1],
                             'previous_quantity': o[0], 'previous_revenue_idr': o[1], 'delta_idr': c[1] - o[1],
                             'growth': _growth(c[1], o[1])})
        return prof, ws, outlets, products

    prof, ws, outlets, products = db.read(q)
    total = sum((x['revenue_idr'] for x in products), Decimal(0))
    prev = sum((x['previous_revenue_idr'] for x in products), Decimal(0))
    payload = {'scope_title': prof['title'], 'level': prof['level'], 'channel': 'sell_out', **_periods_payload(ws),
               'outlet_count': len(outlets), 'revenue_idr': total, 'previous_month_idr': prev, 'delta_vs_previous_idr': total - prev,
               'growth_vs_previous': _growth(total, prev),
               'products': products}
    return _store(db, p, 'report_sell_out', {'period': payload['current']}, payload, corr)


def rank_outlets(db, p: Principal, corr: str) -> dict:
    def q(conn):
        prof = profile(conn, p)
        ws = windows(conn, 1)
        outlets = scope_outlets(conn, p)
        if not outlets:
            raise errors.denied('Tidak ada outlet dalam cakupan akun Anda.')
        data = _grouped(conn, ws, outlets, 'o.id', '')
        meta = {r[0]: (r[1], r[2], r[3]) for r in conn.execute('''SELECT o.id,o.code,o.name,d.name FROM telco.outlet o
            JOIN telco.district d ON d.id=o.district_id WHERE o.id=ANY(%s)''', (outlets,))}
        rows = [{'outlet_id': i, 'code': meta[i][0], 'name': meta[i][1], 'district': meta[i][2], **_metrics(data.get(i, {}), ws)}
                for i in outlets]
        return prof, ws, rows

    prof, ws, rows = db.read(q)
    payload = {'scope_title': prof['title'], 'level': prof['level'], **_periods_payload(ws), 'outlets': rows}
    return _store(db, p, 'report_rank_outlets', {'period': payload['current']}, payload, corr)


def _rank_area(db, p: Principal, corr: str, kind: str) -> dict:
    def q(conn):
        prof = profile(conn, p)
        if kind == 'district':
            _require_level(prof, DISTRICT_LEVELS, 'peringkat kecamatan')
        else:
            _require_level(prof, REGENCY_LEVELS, 'peringkat kabupaten')
        ws = windows(conn, 3)
        outlets = scope_outlets(conn, p)
        if kind == 'district':
            data = _grouped(conn, ws, outlets, 'o.district_id', '')
            meta = {r[0]: (r[1], r[2]) for r in conn.execute('''SELECT d.id,d.name,r.name FROM telco.district d JOIN telco.regency r ON r.id=d.regency_id
                WHERE d.id IN (SELECT district_id FROM telco.outlet WHERE id=ANY(%s))''', (outlets,))}
        else:
            data = _grouped(conn, ws, outlets, 'x.regency_id', 'JOIN telco.district x ON x.id=o.district_id')
            meta = {r[0]: (r[1], None) for r in conn.execute('''SELECT r.id,r.name FROM telco.regency r WHERE r.id IN
                (SELECT d.regency_id FROM telco.district d JOIN telco.outlet o ON o.district_id=d.id WHERE o.id=ANY(%s))''', (outlets,))}
        rows = [{'id': i, 'name': n, 'parent': par, **_metrics(data.get(i, {}), ws)} for i, (n, par) in meta.items()]
        rows.sort(key=lambda r: (-r['sell_out']['revenue_idr'], r['name']))
        return prof, ws, rows

    prof, ws, rows = db.read(q)
    payload = {'scope_title': prof['title'], 'level': prof['level'], **_periods_payload(ws), 'kind': kind, 'order_by': 'sell_out',
               'areas': rows, 'note': 'Hanya wilayah yang memiliki outlet; sell-in dan sell-out tidak dijumlah.'}
    return _store(db, p, f'report_rank_{kind}s', {'period': payload['current']}, payload, corr)


def rank_districts(db, p, corr):
    return _rank_area(db, p, corr, 'district')


def rank_regencies(db, p, corr):
    return _rank_area(db, p, corr, 'regency')


# ------------------------------------------------------------------ WhatsApp text
def _product_line(x) -> str:
    qty = f"{int(dec(x['quantity'])):,} pcs · ".replace(',', '.') if x['stock_unit'] == 'PCS' else ''  # saldo: nominal = rupiah
    return f"- {x['product'].replace('Fiktif ', '')}: {qty}{short_rp(x['revenue_idr'])} ({growth_txt(x['growth'], x['revenue_idr'])})"


def _period_line(e) -> str:
    return f"Periode: {e['current_label']} (data lengkap s.d. {_id_date(e['data_complete_through'])})"


def _id_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f'{d.day} {periods.ID_MONTHS[d.month - 1][:3]} {d.year}'


def _ach(x) -> str:
    return '–' if x is None else f"{(dec(x) * 100).quantize(Decimal('0.1'))}%".replace('.', ',')


def sell_in_text(e: dict) -> str:
    prev_lbl = e['comparisons'][0]['label']
    out = [f"*Sell-in {e['scope_title']}*", _period_line(e), '',
           f"🎯 Target: {rupiah(e['target_idr'])}", f"✅ Realisasi: {rupiah(e['actual_idr'])}",
           f"📈 Achievement: *{_ach(e['achievement'])}*" + (f" (kurang {rupiah(e['gap_idr'])})" if dec(e['gap_idr']) > 0 else ' 🎉'),
           f"Vs {prev_lbl}: {growth_txt(e['growth_vs_previous'], e['actual_idr'])}", '', '*Per produk:*']
    for x in e['products']:
        if dec(x['revenue_idr']) == 0 and dec(x['quantity']) == 0:
            continue
        out.append(_product_line(x))
    if e['children']:
        out += ['', f"*Progres tim ({len(e['children'])}), terendah dulu:*"]
        for c in e['children'][:LIST_LIMIT]:
            out.append(f"- {c['title']}: {_ach(c['achievement'])} ({short_rp(c['actual_idr'])} / {short_rp(c['target_idr'])})")
        if len(e['children']) > LIST_LIMIT:
            out.append(f"…dan {len(e['children']) - LIST_LIMIT} lainnya")
    t = e['today']
    if dec(t['target_idr']) > 0 or dec(t['actual_idr']) > 0:
        ach = _ach(dec(t['actual_idr']) / dec(t['target_idr'])) if dec(t['target_idr']) > 0 else '–'
        out += ['', f"Hari ini ({_id_date(t['date'])}, berjalan): {rupiah(t['actual_idr'])} dari target {rupiah(t['target_idr'])} ({ach})"]
    return '\n'.join(out)


def sell_out_text(e: dict) -> str:
    prev_lbl = e['comparisons'][0]['label']
    out = [f"*Sell-out {e['scope_title']}*", _period_line(e) + f" · {e['outlet_count']} outlet", '',
           f"Total: *{rupiah(e['revenue_idr'])}* ({growth_txt(e['growth_vs_previous'], e['revenue_idr'])} vs {prev_lbl})", '',
           '*Per produk:*']
    for x in e['products']:
        if dec(x['revenue_idr']) == 0 and dec(x['previous_revenue_idr']) == 0:
            continue
        out.append(_product_line(x))
    return '\n'.join(out)


def _top(rows, key):
    return sorted(rows, key=lambda r: (-dec(r[key]['revenue_idr']), r['code']))


def rank_outlets_text(e: dict) -> str:
    rows = e['outlets']
    prev_lbl = e['comparisons'][0]['label']
    out = [f"*Peringkat outlet {e['scope_title']}*", _period_line(e), f"(%) = pertumbuhan vs {prev_lbl}"]
    n = 5 if len(rows) > 5 else len(rows)
    for ch in ('sell_out', 'sell_in'):
        out += ['', f"🏆 *{CHANNEL_LABEL[ch]}* (top {n} dari {len(rows)})"]
        ranked = _top(rows, ch)
        line = lambda i, r: (f"{i}. {r['code']} {r['name'].replace('Outlet Fiktif ', 'Outlet ')} – {short_rp(r[ch]['revenue_idr'])} "  # noqa: E731
                             f"({growth_txt(r[ch]['growth'][0], r[ch]['revenue_idr'])})")
        out += [line(i, r) for i, r in enumerate(ranked[:n], 1)]
        if len(ranked) > 10:  # managers also see where to act first
            out.append('Terbawah:')
            out += [line(i, r) for i, r in enumerate(ranked[-3:], len(ranked) - 2)]
    return '\n'.join(out)


def rank_area_text(e: dict) -> str:
    rows = e['areas']
    what = 'kecamatan' if e['kind'] == 'district' else 'kabupaten/kota'
    labels = [c['label'].split(' ')[0][:3] for c in e['comparisons']]
    out = [f"*Peringkat {what} {e['scope_title']}*", _period_line(e),
           f"Urut sell-out. Pertumbuhan vs {', '.join(labels)} (1/2/3 bulan lalu)."]
    shown = rows if len(rows) <= LIST_LIMIT + TAIL_LIMIT else rows[:LIST_LIMIT]
    tail = [] if len(rows) <= LIST_LIMIT + TAIL_LIMIT else rows[-TAIL_LIMIT:]

    def block(i, r):
        name = r['name'] + (f" ({r['parent'].replace('Kabupaten ', '')})" if r.get('parent') else '')
        so, si = r['sell_out'], r['sell_in']
        g = lambda m: ' / '.join(growth_txt(x, m['revenue_idr']) for x in m['growth'])  # noqa: E731
        return [f"{i}. *{name}*", f"   SO {short_rp(so['revenue_idr'])} ({g(so)})", f"   SI {short_rp(si['revenue_idr'])} ({g(si)})"]

    out.append('')
    for i, r in enumerate(shown, 1):
        out += block(i, r)
    if tail:
        out += ['', f"…{len(rows) - LIST_LIMIT - TAIL_LIMIT} lainnya…", '*Terbawah:*']
        for j, r in enumerate(tail, len(rows) - TAIL_LIMIT + 1):
            out += block(j, r)
    return '\n'.join(out)


def products_text(db, p: Principal | None = None) -> str:
    """Product catalogue for a phone screen: one short block per product (SKU to type, name, prices). The how-to-read and
    how-to-input lines are only for the Sales role, which is the only role that posts sell-in."""
    def q(conn):
        return conn.execute('''SELECT p.sku,p.name,p.category,p.stock_unit,
              max(pp.unit_price) FILTER(WHERE pp.channel='sell_in'),max(pp.unit_price) FILTER(WHERE pp.channel='sell_out'),
              max(pp.max_discount_pct) FILTER(WHERE pp.channel='sell_out')
            FROM telco.product p LEFT JOIN telco.price_policy pp ON pp.product_id=p.id
              AND (now() AT TIME ZONE 'Asia/Jakarta')::date>=pp.valid_from AND (now() AT TIME ZONE 'Asia/Jakarta')::date<pp.valid_to
            GROUP BY p.id ORDER BY CASE p.category WHEN 'PERDANA' THEN 1 WHEN 'VOUCHER' THEN 2 ELSE 3 END,p.id''').fetchall()
    sales = p is not None and p.role == 'sales'
    rp = lambda v: rupiah(v) if v is not None else '–'  # noqa: E731
    out = ['📦 *Daftar produk*']
    if sales:
        out += ['', '_SI = harga ke outlet (sell-in)_', '_SO = harga ke pelanggan (sell-out)_']
    cat = None
    for sku, name, category, unit, si, so, disc in db.read(q):
        if category != cat:
            cat = category
            out += ([] if out[-1] == '' else ['']) + [f"*━ {category.title()} ━*"]
        out.append(f"*{sku}*")
        out.append(name.replace('Fiktif ', ''))
        if unit == 'IDR':
            out.append(f"SI {rp(si)} · SO {rp(so)} per Rp1 saldo" if sales else f"Sell-in {rp(si)} · Sell-out {rp(so)} per Rp1 saldo")
        else:
            d = f" · diskon ≤{dec(disc).normalize():f}%" if disc and dec(disc) > 0 else ''
            out.append(f"SI {rp(si)} · SO {rp(so)}{d}" if sales else f"Sell-in {rp(si)} · Sell-out {rp(so)}{d}")
        out.append('')
    if sales:
        out += ['✍️ *Cara input sell-in*', 'Ketik: kode outlet, SKU, jumlah', '`OUT-0001 SIM-NOVA-8 5`',
                'Saldo dalam rupiah:', '`OUT-0001 BAL-NOVA-IDR 100000`']
    return '\n'.join(out).rstrip()


def menu_rows(level: str | None, role: str) -> list[dict]:
    """Quick commands for this role. Row id = the command text the parser understands (a tap works like typing it)."""
    rows = []
    if role == 'sales':
        rows.append({'id': 'stok saya', 'title': 'Stok saya', 'description': 'Stok per produk yang Anda pegang'})
    rows += [{'id': 'sell in', 'title': 'Sell in', 'description': 'Target, realisasi, achievement, per produk'},
             {'id': 'sell out', 'title': 'Sell out', 'description': 'Sell-out per produk dan pertumbuhan'},
             {'id': 'peringkat outlet', 'title': 'Peringkat outlet', 'description': 'Outlet teratas sell-out dan sell-in'}]
    if level in DISTRICT_LEVELS:
        rows.append({'id': 'peringkat kecamatan', 'title': 'Peringkat kecamatan', 'description': 'SO & SI vs 1/2/3 bulan lalu'})
    if level in REGENCY_LEVELS:
        rows.append({'id': 'peringkat kabupaten', 'title': 'Peringkat kabupaten', 'description': 'SO & SI per kab/kota vs 1/2/3 bulan lalu'})
    rows.append({'id': 'list produk', 'title': 'List produk', 'description': 'SKU dan harga'})
    if role == 'sales':
        rows.append({'id': 'cara input', 'title': 'Cara input sell-in', 'description': 'Format kirim penjualan ke outlet'})
    return rows


def menu_picker(db, p: Principal) -> tuple[str, dict]:
    """WhatsApp list message: tap a row instead of typing. Limits: 10 rows, title 24, description 72, button 20, body 1024."""
    prof = db.read(lambda conn: profile(conn, p))
    body = ('📋 *Menu perintah cepat*\nPilih salah satu di bawah (tanpa AI, tidak memakai kuota).\n\n'
            'Pertanyaan lain? Ketik saja dengan bahasa biasa, dijawab asisten AI.')
    rows = [{k: v[:72] if k == 'description' else v[:24] if k == 'title' else v for k, v in r.items()} for r in menu_rows(prof['level'], p.role)]
    picker = {'type': 'list', 'header': {'type': 'text', 'text': 'Asisten Penjualan TELCO'}, 'body': {'text': body[:1024]},
              'footer': {'text': 'Ketik "menu" kapan saja'},
              'action': {'button': 'Pilih perintah', 'sections': [{'title': 'Perintah cepat', 'rows': rows[:10]}]}}
    return body, picker


def greeting(db, p: Principal) -> tuple[str, dict]:
    """Reply to "Halo": a short greeting with one "Menu" button (tapping it sends the id "menu" -> the list picker)."""
    prof = db.read(lambda conn: profile(conn, p))
    body = (f"Halo, {prof['title']}! 👋\nSelamat datang di Asisten Penjualan TELCO.\n\n"
            'Tekan *Menu* untuk melihat perintah cepat, atau langsung ketik pertanyaan Anda.')
    button = {'type': 'button', 'body': {'text': body[:1024]},
              'action': {'buttons': [{'type': 'reply', 'reply': {'id': 'menu', 'title': 'Menu'}}]}}
    return body, button


def input_help_text(p: Principal) -> str:
    if p.role != 'sales':
        return 'Input sell-in dilakukan oleh akun Sales.'
    return '\n'.join(['✍️ *Cara input sell-in*', '', 'Ketik: kode outlet, SKU, jumlah', '`OUT-0001 SIM-NOVA-8 5`', '',
                      'Beberapa produk sekaligus (pisah koma):', '`OUT-0001 SIM-NOVA-8 5, VCR-NOVA-3 2`', '',
                      'Saldo dalam rupiah:', '`OUT-0001 BAL-NOVA-IDR 100000`', '',
                      'Lalu balas *YA <kode>* untuk posting atau *BATAL <kode>*.', 'SKU lengkap: pilih *List produk*.'])


def menu_text(db, p: Principal, head: str | None = None) -> str:
    level = db.read(lambda conn: profile(conn, p))['level']
    lines = ([head, ''] if head else []) + ['*Perintah cepat (tanpa AI, tidak memakai kuota):*']
    if p.role == 'sales':
        lines += ['- *stok saya*', '- *OUT-0001 SIM-NOVA-8 5* → input sell-in (outlet, produk, jumlah), lalu balas YA <kode>']
    lines += ['- *sell in* → target, realisasi, achievement, per produk', '- *sell out* → sell-out per produk',
              '- *peringkat outlet*', '- *list produk* → SKU dan harga']
    if level in DISTRICT_LEVELS:
        lines.append('- *peringkat kecamatan*')
    if level in REGENCY_LEVELS:
        lines.append('- *peringkat kabupaten*')
    lines += ['', 'Pertanyaan bebas lain dijawab asisten AI (memakai kuota harian).']
    return '\n'.join(lines)


__all__ = ['sell_in', 'sell_out', 'rank_outlets', 'rank_districts', 'rank_regencies', 'sell_in_text', 'sell_out_text',
           'rank_outlets_text', 'rank_area_text', 'menu_text', 'products_text', 'growth_txt']

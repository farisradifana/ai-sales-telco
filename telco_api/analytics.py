"""Read-only analytical tools. Order per call: authorize -> query (one REPEATABLE READ snapshot) -> enrich ->
persist evidence. Nothing unauthorized is serialized, logged, cached or sent to a model."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from telco import tools as ref_tools

from . import authz, errors, evidence, periods
from .money import dec, rupiah, stringify_money
from .security import Principal

QUERY_VERSION = 'diagnose-v1'
API_VERSION = 'api-diagnose-v1'


def _watermark(conn, channel: str) -> tuple[date | None, str | None]:
    row = conn.execute('SELECT complete_through,batch_key FROM telco.ingestion_watermark WHERE source=%s', (channel,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _growth(base: Decimal, delta: Decimal):
    # Null (not 0, not infinity) when baseline is zero or negative: percentage would mislead.
    return (delta / base).quantize(Decimal('0.000001')) if base > 0 else None


def _entity(conn, scope: str, entity_id: int) -> dict:
    if scope == 'outlet':
        r = conn.execute('''SELECT o.id,o.code,o.name,o.district_id,d.name,o.opened_on,o.closed_on FROM telco.outlet o
            JOIN telco.district d ON d.id=o.district_id WHERE o.id=%s''', (entity_id,)).fetchone()
        return {'type': 'outlet', 'id': r[0], 'code': r[1], 'name': r[2], 'district_id': r[3], 'district_name': r[4],
                'opened_on': str(r[5]), 'closed_on': str(r[6]) if r[6] else None}
    r = conn.execute('SELECT id,code,name FROM telco.district WHERE id=%s', (entity_id,)).fetchone()
    return {'type': 'district', 'id': r[0], 'code': r[1], 'name': r[2]}


def _population(conn, scope: str, entity_id: int, b0: date, c1: date) -> dict:
    r = conn.execute('''SELECT count(*),count(*) FILTER(WHERE opened_on>%s),count(*) FILTER(WHERE closed_on IS NOT NULL AND closed_on<%s)
        FROM telco.outlet WHERE (%s='outlet' AND id=%s) OR (%s='district' AND district_id=%s)''',
                     (b0, c1, scope, entity_id, scope, entity_id)).fetchone()
    return {'outlets_in_scope': r[0], 'opened_during_comparison': r[1], 'closed_during_comparison': r[2],
            'note': 'Populasi tetap: seluruh outlet di cakupan, termasuk yang tidak bertransaksi.'}


def _quality(e: dict, c1: date) -> dict:
    c, b = e['coverage'], e['baseline_coverage']
    wm = e.get('watermark')
    complete_through_needed = c1 - timedelta(days=1)
    issues = []
    if wm is None or date.fromisoformat(str(wm)) < complete_through_needed:
        issues.append('watermark_behind_period')
    if c['expected_outlet_days'] == 0 or b['expected_outlet_days'] == 0:
        issues.append('no_expected_outlet_days')
    if c['complete_outlet_days'] < c['expected_outlet_days']:
        issues.append('current_input_incomplete')
    if b['complete_outlet_days'] < b['expected_outlet_days']:
        issues.append('baseline_input_incomplete')
    return {'data_ready': not issues, 'status': 'final' if not issues else 'provisional', 'issues': issues,
            'current_complete_ratio': _ratio(c['complete_outlet_days'], c['expected_outlet_days']),
            'baseline_complete_ratio': _ratio(b['complete_outlet_days'], b['expected_outlet_days'])}


def _ratio(n, d):
    return str((Decimal(n) / Decimal(d)).quantize(Decimal('0.0001'))) if d else None


def recommend(e: dict, entity: dict) -> dict:
    """Reference gate/actions (telco.tools.recommend) enriched into fact -> hypothesis -> action -> owner -> review ->
    success/guardrail. No uplift, p-value or probability is invented. Actions are proposals needing human confirmation."""
    safe = dict(e)
    safe['coverage'] = {k: (int(v) if isinstance(v, Decimal) else v) for k, v in e['coverage'].items()}
    safe['baseline_coverage'] = {k: (int(v) if isinstance(v, Decimal) else v) for k, v in e['baseline_coverage'].items()}
    cur = e['current']
    safe['current'] = [cur['start'], cur['end_exclusive']] if isinstance(cur, dict) else cur  # reference shape
    ref = ref_tools.recommend(safe)
    c = safe['coverage']
    neg = sorted([d for d in e['drivers'] if dec(d['delta_idr']) < 0], key=lambda d: dec(d['delta_idr']))
    out = []
    for a in ref['actions']:
        item = dict(a)
        item['requires_human_confirmation'] = True
        if a['confidence'] == 'insufficient_data':
            item['fact'] = 'Kelengkapan input/watermark belum memenuhi syarat untuk menyimpulkan tren.'
            item['hypothesis'] = None
            item['guardrail'] = 'Jangan ubah stok/harga sebelum data lengkap.'
        elif 'visits' in ' '.join(a['evidence']):
            item['fact'] = f"Kunjungan tercatat {c.get('visits_done')} dari {c.get('visits_planned')} rencana pada periode current."
            item['hypothesis'] = 'Kunjungan yang tidak terlaksana berasosiasi dengan penurunan; kausalitas belum terbukti.'
            item['guardrail'] = 'Bandingkan dengan outlet kontrol sepadan; jangan menambah diskon.'
        elif 'product_id' in a and neg:
            d = neg[0]
            item['fact'] = (f"SKU {d['product']} berubah {rupiah(d['delta_idr'])} (volume {rupiah(d['volume_effect_idr'])}, "
                            f"harga/mix {rupiah(d['price_mix_effect_idr'])}, masuk/keluar {rupiah(d['entry_exit_effect_idr'])}).")
            item['hypothesis'] = 'Permintaan, input, harga efektif atau ketersediaan SKU ini berubah; perlu dicek di lapangan.'
            item['guardrail'] = 'Cek kebutuhan dan ketersediaan sebelum reorder; batasi biaya diskon.'
        else:
            item['fact'] = 'Tidak ada penurunan agregat pada cakupan ini.'
            item['hypothesis'] = None
            item['guardrail'] = 'Pantau periode sebanding berikutnya.'
        item['scope'] = {'type': entity['type'], 'id': entity['id']}
        out.append(item)
    return {'data_ready': ref['data_ready'], 'actions': out, 'limitations': ref['limitations'],
            'execution_policy': 'Rencana tindakan bukan eksekusi; perubahan stok/harga/kampanye butuh konfirmasi manusia.'}


def diagnose(db, p: Principal, req, correlation_id: str, tool: str = 'diagnose') -> dict:
    def q(conn):
        # Authorization strictly before any data query.
        authz.require(conn, p, req.scope, req.entity_id)
        wm, batch = _watermark(conn, req.channel)
        b0, b1, c0, c1, policy = periods.resolve(req, wm)
        e = ref_tools.diagnose(conn, req.scope, req.entity_id, req.channel, *(x.isoformat() for x in (b0, b1, c0, c1)))
        ent = _entity(conn, req.scope, req.entity_id)
        pop = _population(conn, req.scope, req.entity_id, b0, c1)
        return e, ent, pop, (b0, b1, c0, c1, policy), batch

    e, ent, pop, (b0, b1, c0, c1, policy), batch = db.read(q)
    base, cur, delta = dec(e['revenue_baseline_idr']), dec(e['revenue_current_idr']), dec(e['delta_idr'])
    quality = _quality(e, c1)
    for d in e['drivers']:
        d['growth'] = _growth(dec(d['r0']), dec(d['delta_idr']))
    payload = {
        'tool': tool, 'query_version': QUERY_VERSION, 'api_version': API_VERSION, 'scope': req.scope,
        'entity_id': req.entity_id, 'entity': ent, 'channel': req.channel, 'metric': 'net_revenue_idr',
        'baseline': periods.period_obj(b0, b1), 'current': periods.period_obj(c0, c1), 'period_policy': policy,
        'baseline_label': periods.label(b0, b1), 'current_label': periods.label(c0, c1),
        'revenue_baseline_idr': base, 'revenue_current_idr': cur, 'delta_idr': delta, 'growth': _growth(base, delta),
        'drivers': e['drivers'], 'outlet_contributions': e['outlet_contributions'] or [],
        'coverage': e['coverage'], 'baseline_coverage': e['baseline_coverage'], 'population': pop,
        'watermark': str(e['watermark']) if e['watermark'] else None, 'source_batch_key': e['source_batch_key'] or batch,
        'reconciliation_residual_idr': dec(e['reconciliation_residual_idr']),
        'certainty': e['certainty'], **quality,
        'decomposition': 'Volume/harga-mix per SKU adalah dekomposisi akuntansi (Shapley 2 faktor), bukan sebab kausal.',
        'stockout': 'Tidak diukur (observed_stockout_minutes NULL = unknown); saldo nol tidak membuktikan lost sales.',
    }
    payload = _rehydrate(payload)
    payload['recommendation'] = recommend(payload, ent)
    params = {'scope': req.scope, 'entity_id': req.entity_id, 'channel': req.channel,
              'baseline': [str(b0), str(b1)], 'current': [str(c0), str(c1)]}
    stored = db.write(lambda conn: evidence.store(conn, p, tool, params, payload, correlation_id))
    return stringify_money(stored)


def rank_districts(db, p: Principal, req, correlation_id: str) -> dict:
    def q(conn):
        wm, batch = _watermark(conn, req.channel)
        b0, b1, c0, c1, policy = periods.resolve(req, wm)
        # Filtering by grant happens inside the SQL statement: unauthorized rows never leave the database.
        rows = ref_tools.rank_authorized_districts(conn, p.principal_id, req.channel, *(x.isoformat() for x in (b0, b1, c0, c1)))
        cov = {}
        if rows:
            ids = [r['district_id'] for r in rows]
            col = 'sell_out_complete' if req.channel == 'sell_out' else 'sell_in_complete'
            for did, exp, comp in conn.execute(f'''SELECT o.district_id,count(*),count(*) FILTER(WHERE d.{col})
                FROM telco.outlet o CROSS JOIN generate_series(%s::date,%s::date-1,interval '1 day') g
                LEFT JOIN telco.outlet_day d ON d.outlet_id=o.id AND d.day=g::date
                WHERE o.district_id=ANY(%s) AND g::date>=o.opened_on AND (o.closed_on IS NULL OR g::date<o.closed_on)
                AND d.expected_open IS DISTINCT FROM false GROUP BY 1''', (c0, c1, ids)).fetchall():
                cov[did] = {'expected_outlet_days': exp, 'complete_outlet_days': comp, 'complete_ratio': _ratio(comp, exp)}
        return rows, cov, wm, batch, (b0, b1, c0, c1, policy)

    rows, cov, wm, batch, (b0, b1, c0, c1, policy) = db.read(q)
    if not rows:
        raise errors.denied('Tidak ada kecamatan yang diizinkan untuk peringkat ini.')
    for r in rows:
        r['growth'] = _growth(dec(r['baseline_idr']), dec(r['delta_idr']))
        r['coverage'] = cov.get(r['district_id'])
    payload = {'tool': 'rank_districts', 'query_version': 'rank-districts-v1', 'channel': req.channel,
               'baseline': periods.period_obj(b0, b1), 'current': periods.period_obj(c0, c1), 'period_policy': policy,
               'baseline_label': periods.label(b0, b1), 'current_label': periods.label(c0, c1),
               'districts': rows, 'watermark': str(wm) if wm else None, 'source_batch_key': batch,
               'authorized_only': True,
               'next_step': 'Peringkat bukan dasar tindakan: diagnosis coverage dan kontribusi outlet kecamatan terpilih dulu.'}
    params = {'channel': req.channel, 'baseline': [str(b0), str(b1)], 'current': [str(c0), str(c1)]}
    payload = _rehydrate(payload)
    return stringify_money(db.write(lambda conn: evidence.store(conn, p, 'rank_districts', params, payload, correlation_id)))


def stock(db, p: Principal, req, correlation_id: str) -> dict:
    def q(conn):
        if req.account == 'self':
            if p.sales_id is None:
                raise errors.bad_request('ACCOUNT_REQUIRED', 'Pilih akun stok outlet/sales; principal ini bukan sales.')
            acct = conn.execute('SELECT id FROM telco.stock_account WHERE sales_id=%s', (p.sales_id,)).fetchone()
            holder = {'type': 'sales', 'id': p.sales_id}
        elif req.account == 'outlet':
            if req.outlet_id is None:
                raise errors.bad_request('INVALID_INPUT', 'outlet_id wajib untuk akun outlet.')
            authz.require(conn, p, 'outlet', req.outlet_id)
            acct = conn.execute('SELECT id FROM telco.stock_account WHERE outlet_id=%s', (req.outlet_id,)).fetchone()
            holder = {'type': 'outlet', 'id': req.outlet_id}
        else:
            if req.sales_id is None or not authz.sales_visible(conn, p, req.sales_id):
                raise errors.denied()
            acct = conn.execute('SELECT id FROM telco.stock_account WHERE sales_id=%s', (req.sales_id,)).fetchone()
            holder = {'type': 'sales', 'id': req.sales_id}
        if not acct:
            raise errors.not_found('Akun stok tidak ditemukan.')
        at = req.as_of
        rows = conn.execute('''SELECT p.id,p.sku,p.name,p.stock_unit,
              CASE WHEN %s::timestamptz IS NULL THEN coalesce(b.quantity,0) ELSE telco.stock_as_of(%s,p.id,%s) END
            FROM telco.product p LEFT JOIN telco.stock_balance b ON b.product_id=p.id AND b.account_id=%s
            WHERE %s::int IS NULL OR p.id=%s ORDER BY p.id''',
                            (at, acct[0], at, acct[0], req.product_id, req.product_id)).fetchall()
        last = conn.execute('SELECT max(occurred_at) FROM telco.stock_ledger WHERE account_id=%s', (acct[0],)).fetchone()[0]
        return acct[0], holder, rows, last

    acct, holder, rows, last = db.read(q)
    items = [{'product_id': r[0], 'sku': r[1], 'product': r[2], 'stock_unit': r[3], 'quantity': r[4]} for r in rows]
    by_unit = {}
    for i in items:
        by_unit.setdefault(i['stock_unit'], Decimal(0))
        by_unit[i['stock_unit']] += dec(i['quantity'])
    payload = {'tool': 'stock', 'query_version': 'stock-v1', 'account_id': acct, 'holder': holder,
               'as_of': req.as_of.isoformat() if req.as_of else 'current_balance', 'items': items,
               'totals_by_unit': by_unit, 'last_movement_at': last.isoformat() if last else None,
               'note': 'Satuan PCS dan IDR tidak dijumlahkan; saldo nol bukan bukti lost sales.'}
    params = {'account': req.account, 'holder': holder, 'product_id': req.product_id,
              'as_of': req.as_of.isoformat() if req.as_of else None}
    return stringify_money(db.write(lambda conn: evidence.store(conn, p, 'stock', params, payload, correlation_id)))


def target(db, p: Principal, req, correlation_id: str, now: datetime | None = None) -> dict:
    day = req.target_date or periods.today_wib(now)

    def q(conn):
        if p.role == 'sales':
            if req.sales_id not in (None, p.sales_id):
                raise errors.denied()
            sid = p.sales_id
        else:
            if req.sales_id is None or not authz.sales_visible(conn, p, req.sales_id):
                raise errors.denied()
            sid = req.sales_id
        t = conn.execute('SELECT revenue_idr,rationale FROM telco.sales_target WHERE sales_id=%s AND target_date=%s AND channel=%s',
                         (sid, day, req.channel)).fetchone()
        cats = conn.execute('''SELECT p.category,p.stock_unit,coalesce(sum(s.revenue_idr),0),coalesce(sum(s.quantity),0)
            FROM telco.product p LEFT JOIN telco.v_daily_sales s ON s.product_id=p.id AND s.sales_id=%s AND s.day=%s AND s.channel=%s
            GROUP BY 1,2 ORDER BY 1''', (sid, day, req.channel)).fetchall()
        wm, batch = _watermark(conn, req.channel)
        return sid, t, cats, wm, batch

    sid, t, cats, wm, batch = db.read(q)
    actual = sum((dec(c[2]) for c in cats), Decimal(0))
    tgt = dec(t[0]) if t else None
    payload = {'tool': 'target', 'query_version': 'target-v1', 'sales_id': sid, 'target_date': day.isoformat(),
               'channel': req.channel, 'target_idr': tgt, 'actual_idr': actual,
               'gap_idr': max(Decimal(0), tgt - actual) if tgt is not None else None,
               'achievement_ratio': (actual / tgt).quantize(Decimal('0.0001')) if tgt else None,
               'breakdown': [{'category': c[0], 'stock_unit': c[1], 'revenue_idr': c[2], 'quantity': c[3]} for c in cats],
               'combination_rule': 'Fisik (PCS) dan saldo (IDR) dijumlah dalam IDR pada channel yang sama; sell-in dan sell-out tidak dijumlah.',
               'target_rationale': t[1] if t else None, 'target_available': t is not None,
               'intraday': day >= periods.today_wib(now), 'watermark': str(wm) if wm else None, 'source_batch_key': batch}
    params = {'target_date': day.isoformat(), 'channel': req.channel, 'sales_id': sid}
    return stringify_money(db.write(lambda conn: evidence.store(conn, p, 'target', params, payload, correlation_id)))


def resolve_entity(db, p: Principal, req) -> dict:
    """Search only among AUTHORIZED entities; unauthorized matches are indistinguishable from no match."""
    text = req.query.strip()

    def q(conn):
        out = []
        if req.scope in (None, 'outlet'):
            for o in authz.outlets(conn, p):
                if _match(text, o['code'], o['name'], o['id'], 'outlet'):
                    out.append({'scope': 'outlet', 'entity_id': o['id'], 'code': o['code'], 'name': o['name'],
                                'district_id': o['district_id']})
        if req.scope in (None, 'district'):
            for d in authz.districts(conn, p):
                if _match(text, d['code'], d['name'], d['id'], 'district'):
                    out.append({'scope': 'district', 'entity_id': d['id'], 'code': d['code'], 'name': d['name']})
        return out

    cands = db.read(q)
    exact = [c for c in cands if c['code'].lower() == text.lower()]
    if len(exact) == 1:
        cands = exact
    if not cands:
        return {'status': 'not_found', 'candidates': [], 'message': 'Entitas tidak ditemukan dalam cakupan Anda.'}
    if len(cands) > 1:
        return {'status': 'ambiguous', 'candidates': cands[:8],
                'message': 'Nama ambigu; pilih salah satu kode.'}
    return {'status': 'resolved', 'candidates': cands, 'resolved': cands[0]}


def _match(text: str, code: str, name: str, ident: int, kind: str) -> bool:
    t = text.lower().strip()
    if t == code.lower() or t == name.lower():
        return True
    import re
    m = re.fullmatch(r'(outlet|kecamatan|district)\s*#?\s*(\d+)', t)
    if m:
        k = 'outlet' if m.group(1) == 'outlet' else 'district'
        return k == kind and int(m.group(2)) == ident
    return len(t) >= 4 and (t in name.lower())


def trend(conn, scope: str, entity_id: int, channel: str, b0: date, b1: date, c0: date, c1: date) -> list[dict]:
    col = 'sell_out_complete' if channel == 'sell_out' else 'sell_in_complete'
    rows = conn.execute(f'''
      WITH pop AS (SELECT id,opened_on,closed_on FROM telco.outlet WHERE (%s='outlet' AND id=%s) OR (%s='district' AND district_id=%s)),
      days AS (SELECT g::date AS day, CASE WHEN g::date<%s THEN 'baseline' ELSE 'current' END AS period
               FROM generate_series(%s::date,%s::date-1,interval '1 day') g WHERE g::date<%s OR g::date>=%s),
      cov AS (SELECT days.day,count(*) expected,count(*) FILTER(WHERE od.{col}) complete
              FROM days CROSS JOIN pop LEFT JOIN telco.outlet_day od ON od.outlet_id=pop.id AND od.day=days.day
              WHERE days.day>=pop.opened_on AND (pop.closed_on IS NULL OR days.day<pop.closed_on) AND od.expected_open IS DISTINCT FROM false
              GROUP BY days.day),
      rev AS (SELECT s.day,sum(s.revenue_idr) r FROM telco.v_daily_sales s JOIN pop ON pop.id=s.outlet_id
              WHERE s.channel=%s AND ((s.day>=%s AND s.day<%s) OR (s.day>=%s AND s.day<%s)) GROUP BY s.day)
      SELECT days.day,days.period,coalesce(cov.expected,0),coalesce(cov.complete,0),rev.r FROM days
      LEFT JOIN cov ON cov.day=days.day LEFT JOIN rev ON rev.day=days.day ORDER BY days.day''',
                        (scope, entity_id, scope, entity_id, c0, b0, c1, b1, c0, channel, b0, b1, c0, c1)).fetchall()
    out = []
    for day, period, exp, comp, r in rows:
        complete = exp > 0 and comp == exp
        out.append({'day': day.isoformat(), 'period': period, 'expected_outlet_days': exp, 'complete_outlet_days': comp,
                    'revenue_idr': (r if r is not None else Decimal(0)) if complete else None,
                    'observed_partial_idr': r if (not complete and r is not None) else None,
                    'status': 'complete' if complete else ('no_expected_outlets' if exp == 0 else 'missing_input')})
    return out


def dashboard_snapshot(db, p: Principal, req, correlation_id: str) -> dict:
    """One evidence snapshot for every chart on the dashboard (KPI, trend, contributors, SKU waterfall, ranking)."""
    base = diagnose(db, p, req, correlation_id, tool='dashboard-diagnose')
    b0, b1 = date.fromisoformat(base['baseline']['start']), date.fromisoformat(base['baseline']['end_exclusive'])
    c0, c1 = date.fromisoformat(base['current']['start']), date.fromisoformat(base['current']['end_exclusive'])

    def q(conn):
        authz.require(conn, p, req.scope, req.entity_id)
        series = trend(conn, req.scope, req.entity_id, req.channel, b0, b1, c0, c1)
        ranking = ref_tools.rank_authorized_districts(conn, p.principal_id, req.channel, *(x.isoformat() for x in (b0, b1, c0, c1)))
        return series, ranking

    series, ranking = db.read(q)
    for r in ranking:
        r['growth'] = _growth(dec(r['baseline_idr']), dec(r['delta_idr']))
    payload = {k: v for k, v in base.items() if k not in evidence.ID_FIELDS}
    payload.update({'tool': 'dashboard', 'query_version': 'dashboard-v1', 'diagnose_evidence_id': base['evidence_id'],
                    'trend': series, 'district_ranking': ranking,
                    'generated_at': datetime.now(timezone.utc).isoformat()})
    # Re-hydrate money strings into Decimal before storing to keep exact JSON numbers.
    params = {'scope': req.scope, 'entity_id': req.entity_id, 'channel': req.channel,
              'baseline': [str(b0), str(b1)], 'current': [str(c0), str(c1)]}
    return stringify_money(db.write(lambda conn: evidence.store(conn, p, 'dashboard', params, _rehydrate(payload), correlation_id)))


MONEY_KEYS = {'revenue_baseline_idr', 'revenue_current_idr', 'delta_idr', 'r0', 'r1', 'volume_effect_idr', 'price_mix_effect_idr',
              'entry_exit_effect_idr', 'baseline_idr', 'current_idr', 'revenue_idr', 'observed_partial_idr', 'growth',
              'reconciliation_residual_idr', 'q0', 'q1', 'quantity', 'target_idr', 'actual_idr', 'gap_idr'}


def _rehydrate(obj):
    if isinstance(obj, dict):
        return {k: (Decimal(str(v)) if k in MONEY_KEYS and isinstance(v, (str, int, Decimal)) and not isinstance(v, bool)
                    else _rehydrate(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rehydrate(v) for v in obj]
    return obj

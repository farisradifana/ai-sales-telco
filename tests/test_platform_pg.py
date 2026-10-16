"""Real PostgreSQL 17 tests on a disposable clone of the fully loaded 500k database (see conftest.py).
Covers API authorization, domain integrity under two connections, follow-up context and mocked WhatsApp E2E."""
import hashlib
import hmac
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

pytestmark = pytest.mark.pg
WIB = timezone(timedelta(hours=7))


def err(r):
    return r.json()['error']['code']


def now_iso(delta_s=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()


def spv_for_outlet(admin_conn, outlet_id):
    rid = admin_conn.execute('SELECT d.regency_id FROM telco.outlet o JOIN telco.district d ON d.id=o.district_id WHERE o.id=%s',
                             (outlet_id,)).fetchone()[0]
    return f'spv-r{rid:02d}'


def svc(settings):
    return {'Authorization': f'Bearer {settings.n8n_service_token.get_secret_value()}'}


# ============================================================ authorization / API contract
def test_principal_cannot_see_other_scope(login, admin_conn):
    spv = login('spv-r01')
    assert spv.post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 111}).status_code == 403
    s2 = login('sls-002')
    r = s2.post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 1})
    assert r.status_code == 403 and 'Outlet Fiktif 0001' not in r.text
    assert s2.post('/v1/tools/stock', json={'account': 'outlet', 'outlet_id': 1}).status_code == 403
    assert s2.post('/v1/tools/target', json={'channel': 'sell_in', 'sales_id': 1}).status_code == 403
    # Outlet-only grant does NOT imply the district aggregate or district ranking.
    v = login('viewer-o1')
    did = admin_conn.execute('SELECT district_id FROM telco.outlet WHERE id=1').fetchone()[0]
    assert v.post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 1}).status_code == 200
    assert v.post('/v1/tools/diagnose', json={'scope': 'district', 'entity_id': did}).status_code == 403
    assert v.post('/v1/tools/rank-districts', json={'channel': 'sell_out'}).status_code == 403
    # Supervisor with a district grant on the sales' base district can see that sales' target.
    assert spv.post('/v1/tools/target', json={'channel': 'sell_in', 'sales_id': 1, 'target_date': '2026-09-30'}).status_code == 200


def test_ranking_contains_only_authorized_districts(login, admin_conn):
    r = login('spv-r01').post('/v1/tools/rank-districts', json={'channel': 'sell_out'})
    assert r.status_code == 200
    ids = {d['district_id'] for d in r.json()['districts']}
    allowed = {x[0] for x in admin_conn.execute("SELECT entity_id FROM telco.principal_grant WHERE principal_id='spv-r01' AND scope_type='district'")}
    assert ids <= allowed and len(allowed) < 666


def test_context_and_evidence_are_private(login):
    a, b = login('sls-001'), login('sls-002')
    e = a.post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 1}).json()
    ctx = a.post('/v1/contexts', json={'evidence_id': e['evidence_id'], 'conversation_key': 'dash:test-private'}).json()
    assert b.get(f"/v1/contexts/{ctx['context_id']}").status_code == 404
    assert b.post(f"/v1/contexts/{ctx['context_id']}/follow-up", json={'question': 'solusinya?', 'expected_version': 1}).status_code == 404
    assert b.get(f"/v1/evidence/{e['evidence_id']}").status_code == 404


def test_revoked_grant_blocks_follow_up_evidence_and_signed_link(login, admin_conn, settings):
    pid = 'tmp-' + uuid.uuid4().hex[:8]
    admin_conn.execute("INSERT INTO telco.app_principal(principal_id,role,display_name) VALUES(%s,'supervisor','Temp Fiktif')", (pid,))
    admin_conn.execute("INSERT INTO telco.principal_grant(principal_id,scope_type,entity_id) VALUES(%s,'outlet',2)", (pid,))
    admin_conn.commit()
    c = login(pid)
    e = c.post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 2}).json()
    ctx = c.post('/v1/contexts', json={'evidence_id': e['evidence_id'], 'conversation_key': 'dash:revoke-test'}).json()
    from telco_api.charts import signed_url
    link = signed_url(settings, e['evidence_id'], pid).split('127.0.0.1:8000', 1)[-1]
    assert c.get(link).status_code == 200
    admin_conn.execute("DELETE FROM telco.principal_grant WHERE principal_id=%s", (pid,))
    admin_conn.commit()
    assert c.post(f"/v1/contexts/{ctx['context_id']}/follow-up", json={'question': 'solusinya?', 'expected_version': 1}).status_code == 403
    assert c.get(f"/v1/evidence/{e['evidence_id']}").status_code == 403
    assert c.get(link).status_code == 403


def test_no_raw_sql_surface_and_injection_is_inert(login, app, settings, admin_conn):
    from fastapi.testclient import TestClient
    raw = TestClient(app, raise_server_exceptions=False)
    h = {'Authorization': f'Bearer {settings.mcp_service_token.get_secret_value()}', 'X-Agent-Ticket': 'tkt_invalid'}
    assert err(raw.post('/internal/v1/agent-tools/execute_sql', json={'sql': 'SELECT 1'}, headers=h)) == 'UNKNOWN_TOOL'
    specs = raw.get('/internal/v1/agent-tools', headers=h).json()['tools']
    assert not any('sql' in t['name'] or 'sql' in json.dumps(t['input_schema']).lower().replace('sell', '') for t in specs)
    before = admin_conn.execute('SELECT count(*) FROM telco.sell_out').fetchone()[0]
    c = login('sls-001')
    r = c.post('/v1/tools/resolve-entity', json={'query': "OUT-0001'; DROP TABLE telco.sell_out; --"})
    assert r.status_code == 200 and r.json()['status'] == 'not_found'
    assert c.post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': '1; DROP TABLE x'}).status_code == 400
    assert c.post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 1, 'principal_id': 'am-all'}).status_code == 400
    assert admin_conn.execute('SELECT count(*) FROM telco.sell_out').fetchone()[0] == before


def test_ambiguous_name_needs_clarification(login):
    r = login('sls-001').post('/v1/tools/resolve-entity', json={'query': 'Outlet Fiktif 000', 'scope': 'outlet'}).json()
    assert r['status'] == 'ambiguous' and len(r['candidates']) > 1


def test_zero_base_growth_null_and_missing_coverage_not_ready(login):
    c = login('sls-001')
    r = c.post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 1, 'baseline_start': '2024-12-02', 'baseline_end': '2025-01-01',
                                           'current_start': '2025-01-01', 'current_end': '2025-01-31'}).json()
    assert r['revenue_baseline_idr'] == '0' or r['revenue_baseline_idr'].startswith('0')
    assert r['growth'] is None and r['data_ready'] is False


def test_differing_periods_rejected(login):
    r = login('sls-001').post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 1, 'baseline_start': '2026-08-01',
                                                          'baseline_end': '2026-09-01', 'current_start': '2026-09-01', 'current_end': '2026-10-01'})
    assert r.status_code == 400 and err(r) == 'UNEQUAL_PERIODS'


def test_missing_coverage_blocks_strong_diagnosis(login):
    r = login('sls-001').post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 1, 'baseline_start': '2026-08-17',
                                                          'baseline_end': '2026-09-16', 'current_start': '2026-09-16', 'current_end': '2026-10-16'}).json()
    assert r['data_ready'] is False and r['status'] == 'provisional' and 'watermark_behind_period' in r['issues']
    acts = r['recommendation']['actions']
    assert len(acts) == 1 and acts[0]['confidence'] == 'insufficient_data'


def test_default_period_and_reference_numbers(login):
    r = login('sls-001').post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 1}).json()
    assert (r['baseline']['start'], r['current']['start'], r['current']['end_exclusive']) == ('2026-06-01', '2026-09-01', '2026-10-01')
    assert r['revenue_baseline_idr'] == '7332900.00' and r['revenue_current_idr'] == '2159800.00' and r['delta_idr'] == '-5173100.00'
    assert abs(float(r['reconciliation_residual_idr'])) <= 0.01
    assert all(isinstance(d['r0'], str) for d in r['drivers'])


# ============================================================ domain integrity (two connections)
def _sale(outlet, product, qty, price, key=None, at=None):
    return {'external_key': key or f'TEST-{uuid.uuid4().hex[:16]}', 'outlet_id': outlet, 'product_id': product, 'quantity': qty,
            'unit_price': price, 'occurred_at': at or now_iso(), 'client_confirmation_id': 'test-confirm-' + uuid.uuid4().hex[:8]}


def test_concurrent_oversell_never_negative(login, admin_conn):
    c1, c2 = login('sls-003'), login('sls-003')
    acct = admin_conn.execute('SELECT id FROM telco.stock_account WHERE sales_id=3').fetchone()[0]
    avail = admin_conn.execute('SELECT quantity FROM telco.stock_balance WHERE account_id=%s AND product_id=2', (acct,)).fetchone()[0]
    qty = int(avail) // 2 + 1  # two of these cannot both fit
    barrier, results = threading.Barrier(2), []

    def go(client):
        barrier.wait(10)
        results.append(client.post('/v1/transactions/sell-in', json=_sale(21, 2, qty, '45000')))
    ts = [threading.Thread(target=go, args=(c,)) for c in (c1, c2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 422], [r.text for r in results]
    assert [err(r) for r in results if r.status_code == 422] == ['INSUFFICIENT_STOCK']
    admin_conn.rollback()
    left = admin_conn.execute('SELECT quantity FROM telco.stock_balance WHERE account_id=%s AND product_id=2', (acct,)).fetchone()[0]
    assert left == avail - qty and left >= 0


def test_idempotency_replay_and_conflict(login, admin_conn):
    c = login('sls-003')
    body = _sale(22, 3, 2, '10800', key='TEST-IDEM-' + uuid.uuid4().hex[:10])
    a = c.post('/v1/transactions/sell-in', json=body)
    b = c.post('/v1/transactions/sell-in', json=body)
    assert a.status_code == 200 and b.status_code == 200 and b.json()['lines'][0]['idempotent_replay'] is True
    assert a.json()['lines'][0]['transaction_id'] == b.json()['lines'][0]['transaction_id']
    r = c.post('/v1/transactions/sell-in', json={**body, 'quantity': 3})
    assert r.status_code == 409 and err(r) == 'IDEMPOTENCY_CONFLICT'
    with pytest.raises(psycopg.errors.UniqueViolation):
        admin_conn.execute('''INSERT INTO telco.sell_in(id,external_key,occurred_at,outlet_id,sales_id,assignment_id,product_id,quantity,unit_price)
            VALUES(nextval('telco.sell_in_api_id'),%s,now(),22,3,22,3,1,10800)''', (body['external_key'] + '-1',))
    admin_conn.rollback()


def test_price_policy_enforced(login):
    c = login('sls-003')
    assert err(c.post('/v1/transactions/sell-in', json=_sale(23, 1, 1, '25000'))) == 'PRICE_POLICY_VIOLATION'
    d = c.post('/v1/transactions/drafts', json={'channel': 'sell_out', 'outlet_id': 23,
                                                'lines': [{'sku': 'SIM-NOVA-8', 'quantity': 4, 'discount_idr': '20000'}]})
    assert d.status_code == 422 and err(d) == 'PRICE_POLICY_VIOLATION'


def test_reversal_restores_stock_and_revenue(login, admin_conn):
    s4 = login('sls-004')
    r = s4.post('/v1/transactions/sell-in', json=_sale(31, 1, 10, '22500')).json()
    tx = r['lines'][0]['transaction_id']
    sales_acct = admin_conn.execute('SELECT id FROM telco.stock_account WHERE sales_id=4').fetchone()[0]
    before = admin_conn.execute('SELECT quantity FROM telco.stock_balance WHERE account_id=%s AND product_id=1', (sales_acct,)).fetchone()[0]
    req = s4.post('/v1/reversals', json={'channel': 'sell_in', 'transaction_id': tx, 'reason': 'Salah input outlet (tes)'}).json()
    assert s4.post(f"/v1/actions/{req['action_id']}/decision", json={'decision': 'approve'}).status_code == 403  # sales cannot approve
    spv = login(spv_for_outlet(admin_conn, 31))
    assert any(a['action_id'] == req['action_id'] for a in spv.get('/v1/actions').json()['items'])
    ok = spv.post(f"/v1/actions/{req['action_id']}/decision", json={'decision': 'approve'})
    assert ok.status_code == 200, ok.text
    admin_conn.rollback()
    after = admin_conn.execute('SELECT quantity FROM telco.stock_balance WHERE account_id=%s AND product_id=1', (sales_acct,)).fetchone()[0]
    assert after == before + 10
    net = admin_conn.execute("SELECT sum(net_revenue) FROM telco.v_commercial_event WHERE channel='sell_in' AND transaction_id=%s", (tx,)).fetchone()[0]
    assert net == 0
    again = spv.post(f"/v1/actions/{req['action_id']}/decision", json={'decision': 'approve'})
    assert again.status_code == 200 and again.json().get('idempotent_replay')


def test_backdate_rules_and_audited_correction(login, admin_conn):
    s5 = login('sls-005')
    r = s5.post('/v1/transactions/sell-in', json=_sale(41, 3, 5, '10800', at=now_iso(-120)))
    assert r.status_code == 200, r.text
    assert s5.post('/v1/transactions/sell-in', json=_sale(41, 3, 1, '10800', at=now_iso(-60))).status_code == 200
    assert err(s5.post('/v1/transactions/sell-in', json=_sale(41, 3, 1, '10800', at=now_iso(-3 * 86400)))) == 'BACKDATE_REJECTED'
    assert err(s5.post('/v1/transactions/sell-in', json=_sale(41, 3, 1, '10800', at='2026-09-15T10:00:00+07:00'))) == 'BACKDATE_REJECTED'
    # Earlier than the latest ledger movement of this holder/SKU (inside grace): still rejected on the normal path.
    assert err(s5.post('/v1/transactions/sell-out', json={**_sale(41, 3, 1, '12000', at=now_iso(-600)), 'customer_token': 'SYN-TEST-1'})) == 'BACKDATE_REJECTED'
    spv = login(spv_for_outlet(admin_conn, 41))
    bad = spv.post('/v1/corrections/backdated-sale', json={'channel': 'sell_out', 'external_key': 'TEST-CORR-' + uuid.uuid4().hex[:8], 'outlet_id': 41,
                                                           'product_id': 3, 'quantity': 3, 'unit_price': '12000', 'occurred_at': now_iso(-150),
                                                           'customer_token': 'SYN-TEST-CORR', 'reason': 'Koreksi tes: transaksi sebelum stok tiba'})
    assert bad.status_code == 422 and err(bad) == 'HISTORICAL_STOCK_NEGATIVE'
    admin_conn.rollback()
    assert admin_conn.execute("SELECT count(*) FROM telco.sell_out WHERE customer_token='SYN-TEST-CORR'").fetchone()[0] == 0
    good = spv.post('/v1/corrections/backdated-sale', json={'channel': 'sell_out', 'external_key': 'TEST-CORR-' + uuid.uuid4().hex[:8], 'outlet_id': 41,
                                                            'product_id': 3, 'quantity': 2, 'unit_price': '12000', 'occurred_at': now_iso(-90),
                                                            'customer_token': 'SYN-TEST-CORR2', 'reason': 'Koreksi tes: input terlambat sah'})
    assert good.status_code == 200, good.text
    assert admin_conn.execute('SELECT count(*) FROM telco.correction_audit').fetchone()[0] >= 1


def test_assignment_validity(login, admin_conn):
    admin_conn.execute("INSERT INTO telco.principal_grant(principal_id,scope_type,entity_id) VALUES('sls-001','outlet',11) ON CONFLICT DO NOTHING")
    admin_conn.commit()
    try:
        r = login('sls-001').post('/v1/transactions/drafts', json={'channel': 'sell_in', 'outlet_id': 11, 'lines': [{'sku': 'SIM-NOVA-8', 'quantity': 1}]})
        assert r.status_code == 422 and err(r) == 'INVALID_ASSIGNMENT'
    finally:
        admin_conn.execute("DELETE FROM telco.principal_grant WHERE principal_id='sls-001' AND scope_type='outlet' AND entity_id=11")
        admin_conn.commit()
    assert login('sls-001').post('/v1/transactions/drafts', json={'channel': 'sell_in', 'outlet_id': 11,
                                                                  'lines': [{'sku': 'SIM-NOVA-8', 'quantity': 1}]}).status_code == 403


def test_immutable_events(admin_conn):
    # Earlier tests in this module created evidence/posting rows, so row-level triggers have rows to fire on.
    for stmt in ('UPDATE telco.sell_out SET quantity=quantity WHERE id=1', 'DELETE FROM telco.stock_ledger WHERE id=1',
                 'UPDATE telco.evidence_snapshot SET digest=digest', 'DELETE FROM telco.posting_request',
                 'UPDATE telco.assignment SET valid_to=valid_to WHERE id=1'):
        with pytest.raises(psycopg.errors.RaiseException, match='append-only'):
            admin_conn.execute(stmt)
        admin_conn.rollback()


def test_draft_confirm_idempotent_and_expiry(login, admin_conn):
    s6 = login('sls-006')
    d = s6.post('/v1/transactions/drafts', json={'channel': 'sell_in', 'outlet_id': 51, 'lines': [
        {'sku': 'SIM-NOVA-8', 'quantity': 3}, {'sku': 'BAL-NOVA-IDR', 'quantity': 100000}]}).json()
    assert 'Balas YA ' + d['confirm_code'] in d['summary'] and 'satuan tidak dijumlahkan' in d['summary']
    a = s6.post(f"/v1/transactions/drafts/{d['draft_id']}/confirm", json={'confirmation_event_id': 'test:confirm-1'})
    b = s6.post(f"/v1/transactions/drafts/{d['draft_id']}/confirm", json={'confirmation_event_id': 'test:confirm-2'})
    assert a.status_code == 200 and b.json()['idempotent_replay'] and len(a.json()['lines']) == 2
    d2 = s6.post('/v1/transactions/drafts', json={'channel': 'sell_in', 'outlet_id': 51, 'lines': [{'sku': 'SIM-NOVA-8', 'quantity': 1}]}).json()
    admin_conn.execute("UPDATE telco.pending_action SET created_at=now()-interval '2 hours',expires_at=now()-interval '1 second' WHERE id=%s", (d2['draft_id'],))
    admin_conn.commit()
    r = s6.post(f"/v1/transactions/drafts/{d2['draft_id']}/confirm", json={'confirmation_event_id': 'test:confirm-3'})
    assert r.status_code == 410 and err(r) == 'DRAFT_EXPIRED'


def test_validate_sql_after_operations(clone_db):
    with psycopg.connect(clone_db['admin']) as c:
        c.execute(open('sql/validate.sql', encoding='utf-8').read())
        c.execute(open('sql/validate_platform.sql', encoding='utf-8').read())


# ============================================================ conversation context
def _ctx(c, outlet=1, key=None):
    e = c.post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': outlet}).json()
    ctx = c.post('/v1/contexts', json={'evidence_id': e['evidence_id'], 'conversation_key': key or 'dash:' + uuid.uuid4().hex}).json()
    return e, ctx


def test_follow_up_preserves_context(login):
    c = login('spv-r01')
    e, ctx = _ctx(c)
    r = c.post(f"/v1/contexts/{ctx['context_id']}/follow-up", json={'question': 'Solusinya apa? apa yang harus dilakukan?', 'expected_version': 1}).json()
    assert r['status'] == 'pinned' and r['version'] == 1 and r['evidence']['evidence_id'] == e['evidence_id']
    assert r['pinned']['scope'] == {'type': 'outlet', 'id': 1} and r['pinned']['channel'] == 'sell_out'
    assert r['pinned']['current'] == ['2026-09-01', '2026-10-01'] and e['evidence_id'] in r['text']


def test_scope_change_requires_reauthorization(login, admin_conn):
    s1 = login('sls-001')
    _, ctx = _ctx(s1)
    r = s1.post(f"/v1/contexts/{ctx['context_id']}/follow-up", json={'question': 'kalau kecamatannya bagaimana?', 'expected_version': 1})
    assert r.status_code == 403
    spv = login('spv-r01')
    _, ctx2 = _ctx(spv)
    r2 = spv.post(f"/v1/contexts/{ctx2['context_id']}/follow-up", json={'question': 'kalau kecamatannya bagaimana?', 'expected_version': 1}).json()
    did = admin_conn.execute('SELECT district_id FROM telco.outlet WHERE id=1').fetchone()[0]
    assert r2['status'] == 'scope_changed' and r2['pinned']['scope'] == {'type': 'district', 'id': did} and r2['version'] == 2
    period = spv.post(f"/v1/contexts/{ctx2['context_id']}/follow-up", json={'question': 'kalau bulan lalu?', 'expected_version': 2}).json()
    assert period['status'] == 'needs_clarification'


def test_stale_ttl_prompts_and_conflict_409(login, admin_conn):
    c = login('spv-r01')
    _, ctx = _ctx(c)
    r = c.post(f"/v1/contexts/{ctx['context_id']}/follow-up", json={'question': 'solusinya?', 'expected_version': 7})
    assert r.status_code == 409 and err(r) == 'CONTEXT_VERSION_CONFLICT'
    admin_conn.execute("UPDATE telco.conversation_context SET expires_at=now()-interval '1 second' WHERE id=%s", (ctx['context_id'],))
    admin_conn.commit()
    r = c.post(f"/v1/contexts/{ctx['context_id']}/follow-up", json={'question': 'solusinya?', 'expected_version': 1})
    assert r.status_code == 410 and err(r) == 'CONTEXT_EXPIRED' and 'Sebutkan' in r.json()['error']['message']


def test_watermark_change_recomputes(login, admin_conn):
    c = login('spv-r01')
    e, ctx = _ctx(c)
    old = admin_conn.execute("SELECT batch_key FROM telco.ingestion_watermark WHERE source='sell_out'").fetchone()[0]
    admin_conn.execute("UPDATE telco.ingestion_watermark SET batch_key=%s WHERE source='sell_out'", (old + '-republished',))
    admin_conn.commit()
    try:
        r = c.post(f"/v1/contexts/{ctx['context_id']}/follow-up", json={'question': 'apa yang harus dilakukan?', 'expected_version': 1}).json()
        assert r['status'] == 'recomputed' and r['version'] == 2 and r['evidence']['evidence_id'] != e['evidence_id']
        assert r['evidence']['source_batch_key'] == old + '-republished' and 'dihitung ulang' in r['text']
        assert r['pinned']['current'] == ['2026-09-01', '2026-10-01']
    finally:
        admin_conn.execute("UPDATE telco.ingestion_watermark SET batch_key=%s WHERE source='sell_out'", (old,))
        admin_conn.commit()


def test_chart_and_narrative_match_evidence(login):
    from decimal import Decimal
    from telco_api import narrative
    from telco_api.charts import waterfall_points
    from telco_api.money import rupiah
    c = login('spv-r01')
    e = c.post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 1}).json()
    svg = c.get(f"/v1/evidence/{e['evidence_id']}/chart.svg").text
    for d in e['drivers']:
        assert rupiah(d['delta_idr']) in svg
    assert rupiah(e['revenue_baseline_idr']) in svg and rupiah(e['revenue_current_idr']) in svg and e['evidence_id'] in svg
    pts = waterfall_points(e)
    assert abs(sum(p['value'] for p in pts if p['kind'] == 'delta') - Decimal(e['delta_idr'])) <= Decimal('0.01')
    text = narrative.diagnose_text(e)
    assert narrative.verify(text, [e]) == [] and rupiah(e['delta_idr']) in text


def test_dashboard_snapshot_single_evidence(login):
    c = login('spv-r01')
    d = c.post('/v1/tools/dashboard', json={'scope': 'outlet', 'entity_id': 1, 'channel': 'sell_out'}).json()
    assert d['tool'] == 'dashboard' and d['diagnose_evidence_id'] and len(d['trend']) == 60
    known = [t for t in d['trend'] if t['revenue_idr'] is not None]
    from decimal import Decimal
    assert sum(Decimal(t['revenue_idr']) for t in known if t['period'] == 'current') == Decimal(d['revenue_current_idr'])
    gap = c.post('/v1/tools/dashboard', json={'scope': 'outlet', 'entity_id': 1, 'channel': 'sell_out', 'baseline_start': '2026-08-17',
                                              'baseline_end': '2026-09-16', 'current_start': '2026-09-16', 'current_end': '2026-10-16'}).json()
    assert any(t['status'] == 'missing_input' and t['revenue_idr'] is None for t in gap['trend'])


def test_export_csv_scoped_with_metadata(login):
    c = login('spv-r01')
    r = c.get('/v1/exports/sales.csv', params={'scope': 'outlet', 'entity_id': 1, 'channel': 'sell_out', 'start': '2026-09-01', 'end': '2026-10-01'})
    assert r.status_code == 200 and '# watermark: 2026-09-30' in r.text and '# channel: sell_out' in r.text
    assert r.headers['x-export-total-revenue-idr'] == '2159800.00' and 'TOTAL' in r.text
    assert c.get('/v1/exports/sales.csv', params={'scope': 'outlet', 'entity_id': 111, 'channel': 'sell_out', 'start': '2026-09-01',
                                                   'end': '2026-10-01'}).status_code == 403


# ============================================================ mocked WhatsApp E2E
def wa_payload(wa_id, msg_id, text, ts=None):
    return json.dumps({'object': 'whatsapp_business_account', 'entry': [{'id': 'WABA-TEST', 'changes': [{'field': 'messages', 'value': {
        'messaging_product': 'whatsapp', 'metadata': {'phone_number_id': '000000000000000'},
        'messages': [{'from': wa_id, 'id': msg_id, 'timestamp': str(ts or int(time.time())), 'type': 'text', 'text': {'body': text}}]}}]}]},
        separators=(',', ':')).encode()


def post_wa(client, settings, raw, sig=None):
    sig = sig or 'sha256=' + hmac.new(settings.wa_app_secret.get_secret_value().encode(), raw, hashlib.sha256).hexdigest()
    return client.post('/webhooks/whatsapp', content=raw, headers={'X-Hub-Signature-256': sig, 'Content-Type': 'application/json'})


def run_n8n_like(client, settings, msg_id):
    """What workflows 01-04 do: dispatch -> claim -> intent -> act -> (narrate) -> reply -> complete -> outbox dispatch."""
    h, ref = svc(settings), {'provider': 'whatsapp', 'message_id': msg_id}
    claim = client.post('/internal/v1/events/claim', json=ref, headers=h).json()
    if claim['status'] == 'completed':
        return claim
    client.post('/internal/v1/events/intent', json=ref, headers=h)
    act = client.post('/internal/v1/events/act', json=ref, headers=h)
    assert act.status_code == 200, act.text
    if act.json().get('next') == 'narrate':
        assert client.post('/internal/v1/events/narrate', json=ref, headers=h).status_code == 200
    client.post('/internal/v1/events/reply', json=ref, headers=h)
    client.post('/internal/v1/events/complete', json=ref, headers=h)
    return claim


def dispatch(client, settings, worker='test-worker'):
    return client.post('/internal/v1/outbox/dispatch', json={'limit': 50, 'lease_seconds': 30, 'worker_id': worker}, headers=svc(settings)).json()


WA1 = '009900000001'  # sls-001, non-routable test id


def test_wa_end_to_end_sale_insight_follow_up(app, settings, mocks, admin_conn):
    from fastapi.testclient import TestClient
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get('/webhooks/whatsapp', params={'hub.mode': 'subscribe', 'hub.verify_token': settings.wa_verify_token.get_secret_value(),
                                               'hub.challenge': '12345'}).text == '12345'
    assert c.get('/webhooks/whatsapp', params={'hub.mode': 'subscribe', 'hub.verify_token': 'wrong', 'hub.challenge': '1'}).status_code == 403
    # 1) sale input -> summary confirmation
    m1 = 'wamid.TEST.' + uuid.uuid4().hex
    assert post_wa(c, settings, wa_payload(WA1, m1, 'SELLIN OUT-0001 SIM-NOVA-8 5, BAL-NOVA-IDR 50000')).json()['accepted'] == 1
    n_before = len(mocks.n8n_received)
    dispatch(c, settings)
    assert any(x['message_id'] == m1 for x in mocks.n8n_received[n_before:])
    run_n8n_like(c, settings, m1)
    sent_before = len(mocks.wa_sent)
    dispatch(c, settings)
    summary = mocks.wa_sent[-1]['text']['body']
    assert len(mocks.wa_sent) == sent_before + 1 and 'Konfirmasi SELL-IN OUT-0001' in summary
    code = re.search(r'Balas YA ([A-F0-9]{6})', summary).group(1)
    assert admin_conn.execute("SELECT count(*) FROM telco.sell_in WHERE external_key LIKE 'DRF-%%' AND outlet_id=1").fetchone()[0] == 0
    # 2) confirmation -> posted atomically
    m2 = 'wamid.TEST.' + uuid.uuid4().hex
    post_wa(c, settings, wa_payload(WA1, m2, f'YA {code}'))
    dispatch(c, settings)
    run_n8n_like(c, settings, m2)
    dispatch(c, settings)
    assert mocks.wa_sent[-1]['text']['body'].startswith('Tercatat:')
    row = admin_conn.execute("SELECT confirmation_event_id,status FROM telco.pending_action WHERE payload->>'confirm_code'=%s", (code,)).fetchone()
    assert row == (f'whatsapp:{m2}', 'posted')
    # 3) insight -> Hermes narrative from evidence -> chart link -> context
    calls = mocks.hermes_calls
    m3 = 'wamid.TEST.' + uuid.uuid4().hex
    post_wa(c, settings, wa_payload(WA1, m3, 'kenapa omzet OUT-0001 turun?'))
    dispatch(c, settings)
    run_n8n_like(c, settings, m3)
    dispatch(c, settings)
    body = mocks.wa_sent[-1]['text']['body']
    assert mocks.hermes_calls == calls + 1 and '-Rp5.173.100' in body and 'Evidence ID' in body and '/charts/' in body
    # 4) follow-up keeps entity/channel/period
    m4 = 'wamid.TEST.' + uuid.uuid4().hex
    post_wa(c, settings, wa_payload(WA1, m4, 'apa yang harus dilakukan?'))
    dispatch(c, settings)
    run_n8n_like(c, settings, m4)
    dispatch(c, settings)
    fu = mocks.wa_sent[-1]['text']['body']
    assert 'Konteks tetap: sell-out Outlet Fiktif 0001 (OUT-0001), September 2026 vs Juni 2026' in fu
    # every outbound went only to the non-routable mock id
    assert all(m['to'].startswith('0099') for m in mocks.wa_sent)


def test_wa_duplicate_mismatch_invalid_signature(app, settings, admin_conn):
    from fastapi.testclient import TestClient
    c = TestClient(app, raise_server_exceptions=False)
    mid = 'wamid.TEST.' + uuid.uuid4().hex
    raw = wa_payload(WA1, mid, 'stok', ts=1792130000)
    assert post_wa(c, settings, raw).json()['accepted'] == 1
    assert post_wa(c, settings, raw).json()['duplicate'] == 1
    assert admin_conn.execute('SELECT count(*) FROM telco.outbox WHERE idempotency_key=%s', (f'dispatch:whatsapp:{mid}',)).fetchone()[0] == 1
    r = post_wa(c, settings, wa_payload(WA1, mid, 'target', ts=1792130000))
    assert r.status_code == 409 and err(r) == 'DUPLICATE_PAYLOAD_MISMATCH'
    bad_id = 'wamid.TEST.' + uuid.uuid4().hex
    r = post_wa(c, settings, wa_payload(WA1, bad_id, 'stok'), sig='sha256=' + '0' * 64)
    assert r.status_code == 401
    assert admin_conn.execute('SELECT count(*) FROM telco.inbound_event WHERE message_id=%s', (bad_id,)).fetchone()[0] == 0
    # unknown sender: stored for audit, never dispatched or replied to
    unk = 'wamid.TEST.' + uuid.uuid4().hex
    assert post_wa(c, settings, wa_payload('009977777777', unk, 'halo')).json()['unregistered'] == 1
    assert admin_conn.execute('SELECT count(*) FROM telco.outbox WHERE idempotency_key=%s', (f'dispatch:whatsapp:{unk}',)).fetchone()[0] == 0


def test_wa_out_of_order_and_idempotent_steps(app, settings, mocks):
    from fastapi.testclient import TestClient
    c = TestClient(app, raise_server_exceptions=False)
    t0 = int(time.time())
    early, late = 'wamid.TEST.E' + uuid.uuid4().hex, 'wamid.TEST.L' + uuid.uuid4().hex
    post_wa(c, settings, wa_payload('009900000002', late, 'stok', ts=t0 + 5))
    post_wa(c, settings, wa_payload('009900000002', early, 'target sell-in', ts=t0))
    run_n8n_like(c, settings, late)
    claim = run_n8n_like(c, settings, early)
    assert claim['out_of_order'] is True
    # Re-running every step after completion has no new side effect.
    again = run_n8n_like(c, settings, early)
    assert again['status'] == 'completed'


def test_outbox_crash_before_send_and_after_send(app, settings, mocks, db, admin_conn):
    from telco_api import outbox
    key = 'test-crash-' + uuid.uuid4().hex
    db.write(lambda conn: outbox.enqueue(conn, 'sls-001', 'whatsapp_text', {'text': 'tes crash'}, key, 'corr-test'))
    # (a) crash after claim commit, before send: nothing sent, lease expires, re-claimed and sent exactly once
    rows = db.write(lambda conn: outbox.claim(conn, 50, 30, 'dead-worker'))
    assert any(r['idempotency_key'] == key for r in rows)
    admin_conn.execute("UPDATE telco.outbox SET lease_expires_at=now()-interval '1 second' WHERE lease_owner='dead-worker'")
    admin_conn.commit()
    n = len(mocks.wa_sent)
    from fastapi.testclient import TestClient
    c = TestClient(app, raise_server_exceptions=False)
    dispatch(c, settings)
    assert sum(1 for m in mocks.wa_sent[n:] if m['biz_opaque_callback_data'] == key) == 1
    # (b) crash after provider accepted, before ACK -> at-least-once duplicate after lease expiry (documented)
    key2 = 'test-crash2-' + uuid.uuid4().hex
    db.write(lambda conn: outbox.enqueue(conn, 'sls-001', 'whatsapp_text', {'text': 'tes crash 2'}, key2, 'corr-test'))

    class Crash(Exception):
        pass

    def boom(row):
        if row['idempotency_key'] == key2:
            raise Crash()
    with pytest.raises(Crash):
        outbox.dispatch_batch(db, settings, _wa(app), _n8n(app), 50, 30, 'crashy', crash_after_send=boom)
    admin_conn.execute("UPDATE telco.outbox SET lease_expires_at=now()-interval '1 second' WHERE idempotency_key=%s", (key2,))
    admin_conn.commit()
    # (c) the provider status webhook arrives first -> reconciliation marks it sent -> no duplicate
    status = json.dumps({'object': 'whatsapp_business_account', 'entry': [{'id': 'W', 'changes': [{'field': 'messages', 'value': {
        'messaging_product': 'whatsapp', 'statuses': [{'id': 'wamid.MOCKX', 'status': 'sent', 'biz_opaque_callback_data': key2}]}}]}]},
        separators=(',', ':')).encode()
    assert post_wa(c, settings, status).json()['statuses'] == 1
    n2 = len(mocks.wa_sent)
    dispatch(c, settings)
    assert sum(1 for m in mocks.wa_sent[n2:] if m['biz_opaque_callback_data'] == key2) == 0
    assert admin_conn.execute('SELECT state FROM telco.outbox WHERE idempotency_key=%s', (key2,)).fetchone()[0] == 'sent'


def _wa(app):
    from telco_api.outbox import WhatsAppSender
    import httpx
    return WhatsAppSender(app.state.settings, client=httpx.Client(transport=httpx.MockTransport(app.state.mocks.wa_handler)))


def _n8n(app):
    from telco_api.outbox import N8nDispatcher
    import httpx
    return N8nDispatcher(app.state.settings, client=httpx.Client(transport=httpx.MockTransport(app.state.mocks.n8n_handler)))


def test_outbox_retry_and_dead_letter(app, settings, mocks, db, admin_conn):
    from telco_api import outbox
    from fastapi.testclient import TestClient
    c = TestClient(app, raise_server_exceptions=False)
    dispatch(c, settings)  # drain
    k1, k2 = 'test-retry-' + uuid.uuid4().hex, 'test-dead-' + uuid.uuid4().hex
    db.write(lambda conn: outbox.enqueue(conn, 'sls-001', 'whatsapp_text', {'text': 'retry'}, k1, 'corr-test'))
    mocks.wa_fail[:] = [503]
    dispatch(c, settings)
    st, att, nxt = admin_conn.execute('SELECT state,attempts,next_attempt_at>now() FROM telco.outbox WHERE idempotency_key=%s', (k1,)).fetchone()
    assert (st, att, nxt) == ('pending', 1, True)
    db.write(lambda conn: outbox.enqueue(conn, 'sls-001', 'whatsapp_text', {'text': 'dead'}, k2, 'corr-test'))
    mocks.wa_fail[:] = [400]
    dispatch(c, settings)
    admin_conn.rollback()
    st, dl = admin_conn.execute('SELECT state,dead_lettered_at IS NOT NULL FROM telco.outbox WHERE idempotency_key=%s', (k2,)).fetchone()
    assert (st, dl) == ('failed', True)


def test_narrator_fallbacks_and_no_numbers_when_tools_fail(app, settings, mocks, login, monkeypatch):
    from fastapi.testclient import TestClient
    c = TestClient(app, raise_server_exceptions=False)
    narrator = app.state.pipeline.narrator
    p = narrator.db.write(lambda conn: __import__('telco_api.security', fromlist=['load_principal']).load_principal(conn, 'sls-001', 'jwt'))
    e = login('sls-001').post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 1}).json()
    from telco_api.money import json_loads
    e = json_loads(json.dumps(e))
    for mode, expected in (('good', 'hermes'), ('fabricate', 'router_fallback'), ('down', 'router_fallback'), ('toolset_violation', 'router_fallback')):
        mocks.hermes_mode = mode
        narrator.hermes._probe = None
        narrator.hermes.breaker.success()
        out = narrator.narrate(p, e, 'kenapa turun?', 'wa:test-' + mode, 'corr-narr')
        assert out['source'] == expected, (mode, out)
        assert '987.654.321' not in out['text']
    mocks.hermes_mode = 'good'
    narrator.hermes._probe = None
    # Tool/DB failure: the act step returns 503 (retryable), caches nothing, and no numeric reply is produced.
    from telco_api import analytics, errors
    monkeypatch.setattr(analytics, 'diagnose', lambda *a, **k: (_ for _ in ()).throw(errors.unavailable('DB_UNAVAILABLE', 'db down')))
    mid = 'wamid.TEST.' + uuid.uuid4().hex
    post_wa(c, settings, wa_payload(WA1, mid, 'kenapa omzet OUT-0002 turun?'))
    h, ref = svc(settings), {'provider': 'whatsapp', 'message_id': mid}
    c.post('/internal/v1/events/claim', json=ref, headers=h)
    r = c.post('/internal/v1/events/act', json=ref, headers=h)
    assert r.status_code == 503
    steps = narrator.db.write(lambda conn: conn.execute("SELECT result->'steps' FROM telco.inbound_event WHERE message_id=%s", (mid,)).fetchone()[0])
    assert 'act' not in (steps or {})


def test_daily_target_nudge_once_per_day(app, settings, admin_conn):
    from fastapi.testclient import TestClient
    c = TestClient(app, raise_server_exceptions=False)
    friday_16 = '2026-10-16T16:05:00+07:00'
    a = c.post('/internal/v1/targets/nudges', json={'now': friday_16}, headers=svc(settings)).json()
    assert a['due'] == 2 and a['enqueued'] == 2
    b = c.post('/internal/v1/targets/nudges', json={'now': friday_16}, headers=svc(settings)).json()
    assert b['enqueued'] == 0 and b['already_sent'] == 2
    txt = admin_conn.execute("SELECT payload->>'text' FROM telco.outbox WHERE idempotency_key='target:sls-001:2026-10-16:sell_in'").fetchone()[0]
    assert 'Target sell-in 2026-10-16: realisasi' in txt and 'STOP TARGET' in txt
    sat = c.post('/internal/v1/targets/nudges', json={'now': '2026-10-17T16:05:00+07:00'}, headers=svc(settings)).json()
    assert sat['due'] == 0

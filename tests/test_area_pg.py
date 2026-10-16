"""Area scopes (migration 009): regency (kabupaten/kota) and province diagnosis for Head of Branch / Sales / Region.
An area aggregate requires a district grant on EVERY kecamatan in it (default-deny); numbers reconcile to kecamatan."""
from decimal import Decimal

import psycopg
import pytest

pytestmark = pytest.mark.pg
PERIOD = {'baseline_start': '2026-08-01', 'baseline_end': '2026-08-31', 'current_start': '2026-08-31', 'current_end': '2026-09-30'}
TOL = Decimal('0.01')


@pytest.fixture(scope='module')
def area(clone_db):
    with psycopg.connect(clone_db['admin']) as c:
        q = lambda sql, *a: c.execute(sql, a).fetchall()  # noqa: E731
        reg = dict(q('SELECT code,id FROM telco.regency'))
        east = {r[0] for r in q('''SELECT t.entity_id FROM telco.org_territory t JOIN telco.org_position p ON p.id=t.position_id
            WHERE p.code='HOS-EAST' AND t.scope_type='regency' ''')}
        madiun_d = [r[0] for r in q('SELECT id FROM telco.district WHERE regency_id=%s ORDER BY id', reg['35.19'])]
        province = q("SELECT id FROM telco.province WHERE code='35'")[0][0]
    return dict(reg=reg, east=east, west=set(reg.values()) - east, madiun=reg['35.19'], madiun_d=madiun_d, province=province)


def diag(client, scope, eid, channel='sell_out', **period):
    return client.post('/v1/tools/diagnose', json={'scope': scope, 'entity_id': eid, 'channel': channel, **(period or PERIOD)})


def D(v) -> Decimal:
    return Decimal(str(v))


def test_head_of_branch_only_own_regency(login, area):
    hob = login('hob-35.19')
    r = diag(hob, 'regency', area['madiun'])
    assert r.status_code == 200, r.text
    e = r.json()
    assert e['query_version'] == 'diagnose-area-v1' and e['entity']['type'] == 'regency' and e['entity']['code'] == '35.19'
    assert e['entity']['district_count'] == len(area['madiun_d']) and e['drill_down']['next_scope'] == 'district'
    for code in ('35.77', '35.78', '35.01'):  # Kota Madiun, Kota Surabaya, Pacitan
        assert diag(hob, 'regency', area['reg'][code]).status_code == 403
    assert diag(hob, 'province', area['province']).status_code == 403
    me = hob.get('/v1/me').json()
    assert [x['code'] for x in me['regencies']] == ['35.19'] and me['provinces'] == []


def test_head_of_sales_only_own_half(login, area):
    hos = login('hos-east')
    assert diag(hos, 'regency', min(area['east'])).status_code == 200
    assert diag(hos, 'regency', min(area['west'])).status_code == 403
    assert diag(hos, 'province', area['province']).status_code == 403
    r = hos.post('/v1/tools/rank-regencies', json={'channel': 'sell_out', **PERIOD})
    assert r.status_code == 200, r.text
    assert {x['regency_id'] for x in r.json()['regencies']} == area['east']
    west = login('hos-west').post('/v1/tools/rank-regencies', json={'channel': 'sell_in'}).json()
    assert {x['regency_id'] for x in west['regencies']} == area['west'] and len(area['west']) == 19


def test_head_of_region_province(login, area):
    hor = login('hor-35')
    r = diag(hor, 'province', area['province'], 'sell_in')
    assert r.status_code == 200, r.text
    e = r.json()
    assert len(e['regency_contributions']) == 38 and e['drill_down']['next_scope'] == 'regency'
    assert abs(sum(D(x['delta_idr']) for x in e['regency_contributions']) - D(e['delta_idr'])) <= TOL
    rank = hor.post('/v1/tools/rank-regencies', json={'channel': 'sell_in', **PERIOD}).json()
    assert len(rank['regencies']) == 38
    # Ranking and province diagnosis come from the same events: per-regency deltas sum to the province delta.
    assert abs(sum(D(x['delta_idr']) for x in rank['regencies']) - D(e['delta_idr'])) <= TOL
    assert login('hor-35').get('/v1/me').json()['provinces'][0]['id'] == area['province']


@pytest.mark.parametrize('pid', ['tm-35.19-01', 'sls-35.19.01', 'viewer-o1'])
def test_tm_sales_viewer_denied_area(login, area, pid):
    c = login(pid)
    assert diag(c, 'regency', area['madiun']).status_code == 403
    assert diag(c, 'province', area['province']).status_code == 403
    assert c.post('/v1/tools/rank-regencies', json={'channel': 'sell_out'}).status_code == 403
    assert c.post('/v1/tools/dashboard', json={'scope': 'regency', 'entity_id': area['madiun'], 'channel': 'sell_out'}).status_code == 403
    rr = c.post('/v1/tools/resolve-entity', json={'query': 'Kabupaten Madiun', 'scope': 'regency'}).json()
    assert rr['status'] == 'not_found'


@pytest.mark.parametrize('channel', ['sell_out', 'sell_in'])
def test_regency_equals_sum_of_its_kecamatan(login, area, clone_db, channel):
    hob = login('hob-35.19')
    e = diag(hob, 'regency', area['madiun'], channel).json()
    parts = {did: diag(hob, 'district', did, channel).json() for did in area['madiun_d']}
    for k in ('revenue_baseline_idr', 'revenue_current_idr', 'delta_idr'):
        assert abs(D(e[k]) - sum(D(p[k]) for p in parts.values())) <= TOL, k
    for c in e['district_contributions']:
        assert abs(D(c['delta_idr']) - D(parts[c['district_id']]['delta_idr'])) <= TOL
    assert {c['district_id'] for c in e['district_contributions']} <= set(area['madiun_d'])
    assert e['population']['outlets_in_scope'] == sum(p['population']['outlets_in_scope'] for p in parts.values())
    assert e['coverage']['expected_outlet_days'] == sum(p['coverage']['expected_outlet_days'] for p in parts.values())
    # Driver reconciliation <= Rp0,01 and only one channel (ground truth straight from that channel's view rows).
    assert abs(D(e['reconciliation_residual_idr'])) <= TOL
    assert abs(sum(D(d['delta_idr']) for d in e['drivers']) - D(e['delta_idr'])) <= TOL
    with psycopg.connect(clone_db['admin']) as c:
        truth = c.execute('''SELECT coalesce(sum(s.revenue_idr),0) FROM telco.v_daily_sales s JOIN telco.outlet o ON o.id=s.outlet_id
            JOIN telco.district d ON d.id=o.district_id WHERE d.regency_id=%s AND s.channel=%s AND s.day>=%s AND s.day<%s''',
                          (area['madiun'], channel, PERIOD['current_start'], PERIOD['current_end'])).fetchone()[0]
    assert D(e['revenue_current_idr']) == truth
    assert e['channel'] == channel and all(x['scope']['type'] in ('regency', 'district') for x in e['recommendation']['actions'])


def test_growth_null_when_baseline_zero(login, area):
    # Outlets open 2025-01-01: a 2024 baseline has no revenue -> growth null, never 0 or infinity.
    e = diag(login('hor-35'), 'province', area['province'], 'sell_out', baseline_start='2024-11-01', baseline_end='2024-12-01',
             current_start='2025-01-01', current_end='2025-01-31').json()
    assert D(e['revenue_baseline_idr']) == 0 and e['growth'] is None and e['data_ready'] is False


def test_area_grant_requires_every_kecamatan(admin_conn, login, area):
    admin_conn.execute('''INSERT INTO telco.app_principal(principal_id,role,display_name) VALUES('area-probe','supervisor','Probe Area Fiktif')
        ON CONFLICT DO NOTHING''')
    for did in area['madiun_d'][:-1]:
        admin_conn.execute("INSERT INTO telco.principal_grant(principal_id,scope_type,entity_id) VALUES('area-probe','district',%s)", (did,))
    admin_conn.commit()
    probe = login('area-probe')
    assert diag(probe, 'district', area['madiun_d'][0]).status_code == 200
    assert diag(probe, 'regency', area['madiun']).status_code == 403  # one kecamatan missing -> no aggregate
    admin_conn.execute("INSERT INTO telco.principal_grant(principal_id,scope_type,entity_id) VALUES('area-probe','district',%s)",
                       (area['madiun_d'][-1],))
    admin_conn.commit()
    e = diag(probe, 'regency', area['madiun']).json()
    # Evidence and context reauthorize on read: revoking one kecamatan blocks the regency evidence again.
    ctx = probe.post('/v1/contexts', json={'evidence_id': e['evidence_id'], 'conversation_key': 'dash:area-probe'}).json()
    admin_conn.execute("DELETE FROM telco.principal_grant WHERE principal_id='area-probe' AND entity_id=%s", (area['madiun_d'][-1],))
    admin_conn.commit()
    assert probe.get(f"/v1/evidence/{e['evidence_id']}").status_code == 403
    assert probe.post(f"/v1/contexts/{ctx['context_id']}/follow-up", json={'question': 'solusinya?', 'expected_version': 1}).status_code == 403


def test_area_context_follow_up(login, area, clone_db):
    hob = login('hob-35.19')
    e = diag(hob, 'regency', area['madiun']).json()
    ctx = hob.post('/v1/contexts', json={'evidence_id': e['evidence_id'], 'conversation_key': 'dash:area-ctx'}).json()
    r = hob.post(f"/v1/contexts/{ctx['context_id']}/follow-up", json={'question': 'apa yang harus dilakukan?', 'expected_version': 1}).json()
    assert r['status'] == 'pinned' and r['evidence']['evidence_id'] == e['evidence_id']  # area query_version is not "stale"
    k = hob.post(f"/v1/contexts/{ctx['context_id']}/follow-up", json={'question': 'kalau kecamatannya?', 'expected_version': 1}).json()
    assert k['status'] == 'needs_clarification'
    # Outlet -> "kabupatennya": allowed for the Head of Branch, re-authorized and denied for a Territory Manager.
    with psycopg.connect(clone_db['admin']) as c:
        oid = c.execute('''SELECT o.id FROM telco.outlet o JOIN telco.district d ON d.id=o.district_id
            JOIN telco.org_territory t ON t.scope_type='district' AND t.entity_id=d.id
            JOIN telco.org_position p ON p.id=t.position_id AND p.code='TM-35.19-01' ORDER BY o.id LIMIT 1''').fetchone()[0]
    eo = diag(hob, 'outlet', oid).json()
    c1 = hob.post('/v1/contexts', json={'evidence_id': eo['evidence_id'], 'conversation_key': 'dash:area-ctx-o'}).json()
    up = hob.post(f"/v1/contexts/{c1['context_id']}/follow-up", json={'question': 'kalau kabupatennya?', 'expected_version': 1}).json()
    assert up['status'] == 'scope_changed' and up['pinned']['scope'] == {'type': 'regency', 'id': area['madiun']}
    assert up['evidence']['query_version'] == 'diagnose-area-v1'
    tm = login('tm-35.19-01')
    et = diag(tm, 'outlet', oid).json()
    c2 = tm.post('/v1/contexts', json={'evidence_id': et['evidence_id'], 'conversation_key': 'dash:area-ctx-tm'}).json()
    assert tm.post(f"/v1/contexts/{c2['context_id']}/follow-up",
                   json={'question': 'kalau kabupatennya?', 'expected_version': 1}).status_code == 403


def test_area_dashboard_and_resolve(login, area):
    hob = login('hob-35.19')
    d = hob.post('/v1/tools/dashboard', json={'scope': 'regency', 'entity_id': area['madiun'], 'channel': 'sell_out'})
    assert d.status_code == 200, d.text
    j = d.json()
    assert [x['regency_id'] for x in j['regency_ranking']] == [area['madiun']] and j['district_contributions']
    complete = [t for t in j['trend'] if t['revenue_idr'] is not None and t['period'] == 'current']
    assert abs(sum(D(t['revenue_idr']) for t in complete) - D(j['revenue_current_idr'])) <= TOL or len(complete) < len(
        [t for t in j['trend'] if t['period'] == 'current'])
    rr = hob.post('/v1/tools/resolve-entity', json={'query': 'Kabupaten Madiun'}).json()
    assert rr['status'] == 'resolved' and rr['resolved'] == {'scope': 'regency', 'entity_id': area['madiun'], 'code': '35.19',
                                                              'name': 'Kabupaten Madiun'}
    hor = login('hor-35').post('/v1/tools/resolve-entity', json={'query': 'madiun', 'scope': 'regency'}).json()
    assert hor['status'] == 'ambiguous' and {c['code'] for c in hor['candidates']} == {'35.19', '35.77'}

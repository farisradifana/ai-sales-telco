"""Area-role hierarchy: Head of Region -> Head of Sales East/West -> Head of Branch -> Territory Manager -> Sales."""
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

pytestmark = pytest.mark.pg


@pytest.fixture(scope='module')
def org(clone_db):
    with psycopg.connect(clone_db['admin']) as c:
        q = lambda sql, *a: c.execute(sql, a).fetchall()  # noqa: E731
        counts = dict(q('SELECT level,count(*) FROM telco.org_position GROUP BY 1'))
        hos = dict(q('''SELECT p.code,count(*) FROM telco.org_position p JOIN telco.org_territory t ON t.position_id=p.id
            WHERE p.level='head_of_sales' GROUP BY 1'''))
        madiun = q("SELECT d.id FROM telco.district d JOIN telco.regency r ON r.id=d.regency_id WHERE r.code='35.19' ORDER BY d.code")
        east = q('''SELECT d.id FROM telco.district d JOIN telco.org_territory t ON t.entity_id=d.regency_id AND t.scope_type='regency'
            JOIN telco.org_position p ON p.id=t.position_id AND p.code='HOS-EAST' ''')
        tm1 = q('''SELECT t.entity_id FROM telco.org_territory t JOIN telco.org_position p ON p.id=t.position_id WHERE p.code='TM-35.19-01' ''')
        with_outlets = {r[0] for r in q('SELECT DISTINCT district_id FROM telco.outlet')}
        names = dict(q("SELECT code,title FROM telco.org_position WHERE code IN ('HOR-35','HOS-EAST','HOS-WEST','HOB-35.19','HOB-35.77','TM-35.19-01','SLS-35.19.01')"))
        o1_sales = q('SELECT p.principal_id FROM telco.assignment a JOIN telco.app_principal p ON p.sales_id=a.sales_id WHERE a.outlet_id=1')[0][0]
        o1_tm = q('''SELECT ap.principal_id FROM telco.outlet o JOIN telco.org_position s ON s.level='sales' AND s.sales_id=o.district_id
            JOIN telco.app_principal ap ON ap.org_position_id=s.parent_id WHERE o.id=1''')[0][0]
        other_tm = q("SELECT principal_id FROM telco.app_principal WHERE principal_id LIKE 'tm-35.78-%%' ORDER BY 1 LIMIT 1")[0][0]
    return dict(with_outlets=with_outlets, counts=counts, hos=hos, madiun={r[0] for r in madiun}, east={r[0] for r in east}, tm1={r[0] for r in tm1},
                names=names, o1_sales=o1_sales, o1_tm=o1_tm, other_tm=other_tm)


def test_structure_and_names(org):
    assert org['counts'] == {'head_of_region': 1, 'head_of_sales': 2, 'head_of_branch': 38, 'territory_manager': 231, 'sales': 666}
    assert org['hos'] == {'HOS-EAST': 19, 'HOS-WEST': 19}
    assert org['names'] == {'HOR-35': 'Head of Region Jawa Timur', 'HOS-EAST': 'Head of Sales East', 'HOS-WEST': 'Head of Sales West',
                            'HOB-35.19': 'Head of Branch Madiun', 'HOB-35.77': 'Head of Branch Kota Madiun',
                            'TM-35.19-01': 'Territory Manager Madiun Satu', 'SLS-35.19.01': 'Sales_Kebonsari'}
    assert len(org['tm1']) == 3 and org['tm1'] <= org['madiun']


def rank_ids(client):
    r = client.post('/v1/tools/rank-districts', json={'channel': 'sell_out'})
    return r.status_code, {d['district_id'] for d in r.json().get('districts', [])}


def test_each_level_sees_exactly_its_area(login, org):
    # Ranking population = districts that have outlets (telco.rank_districts), filtered to the principal's grants.
    w = org['with_outlets']
    assert rank_ids(login('hor-35')) == (200, w) and len(w) == 120
    assert rank_ids(login('hos-east')) == (200, org['east'] & w)
    assert rank_ids(login('hob-35.19')) == (200, org['madiun'] & w)
    for pid, area in (('tm-35.19-01', org['tm1']),):
        code, ids = rank_ids(login(pid))
        assert (code, ids) == ((200, area & w) if area & w else (403, set()))
    me = login('sls-35.19.01').get('/v1/me').json()
    assert [d['id'] for d in me['districts']] == [min(org['tm1'])] and len(me['districts']) == 1


def test_sales_cannot_see_neighbouring_kecamatan(login, org):
    s = login(org['o1_sales'])
    assert s.post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 1}).status_code == 200
    assert s.post('/v1/tools/diagnose', json={'scope': 'outlet', 'entity_id': 2}).status_code == 403
    assert login('hos-west').post('/v1/tools/diagnose', json={'scope': 'district', 'entity_id': min(org['east'])}).status_code == 403


def test_me_reports_position_and_manager(login, org):
    me = login('tm-35.19-01').get('/v1/me').json()
    assert me['org']['title'] == 'Territory Manager Madiun Satu' and me['org']['manager_title'] == 'Head of Branch Madiun'
    assert me['org']['sales_area'] == 'WEST' and {d['id'] for d in me['districts']} == org['tm1']
    s = login('sls-35.19.01').get('/v1/me').json()
    assert s['role'] == 'sales' and s['org']['manager_title'] == 'Territory Manager Madiun Satu'


def test_reversal_approved_only_within_area(login, org):
    sales = login(org['o1_sales'])
    r = sales.post('/v1/transactions/sell-in', json={'external_key': 'TEST-ORG-' + uuid.uuid4().hex[:10], 'outlet_id': 1, 'product_id': 4,
                                                     'quantity': 2, 'unit_price': '19800', 'client_confirmation_id': 'org-test-1'})
    assert r.status_code == 200, r.text
    req = sales.post('/v1/reversals', json={'channel': 'sell_in', 'transaction_id': r.json()['lines'][0]['transaction_id'],
                                            'reason': 'Tes otorisasi area'}).json()
    outsider = login(org['other_tm'])  # Territory Manager in Kota Surabaya
    assert req['action_id'] not in {a['action_id'] for a in outsider.get('/v1/actions').json()['items']}
    assert outsider.post(f"/v1/actions/{req['action_id']}/decision", json={'decision': 'approve'}).status_code == 404
    ok = login(org['o1_tm']).post(f"/v1/actions/{req['action_id']}/decision", json={'decision': 'approve'})
    assert ok.status_code == 200 and ok.json()['status'] == 'posted'
    assert datetime.now(timezone.utc).year >= 2026

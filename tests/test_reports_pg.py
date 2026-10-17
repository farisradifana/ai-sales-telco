"""Quick WhatsApp commands on real PostgreSQL: fixed reports per org level (scope, roll-up, level gates), short sell-in
input, and that none of it touches the AI chat quota."""
import re
import uuid
from decimal import Decimal

import psycopg
import pytest

from tests.test_chat_agent_pg import outbox_text, send, svc

pytestmark = pytest.mark.pg


@pytest.fixture(scope='module')
def org(clone_db):
    with psycopg.connect(clone_db['admin']) as c:
        o1_sales = c.execute('''SELECT p.principal_id FROM telco.assignment a JOIN telco.app_principal p ON p.sales_id=a.sales_id
            WHERE a.outlet_id=1 AND a.valid_to>now()''').fetchone()[0]
        hob = c.execute('''SELECT ap.principal_id FROM telco.outlet o JOIN telco.district d ON d.id=o.district_id
            JOIN telco.org_position b ON b.level='head_of_branch' JOIN telco.org_territory t ON t.position_id=b.id AND t.scope_type='regency'
            AND t.entity_id=d.regency_id JOIN telco.app_principal ap ON ap.org_position_id=b.id WHERE o.id=1''').fetchone()[0]
        tm = c.execute('''SELECT ap.principal_id FROM telco.outlet o JOIN telco.org_position s ON s.level='sales' AND s.sales_id=o.district_id
            JOIN telco.app_principal ap ON ap.org_position_id=s.parent_id WHERE o.id=1''').fetchone()[0]
    return {'sales': o1_sales, 'tm': tm, 'hob': hob, 'hos': 'hos-west', 'hor': 'hor-35'}


def principal(db, pid):
    from telco_api.security import load_principal
    return db.write(lambda c: load_principal(c, pid, 'test'))


def test_sell_in_rolls_up_from_sales(db, org, admin_conn):
    from telco_api import reports
    hob = reports.sell_in(db, principal(db, org['hob']), 'corr_t')
    assert hob['current_label'] == 'September 2026' and hob['comparisons'][0]['label'] == 'Agustus 2026'
    # Team rows add up to the head-of-branch totals (all targets sit with sales under some territory manager).
    assert sum(Decimal(c['target_idr']) for c in hob['children']) == Decimal(hob['target_idr'])
    assert sum(Decimal(c['actual_idr']) for c in hob['children']) == Decimal(hob['actual_idr'])
    assert [c['achievement'] for c in hob['children']] == sorted(c['achievement'] for c in hob['children'])
    want = admin_conn.execute('''SELECT sum(t.revenue_idr) FROM telco.sales_target t JOIN telco.principal_grant g ON g.principal_id=%s
        AND g.scope_type='district' JOIN telco.sales s ON s.id=t.sales_id AND s.district_id=g.entity_id
        WHERE t.channel='sell_in' AND t.target_date>='2026-09-01' AND t.target_date<'2026-10-01' ''', (org['hob'],)).fetchone()[0]
    assert Decimal(hob['target_idr']) == want
    s = reports.sell_in(db, principal(db, org['sales']), 'corr_t')
    assert s['sales_count'] == 1 and s['children'] == []
    text = reports.sell_in_text(s)
    assert 'Achievement' in text and 'Per produk' in text and 'tidak dijumlahkan' not in text


def test_reports_respect_scope_and_level(db, org):
    from telco_api import errors, reports
    sales = principal(db, org['sales'])
    so = reports.sell_out(db, sales, 'corr_t')
    ro = reports.rank_outlets(db, sales, 'corr_t')
    assert so['outlet_count'] == len(ro['outlets']) and all(o['code'] for o in ro['outlets'])
    for fn, who in ((reports.rank_districts, 'sales'), (reports.rank_regencies, 'tm'), (reports.rank_regencies, 'hob')):
        with pytest.raises(errors.AppError) as exc:
            fn(db, principal(db, org[who]), 'corr_t')
        assert exc.value.code == 'LEVEL_DENIED'
    rd = reports.rank_districts(db, principal(db, org['tm']), 'corr_t')
    assert len(rd['comparisons']) == 3 and all(len(a['sell_out']['growth']) == 3 for a in rd['areas'])
    west = reports.rank_regencies(db, principal(db, 'hos-west'), 'corr_t')
    east = reports.rank_regencies(db, principal(db, 'hos-east'), 'corr_t')
    hor = reports.rank_regencies(db, principal(db, 'hor-35'), 'corr_t')
    assert not {a['id'] for a in west['areas']} & {a['id'] for a in east['areas']}
    assert len(hor['areas']) == len(west['areas']) + len(east['areas'])
    assert len(reports.rank_area_text(hor)) < 3500  # long lists are cut to top 10 + bottom 5 for WhatsApp


def test_quick_commands_are_free_and_short_sell_in_posts(client, settings, clone_db, org, admin_conn, db):
    from telco_api.chat_agent import quota_status
    h, p = svc(settings), org['sales']
    used = quota_status(db, p)['used']
    for text, expect in (('Halo', 'Tekan *Menu*'), ('menu', 'Menu perintah cepat'), ('list produk', '*SIM-NOVA-8*\nPerdana Nova 8GB 30 Hari\nSI Rp22.500 · SO Rp25.000'),
                         ('cara input', 'OUT-0001 SIM-NOVA-8 5'), ('stok saya', 'Stok yang Anda pegang'), ('sell in', 'Achievement'),
                         ('sell out', 'Per produk'), ('peringkat outlet', 'Peringkat outlet'),
                         ('peringkat kecamatan', 'hanya untuk Territory Manager')):
        ref = send(client, settings, clone_db, p, text)
        for step in ('act', 'reply', 'complete'):
            assert client.post(f'/internal/v1/events/{step}', json=ref, headers=h).status_code == 200
        body = outbox_text(admin_conn, ref)
        assert expect in body, (text, body)
        assert 'tidak dijumlahkan' not in body
    assert quota_status(db, p)['used'] == used
    # Short input: outlet + product (+ quantity) is always a sell-in draft, posted only after YA <code>.
    ref = send(client, settings, clone_db, p, 'OUT-0001 SIM-NOVA-8 2')
    for step in ('act', 'reply', 'complete'):
        client.post(f'/internal/v1/events/{step}', json=ref, headers=h)
    summary = outbox_text(admin_conn, ref)
    assert 'Konfirmasi SELL-IN OUT-0001' in summary
    code = re.search(r'Balas \*YA ([A-F0-9]{6})\* untuk posting atau \*BATAL [A-F0-9]{6}\*', summary).group(1)
    ref = send(client, settings, clone_db, p, f'YA {code}')
    for step in ('act', 'reply', 'complete'):
        client.post(f'/internal/v1/events/{step}', json=ref, headers=h)
    done = outbox_text(admin_conn, ref)
    assert done.startswith('Tercatat:') and 'Total *Rp' in done and '.00 IDR' not in done
    act = admin_conn.execute("SELECT result->'steps'->'act' FROM telco.inbound_event WHERE message_id=%s", (ref['message_id'],)).fetchone()[0]
    assert [r['SKU'] for r in act['sheet_rows']] == ['SIM-NOVA-8'] and act['sheet_rows'][0]['Kode Outlet'] == 'OUT-0001'
    assert act['sheet_log'][0]['Keterangan'].startswith('Log input sell-in') and f'kode {code}' in act['sheet_log'][0]['Nilai']
    assert admin_conn.execute("SELECT status FROM telco.pending_action WHERE payload->>'confirm_code'=%s", (code,)).fetchone()[0] == 'posted'


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app, raise_server_exceptions=False)


def test_ai_report_tool_is_ticket_bound(client, settings, clone_db, org):
    h = svc(settings)
    ref = send(client, settings, clone_db, org['hob'], 'kecamatan mana yang paling bagus bulan lalu? ' + uuid.uuid4().hex[:4])
    s = client.post('/internal/v1/agent/start', json=ref, headers=h).json()
    r = client.post('/internal/v1/agent-tools/report', json={'kind': 'rank_districts'}, headers={**h, 'X-Agent-Ticket': s['ticket']})
    assert r.status_code == 200 and r.json()['kind'] == 'district'
    r = client.post('/internal/v1/agent-tools/report', json={'kind': 'rank_regencies'}, headers={**h, 'X-Agent-Ticket': s['ticket']})
    assert r.status_code == 403 and r.json()['error']['code'] == 'LEVEL_DENIED'


def test_ai_report_numbers_verified_or_template(client, settings, clone_db, org, admin_conn):
    from tests.test_chat_agent_pg import finish_and_reply
    h = svc(settings)
    ref = send(client, settings, clone_db, org['hob'], 'kecamatan mana yang sell out nya paling turun? ' + uuid.uuid4().hex[:4])
    s = client.post('/internal/v1/agent/start', json=ref, headers=h).json()
    e = client.post('/internal/v1/agent-tools/report', json={'kind': 'rank_districts'}, headers={**h, 'X-Agent-Ticket': s['ticket']}).json()
    worst = min(e['areas'], key=lambda a: Decimal(a['sell_out']['delta_idr'][0]))
    from telco_api.money import rupiah
    ok = f"Penurunan terbesar: {worst['name']}, sell-out turun {rupiah(worst['sell_out']['delta_idr'][0])} vs bulan lalu."
    assert finish_and_reply(client, settings, ref, ok)['source'] == 'n8n_agent'  # stored delta can be quoted
    ref = send(client, settings, clone_db, org['hob'], 'kecamatan mana yang sell out nya paling turun? ' + uuid.uuid4().hex[:4])
    s = client.post('/internal/v1/agent/start', json=ref, headers=h).json()
    client.post('/internal/v1/agent-tools/report', json={'kind': 'rank_districts'}, headers={**h, 'X-Agent-Ticket': s['ticket']})
    assert finish_and_reply(client, settings, ref, 'Turun Rp 9.876.543.')['source'] == 'template'
    assert outbox_text(admin_conn, ref).startswith('*Peringkat kecamatan')


def test_reply_goes_to_the_sending_number(app, client, settings, clone_db, mocks):
    """A principal can hold several numbers (e.g. a test id registered later): the reply must go to the sender."""
    import hashlib
    import hmac
    import json
    import time
    from telco_api.identity import IdentityVault
    older, newer = '0099' + str(uuid.uuid4().int)[:8], '0099' + str(uuid.uuid4().int)[:8]
    with psycopg.connect(clone_db['identity']) as c:
        IdentityVault(settings).register(c, older, 'hos-west')
        IdentityVault(settings).register(c, newer, 'hos-west')
    mid = 'wamid.REPLYTO' + uuid.uuid4().hex[:12]
    raw = json.dumps({'object': 'whatsapp_business_account', 'entry': [{'id': 'W', 'changes': [{'field': 'messages', 'value': {
        'messaging_product': 'whatsapp', 'metadata': {'phone_number_id': '000000000000000'},
        'messages': [{'from': older, 'id': mid, 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': 'menu'}}]}}]}]},
        separators=(',', ':')).encode()
    sig = 'sha256=' + hmac.new(settings.wa_app_secret.get_secret_value().encode(), raw, hashlib.sha256).hexdigest()
    client.post('/webhooks/whatsapp', content=raw, headers={'X-Hub-Signature-256': sig, 'Content-Type': 'application/json'})
    h, ref = svc(settings), {'provider': 'whatsapp', 'message_id': mid}
    for step in ('claim', 'intent', 'act', 'reply', 'complete'):
        client.post(f'/internal/v1/events/{step}', json=ref, headers=h)
    client.post('/internal/v1/outbox/dispatch', json={'limit': 50, 'lease_seconds': 30, 'worker_id': 't'}, headers=h)
    sent = [m for m in mocks.wa_sent if m['to'] in (older, newer)]
    assert sent and sent[-1]['to'] == older


def _post(client, settings, wa_from, message):
    import hashlib
    import hmac
    import json
    import time
    mid = 'wamid.PICK' + uuid.uuid4().hex[:12]
    raw = json.dumps({'object': 'whatsapp_business_account', 'entry': [{'id': 'W', 'changes': [{'field': 'messages', 'value': {
        'messaging_product': 'whatsapp', 'metadata': {'phone_number_id': '000000000000000'},
        'messages': [{'from': wa_from, 'id': mid, 'timestamp': str(int(time.time())), **message}]}}]}]}, separators=(',', ':')).encode()
    sig = 'sha256=' + hmac.new(settings.wa_app_secret.get_secret_value().encode(), raw, hashlib.sha256).hexdigest()
    assert client.post('/webhooks/whatsapp', content=raw, headers={'X-Hub-Signature-256': sig, 'Content-Type': 'application/json'}).status_code == 200
    h, ref = svc(settings), {'provider': 'whatsapp', 'message_id': mid}
    for step in ('claim', 'intent', 'act', 'reply', 'complete'):
        client.post(f'/internal/v1/events/{step}', json=ref, headers=h)
    for _ in range(20):  # drain: other tests may have left messages queued ahead of ours
        if client.post('/internal/v1/outbox/dispatch', json={'limit': 50, 'lease_seconds': 30, 'worker_id': 't'}, headers=h).json()['claimed'] == 0:
            break
    return mid


def test_halo_sends_a_picker_and_a_tap_runs_the_command(app, client, settings, clone_db, org, mocks, db):
    """'Halo' -> WhatsApp list message with the role's quick commands; tapping a row works exactly like typing it."""
    from telco_api.chat_agent import quota_status
    from telco_api.identity import IdentityVault
    num = '0099' + str(uuid.uuid4().int)[:8]
    with psycopg.connect(clone_db['identity']) as c:
        IdentityVault(settings).register(c, num, org['tm'])
    used = quota_status(db, org['tm'])['used']
    _post(client, settings, num, {'type': 'text', 'text': {'body': 'Halo'}})
    hello = [m for m in mocks.wa_sent if m['to'] == num][-1]
    assert hello['type'] == 'interactive' and hello['interactive']['type'] == 'button' and 'Halo, *Territory Manager' in hello['interactive']['body']['text'] and 'AI Sales Intelligence Telco' in hello['interactive']['body']['text']
    assert hello['interactive']['action']['buttons'] == [{'type': 'reply', 'reply': {'id': 'menu', 'title': 'Menu'}}]
    _post(client, settings, num, {'type': 'interactive', 'interactive': {'type': 'button_reply', 'button_reply': {'id': 'menu', 'title': 'Menu'}}})
    msg = [m for m in mocks.wa_sent if m['to'] == num][-1]
    assert msg['type'] == 'interactive' and msg['interactive']['type'] == 'list' and 'text' not in msg
    rows = msg['interactive']['action']['sections'][0]['rows']
    ids = [r['id'] for r in rows]
    assert 'peringkat kecamatan' in ids and 'peringkat kabupaten' not in ids and 'stok saya' not in ids  # territory manager
    assert all(len(r['title']) <= 24 and len(r.get('description', '')) <= 72 for r in rows) and len(rows) <= 10
    _post(client, settings, num, {'type': 'interactive', 'interactive': {'type': 'list_reply', 'list_reply': {'id': 'sell in', 'title': 'Sell in'}}})
    reply = [m for m in mocks.wa_sent if m['to'] == num][-1]
    # Short report: the report itself is the body of a "Pilih Perintah" list message.
    assert reply['type'] == 'interactive' and reply['interactive']['action']['button'] == 'Pilih Perintah'
    assert 'Achievement' in reply['interactive']['body']['text'] and 'Progres tim' in reply['interactive']['body']['text']
    assert 'Halo, *Territory Manager' in hello['interactive']['body']['text']  # role name in bold
    assert quota_status(db, org['tm'])['used'] == used  # picker and tapped commands never touch the AI quota


def test_long_report_is_followed_by_a_picker(app, client, settings, clone_db, mocks):
    """Reports longer than a list body (1024) go as text, then a separate "Pilih Perintah" message."""
    from telco_api.identity import IdentityVault
    num = '0099' + str(uuid.uuid4().int)[:8]
    with psycopg.connect(clone_db['identity']) as c:
        IdentityVault(settings).register(c, num, 'hor-35')
    _post(client, settings, num, {'type': 'text', 'text': {'body': 'peringkat kabupaten'}})
    last_two = [m for m in mocks.wa_sent if m['to'] == num][-2:]
    assert last_two[0]['type'] == 'text' and 'Peringkat kabupaten' in last_two[0]['text']['body']
    assert last_two[1]['type'] == 'interactive' and last_two[1]['interactive']['action']['button'] == 'Pilih Perintah'


def test_picker_waits_for_its_report(app, client, settings, clone_db, mocks):
    """If the report send fails transiently, the follow-up picker is held until the report goes out."""
    from telco_api.identity import IdentityVault
    num = '0099' + str(uuid.uuid4().int)[:8]
    with psycopg.connect(clone_db['identity']) as c:
        IdentityVault(settings).register(c, num, 'hor-35')
    mocks.wa_fail.append(503)  # the report (first send of the batch for this number) fails once
    _post(client, settings, num, {'type': 'text', 'text': {'body': 'peringkat kecamatan'}})
    with psycopg.connect(clone_db['admin'], autocommit=True) as a:  # make the retry due now
        a.execute("UPDATE telco.outbox SET next_attempt_at=now() WHERE state='pending' AND idempotency_key LIKE 'reply:whatsapp:wamid.PICK%%'")
    h = svc(settings)
    for _ in range(5):
        client.post('/internal/v1/outbox/dispatch', json={'limit': 50, 'lease_seconds': 30, 'worker_id': 't'}, headers=h)
    mine = [m for m in mocks.wa_sent if m['to'] == num]
    assert [m['type'] for m in mine] == ['text', 'interactive']


def test_chart_shows_sell_out_and_sell_in(app, client, settings, db, org):
    """The chart link attached to a diagnosis covers both channels for the same entity and periods."""
    from urllib.parse import urlparse
    from telco_api import charts
    from telco_api.schemas import DiagnoseRequest
    from telco_api import analytics
    p = principal(db, org['sales'])
    e = analytics.diagnose(db, p, DiagnoseRequest(scope='outlet', entity_id=1, channel='sell_out'), 'corr_t')
    u = urlparse(charts.paired_chart_url(db, settings, p, e, 'corr_t'))
    svg = client.get(f'{u.path}?{u.query}').text
    assert 'Sell-out Outlet Fiktif 0001' in svg and 'Sell-in Outlet Fiktif 0001' in svg and svg.count('(pembanding)') == 2

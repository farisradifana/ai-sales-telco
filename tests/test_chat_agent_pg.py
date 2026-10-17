"""n8n AI Agent turn on real PostgreSQL: daily chat quota per org level, ticket-bound tools, number verification,
refund on model failure, memory schema isolation and retention."""
import hashlib
import hmac
import json
import time
import uuid

import psycopg
import pytest

pytestmark = pytest.mark.pg


def svc(settings):
    return {'Authorization': f'Bearer {settings.n8n_service_token.get_secret_value()}'}


@pytest.fixture(scope='module')
def people(clone_db):
    with psycopg.connect(clone_db['admin']) as c:
        q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        return {'sales': q("SELECT principal_id FROM telco.app_principal WHERE principal_id LIKE 'sls-%' ORDER BY 1 OFFSET 7 LIMIT 1"),
                'sales2': q("SELECT principal_id FROM telco.app_principal WHERE principal_id LIKE 'sls-%' ORDER BY 1 OFFSET 8 LIMIT 1"),
                'tm': q("SELECT principal_id FROM telco.app_principal WHERE principal_id LIKE 'tm-%' ORDER BY 1 OFFSET 3 LIMIT 1"),
                'hob': q("SELECT principal_id FROM telco.app_principal WHERE principal_id LIKE 'hob-%' ORDER BY 1 OFFSET 3 LIMIT 1"),
                'hos': 'hos-west', 'hor': 'hor-35', 'admin': 'adm-ops'}


_WA: dict = {}


def wa_id(clone_db, settings, principal_id):
    if principal_id not in _WA:
        from telco_api.identity import IdentityVault
        _WA[principal_id] = '0099' + str(uuid.uuid4().int)[:8]
        with psycopg.connect(clone_db['identity']) as c:
            IdentityVault(settings).register(c, _WA[principal_id], principal_id)
    return _WA[principal_id]


def send(client, settings, clone_db, principal_id, text):
    """Ingest a signed WhatsApp text and run router steps (claim + intent). Returns the message ref."""
    mid = 'wamid.chat' + uuid.uuid4().hex[:16]
    raw = json.dumps({'object': 'whatsapp_business_account', 'entry': [{'id': 'WABA-TEST', 'changes': [{'field': 'messages', 'value': {
        'messaging_product': 'whatsapp', 'metadata': {'phone_number_id': '000000000000000'},
        'messages': [{'from': wa_id(clone_db, settings, principal_id), 'id': mid, 'timestamp': str(int(time.time())), 'type': 'text',
                      'text': {'body': text}}]}}]}]}, separators=(',', ':')).encode()
    sig = 'sha256=' + hmac.new(settings.wa_app_secret.get_secret_value().encode(), raw, hashlib.sha256).hexdigest()
    assert client.post('/webhooks/whatsapp', content=raw, headers={'X-Hub-Signature-256': sig, 'Content-Type': 'application/json'}).status_code == 200
    ref = {'provider': 'whatsapp', 'message_id': mid}
    assert client.post('/internal/v1/events/claim', json=ref, headers=svc(settings)).json()['status'] == 'claimed'
    client.post('/internal/v1/events/intent', json=ref, headers=svc(settings))
    return ref


def finish_and_reply(client, settings, ref, text, failed=False):
    h = svc(settings)
    r = client.post('/internal/v1/agent/finish', json={**ref, 'text': text, 'failed': failed}, headers=h)
    assert r.status_code == 200, r.text
    assert client.post('/internal/v1/events/reply', json=ref, headers=h).status_code == 200
    assert client.post('/internal/v1/events/complete', json=ref, headers=h).status_code == 200
    return r.json()


def outbox_text(admin_conn, ref):
    return admin_conn.execute("SELECT payload->>'text' FROM telco.outbox WHERE idempotency_key=%s",
                              (f"reply:{ref['provider']}:{ref['message_id']}",)).fetchone()[0]


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app, raise_server_exceptions=False)


def test_quota_policy_per_level(db, people):
    from telco_api.chat_agent import quota_status
    want = {'sales': 3, 'tm': 3, 'hob': 5, 'hos': 10, 'hor': 20, 'admin': 0}
    assert {k: quota_status(db, people[k])['limit'] for k in want} == want


def test_sales_gets_three_chats_then_deterministic_answer(client, settings, clone_db, people, admin_conn):
    p, h = people['sales'], svc(settings)
    for i in range(3):
        ref = send(client, settings, clone_db, p, f'halo, gimana kabar penjualan saya hari ini? ({i})')
        s1 = client.post('/internal/v1/agent/start', json=ref, headers=h).json()
        s2 = client.post('/internal/v1/agent/start', json=ref, headers=h).json()  # n8n retry: counted once, fresh ticket
        assert s1['next'] == s2['next'] == 'agent' and s1['ticket'] != s2['ticket']
        assert s2['quota'] == {'allowed': True, 'limit': 3, 'used': i + 1, 'remaining': 2 - i}
        assert s1['session_key'] == f'wa:{p}' and 'ticket' not in s1['system_prompt'] and s1['ticket'] not in s1['system_prompt']
        finish_and_reply(client, settings, ref, 'Siap, ada yang bisa saya bantu?')
    ref = send(client, settings, clone_db, p, 'stok saya berapa ya')
    s = client.post('/internal/v1/agent/start', json=ref, headers=h).json()
    assert s['next'] == 'reply' and 'ticket' not in s
    client.post('/internal/v1/events/reply', json=ref, headers=h)
    text = outbox_text(admin_conn, ref)
    # Quota used up: no model call, the reply lists the quick commands that stay free.
    assert 'Kuota chat AI Anda hari ini sudah habis (3 dari 3)' in text and '*stok saya*' in text and '*sell in*' in text
    assert 'peringkat kecamatan' not in text  # sales level: no area rankings


def test_admin_without_position_has_no_ai_chat(client, settings, clone_db, people, admin_conn):
    ref = send(client, settings, clone_db, people['admin'], 'apa kabar')
    s = client.post('/internal/v1/agent/start', json=ref, headers=svc(settings)).json()
    assert s['next'] == 'reply'
    client.post('/internal/v1/events/reply', json=ref, headers=svc(settings))
    assert 'tidak mendapat kuota chat AI' in outbox_text(admin_conn, ref)


def test_tools_need_ticket_and_verified_numbers(client, settings, clone_db, people, admin_conn):
    p, h = people['sales2'], svc(settings)
    ref = send(client, settings, clone_db, p, 'stok saya masih berapa?')
    s = client.post('/internal/v1/agent/start', json=ref, headers=h).json()
    # Service token alone is not enough: the one-turn ticket decides the principal.
    assert client.post('/internal/v1/agent-tools/stock', json={'account': 'self'}, headers=h).status_code == 401
    tool = client.post('/internal/v1/agent-tools/stock', json={'account': 'self', 'outlet_id': None},
                       headers={**h, 'X-Agent-Ticket': s['ticket']})
    assert tool.status_code == 200, tool.text
    item = tool.json()['items'][0]
    qty = int(float(item['quantity']))
    good = f"Stok {item['sku']} Anda tinggal {qty:,} pcs.".replace(',', '.')
    r = finish_and_reply(client, settings, ref, good + ' evidence 123e4567-e89b-12d3-a456-426614174000')
    assert r == {'next': 'reply', 'source': 'n8n_agent', 'verified': True}
    text = outbox_text(admin_conn, ref)
    assert text.startswith('Stok ') and '123e4567' not in text
    # Fabricated number -> replaced by the deterministic rendering of the turn's evidence.
    ref2 = send(client, settings, clone_db, p, 'stok saya masih berapa? (lagi)')
    s2 = client.post('/internal/v1/agent/start', json=ref2, headers=h).json()
    client.post('/internal/v1/agent-tools/stock', json={'account': 'self'}, headers={**h, 'X-Agent-Ticket': s2['ticket']})
    r2 = finish_and_reply(client, settings, ref2, 'Stok Anda 987654 pcs, naik 73%.')
    assert r2['source'] == 'template' and 'Stok yang Anda pegang' in outbox_text(admin_conn, ref2)
    # Ticket is single-turn: it does not work for another principal's tools after the TTL/budget, nor across turns.
    assert client.post('/internal/v1/agent-tools/target', json={'channel': 'sell_in', 'sales_id': 1},
                       headers={**h, 'X-Agent-Ticket': 'tkt_forged'}).status_code == 401


def test_model_failure_refunds_quota(client, settings, clone_db, people, admin_conn, db):
    from telco_api.chat_agent import quota_status
    p, h = people['tm'], svc(settings)
    before = quota_status(db, p)['used']
    ref = send(client, settings, clone_db, p, 'bagaimana performa kecamatan saya?')
    assert client.post('/internal/v1/agent/start', json=ref, headers=h).json()['quota']['used'] == before + 1
    r = finish_and_reply(client, settings, ref, None, failed=True)
    assert r['source'] == 'apology' and quota_status(db, p)['used'] == before
    assert 'Kuota chat Anda tidak terpakai' in outbox_text(admin_conn, ref)
    # finish is idempotent (n8n retry) and start after finish skips the model.
    assert client.post('/internal/v1/agent/finish', json={**ref, 'text': 'x', 'failed': False}, headers=h).json()['source'] == 'apology'
    assert client.post('/internal/v1/agent/start', json=ref, headers=h).json() == {'next': 'reply'}


def test_memory_role_isolated_and_pruned(clone_db, client, settings, people):
    if not clone_db['memory'].split(':')[2].split('@')[0]:
        pytest.skip('TELCO_AGENT_MEMORY_PASSWORD absent')
    with psycopg.connect(clone_db['memory'], autocommit=True) as m:
        m.execute('''CREATE TABLE IF NOT EXISTS agent_memory.chat_history (id SERIAL PRIMARY KEY, session_id VARCHAR(255) NOT NULL,
            message JSONB NOT NULL)''')  # what LangChain runs before every read
        m.execute("INSERT INTO agent_memory.chat_history(session_id,message) VALUES('wa:test','{\"type\":\"human\"}')")
        for sql in ('SELECT 1 FROM telco.app_principal', 'SELECT 1 FROM telco_identity.wa_identity', 'DELETE FROM agent_memory.chat_history'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                m.execute(sql)
    with psycopg.connect(clone_db['admin'], autocommit=True) as a:
        a.execute("UPDATE agent_memory.chat_history SET created_at=now()-interval '30 days' WHERE session_id='wa:test'")
    ref = send(client, settings, clone_db, people['hor'], 'ringkas performa provinsi')
    client.post('/internal/v1/agent/start', json=ref, headers=svc(settings))  # start prunes past retention
    with psycopg.connect(clone_db['admin']) as a:
        assert a.execute("SELECT count(*) FROM agent_memory.chat_history WHERE session_id='wa:test'").fetchone()[0] == 0

"""Process-level mocked E2E (no real WhatsApp, no real model vendor).

Pre-started services (see docs/RUNBOOK.md "E2E lokal"): mock WA Graph :8790, mock LLM :8791, MCP bridge :8765,
Hermes gateway :8642 (isolated HERMES_HOME), API :8000 (WA_SEND_MODE=mock), n8n :5678 with workflows 01-04+06
imported/published and credentials bound. This script only talks to the public webhook + read-only endpoints and the DB.

  python scripts/e2e_local.py --report reports/e2e_local.json
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import time
import uuid

import httpx
import psycopg

API = 'http://127.0.0.1:8000'
WA = 'http://127.0.0.1:8790'
SENDER = ''  # set in main(): fresh non-routable test id bound to the district sales covering OUT-0001


def sign_and_post(text: str, msg_id: str | None = None, raw: bytes | None = None):
    msg_id = msg_id or 'wamid.E2E.' + uuid.uuid4().hex
    raw = raw or json.dumps({'object': 'whatsapp_business_account', 'entry': [{'id': 'WABA-E2E', 'changes': [{'field': 'messages', 'value': {
        'messaging_product': 'whatsapp', 'metadata': {'phone_number_id': '000000000000000'},
        'messages': [{'from': SENDER, 'id': msg_id, 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}]}}]}]},
        separators=(',', ':')).encode()
    sig = 'sha256=' + hmac.new(os.environ['WA_APP_SECRET'].encode(), raw, hashlib.sha256).hexdigest()
    r = httpx.post(API + '/webhooks/whatsapp', content=raw, headers={'X-Hub-Signature-256': sig, 'Content-Type': 'application/json'}, timeout=10)
    return msg_id, raw, r


def wait_reply(msg_id: str, timeout: float = 180) -> dict:
    """Replies are matched to their inbound message via the outbox idempotency key echoed as biz_opaque_callback_data."""
    end, key = time.time() + timeout, f'reply:whatsapp:{msg_id}'
    while time.time() < end:
        hit = [m for m in httpx.get(WA + '/_sent', timeout=5).json() if m.get('biz_opaque_callback_data') == key]
        if hit:
            return hit[-1]
        time.sleep(1.5)
    raise TimeoutError('no outbound WhatsApp (mock) within timeout for ' + msg_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--report', default='reports/e2e_local.json')
    a = ap.parse_args()
    db = psycopg.connect(os.environ['ADMIN_DATABASE_URL'], autocommit=True)
    global SENDER
    from telco_api.identity import IdentityVault
    from telco_api.settings import Settings
    principal = db.execute('''SELECT p.principal_id FROM telco.assignment a JOIN telco.app_principal p ON p.sales_id=a.sales_id
        WHERE a.outlet_id=1''').fetchone()[0]
    SENDER = '0099' + str(uuid.uuid4().int)[:8]  # '0' prefix: never a valid E.164 number
    IdentityVault(Settings(reader_database_url='postgresql://unused', writer_database_url='postgresql://unused')).register(db, SENDER, principal)
    steps, t0 = [], time.time()

    def count():
        return len(httpx.get(WA + '/_sent', timeout=5).json())

    def step(name, text):
        mid, raw, r = sign_and_post(text)
        assert r.status_code == 200 and r.json()['accepted'] == 1, r.text
        reply = wait_reply(mid)
        for _ in range(40):  # reply is sent before the 'complete' step finishes; wait for the final state
            if db.execute('SELECT state FROM telco.inbound_event WHERE message_id=%s', (mid,)).fetchone()[0] == 'completed':
                break
            time.sleep(0.5)
        ev = db.execute("SELECT state,result->'steps'->'narrate'->>'source' FROM telco.inbound_event WHERE message_id=%s", (mid,)).fetchone()
        steps.append({'step': name, 'message_id': mid, 'reply': reply['text']['body'], 'to_prefix': reply['to'][:4],
                      'event_state': ev[0], 'narrative_source': ev[1], 'seconds': round(time.time() - t0, 1)})
        return mid, raw, reply['text']['body']

    _, _, summary = step('sale_draft', 'SELLIN OUT-0001 SIM-NOVA-8 2')
    code = re.search(r'Balas \*?YA ([A-F0-9]{6})', summary).group(1)
    step('sale_confirm', f'YA {code}')
    mid, raw, insight = step('insight', 'kenapa omzet OUT-0001 turun?')
    _, _, follow = step('follow_up', 'apa yang harus dilakukan?')
    # Duplicate delivery of the insight webhook: deduplicated at ingress, no second reply.
    n = count()
    _, _, r = sign_and_post('', msg_id=mid, raw=raw)
    time.sleep(25)
    dup = {'status': r.status_code, 'body': r.json(), 'extra_replies': count() - n}
    checks = {
        'summary_has_confirmation_code': bool(code),
        'confirm_posted': steps[1]['reply'].startswith('Tercatat:'),
        'insight_from_hermes': steps[2]['narrative_source'] == 'hermes',
        'insight_numbers_from_evidence': '-Rp5.173.100' in insight and 'Evidence ID' not in insight,
        'follow_up_preserves_context': 'Konteks tetap: sell-out Outlet Fiktif 0001 (OUT-0001), September 2026 vs Juni 2026' in follow,
        'duplicate_ignored': dup['body'].get('duplicate') == 1 and dup['extra_replies'] == 0,
        'only_non_routable_recipients': all(s['to_prefix'] == '0099' for s in steps),
        'all_events_completed': all(s['event_state'] == 'completed' for s in steps),
    }
    report = {'passed': all(checks.values()), 'checks': checks, 'steps': steps, 'duplicate': dup,
              'path': 'signed webhook -> API ingress -> outbox -> n8n (regular mode) -> API steps -> Hermes 0.20.5 -> MCP bridge -> '
                      'API tools -> PostgreSQL 17.11 -> outbox -> mock WhatsApp Graph'}
    with open(a.report, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(checks, indent=2))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()

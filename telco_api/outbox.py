"""Transactional outbox + dispatcher. Delivery to WhatsApp/n8n is AT-LEAST-ONCE:
- crash after enqueue commit, before send  -> lease expires, row re-claimed, sent once;
- crash after provider accepted, before ACK -> lease expires, row re-sent (possible duplicate). Mitigation: the
  idempotency key travels as biz_opaque_callback_data; a status webhook for a still-'sending' row marks it sent.
Exactly-once delivery is NOT claimed."""
import hashlib
import hmac
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from psycopg.types.json import Jsonb

from . import errors
from .identity import IdentityVault
from .settings import Settings

log = logging.getLogger('telco.outbox')
LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1', 'mock-wa', 'mock-whatsapp'}


def enqueue(conn, principal_id: str, kind: str, payload: dict, idempotency_key: str, correlation_id: str,
            max_attempts: int = 8) -> int | None:
    r = conn.execute('''INSERT INTO telco.outbox(idempotency_key,principal_id,payload,kind,correlation_id,max_attempts)
        VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id''',
                     (idempotency_key, principal_id, Jsonb(payload), kind, correlation_id, max_attempts)).fetchone()
    if r:
        return r[0]
    return conn.execute('SELECT id FROM telco.outbox WHERE idempotency_key=%s', (idempotency_key,)).fetchone()[0]


def claim(conn, limit: int, lease_seconds: int, worker_id: str) -> list[dict]:
    rows = conn.execute('''UPDATE telco.outbox o SET state='sending',lease_owner=%s,lease_expires_at=now()+make_interval(secs=>%s),
            attempts=o.attempts+1
        WHERE o.id IN (SELECT id FROM telco.outbox
            WHERE (state='pending' AND next_attempt_at<=now()) OR (state='sending' AND lease_expires_at<now())
            ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s)
        RETURNING o.id,o.idempotency_key,o.principal_id,o.payload,o.kind,o.attempts,o.max_attempts,o.correlation_id''',
                        (worker_id, lease_seconds, limit)).fetchall()
    return [{'id': r[0], 'idempotency_key': r[1], 'principal_id': r[2], 'payload': r[3], 'kind': r[4], 'attempts': r[5],
             'max_attempts': r[6], 'correlation_id': r[7]} for r in rows]


def ack(conn, row_id: int, worker_id: str, provider_message_id: str | None) -> bool:
    r = conn.execute('''UPDATE telco.outbox SET state='sent',sent_at=now(),provider_message_id=%s,lease_owner=NULL,lease_expires_at=NULL,
        last_error=NULL WHERE id=%s AND state='sending' AND lease_owner=%s RETURNING id''', (provider_message_id, row_id, worker_id)).fetchone()
    return bool(r)


def backoff_seconds(attempts: int, base: float = 5.0, cap: float = 3600.0) -> float:
    # Exponential with full jitter, bounded.
    return random.uniform(0.5, 1.0) * min(cap, base * (2 ** max(0, attempts - 1)))


def fail(conn, row_id: int, worker_id: str, error: str, attempts: int, max_attempts: int, permanent: bool = False) -> str:
    if permanent or attempts >= max_attempts:
        conn.execute('''UPDATE telco.outbox SET state='failed',dead_lettered_at=now(),last_error=%s,lease_owner=NULL,lease_expires_at=NULL
            WHERE id=%s AND lease_owner=%s''', (error[:500], row_id, worker_id))
        return 'dead_letter'
    conn.execute('''UPDATE telco.outbox SET state='pending',next_attempt_at=now()+make_interval(secs=>%s),last_error=%s,
        lease_owner=NULL,lease_expires_at=NULL WHERE id=%s AND lease_owner=%s''', (backoff_seconds(attempts), error[:500], row_id, worker_id))
    return 'retry'


def reconcile_status(conn, idempotency_key: str | None, provider_message_id: str | None) -> None:
    """Provider status webhook: if our row is still 'sending' (crash before ACK), mark it sent to avoid a re-send."""
    if idempotency_key:
        conn.execute('''UPDATE telco.outbox SET state='sent',sent_at=coalesce(sent_at,now()),provider_message_id=coalesce(provider_message_id,%s),
            lease_owner=NULL,lease_expires_at=NULL WHERE idempotency_key=%s AND state IN ('sending','pending')''',
                     (provider_message_id, idempotency_key))


class SendError(Exception):
    def __init__(self, message: str, permanent: bool):
        super().__init__(message)
        self.permanent = permanent


class WhatsAppSender:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.s = settings
        self.client = client or httpx.Client(timeout=10)

    def _guard(self, recipient_hmac: str):
        mode = self.s.wa_send_mode
        if mode == 'disabled':
            raise SendError('WA_SEND_MODE=disabled', permanent=False)
        host = urlparse(self.s.wa_graph_base_url).hostname or ''
        if mode == 'mock' and host not in LOCAL_HOSTS:
            raise SendError('mock mode requires a local mock Graph host', permanent=True)
        if mode == 'live':
            allow = {h.strip() for h in self.s.wa_live_allowlist_hmacs.split(',') if h.strip()}
            if recipient_hmac not in allow:
                raise SendError('recipient not in live allowlist', permanent=True)

    def send(self, to: str, recipient_hmac: str, payload: dict, idempotency_key: str) -> str:
        self._guard(recipient_hmac)
        if not self.s.wa_phone_number_id or not self.s.wa_access_token:
            raise SendError('WhatsApp sender not configured', permanent=False)
        body = {'messaging_product': 'whatsapp', 'recipient_type': 'individual', 'to': to, 'type': 'text',
                'text': {'body': payload['text'][:4096], 'preview_url': bool(payload.get('preview_url'))},
                'biz_opaque_callback_data': idempotency_key}
        url = f"{self.s.wa_graph_base_url.rstrip('/')}/{self.s.wa_graph_version}/{self.s.wa_phone_number_id}/messages"
        try:
            r = self.client.post(url, json=body, headers={'Authorization': f'Bearer {self.s.wa_access_token.get_secret_value()}'})
        except httpx.HTTPError as exc:
            raise SendError(f'transport: {type(exc).__name__}', permanent=False)
        if r.status_code == 429 or r.status_code >= 500:
            raise SendError(f'provider {r.status_code}', permanent=False)
        if r.status_code >= 400:
            raise SendError(f'provider {r.status_code}', permanent=True)
        return r.json()['messages'][0]['id']


class N8nDispatcher:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.s = settings
        self.client = client or httpx.Client(timeout=10)

    def send(self, payload: dict) -> str:
        if not self.s.n8n_dispatch_url or not self.s.n8n_dispatch_secret:
            raise SendError('n8n dispatch not configured', permanent=False)
        raw = json.dumps(payload, separators=(',', ':')).encode()
        sig = hmac.new(self.s.n8n_dispatch_secret.get_secret_value().encode(), raw, hashlib.sha256).hexdigest()
        try:
            r = self.client.post(self.s.n8n_dispatch_url, content=raw,
                                 # Header token is checked by the n8n Webhook node (Header Auth credential);
                                 # the HMAC lets receivers verify body integrity as well.
                                 headers={'Content-Type': 'application/json', 'X-Telco-Signature': 'sha256=' + sig,
                                          'X-Telco-Dispatch-Token': self.s.n8n_dispatch_secret.get_secret_value()})
        except httpx.HTTPError as exc:
            raise SendError(f'transport: {type(exc).__name__}', permanent=False)
        if r.status_code >= 500 or r.status_code == 429:
            raise SendError(f'n8n {r.status_code}', permanent=False)
        if r.status_code >= 400:
            raise SendError(f'n8n {r.status_code}', permanent=True)
        return 'n8n:' + r.headers.get('x-n8n-execution-id', 'accepted')


def dispatch_batch(db, settings: Settings, wa: WhatsAppSender, n8n: N8nDispatcher, limit: int, lease_seconds: int,
                   worker_id: str, crash_after_send=None) -> dict:
    """Claim (commit) -> send (no DB tx open) -> ack/fail (commit). `crash_after_send` is a test hook."""
    rows = db.write(lambda conn: claim(conn, limit, lease_seconds, worker_id))
    vault = IdentityVault(settings)
    stats = {'claimed': len(rows), 'sent': 0, 'retry': 0, 'dead_letter': 0}
    for row in rows:
        try:
            if row['kind'] == 'n8n_dispatch':
                provider_id = n8n.send(row['payload'])
            else:
                rec = db.write_identity(lambda conn: vault.recipient_for(conn, row['principal_id'], (row['payload'] or {}).get('reply_to')))
                if rec is None:
                    raise SendError('no active WhatsApp identity for principal', permanent=True)
                provider_id = wa.send(rec[0], rec[1], row['payload'], row['idempotency_key'])
            if crash_after_send:
                crash_after_send(row)
            if db.write(lambda conn: ack(conn, row['id'], worker_id, provider_id)):
                stats['sent'] += 1
        except SendError as exc:
            outcome = db.write(lambda conn: fail(conn, row['id'], worker_id, str(exc), row['attempts'], row['max_attempts'], exc.permanent))
            stats[outcome] += 1
            log.info('outbox %s %s attempt=%s corr=%s', row['id'], outcome, row['attempts'], row['correlation_id'])
    return stats


def stats(conn) -> dict:
    rows = conn.execute('''SELECT state,count(*),coalesce(extract(epoch FROM now()-min(created_at)) FILTER(WHERE state IN ('pending','sending')),0)
        FROM telco.outbox GROUP BY state''').fetchall()
    return {r[0]: {'count': r[1], 'oldest_age_s': float(r[2] or 0)} for r in rows}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ['enqueue', 'claim', 'ack', 'fail', 'dispatch_batch', 'WhatsAppSender', 'N8nDispatcher', 'SendError', 'errors',
           'timedelta']

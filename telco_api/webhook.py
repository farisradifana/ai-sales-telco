"""WhatsApp Cloud API ingress. Signature over the RAW body is verified before parsing; invalid signatures never reach
storage or the n8n queue. Verified events + their n8n dispatch outbox row commit in ONE transaction, then we ACK."""
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from psycopg.types.json import Jsonb

from . import errors, outbox
from .identity import IdentityVault
from .settings import Settings


class _Loose(BaseModel):
    model_config = ConfigDict(extra='allow')


class WAText(_Loose):
    body: str = Field(max_length=4096)


class WAChoice(_Loose):
    id: str = Field(max_length=200)
    title: str | None = Field(None, max_length=100)


class WAInteractive(_Loose):
    type: str
    list_reply: WAChoice | None = None
    button_reply: WAChoice | None = None


class WAMessage(_Loose):
    id: str = Field(min_length=6, max_length=160)
    from_: str = Field(alias='from', pattern=r'^\d{6,20}$')
    timestamp: str = Field(pattern=r'^\d{9,11}$')
    type: str
    text: WAText | None = None
    interactive: WAInteractive | None = None


class WAStatus(_Loose):
    id: str
    status: str
    biz_opaque_callback_data: str | None = None


class WAValue(_Loose):
    messaging_product: Literal['whatsapp']
    messages: list[WAMessage] = Field(default_factory=list, max_length=50)
    statuses: list[WAStatus] = Field(default_factory=list, max_length=200)


class WAChange(_Loose):
    field: str
    value: WAValue


class WAEntry(_Loose):
    id: str
    changes: list[WAChange] = Field(max_length=50)


class WAPayload(_Loose):
    object: Literal['whatsapp_business_account']
    entry: list[WAEntry] = Field(max_length=50)


def verify_challenge(s: Settings, mode: str | None, token: str | None, challenge: str | None) -> str:
    if mode != 'subscribe' or not token or not challenge or not s.wa_verify_token:
        raise errors.AppError(403, 'VERIFY_FAILED', 'Verifikasi webhook gagal.')
    if not hmac.compare_digest(token.encode(), s.wa_verify_token.get_secret_value().encode()):
        raise errors.AppError(403, 'VERIFY_FAILED', 'Verifikasi webhook gagal.')
    if len(challenge) > 128:
        raise errors.bad_request('INVALID_INPUT', 'Challenge terlalu panjang.')
    return challenge


def verify_signature(s: Settings, raw: bytes, header: str | None) -> None:
    if not s.wa_app_secret:
        raise errors.unavailable('WEBHOOK_NOT_CONFIGURED', 'App secret belum dikonfigurasi.')
    if not header or not header.startswith('sha256='):
        raise errors.AppError(401, 'INVALID_SIGNATURE', 'Signature tidak valid.')
    expected = hmac.new(s.wa_app_secret.get_secret_value().encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header[7:].strip().lower()):
        raise errors.AppError(401, 'INVALID_SIGNATURE', 'Signature tidak valid.')


def normalize(msg: WAMessage) -> dict:
    """Message content WITHOUT the sender phone number. Non-text types are recorded but not interpreted."""
    text = msg.text.body if msg.text else None
    choice = msg.interactive and (msg.interactive.list_reply or msg.interactive.button_reply)
    if choice:  # a tapped quick-command row: its id is the command text (e.g. "sell in"), parsed like typed text
        text = choice.id
    return {'type': msg.type, 'text': text, **({'choice_title': choice.title} if choice else {}),
            'provider_ts': datetime.fromtimestamp(int(msg.timestamp), timezone.utc).isoformat()}


def ingest(db, s: Settings, raw: bytes, signature: str | None, correlation_id: str) -> dict:
    if len(raw) > s.max_webhook_bytes:
        raise errors.AppError(413, 'PAYLOAD_TOO_LARGE', 'Payload terlalu besar.')
    verify_signature(s, raw, signature)
    try:
        payload = WAPayload.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        raise errors.bad_request('INVALID_PAYLOAD', 'Skema payload WhatsApp tidak valid.')
    vault = IdentityVault(s)
    msgs = [m for e in payload.entry for c in e.changes if c.field == 'messages' for m in c.value.messages]
    statuses = [st for e in payload.entry for c in e.changes if c.field == 'messages' for st in c.value.statuses]

    # Resolve identity in the identity schema (separate role) before the business transaction.
    resolved = {}
    for m in msgs:
        resolved[m.id] = db.write_identity(lambda conn: vault.resolve(conn, m.from_))

    def tx(conn):
        out = {'accepted': 0, 'duplicate': 0, 'statuses': 0, 'unregistered': 0}
        for m in msgs:
            canonical = json.dumps(m.model_dump(by_alias=True, mode='json'), sort_keys=True, separators=(',', ':'))
            h = hashlib.sha256(canonical.encode()).hexdigest()
            principal, ident_ref = resolved[m.id]
            norm = normalize(m)
            row = conn.execute('''INSERT INTO telco.inbound_event(provider,message_id,principal_id,payload_hash,correlation_id,message,
                identity_ref,provider_ts) VALUES('whatsapp',%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (provider,message_id) DO NOTHING
                RETURNING message_id''', (m.id, principal or 'unregistered', h, correlation_id, Jsonb(norm), ident_ref,
                                          norm['provider_ts'])).fetchone()
            if row is None:
                prior = conn.execute("SELECT payload_hash FROM telco.inbound_event WHERE provider='whatsapp' AND message_id=%s",
                                     (m.id,)).fetchone()[0]
                if prior != h:
                    raise errors.conflict('DUPLICATE_PAYLOAD_MISMATCH', 'message_id sama dengan isi berbeda.')
                out['duplicate'] += 1
                continue
            if principal is None:
                # Unknown sender: stored for audit, never dispatched, never replied to.
                conn.execute("UPDATE telco.inbound_event SET state='completed',completed_at=now(),result=%s WHERE provider='whatsapp' AND message_id=%s",
                             (Jsonb({'status': 'unregistered_sender'}), m.id))
                out['unregistered'] += 1
                continue
            outbox.enqueue(conn, principal, 'n8n_dispatch', {'provider': 'whatsapp', 'message_id': m.id, 'correlation_id': correlation_id},
                           f'dispatch:whatsapp:{m.id}', correlation_id, max_attempts=12)
            out['accepted'] += 1
        for st in statuses:
            if st.status in ('sent', 'delivered', 'read'):
                outbox.reconcile_status(conn, st.biz_opaque_callback_data, st.id)
            out['statuses'] += 1
        return out

    return db.write(tx)

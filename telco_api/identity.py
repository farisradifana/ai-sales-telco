"""WhatsApp identity mapping (PII). Stored only in telco_identity.wa_identity as AES-256-GCM ciphertext plus an
HMAC-SHA256 blind index. The raw wa_id is never logged, never stored in telco.* tables and never sent to n8n/Hermes.
"""
import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import errors
from .settings import Settings


def _key(secret) -> bytes:
    if not secret:
        raise errors.unavailable('IDENTITY_NOT_CONFIGURED', 'Kunci identitas belum dikonfigurasi.')
    raw = base64.b64decode(secret.get_secret_value())
    if len(raw) != 32:
        raise errors.unavailable('IDENTITY_NOT_CONFIGURED', 'Kunci identitas harus 32 byte base64.')
    return raw


class IdentityVault:
    def __init__(self, settings: Settings):
        self.s = settings

    def blind_index(self, wa_id: str) -> str:
        return hmac.new(_key(self.s.identity_hmac_key), wa_id.strip().encode(), hashlib.sha256).hexdigest()

    def encrypt(self, wa_id: str) -> str:
        nonce = os.urandom(12)
        ct = AESGCM(_key(self.s.identity_enc_key)).encrypt(nonce, wa_id.encode(), b'wa_identity:v1')
        return base64.b64encode(nonce + ct).decode()

    def decrypt(self, blob: str) -> str:
        raw = base64.b64decode(blob)
        return AESGCM(_key(self.s.identity_enc_key)).decrypt(raw[:12], raw[12:], b'wa_identity:v1').decode()

    def resolve(self, conn, wa_id: str) -> tuple[str | None, str]:
        """Return (principal_id or None, identity_ref). identity_ref is the blind index prefix for correlation."""
        h = self.blind_index(wa_id)
        row = conn.execute('''UPDATE telco_identity.wa_identity SET last_seen_at=now()
                              WHERE wa_hmac=%s AND active RETURNING principal_id''', (h,)).fetchone()
        return (row[0] if row else None), h[:16]

    def recipient_for(self, conn, principal_id: str) -> tuple[str, str] | None:
        row = conn.execute('''SELECT wa_ciphertext,wa_hmac FROM telco_identity.wa_identity
                              WHERE principal_id=%s AND active ORDER BY id DESC LIMIT 1''', (principal_id,)).fetchone()
        if not row:
            return None
        return self.decrypt(row[0]), row[1]

    def register(self, conn, wa_id: str, principal_id: str) -> None:
        digits = wa_id.strip()
        conn.execute('''INSERT INTO telco_identity.wa_identity(wa_hmac,wa_ciphertext,key_version,principal_id)
                        VALUES(%s,%s,%s,%s) ON CONFLICT (wa_hmac) DO UPDATE SET principal_id=EXCLUDED.principal_id,active=true''',
                     (self.blind_index(digits), self.encrypt(digits), self.s.identity_key_version, principal_id))

"""Authentication: JWT/session for humans, constant-time service tokens for n8n/MCP, one-turn agent tickets.

The principal is ALWAYS loaded from app_principal using a server-verified subject. Request bodies never carry
principal_id, sales_id or role.
"""
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Request

from . import errors
from .settings import Settings


@dataclass(frozen=True)
class Principal:
    principal_id: str
    role: str
    sales_id: int | None
    display_name: str
    auth_method: str  # jwt | whatsapp | agent_ticket

    @property
    def is_supervisor(self) -> bool:
        return self.role in ('supervisor', 'area_manager', 'admin')


def load_principal(conn, principal_id: str, auth_method: str) -> Principal:
    row = conn.execute('SELECT principal_id,role,sales_id,display_name FROM telco.app_principal WHERE principal_id=%s AND active',
                       (principal_id,)).fetchone()
    if not row:
        raise errors.unauthenticated('Principal tidak aktif atau tidak dikenal.')
    return Principal(row[0], row[1], row[2], row[3], auth_method)


class TokenService:
    def __init__(self, settings: Settings):
        self.s = settings
        self._jwks = None
        if settings.jwt_jwks_url:
            self._jwks = jwt.PyJWKClient(settings.jwt_jwks_url, cache_keys=True)

    def issue_session(self, principal_id: str) -> str:
        """Local/staging session token. Production deployments put an OIDC IdP in front (JWKS mode)."""
        if not self.s.jwt_secret:
            raise errors.unavailable('AUTH_NOT_CONFIGURED', 'JWT secret belum dikonfigurasi.')
        now = datetime.now(timezone.utc)
        claims = {'sub': principal_id, 'iss': self.s.jwt_issuer, 'aud': self.s.jwt_audience,
                  'iat': now, 'nbf': now, 'exp': now + timedelta(minutes=self.s.session_ttl_minutes),
                  'jti': secrets.token_hex(8)}
        return jwt.encode(claims, self.s.jwt_secret.get_secret_value(), algorithm='HS256')

    def verify(self, token: str) -> str:
        try:
            if self._jwks:
                key = self._jwks.get_signing_key_from_jwt(token).key
                claims = jwt.decode(token, key, algorithms=['RS256', 'ES256'], audience=self.s.jwt_audience,
                                    issuer=self.s.jwt_issuer, options={'require': ['exp', 'sub', 'iss', 'aud']})
            elif self.s.jwt_secret:
                claims = jwt.decode(token, self.s.jwt_secret.get_secret_value(), algorithms=['HS256'],
                                    audience=self.s.jwt_audience, issuer=self.s.jwt_issuer,
                                    options={'require': ['exp', 'sub', 'iss', 'aud']})
            else:
                raise errors.unavailable('AUTH_NOT_CONFIGURED', 'Verifikasi token belum dikonfigurasi.')
        except jwt.PyJWTError:
            raise errors.unauthenticated('Token tidak valid atau kedaluwarsa.')
        return str(claims['sub'])


def bearer(request: Request) -> str | None:
    h = request.headers.get('authorization', '')
    if h.lower().startswith('bearer '):
        return h[7:].strip() or None
    return None


def check_service_token(request: Request, expected) -> None:
    token = bearer(request)
    if not expected or not token or not hmac.compare_digest(token.encode(), expected.get_secret_value().encode()):
        raise errors.unauthenticated('Service token tidak valid.')


def ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


def new_ticket() -> str:
    return 'tkt_' + secrets.token_urlsafe(24)


class RateLimiter:
    """Per-principal token bucket (process-local). Multi-replica deployments should front this with the gateway."""

    def __init__(self, per_minute: int):
        self.rate = per_minute / 60.0
        self.capacity = float(per_minute)
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self.lock:
            tokens, ts = self.buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - ts) * self.rate)
            if tokens < 1:
                self.buckets[key] = (tokens, now)
                raise errors.rate_limited()
            self.buckets[key] = (tokens - 1, now)


def sign(key: bytes, *parts: str) -> str:
    return hmac.new(key, '|'.join(parts).encode(), hashlib.sha256).hexdigest()

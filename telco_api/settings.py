"""Configuration from environment / secret manager. No secret has a usable default."""
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra='ignore', case_sensitive=False)

    app_env: Literal['local', 'test', 'staging', 'production'] = 'local'
    log_level: str = 'INFO'

    # Separate least-privilege LOGIN roles (sql/security*.sql). Never the owner/superuser.
    reader_database_url: SecretStr
    writer_database_url: SecretStr
    identity_database_url: SecretStr | None = None
    statement_timeout_ms: int = Field(15000, ge=100, le=120000)
    lock_timeout_ms: int = Field(3000, ge=50, le=60000)
    pool_max: int = Field(8, ge=1, le=64)
    tx_retries: int = Field(3, ge=0, le=6)

    # Human auth: JWT verified server-side (HS256 secret for local/staging IdP shim, or RS256 via JWKS).
    jwt_secret: SecretStr | None = None
    jwt_jwks_url: str | None = None
    jwt_issuer: str = 'telco-sales-ai'
    jwt_audience: str = 'telco-api'
    session_cookie: str = 'telco_session'
    session_ttl_minutes: int = Field(480, ge=5, le=1440)
    dev_login_enabled: bool = False
    cors_origins: str = ''

    # Service-to-service bearer tokens (n8n, MCP bridge). Compared in constant time.
    n8n_service_token: SecretStr | None = None
    mcp_service_token: SecretStr | None = None

    # WhatsApp Cloud API.
    wa_app_secret: SecretStr | None = None
    wa_verify_token: SecretStr | None = None
    wa_graph_base_url: str = 'https://graph.facebook.com'
    wa_graph_version: str = 'v23.0'
    wa_phone_number_id: str | None = None
    wa_access_token: SecretStr | None = None
    # disabled: never sends; mock: sends only to wa_graph_base_url (must be a local mock); live: requires allowlist.
    wa_send_mode: Literal['disabled', 'mock', 'live'] = 'disabled'
    wa_live_allowlist_hmacs: str = ''
    max_webhook_bytes: int = 262144
    # Verified inbound events are dispatched to n8n's internal webhook (queue mode) with an HMAC header.
    n8n_dispatch_url: str | None = None
    n8n_dispatch_secret: SecretStr | None = None

    # PII mapping encryption (AES-256-GCM key + HMAC blind-index key), base64 32 bytes each.
    identity_enc_key: SecretStr | None = None
    identity_hmac_key: SecretStr | None = None
    identity_key_version: int = 1

    # Agent stack. Pinned + configurable; capability-probed at runtime.
    hermes_base_url: str | None = None
    hermes_api_key: SecretStr | None = None
    hermes_model: str = 'hermes-agent'
    hermes_allowed_toolsets: str = ''
    router_base_url: str | None = None
    router_api_key: SecretStr | None = None
    model_id: str | None = None
    fallback_model_id: str | None = None
    agent_timeout_s: float = Field(45, ge=1, le=300)
    circuit_failures: int = Field(3, ge=1, le=20)
    circuit_reset_s: float = Field(60, ge=1, le=3600)
    agent_ticket_ttl_s: int = Field(180, ge=10, le=900)

    # Signed chart URLs.
    chart_signing_key: SecretStr | None = None
    public_base_url: str = 'http://127.0.0.1:8000'
    chart_url_ttl_s: int = Field(900, ge=60, le=86400)

    # Posting policy: no backdate before watermark / grace / latest ledger event.
    posting_grace_hours: int = Field(48, ge=1, le=24 * 14)
    future_skew_seconds: int = Field(300, ge=0, le=3600)
    draft_ttl_minutes: int = Field(30, ge=1, le=1440)
    context_ttl_hours: int = Field(24, ge=1, le=168)
    rate_limit_per_minute: int = Field(60, ge=1, le=10000)


@lru_cache
def get_settings() -> Settings:
    return Settings()

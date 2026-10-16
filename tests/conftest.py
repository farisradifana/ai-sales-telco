"""Shared fixtures. Server tests clone a fully loaded TEMPLATE database into a disposable telco_test_clone_<hex>
database (so the 500k reference load is never mutated) and drop ONLY that clone at session end.

Required env for server tests (see scripts/devenv.sh): TEST_ADMIN_DATABASE_URL (owner on the disposable cluster, any db),
TEST_TEMPLATE_DB (default telco_test_full), TELCO_API_*_PASSWORD, TELCO_OPS_PASSWORD and the secrets in .local/dev.env.
"""
import os
import uuid
from urllib.parse import urlparse, urlunparse

import pytest

ADMIN = os.environ.get('TEST_ADMIN_DATABASE_URL', '')


def _with_db(url: str, db: str, user: str | None = None, password: str | None = None) -> str:
    u = urlparse(url)
    netloc = u.netloc
    if user:
        host = netloc.split('@', 1)[1]
        netloc = f'{user}:{password}@{host}'
    return urlunparse(u._replace(netloc=netloc, path='/' + db))


@pytest.fixture(scope='session')
def clone_db():
    if not ADMIN:
        pytest.skip('TEST_ADMIN_DATABASE_URL absent: real PostgreSQL tests skipped (NOT passed)')
    import psycopg
    template = os.environ.get('TEST_TEMPLATE_DB', 'telco_test_full')
    if not template.startswith('telco_test_'):
        raise RuntimeError('template must be a disposable telco_test_* database')
    name = 'telco_test_clone_' + uuid.uuid4().hex[:10]
    maint = _with_db(ADMIN, 'postgres')
    with psycopg.connect(maint, autocommit=True) as c:
        c.execute(f'CREATE DATABASE {name} TEMPLATE {template} STRATEGY FILE_COPY')
    import telco.db as tdb
    with psycopg.connect(_with_db(ADMIN, name)) as c:
        tdb.migrate(c)  # migrations newer than the template (e.g. 009_area_scope) go to the clone only, never the template
    env = os.environ
    urls = {
        'name': name,
        'admin': _with_db(ADMIN, name),
        'reader': _with_db(ADMIN, name, 'telco_api_reader', env['TELCO_API_READER_PASSWORD']),
        'writer': _with_db(ADMIN, name, 'telco_api_writer', env['TELCO_API_WRITER_PASSWORD']),
        'identity': _with_db(ADMIN, name, 'telco_api_identity', env['TELCO_API_IDENTITY_PASSWORD']),
        'ops': _with_db(ADMIN, name, 'telco_ops_login', env['TELCO_OPS_PASSWORD']),
    }
    yield urls
    if os.environ.get('TELCO_KEEP_CLONE') != '1':
        with psycopg.connect(maint, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')  # only the clone created above


@pytest.fixture(scope='session')
def settings(clone_db):
    from telco_api.settings import Settings
    e = os.environ
    return Settings(app_env='test', reader_database_url=clone_db['reader'], writer_database_url=clone_db['writer'],
                    identity_database_url=clone_db['identity'], jwt_secret=e['JWT_SECRET'], dev_login_enabled=True,
                    n8n_service_token=e['N8N_SERVICE_TOKEN'], mcp_service_token=e['MCP_SERVICE_TOKEN'],
                    wa_app_secret=e['WA_APP_SECRET'], wa_verify_token=e['WA_VERIFY_TOKEN'], wa_access_token=e['WA_ACCESS_TOKEN'],
                    wa_phone_number_id='000000000000000', wa_send_mode='mock', wa_graph_base_url='http://mock-wa',
                    identity_enc_key=e['IDENTITY_ENC_KEY'], identity_hmac_key=e['IDENTITY_HMAC_KEY'],
                    chart_signing_key=e['CHART_SIGNING_KEY'], n8n_dispatch_url='http://mock-n8n/webhook/telco-inbound',
                    n8n_dispatch_secret=e['N8N_DISPATCH_SECRET'], rate_limit_per_minute=10000,
                    hermes_base_url='http://mock-hermes', hermes_api_key=e['HERMES_API_KEY'],
                    router_base_url='http://mock-router', router_api_key=e['ROUTER_API_KEY'], fallback_model_id='mock-fallback-model')


@pytest.fixture(scope='session')
def db(settings):
    from telco_api.db import Database
    d = Database(settings)
    d.open()
    yield d
    d.close()


@pytest.fixture(scope='session')
def mocks():
    from tests.mocks import Mocks
    return Mocks()


@pytest.fixture(scope='session')
def app(settings, db, mocks):
    import httpx
    from fastapi.testclient import TestClient
    from telco_api.agent import HermesAdapter, Narrator, RouterFallback
    from telco_api.main import create_app
    from telco_api.outbox import N8nDispatcher, WhatsAppSender

    hermes = HermesAdapter(settings, client=httpx.Client(transport=httpx.MockTransport(mocks.hermes_handler)))
    router = RouterFallback(settings, db, client=httpx.Client(transport=httpx.MockTransport(mocks.router_handler)))
    narrator = Narrator(settings, db, hermes, router)
    wa = WhatsAppSender(settings, client=httpx.Client(transport=httpx.MockTransport(mocks.wa_handler)))
    n8n = N8nDispatcher(settings, client=httpx.Client(transport=httpx.MockTransport(mocks.n8n_handler)))
    application = create_app(settings, db, narrator, wa, n8n)
    application.state.mocks = mocks
    client = TestClient(application, raise_server_exceptions=False)
    client.__enter__()
    mocks.bind_api(client, settings)
    yield application
    client.__exit__(None, None, None)


@pytest.fixture
def login(app):
    from fastapi.testclient import TestClient

    def _login(principal_id: str):
        c = TestClient(app, raise_server_exceptions=False)
        r = c.post('/v1/auth/dev-login', json={'principal_id': principal_id})
        assert r.status_code == 200, r.text
        return c
    return _login


@pytest.fixture
def admin_conn(clone_db):
    import psycopg
    with psycopg.connect(clone_db['admin']) as c:
        yield c

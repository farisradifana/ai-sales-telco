"""Migration runner and loader guarantees on real PostgreSQL (disposable databases only)."""
import shutil
import threading
import uuid

import psycopg
import pytest

import telco.db as tdb
from tests.conftest import ADMIN, _with_db

pytestmark = pytest.mark.pg


@pytest.fixture
def empty_db():
    if not ADMIN:
        pytest.skip('TEST_ADMIN_DATABASE_URL absent')
    name = 'telco_test_mig_' + uuid.uuid4().hex[:8]
    maint = _with_db(ADMIN, 'postgres')
    with psycopg.connect(maint, autocommit=True) as c:
        c.execute(f'CREATE DATABASE {name}')
    yield _with_db(ADMIN, name)
    with psycopg.connect(maint, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')  # only the empty db this fixture created


def test_concurrent_migrate_runners_serialize(empty_db):
    errors_, barrier = [], threading.Barrier(2)

    def run():
        try:
            with psycopg.connect(empty_db) as c:
                barrier.wait(10)
                tdb.migrate(c)
        except Exception as exc:  # collect for assertion
            errors_.append(exc)
    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    with psycopg.connect(empty_db) as c:
        tdb.migrate(c)  # rerun is a no-op
        rows = c.execute('SELECT version FROM public.telco_migration ORDER BY 1').fetchall()
    files = sorted(p.name for p in (tdb.ROOT / 'sql').glob('[0-9][0-9][0-9]_*.sql'))
    assert [r[0] for r in rows] == files
    assert errors_ == []


def test_checksum_mismatch_rejected(empty_db, tmp_path, monkeypatch):
    with psycopg.connect(empty_db) as c:
        tdb.migrate(c)
    shutil.copytree(tdb.ROOT / 'sql', tmp_path / 'sql')
    f = tmp_path / 'sql' / '001_core.sql'
    f.write_text(f.read_text(encoding='utf-8') + '\n-- edited after apply\n', encoding='utf-8')
    monkeypatch.setattr(tdb, 'ROOT', tmp_path)
    with psycopg.connect(empty_db) as c:
        with pytest.raises(ValueError, match='checksum changed: 001_core.sql'):
            tdb.migrate(c)


def test_second_load_refused(clone_db):
    with psycopg.connect(clone_db['admin']) as c:
        with pytest.raises(ValueError, match='refusing duplicate'):
            tdb.load(c, tdb.ROOT / 'data' / 'full')


def test_app_roles_are_least_privilege(clone_db):
    with psycopg.connect(clone_db['reader']) as c:
        for stmt in ("INSERT INTO telco.sell_out(id) VALUES(1)", 'UPDATE telco.stock_balance SET quantity=0',
                     'SELECT * FROM telco_identity.wa_identity', 'SELECT * FROM telco.conversation_context', 'CREATE TABLE telco.x(i int)'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(stmt)
            c.rollback()
    with psycopg.connect(clone_db['writer']) as c:
        for stmt in ('UPDATE telco.stock_balance SET quantity=0', 'INSERT INTO telco.principal_grant VALUES(%s,%s,1)',
                     'SELECT * FROM telco_identity.wa_identity', 'CREATE TABLE telco.x(i int)', 'CREATE SCHEMA evil'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(stmt, ('sls-001', 'district') if '%s' in stmt else None)
            c.rollback()
        roles = c.execute("SELECT rolsuper,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=current_user").fetchone()
        assert roles == (False, False, False)

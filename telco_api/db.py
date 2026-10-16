"""Connection pools per least-privilege role, bounded timeouts, and whole-transaction retry (40001/40P01 only)."""
import logging
import random
import time
from typing import Callable, TypeVar

import psycopg
from psycopg import IsolationLevel, errors
from psycopg.types.json import set_json_loads
from psycopg_pool import ConnectionPool

from .errors import unavailable
from .money import json_loads
from .settings import Settings

log = logging.getLogger('telco.db')
T = TypeVar('T')
RETRYABLE = (errors.SerializationFailure, errors.DeadlockDetected)


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.reader = self._pool(settings.reader_database_url.get_secret_value(), 'telco-api-reader')
        self.writer = self._pool(settings.writer_database_url.get_secret_value(), 'telco-api-writer')
        ident = settings.identity_database_url or settings.writer_database_url
        self.identity = self._pool(ident.get_secret_value(), 'telco-api-identity', max_size=2)

    def _pool(self, url: str, app_name: str, max_size: int | None = None) -> ConnectionPool:
        s = self.settings

        def configure(conn: psycopg.Connection):
            set_json_loads(json_loads, conn)
            conn.autocommit = True
            conn.execute(f"SET statement_timeout = {int(s.statement_timeout_ms)}")
            conn.execute(f"SET lock_timeout = {int(s.lock_timeout_ms)}")
            conn.execute("SET idle_in_transaction_session_timeout = 60000")
            conn.execute("SET TIME ZONE 'UTC'")
            conn.autocommit = False

        return ConnectionPool(url, min_size=0, max_size=max_size or s.pool_max, open=False, configure=configure,
                              kwargs={'application_name': app_name, 'connect_timeout': 5}, timeout=10, name=app_name)

    def open(self):
        for p in (self.reader, self.writer, self.identity):
            p.open(wait=False)

    def close(self):
        for p in (self.reader, self.writer, self.identity):
            p.close()

    def run(self, pool: ConnectionPool, fn: Callable[[psycopg.Connection], T], *,
            isolation: IsolationLevel = IsolationLevel.READ_COMMITTED, read_only: bool = False) -> T:
        """Run fn inside ONE transaction; on serialization/deadlock, retry the whole transaction with jittered backoff."""
        attempts = self.settings.tx_retries + 1
        for attempt in range(attempts):
            try:
                with pool.connection() as conn:
                    conn.isolation_level = isolation
                    conn.read_only = read_only
                    try:
                        with conn.transaction():
                            return fn(conn)
                    finally:
                        conn.read_only = False
                        conn.isolation_level = None
            except RETRYABLE as exc:
                if attempt + 1 >= attempts:
                    log.warning('tx retries exhausted: %s', type(exc).__name__)
                    raise unavailable('DB_CONFLICT_RETRY_EXHAUSTED', 'Transaksi bentrok; silakan ulangi.')
                time.sleep(min(2.0, 0.05 * (2 ** attempt)) * (0.5 + random.random()))
            except errors.LockNotAvailable:
                raise unavailable('DB_LOCK_TIMEOUT', 'Data sedang dipakai proses lain; coba lagi.')
            except errors.QueryCanceled:
                raise unavailable('DB_STATEMENT_TIMEOUT', 'Kueri melebihi batas waktu.')
            except psycopg.OperationalError:
                raise unavailable('DB_UNAVAILABLE', 'Database sementara tidak tersedia.')
        raise AssertionError('unreachable')

    def read(self, fn: Callable[[psycopg.Connection], T]) -> T:
        # One consistent snapshot for every number in a single evidence object.
        return self.run(self.reader, fn, isolation=IsolationLevel.REPEATABLE_READ, read_only=True)

    def write(self, fn: Callable[[psycopg.Connection], T]) -> T:
        return self.run(self.writer, fn)

    def write_identity(self, fn: Callable[[psycopg.Connection], T]) -> T:
        return self.run(self.identity, fn)

    def ping(self) -> dict:
        out = {}
        for name, pool in (('reader', self.reader), ('writer', self.writer)):
            try:
                with pool.connection(timeout=3) as conn:
                    conn.execute('SELECT 1')
                    conn.rollback()
                out[name] = 'ok'
            except Exception as exc:  # readiness must not raise
                out[name] = 'error:' + type(exc).__name__
        return out

"""Admin provisioning CLI (never exposed as an HTTP endpoint).

  python -m telco_api.provision roles   # as admin/owner: LOGIN roles from env passwords + group membership
  python -m telco_api.provision demo    # as telco_ops: fictional principals, grants, price policy, WA test ids, targets, receipts

Demo WhatsApp ids start with "0": not valid E.164 (country codes never start with 0), so they cannot reach a real phone.
"""
import argparse
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg
from psycopg import sql

from .identity import IdentityVault
from .settings import get_settings

LOGIN_ROLES = {  # login role -> (group roles, password env var)
    'telco_api_reader': (['telco_reader'], 'TELCO_API_READER_PASSWORD'),
    'telco_api_writer': (['telco_writer'], 'TELCO_API_WRITER_PASSWORD'),
    'telco_api_identity': (['telco_identity_rw'], 'TELCO_API_IDENTITY_PASSWORD'),
    'telco_ops_login': (['telco_ops'], 'TELCO_OPS_PASSWORD'),
}
DEMO_WA_PREFIX = '0099'


def roles(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as c:
        for role, (groups, env) in LOGIN_ROLES.items():
            pw = os.environ.get(env)
            if not pw:
                raise SystemExit(f'{env} wajib di-set (secret manager)')
            exists = c.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone()
            verb = 'ALTER' if exists else 'CREATE'
            c.execute(sql.SQL('{} ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}').format(
                sql.SQL(verb), sql.Identifier(role), sql.Literal(pw)))
            for g in groups:
                c.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(g), sql.Identifier(role)))
            c.execute(sql.SQL('ALTER ROLE {} SET statement_timeout={}').format(sql.Identifier(role), sql.Literal('15s')))
            c.execute(sql.SQL('ALTER ROLE {} SET lock_timeout={}').format(sql.Identifier(role), sql.Literal('3s')))
    print('roles: OK')


def demo(url: str, today: date | None = None) -> dict:
    s = get_settings()
    vault = IdentityVault(s)
    today = today or datetime.now(timezone(timedelta(hours=7))).date()
    out = {}
    with psycopg.connect(url) as c:
        c.execute("SET application_name='telco-provision'")
        products = c.execute('SELECT id,sku,stock_unit,list_price FROM telco.product ORDER BY id').fetchall()
        # Price policy: sell-out at list price (max 10% discount PCS, 0% SALDO); sell-in = generator dealer price.
        for pid, sku, unit, lp in products:
            si_price = Decimal('1.0000') if unit == 'IDR' else (Decimal(int(lp)) * 9 // 10)
            for ch, price, pct in (('sell_out', lp, 10 if unit == 'PCS' else 0), ('sell_in', si_price, 0)):
                c.execute('''INSERT INTO telco.price_policy(product_id,channel,unit_price,max_discount_pct,valid_from,valid_to)
                    VALUES(%s,%s,%s,%s,'2025-01-01','2027-01-01') ON CONFLICT DO NOTHING''', (pid, ch, price, pct))
        sales = c.execute('SELECT id,code,district_id FROM telco.sales ORDER BY id').fetchall()
        for sid, code, did in sales:
            pid_ = f'sls-{sid:03d}'
            c.execute('''INSERT INTO telco.app_principal(principal_id,role,sales_id,display_name) VALUES(%s,'sales',%s,%s)
                ON CONFLICT DO NOTHING''', (pid_, sid, f'Sales Fiktif {sid:03d}'))
            for (oid,) in c.execute('SELECT DISTINCT outlet_id FROM telco.assignment WHERE sales_id=%s AND valid_to>%s', (sid, today)).fetchall():
                _grant(c, pid_, 'outlet', oid)
            c.execute('''INSERT INTO telco.notification_pref(principal_id,target_channel,daily_target_opt_in,send_hour_wib)
                VALUES(%s,'sell_in',%s,16) ON CONFLICT DO NOTHING''', (pid_, sid <= 2))
        regs = c.execute('SELECT id FROM telco.regency ORDER BY id').fetchall()
        for (rid,) in regs:
            pid_ = f'spv-r{rid:02d}'
            c.execute('''INSERT INTO telco.app_principal(principal_id,role,display_name) VALUES(%s,'supervisor',%s) ON CONFLICT DO NOTHING''',
                      (pid_, f'Supervisor Fiktif Kab {rid:02d}'))
            for (did,) in c.execute('SELECT id FROM telco.district WHERE regency_id=%s', (rid,)).fetchall():
                _grant(c, pid_, 'district', did)
        c.execute('''INSERT INTO telco.app_principal(principal_id,role,display_name) VALUES('am-all','area_manager','Area Manager Fiktif')
            ON CONFLICT DO NOTHING''')
        for (did,) in c.execute('SELECT id FROM telco.district').fetchall():
            _grant(c, 'am-all', 'district', did)
        c.execute('''INSERT INTO telco.app_principal(principal_id,role,display_name) VALUES('adm-ops','admin','Admin Ops Fiktif') ON CONFLICT DO NOTHING''')
        _grant(c, 'adm-ops', 'district', 1)
        # Outlet-only supervisor-like viewer: proves outlet grant does NOT imply district aggregate.
        c.execute('''INSERT INTO telco.app_principal(principal_id,role,display_name) VALUES('viewer-o1','supervisor','Viewer Outlet 1 Fiktif')
            ON CONFLICT DO NOTHING''')
        _grant(c, 'viewer-o1', 'outlet', 1)
        # Non-routable WhatsApp test identities.
        for i, (sid, _, _) in enumerate(sales, 1):
            vault.register(c, f'{DEMO_WA_PREFIX}{i:08d}', f'sls-{sid:03d}')
        for rid, in regs:
            vault.register(c, f'{DEMO_WA_PREFIX}9{rid:07d}', f'spv-r{rid:02d}')
        # Demo targets for the live posting window (dataset targets stop 2026-09-30). Fictional parameters.
        start = date(2026, 10, 1)
        for d in range(0, 92):
            day = start + timedelta(days=d)
            for sid, _, _ in sales:
                for ch, amt in (('sell_in', 3500000), ('sell_out', 4000000)):
                    c.execute('''INSERT INTO telco.sales_target(sales_id,target_date,channel,revenue_idr,rationale) VALUES(%s,%s,%s,%s,
                        'Fictional demo planning target; validate with business owner') ON CONFLICT DO NOTHING''', (sid, day, ch, amt))
        # Evidenced demo receipts (opening stock) for each sales holder; idempotent by external_key.
        now = datetime.now(timezone.utc)
        for sid, _, _ in sales:
            acct = c.execute('SELECT id FROM telco.stock_account WHERE sales_id=%s', (sid,)).fetchone()[0]
            for pid, sku, unit, _ in products:
                key = f'DEMO-SUP-{today.isoformat()}-{sid}-{pid}'
                qty = 2000000 if unit == 'IDR' else 200
                c.execute('''INSERT INTO telco.supply(id,external_key,occurred_at,account_id,product_id,quantity,reason)
                    SELECT nextval('telco.supply_api_id'),%s,%s,%s,%s,%s,'Synthetic demo opening stock (provision demo)'
                    WHERE NOT EXISTS(SELECT 1 FROM telco.supply WHERE external_key=%s)''', (key, now, acct, pid, qty, key))
        c.commit()
        out = {'principals': c.execute('SELECT count(*) FROM telco.app_principal').fetchone()[0],
               'grants': c.execute('SELECT count(*) FROM telco.principal_grant').fetchone()[0],
               'wa_identities': c.execute('SELECT count(*) FROM telco_identity.wa_identity').fetchone()[0]}
    print('demo:', out)
    return out


def _grant(c, principal: str, scope: str, entity: int) -> None:
    r = c.execute('INSERT INTO telco.principal_grant(principal_id,scope_type,entity_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1',
                  (principal, scope, entity)).fetchone()
    if r:
        c.execute("INSERT INTO telco.grant_audit(principal_id,scope_type,entity_id,action,actor,reason) VALUES(%s,%s,%s,'grant','provision','demo')",
                  (principal, scope, entity))


def revoke(url: str, principal: str, scope: str, entity: int, actor: str, reason: str) -> None:
    with psycopg.connect(url) as c:
        c.execute('DELETE FROM telco.principal_grant WHERE principal_id=%s AND scope_type=%s AND entity_id=%s', (principal, scope, entity))
        c.execute("INSERT INTO telco.grant_audit(principal_id,scope_type,entity_id,action,actor,reason) VALUES(%s,%s,%s,'revoke',%s,%s)",
                  (principal, scope, entity, actor, reason))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', choices=['roles', 'demo', 'revoke'])
    ap.add_argument('--principal'), ap.add_argument('--scope'), ap.add_argument('--entity', type=int)
    ap.add_argument('--reason', default='admin revoke')
    a = ap.parse_args()
    if a.action == 'roles':
        roles(os.environ['ADMIN_DATABASE_URL'])
    elif a.action == 'demo':
        demo(os.environ['OPS_DATABASE_URL'])
    else:
        revoke(os.environ['OPS_DATABASE_URL'], a.principal, a.scope, a.entity, 'cli', a.reason)


if __name__ == '__main__':
    main()

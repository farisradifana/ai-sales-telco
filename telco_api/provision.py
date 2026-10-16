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
    'telco_agent_memory_login': (['telco_agent_memory'], 'TELCO_AGENT_MEMORY_PASSWORD'),  # n8n Postgres Chat Memory
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


ROLE_BY_LEVEL = {'sales': 'sales', 'territory_manager': 'supervisor', 'head_of_branch': 'supervisor',
                 'head_of_sales': 'area_manager', 'head_of_region': 'area_manager'}


def principal_id_for(code: str) -> str:
    # HOR-35 -> hor-35, HOS-EAST -> hos-east, HOB-35.19 -> hob-35.19, TM-35.19-01 -> tm-35.19-01, SLS-35.19.01 -> sls-35.19.01
    return code.lower()


def demo(url: str, today: date | None = None) -> dict:
    """Principals and grants derived from the org hierarchy (telco.org_position / v_org_coverage). Fictional persons."""
    s = get_settings()
    vault = IdentityVault(s)
    today = today or datetime.now(timezone(timedelta(hours=7))).date()
    with psycopg.connect(url) as c:
        c.execute("SET application_name='telco-provision'")
        products = c.execute('SELECT id,sku,stock_unit,list_price FROM telco.product ORDER BY id').fetchall()
        # Price policy: sell-out at list price (max 10% discount PCS, 0% SALDO); sell-in = generator dealer price.
        for pid, sku, unit, lp in products:
            si_price = Decimal('1.0000') if unit == 'IDR' else (Decimal(int(lp)) * 9 // 10)
            for ch, price, pct in (('sell_out', lp, 10 if unit == 'PCS' else 0), ('sell_in', si_price, 0)):
                c.execute('''INSERT INTO telco.price_policy(product_id,channel,unit_price,max_discount_pct,valid_from,valid_to)
                    VALUES(%s,%s,%s,%s,'2025-01-01','2027-01-01') ON CONFLICT DO NOTHING''', (pid, ch, price, pct))
        positions = c.execute('SELECT id,code,level,title,sales_id FROM telco.org_position ORDER BY id').fetchall()
        for pos_id, code, level, title, sales_id in positions:
            pid_ = principal_id_for(code)
            c.execute('''INSERT INTO telco.app_principal(principal_id,role,sales_id,display_name,org_position_id) VALUES(%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING''', (pid_, ROLE_BY_LEVEL[level], sales_id, title, pos_id))
            for (did,) in c.execute('SELECT district_id FROM telco.v_org_coverage WHERE position_id=%s ORDER BY 1', (pos_id,)).fetchall():
                _grant(c, pid_, 'district', did)
        c.execute('''INSERT INTO telco.app_principal(principal_id,role,display_name) VALUES('adm-ops','admin','Admin Ops Fiktif') ON CONFLICT DO NOTHING''')
        _grant(c, 'adm-ops', 'district', 1)
        # Outlet-only viewer: proves an outlet grant does NOT imply the district aggregate.
        c.execute('''INSERT INTO telco.app_principal(principal_id,role,display_name) VALUES('viewer-o1','supervisor','Viewer Outlet 1 Fiktif')
            ON CONFLICT DO NOTHING''')
        _grant(c, 'viewer-o1', 'outlet', 1)
        active_sales = c.execute('''SELECT a.sales_id,count(*) FROM telco.assignment a WHERE a.valid_to>%s GROUP BY 1 ORDER BY 1''',
                                 (today,)).fetchall()
        for i, (sid, _) in enumerate(active_sales):
            c.execute('''INSERT INTO telco.notification_pref(principal_id,target_channel,daily_target_opt_in,send_hour_wib)
                SELECT principal_id,'sell_in',%s,16 FROM telco.app_principal WHERE sales_id=%s ON CONFLICT DO NOTHING''', (i < 2, sid))
        # Non-routable WhatsApp test identities for every principal ("0099" + 8 digits, never valid E.164).
        for i, (pid_,) in enumerate(c.execute('SELECT principal_id FROM telco.app_principal ORDER BY org_position_id NULLS LAST,principal_id').fetchall(), 1):
            vault.register(c, f'{DEMO_WA_PREFIX}{i:08d}', pid_)
        # Demo targets for the live posting window (dataset targets stop 2026-09-30). Fictional, per assigned outlet.
        start = date(2026, 10, 1)
        for d in range(0, 92):
            day = start + timedelta(days=d)
            for sid, n_outlets in active_sales:
                for ch, per in (('sell_in', 350000), ('sell_out', 400000)):
                    c.execute('''INSERT INTO telco.sales_target(sales_id,target_date,channel,revenue_idr,rationale) VALUES(%s,%s,%s,%s,
                        'Fictional demo planning target per assigned outlet; validate with business owner') ON CONFLICT DO NOTHING''',
                              (sid, day, ch, per * n_outlets))
        # Evidenced demo receipts (opening stock) for sales holders that cover outlets; idempotent by external_key.
        now = datetime.now(timezone.utc)
        for sid, _ in active_sales:
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
               'wa_identities': c.execute('SELECT count(*) FROM telco_identity.wa_identity').fetchone()[0],
               'by_role': dict(c.execute('SELECT role,count(*) FROM telco.app_principal GROUP BY 1').fetchall())}
    print('demo:', out)
    return out


def _grant(c, principal: str, scope: str, entity: int) -> None:
    r = c.execute('INSERT INTO telco.principal_grant(principal_id,scope_type,entity_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1',
                  (principal, scope, entity)).fetchone()
    if r:
        c.execute("INSERT INTO telco.grant_audit(principal_id,scope_type,entity_id,action,actor,reason) VALUES(%s,%s,%s,'grant','provision','demo')",
                  (principal, scope, entity))


def recap_bot(url: str) -> None:
    """Read-only reporting principal for the n8n recap workflow: district grants for the whole province, no sales role."""
    with psycopg.connect(url) as c:
        c.execute('''INSERT INTO telco.app_principal(principal_id,role,display_name) VALUES('svc-recap','supervisor','Bot Rekap Spreadsheet')
            ON CONFLICT DO NOTHING''')
        for (did,) in c.execute('SELECT id FROM telco.district').fetchall():
            _grant(c, 'svc-recap', 'district', did)
        c.commit()
    print('recap-bot: OK')


def mint_jwt(principal: str, days: int, out: str | None) -> None:
    """Long-lived service JWT for an n8n credential (max 30 days; rotate before expiry). Written to a file, never printed."""
    import jwt
    import secrets
    from pathlib import Path
    s = get_settings()
    if not 1 <= days <= 30:
        raise SystemExit('days must be 1..30')
    now = datetime.now(timezone.utc)
    tok = jwt.encode({'sub': principal, 'iss': s.jwt_issuer, 'aud': s.jwt_audience, 'iat': now, 'nbf': now,
                      'exp': now + timedelta(days=days), 'jti': secrets.token_hex(8)}, s.jwt_secret.get_secret_value(), algorithm='HS256')
    if out:
        Path(out).write_text(tok, encoding='utf-8')
        print(f'jwt for {principal} written to {out} (expires in {days} days)')
    else:
        raise SystemExit('use --out <file>; tokens are not printed')


def revoke(url: str, principal: str, scope: str, entity: int, actor: str, reason: str) -> None:
    with psycopg.connect(url) as c:
        c.execute('DELETE FROM telco.principal_grant WHERE principal_id=%s AND scope_type=%s AND entity_id=%s', (principal, scope, entity))
        c.execute("INSERT INTO telco.grant_audit(principal_id,scope_type,entity_id,action,actor,reason) VALUES(%s,%s,%s,'revoke',%s,%s)",
                  (principal, scope, entity, actor, reason))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', choices=['roles', 'demo', 'revoke', 'recap-bot', 'mint-jwt'])
    ap.add_argument('--principal'), ap.add_argument('--scope'), ap.add_argument('--entity', type=int)
    ap.add_argument('--reason', default='admin revoke')
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--out')
    a = ap.parse_args()
    if a.action == 'roles':
        roles(os.environ['ADMIN_DATABASE_URL'])
    elif a.action == 'demo':
        demo(os.environ['OPS_DATABASE_URL'])
    elif a.action == 'recap-bot':
        recap_bot(os.environ['OPS_DATABASE_URL'])
    elif a.action == 'mint-jwt':
        mint_jwt(a.principal, a.days, a.out)
    else:
        revoke(os.environ['OPS_DATABASE_URL'], a.principal, a.scope, a.entity, 'cli', a.reason)


if __name__ == '__main__':
    main()

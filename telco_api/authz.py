"""Default-deny scope authorization on top of telco.principal_grant / telco.can_access (single tenant, no RLS)."""
from . import errors
from .security import Principal


def can_access(conn, p: Principal, scope: str, entity_id: int) -> bool:
    return bool(conn.execute('SELECT telco.can_access(%s,%s,%s)', (p.principal_id, scope, int(entity_id))).fetchone()[0])


def require(conn, p: Principal, scope: str, entity_id: int) -> None:
    if not can_access(conn, p, scope, entity_id):
        raise errors.denied()


def outlets(conn, p: Principal) -> list[dict]:
    rows = conn.execute('''SELECT o.id,o.code,o.name,o.district_id,d.name FROM telco.outlet o JOIN telco.district d ON d.id=o.district_id
        WHERE telco.can_access(%s,'outlet',o.id) ORDER BY o.id''', (p.principal_id,)).fetchall()
    return [{'id': r[0], 'code': r[1], 'name': r[2], 'district_id': r[3], 'district_name': r[4]} for r in rows]


def districts(conn, p: Principal) -> list[dict]:
    rows = conn.execute('''SELECT d.id,d.code,d.name FROM telco.district d
        WHERE telco.can_access(%s,'district',d.id) ORDER BY d.id''', (p.principal_id,)).fetchall()
    return [{'id': r[0], 'code': r[1], 'name': r[2]} for r in rows]


def require_supervisor(p: Principal) -> None:
    if not p.is_supervisor:
        raise errors.role_denied()


def require_role(p: Principal, *roles: str) -> None:
    if p.role not in roles:
        raise errors.role_denied()


def sales_visible(conn, p: Principal, sales_id: int) -> bool:
    """Own sales account, or a district grant on the sales' base district."""
    if p.sales_id is not None and p.sales_id == sales_id:
        return True
    row = conn.execute('SELECT district_id FROM telco.sales WHERE id=%s', (sales_id,)).fetchone()
    return bool(row) and p.role != 'sales' and can_access(conn, p, 'district', row[0])

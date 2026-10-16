"""Immutable evidence snapshots. Every tool result shown to a user, chart, narrative or export cites one evidence_id."""
import hashlib
import uuid

from psycopg.types.json import Jsonb

from . import authz, errors
from .money import canonical_json, json_loads
from .security import Principal

ID_FIELDS = ('evidence_id', 'correlation_id', 'evidence_digest', 'created_at')


def digest(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k not in ID_FIELDS}
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()


def store(conn, p: Principal, tool: str, params: dict, payload: dict, correlation_id: str) -> dict:
    payload = dict(payload)
    payload['evidence_digest'] = digest(payload)
    eid = uuid.uuid4()
    payload['evidence_id'] = str(eid)
    payload['correlation_id'] = correlation_id
    # Jsonb with canonical (Decimal-as-string-free) dumps: Decimal is serialized as JSON number text.
    conn.execute('''INSERT INTO telco.evidence_snapshot(evidence_id,principal_id,tool_name,scope_type,entity_id,channel,
        params,payload,digest,query_version,watermark,batch_key,correlation_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                 (eid, p.principal_id, tool, payload.get('scope'), payload.get('entity_id'), payload.get('channel'),
                  Jsonb(params, dumps=_dumps), Jsonb(payload, dumps=_dumps), payload['evidence_digest'],
                  payload.get('query_version', tool), payload.get('watermark'), payload.get('source_batch_key'), correlation_id))
    return payload


def _dumps(obj) -> str:
    import json
    from decimal import Decimal

    class Enc(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, Decimal):
                return _DecimalNumber(o)
            return str(o)

    # Emit Decimal as exact JSON numbers (not strings) so SQL/clients can parse with arbitrary precision.
    return json.dumps(obj, cls=Enc, ensure_ascii=False).replace('"__DEC__', '').replace('__CED__"', '')


class _DecimalNumber(str):
    def __new__(cls, d):
        return str.__new__(cls, f'__DEC__{d}__CED__')


def load(conn, p: Principal, evidence_id: str, *, reauthorize: bool = True) -> dict:
    try:
        eid = uuid.UUID(str(evidence_id))
    except ValueError:
        raise errors.not_found('Evidence tidak ditemukan.')
    row = conn.execute('SELECT principal_id,tool_name,scope_type,entity_id,payload FROM telco.evidence_snapshot WHERE evidence_id=%s',
                       (eid,)).fetchone()
    # Same response for "not yours" and "missing": no existence oracle across principals.
    if not row or row[0] != p.principal_id:
        raise errors.not_found('Evidence tidak ditemukan.')
    payload = row[4] if isinstance(row[4], dict) else json_loads(row[4])
    if reauthorize:
        if row[2] in ('outlet', 'district'):
            authz.require(conn, p, row[2], row[3])
        for d in payload.get('districts', []) or []:
            authz.require(conn, p, 'district', d['district_id'])
    return payload

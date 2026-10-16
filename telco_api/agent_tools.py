"""Single source of truth for agent-callable business tools (read-only). Used by:
  - the MCP bridge (telco_mcp) that Hermes connects to, and
  - the router fallback function-calling loop.
Every call carries an opaque one-turn `ticket`; the API maps it to the principal server-side. Write transactions are
deliberately NOT exposed to the model."""
from datetime import datetime, timedelta, timezone

from pydantic import Field

from . import analytics, errors, evidence
from .schemas import ConversationKey, DiagnoseRequest, RankRequest, ResolveRequest, StockRequest, Strict, TargetRequest
from .security import Principal, load_principal, new_ticket, ticket_hash


class GetEvidenceArgs(Strict):
    evidence_id: str = Field(pattern=r'^[0-9a-f-]{36}$')


TOOLS = {
    'get_evidence': (GetEvidenceArgs, 'Ambil evidence JSON (angka resmi) berdasarkan evidence_id milik percakapan ini.'),
    'diagnose': (DiagnoseRequest, 'Diagnosis omzet outlet/kecamatan/kab-kota/provinsi per channel; periode sama panjang, akhir eksklusif.'),
    'rank_districts': (RankRequest, 'Peringkat kecamatan yang diizinkan untuk principal ini.'),
    'rank_regencies': (RankRequest, 'Peringkat kabupaten/kota yang seluruh kecamatannya diizinkan untuk principal ini.'),
    'stock': (StockRequest, 'Stok per akun (sales/outlet) dan satuan; tidak menjumlahkan PCS dengan IDR.'),
    'target': (TargetRequest, 'Realisasi vs target harian sales pada satu channel.'),
    'resolve_entity': (ResolveRequest, 'Cari outlet/kecamatan/kab-kota/provinsi dalam cakupan izin; nama ambigu butuh klarifikasi.'),
}
DEFAULT_ALLOWED = list(TOOLS)


def input_schema(name: str) -> dict:
    model = TOOLS[name][0]
    schema = model.model_json_schema()
    props = {'ticket': {'type': 'string', 'description': 'Ticket turn dari instruksi sistem.'}, **schema.get('properties', {})}
    return {'type': 'object', 'properties': props, 'required': ['ticket', *schema.get('required', [])],
            'additionalProperties': False, **({'$defs': schema['$defs']} if '$defs' in schema else {})}


def openai_tools() -> list[dict]:
    return [{'type': 'function', 'function': {'name': n, 'description': d, 'parameters': input_schema(n)}} for n, (_, d) in TOOLS.items()]


def mint_ticket(db, p: Principal, conversation_key: str, correlation_id: str, ttl_s: int, allowed=None, max_calls: int = 6,
                evidence_ids=()) -> str:
    t = new_ticket()

    def tx(conn):
        conn.execute('''INSERT INTO telco.agent_turn(ticket_hash,principal_id,conversation_key,correlation_id,allowed_tools,max_calls,
            evidence_ids,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s::uuid[],now()+make_interval(secs=>%s))''',
                     (ticket_hash(t), p.principal_id, conversation_key, correlation_id, list(allowed or DEFAULT_ALLOWED), max_calls,
                      [str(e) for e in evidence_ids], ttl_s))
    db.write(tx)
    return t


def _consume(conn, ticket: str, tool: str):
    r = conn.execute('''UPDATE telco.agent_turn SET calls=calls+1 WHERE ticket_hash=%s AND expires_at>now() AND calls<max_calls
        AND %s=ANY(allowed_tools) RETURNING principal_id,correlation_id,conversation_key''', (ticket_hash(ticket), tool)).fetchone()
    if not r:
        raise errors.unauthenticated('Ticket agent tidak valid, kedaluwarsa, habis, atau tool tidak diizinkan.')
    return r


def execute(db, ticket: str, name: str, arguments: dict) -> dict:
    if name not in TOOLS:
        raise errors.bad_request('UNKNOWN_TOOL', 'Tool tidak tersedia.')
    model = TOOLS[name][0]
    args = model.model_validate({k: v for k, v in arguments.items() if k != 'ticket'})
    principal_id, corr, conv = db.write(lambda conn: _consume(conn, ticket, name))
    p = db.write(lambda conn: load_principal(conn, principal_id, 'agent_ticket'))
    if name == 'get_evidence':
        def q(conn):
            allowed = conn.execute('SELECT %s::uuid=ANY(evidence_ids) FROM telco.agent_turn WHERE ticket_hash=%s',
                                   (args.evidence_id, ticket_hash(ticket))).fetchone()[0]
            if not allowed:
                raise errors.not_found('Evidence tidak ditemukan.')
            return evidence.load(conn, p, args.evidence_id)
        return db.write(q)
    if name == 'resolve_entity':
        return analytics.resolve_entity(db, p, args)
    fn = {'diagnose': analytics.diagnose, 'rank_districts': analytics.rank_districts, 'rank_regencies': analytics.rank_regencies,
          'stock': analytics.stock,
          'target': analytics.target}[name]
    out = fn(db, p, args, corr)
    db.write(lambda conn: conn.execute('UPDATE telco.agent_turn SET evidence_ids=array_append(evidence_ids,%s::uuid) WHERE ticket_hash=%s',
                                       (out['evidence_id'], ticket_hash(ticket))))
    return out


def turn_evidence(db, ticket: str) -> list[str]:
    r = db.write(lambda conn: conn.execute('SELECT evidence_ids FROM telco.agent_turn WHERE ticket_hash=%s', (ticket_hash(ticket),)).fetchone())
    return [str(x) for x in (r[0] if r else [])]


def utc() -> datetime:
    return datetime.now(timezone.utc) + timedelta(0)


__all__ = ['TOOLS', 'execute', 'mint_ticket', 'openai_tools', 'input_schema', 'turn_evidence', 'ConversationKey']

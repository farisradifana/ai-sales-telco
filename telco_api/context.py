"""Structured analysis context for follow-ups. The database (not Hermes memory) fixes entity/metric/period/evidence."""
import re
import uuid
from datetime import date

from psycopg.types.json import Jsonb

from . import analytics, authz, errors, evidence, narrative, periods
from .schemas import DiagnoseRequest
from .security import Principal

ACTION_Q = re.compile(r'apa yang harus|solusi|saran|rekomendasi|langkah|tindakan|harus (saya|kami) (lakukan|apa)|what should', re.I)
DISTRICT_Q = re.compile(r'kecamatan', re.I)
REGENCY_Q = re.compile(r'\bkab(upaten)?(nya)?\b|kab/kota|\bkotanya\b', re.I)
PERIOD_Q = re.compile(r'bulan lalu|minggu lalu|tahun lalu|periode lain|kuartal|bulan (sebelumnya|kemarin)', re.I)
OTHER_ENTITY_Q = re.compile(r'\b(OUT-\d{1,6}|FX-D\d{2})\b', re.I)
CHANNEL_Q = re.compile(r'sell[- ]?(in|out)', re.I)


def _row(conn, context_id: str, p: Principal, lock: bool = False):
    return conn.execute(f'''SELECT id,scope,metric,baseline_start,baseline_end,current_start,current_end,evidence_id,version,
        expires_at>now(),watermark,batch_key,query_version FROM telco.conversation_context WHERE id=%s AND principal_id=%s
        {'FOR UPDATE' if lock else ''}''', (context_id, p.principal_id)).fetchone()


def create(db, p: Principal, evidence_id: str, conversation_key: str, channel: str, ttl_hours: int) -> dict:
    def tx(conn):
        e = evidence.load(conn, p, evidence_id)  # reauthorizes scope
        if e.get('tool') not in ('diagnose', 'dashboard-diagnose', 'dashboard'):
            raise errors.bad_request('UNSUPPORTED_EVIDENCE', 'Konteks follow-up hanya untuk evidence diagnosis.')
        cid = 'ctx_' + uuid.uuid4().hex
        scope = {'type': e['scope'], 'id': e['entity_id']}
        conn.execute('''INSERT INTO telco.conversation_context(id,principal_id,channel,scope,metric,baseline_start,baseline_end,
            current_start,current_end,evidence,expires_at,evidence_id,watermark,batch_key,query_version)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()+make_interval(hours=>%s),%s,%s,%s,%s)''',
                     (cid, p.principal_id, channel, Jsonb(scope), e['channel'], e['baseline']['start'], e['baseline']['end_exclusive'],
                      e['current']['start'], e['current']['end_exclusive'],
                      Jsonb({'evidence_id': e['evidence_id'], 'digest': e['evidence_digest']}), ttl_hours, e['evidence_id'],
                      e.get('watermark'), e.get('source_batch_key'), e.get('query_version')))
        conn.execute('''INSERT INTO telco.conversation_pointer(principal_id,conversation_key,context_id) VALUES(%s,%s,%s)
            ON CONFLICT (principal_id,conversation_key) DO UPDATE SET context_id=EXCLUDED.context_id,updated_at=now()''',
                     (p.principal_id, conversation_key, cid))
        conn.execute('INSERT INTO telco.analysis_audit(context_id,tool_name,parameters,result_digest) VALUES(%s,%s,%s,%s)',
                     (cid, 'context.create', Jsonb(scope), e['evidence_digest']))
        return {'context_id': cid, 'version': 1, 'evidence_id': e['evidence_id'], 'scope': scope, 'channel': e['channel']}

    return db.write(tx)


def pointer(db, p: Principal, conversation_key: str) -> str | None:
    def q(conn):
        r = conn.execute('SELECT context_id FROM telco.conversation_pointer WHERE principal_id=%s AND conversation_key=%s',
                         (p.principal_id, conversation_key)).fetchone()
        return r[0] if r else None
    return db.write(q)


def get(db, p: Principal, context_id: str) -> dict:
    def q(conn):
        r = _row(conn, context_id, p)
        if not r:
            raise errors.not_found('Konteks tidak ditemukan.')
        authz.require(conn, p, r[1]['type'], r[1]['id'])
        return {'context_id': r[0], 'scope': r[1], 'channel': r[2], 'baseline': [str(r[3]), str(r[4])],
                'current': [str(r[5]), str(r[6])], 'evidence_id': str(r[7]) if r[7] else None, 'version': r[8], 'active': r[9]}
    return db.write(q)


def follow_up(db, p: Principal, context_id: str, question: str, expected_version: int, correlation_id: str, ttl_hours: int) -> dict:
    def load(conn):
        r = _row(conn, context_id, p)
        if not r:
            raise errors.not_found('Konteks tidak ditemukan.')
        if not r[9]:
            raise errors.gone('CONTEXT_EXPIRED', 'Konteks sudah kedaluwarsa. Sebutkan outlet/kecamatan dan periode yang dimaksud.')
        if r[8] != expected_version:
            raise errors.conflict('CONTEXT_VERSION_CONFLICT', 'Konteks sudah berubah di sesi lain; muat ulang sebelum melanjutkan.',
                                  current_version=r[8])
        # Reauthorize on EVERY follow-up: a revoked grant blocks even previously-read evidence.
        authz.require(conn, p, r[1]['type'], r[1]['id'])
        wm = conn.execute('SELECT complete_through,batch_key FROM telco.ingestion_watermark WHERE source=%s', (r[2],)).fetchone()
        return r, wm

    r, wm = db.write(load)
    scope, metric = r[1], r[2]
    b0, b1, c0, c1 = r[3], r[4], r[5], r[6]

    other = OTHER_ENTITY_Q.findall(question)
    ch = CHANNEL_Q.search(question)
    if PERIOD_Q.search(question):
        return {'status': 'needs_clarification', 'context_id': context_id, 'version': r[8],
                'message': ('Periode baru harus jelas dan jumlah harinya sama. Contoh: Juli 2026 vs Juni 2026 tidak setara '
                            '(31 vs 30 hari). Sebutkan tanggal awal dan akhir (akhir eksklusif) kedua periode.')}
    if other or (ch and ('sell_' + ch.group(1).lower()) != metric):
        return {'status': 'needs_clarification', 'context_id': context_id, 'version': r[8],
                'message': 'Pertanyaan menyebut cakupan/channel berbeda dari konteks. Kirim pertanyaan baru dengan entitas dan channel yang dimaksud.'}

    new_scope = None
    if DISTRICT_Q.search(question) and scope['type'] == 'outlet':
        did = db.read(lambda conn: conn.execute('SELECT district_id FROM telco.outlet WHERE id=%s', (scope['id'],)).fetchone()[0])
        if not db.read(lambda conn: authz.can_access(conn, p, 'district', did)):
            raise errors.denied('Anda tidak memiliki izin agregat kecamatan untuk outlet ini.')
        new_scope = {'type': 'district', 'id': did}
    elif DISTRICT_Q.search(question) and scope['type'] in analytics.AREA_SCOPES:
        # "kecamatannya" from a kab/kota/provinsi is ambiguous: list kecamatan from the pinned evidence, do not guess one.
        e = db.write(lambda conn: evidence.load(conn, p, str(r[7])))
        names = ', '.join(c['name'] for c in (e.get('district_contributions') or [])[:5])
        return {'status': 'needs_clarification', 'context_id': context_id, 'version': r[8],
                'message': 'Konteks ini mencakup beberapa kecamatan; sebutkan kecamatan yang dimaksud'
                           + (f' (kontribusi terbesar dulu: {names}).' if names else '.')}
    elif REGENCY_Q.search(question) and scope['type'] in ('outlet', 'district'):
        rid = db.read(lambda conn: conn.execute('''SELECT d.regency_id FROM telco.district d WHERE d.id=
            (CASE WHEN %s='outlet' THEN (SELECT district_id FROM telco.outlet WHERE id=%s) ELSE %s END)''',
                                                (scope['type'], scope['id'], scope['id'])).fetchone()[0])
        if not db.read(lambda conn: authz.can_access(conn, p, 'regency', rid)):
            raise errors.denied('Anda tidak memiliki izin agregat kabupaten/kota untuk cakupan ini.')
        new_scope = {'type': 'regency', 'id': rid}

    stale = (wm is None or r[10] is None or wm[0] != r[10] or wm[1] != r[11]
             or r[12] != analytics.query_version_for(scope['type']))
    if new_scope or stale:
        target = new_scope or scope
        req = DiagnoseRequest(scope=target['type'], entity_id=target['id'], channel=metric, baseline_start=b0, baseline_end=b1,
                              current_start=c0, current_end=c1)
        e = analytics.diagnose(db, p, req, correlation_id)

        def upd(conn):
            row = conn.execute('''UPDATE telco.conversation_context SET scope=%s,evidence=%s,evidence_id=%s,watermark=%s,batch_key=%s,
                query_version=%s,version=version+1,updated_at=now(),expires_at=now()+make_interval(hours=>%s)
                WHERE id=%s AND principal_id=%s AND version=%s AND expires_at>now() RETURNING version''',
                               (Jsonb(target), Jsonb({'evidence_id': e['evidence_id'], 'digest': e['evidence_digest']}), e['evidence_id'],
                                e.get('watermark'), e.get('source_batch_key'), e.get('query_version'), ttl_hours,
                                context_id, p.principal_id, expected_version)).fetchone()
            if not row:
                raise errors.conflict('CONTEXT_VERSION_CONFLICT', 'Konteks berubah bersamaan; tidak ditimpa diam-diam.')
            conn.execute('INSERT INTO telco.analysis_audit(context_id,tool_name,parameters,result_digest) VALUES(%s,%s,%s,%s)',
                         (context_id, 'context.recompute' if not new_scope else 'context.scope_change', Jsonb(target), e['evidence_digest']))
            return row[0]

        version = db.write(upd)
        status = 'scope_changed' if new_scope else 'recomputed'
    else:
        e = db.write(lambda conn: evidence.load(conn, p, str(r[7])))
        version = r[8]
        status = 'pinned'

    # New scope -> fresh diagnosis text; otherwise the same pinned evidence answers "what to do?".
    text = narrative.diagnose_text(e) if new_scope else narrative.recommendation_text(e)
    if status == 'recomputed':
        text = 'Data sumber berubah sejak jawaban sebelumnya; angka dihitung ulang untuk cakupan dan periode yang sama.\n' + text
    return {'status': status, 'context_id': context_id, 'version': version, 'evidence': e,
            'recommendation': e.get('recommendation'), 'text': text,
            'pinned': {'scope': new_scope or scope, 'channel': metric, 'baseline': [str(b0), str(b1)], 'current': [str(c0), str(c1)]}}


def as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


__all__ = ['create', 'follow_up', 'get', 'pointer', 'periods']

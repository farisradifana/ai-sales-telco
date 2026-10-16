"""Daily target nudges. Opt-in only, WIB workdays, configured hour, explicit per-principal channel (never summed).
Idempotency key target:<principal>:<date>:<channel> means n8n retries / double schedules never repeat a message."""
from datetime import datetime, timezone

from . import analytics, narrative, outbox
from .periods import WIB
from .schemas import TargetRequest
from .security import load_principal


def run(db, s, correlation_id: str, now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(WIB)
    day, hour, dow = now.date(), now.hour, now.isoweekday()

    def due(conn):
        if conn.execute('SELECT 1 FROM telco.business_holiday WHERE day=%s', (day,)).fetchone():
            return []
        return conn.execute('''SELECT n.principal_id,n.target_channel FROM telco.notification_pref n
            JOIN telco.app_principal p ON p.principal_id=n.principal_id AND p.active AND p.role='sales'
            WHERE n.daily_target_opt_in AND n.send_hour_wib=%s AND %s=ANY(n.workdays)''', (hour, dow)).fetchall()

    rows = db.write(due)
    out = {'date': day.isoformat(), 'hour_wib': hour, 'due': len(rows), 'enqueued': 0, 'skipped_no_target': 0, 'already_sent': 0}
    for principal_id, channel in rows:
        key = f'target:{principal_id}:{day.isoformat()}:{channel}'
        if db.write(lambda conn: conn.execute('SELECT 1 FROM telco.outbox WHERE idempotency_key=%s', (key,)).fetchone()):
            out['already_sent'] += 1
            continue
        p = db.write(lambda conn: load_principal(conn, principal_id, 'scheduler'))
        e = analytics.target(db, p, TargetRequest(channel=channel, target_date=day), correlation_id, now=now)
        if not e.get('target_available'):
            out['skipped_no_target'] += 1
            continue
        text = narrative.target_text(e, motivational=True) + '\nBalas STOP TARGET untuk berhenti.'
        db.write(lambda conn: outbox.enqueue(conn, principal_id, 'whatsapp_text', {'text': text, 'evidence_id': e['evidence_id']},
                                             key, correlation_id))
        out['enqueued'] += 1
    return out

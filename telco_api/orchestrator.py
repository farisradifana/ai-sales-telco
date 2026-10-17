"""Inbound WhatsApp event pipeline used by n8n: claim -> intent -> act -> (narrate) -> reply -> complete.
n8n sends only (provider, message_id). The principal is read from the verified inbound_event row. Each step is
idempotent: its result is cached on the event so n8n retries / worker crashes never repeat side effects."""
from psycopg.types.json import Jsonb

from . import analytics, charts, context, errors, intents, narrative, outbox, reports, transactions
from .agent import Narrator
from .money import dec, rupiah
from .schemas import DiagnoseRequest, DraftRequest, RankRequest, ResolveRequest, SaleLine, StockRequest, TargetRequest
from .security import load_principal


class Pipeline:
    def __init__(self, db, settings, narrator: Narrator):
        self.db, self.s, self.narrator = db, settings, narrator

    # ------------------------------------------------------------ plumbing
    def _event(self, conn, provider: str, message_id: str, lock: bool = False):
        r = conn.execute(f'''SELECT principal_id,message,correlation_id,state,result,identity_ref,provider_ts,claimed_until>now()
            FROM telco.inbound_event WHERE provider=%s AND message_id=%s {'FOR UPDATE' if lock else ''}''',
                         (provider, message_id)).fetchone()
        if not r:
            raise errors.not_found('Event tidak ditemukan.')
        return {'principal_id': r[0], 'message': r[1] or {}, 'correlation_id': r[2], 'state': r[3], 'result': r[4] or {},
                'identity_ref': r[5], 'provider_ts': r[6], 'leased': r[7]}

    def _cached(self, provider, message_id, step):
        ev = self.db.write(lambda conn: self._event(conn, provider, message_id))
        return ev, (ev['result'].get('steps') or {}).get(step)

    def _save(self, provider, message_id, step, value):
        self.db.write(lambda conn: conn.execute('''UPDATE telco.inbound_event SET result=jsonb_set(coalesce(result,'{}'::jsonb),
            ARRAY['steps',%s],%s::jsonb,true) WHERE provider=%s AND message_id=%s''', (step, Jsonb(value), provider, message_id)))
        # jsonb_set with a missing 'steps' parent: initialise then retry once.
        ev = self.db.write(lambda conn: self._event(conn, provider, message_id))
        if (ev['result'].get('steps') or {}).get(step) is None:
            self.db.write(lambda conn: conn.execute('''UPDATE telco.inbound_event SET result=coalesce(result,'{}'::jsonb)||
                jsonb_build_object('steps',coalesce(result->'steps','{}'::jsonb)||jsonb_build_object(%s::text,%s::jsonb))
                WHERE provider=%s AND message_id=%s''', (step, Jsonb(value), provider, message_id)))
        return value

    def _principal(self, ev):
        return self.db.write(lambda conn: load_principal(conn, ev['principal_id'], 'whatsapp'))

    # ------------------------------------------------------------ steps
    def claim(self, provider: str, message_id: str, lease_s: int = 120) -> dict:
        def tx(conn):
            r = conn.execute('''UPDATE telco.inbound_event SET state='processing',claimed_until=now()+make_interval(secs=>%s),attempts=attempts+1
                WHERE provider=%s AND message_id=%s AND (state='received' OR (state='processing' AND claimed_until<now())
                OR (state='failed' AND attempts<5)) RETURNING principal_id,provider_ts''', (lease_s, provider, message_id)).fetchone()
            if not r:
                ev = self._event(conn, provider, message_id)
                if ev['state'] == 'completed':
                    return {'status': 'completed', 'result': ev['result']}
                raise errors.conflict('EVENT_IN_PROGRESS', 'Event sedang diproses worker lain.')
            later = conn.execute('''SELECT count(*) FROM telco.inbound_event WHERE principal_id=%s AND provider=%s
                AND provider_ts>%s AND state='completed' ''', (r[0], provider, r[1])).fetchone()[0]
            return {'status': 'claimed', 'out_of_order': later > 0}
        return self.db.write(tx)

    def intent(self, provider: str, message_id: str) -> dict:
        ev, hit = self._cached(provider, message_id, 'intent')
        if hit:
            return hit
        it = intents.parse(ev['message'].get('text'))
        return self._save(provider, message_id, 'intent', {'name': it.name, 'slots': it.slots, 'clarification': it.clarification})

    def act(self, provider: str, message_id: str) -> dict:
        ev, hit = self._cached(provider, message_id, 'act')
        if hit:
            return hit
        it = self.intent(provider, message_id)
        p = self._principal(ev)
        corr = ev['correlation_id']
        conv_key = f"wa:{ev['identity_ref']}"
        try:
            res = self._dispatch(it, p, ev, corr, conv_key, provider, message_id)
        except errors.AppError as exc:
            if exc.status >= 500:
                raise  # transient: n8n retries this step; nothing was cached
            res = {'next': 'reply', 'text': _user_error(exc), 'error_code': exc.code}
        return self._save(provider, message_id, 'act', res)

    def _dispatch(self, it, p, ev, corr, conv_key, provider, message_id) -> dict:
        name, slots = it['name'], it['slots']
        if it.get('clarification'):
            return {'next': 'reply', 'text': it['clarification'], 'intent': name}
        if name in QUICK:  # fixed-format reports: no model, no quota
            if name == 'cmd_greeting':
                body, button = reports.greeting(self.db, p)
                return {'next': 'reply', 'intent': name, 'text': body, 'interactive': button}
            if name == 'cmd_menu':
                body, picker = reports.menu_picker(self.db, p)
                return {'next': 'reply', 'intent': name, 'text': body, 'interactive': picker}
            if name == 'cmd_input_help':
                return {'next': 'reply', 'intent': name, 'text': reports.input_help_text(p)}
            if name == 'cmd_products':
                return {'next': 'reply', 'intent': name, 'text': reports.products_text(self.db, p)}
            if name == 'cmd_stock':
                if p.sales_id is None:
                    return {'next': 'reply', 'intent': name, 'text': 'Stok hanya dipegang akun sales. Ketik *sell in* atau *sell out* '
                            'untuk laporan wilayah Anda.'}
                e = analytics.stock(self.db, p, StockRequest(account='self'), corr)
                return {'next': 'reply', 'intent': name, 'text': narrative.stock_text(e), 'evidence_id': e['evidence_id']}
            fn, render = QUICK[name]
            e = fn(self.db, p, corr)
            return {'next': 'reply', 'intent': name, 'text': render(e), 'evidence_id': e['evidence_id']}
        if name == 'sales_input':
            req = DraftRequest(channel=slots['channel'], outlet_code=slots['outlet_code'],
                               lines=[SaleLine(sku=x['sku'], quantity=x['quantity']) for x in slots['lines']])
            d = transactions.create_draft(self.db, self.s, p, req, corr, source_ref=f'{provider}:{message_id}')
            return {'next': 'reply', 'text': d['summary'], 'intent': name, 'draft_id': d['draft_id']}
        if name in ('confirm', 'cancel'):
            draft_id = self.db.write(lambda conn: transactions.find_draft_by_code(conn, p, slots['code']))
            if not draft_id:
                return {'next': 'reply', 'text': f"Kode {slots['code']} tidak ditemukan atau ambigu. Kirim ulang data penjualan.", 'intent': name}
            if name == 'cancel':
                transactions.cancel_draft(self.db, p, draft_id, f'{provider}:{message_id}', corr)
                return {'next': 'reply', 'text': f"Draft {slots['code']} dibatalkan; tidak ada yang diposting.", 'intent': name}
            r = transactions.confirm_draft(self.db, self.s, p, draft_id, f'{provider}:{message_id}', corr)
            lines = '\n'.join(f"- {x['sku']} x{_qty(x['quantity'])} → #{x['transaction_id']} ({rupiah(x['net_revenue_idr'])})" for x in r['lines'])
            prefix = 'Sudah tercatat sebelumnya (tidak diposting ulang):' if r.get('idempotent_replay') else 'Tercatat:'
            out = {'next': 'reply', 'text': f"{prefix}\n{lines}\nTotal *{rupiah(r['total_net_revenue_idr'])}*.", 'intent': name}
            if not r.get('idempotent_replay'):  # n8n appends these to the Google Sheet (tab Rekap) and logs to tab Info
                out.update(self._sheet_rows(p, r, slots['code']))
            return out
        if name == 'insight':
            rr = analytics.resolve_entity(self.db, p, ResolveRequest(query=slots['query'], scope=slots.get('scope')))
            if rr['status'] != 'resolved':
                opts = ', '.join(f"{c['code']} ({c['name']})" for c in rr['candidates'])
                return {'next': 'reply', 'intent': name,
                        'text': rr['message'] + (f' Pilihan: {opts}.' if opts else '')}
            ent = rr['resolved']
            e = analytics.diagnose(self.db, p, DiagnoseRequest(scope=ent['scope'], entity_id=ent['entity_id'], channel=slots['channel']), corr)
            return {'next': 'narrate', 'intent': name, 'evidence_id': e['evidence_id']}
        if name == 'follow_up':
            cid = context.pointer(self.db, p, conv_key)
            if not cid:
                return {'next': 'reply', 'intent': name, 'text': 'Belum ada konteks analisis. Sebutkan outlet/kecamatan, contoh: "kenapa omzet OUT-0001 turun?".'}
            cur = context.get(self.db, p, cid)
            r = context.follow_up(self.db, p, cid, slots['question'], cur['version'], corr, self.s.context_ttl_hours)
            text = r.get('text') or r.get('message')
            return {'next': 'reply', 'intent': name, 'text': text, 'context_id': cid, 'status': r['status']}
        if name == 'stock':
            if slots.get('outlet_code'):
                rr = analytics.resolve_entity(self.db, p, ResolveRequest(query=slots['outlet_code'], scope='outlet'))
                if rr['status'] != 'resolved':
                    return {'next': 'reply', 'intent': name, 'text': rr['message']}
                e = analytics.stock(self.db, p, StockRequest(account='outlet', outlet_id=rr['resolved']['entity_id']), corr)
            else:
                e = analytics.stock(self.db, p, StockRequest(account='self'), corr)
            return {'next': 'reply', 'intent': name, 'text': narrative.stock_text(e), 'evidence_id': e['evidence_id']}
        if name == 'target':
            e = analytics.target(self.db, p, TargetRequest(channel=slots['channel']), corr)
            return {'next': 'reply', 'intent': name, 'text': narrative.target_text(e), 'evidence_id': e['evidence_id']}
        if name == 'rank_regencies':
            e = analytics.rank_regencies(self.db, p, RankRequest(channel=slots['channel']), corr)
            return {'next': 'reply', 'intent': name, 'text': narrative.regency_ranking_text(e), 'evidence_id': e['evidence_id']}
        if name == 'rank_districts':
            e = analytics.rank_districts(self.db, p, RankRequest(channel=slots['channel']), corr)
            return {'next': 'reply', 'intent': name, 'text': narrative.ranking_text(e), 'evidence_id': e['evidence_id']}
        if name in ('target_opt_in', 'target_opt_out'):
            on = name == 'target_opt_in'
            ch = slots.get('channel', 'sell_out')
            self.db.write(lambda conn: conn.execute('''INSERT INTO telco.notification_pref(principal_id,target_channel,daily_target_opt_in)
                VALUES(%s,%s,%s) ON CONFLICT (principal_id) DO UPDATE SET daily_target_opt_in=EXCLUDED.daily_target_opt_in,
                target_channel=CASE WHEN %s THEN EXCLUDED.target_channel ELSE telco.notification_pref.target_channel END,updated_at=now()''',
                                                    (p.principal_id, ch, on, on)))
            return {'next': 'reply', 'intent': name,
                    'text': f"Pengingat target harian {'aktif untuk ' + ch.replace('_', '-') if on else 'dimatikan'}."}
        return {'next': 'reply', 'intent': name, 'text': it.get('clarification') or 'Perintah belum dikenali.'}

    def _sheet_rows(self, p, r: dict, code: str) -> dict:
        ids = [int(x['transaction_id']) for x in r['lines']]

        def q(conn):
            return conn.execute('''SELECT (s.occurred_at AT TIME ZONE 'Asia/Jakarta')::date,to_char(s.occurred_at AT TIME ZONE 'Asia/Jakarta','DD-MM-YYYY HH24:MI'),
                o.code,o.name,pr.sku,pr.stock_unit,s.quantity,s.net_revenue,s.id FROM telco.sell_in s JOIN telco.outlet o ON o.id=s.outlet_id
                JOIN telco.product pr ON pr.id=s.product_id WHERE s.id=ANY(%s) ORDER BY s.id''', (ids,)).fetchall()
        rows = self.db.read(q)
        if not rows:
            return {}
        sheet = [{'Tanggal (WIB)': str(d), 'Kode Outlet': oc, 'Nama Outlet': on, 'SKU': sku, 'Satuan': unit, 'Kuantitas': float(qty),
                  'Omzet (IDR)': float(rev), 'Jumlah Transaksi Bersih': 1} for d, _, oc, on, sku, unit, qty, rev, _ in rows]
        items = ', '.join(f"{sku} x{_qty(qty)} ({rupiah(rev)})" for _, _, _, _, sku, _, qty, rev, _ in rows)
        log = [{'Keterangan': f"Log input sell-in {rows[0][1]} WIB",
                'Nilai': f"{p.display_name} · {rows[0][2]} · {items} · total {rupiah(r['total_net_revenue_idr'])} · kode {code} · "
                         f"transaksi #{', #'.join(str(x[8]) for x in rows)}"}]
        return {'sheet_rows': sheet, 'sheet_log': log}

    def narrate(self, provider: str, message_id: str) -> dict:
        ev, hit = self._cached(provider, message_id, 'narrate')
        if hit:
            return hit
        act = self.act(provider, message_id)
        if act.get('next') != 'narrate':
            return self._save(provider, message_id, 'narrate', {'skipped': True})
        p = self._principal(ev)
        from . import evidence as ev_mod
        e = self.db.write(lambda conn: ev_mod.load(conn, p, act['evidence_id']))
        conv_key = f"wa:{ev['identity_ref']}"
        out = self.narrator.narrate(p, e, ev['message'].get('text') or '', conv_key, ev['correlation_id'])
        try:
            out['chart_url'] = charts.paired_chart_url(self.db, self.s, p, e, ev['correlation_id'])
        except errors.AppError:
            out['chart_url'] = None
        ctx = context.create(self.db, p, e['evidence_id'], conv_key, 'whatsapp', self.s.context_ttl_hours)
        out['context_id'] = ctx['context_id']
        return self._save(provider, message_id, 'narrate', out)

    def reply(self, provider: str, message_id: str) -> dict:
        ev, hit = self._cached(provider, message_id, 'reply')
        if hit:
            return hit
        act = self.act(provider, message_id)
        if act.get('next') == 'narrate':
            n = self.narrate(provider, message_id)
            text = n['text'] + (f"\nGrafik (tautan privat, berlaku terbatas): {n['chart_url']}" if n.get('chart_url') else '')
        else:
            text = act['text']
        # Evidence ids are kept for audit in the outbox payload; they are not part of the user-facing text.
        eids = (self.narrate(provider, message_id).get('evidence_ids') if act.get('next') == 'narrate'
                else [act['evidence_id']] if act.get('evidence_id') else act.get('evidence_ids') or [])
        base = {'preview_url': False, 'evidence_ids': eids, 'reply_to': ev['identity_ref']}  # reply_to: blind-index prefix of sender
        interactive, follow_up = act.get('interactive'), None
        if not interactive and _offers_menu(act):
            # Quick-command replies end with a "Pilih Perintah" picker: inside the message when it fits a list body (1024),
            # otherwise as a short second message right after the report.
            p = self._principal(ev)
            if len(text) <= 1000:
                interactive = reports.picker(self.db, p, text)
            else:
                follow_up = reports.picker(self.db, p, 'Pilih perintah lain 👇')

        def tx(conn):
            oid = outbox.enqueue(conn, ev['principal_id'], 'whatsapp_text',
                                 {'text': text, **base, **({'interactive': interactive} if interactive else {})},
                                 f'reply:{provider}:{message_id}', ev['correlation_id'])
            if follow_up:
                outbox.enqueue(conn, ev['principal_id'], 'whatsapp_text', {'text': follow_up['body']['text'], **base, 'interactive': follow_up},
                               f'reply:{provider}:{message_id}:menu', ev['correlation_id'])
            return oid
        oid = self.db.write(tx)
        return self._save(provider, message_id, 'reply', {'outbox_id': oid})

    def complete(self, provider: str, message_id: str) -> dict:
        def tx(conn):
            conn.execute('''UPDATE telco.inbound_event SET state='completed',completed_at=now(),claimed_until=NULL
                WHERE provider=%s AND message_id=%s AND state<>'completed' ''', (provider, message_id))
            return {'status': 'completed'}
        return self.db.write(tx)

    def fail(self, provider: str, message_id: str, error: str) -> dict:
        self.db.write(lambda conn: conn.execute('''UPDATE telco.inbound_event SET state='failed',claimed_until=NULL,
            result=coalesce(result,'{}'::jsonb)||jsonb_build_object('last_error',%s::text) WHERE provider=%s AND message_id=%s AND state<>'completed' ''',
                                                (error[:300], provider, message_id)))
        return {'status': 'failed'}

    def run_all(self, provider: str, message_id: str) -> dict:
        """Same steps in-process (used by the Python worker fallback and tests)."""
        c = self.claim(provider, message_id)
        if c['status'] == 'completed':
            return c
        self.intent(provider, message_id)
        a = self.act(provider, message_id)
        if a.get('next') == 'narrate':
            self.narrate(provider, message_id)
        r = self.reply(provider, message_id)
        self.complete(provider, message_id)
        return {'status': 'completed', 'outbox_id': r['outbox_id'], 'out_of_order': c.get('out_of_order')}


QUICK = {'cmd_greeting': None, 'cmd_menu': None, 'cmd_stock': None, 'cmd_products': None, 'cmd_input_help': None,
         'cmd_sell_in': (reports.sell_in, reports.sell_in_text), 'cmd_sell_out': (reports.sell_out, reports.sell_out_text),
         'cmd_rank_outlets': (reports.rank_outlets, reports.rank_outlets_text),
         'cmd_rank_districts': (reports.rank_districts, reports.rank_area_text),
         'cmd_rank_regencies': (reports.rank_regencies, reports.rank_area_text)}


def _qty(q) -> str:
    v = dec(q)
    return f"{int(v):,}".replace(',', '.') if v == v.to_integral() else str(v)


def _offers_menu(act: dict) -> bool:
    intent = act.get('intent') or ''
    return (intent.startswith('cmd_') and intent not in ('cmd_menu', 'cmd_greeting')) or act.get('source') == 'quota_exhausted'


def _user_error(exc: errors.AppError) -> str:
    if exc.code == 'INSUFFICIENT_STOCK':
        return exc.message + ' Tidak ada yang diposting; periksa stok lalu kirim ulang.'
    if exc.code == 'SCOPE_DENIED':
        return 'Cakupan tersebut tidak diizinkan untuk akun Anda.'
    if exc.code in ('LEVEL_DENIED', 'ACCOUNT_REQUIRED'):
        return exc.message + ' Ketik *menu* untuk melihat perintah yang tersedia.'
    return f'{exc.message} (kode {exc.code})'

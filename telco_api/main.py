"""FastAPI application: routes per docs/TOOL_CONTRACTS.md plus internal n8n/MCP endpoints."""
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import (agent_tools, analytics, authz, charts, context, errors, evidence, nudges, outbox, spreadsheet, transactions,
               webhook)
from .agent import Narrator
from .db import Database
from .money import stringify_money
from .orchestrator import Pipeline
from .schemas import (ActionDecision, ConfirmRequest, ContextCreate, CorrectionRequest, DashboardRequest, DevLogin,
                      DiagnoseRequest, DirectSale, DraftRequest, FollowUp, NotificationPrefIn, NudgeRun, OutboxDispatch,
                      RankRequest, ResolveRequest, ReversalRequest, StockRequest, TargetRequest, TransferRequest)
from .security import Principal, RateLimiter, TokenService, bearer, check_service_token, load_principal
from .settings import Settings, get_settings

log = logging.getLogger('telco.api')
REDACT = re.compile(r'(\b\d{8,15}\b)|(Bearer\s+\S+)')


class EventRef(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str = Field(pattern=r'^whatsapp$')
    message_id: str = Field(min_length=6, max_length=160)
    error: str | None = Field(None, max_length=300)


def create_app(settings: Settings | None = None, db: Database | None = None, narrator: Narrator | None = None,
               wa_sender=None, n8n_sender=None) -> FastAPI:
    s = settings or get_settings()
    logging.basicConfig(level=s.log_level, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    database = db or Database(s)
    tokens = TokenService(s)
    limiter = RateLimiter(s.rate_limit_per_minute)
    narr = narrator or Narrator(s, database)
    pipeline = Pipeline(database, s, narr)
    wa = wa_sender or outbox.WhatsAppSender(s)
    n8n = n8n_sender or outbox.N8nDispatcher(s)

    @asynccontextmanager
    async def lifespan(app):
        database.open()
        yield
        database.close()

    app = FastAPI(title='TELCO Sales AI API', version='1.0.0', lifespan=lifespan,
                  description='Single-tenant. Otorisasi default-deny via principal_grant/can_access. Tidak ada RLS multi-tenant.')
    app.state.db, app.state.settings, app.state.pipeline = database, s, pipeline
    if s.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in s.cors_origins.split(',') if o.strip()],
                           allow_credentials=True, allow_methods=['GET', 'POST', 'PUT'], allow_headers=['Content-Type', 'X-Correlation-Id'])

    @app.middleware('http')
    async def correlation(request: Request, call_next):
        cid = request.headers.get('x-correlation-id', '')
        if not re.fullmatch(r'[A-Za-z0-9._:-]{8,64}', cid):
            cid = 'corr_' + uuid.uuid4().hex
        request.state.correlation_id = cid
        response = await call_next(request)
        response.headers['X-Correlation-Id'] = cid
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        # Path only (no query strings/bodies) and phone-like digits redacted.
        log.info('%s %s %s corr=%s', request.method, REDACT.sub('[redacted]', request.url.path), response.status_code, cid)
        return response

    @app.exception_handler(errors.AppError)
    async def app_error(request: Request, exc: errors.AppError):
        return JSONResponse({'error': {'code': exc.code, 'message': exc.message, 'correlation_id': request.state.correlation_id,
                                       'details': exc.details}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [{'loc': list(e['loc']), 'msg': e['msg'], 'type': e['type']} for e in exc.errors()]
        return JSONResponse({'error': {'code': 'INVALID_INPUT', 'message': 'Input tidak valid.',
                                       'correlation_id': request.state.correlation_id, 'details': {'errors': details}}}, status_code=400)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        log.exception('unhandled corr=%s', getattr(request.state, 'correlation_id', '-'))
        return JSONResponse({'error': {'code': 'INTERNAL', 'message': 'Kesalahan internal.',
                                       'correlation_id': getattr(request.state, 'correlation_id', None), 'details': {}}}, status_code=500)

    # ------------------------------------------------------------------ dependencies
    def principal(request: Request) -> Principal:
        token = bearer(request) or request.cookies.get(s.session_cookie)
        if not token:
            raise errors.unauthenticated()
        sub = tokens.verify(token)
        p = database.write(lambda conn: load_principal(conn, sub, 'jwt'))
        limiter.check(p.principal_id)
        return p

    def n8n_service(request: Request):
        check_service_token(request, s.n8n_service_token)

    def mcp_service(request: Request):
        check_service_token(request, s.mcp_service_token)

    corr = lambda r: r.state.correlation_id  # noqa: E731

    # ------------------------------------------------------------------ health
    @app.get('/health/live')
    def live():
        return {'status': 'ok'}

    @app.get('/health/ready')
    def ready():
        dbs = database.ping()
        agent = narr.hermes.probe() if narr.hermes.configured else {'ok': False, 'reason': 'not_configured'}
        ok = all(v == 'ok' for v in dbs.values())
        body = {'status': 'ready' if ok else 'not_ready', 'database': dbs,
                'agent': {'hermes_ok': agent.get('ok'), 'reason': agent.get('reason'), 'circuit': narr.hermes.breaker.state},
                'whatsapp_send_mode': s.wa_send_mode}
        return JSONResponse(body, status_code=200 if ok else 503)

    # ------------------------------------------------------------------ auth/session
    @app.post('/v1/auth/dev-login')
    def dev_login(body: DevLogin):
        if not s.dev_login_enabled or s.app_env not in ('local', 'test'):
            raise errors.not_found()
        database.write(lambda conn: load_principal(conn, body.principal_id, 'jwt'))
        token = tokens.issue_session(body.principal_id)
        resp = JSONResponse({'status': 'ok', 'principal_id': body.principal_id})
        resp.set_cookie(s.session_cookie, token, httponly=True, samesite='strict', secure=s.app_env not in ('local', 'test'),
                        max_age=s.session_ttl_minutes * 60)
        return resp

    @app.post('/v1/auth/logout')
    def logout():
        resp = JSONResponse({'status': 'ok'})
        resp.delete_cookie(s.session_cookie)
        return resp

    @app.get('/v1/me')
    def me(p: Principal = Depends(principal)):
        def q(conn):
            o = conn.execute('''SELECT op.code,op.level,op.title,op.sales_area,mp.title,mp.level FROM telco.app_principal ap
                JOIN telco.org_position op ON op.id=ap.org_position_id LEFT JOIN telco.org_position mp ON mp.id=op.parent_id
                WHERE ap.principal_id=%s''', (p.principal_id,)).fetchone()
            org = {'code': o[0], 'level': o[1], 'title': o[2], 'sales_area': o[3], 'manager_title': o[4], 'manager_level': o[5]} if o else None
            return {'principal_id': p.principal_id, 'role': p.role, 'display_name': p.display_name, 'sales_id': p.sales_id, 'org': org,
                    'outlets': authz.outlets(conn, p), 'districts': authz.districts(conn, p),
                    'regencies': authz.regencies(conn, p), 'provinces': authz.provinces(conn, p),
                    'watermarks': {r[0]: str(r[1]) for r in conn.execute('SELECT source,complete_through FROM telco.ingestion_watermark')}}
        return database.read(q)

    # ------------------------------------------------------------------ analytical tools
    @app.post('/v1/tools/diagnose')
    def t_diagnose(body: DiagnoseRequest, request: Request, p: Principal = Depends(principal)):
        return analytics.diagnose(database, p, body, corr(request))

    @app.post('/v1/tools/rank-districts')
    def t_rank(body: RankRequest, request: Request, p: Principal = Depends(principal)):
        return analytics.rank_districts(database, p, body, corr(request))

    @app.post('/v1/tools/rank-regencies')
    def t_rank_regencies(body: RankRequest, request: Request, p: Principal = Depends(principal)):
        return analytics.rank_regencies(database, p, body, corr(request))

    @app.post('/v1/tools/stock')
    def t_stock(body: StockRequest, request: Request, p: Principal = Depends(principal)):
        return analytics.stock(database, p, body, corr(request))

    @app.post('/v1/tools/target')
    def t_target(body: TargetRequest, request: Request, p: Principal = Depends(principal)):
        return analytics.target(database, p, body, corr(request))

    @app.post('/v1/tools/resolve-entity')
    def t_resolve(body: ResolveRequest, p: Principal = Depends(principal)):
        return analytics.resolve_entity(database, p, body)

    @app.post('/v1/tools/dashboard')
    def t_dashboard(body: DashboardRequest, request: Request, p: Principal = Depends(principal)):
        return analytics.dashboard_snapshot(database, p, body, corr(request))

    @app.get('/v1/evidence/{evidence_id}')
    def get_evidence(evidence_id: str, p: Principal = Depends(principal)):
        return stringify_money(database.write(lambda conn: evidence.load(conn, p, evidence_id)))

    @app.get('/v1/evidence/{evidence_id}/chart.svg')
    def chart_auth(evidence_id: str, p: Principal = Depends(principal)):
        e = database.write(lambda conn: evidence.load(conn, p, evidence_id))
        return Response(charts.render_waterfall(e), media_type='image/svg+xml')

    @app.get('/charts/{evidence_id}.svg')
    def chart_signed(evidence_id: str, exp: str = Query(...), sig: str = Query(...)):
        owner = database.write(lambda conn: conn.execute('SELECT principal_id FROM telco.evidence_snapshot WHERE evidence_id::text=%s',
                                                         (evidence_id,)).fetchone())
        if not owner:
            raise errors.AppError(403, 'LINK_INVALID', 'Tautan chart tidak valid.')
        charts.check_signature(s, evidence_id, owner[0], exp, sig)
        p = database.write(lambda conn: load_principal(conn, owner[0], 'signed_link'))
        e = database.write(lambda conn: evidence.load(conn, p, evidence_id))  # reauthorizes: revoked grant kills link
        return Response(charts.render_waterfall(e), media_type='image/svg+xml')

    # ------------------------------------------------------------------ contexts
    @app.post('/v1/contexts')
    def ctx_create(body: ContextCreate, p: Principal = Depends(principal)):
        return context.create(database, p, str(body.evidence_id), body.conversation_key, 'dashboard', s.context_ttl_hours)

    @app.get('/v1/contexts/{context_id}')
    def ctx_get(context_id: str, p: Principal = Depends(principal)):
        return context.get(database, p, context_id)

    @app.post('/v1/contexts/{context_id}/follow-up')
    def ctx_follow(context_id: str, body: FollowUp, request: Request, p: Principal = Depends(principal)):
        return context.follow_up(database, p, context_id, body.question, body.expected_version, corr(request), s.context_ttl_hours)

    # ------------------------------------------------------------------ transactions
    @app.post('/v1/transactions/drafts')
    def tx_draft(body: DraftRequest, request: Request, p: Principal = Depends(principal)):
        return transactions.create_draft(database, s, p, body, corr(request), source_ref='api')

    @app.post('/v1/transactions/drafts/{draft_id}/confirm')
    def tx_confirm(draft_id: uuid.UUID, body: ConfirmRequest, request: Request, p: Principal = Depends(principal)):
        return transactions.confirm_draft(database, s, p, draft_id, body.confirmation_event_id, corr(request))

    @app.post('/v1/transactions/drafts/{draft_id}/cancel')
    def tx_cancel(draft_id: uuid.UUID, body: ConfirmRequest, request: Request, p: Principal = Depends(principal)):
        return transactions.cancel_draft(database, p, draft_id, body.confirmation_event_id, corr(request))

    @app.post('/v1/transactions/sell-in')
    def tx_sell_in(body: DirectSale, request: Request, p: Principal = Depends(principal)):
        return transactions.post_direct(database, s, p, 'sell_in', body, corr(request))

    @app.post('/v1/transactions/sell-out')
    def tx_sell_out(body: DirectSale, request: Request, p: Principal = Depends(principal)):
        return transactions.post_direct(database, s, p, 'sell_out', body, corr(request))

    @app.post('/v1/corrections/backdated-sale')
    def tx_correction(body: CorrectionRequest, request: Request, p: Principal = Depends(principal)):
        return transactions.post_correction(database, s, p, body, corr(request))

    @app.post('/v1/reversals')
    def tx_reversal(body: ReversalRequest, request: Request, p: Principal = Depends(principal)):
        return transactions.request_reversal(database, s, p, body, corr(request))

    @app.post('/v1/transfers')
    def tx_transfer(body: TransferRequest, request: Request, p: Principal = Depends(principal)):
        return transactions.request_transfer(database, s, p, body, corr(request))

    @app.get('/v1/actions')
    def actions(p: Principal = Depends(principal)):
        return {'items': transactions.list_actions(database, p)}

    @app.post('/v1/actions/{action_id}/decision')
    def action_decide(action_id: uuid.UUID, body: ActionDecision, request: Request, p: Principal = Depends(principal)):
        event_id = f'dashboard:{corr(request)}'
        return transactions.decide_action(database, s, p, action_id, body.decision, body.note, event_id, corr(request))

    @app.put('/v1/me/notifications')
    def prefs(body: NotificationPrefIn, p: Principal = Depends(principal)):
        if p.role != 'sales':
            raise errors.role_denied()
        database.write(lambda conn: conn.execute('''INSERT INTO telco.notification_pref(principal_id,target_channel,daily_target_opt_in,
            send_hour_wib,workdays) VALUES(%s,%s,%s,%s,%s) ON CONFLICT (principal_id) DO UPDATE SET target_channel=EXCLUDED.target_channel,
            daily_target_opt_in=EXCLUDED.daily_target_opt_in,send_hour_wib=EXCLUDED.send_hour_wib,workdays=EXCLUDED.workdays,updated_at=now()''',
                                                 (p.principal_id, body.target_channel, body.daily_target_opt_in, body.send_hour_wib, body.workdays)))
        return {'status': 'ok'}

    # ------------------------------------------------------------------ exports / imports
    @app.get('/v1/exports/sales.csv')
    def export_csv(scope: str = Query(pattern='^(outlet|district)$'), entity_id: int = Query(ge=1),
                   channel: str = Query(pattern='^(sell_in|sell_out)$'), start: date = Query(...), end: date = Query(...),
                   p: Principal = Depends(principal)):
        body, meta = spreadsheet.export_sales_csv(database, p, scope, entity_id, channel, start, end)
        return Response(body, media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="rekap_{channel}_{scope}{entity_id}_{start}_{end}.csv"',
                                 'X-Export-Total-Revenue-Idr': meta['total_revenue_idr'], 'X-Export-Watermark': meta['watermark'] or ''})

    @app.post('/v1/imports')
    async def import_stage(request: Request, file: UploadFile = File(...), p: Principal = Depends(principal)):
        content = await file.read(2_000_001)
        if len(content) > 2_000_000:
            raise errors.AppError(413, 'PAYLOAD_TOO_LARGE', 'File maksimal 2 MB.')
        return spreadsheet.stage_import(database, s, p, file.filename or 'upload.csv', content, corr(request))

    @app.get('/v1/imports/{import_id}')
    def import_get(import_id: uuid.UUID, p: Principal = Depends(principal)):
        authz.require_role(p, 'admin')
        r = database.write(lambda conn: conn.execute('SELECT status,preview,row_count,valid_rows FROM telco.import_file WHERE id=%s', (import_id,)).fetchone())
        if not r:
            raise errors.not_found()
        return {'import_id': str(import_id), 'status': r[0], 'preview': r[1], 'row_count': r[2], 'valid_rows': r[3]}

    @app.post('/v1/imports/{import_id}/confirm')
    def import_confirm(import_id: uuid.UUID, request: Request, p: Principal = Depends(principal)):
        return spreadsheet.confirm_import(database, s, p, str(import_id), corr(request))

    # ------------------------------------------------------------------ WhatsApp ingress (public)
    @app.get('/webhooks/whatsapp', response_class=PlainTextResponse)
    def wa_verify(request: Request):
        q = request.query_params
        return webhook.verify_challenge(s, q.get('hub.mode'), q.get('hub.verify_token'), q.get('hub.challenge'))

    @app.post('/webhooks/whatsapp')
    async def wa_ingest(request: Request):
        raw = await request.body()
        res = webhook.ingest(database, s, raw, request.headers.get('x-hub-signature-256'), corr(request))
        return {'status': 'received', **res}

    # ------------------------------------------------------------------ internal (n8n service token)
    for step in ('claim', 'intent', 'act', 'narrate', 'reply', 'complete', 'fail'):
        def make(step_name):
            def handler(body: EventRef, _=Depends(n8n_service)):
                fn = getattr(pipeline, step_name)
                return fn(body.provider, body.message_id, body.error or 'n8n-error') if step_name == 'fail' else fn(body.provider, body.message_id)
            handler.__name__ = f'event_{step_name}'
            return handler
        app.post(f'/internal/v1/events/{step}')(make(step))

    @app.post('/internal/v1/outbox/dispatch')
    def outbox_dispatch(body: OutboxDispatch, _=Depends(n8n_service)):
        return outbox.dispatch_batch(database, s, wa, n8n, body.limit, body.lease_seconds, body.worker_id)

    @app.get('/internal/v1/outbox/stats')
    def outbox_stats(_=Depends(n8n_service)):
        return database.write(outbox.stats)

    @app.post('/internal/v1/targets/nudges')
    def run_nudges(body: NudgeRun, request: Request, _=Depends(n8n_service)):
        now = body.now if (body.now and s.app_env in ('local', 'test')) else None
        return nudges.run(database, s, corr(request), now=now)

    # ------------------------------------------------------------------ MCP bridge -> business tools (ticket-bound)
    @app.get('/internal/v1/agent-tools')
    def agent_tool_specs(_=Depends(mcp_service)):
        return {'tools': [{'name': n, 'description': d, 'input_schema': agent_tools.input_schema(n)} for n, (_m, d) in agent_tools.TOOLS.items()]}

    @app.post('/internal/v1/agent-tools/{name}')
    async def agent_tool_call(name: str, request: Request, _=Depends(mcp_service)):
        args = await request.json()
        if not isinstance(args, dict):
            raise errors.bad_request('INVALID_INPUT', 'Argumen harus objek JSON.')
        ticket = request.headers.get('x-agent-ticket') or args.get('ticket') or ''
        return agent_tools.execute(database, ticket, name, args)

    return app


def app_factory() -> FastAPI:
    return create_app()

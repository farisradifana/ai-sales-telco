"""Streamable-HTTP MCP server (mcp 2.3.0 MCPServer) for Hermes.

Security model:
  * Hermes authenticates to this bridge with MCP_BRIDGE_TOKEN (static bearer from Hermes' env, checked here).
  * The bridge calls the API with MCP_SERVICE_TOKEN + the per-turn ticket. The API resolves the ticket to a principal
    and authorizes every scope. No SQL, shell, filesystem or network tools exist here.
  * Tool argument models are imported from telco_api.agent_tools (single schema source); contract tests compare them.
"""
import hmac
import json
import os

import httpx
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

API_BASE = os.environ.get('TELCO_API_INTERNAL_URL', 'http://api:8000')
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False)

mcp = MCPServer(name='telco', instructions='Tool bisnis TELCO read-only. Selalu kirim ticket dari instruksi sistem.',
                version='1.0.0')
_client: httpx.Client | None = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(base_url=API_BASE, timeout=30)
    return _client


def _call(name: str, ticket: str, args: dict) -> str:
    body = {k: v for k, v in args.items() if v is not None}
    r = _http().post(f'/internal/v1/agent-tools/{name}', json=body,
                     headers={'Authorization': f"Bearer {os.environ['MCP_SERVICE_TOKEN']}", 'X-Agent-Ticket': ticket})
    if r.status_code >= 400:
        err = r.json().get('error', {}) if r.headers.get('content-type', '').startswith('application/json') else {}
        return json.dumps({'error': err.get('code', f'HTTP_{r.status_code}'), 'message': err.get('message', '')}, ensure_ascii=False)
    return r.text


@mcp.tool(name='get_evidence', description='Ambil evidence JSON resmi milik turn ini.', annotations=READ_ONLY)
def get_evidence(ticket: str, evidence_id: str) -> str:
    return _call('get_evidence', ticket, {'evidence_id': evidence_id})


@mcp.tool(name='diagnose', description='Diagnosis omzet outlet/kecamatan/kab-kota/provinsi per channel; periode sama panjang, akhir eksklusif.',
          annotations=READ_ONLY)
def diagnose(ticket: str, scope: str, entity_id: int, channel: str = 'sell_out', baseline_start: str | None = None,
             baseline_end: str | None = None, current_start: str | None = None, current_end: str | None = None) -> str:
    return _call('diagnose', ticket, {'scope': scope, 'entity_id': entity_id, 'channel': channel, 'baseline_start': baseline_start,
                                      'baseline_end': baseline_end, 'current_start': current_start, 'current_end': current_end})


@mcp.tool(name='rank_districts', description='Peringkat kecamatan yang diizinkan.', annotations=READ_ONLY)
def rank_districts(ticket: str, channel: str = 'sell_out', baseline_start: str | None = None, baseline_end: str | None = None,
                   current_start: str | None = None, current_end: str | None = None) -> str:
    return _call('rank_districts', ticket, {'channel': channel, 'baseline_start': baseline_start, 'baseline_end': baseline_end,
                                            'current_start': current_start, 'current_end': current_end})


@mcp.tool(name='rank_regencies', description='Peringkat kabupaten/kota yang diizinkan.', annotations=READ_ONLY)
def rank_regencies(ticket: str, channel: str = 'sell_out', baseline_start: str | None = None, baseline_end: str | None = None,
                   current_start: str | None = None, current_end: str | None = None) -> str:
    return _call('rank_regencies', ticket, {'channel': channel, 'baseline_start': baseline_start, 'baseline_end': baseline_end,
                                            'current_start': current_start, 'current_end': current_end})


@mcp.tool(name='stock', description='Stok per akun dan satuan (PCS/IDR tidak dijumlah).', annotations=READ_ONLY)
def stock(ticket: str, account: str = 'self', outlet_id: int | None = None, sales_id: int | None = None,
          product_id: int | None = None, as_of: str | None = None) -> str:
    return _call('stock', ticket, {'account': account, 'outlet_id': outlet_id, 'sales_id': sales_id, 'product_id': product_id,
                                   'as_of': as_of})


@mcp.tool(name='target', description='Realisasi vs target harian sales pada satu channel.', annotations=READ_ONLY)
def target(ticket: str, channel: str, target_date: str | None = None, sales_id: int | None = None) -> str:
    return _call('target', ticket, {'channel': channel, 'target_date': target_date, 'sales_id': sales_id})


@mcp.tool(name='resolve_entity', description='Cari outlet/kecamatan/kab-kota/provinsi dalam cakupan izin.', annotations=READ_ONLY)
def resolve_entity(ticket: str, query: str, scope: str | None = None) -> str:
    return _call('resolve_entity', ticket, {'query': query, 'scope': scope})


class BridgeAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == '/health':
            return JSONResponse({'status': 'ok'})
        expected = os.environ.get('MCP_BRIDGE_TOKEN', '')
        got = request.headers.get('authorization', '')[7:]
        if not expected or not hmac.compare_digest(got.encode(), expected.encode()):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        return await call_next(request)


def build_app():
    from mcp.server.transport_security import TransportSecuritySettings
    hosts = [h.strip() for h in os.environ.get('MCP_ALLOWED_HOSTS', '127.0.0.1:*,localhost:*,mcp-bridge:*').split(',') if h.strip()]
    app = mcp.streamable_http_app(streamable_http_path='/mcp', stateless_http=True, json_response=True,
                                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts))
    app.add_middleware(BridgeAuth)
    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(build_app(), host=os.environ.get('MCP_HOST', '127.0.0.1'), port=int(os.environ.get('MCP_PORT', '8765')))

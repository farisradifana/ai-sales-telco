"""In-process mocks of external services (no network, no real WhatsApp). Hermes mock mimics the verified 0.20.5 API
surface and calls business tools the way the MCP bridge does (ticket + service token)."""
import hashlib
import hmac
import json
import re

import httpx

from telco_api import narrative
from telco_api.money import json_loads


class Mocks:
    def __init__(self):
        self.wa_sent: list[dict] = []
        self.wa_fail: list[int] = []
        self.n8n_received: list[dict] = []
        self.hermes_mode = 'good'
        self.hermes_calls = 0
        self.router_calls = 0
        self.api = None
        self.settings = None

    def bind_api(self, client, settings):
        self.api, self.settings = client, settings

    # ---------------------------------------------------------------- WhatsApp Graph
    def wa_handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == 'mock-wa', 'tests must never reach a real Graph host'
        if self.wa_fail:
            return httpx.Response(self.wa_fail.pop(0), json={'error': {'message': 'mock failure'}})
        body = json.loads(request.content)
        assert body['to'].startswith('0'), 'demo recipients must be non-routable test ids'
        self.wa_sent.append(body)
        return httpx.Response(200, json={'messages': [{'id': f'wamid.MOCK{len(self.wa_sent)}'}]})

    # ---------------------------------------------------------------- n8n webhook
    def n8n_handler(self, request: httpx.Request) -> httpx.Response:
        sig = request.headers['x-telco-signature'][7:]
        good = hmac.new(self.settings.n8n_dispatch_secret.get_secret_value().encode(), request.content, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return httpx.Response(401)
        self.n8n_received.append(json.loads(request.content))
        return httpx.Response(200, json={'message': 'Workflow was started'}, headers={'x-n8n-execution-id': str(len(self.n8n_received))})

    # ---------------------------------------------------------------- tool call like the MCP bridge
    def _tool(self, name: str, ticket: str, args: dict) -> dict:
        r = self.api.post(f'/internal/v1/agent-tools/{name}', json=args,
                          headers={'Authorization': f'Bearer {self.settings.mcp_service_token.get_secret_value()}', 'X-Agent-Ticket': ticket})
        return json_loads(r.content) if r.status_code == 200 else {'error': r.json()['error']['code']}

    @staticmethod
    def _parse_system(text: str):
        return re.search(r'ticket=(\S+)', text).group(1), re.search(r'evidence_id=(\S+)', text).group(1)

    # ---------------------------------------------------------------- Hermes API server
    def hermes_handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == '/health':
            return httpx.Response(200, json={'status': 'ok'})
        if request.headers.get('authorization') != f'Bearer {self.settings.hermes_api_key.get_secret_value()}':
            return httpx.Response(401)
        if path == '/v1/capabilities':
            return httpx.Response(200, json={'object': 'hermes.api_server.capabilities', 'platform': 'hermes-agent', 'model': 'hermes-agent',
                                             'features': {'chat_completions': True, 'responses_api': True}})
        if path == '/v1/toolsets':
            enabled = self.hermes_mode == 'toolset_violation'
            return httpx.Response(200, json=[{'name': 'terminal', 'enabled': enabled, 'tools': ['terminal']},
                                             {'name': 'file', 'enabled': False, 'tools': ['read_file']}])
        if path == '/v1/chat/completions':
            self.hermes_calls += 1
            if self.hermes_mode == 'down':
                return httpx.Response(503)
            body = json.loads(request.content)
            ticket, eid = self._parse_system(body['messages'][0]['content'])
            e = self._tool('get_evidence', ticket, {'evidence_id': eid})
            if 'error' in e:
                text = 'Maaf, data tidak tersedia.'
            else:
                text = narrative.diagnose_text(e)
                if self.hermes_mode == 'fabricate':
                    text += '\nPerkiraan uplift kampanye Rp987.654.321 (confidence 87%).'
            return httpx.Response(200, json={'id': 'chatcmpl-mock', 'object': 'chat.completion', 'model': 'hermes-agent',
                                             'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]})
        return httpx.Response(404)

    # ---------------------------------------------------------------- 9Router (OpenAI-compatible, tool calling)
    def router_handler(self, request: httpx.Request) -> httpx.Response:
        self.router_calls += 1
        body = json.loads(request.content)
        assert any(t['function']['name'] == 'get_evidence' for t in body['tools'])
        assert not any(t['function']['name'] in ('execute_sql', 'terminal', 'shell') for t in body['tools'])
        msgs = body['messages']
        tool_msgs = [m for m in msgs if m['role'] == 'tool']
        if not tool_msgs:
            ticket, eid = self._parse_system(msgs[0]['content'])
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call_1', 'type': 'function', 'function': {'name': 'get_evidence',
                                                                  'arguments': json.dumps({'ticket': ticket, 'evidence_id': eid})}}]}}]})
        e = json_loads(tool_msgs[-1]['content'])
        text = narrative.diagnose_text(e) if 'evidence_id' in e else 'Data tidak tersedia.'
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': text}}]})

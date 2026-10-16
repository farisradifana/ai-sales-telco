"""Local HTTP mocks for the process-level E2E (scripts/e2e_local.py). Never contact real WhatsApp or model vendors.

  python -m tests.mock_servers wa  --port 8790   # WhatsApp Graph API: records sends to .local/mock_wa_sent.jsonl
  python -m tests.mock_servers llm --port 8791   # OpenAI-compatible provider used by Hermes (as 9Router stand-in)

The mock LLM is scripted: first turn -> calls the *get_evidence tool with ticket/evidence_id from the system prompt;
after the tool result -> answers with the deterministic narrative built only from that evidence JSON."""
import argparse
import json
import os
import re
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

SENT = Path('.local/mock_wa_sent.jsonl')


def wa_app() -> FastAPI:
    app = FastAPI()

    @app.post('/{version}/{phone_id}/messages')
    async def send(version: str, phone_id: str, request: Request):
        body = await request.json()
        if not str(body.get('to', '')).startswith('0'):
            return JSONResponse({'error': {'message': 'mock refuses routable numbers'}}, status_code=400)
        SENT.parent.mkdir(exist_ok=True)
        with SENT.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'at': time.time(), **body}, ensure_ascii=False) + '\n')
        return {'messaging_product': 'whatsapp', 'messages': [{'id': f'wamid.MOCK{int(time.time() * 1000)}'}]}

    @app.get('/_sent')
    def sent():
        return [json.loads(x) for x in SENT.read_text(encoding='utf-8').splitlines()] if SENT.exists() else []
    return app


def llm_app() -> FastAPI:
    from telco_api import narrative
    from telco_api.money import json_loads
    app = FastAPI()
    calls = {'n': 0}

    @app.get('/v1/models')
    def models():
        return {'object': 'list', 'data': [{'id': 'mock-model', 'object': 'model', 'owned_by': 'mock'}]}

    @app.post('/v1/chat/completions')
    async def chat(request: Request):
        calls['n'] += 1
        body = await request.json()
        if os.environ.get('MOCK_LLM_DUMP'):
            Path(os.environ['MOCK_LLM_DUMP'] + f'.{calls["n"]}').write_text(json.dumps(body, ensure_ascii=False)[:200000], encoding='utf-8')
        msgs = body.get('messages', [])
        system = '\n'.join(m.get('content') or '' for m in msgs if m.get('role') == 'system' and isinstance(m.get('content'), str))
        tools = [t['function']['name'] for t in body.get('tools', [])]
        tool_results = [m for m in msgs if m.get('role') == 'tool']
        ev_tool = next((t for t in tools if t.endswith('get_evidence')), None)
        t = re.search(r'ticket=(\S+)', system)
        e = re.search(r'evidence_id=(\S+)', system)
        if ev_tool and t and e and not tool_results:
            msg = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': f'call_{calls["n"]}', 'type': 'function', 'function': {
                'name': ev_tool, 'arguments': json.dumps({'ticket': t.group(1), 'evidence_id': e.group(1)})}}]}
            finish = 'tool_calls'
        else:
            text = 'Data tidak tersedia.'
            for m in reversed(tool_results):
                content = m.get('content')
                if isinstance(content, list):
                    content = ''.join(c.get('text', '') for c in content if isinstance(c, dict))
                if isinstance(content, str) and '<untrusted_tool_result' in content:
                    # Hermes wraps tool output in an untrusted-data envelope; the payload is the JSON inside it.
                    content = content[content.find('{'):content.rfind('}') + 1]
                try:
                    ev = json_loads(content)
                    # MCP structured output arrives wrapped as {"result": "<tool text>"}.
                    while isinstance(ev, dict) and isinstance(ev.get('result'), str) and 'evidence_id' not in ev:
                        ev = json_loads(ev['result'])
                    if isinstance(ev, dict) and 'evidence_id' in ev:
                        text = narrative.diagnose_text(ev)
                        break
                except (ValueError, TypeError):
                    continue
            msg, finish = {'role': 'assistant', 'content': text}, 'stop'
        cid = f'chatcmpl-mock-{calls["n"]}'
        if body.get('stream'):
            def sse():
                base = {'id': cid, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': body.get('model', 'mock-model')}
                if msg.get('tool_calls'):
                    tc = msg['tool_calls'][0]
                    delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': tc['id'], 'type': 'function',
                                                                  'function': {'name': tc['function']['name'], 'arguments': tc['function']['arguments']}}]}
                else:
                    delta = {'role': 'assistant', 'content': msg['content']}
                yield 'data: ' + json.dumps({**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}) + '\n\n'
                yield 'data: ' + json.dumps({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
                                            'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}) + '\n\n'
                yield 'data: [DONE]\n\n'
            return StreamingResponse(sse(), media_type='text/event-stream')
        return {'id': cid, 'object': 'chat.completion', 'created': int(time.time()), 'model': body.get('model', 'mock-model'),
                'choices': [{'index': 0, 'message': msg, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
    return app


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('kind', choices=['wa', 'llm'])
    ap.add_argument('--port', type=int, required=True)
    a = ap.parse_args()
    uvicorn.run(wa_app() if a.kind == 'wa' else llm_app(), host='127.0.0.1', port=a.port, log_level='warning')

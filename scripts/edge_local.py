"""Local public edge for the Cloudflare tunnel: forwards ONLY the WhatsApp webhook and signed chart links to the API.
Everything else (dev-login, /v1, /internal) returns 404, mirroring deploy/edge/Caddyfile.

  python scripts/edge_local.py --port 8089 --api http://127.0.0.1:8000
"""
import argparse

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

MAX_BODY = 256 * 1024


def build(api: str) -> Starlette:
    client = httpx.AsyncClient(base_url=api, timeout=15)

    async def forward(request: Request) -> Response:
        body = await request.body()
        if len(body) > MAX_BODY:
            return PlainTextResponse('payload too large', status_code=413)
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() in ('content-type', 'x-hub-signature-256', 'user-agent')}
        r = await client.request(request.method, request.url.path, params=request.query_params, content=body, headers=headers)
        keep = {k: v for k, v in r.headers.items() if k.lower() in ('content-type', 'x-correlation-id', 'cache-control')}
        return Response(r.content, status_code=r.status_code, headers=keep)

    async def not_found(_: Request) -> Response:
        return PlainTextResponse('not found', status_code=404)

    return Starlette(routes=[
        Route('/webhooks/whatsapp', forward, methods=['GET', 'POST']),
        Route('/charts/{name:path}', forward, methods=['GET']),
        Route('/{rest:path}', not_found, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH']),
    ])


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8089)
    ap.add_argument('--api', default='http://127.0.0.1:8000')
    a = ap.parse_args()
    uvicorn.run(build(a.api), host='127.0.0.1', port=a.port, log_level='warning')

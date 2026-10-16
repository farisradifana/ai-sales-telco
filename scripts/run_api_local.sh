#!/usr/bin/env bash
# Local API on 127.0.0.1:8000 against a disposable DB (default telco_test_dev). Mock-only external services.
set -euo pipefail
cd "$(dirname "$0")/.."
DB=${DB:-telco_test_dev3} . scripts/devenv.sh
export READER_DATABASE_URL WRITER_DATABASE_URL IDENTITY_DATABASE_URL
export WA_SEND_MODE=${LOCAL_WA_SEND_MODE:-mock} WA_GRAPH_BASE_URL=${WA_GRAPH_BASE_URL:-http://127.0.0.1:8790}
export N8N_DISPATCH_URL=${N8N_DISPATCH_URL:-http://127.0.0.1:${LOCAL_N8N_PORT:-5688}/webhook/telco-inbound}
export HERMES_BASE_URL=${HERMES_BASE_URL:-http://127.0.0.1:8642} ROUTER_BASE_URL=${ROUTER_BASE_URL:-http://127.0.0.1:8791} FALLBACK_MODEL_ID=${FALLBACK_MODEL_ID:-mock-model}
export CORS_ORIGINS='' PYTHONPATH=.
exec .venv/Scripts/python -m uvicorn telco_api.main:app_factory --factory --host 127.0.0.1 --port 8000 --no-access-log

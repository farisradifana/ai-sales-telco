# API, MCP bridge and one-shot jobs (migrate/load/provision) share this image; entrypoint chosen per service.
FROM python:3.12.15-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements-api.lock ./
RUN pip install --require-hashes -r requirements-api.lock && useradd --uid 10001 --create-home app
COPY telco ./telco
COPY telco_api ./telco_api
COPY telco_mcp ./telco_mcp
COPY sql ./sql
COPY data/reference ./data/reference
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live',timeout=2).status==200 else 1)"
CMD ["uvicorn", "telco_api.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers"]

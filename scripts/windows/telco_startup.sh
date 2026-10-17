#!/usr/bin/env bash
# Start the live WhatsApp stack on this PC in dependency order, then keep it alive (watchdog).
# Launched hidden at Windows logon by scripts/windows/telco-startup.vbs (copied into the user's Startup folder).
#
#   PostgreSQL :55432 -> 9Router :20128 -> API :8000 (LIVE) -> edge :8089 -> n8n :5688 -> named tunnel telco-wa
#
# A service is (re)started only when its port is not listening AND the process this script started for it is gone,
# so a slow starter (n8n needs several minutes on Windows) is never launched twice. Running this script twice is harmless.
# Logs: .local/startup.log (this script) + one log per service in .local/. Stop everything: scripts/windows/telco_stop.sh
set -uo pipefail
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
LOG=.local/startup.log
RUN=.local/run
mkdir -p "$RUN"
PUBLIC_URL=${TELCO_PUBLIC_URL:-https://telco-wa.farisradifana.my.id}
TUNNEL_CFG=.local/cloudflared/telco-wa.yml
CLOUDFLARED=.local/download/cloudflared/cloudflared.exe
START_ROUTER=${TELCO_START_ROUTER:-1}   # 0 = 9Router is managed elsewhere
WATCH_S=${TELCO_WATCH_S:-30}

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }
listening() { netstat -ano 2>/dev/null | grep -E "(127\.0\.0\.1|0\.0\.0\.0|\[::\]):$1 " | grep -q LISTENING; }
launched() { [ -f "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }   # our process still alive?
wait_port() {  # name port timeout_s
  for _ in $(seq 1 "$3"); do listening "$2" && { log "OK   $1 :$2"; return 0; }; sleep 1; done
  log "WAIT $1 :$2 not listening after $3s (still starting? see its log)"; return 1
}

start_postgres() {
  listening 55432 && return 0
  log "start PostgreSQL"
  .local/pgsql/bin/pg_ctl.exe -D .local/pgdata -l .local/pg.log -w -t 60 start >> "$LOG" 2>&1
  wait_port PostgreSQL 55432 60
}

start_router() {
  [ "$START_ROUTER" = 1 ] || return 0
  listening 20128 || launched router && return 0
  log "start 9Router (tray, no browser)"
  nohup "$APPDATA/npm/9router.cmd" --tray --no-browser > .local/9router.log 2>&1 &
  echo $! > "$RUN/router.pid"
  wait_port 9Router 20128 90
}

start_api() {
  listening 8000 || launched api && return 0
  log "start API (LIVE, PUBLIC_BASE_URL=$PUBLIC_URL)"
  LIVE=1 AGENT_TIMEOUT_S=90 PUBLIC_BASE_URL="$PUBLIC_URL" nohup bash scripts/run_api_local.sh > .local/api_live.log 2>&1 &
  echo $! > "$RUN/api.pid"
  wait_port API 8000 90
}

start_edge() {
  listening 8089 || launched edge && return 0
  log "start edge (only /webhooks/whatsapp + /charts/*)"
  nohup .venv/Scripts/python scripts/edge_local.py --port 8089 --api http://127.0.0.1:8000 > .local/edge.log 2>&1 &
  echo $! > "$RUN/edge.pid"
  wait_port edge 8089 30
}

start_n8n() {
  listening 5688 || launched n8n && return 0
  log "start n8n (first start can take ~5 minutes)"
  (
    set -a; . .local/dev.env; set +a
    export N8N_USER_FOLDER="$ROOT/.local/n8n-e2e/home" N8N_DIAGNOSTICS_ENABLED=false N8N_ENCRYPTION_KEY N8N_PORT=5688 \
      N8N_LISTEN_ADDRESS=127.0.0.1 N8N_RUNNERS_BROKER_PORT=5689 GENERIC_TIMEZONE=Asia/Jakarta \
      EXECUTIONS_DATA_SAVE_ON_SUCCESS=all EXECUTIONS_DATA_PRUNE=true EXECUTIONS_DATA_MAX_AGE=24
    nohup "$APPDATA/npm/n8n.cmd" start > .local/n8n-e2e/n8n.log 2>&1 &
    echo $! > "$RUN/n8n.pid"
  )
  wait_port n8n 5688 600 || return 1
  # The port opens minutes before workflows are activated; 404 = webhook not registered yet, 403 = active (needs auth).
  for _ in $(seq 1 120); do
    c=$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:5688/webhook/telco-inbound)
    [ "$c" != 404 ] && [ "$c" != 503 ] && [ "$c" != 000 ] && { log "OK   n8n workflows active (telco-inbound HTTP $c)"; return 0; }
    sleep 5
  done
  log "WAIT n8n webhook telco-inbound not registered after 10 min (workflow published?)"
}

start_tunnel() {  # 9Router runs its own cloudflared, so the tunnel is tracked by pid, not by image name
  launched tunnel && return 0
  [ -f "$TUNNEL_CFG" ] || { log "FAIL tunnel config $TUNNEL_CFG missing"; return 1; }
  log "start tunnel telco-wa -> $PUBLIC_URL"
  nohup "$CLOUDFLARED" tunnel --no-autoupdate --config "$TUNNEL_CFG" run telco-wa > .local/tunnel_named.log 2>&1 &
  echo $! > "$RUN/tunnel.pid"
  for _ in $(seq 1 30); do grep -q "Registered tunnel connection" .local/tunnel_named.log && { log "OK   tunnel telco-wa"; return 0; }; sleep 1; done
  log "WAIT tunnel not registered yet (see .local/tunnel_named.log)"
}

start_all() { start_postgres && start_router; start_api; start_edge; start_tunnel; start_n8n; }

PIDFILE=$RUN/startup.pid
if launched startup; then log "another startup/watchdog is running (pid $(cat "$PIDFILE")); exit"; exit 0; fi
echo $$ > "$PIDFILE"
log "=== startup (pid $$) ==="
sleep "${TELCO_BOOT_DELAY_S:-20}"   # let the network and Windows services settle after logon
start_all
log "=== startup done; watchdog every ${WATCH_S}s ==="
while true; do
  sleep "$WATCH_S"
  for p in 55432 8000 8089 5688; do listening $p || { start_all; break; }; done
  start_router
  start_tunnel
done

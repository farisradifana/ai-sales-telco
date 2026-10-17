#!/usr/bin/env bash
# Stop the stack started by telco_startup.sh (watchdog first, so nothing is restarted).
# 9Router (and its own cloudflared) is left running unless --all.
set -uo pipefail
cd "$(dirname "$0")/../.."
RUN=.local/run
[ -f $RUN/startup.pid ] && kill "$(cat $RUN/startup.pid)" 2>/dev/null && echo "watchdog stopped"
# Only the telco-wa tunnel, never 9Router's quick tunnel.
powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | Where-Object { \$_.CommandLine -match 'telco-wa' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force; 'tunnel telco-wa stopped' }"
ports="5688 8089 8000"
[ "${1:-}" = --all ] && ports="$ports 20128"
for p in $ports; do
  pid=$(netstat -ano | grep -E "(127\.0\.0\.1|0\.0\.0\.0):$p " | grep LISTENING | awk '{print $5}' | head -1)
  [ -n "$pid" ] && taskkill //PID "$pid" //T //F > /dev/null 2>&1 && echo "port $p stopped"
done
.local/pgsql/bin/pg_ctl.exe -D .local/pgdata -m fast stop > /dev/null 2>&1 && echo "PostgreSQL stopped"
rm -f $RUN/*.pid

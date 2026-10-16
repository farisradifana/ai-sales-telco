#!/usr/bin/env bash
# Build a clean, fully loaded disposable template DB: create -> migrate (x2) -> load 500k -> validate -> refuse 2nd load
# -> security provisioning -> demo principals. Usage: DB=telco_test_jatim_clean bash scripts/build_template.sh
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/devenv.sh
case "$DB" in telco_test_*) ;; *) echo "refusing: DB must be a disposable telco_test_* database"; exit 2;; esac
$PSQL "postgresql://telco_owner:${PGOWNER_PW}@127.0.0.1:55432/postgres" -c "CREATE DATABASE $DB"
export DATABASE_URL="$ADMIN_DATABASE_URL"
.venv/Scripts/python -m telco.db migrate && .venv/Scripts/python -m telco.db migrate
time .venv/Scripts/python -m telco.db load --data data/full
.venv/Scripts/python -m telco.db validate
if .venv/Scripts/python -m telco.db load --data data/full 2>/dev/null; then echo "second load unexpectedly accepted"; exit 1; else echo "second load refused: OK"; fi
$PSQL "$ADMIN_DATABASE_URL" -f sql/security.sql
$PSQL "$ADMIN_DATABASE_URL" -f sql/security_platform.sql 2>&1 | grep -v NOTICE || true
$PSQL "$ADMIN_DATABASE_URL" -f sql/validate_platform.sql
PYTHONPATH=. .venv/Scripts/python -m telco_api.provision demo
echo "TEMPLATE-READY $DB"

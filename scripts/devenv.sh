# Source me (bash): exports local dev env for the disposable cluster on 127.0.0.1:55432.
# Usage: DB=telco_test_full . scripts/devenv.sh
set -a; . .local/dev.env; set +a
PGOWNER_PW=$(cat .local/pgpass.txt)
DB=${DB:-telco_test_full}
export ADMIN_DATABASE_URL="postgresql://telco_owner:${PGOWNER_PW}@127.0.0.1:55432/${DB}"
# DATABASE_URL is NOT exported: the reference tests (tests/test_postgres.py) write permanent fixtures into whatever it points at.
export READER_DATABASE_URL="postgresql://telco_api_reader:${TELCO_API_READER_PASSWORD}@127.0.0.1:55432/${DB}"
export WRITER_DATABASE_URL="postgresql://telco_api_writer:${TELCO_API_WRITER_PASSWORD}@127.0.0.1:55432/${DB}"
export IDENTITY_DATABASE_URL="postgresql://telco_api_identity:${TELCO_API_IDENTITY_PASSWORD}@127.0.0.1:55432/${DB}"
export OPS_DATABASE_URL="postgresql://telco_ops_login:${TELCO_OPS_PASSWORD}@127.0.0.1:55432/${DB}"
export APP_ENV=local DEV_LOGIN_ENABLED=true WA_SEND_MODE=${WA_SEND_MODE:-disabled} WA_PHONE_NUMBER_ID=000000000000000
export PUBLIC_BASE_URL=${PUBLIC_BASE_URL:-http://127.0.0.1:8000}
export PSQL="$(pwd)/.local/pgsql/bin/psql -v ON_ERROR_STOP=1 -q"

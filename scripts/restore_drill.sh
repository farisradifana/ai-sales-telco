#!/usr/bin/env bash
# Backup/restore drill on the disposable local cluster: logical dump -> restore into a NEW database -> run SQL audits ->
# compare row counts and content fingerprints with the source. Never targets non-telco_test_* databases.
# Usage: SRC=telco_test_jatim_clean bash scripts/restore_drill.sh   (writes reports/restore_drill.json)
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/devenv.sh
SRC=${SRC:?set SRC}; DST=telco_test_restore_$(date +%Y%m%d%H%M%S)
case "$SRC" in telco_test_*) ;; *) echo "refusing non-test source"; exit 2;; esac
BIN=.local/pgsql/bin; OWNER="postgresql://telco_owner:${PGOWNER_PW}@127.0.0.1:55432"
DUMP=.local/backup/${SRC}.dump; mkdir -p .local/backup
t0=$(date +%s)
"$BIN/pg_dump" --format=custom --no-owner --file="$DUMP" "$OWNER/$SRC"
t1=$(date +%s)
$PSQL "$OWNER/postgres" -c "CREATE DATABASE $DST"
# Roles are cluster-level (created by provision roles); grants are restored from the dump's ACLs.
"$BIN/pg_restore" --exit-on-error --no-owner --role=telco_owner --dbname="$OWNER/$DST" "$DUMP"
t2=$(date +%s)
$PSQL "$OWNER/$DST" -f sql/validate.sql
$PSQL "$OWNER/$DST" -f sql/validate_platform.sql
FP="SELECT json_build_object(
 'sell_in',(SELECT count(*) FROM telco.sell_in),'sell_out',(SELECT count(*) FROM telco.sell_out),
 'ledger',(SELECT count(*) FROM telco.stock_ledger),'balance_sum_pcs',(SELECT sum(b.quantity) FROM telco.stock_balance b JOIN telco.product p ON p.id=b.product_id WHERE p.stock_unit='PCS'),
 'revenue_sell_out',(SELECT sum(net_revenue) FROM telco.sell_out),'districts',(SELECT count(*) FROM telco.district),
 'migrations',(SELECT string_agg(version||':'||left(sha256,12),',' ORDER BY version) FROM public.telco_migration),
 'grants',(SELECT count(*) FROM telco.principal_grant),'wa_identities',(SELECT count(*) FROM telco_identity.wa_identity),
 'sell_out_md5',(SELECT md5(string_agg(id||':'||net_revenue,',' ORDER BY id)) FROM telco.sell_out))"
A=$($PSQL "$OWNER/$SRC" -Atc "$FP"); B=$($PSQL "$OWNER/$DST" -Atc "$FP")
SIZE=$(stat -c %s "$DUMP")
.venv/Scripts/python - "$A" "$B" "$SRC" "$DST" "$((t1-t0))" "$((t2-t1))" "$SIZE" <<'EOF'
import json, sys
a, b = json.loads(sys.argv[1]), json.loads(sys.argv[2])
r = {'source': sys.argv[3], 'restored_to': sys.argv[4], 'dump_seconds': int(sys.argv[5]), 'restore_seconds': int(sys.argv[6]),
     'dump_bytes': int(sys.argv[7]), 'fingerprints_equal': a == b, 'source_fp': a, 'restored_fp': b,
     'sql_audits': 'validate.sql + validate_platform.sql passed on restored copy',
     'scope': 'Local logical backup drill on PostgreSQL 17.11. NOT a cloud PITR/RPO/RTO proof.'}
json.dump(r, open('reports/restore_drill.json', 'w'), indent=2)
print(json.dumps({k: r[k] for k in ('fingerprints_equal', 'dump_seconds', 'restore_seconds', 'dump_bytes', 'restored_to')}))
sys.exit(0 if a == b else 1)
EOF

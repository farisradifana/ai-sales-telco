-- OPTIONAL provision step, run by a role with CREATEROLE after migrations.
-- Dedicated single-company database. API enforces principal->outlet/district authorization.
-- Do NOT expose these database credentials or arbitrary SQL to n8n/Hermes/dashboard/browser.
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='telco_reader') THEN CREATE ROLE telco_reader NOLOGIN; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='telco_writer') THEN CREATE ROLE telco_writer NOLOGIN; END IF;
END $$;
REVOKE ALL ON SCHEMA telco FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA telco FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA telco FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA telco REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
GRANT USAGE ON SCHEMA telco TO telco_reader,telco_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA telco TO telco_reader;
REVOKE SELECT ON telco.conversation_context,telco.analysis_audit,telco.inbound_event,telco.outbox FROM telco_reader;
GRANT EXECUTE ON FUNCTION telco.diagnose(text,integer,text,date,date,date,date),
 telco.rank_districts(text,date,date,date,date) TO telco_reader;
GRANT EXECUTE ON FUNCTION telco.can_access(text,text,integer),telco.stock_as_of(integer,integer,timestamptz) TO telco_reader;
-- Area scope (migration 009): regency/province aggregates, default-deny area check.
GRANT EXECUTE ON FUNCTION telco.can_access_area(text,text,integer),telco.diagnose_area(text,integer,text,date,date,date,date),
 telco.rank_regencies(text,date,date,date,date) TO telco_reader;
-- Posting is the only route to mutate ledger/balance. Functions must remain owned by trusted migration owner.
ALTER FUNCTION telco.post_supply() SECURITY DEFINER;
ALTER FUNCTION telco.post_sale() SECURITY DEFINER;
ALTER FUNCTION telco.post_reversal() SECURITY DEFINER;
GRANT INSERT,SELECT ON telco.supply,telco.sell_in,telco.sell_out,telco.reversal TO telco_writer;
GRANT SELECT,INSERT,UPDATE ON telco.conversation_context,telco.inbound_event,telco.outbox TO telco_writer;
GRANT INSERT,SELECT ON telco.analysis_audit TO telco_writer;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA telco TO telco_writer;
-- Grant these group roles to separately created LOGIN roles via your cloud IAM/secret provisioning.
-- Never grant membership of migration owner or superuser to application roles.

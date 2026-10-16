-- Provision step AFTER sql/security.sql and migration 005. Run by an admin with CREATEROLE (not by the API).
-- Group roles only (NOLOGIN). LOGIN roles + passwords are created by `python -m telco_api.provision roles`
-- from secret-manager values; passwords never live in SQL files.
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='telco_identity_rw') THEN CREATE ROLE telco_identity_rw NOLOGIN; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='telco_ops') THEN CREATE ROLE telco_ops NOLOGIN; END IF;
END $$;
SET search_path=telco,public;

-- security.sql granted SELECT ON ALL TABLES at its run time; re-tighten the reader for platform state tables.
REVOKE ALL ON telco.pending_action,telco.action_audit,telco.agent_turn,telco.import_file,telco.import_row,
 telco.conversation_pointer,telco.evidence_snapshot,telco.posting_request,telco.correction_audit,telco.grant_audit
 FROM telco_reader;
GRANT SELECT ON telco.app_principal,telco.price_policy,telco.notification_pref,telco.business_holiday,
 telco.stock_transfer,telco.principal_grant TO telco_reader;
GRANT EXECUTE ON FUNCTION telco.min_running_balance(integer,integer) TO telco_reader;

-- Writer: domain posting + platform state. Inherits reader SELECTs; never owner, never CREATE.
GRANT telco_reader TO telco_writer;
GRANT SELECT,INSERT ON telco.evidence_snapshot,telco.posting_request,telco.action_audit,telco.correction_audit,
 telco.stock_transfer TO telco_writer;
GRANT SELECT,INSERT,UPDATE ON telco.pending_action,telco.conversation_pointer,telco.agent_turn,
 telco.import_file,telco.import_row,telco.notification_pref TO telco_writer;
GRANT SELECT,INSERT,UPDATE ON telco.conversation_context,telco.inbound_event,telco.outbox TO telco_writer;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA telco TO telco_writer;
ALTER FUNCTION telco.post_transfer() SECURITY DEFINER;
GRANT EXECUTE ON FUNCTION telco.lock_stock_accounts(integer[]) TO telco_writer;

-- Identity mapping: separate schema, only identity role (API identity resolver) can read ciphertext/HMAC.
REVOKE ALL ON SCHEMA telco_identity FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA telco_identity FROM PUBLIC;
GRANT USAGE ON SCHEMA telco_identity TO telco_identity_rw;
GRANT SELECT,INSERT,UPDATE ON telco_identity.wa_identity TO telco_identity_rw;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA telco_identity TO telco_identity_rw;
GRANT USAGE ON SCHEMA telco TO telco_identity_rw;
GRANT SELECT ON telco.app_principal TO telco_identity_rw;

-- Ops/admin CLI: provisioning principals/grants/policy/targets and evidenced receipts. No DDL.
GRANT telco_writer TO telco_ops;
GRANT SELECT,INSERT,UPDATE ON telco.app_principal,telco.price_policy,telco.business_holiday TO telco_ops;
GRANT SELECT,INSERT,DELETE ON telco.principal_grant TO telco_ops;
GRANT SELECT,INSERT ON telco.grant_audit,telco.supply,telco.sales_target TO telco_ops;
GRANT telco_identity_rw TO telco_ops;

-- 010: AI chat quota + n8n agent memory. The memory role reaches ONLY schema agent_memory (no telco objects).
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='telco_agent_memory') THEN CREATE ROLE telco_agent_memory NOLOGIN; END IF;
END $$;
GRANT SELECT ON telco.chat_quota_policy TO telco_reader;
GRANT SELECT,INSERT,UPDATE ON telco.chat_quota_usage TO telco_writer;
GRANT UPDATE ON telco.chat_quota_policy TO telco_ops;
REVOKE ALL ON SCHEMA agent_memory FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA agent_memory FROM PUBLIC;
-- USAGE+CREATE: LangChain issues CREATE TABLE IF NOT EXISTS before every read; the table already exists (migration 010).
GRANT USAGE,CREATE ON SCHEMA agent_memory TO telco_agent_memory;
GRANT SELECT,INSERT ON agent_memory.chat_history TO telco_agent_memory;
GRANT USAGE,SELECT ON SEQUENCE agent_memory.chat_history_id_seq TO telco_agent_memory;
-- API writer prunes memory past retention (and per-session overflow); it never reads conversation content.
GRANT USAGE ON SCHEMA agent_memory TO telco_writer;
GRANT DELETE ON agent_memory.chat_history TO telco_writer;
GRANT SELECT(id,session_id,created_at) ON agent_memory.chat_history TO telco_writer;

-- n8n AI Agent support: daily chat quota per org level and the agent's conversation memory.
--   * Quota counts AI conversations only (deterministic commands such as sales input / YA / BATAL are free).
--     One row per inbound message (idempotent under n8n retries); a failed model turn is refunded.
--   * Memory lives in its own schema so the n8n memory login role can touch NOTHING in schema telco.
SET search_path=telco,public;

CREATE TABLE chat_quota_policy(
 level text PRIMARY KEY CHECK(level IN ('head_of_region','head_of_sales','head_of_branch','territory_manager','sales','default')),
 daily_limit integer NOT NULL CHECK(daily_limit BETWEEN 0 AND 1000),
 updated_at timestamptz NOT NULL DEFAULT now());
-- 'default' = principals without an org position (admin, service, viewer): no AI chat over WhatsApp.
INSERT INTO chat_quota_policy(level,daily_limit) VALUES
 ('sales',3),('territory_manager',3),('head_of_branch',5),('head_of_sales',10),('head_of_region',20),('default',0);

CREATE TABLE chat_quota_usage(
 principal_id text NOT NULL REFERENCES app_principal,
 message_ref text NOT NULL,
 day_wib date NOT NULL,
 used_at timestamptz NOT NULL DEFAULT now(),
 refunded_at timestamptz,
 PRIMARY KEY(principal_id,message_ref));
CREATE INDEX chat_quota_usage_day ON chat_quota_usage(principal_id,day_wib) WHERE refunded_at IS NULL;

-- Layout matches LangChain PostgresChatMessageHistory (id, session_id, message) used by n8n "Postgres Chat Memory",
-- plus created_at for retention pruning. session_id is "wa:<principal_id>" (never a phone number).
CREATE SCHEMA agent_memory;
CREATE TABLE agent_memory.chat_history(
 id serial PRIMARY KEY,
 session_id varchar(255) NOT NULL,
 message jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX chat_history_session ON agent_memory.chat_history(session_id,id);
CREATE INDEX chat_history_created ON agent_memory.chat_history(created_at);

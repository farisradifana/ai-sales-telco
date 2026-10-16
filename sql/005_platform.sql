-- Platform tables for API/WhatsApp/n8n/agent. Additive only: existing objects from 001-004 are untouched
-- except (a) new nullable columns, (b) widening stock_ledger.source_kind to accept 'transfer'.
SET search_path=telco,public;

-- Server-verified principals. No phone number / email here; WhatsApp mapping lives in telco_identity.
CREATE TABLE app_principal(
 principal_id text PRIMARY KEY CHECK(principal_id ~ '^[a-z0-9][a-z0-9._:-]{2,63}$'),
 role text NOT NULL CHECK(role IN ('sales','supervisor','area_manager','admin')),
 sales_id integer UNIQUE REFERENCES sales,
 display_name text NOT NULL,
 active boolean NOT NULL DEFAULT true,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK((role='sales')=(sales_id IS NOT NULL)));

CREATE TABLE grant_audit(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, principal_id text NOT NULL,
 scope_type text NOT NULL, entity_id integer NOT NULL, action text NOT NULL CHECK(action IN ('grant','revoke')),
 actor text NOT NULL, reason text NOT NULL, at timestamptz NOT NULL DEFAULT now());

-- Immutable evidence returned by tools. Charts, narrative, context and exports reference one evidence_id.
CREATE TABLE evidence_snapshot(evidence_id uuid PRIMARY KEY, principal_id text NOT NULL, tool_name text NOT NULL,
 scope_type text, entity_id integer, channel text CHECK(channel IN ('sell_in','sell_out')),
 params jsonb NOT NULL, payload jsonb NOT NULL, digest text NOT NULL, query_version text NOT NULL,
 watermark date, batch_key text, correlation_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX evidence_principal_time ON evidence_snapshot(principal_id,created_at DESC);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON evidence_snapshot FOR EACH ROW EXECUTE FUNCTION reject_mutation();

ALTER TABLE conversation_context ADD COLUMN evidence_id uuid REFERENCES evidence_snapshot;
ALTER TABLE conversation_context ADD COLUMN watermark date;
ALTER TABLE conversation_context ADD COLUMN batch_key text;
ALTER TABLE conversation_context ADD COLUMN query_version text;
-- Opaque per-principal conversation key -> current context. No global "latest context" lookup exists.
CREATE TABLE conversation_pointer(principal_id text NOT NULL, conversation_key text NOT NULL,
 context_id text NOT NULL REFERENCES conversation_context, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(principal_id,conversation_key));

-- Retry identical => original result; same key different payload => 409.
CREATE TABLE posting_request(channel text NOT NULL CHECK(channel IN ('sell_in','sell_out','reversal','transfer')),
 external_key text NOT NULL, payload_hash text NOT NULL, principal_id text NOT NULL, transaction_id bigint NOT NULL,
 result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(channel,external_key));
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON posting_request FOR EACH ROW EXECUTE FUNCTION reject_mutation();

-- Draft -> human confirmation -> posting. Status is mutable; every transition is in action_audit.
CREATE TABLE pending_action(id uuid PRIMARY KEY, principal_id text NOT NULL,
 kind text NOT NULL CHECK(kind IN ('sell_in','sell_out','reversal','transfer')),
 payload jsonb NOT NULL, payload_hash text NOT NULL, summary text NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','posted','cancelled','expired','rejected')),
 confirm_role text NOT NULL CHECK(confirm_role IN ('self','supervisor')),
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 confirmed_by text, confirmation_event_id text, confirmation_payload_hash text, confirmed_at timestamptz,
 result jsonb, CHECK(expires_at>created_at));
CREATE INDEX pending_action_principal ON pending_action(principal_id,status,created_at DESC);
CREATE TABLE action_audit(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, action_id uuid NOT NULL REFERENCES pending_action,
 actor text NOT NULL, event text NOT NULL, event_ref text, payload_hash text, correlation_id text, at timestamptz NOT NULL DEFAULT now());
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON action_audit FOR EACH ROW EXECUTE FUNCTION reject_mutation();

-- Sale price policy per product/channel/date interval [valid_from,valid_to).
CREATE TABLE price_policy(product_id integer NOT NULL REFERENCES product, channel text NOT NULL CHECK(channel IN ('sell_in','sell_out')),
 unit_price numeric(14,4) NOT NULL CHECK(unit_price>0), max_discount_pct numeric(5,2) NOT NULL CHECK(max_discount_pct BETWEEN 0 AND 100),
 valid_from date NOT NULL, valid_to date NOT NULL CHECK(valid_to>valid_from), PRIMARY KEY(product_id,channel,valid_from));

-- Corrections that bypass the standard "no backdate" rule: always audited.
CREATE TABLE correction_audit(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, channel text NOT NULL, external_key text NOT NULL,
 occurred_at timestamptz NOT NULL, approved_by text NOT NULL, reason text NOT NULL, min_running_balance numeric NOT NULL,
 at timestamptz NOT NULL DEFAULT now());
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON correction_audit FOR EACH ROW EXECUTE FUNCTION reject_mutation();

-- Minimum chronological balance for one account+SKU (simultaneous events netted first). Used by the
-- audited correction path to prove a backdated posting never made historical stock negative.
CREATE FUNCTION min_running_balance(p_account integer,p_product integer) RETURNS numeric
LANGUAGE sql STABLE SET search_path=telco,pg_temp AS $$
 WITH d AS (SELECT occurred_at,sum(delta) delta FROM stock_ledger WHERE account_id=p_account AND product_id=p_product GROUP BY 1)
 SELECT coalesce(min(q),0) FROM (SELECT sum(delta) OVER(ORDER BY occurred_at) q FROM d) s;
$$;

-- Stock transfer between sales holders (supervisor-confirmed). New ledger source kind.
ALTER TABLE stock_ledger DROP CONSTRAINT stock_ledger_source_kind_check;
ALTER TABLE stock_ledger ADD CONSTRAINT stock_ledger_source_kind_check
 CHECK(source_kind IN ('supply','sell_in','sell_out','reversal','transfer'));
CREATE TABLE stock_transfer(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, external_key text UNIQUE NOT NULL,
 occurred_at timestamptz NOT NULL, from_account integer NOT NULL REFERENCES stock_account, to_account integer NOT NULL REFERENCES stock_account,
 product_id integer NOT NULL REFERENCES product, quantity numeric(18,2) NOT NULL CHECK(quantity>0),
 reason text NOT NULL, approved_by text NOT NULL, CHECK(from_account<>to_account));
CREATE FUNCTION post_transfer() RETURNS trigger LANGUAGE plpgsql SET search_path=telco,pg_temp AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM stock_account WHERE id IN (NEW.from_account,NEW.to_account) AND sales_id IS NULL) THEN
 RAISE EXCEPTION 'transfer only between sales stock accounts'; END IF;
 PERFORM id FROM stock_account WHERE id IN (NEW.from_account,NEW.to_account) ORDER BY id FOR UPDATE;
 INSERT INTO stock_ledger(source_kind,source_id,leg,occurred_at,account_id,product_id,delta) VALUES
 ('transfer',NEW.id,1,NEW.occurred_at,NEW.from_account,NEW.product_id,-NEW.quantity),
 ('transfer',NEW.id,2,NEW.occurred_at,NEW.to_account,NEW.product_id,NEW.quantity);
 RETURN NEW;
END $$;
CREATE TRIGGER transfer_post AFTER INSERT ON stock_transfer FOR EACH ROW EXECUTE FUNCTION post_transfer();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON stock_transfer FOR EACH ROW EXECUTE FUNCTION reject_mutation();

-- Inbound WhatsApp events (already in 001) gain processing metadata. Message content is normalized, no phone number.
ALTER TABLE inbound_event ADD COLUMN correlation_id text;
ALTER TABLE inbound_event ADD COLUMN message jsonb;
ALTER TABLE inbound_event ADD COLUMN identity_ref text;
ALTER TABLE inbound_event ADD COLUMN provider_ts timestamptz;
ALTER TABLE inbound_event ADD COLUMN attempts integer NOT NULL DEFAULT 0;
ALTER TABLE inbound_event ADD COLUMN claimed_until timestamptz;
ALTER TABLE inbound_event ADD COLUMN result jsonb;
ALTER TABLE inbound_event ADD COLUMN completed_at timestamptz;

-- Outbox lease/dead-letter/provider reference. 'failed' is the terminal dead-letter state.
ALTER TABLE outbox ADD COLUMN kind text NOT NULL DEFAULT 'whatsapp_text' CHECK(kind IN ('whatsapp_text','whatsapp_image','n8n_dispatch'));
ALTER TABLE outbox ADD COLUMN correlation_id text;
ALTER TABLE outbox ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE outbox ADD COLUMN lease_owner text;
ALTER TABLE outbox ADD COLUMN lease_expires_at timestamptz;
ALTER TABLE outbox ADD COLUMN provider_message_id text;
ALTER TABLE outbox ADD COLUMN last_error text;
ALTER TABLE outbox ADD COLUMN sent_at timestamptz;
ALTER TABLE outbox ADD COLUMN dead_lettered_at timestamptz;
ALTER TABLE outbox ADD COLUMN max_attempts integer NOT NULL DEFAULT 8 CHECK(max_attempts BETWEEN 1 AND 50);
CREATE INDEX outbox_due ON outbox(state,next_attempt_at) WHERE state IN ('pending','sending');

-- Opt-in daily target nudges; channel explicit per principal (never summed).
CREATE TABLE notification_pref(principal_id text PRIMARY KEY REFERENCES app_principal,
 target_channel text NOT NULL CHECK(target_channel IN ('sell_in','sell_out')),
 daily_target_opt_in boolean NOT NULL DEFAULT false,
 send_hour_wib smallint NOT NULL DEFAULT 16 CHECK(send_hour_wib BETWEEN 7 AND 20),
 workdays smallint[] NOT NULL DEFAULT '{1,2,3,4,5}' CHECK(workdays <@ '{1,2,3,4,5,6,7}'::smallint[]),
 updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE business_holiday(day date PRIMARY KEY, name text NOT NULL);

-- One-turn capability ticket for Hermes tool calls: principal bound server-side, hash stored only.
CREATE TABLE agent_turn(ticket_hash text PRIMARY KEY, principal_id text NOT NULL, conversation_key text NOT NULL,
 correlation_id text NOT NULL, allowed_tools text[] NOT NULL, max_calls integer NOT NULL CHECK(max_calls BETWEEN 1 AND 20),
 calls integer NOT NULL DEFAULT 0, evidence_ids uuid[] NOT NULL DEFAULT '{}', expires_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), CHECK(calls<=max_calls));

-- Spreadsheet import staging. Rows become domain events only after admin confirmation.
CREATE TABLE import_file(id uuid PRIMARY KEY, sha256 text UNIQUE NOT NULL, filename text NOT NULL, kind text NOT NULL CHECK(kind IN ('sell_out')),
 uploaded_by text NOT NULL, status text NOT NULL DEFAULT 'staged' CHECK(status IN ('staged','rejected','confirmed','posted')),
 row_count integer NOT NULL, valid_rows integer NOT NULL, preview jsonb NOT NULL,
 confirmed_by text, confirmed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE import_row(file_id uuid NOT NULL REFERENCES import_file, row_no integer NOT NULL, raw jsonb NOT NULL,
 normalized jsonb, errors jsonb NOT NULL DEFAULT '[]', external_key text UNIQUE,
 status text NOT NULL DEFAULT 'staged' CHECK(status IN ('staged','invalid','posted','duplicate')),
 transaction_id bigint, PRIMARY KEY(file_id,row_no));

-- Separate schema for WhatsApp identity mapping. Values are app-encrypted (AES-GCM) with an HMAC blind index.
CREATE SCHEMA telco_identity;
CREATE TABLE telco_identity.wa_identity(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 wa_hmac text UNIQUE NOT NULL, wa_ciphertext text NOT NULL, key_version smallint NOT NULL,
 principal_id text NOT NULL REFERENCES telco.app_principal, active boolean NOT NULL DEFAULT true,
 created_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz);

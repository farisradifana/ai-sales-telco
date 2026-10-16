CREATE SCHEMA telco;
SET search_path=telco,public;
CREATE TABLE province(id integer PRIMARY KEY, code text UNIQUE NOT NULL, name text NOT NULL);
CREATE TABLE regency(id integer PRIMARY KEY, province_id integer NOT NULL REFERENCES province, code text UNIQUE NOT NULL, name text NOT NULL);
CREATE TABLE district(id integer PRIMARY KEY, regency_id integer NOT NULL REFERENCES regency, code text UNIQUE NOT NULL, name text NOT NULL);
CREATE TABLE sales(id integer PRIMARY KEY, code text UNIQUE NOT NULL, name text NOT NULL, district_id integer NOT NULL REFERENCES district);
CREATE TABLE outlet(id integer PRIMARY KEY, code text UNIQUE NOT NULL, name text NOT NULL,
 district_id integer NOT NULL REFERENCES district, owner_alias text NOT NULL, fictional_address text NOT NULL,
 latitude numeric(9,6) CHECK(latitude BETWEEN -90 AND 90), longitude numeric(9,6) CHECK(longitude BETWEEN -180 AND 180),
 map_url text GENERATED ALWAYS AS ('https://www.google.com/maps?q='||latitude::text||','||longitude::text) STORED,
 opened_on date NOT NULL, closed_on date CHECK(closed_on>=opened_on), is_synthetic boolean NOT NULL DEFAULT true);
CREATE TABLE product(id integer PRIMARY KEY, sku text UNIQUE NOT NULL, name text NOT NULL,
 category text NOT NULL CHECK(category IN ('PERDANA','VOUCHER','SALDO')),
 stock_unit text NOT NULL CHECK(stock_unit IN ('PCS','IDR')), face_value_idr numeric(14,2) NOT NULL CHECK(face_value_idr>0),
 list_price numeric(14,4) NOT NULL CHECK(list_price>0),
 CHECK ((category='SALDO' AND stock_unit='IDR') OR (category<>'SALDO' AND stock_unit='PCS')));
CREATE TABLE assignment(id integer PRIMARY KEY, outlet_id integer NOT NULL REFERENCES outlet, sales_id integer NOT NULL REFERENCES sales,
 valid_from date NOT NULL, valid_to date NOT NULL CHECK(valid_to>valid_from), UNIQUE(id,outlet_id,sales_id));
-- Exclusive valid_to; overlap protection serialized per outlet without extension dependencies.
CREATE FUNCTION guard_assignment() RETURNS trigger LANGUAGE plpgsql SET search_path=telco,pg_temp AS $$
BEGIN
 PERFORM 1 FROM outlet WHERE id=NEW.outlet_id FOR UPDATE;
 IF EXISTS(SELECT 1 FROM assignment WHERE outlet_id=NEW.outlet_id AND id<>NEW.id
 AND valid_from<NEW.valid_to AND valid_to>NEW.valid_from) THEN RAISE EXCEPTION 'assignment overlap'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER assignment_overlap BEFORE INSERT OR UPDATE ON assignment FOR EACH ROW EXECUTE FUNCTION guard_assignment();
CREATE TABLE stock_account(id integer PRIMARY KEY, sales_id integer UNIQUE REFERENCES sales, outlet_id integer UNIQUE REFERENCES outlet,
 CHECK((sales_id IS NULL) <> (outlet_id IS NULL)));
CREATE TABLE stock_balance(account_id integer REFERENCES stock_account, product_id integer REFERENCES product,
 quantity numeric(18,2) NOT NULL DEFAULT 0 CHECK(quantity>=0), PRIMARY KEY(account_id,product_id));
CREATE TABLE supply(id bigint PRIMARY KEY, external_key text UNIQUE NOT NULL, occurred_at timestamptz NOT NULL,
 account_id integer NOT NULL REFERENCES stock_account, product_id integer NOT NULL REFERENCES product,
 quantity numeric(18,2) NOT NULL CHECK(quantity>0), reason text NOT NULL);
-- One row = one completed product line, not a basket/invoice. ID and external_key are per channel.
CREATE TABLE sell_in(id bigint PRIMARY KEY, external_key text UNIQUE NOT NULL, occurred_at timestamptz NOT NULL,
 outlet_id integer NOT NULL REFERENCES outlet, sales_id integer NOT NULL REFERENCES sales, assignment_id integer NOT NULL,
 product_id integer NOT NULL REFERENCES product, quantity numeric(18,2) NOT NULL CHECK(quantity>0),
 unit_price numeric(14,4) NOT NULL CHECK(unit_price>=0), discount_idr numeric(18,2) NOT NULL DEFAULT 0 CHECK(discount_idr>=0),
 net_revenue numeric(18,2) GENERATED ALWAYS AS (round(quantity*unit_price-discount_idr,2)) STORED,
 FOREIGN KEY(assignment_id,outlet_id,sales_id) REFERENCES assignment(id,outlet_id,sales_id),
 CHECK(discount_idr<=round(quantity*unit_price,2)));
CREATE TABLE sell_out(LIKE sell_in INCLUDING ALL);
ALTER TABLE sell_out ADD FOREIGN KEY(outlet_id) REFERENCES outlet;
ALTER TABLE sell_out ADD FOREIGN KEY(sales_id) REFERENCES sales;
ALTER TABLE sell_out ADD FOREIGN KEY(product_id) REFERENCES product;
ALTER TABLE sell_out ADD FOREIGN KEY(assignment_id,outlet_id,sales_id) REFERENCES assignment(id,outlet_id,sales_id);
ALTER TABLE sell_out ADD customer_token text NOT NULL CHECK(customer_token LIKE 'SYN-%');
CREATE TABLE reversal(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, channel text NOT NULL CHECK(channel IN ('sell_in','sell_out')),
 transaction_id bigint NOT NULL, external_key text UNIQUE NOT NULL, occurred_at timestamptz NOT NULL, reason text NOT NULL,
 UNIQUE(channel,transaction_id));
-- Polymorphic references are checked by posting triggers (cannot use one FK for two source tables).
CREATE TABLE stock_ledger(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 source_kind text NOT NULL CHECK(source_kind IN ('supply','sell_in','sell_out','reversal')),
 source_id bigint NOT NULL, leg smallint NOT NULL, occurred_at timestamptz NOT NULL,
 account_id integer NOT NULL REFERENCES stock_account, product_id integer NOT NULL REFERENCES product,
 delta numeric(18,2) NOT NULL CHECK(delta<>0), UNIQUE(source_kind,source_id,leg));
CREATE INDEX ledger_account_product_time ON stock_ledger(account_id,product_id,occurred_at,id);
CREATE INDEX sell_in_scope_time ON sell_in(outlet_id,occurred_at);
CREATE INDEX sell_out_scope_time ON sell_out(outlet_id,occurred_at);
CREATE INDEX sell_out_product_time ON sell_out(product_id,occurred_at);
CREATE TABLE sales_target(sales_id integer REFERENCES sales, target_date date NOT NULL,
 channel text NOT NULL CHECK(channel IN ('sell_in','sell_out')), revenue_idr numeric(18,2) NOT NULL CHECK(revenue_idr>0),
 rationale text NOT NULL, PRIMARY KEY(sales_id,target_date,channel));
CREATE TABLE outlet_day(outlet_id integer REFERENCES outlet, day date NOT NULL, expected_open boolean NOT NULL,
 sell_out_complete boolean NOT NULL, sell_in_complete boolean NOT NULL, visits_planned integer NOT NULL CHECK(visits_planned>=0),
 visits_done integer NOT NULL CHECK(visits_done BETWEEN 0 AND visits_planned),
 observed_stockout_minutes integer CHECK(observed_stockout_minutes BETWEEN 0 AND 1440),
 observation_source text NOT NULL, PRIMARY KEY(outlet_id,day));
CREATE TABLE ingestion_watermark(source text PRIMARY KEY, complete_through date NOT NULL, batch_key text NOT NULL UNIQUE);
CREATE TABLE conversation_context(id text PRIMARY KEY, principal_id text NOT NULL, channel text NOT NULL,
 scope jsonb NOT NULL, metric text NOT NULL CHECK(metric IN ('sell_in','sell_out')),
 baseline_start date NOT NULL, baseline_end date NOT NULL, current_start date NOT NULL, current_end date NOT NULL,
 evidence jsonb NOT NULL DEFAULT '{}', version integer NOT NULL DEFAULT 1,
 expires_at timestamptz NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK(baseline_start<baseline_end AND current_start<current_end));
CREATE TABLE analysis_audit(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, context_id text REFERENCES conversation_context,
 tool_name text NOT NULL, parameters jsonb NOT NULL, result_digest text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE inbound_event(provider text NOT NULL, message_id text NOT NULL, principal_id text NOT NULL,
 received_at timestamptz NOT NULL DEFAULT now(), payload_hash text NOT NULL, state text NOT NULL DEFAULT 'received'
 CHECK(state IN ('received','processing','completed','failed')), PRIMARY KEY(provider,message_id));
CREATE TABLE outbox(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, idempotency_key text UNIQUE NOT NULL,
 principal_id text NOT NULL, payload jsonb NOT NULL, state text NOT NULL DEFAULT 'pending'
 CHECK(state IN ('pending','sending','sent','failed')), attempts integer NOT NULL DEFAULT 0, next_attempt_at timestamptz NOT NULL DEFAULT now());
CREATE FUNCTION reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only: use reversal/new event'; END $$;
CREATE FUNCTION ledger_apply() RETURNS trigger LANGUAGE plpgsql SET search_path=telco,pg_temp AS $$
BEGIN
 IF (SELECT stock_unit FROM product WHERE id=NEW.product_id)='PCS' AND NEW.delta<>trunc(NEW.delta) THEN
 RAISE EXCEPTION 'PCS must be integer'; END IF;
 INSERT INTO stock_balance(account_id,product_id,quantity) VALUES(NEW.account_id,NEW.product_id,0) ON CONFLICT DO NOTHING;
 UPDATE stock_balance SET quantity=quantity+NEW.delta WHERE account_id=NEW.account_id AND product_id=NEW.product_id;
 RETURN NEW;
END $$;
CREATE TRIGGER ledger_balance AFTER INSERT ON stock_ledger FOR EACH ROW EXECUTE FUNCTION ledger_apply();
CREATE FUNCTION post_supply() RETURNS trigger LANGUAGE plpgsql SET search_path=telco,pg_temp AS $$
BEGIN
 INSERT INTO stock_ledger(source_kind,source_id,leg,occurred_at,account_id,product_id,delta)
 VALUES('supply',NEW.id,1,NEW.occurred_at,NEW.account_id,NEW.product_id,NEW.quantity);
 RETURN NEW;
END $$;
CREATE TRIGGER supply_post AFTER INSERT ON supply FOR EACH ROW EXECUTE FUNCTION post_supply();
CREATE FUNCTION post_sale() RETURNS trigger LANGUAGE plpgsql SET search_path=telco,pg_temp AS $$
DECLARE outlet_account integer; sales_account integer; local_day date;
BEGIN
 local_day := (NEW.occurred_at AT TIME ZONE 'Asia/Jakarta')::date;
 IF NOT EXISTS(SELECT 1 FROM assignment WHERE id=NEW.assignment_id AND local_day>=valid_from AND local_day<valid_to) THEN
 RAISE EXCEPTION 'assignment invalid on transaction date'; END IF;
 IF NOT EXISTS(SELECT 1 FROM outlet WHERE id=NEW.outlet_id AND local_day>=opened_on AND (closed_on IS NULL OR local_day<closed_on)) THEN
 RAISE EXCEPTION 'outlet not open on transaction date'; END IF;
 SELECT id INTO STRICT outlet_account FROM stock_account WHERE outlet_id=NEW.outlet_id;
 IF TG_TABLE_NAME='sell_in' THEN
 SELECT id INTO STRICT sales_account FROM stock_account WHERE sales_id=NEW.sales_id;
 -- Lock in account order for predictable deadlock avoidance in the standard posting path.
 PERFORM id FROM stock_account WHERE id IN (outlet_account,sales_account) ORDER BY id FOR UPDATE;
 INSERT INTO stock_ledger(source_kind,source_id,leg,occurred_at,account_id,product_id,delta) VALUES
 ('sell_in',NEW.id,1,NEW.occurred_at,sales_account,NEW.product_id,-NEW.quantity),
 ('sell_in',NEW.id,2,NEW.occurred_at,outlet_account,NEW.product_id,NEW.quantity);
 ELSE
 PERFORM id FROM stock_account WHERE id=outlet_account FOR UPDATE;
 INSERT INTO stock_ledger(source_kind,source_id,leg,occurred_at,account_id,product_id,delta)
 VALUES('sell_out',NEW.id,1,NEW.occurred_at,outlet_account,NEW.product_id,-NEW.quantity);
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER sell_in_post AFTER INSERT ON sell_in FOR EACH ROW EXECUTE FUNCTION post_sale();
CREATE TRIGGER sell_out_post AFTER INSERT ON sell_out FOR EACH ROW EXECUTE FUNCTION post_sale();
CREATE FUNCTION post_reversal() RETURNS trigger LANGUAGE plpgsql SET search_path=telco,pg_temp AS $$
DECLARE tx_time timestamptz;
BEGIN
 IF NEW.channel='sell_in' THEN SELECT occurred_at INTO tx_time FROM sell_in WHERE id=NEW.transaction_id;
 ELSE SELECT occurred_at INTO tx_time FROM sell_out WHERE id=NEW.transaction_id; END IF;
 IF tx_time IS NULL OR NEW.occurred_at<tx_time THEN RAISE EXCEPTION 'invalid reversal source/date'; END IF;
 PERFORM a.id FROM stock_account a JOIN stock_ledger l ON l.account_id=a.id
 WHERE l.source_kind=NEW.channel AND l.source_id=NEW.transaction_id ORDER BY a.id FOR UPDATE OF a;
 INSERT INTO stock_ledger(source_kind,source_id,leg,occurred_at,account_id,product_id,delta)
 SELECT 'reversal',NEW.id,leg,NEW.occurred_at,account_id,product_id,-delta FROM stock_ledger
 WHERE source_kind=NEW.channel AND source_id=NEW.transaction_id ORDER BY account_id;
 RETURN NEW;
END $$;
CREATE TRIGGER reversal_post AFTER INSERT ON reversal FOR EACH ROW EXECUTE FUNCTION post_reversal();
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['supply','sell_in','sell_out','reversal','stock_ledger'] LOOP
 EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON telco.%I FOR EACH ROW EXECUTE FUNCTION telco.reject_mutation()',t);
 END LOOP;
END $$;
-- Keep reporting attribution immutable; reassign by inserting a new non-overlapping interval.
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON assignment FOR EACH ROW EXECUTE FUNCTION reject_mutation();

-- Sequences for API/admin-posted events. Ranges start far above seeded dataset IDs (<=400000) so
-- generated history and live postings never collide. Columns keep no default: callers use nextval explicitly.
SET search_path=telco,public;
CREATE SEQUENCE sell_in_api_id START 1000000001 OWNED BY sell_in.id;
CREATE SEQUENCE sell_out_api_id START 1000000001 OWNED BY sell_out.id;
CREATE SEQUENCE supply_api_id START 1000000001 OWNED BY supply.id;
-- Fast lookup for the latest ledger time per holder+SKU (no-backdate rule) already uses ledger_account_product_time.

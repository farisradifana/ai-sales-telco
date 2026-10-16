-- Row locks (SELECT ... FOR UPDATE) need UPDATE privilege. Instead of granting UPDATE on stock_account to the API
-- writer, expose one narrow SECURITY DEFINER function that only locks holder rows in ascending id order (deadlock-safe).
SET search_path=telco,public;
CREATE FUNCTION lock_stock_accounts(p_ids integer[]) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=telco,pg_temp AS $$
DECLARE n integer;
BEGIN
 PERFORM id FROM stock_account WHERE id=ANY(p_ids) ORDER BY id FOR UPDATE;
 GET DIAGNOSTICS n = ROW_COUNT;
 RETURN n;
END $$;
REVOKE ALL ON FUNCTION lock_stock_accounts(integer[]) FROM PUBLIC;

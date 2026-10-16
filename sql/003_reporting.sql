SET search_path=telco,public;
CREATE VIEW v_calendar AS
 SELECT d::date AS day,extract(year FROM d)::integer AS year,extract(quarter FROM d)::integer AS quarter,
 to_char(d,'YYYY-MM') AS year_month,extract(isodow FROM d)::integer AS iso_weekday
 FROM generate_series(timestamp '2025-01-01',timestamp '2026-09-30',interval '1 day') d;
CREATE VIEW v_quarterly_outlet AS
 SELECT channel,extract(year FROM day)::integer AS year,extract(quarter FROM day)::integer AS quarter,
 outlet_id,sum(revenue_idr) revenue_idr,sum(net_lines) net_lines FROM v_daily_sales GROUP BY 1,2,3,4;
CREATE VIEW v_daily_active_outlets AS
 SELECT channel,day,count(DISTINCT outlet_id) FILTER(WHERE gross_lines>0) AS active_outlets
 FROM v_daily_sales GROUP BY 1,2;
CREATE VIEW v_outlet_day_revenue AS
 SELECT d.outlet_id,d.day,d.expected_open,d.sell_out_complete,
 CASE WHEN d.sell_out_complete THEN coalesce(s.revenue_idr,0) ELSE NULL END AS sell_out_revenue_idr
 FROM outlet_day d LEFT JOIN (SELECT outlet_id,day,sum(revenue_idr) revenue_idr FROM v_daily_sales
 WHERE channel='sell_out' GROUP BY 1,2) s ON s.outlet_id=d.outlet_id AND s.day=d.day;
CREATE FUNCTION stock_as_of(p_account integer,p_product integer,p_at timestamptz)
RETURNS numeric LANGUAGE sql STABLE SET search_path=telco,pg_temp AS $$
 SELECT coalesce(sum(delta),0) FROM stock_ledger WHERE account_id=p_account AND product_id=p_product AND occurred_at<=p_at;
$$;
-- Positive evidence of missing vs zero; missing outlet_day rows remain missing on an outer calendar join.
COMMENT ON VIEW v_outlet_day_revenue IS 'Zero only when input is complete. Missing/incomplete days must not become zero in charts.';

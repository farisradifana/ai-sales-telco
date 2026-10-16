SET search_path=telco,public;
-- Reversals are negative revenue on their posting date. Original historical dates do not change.
CREATE VIEW v_commercial_event AS
 SELECT 'sell_in'::text channel,id transaction_id,occurred_at,outlet_id,sales_id,product_id,quantity,net_revenue,1 sign FROM sell_in
 UNION ALL SELECT 'sell_out',id,occurred_at,outlet_id,sales_id,product_id,quantity,net_revenue,1 FROM sell_out
 UNION ALL
 SELECT r.channel,t.id,r.occurred_at,t.outlet_id,t.sales_id,t.product_id,-t.quantity,-t.net_revenue,-1
 FROM reversal r JOIN sell_in t ON r.channel='sell_in' AND r.transaction_id=t.id
 UNION ALL
 SELECT r.channel,t.id,r.occurred_at,t.outlet_id,t.sales_id,t.product_id,-t.quantity,-t.net_revenue,-1
 FROM reversal r JOIN sell_out t ON r.channel='sell_out' AND r.transaction_id=t.id;
CREATE VIEW v_daily_sales AS
 SELECT channel,(occurred_at AT TIME ZONE 'Asia/Jakarta')::date AS day,outlet_id,sales_id,product_id,
 sum(quantity) quantity,sum(net_revenue) revenue_idr,sum(sign) net_lines,count(*) FILTER(WHERE sign=1) gross_lines
 FROM v_commercial_event GROUP BY 1,2,3,4,5;
CREATE VIEW v_monthly_outlet AS
 SELECT channel,date_trunc('month',day)::date AS month,outlet_id,sum(revenue_idr) revenue_idr,sum(net_lines) net_lines
 FROM v_daily_sales GROUP BY 1,2,3;
CREATE VIEW v_monthly_district AS
 SELECT m.channel,m.month,o.district_id,sum(m.revenue_idr) revenue_idr,sum(m.net_lines) net_lines
 FROM v_monthly_outlet m JOIN outlet o ON o.id=m.outlet_id GROUP BY 1,2,3;
CREATE VIEW v_stock AS SELECT b.account_id,b.product_id,p.stock_unit,b.quantity FROM stock_balance b JOIN product p ON p.id=b.product_id;
CREATE VIEW v_target_achievement AS
 SELECT t.sales_id,t.target_date,t.channel,t.revenue_idr target_idr,coalesce(a.actual_idr,0) actual_idr,
 coalesce(a.actual_idr,0)/t.revenue_idr achievement_ratio
 FROM sales_target t LEFT JOIN (SELECT sales_id,day,channel,sum(revenue_idr) actual_idr FROM v_daily_sales GROUP BY 1,2,3) a
 ON a.sales_id=t.sales_id AND a.day=t.target_date AND a.channel=t.channel;
-- Parameterized bounded tool. End dates EXCLUSIVE; same-length comparisons required by API.
CREATE FUNCTION diagnose(p_scope text,p_id integer,p_channel text,b0 date,b1 date,c0 date,c1 date)
RETURNS jsonb LANGUAGE plpgsql STABLE SET search_path=telco,pg_temp AS $$
DECLARE result jsonb;
BEGIN
 IF p_scope NOT IN ('outlet','district') OR p_channel NOT IN ('sell_in','sell_out') OR b0>=b1 OR c0>=c1 OR b1>c0
 OR (b1-b0)<>(c1-c0) OR c1-c0>366 THEN RAISE EXCEPTION 'invalid scope/channel/comparison (equal days, nonoverlap, <=366)'; END IF;
 IF (p_scope='outlet' AND NOT EXISTS(SELECT 1 FROM outlet WHERE id=p_id)) OR
 (p_scope='district' AND NOT EXISTS(SELECT 1 FROM district WHERE id=p_id)) THEN RAISE EXCEPTION 'unknown scope'; END IF;
 WITH population AS (SELECT id FROM outlet WHERE (p_scope='outlet' AND id=p_id) OR (p_scope='district' AND district_id=p_id)),
 agg AS (
 SELECT e.product_id,
 coalesce(sum(e.quantity) FILTER(WHERE day>=b0 AND day<b1),0) q0,
 coalesce(sum(e.quantity) FILTER(WHERE day>=c0 AND day<c1),0) q1,
 coalesce(sum(e.revenue_idr) FILTER(WHERE day>=b0 AND day<b1),0) r0,
 coalesce(sum(e.revenue_idr) FILTER(WHERE day>=c0 AND day<c1),0) r1
 FROM v_daily_sales e JOIN population o ON o.id=e.outlet_id WHERE e.channel=p_channel
 AND ((day>=b0 AND day<b1) OR (day>=c0 AND day<c1)) GROUP BY e.product_id),
 drivers AS (
 SELECT a.*,p.name,p.stock_unit,
 CASE WHEN q0>0 AND q1>0 THEN (q1-q0)*(r0/q0+r1/q1)/2 ELSE 0 END volume_effect,
 CASE WHEN q0>0 AND q1>0 THEN (r1/q1-r0/q0)*(q0+q1)/2 ELSE 0 END price_mix_effect,
 CASE WHEN q0<=0 OR q1<=0 THEN r1-r0 ELSE 0 END entry_exit_effect
 FROM agg a JOIN product p ON p.id=a.product_id),
 coverage AS (
 SELECT count(*) expected_outlet_days,count(d.day) observed_outlet_days,
 count(*) FILTER(WHERE CASE WHEN p_channel='sell_out' THEN d.sell_out_complete ELSE d.sell_in_complete END) complete_outlet_days,
 sum(d.visits_planned) visits_planned,sum(d.visits_done) visits_done,
 sum(d.observed_stockout_minutes) observed_stockout_minutes,
 count(d.observed_stockout_minutes) stockout_observed_days
 FROM population o JOIN outlet x ON x.id=o.id
 CROSS JOIN generate_series(c0,c1-1,interval '1 day') g
 LEFT JOIN outlet_day d ON d.outlet_id=o.id AND d.day=g::date
 WHERE g::date>=x.opened_on AND (x.closed_on IS NULL OR g::date<x.closed_on) AND d.expected_open IS DISTINCT FROM false),
 basecoverage AS (
 SELECT count(*) expected_outlet_days,count(*) FILTER(WHERE CASE WHEN p_channel='sell_out' THEN d.sell_out_complete ELSE d.sell_in_complete END) complete_outlet_days
 FROM population o JOIN outlet x ON x.id=o.id CROSS JOIN generate_series(b0,b1-1,interval '1 day') g
 LEFT JOIN outlet_day d ON d.outlet_id=o.id AND d.day=g::date
 WHERE g::date>=x.opened_on AND (x.closed_on IS NULL OR g::date<x.closed_on) AND d.expected_open IS DISTINCT FROM false),
 outlet_contributions AS (
 SELECT o.id,outlet.name,
 coalesce(sum(e.revenue_idr) FILTER(WHERE day>=b0 AND day<b1),0) r0,
 coalesce(sum(e.revenue_idr) FILTER(WHERE day>=c0 AND day<c1),0) r1
 FROM population o JOIN outlet ON outlet.id=o.id LEFT JOIN v_daily_sales e ON e.outlet_id=o.id AND e.channel=p_channel
 AND ((day>=b0 AND day<b1) OR(day>=c0 AND day<c1)) GROUP BY o.id,outlet.name)
 SELECT jsonb_build_object('scope',p_scope,'entity_id',p_id,'channel',p_channel,
 'baseline',jsonb_build_array(b0,b1),'current',jsonb_build_array(c0,c1),'timezone','Asia/Jakarta',
 'revenue_baseline_idr',coalesce((SELECT sum(r0) FROM drivers),0),
 'revenue_current_idr',coalesce((SELECT sum(r1) FROM drivers),0),
 'delta_idr',coalesce((SELECT sum(r1-r0) FROM drivers),0),
 'drivers',coalesce((SELECT jsonb_agg(jsonb_build_object('product_id',product_id,'product',name,'stock_unit',stock_unit,
 'q0',q0,'q1',q1,'r0',r0,'r1',r1,'delta_idr',r1-r0,'volume_effect_idr',volume_effect,
 'price_mix_effect_idr',price_mix_effect,'entry_exit_effect_idr',entry_exit_effect) ORDER BY r1-r0) FROM drivers),'[]'::jsonb),
 'coverage',(SELECT to_jsonb(coverage) FROM coverage),'baseline_coverage',(SELECT to_jsonb(basecoverage) FROM basecoverage),
 'outlet_contributions',(SELECT jsonb_agg(jsonb_build_object('outlet_id',id,'name',name,'baseline_idr',r0,'current_idr',r1,'delta_idr',r1-r0) ORDER BY r1-r0,id) FROM outlet_contributions),
 'watermark',(SELECT complete_through FROM ingestion_watermark WHERE source=p_channel),
 'source_batch_key',(SELECT batch_key FROM ingestion_watermark WHERE source=p_channel),
 'certainty','Accounting decomposition verified; causal drivers unproven',
 'query_version','diagnose-v1') INTO result;
 RETURN result;
END $$;
CREATE FUNCTION rank_districts(p_channel text,b0 date,b1 date,c0 date,c1 date)
RETURNS TABLE(district_id integer,district_name text,baseline_idr numeric,current_idr numeric,delta_idr numeric,growth numeric)
LANGUAGE plpgsql STABLE SET search_path=telco,pg_temp AS $$
BEGIN
 IF p_channel NOT IN ('sell_in','sell_out') OR b0>=b1 OR c0>=c1 OR b1>c0
 OR (b1-b0)<>(c1-c0) OR c1-c0>366 THEN RAISE EXCEPTION 'invalid channel/comparison'; END IF;
 RETURN QUERY
 WITH a AS (SELECT o.district_id,
 coalesce(sum(e.revenue_idr) FILTER(WHERE e.day>=b0 AND e.day<b1),0) r0,
 coalesce(sum(e.revenue_idr) FILTER(WHERE e.day>=c0 AND e.day<c1),0) r1
 FROM outlet o LEFT JOIN v_daily_sales e ON e.outlet_id=o.id AND e.channel=p_channel
 AND ((e.day>=b0 AND e.day<b1) OR(e.day>=c0 AND e.day<c1)) GROUP BY o.district_id)
 SELECT d.id,d.name,a.r0,a.r1,a.r1-a.r0,(a.r1-a.r0)/nullif(a.r0,0)
 FROM district d JOIN a ON a.district_id=d.id ORDER BY a.r1-a.r0,d.id;
END $$;

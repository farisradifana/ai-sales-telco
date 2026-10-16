-- Aggregate analytics for area roles: scope regency (kabupaten/kota) and province, next to outlet/district.
-- Same contract as diagnose() (002): fixed population = every outlet in the area (incl. non-transacting), two-period
-- coverage, per-SKU Shapley drivers, outlet + kecamatan contributions, watermark/batch. Channels are never summed.
-- Authorization: can_access_area() is default-deny; an area aggregate requires a district grant for EVERY kecamatan in it.
SET search_path=telco,public;

CREATE FUNCTION can_access_area(p_principal text,p_scope text,p_id integer) RETURNS boolean
LANGUAGE sql STABLE SET search_path=telco,pg_temp AS $$
 SELECT CASE
  WHEN p_scope IN ('outlet','district') THEN coalesce(can_access(p_principal,p_scope,p_id),false)
  WHEN p_scope IN ('regency','province') THEN
   EXISTS(SELECT 1 FROM district d JOIN regency r ON r.id=d.regency_id
     WHERE (p_scope='regency' AND d.regency_id=p_id) OR (p_scope='province' AND r.province_id=p_id))
   AND NOT EXISTS(SELECT 1 FROM district d JOIN regency r ON r.id=d.regency_id
     WHERE ((p_scope='regency' AND d.regency_id=p_id) OR (p_scope='province' AND r.province_id=p_id))
     AND NOT EXISTS(SELECT 1 FROM principal_grant g WHERE g.principal_id=p_principal AND g.scope_type='district' AND g.entity_id=d.id))
  ELSE false END;
$$;

-- Parameterized bounded tool. End dates EXCLUSIVE; same-length comparisons required by API.
CREATE FUNCTION diagnose_area(p_scope text,p_id integer,p_channel text,b0 date,b1 date,c0 date,c1 date)
RETURNS jsonb LANGUAGE plpgsql STABLE SET search_path=telco,pg_temp AS $$
DECLARE result jsonb;
BEGIN
 IF p_scope NOT IN ('outlet','district','regency','province') OR p_channel NOT IN ('sell_in','sell_out') OR b0>=b1 OR c0>=c1 OR b1>c0
 OR (b1-b0)<>(c1-c0) OR c1-c0>366 THEN RAISE EXCEPTION 'invalid scope/channel/comparison (equal days, nonoverlap, <=366)'; END IF;
 IF (p_scope='outlet' AND NOT EXISTS(SELECT 1 FROM outlet WHERE id=p_id)) OR
 (p_scope='district' AND NOT EXISTS(SELECT 1 FROM district WHERE id=p_id)) OR
 (p_scope='regency' AND NOT EXISTS(SELECT 1 FROM regency WHERE id=p_id)) OR
 (p_scope='province' AND NOT EXISTS(SELECT 1 FROM province WHERE id=p_id)) THEN RAISE EXCEPTION 'unknown scope'; END IF;
 -- MATERIALIZED: the scope predicate is generic in a plpgsql plan; evaluate the population and the event window once
 -- instead of letting the planner re-aggregate v_daily_sales per outlet (nested loop on a rows=1 estimate).
 WITH population AS MATERIALIZED (
 SELECT o.id,o.district_id,d.regency_id FROM outlet o JOIN district d ON d.id=o.district_id JOIN regency r ON r.id=d.regency_id
 WHERE (p_scope='outlet' AND o.id=p_id) OR (p_scope='district' AND o.district_id=p_id)
 OR (p_scope='regency' AND d.regency_id=p_id) OR (p_scope='province' AND r.province_id=p_id)),
 events AS MATERIALIZED (
 SELECT e.outlet_id,e.product_id,e.day,e.quantity,e.revenue_idr FROM v_daily_sales e JOIN population o ON o.id=e.outlet_id
 WHERE e.channel=p_channel AND ((e.day>=b0 AND e.day<b1) OR (e.day>=c0 AND e.day<c1))),
 agg AS (
 SELECT e.product_id,
 coalesce(sum(e.quantity) FILTER(WHERE day>=b0 AND day<b1),0) q0,
 coalesce(sum(e.quantity) FILTER(WHERE day>=c0 AND day<c1),0) q1,
 coalesce(sum(e.revenue_idr) FILTER(WHERE day>=b0 AND day<b1),0) r0,
 coalesce(sum(e.revenue_idr) FILTER(WHERE day>=c0 AND day<c1),0) r1
 FROM events e GROUP BY e.product_id),
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
 SELECT o.id,outlet.code,outlet.name,o.district_id,o.regency_id,
 coalesce(sum(e.revenue_idr) FILTER(WHERE day>=b0 AND day<b1),0) r0,
 coalesce(sum(e.revenue_idr) FILTER(WHERE day>=c0 AND day<c1),0) r1
 FROM population o JOIN outlet ON outlet.id=o.id LEFT JOIN events e ON e.outlet_id=o.id GROUP BY o.id,outlet.code,outlet.name,o.district_id,o.regency_id),
 district_contributions AS (
 SELECT c.district_id,d.code,d.name,c.regency_id,count(*) outlets,sum(c.r0) r0,sum(c.r1) r1
 FROM outlet_contributions c JOIN district d ON d.id=c.district_id GROUP BY c.district_id,d.code,d.name,c.regency_id),
 regency_contributions AS (
 SELECT c.regency_id,r.code,r.name,count(*) districts,sum(c.outlets) outlets,sum(c.r0) r0,sum(c.r1) r1
 FROM district_contributions c JOIN regency r ON r.id=c.regency_id GROUP BY c.regency_id,r.code,r.name)
 SELECT jsonb_build_object('scope',p_scope,'entity_id',p_id,'channel',p_channel,
 'baseline',jsonb_build_array(b0,b1),'current',jsonb_build_array(c0,c1),'timezone','Asia/Jakarta',
 'revenue_baseline_idr',coalesce((SELECT sum(r0) FROM drivers),0),
 'revenue_current_idr',coalesce((SELECT sum(r1) FROM drivers),0),
 'delta_idr',coalesce((SELECT sum(r1-r0) FROM drivers),0),
 'drivers',coalesce((SELECT jsonb_agg(jsonb_build_object('product_id',product_id,'product',name,'stock_unit',stock_unit,
 'q0',q0,'q1',q1,'r0',r0,'r1',r1,'delta_idr',r1-r0,'volume_effect_idr',volume_effect,
 'price_mix_effect_idr',price_mix_effect,'entry_exit_effect_idr',entry_exit_effect) ORDER BY r1-r0) FROM drivers),'[]'::jsonb),
 'coverage',(SELECT to_jsonb(coverage) FROM coverage),'baseline_coverage',(SELECT to_jsonb(basecoverage) FROM basecoverage),
 'outlet_contributions',coalesce((SELECT jsonb_agg(jsonb_build_object('outlet_id',id,'code',code,'name',name,'district_id',district_id,
 'baseline_idr',r0,'current_idr',r1,'delta_idr',r1-r0) ORDER BY r1-r0,id) FROM outlet_contributions),'[]'::jsonb),
 'district_contributions',coalesce((SELECT jsonb_agg(jsonb_build_object('district_id',district_id,'code',code,'name',name,
 'regency_id',regency_id,'outlets',outlets,'baseline_idr',r0,'current_idr',r1,'delta_idr',r1-r0) ORDER BY r1-r0,district_id)
 FROM district_contributions),'[]'::jsonb),
 'regency_contributions',coalesce((SELECT jsonb_agg(jsonb_build_object('regency_id',regency_id,'code',code,'name',name,
 'districts',districts,'outlets',outlets,'baseline_idr',r0,'current_idr',r1,'delta_idr',r1-r0) ORDER BY r1-r0,regency_id)
 FROM regency_contributions),'[]'::jsonb),
 'watermark',(SELECT complete_through FROM ingestion_watermark WHERE source=p_channel),
 'source_batch_key',(SELECT batch_key FROM ingestion_watermark WHERE source=p_channel),
 'certainty','Accounting decomposition verified; causal drivers unproven',
 'query_version','diagnose-area-v1') INTO result;
 RETURN result;
END $$;

-- Ranking per kabupaten/kota (population = regencies that have outlets). Caller filters with can_access_area.
CREATE FUNCTION rank_regencies(p_channel text,b0 date,b1 date,c0 date,c1 date)
RETURNS TABLE(regency_id integer,regency_code text,regency_name text,districts bigint,outlets bigint,
 baseline_idr numeric,current_idr numeric,delta_idr numeric,growth numeric)
LANGUAGE plpgsql STABLE SET search_path=telco,pg_temp AS $$
BEGIN
 IF p_channel NOT IN ('sell_in','sell_out') OR b0>=b1 OR c0>=c1 OR b1>c0
 OR (b1-b0)<>(c1-c0) OR c1-c0>366 THEN RAISE EXCEPTION 'invalid channel/comparison'; END IF;
 RETURN QUERY
 WITH o AS (SELECT x.id,d.regency_id FROM outlet x JOIN district d ON d.id=x.district_id),
 a AS (SELECT o.regency_id,count(DISTINCT o.id) n_outlets,
 coalesce(sum(e.revenue_idr) FILTER(WHERE e.day>=b0 AND e.day<b1),0) r0,
 coalesce(sum(e.revenue_idr) FILTER(WHERE e.day>=c0 AND e.day<c1),0) r1
 FROM o LEFT JOIN v_daily_sales e ON e.outlet_id=o.id AND e.channel=p_channel
 AND ((e.day>=b0 AND e.day<b1) OR(e.day>=c0 AND e.day<c1)) GROUP BY o.regency_id)
 SELECT r.id,r.code,r.name,(SELECT count(*) FROM district d WHERE d.regency_id=r.id),a.n_outlets,
 a.r0,a.r1,a.r1-a.r0,(a.r1-a.r0)/nullif(a.r0,0)
 FROM regency r JOIN a ON a.regency_id=r.id ORDER BY a.r1-a.r0,r.id;
END $$;

-- security.sql revokes PUBLIC execute by default; grant the reader here when the group role is provisioned.
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='telco_reader') THEN
  REVOKE ALL ON FUNCTION telco.can_access_area(text,text,integer),
   telco.diagnose_area(text,integer,text,date,date,date,date),telco.rank_regencies(text,date,date,date,date) FROM PUBLIC;
  GRANT EXECUTE ON FUNCTION telco.can_access_area(text,text,integer),
   telco.diagnose_area(text,integer,text,date,date,date,date),telco.rank_regencies(text,date,date,date,date) TO telco_reader;
 END IF;
END $$;

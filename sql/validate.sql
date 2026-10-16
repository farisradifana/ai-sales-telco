-- Must raise on failure; safe read-only consistency audit after load and periodically in production.
SET search_path=telco,public;
DO $$
BEGIN
 IF EXISTS(SELECT 1 FROM stock_balance WHERE quantity<0) THEN RAISE EXCEPTION 'negative balance'; END IF;
 IF EXISTS(WITH s AS(SELECT account_id,product_id,sum(delta) q FROM stock_ledger GROUP BY 1,2)
 SELECT 1 FROM s FULL JOIN stock_balance b USING(account_id,product_id) WHERE coalesce(s.q,0)<>coalesce(b.quantity,0)) THEN
 RAISE EXCEPTION 'ledger/balance reconciliation'; END IF;
 IF EXISTS(WITH d AS(SELECT account_id,product_id,occurred_at,sum(delta) delta FROM stock_ledger GROUP BY 1,2,3),
 b AS(SELECT sum(delta) OVER(PARTITION BY account_id,product_id ORDER BY occurred_at) quantity FROM d)
 SELECT 1 FROM b WHERE quantity<0) THEN RAISE EXCEPTION 'historical negative balance'; END IF;
 IF EXISTS(SELECT 1 FROM sell_in t LEFT JOIN stock_ledger l ON l.source_kind='sell_in' AND l.source_id=t.id
 GROUP BY t.id,t.quantity HAVING count(l.id)<>2 OR sum(l.delta)<>0 OR max(l.delta)<>t.quantity OR min(l.delta)<>-t.quantity) THEN
 RAISE EXCEPTION 'sell-in legs'; END IF;
 IF EXISTS(SELECT 1 FROM sell_out t LEFT JOIN stock_ledger l ON l.source_kind='sell_out' AND l.source_id=t.id
 GROUP BY t.id,t.quantity HAVING count(l.id)<>1 OR sum(l.delta)<>-t.quantity) THEN RAISE EXCEPTION 'sell-out legs'; END IF;
 IF EXISTS(SELECT 1 FROM supply t LEFT JOIN stock_ledger l ON l.source_kind='supply' AND l.source_id=t.id
 GROUP BY t.id,t.quantity HAVING count(l.id)<>1 OR sum(l.delta)<>t.quantity) THEN RAISE EXCEPTION 'supply legs'; END IF;
 IF EXISTS(SELECT 1 FROM stock_ledger l WHERE
 (source_kind='supply' AND NOT EXISTS(SELECT 1 FROM supply s WHERE s.id=l.source_id)) OR
 (source_kind='sell_in' AND NOT EXISTS(SELECT 1 FROM sell_in s WHERE s.id=l.source_id)) OR
 (source_kind='sell_out' AND NOT EXISTS(SELECT 1 FROM sell_out s WHERE s.id=l.source_id)) OR
 (source_kind='reversal' AND NOT EXISTS(SELECT 1 FROM reversal s WHERE s.id=l.source_id))) THEN RAISE EXCEPTION 'orphan ledger'; END IF;
 IF EXISTS(SELECT 1 FROM stock_ledger l JOIN sell_out t ON l.source_kind='sell_out' AND l.source_id=t.id
 JOIN stock_account a ON a.id=l.account_id WHERE l.product_id<>t.product_id OR a.outlet_id IS DISTINCT FROM t.outlet_id
 OR l.occurred_at<>t.occurred_at) THEN RAISE EXCEPTION 'sell-out source attribution'; END IF;
 IF EXISTS(SELECT 1 FROM stock_ledger l JOIN sell_in t ON l.source_kind='sell_in' AND l.source_id=t.id
 JOIN stock_account a ON a.id=l.account_id WHERE l.product_id<>t.product_id OR l.occurred_at<>t.occurred_at OR
 (l.leg=1 AND a.sales_id IS DISTINCT FROM t.sales_id) OR (l.leg=2 AND a.outlet_id IS DISTINCT FROM t.outlet_id)) THEN
 RAISE EXCEPTION 'sell-in source attribution'; END IF;
 IF EXISTS(SELECT 1 FROM reversal r WHERE
 (SELECT coalesce(sum(delta),0) FROM stock_ledger WHERE source_kind='reversal' AND source_id=r.id) <>
 -(SELECT coalesce(sum(delta),0) FROM stock_ledger WHERE source_kind=r.channel AND source_id=r.transaction_id)) THEN
 RAISE EXCEPTION 'reversal ledger'; END IF;
 IF (SELECT coalesce(sum(net_revenue),0) FROM v_commercial_event WHERE channel='sell_out') <>
 (SELECT coalesce(sum(net_revenue),0) FROM sell_out) -
 (SELECT coalesce(sum(t.net_revenue),0) FROM reversal r JOIN sell_out t ON r.channel='sell_out' AND r.transaction_id=t.id) THEN
 RAISE EXCEPTION 'sell-out double count'; END IF;
 IF (SELECT coalesce(sum(revenue_idr),0) FROM v_daily_sales WHERE channel='sell_in') <>
 (SELECT coalesce(sum(net_revenue),0) FROM v_commercial_event WHERE channel='sell_in') THEN RAISE EXCEPTION 'sell-in KPI mismatch'; END IF;
END $$;

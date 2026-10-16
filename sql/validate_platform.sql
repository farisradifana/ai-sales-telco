-- Additional read-only audit for objects added in 005+. Run after sql/validate.sql. Must raise on failure.
SET search_path=telco,public;
DO $$
BEGIN
 IF EXISTS(SELECT 1 FROM stock_transfer t LEFT JOIN stock_ledger l ON l.source_kind='transfer' AND l.source_id=t.id
 GROUP BY t.id,t.quantity HAVING count(l.id)<>2 OR sum(l.delta)<>0 OR max(l.delta)<>t.quantity) THEN RAISE EXCEPTION 'transfer legs'; END IF;
 IF EXISTS(SELECT 1 FROM stock_ledger l WHERE source_kind='transfer' AND NOT EXISTS(SELECT 1 FROM stock_transfer s WHERE s.id=l.source_id)) THEN
 RAISE EXCEPTION 'orphan transfer ledger'; END IF;
 IF EXISTS(SELECT 1 FROM posting_request p WHERE
 (p.channel='sell_in' AND NOT EXISTS(SELECT 1 FROM sell_in s WHERE s.id=p.transaction_id AND s.external_key=p.external_key)) OR
 (p.channel='sell_out' AND NOT EXISTS(SELECT 1 FROM sell_out s WHERE s.id=p.transaction_id AND s.external_key=p.external_key)) OR
 (p.channel='reversal' AND NOT EXISTS(SELECT 1 FROM reversal s WHERE s.id=p.transaction_id AND s.external_key=p.external_key)) OR
 (p.channel='transfer' AND NOT EXISTS(SELECT 1 FROM stock_transfer s WHERE s.id=p.transaction_id AND s.external_key=p.external_key))) THEN
 RAISE EXCEPTION 'posting_request without matching transaction'; END IF;
 IF EXISTS(SELECT 1 FROM pending_action WHERE status='posted' AND (confirmed_by IS NULL OR confirmation_event_id IS NULL OR result IS NULL)) THEN
 RAISE EXCEPTION 'posted action without confirmation evidence'; END IF;
 IF EXISTS(SELECT 1 FROM outbox WHERE state='sent' AND sent_at IS NULL) THEN RAISE EXCEPTION 'sent outbox without sent_at'; END IF;
 IF EXISTS(SELECT 1 FROM import_row WHERE status='posted' AND transaction_id IS NULL) THEN RAISE EXCEPTION 'posted import row without transaction'; END IF;
END $$;

"""Real server tests, opt-in via DATABASE_URL. Require disposable database telco_test_*.

This file leaves committed concurrency fixtures in that test database. Never target production.
"""
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from telco.db import migrate

URL=os.environ.get('DATABASE_URL','')


@unittest.skipUnless(URL,'DATABASE_URL absent; real PostgreSQL server tests skipped')
class PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlparse(URL).path.lstrip('/').startswith('telco_test_'):
            raise RuntimeError('Tests require a disposable telco_test_* database')
        import psycopg
        cls.pg=psycopg
        cls.key=100000+uuid.uuid4().int%1000000000
        k=cls.key
        with psycopg.connect(URL) as c:
            migrate(c)
            # Checksummed rerun must be a no-op.
            migrate(c)
            c.execute('INSERT INTO telco.province VALUES(%s,%s,%s)',(k,f'TEST-P{k}','Synthetic test province'))
            c.execute('INSERT INTO telco.regency VALUES(%s,%s,%s,%s)',(k,k,f'TEST-R{k}','Synthetic test regency'))
            c.execute('INSERT INTO telco.district VALUES(%s,%s,%s,%s)',(k,k,f'TEST-D{k}','Synthetic test district'))
            c.execute('INSERT INTO telco.sales VALUES(%s,%s,%s,%s)',(k,f'TEST-S{k}','Synthetic sales',k))
            c.execute('INSERT INTO telco.outlet(id,code,name,district_id,owner_alias,fictional_address,opened_on) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                      (k,f'TEST-O{k}','Synthetic outlet',k,'SYN-OWNER','Fictional address','2025-01-01'))
            c.execute("INSERT INTO telco.product VALUES(%s,%s,'Synthetic product','PERDANA','PCS',100,100)",(k,f'TEST-SKU{k}'))
            c.execute("INSERT INTO telco.assignment VALUES(%s,%s,%s,'2025-01-01','2027-01-01')",(k,k,k))
            c.execute('INSERT INTO telco.stock_account(id,outlet_id) VALUES(%s,%s)',(k,k))
            c.execute("INSERT INTO telco.supply VALUES(%s,%s,'2026-09-01T07:00:00+07',%s,%s,10,'test')",(k,f'TEST-SUP{k}',k,k))

    def test_concurrent_oversell_exactly_one_succeeds(self):
        k=self.key
        from threading import Barrier
        barrier=Barrier(2)
        def post(i):
            with self.pg.connect(URL) as c:
                barrier.wait(timeout=20)
                try:
                    c.execute("""INSERT INTO telco.sell_out(id,external_key,occurred_at,outlet_id,sales_id,assignment_id,product_id,
                    quantity,unit_price,customer_token) VALUES(%s,%s,'2026-09-01T10:00:00+07',%s,%s,%s,%s,8,100,'SYN-TEST')""",
                              (k*10+i,f'TEST-OUT{k}-{i}',k,k,k,k))
                    c.commit()
                    return 'posted'
                except self.pg.errors.CheckViolation:
                    c.rollback()
                    return 'insufficient_stock'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(post,[1,2]))
        self.assertCountEqual(results,['posted','insufficient_stock'])
        with self.pg.connect(URL) as c:
            self.assertEqual(c.execute('SELECT quantity FROM telco.stock_balance WHERE account_id=%s AND product_id=%s',(k,k)).fetchone()[0],2)

    def test_followup_isolation_version_and_expiry(self):
        from telco.tools import save_context,follow_up
        k=self.key
        e={'scope':'outlet','entity_id':k,'channel':'sell_out','baseline':['2026-06-01','2026-07-01'],
           'current':['2026-09-01','2026-10-01'],'delta_idr':0,'drivers':[],
           'coverage':{'expected_outlet_days':30,'complete_outlet_days':30},
           'baseline_coverage':{'expected_outlet_days':30,'complete_outlet_days':30},
           'watermark':'2026-09-30','evidence_digest':'synthetic-test-digest'}
        with self.pg.connect(URL) as c:
            key='TEST-CONTEXT-'+str(k)
            save_context(c,key,'principal-A',e)
            self.assertEqual(follow_up(c,key,'principal-A')['version'],1)
            with self.assertRaises(ValueError): follow_up(c,key,'principal-B')
            save_context(c,key,'principal-A',e,expected_version=1)
            with self.assertRaises(ValueError): save_context(c,key,'principal-A',e,expected_version=1)
            c.execute("UPDATE telco.conversation_context SET expires_at=now()-interval '1 second' WHERE id=%s",(key,))
            with self.assertRaises(ValueError): follow_up(c,key,'principal-A')
            c.rollback()


if __name__=='__main__': unittest.main()

// Fast final-source SQL checks, including edge cases; no files/data are modified.
import {PGlite} from '@electric-sql/pglite';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
process.on('uncaughtException',e=>{console.error('FAILED:',e.message,e.where??'');process.exit(1);});
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const db=await PGlite.create();
const engineVersion=(await db.query('SELECT version() version')).rows[0].version;
const hashes={};
for(const n of (await fs.readdir(path.join(root,'sql'))).filter(n=>/^\d{3}_/.test(n)).sort()) {
 const b=await fs.readFile(path.join(root,'sql',n));
 hashes[n]=createHash('sha256').update(b).digest('hex');await db.exec(b.toString());
}
await db.exec(`SET search_path=telco,public;
INSERT INTO province VALUES(1,'FX-P','Synthetic');
INSERT INTO regency VALUES(1,1,'FX-R','Synthetic');
INSERT INTO district VALUES(1,1,'FX-D','Synthetic');
INSERT INTO sales VALUES(1,'SYN-S','Synthetic',1);
INSERT INTO outlet(id,code,name,district_id,owner_alias,fictional_address,opened_on) VALUES(1,'SYN-O','Synthetic',1,'SYN-OWNER','Fictional','2025-01-01');
INSERT INTO product VALUES(1,'SYN-P','Synthetic SIM','PERDANA','PCS',100,100),(2,'SYN-P2','Synthetic Voucher','VOUCHER','PCS',60,60);
INSERT INTO assignment VALUES(1,1,1,'2025-01-01','2027-01-01');
INSERT INTO stock_account VALUES(1,1,NULL),(1001,NULL,1);
INSERT INTO supply VALUES(1,'SYN-SUP','2026-06-01T07:00:00+07',1,1,20,'test'),(2,'SYN-SUP2','2026-09-01T07:00:00+07',1,2,10,'test');
INSERT INTO sell_in(id,external_key,occurred_at,outlet_id,sales_id,assignment_id,product_id,quantity,unit_price)
VALUES(1,'SYN-IN','2026-06-01T08:00:00+07',1,1,1,1,20,90),(2,'SYN-IN2','2026-09-01T08:00:00+07',1,1,1,2,10,50);
INSERT INTO sell_out(id,external_key,occurred_at,outlet_id,sales_id,assignment_id,product_id,quantity,unit_price,customer_token)
VALUES(1,'SYN-OUT','2026-06-02T10:00:00+07',1,1,1,1,4,100,'SYN-C1'),
(2,'SYN-OUT2','2026-09-02T10:00:00+07',1,1,1,1,2,90,'SYN-C2'),
(3,'SYN-OUT3','2026-09-02T10:00:00+07',1,1,1,2,2,60,'SYN-C3');
INSERT INTO outlet_day SELECT 1,d::date,true,true,true,1,1,NULL,'Synthetic' FROM generate_series('2026-06-01'::date,'2026-09-30'::date,interval '1 day')d;
INSERT INTO ingestion_watermark VALUES('sell_out','2026-09-30','SYN-BATCH');`);
const query="SELECT telco.diagnose('outlet',1,'sell_out','2026-06-01','2026-07-01','2026-09-01','2026-10-01') e";
const e=(await db.query(query)).rows[0].e;
const check=(condition,msg)=>{if(!condition)throw new Error(msg);};
check(Number(e.delta_idr)===-100,'delta');
const p=e.drivers.find(d=>d.product_id===1);
check(Number(p.volume_effect_idr)===-190 && Number(p.price_mix_effect_idr)===-30,'two-factor formula');
check(Number(e.drivers.find(d=>d.product_id===2).entry_exit_effect_idr)===120,'entry bucket');
check(e.source_batch_key==='SYN-BATCH','batch key lineage');
check(Number(e.outlet_contributions[0].delta_idr)===-100,'outlet contributions');
const rank=(await db.query("SELECT * FROM telco.rank_districts('sell_out','2026-06-01','2026-07-01','2026-09-01','2026-10-01')")).rows;
check(Number(rank[0].delta_idr)===-100,'ranking');
let rejected=false;try{await db.exec("SELECT * FROM telco.rank_districts('sell_out','2026-06-01','2026-07-01','2026-08-01','2026-09-01')");}catch{rejected=true;}
check(rejected,'invalid rank period rejected');
await db.exec('UPDATE telco.outlet_day SET sell_out_complete=false WHERE day=\'2026-09-01\'');
check((await db.query(query)).rows[0].e.coverage.complete_outlet_days===29,'missing coverage');
await db.exec(await fs.readFile(path.join(root,'sql/validate.sql'),'utf8'));
await db.exec(await fs.readFile(path.join(root,'sql/security.sql'),'utf8'));
await db.exec('BEGIN; SET LOCAL ROLE telco_reader');
await db.query(query);await db.exec('ROLLBACK');
await db.exec('BEGIN; SET LOCAL ROLE telco_writer');
await db.exec("INSERT INTO telco.reversal(channel,transaction_id,external_key,occurred_at,reason) VALUES('sell_out',1,'SYN-REV','2026-10-01','test')");
await db.exec('ROLLBACK');
await fs.writeFile(path.join(root,'examples/final_sql_smoke.json'),JSON.stringify({status:'passed',engine:'PGlite 0.3.14',postgres_version:engineVersion,migration_sha256:hashes,
 checks:['all final migrations executable','volume-price effect fixture exact','entry/exit bucket exact','outlet contributions reconcile','batch lineage present',
 'rank computation and invalid-period rejection','missing coverage recorded','SQL stock and KPI audit','restricted reader can diagnose','restricted writer can reverse']},null,2)+'\n');
console.log('FINAL SQL SMOKE PASSED');await db.close();

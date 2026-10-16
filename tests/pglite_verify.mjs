import { PGlite } from '@electric-sql/pglite';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
process.on('uncaughtException',e=>{console.error('FAILED:',e.message,e.detail??'',e.where??'');process.exit(1);});
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const db=await PGlite.create();
const reports=[];
for(const name of (await fs.readdir(path.join(root,'sql'))).filter(n=>/^\d{3}_.*\.sql$/.test(n)).sort()) {
 await db.exec(await fs.readFile(path.join(root,'sql',name),'utf8'));
 console.log('migration OK',name);
}
const names=['province','regency','district','sales','outlet','product','assignment','stock_account','supply','sell_in','sell_out','sales_target','outlet_day','ingestion_watermark'];
const started=Date.now();
for(const name of names) {
 const data=await fs.readFile(path.join(root,'data/full',name+'.csv'));
 const header=data.subarray(0,data.indexOf(10)).toString().trim();
 // Bound per-statement row versions; avoids huge hot-row version chains on WASM.
 const lines=data.toString().trimEnd().split('\n').slice(1);
 for(let offset=0;offset<lines.length;offset+=1000) {
  const csv=header+'\n'+lines.slice(offset,offset+1000).join('\n')+'\n';
  await db.exec(`COPY telco.${name} (${header}) FROM '/dev/blob' WITH (FORMAT CSV,HEADER TRUE)`,{blob:new Blob([csv])});
 }
 console.log('loaded',name,Math.round((Date.now()-started)/1000)+'s');
}
await db.exec(await fs.readFile(path.join(root,'sql/validate.sql'),'utf8'));
reports.push('SQL validate.sql passed on full dataset');
const counts=(await db.query(`SELECT (SELECT count(*) FROM telco.sell_in) sell_in,(SELECT count(*) FROM telco.sell_out) sell_out,(SELECT count(*) FROM telco.stock_ledger) ledger`)).rows[0];
if(Number(counts.sell_in)!==100000 || Number(counts.sell_out)!==400000 || Number(counts.ledger)!==700000) throw new Error('Counts mismatch');
async function fail(sql,label,expectedCode) {
 await db.exec('BEGIN'); let failed=false;
 try {await db.exec(sql);}catch(e){failed=true;if(expectedCode && e.code!==expectedCode)throw new Error(label+': wrong SQLSTATE '+e.code);}
 await db.exec('ROLLBACK');
 if(!failed)throw new Error('Expected failure: '+label);
 reports.push(label);
}
await fail(`INSERT INTO telco.sell_out(id,external_key,occurred_at,outlet_id,sales_id,assignment_id,product_id,quantity,unit_price,discount_idr,customer_token)
VALUES(99999999,'test-oversell','2026-10-01T10:00:00+07',1,1,1,1,9999999,25000,0,'SYN-TEST')`,'negative stock rejected with atomic rollback','23514');
await fail(`UPDATE telco.sell_out SET quantity=0 WHERE id=1`,'immutable sales rejected');
await fail(`INSERT INTO telco.assignment VALUES(999,1,2,'2026-01-01','2026-02-01')`,'overlapping assignment rejected');
await fail(`INSERT INTO telco.sell_in(id,external_key,occurred_at,outlet_id,sales_id,assignment_id,product_id,quantity,unit_price,discount_idr)
SELECT id,external_key,occurred_at,outlet_id,sales_id,assignment_id,product_id,quantity,unit_price,discount_idr FROM telco.sell_in WHERE id=1`,
'duplicate commercial event rejected','23505');
await fail(`INSERT INTO telco.supply VALUES(99999999,'badpcs','2026-10-01',1,1,1.5,'test')`,'fractional PCS rejected');
await fail(`INSERT INTO telco.reversal(channel,transaction_id,external_key,occurred_at,reason) VALUES('sell_out',99999999,'badref','2026-10-01','test')`,'missing reversal source rejected');
await fail(`SELECT telco.diagnose('outlet',1,'sell_out','2026-06-01','2026-07-01','2026-08-01','2026-09-01')`,'unequal comparison rejected');
await fail(`SELECT telco.diagnose('outlet',99999,'sell_out','2026-06-01','2026-07-01','2026-09-01','2026-10-01')`,'unknown outlet rejected');
await db.exec('BEGIN');
const tx=(await db.query('SELECT * FROM telco.sell_out WHERE id=1')).rows[0];
const original=(await db.query("SELECT sum(net_revenue) revenue FROM telco.v_commercial_event WHERE channel='sell_out'")).rows[0].revenue;
await db.exec(`INSERT INTO telco.reversal(channel,transaction_id,external_key,occurred_at,reason) VALUES('sell_out',1,'test-reverse','2026-10-01','test')`);
const after=(await db.query("SELECT sum(net_revenue) revenue FROM telco.v_commercial_event WHERE channel='sell_out'")).rows[0].revenue;
if(Math.abs(Number(original)-Number(after)-Number(tx.net_revenue))>.01)throw new Error('Reversal revenue mismatch');
await db.exec(await fs.readFile(path.join(root,'sql/validate.sql'),'utf8'));
await db.exec('ROLLBACK'); reports.push('reversal stock and net KPI reconcile; reversed in rollback');
const results={};
for(const scope of ['outlet','district']) {
 const e=(await db.query(`SELECT telco.diagnose($1,1,'sell_out','2026-06-01','2026-07-01','2026-09-01','2026-10-01') evidence`,[scope])).rows[0].evidence;
 const drivers=e.drivers;
 const sum=drivers.reduce((s,d)=>s+Number(d.delta_idr),0);
 const residual=drivers.reduce((s,d)=>s+Number(d.delta_idr)-Number(d.volume_effect_idr)-Number(d.price_mix_effect_idr)-Number(d.entry_exit_effect_idr),0);
 if(Math.abs(sum-Number(e.delta_idr))>.01 || Math.abs(residual)>.01 || Number(e.delta_idr)>=0) throw new Error('Diagnosis scenario reconciliation');
 results[scope]=e; await fs.writeFile(path.join(root,'examples',scope+'_evidence.json'),JSON.stringify(e,null,2)+'\n');
}
reports.push('outlet and district decomposition reconcile to <0.01 IDR');
for(const e of Object.values(results)) {
 const sum=e.outlet_contributions.reduce((s,o)=>s+Number(o.delta_idr),0);
 if(Math.abs(sum-Number(e.delta_idr))>.01)throw new Error('Outlet contributions mismatch');
}
reports.push('outlet contributions reconcile at district and outlet grain');
// Coverage distinction and authorized scope are tested inside a rollback fixture.
await db.exec('BEGIN');
await db.exec("UPDATE telco.outlet_day SET sell_out_complete=false WHERE outlet_id=1 AND day='2026-09-01'");
let cov=(await db.query("SELECT telco.diagnose('outlet',1,'sell_out','2026-06-01','2026-07-01','2026-09-01','2026-10-01') e")).rows[0].e;
if(cov.coverage.complete_outlet_days!==29)throw new Error('Missing sell-out coverage');
cov=(await db.query("SELECT telco.diagnose('outlet',1,'sell_in','2026-06-01','2026-07-01','2026-09-01','2026-10-01') e")).rows[0].e;
if(cov.coverage.complete_outlet_days!==30)throw new Error('Channels conflate coverage');
await db.exec("INSERT INTO telco.principal_grant(principal_id,scope_type,entity_id) VALUES('SYN-A','outlet',1)");
let a=(await db.query("SELECT telco.can_access('SYN-A','outlet',1) yes,telco.can_access('SYN-A','outlet',2) no,telco.can_access('SYN-A','district',1) district_no")).rows[0];
if(!a.yes || a.no || a.district_no)throw new Error('Authorization failure');
await db.exec('ROLLBACK');
reports.push('coverage separated per channel; principal scope default-deny verified');
await db.exec(await fs.readFile(path.join(root,'sql/security.sql'),'utf8'));
await fail("SET LOCAL ROLE telco_reader; UPDATE telco.stock_balance SET quantity=999",'reader cannot mutate balances','42501');
await fail("SET LOCAL ROLE telco_writer; INSERT INTO telco.principal_grant(principal_id,scope_type,entity_id) VALUES('bad','district',1)",'writer cannot grant itself scope','42501');
await db.exec('BEGIN; SET LOCAL ROLE telco_writer');
await db.exec("INSERT INTO telco.reversal(channel,transaction_id,external_key,occurred_at,reason) VALUES('sell_out',1,'writer-test-reverse','2026-10-01','test')");
await db.exec('ROLLBACK');
reports.push('restricted writer can post reversal via SECURITY DEFINER trigger only');
const ranked=(await db.query(`SELECT * FROM telco.rank_districts('sell_out','2026-06-01','2026-07-01','2026-09-01','2026-10-01')`)).rows;
await fs.writeFile(path.join(root,'examples/district_ranking.json'),JSON.stringify(ranked,null,2)+'\n');
await fs.writeFile(path.join(root,'examples/postgres_verification.json'),JSON.stringify({status:'passed',engine:'PGlite 0.3.14 (PostgreSQL WASM); not a multi-connection server',counts,checks:reports,elapsed_seconds:Math.round((Date.now()-started)/1000),limitations:['Cloud connection/TLS and two-session concurrency not exercised by this engine.']},null,2)+'\n');
console.log('ALL PASSED',counts);
await db.close();

"""Bounded SQL-backed tool adapter. API must authenticate/authorize BEFORE invoking it."""
import argparse
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from .db import connect


def recommend(e):
    """Facts remain separate from hypotheses; no numerical causal probability invented."""
    c=e['coverage']; b=e['baseline_coverage']
    watermark=e.get('watermark')
    quality_ok=(c['expected_outlet_days']>0 and b['expected_outlet_days']>0
                and c['complete_outlet_days']==c['expected_outlet_days']
                and b['complete_outlet_days']==b['expected_outlet_days']
                and watermark is not None and str(watermark)>=str(date.fromisoformat(e['current'][1])-timedelta(days=1)))
    actions=[]
    if not quality_ok:
        actions.append({'priority':1,'action':'Verifikasi kelengkapan input dan watermark sebelum menyimpulkan penurunan.',
                        'evidence':['coverage','baseline_coverage','watermark'],'owner':'Data Operations','review_in_days':1,
                        'success_metric':'100% expected outlet-days complete', 'confidence':'insufficient_data'})
    elif e['delta_idr']<0:
        if c.get('visits_planned',0) and (c.get('visits_done') or 0)<c['visits_planned']*.8:
            actions.append({'priority':1,'action':'Jadwalkan kunjungan verifikasi outlet dan tanyakan kendala; uji pemulihan kunjungan pada outlet pembanding.',
                            'evidence':['delta_idr','coverage.visits_done','coverage.visits_planned'],'owner':'Sales Supervisor',
                            'review_in_days':7,'success_metric':'Visit completion >= 80%; compare sell-out/day vs matched controls',
                            'confidence':'hypothesis; visits and sales co-occur, causality unproven'})
        declining=[d for d in e['drivers'] if d['delta_idr']<0]
        if declining:
            actions.append({'priority':2,'action':'Audit volume dan harga efektif SKU dengan kontribusi negatif terbesar; cek ketersediaan fisik dan permintaan sebelum mengubah stok/harga.',
                            'product_id':declining[0]['product_id'],'evidence':['drivers[0]'],'owner':'Sales + Trade Marketing',
                            'review_in_days':7,'success_metric':'SKU net revenue/day and effective price; guardrail discount cost',
                            'confidence':'accounting contribution verified; underlying demand unknown'})
    else:
        actions.append({'priority':3,'action':'Pantau periode sebanding berikutnya; tidak ada bukti penurunan agregat pada cakupan ini.',
                        'evidence':['delta_idr'],'owner':'Sales Supervisor','review_in_days':7,
                        'success_metric':'Net revenue/day','confidence':'descriptive'})
    return {'data_ready':quality_ok,'actions':actions,
            'limitations':['Decomposition is not causal attribution.',
                          'Stockout is unknown when observed_stockout_minutes is null; zero closing balance is not proof of lost demand.',
                          'Price effect includes discounts and within-SKU mix; taxes, COGS and competitor data absent.',
                          'No uplift forecast or confidence interval without an experiment/model; actions need human review.']}


def diagnose(conn, scope, entity_id, channel, b0, b1, c0, c1):
    if scope not in ('outlet','district') or channel not in ('sell_in','sell_out'):
        raise ValueError('Unsupported scope/channel')
    args=[date.fromisoformat(x) for x in (b0,b1,c0,c1)]
    # One snapshot for all evidence; caller should use REPEATABLE READ READ ONLY for plain diagnosis.
    e=conn.execute('SELECT telco.diagnose(%s,%s,%s,%s,%s,%s,%s)',(scope,int(entity_id),channel,*args)).fetchone()[0]
    total=sum((Decimal(str(d['delta_idr'])) for d in e['drivers']),Decimal(0))
    residual=sum((Decimal(str(d['delta_idr']))-Decimal(str(d['volume_effect_idr']))-
                  Decimal(str(d['price_mix_effect_idr']))-Decimal(str(d['entry_exit_effect_idr'])) for d in e['drivers']),Decimal(0))
    if abs(total-Decimal(str(e['delta_idr'])))>Decimal('.01') or abs(residual)>Decimal('.01'):
        raise ValueError('Decomposition does not reconcile')
    e['reconciliation_residual_idr']=str(residual)
    e['recommendation']=recommend(e)
    e['evidence_digest']=hashlib.sha256(json.dumps(e,sort_keys=True,default=str).encode()).hexdigest()
    return e


def authorized_diagnose(conn, principal, scope, entity_id, channel, b0, b1, c0, c1):
    """Public service boundary; principal must be resolved from verified JWT/WA mapping."""
    allowed=conn.execute('SELECT telco.can_access(%s,%s,%s)',(principal,scope,entity_id)).fetchone()[0]
    if not allowed: raise PermissionError('Scope not granted')
    return diagnose(conn,scope,entity_id,channel,b0,b1,c0,c1)


def rank_authorized_districts(conn, principal, channel, b0, b1, c0, c1):
    dates=[date.fromisoformat(x) for x in (b0,b1,c0,c1)]
    rows=conn.execute('''SELECT r.* FROM telco.rank_districts(%s,%s,%s,%s,%s) r
      WHERE telco.can_access(%s,'district',r.district_id) ORDER BY r.delta_idr,r.district_id''',
      (channel,*dates,principal)).fetchall()
    return [dict(zip(['district_id','district_name','baseline_idr','current_idr','delta_idr','growth'],r)) for r in rows]


def save_context(conn, context_id, principal, evidence, expected_version=None):
    """Principal is trusted authentication output, never an LLM-supplied identity."""
    from psycopg.types.json import Jsonb
    scope={'type':evidence['scope'],'id':evidence['entity_id']}
    if expected_version is None:
        conn.execute('''INSERT INTO telco.conversation_context(id,principal_id,channel,scope,metric,
         baseline_start,baseline_end,current_start,current_end,evidence,expires_at)
         VALUES(%s,%s,'whatsapp',%s,%s,%s,%s,%s,%s,%s,now()+interval '24 hours')''',
         (context_id,principal,Jsonb(scope),evidence['channel'],*evidence['baseline'],*evidence['current'],Jsonb(evidence)))
    else:
        row=conn.execute('''UPDATE telco.conversation_context SET scope=%s,metric=%s,baseline_start=%s,baseline_end=%s,
          current_start=%s,current_end=%s,evidence=%s,version=version+1,updated_at=now(),expires_at=now()+interval '24 hours'
          WHERE id=%s AND principal_id=%s AND version=%s AND expires_at>now() RETURNING version''',
          (Jsonb(scope),evidence['channel'],*evidence['baseline'],*evidence['current'],Jsonb(evidence),context_id,principal,expected_version)).fetchone()
        if not row: raise ValueError('Context stale, unauthorized or version conflict')
    conn.execute('INSERT INTO telco.analysis_audit(context_id,tool_name,parameters,result_digest) VALUES(%s,%s,%s,%s)',
                 (context_id,'diagnose-v1',Jsonb(scope),evidence['evidence_digest']))


def follow_up(conn, context_id, principal):
    row=conn.execute('''SELECT evidence,version,expires_at FROM telco.conversation_context
      WHERE id=%s AND principal_id=%s AND expires_at>now()''',(context_id,principal)).fetchone()
    if not row: raise ValueError('Context missing, unauthorized or expired; ask for scope/period')
    # Evidence remains pinned; API must reauthorize scope and re-run diagnosis if data watermark/batch changed.
    return {'context_id':context_id,'version':row[1],'expires_at':str(row[2]),'evidence':row[0],
            'recommendation':recommend(row[0]),'requires_scope_reauthorization':True}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--scope',choices=['outlet','district'],required=True)
    ap.add_argument('--id',type=int,required=True)
    ap.add_argument('--channel',choices=['sell_in','sell_out'],default='sell_out')
    ap.add_argument('--baseline',nargs=2,default=['2026-06-01','2026-07-01'])
    ap.add_argument('--current',nargs=2,default=['2026-09-01','2026-10-01'])
    a=ap.parse_args()
    with connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        e=diagnose(conn,a.scope,a.id,a.channel,*a.baseline,*a.current)
        print(json.dumps(e,indent=2,default=str,ensure_ascii=False))


if __name__=='__main__': main()

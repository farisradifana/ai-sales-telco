"""Trusted operator demo: diagnosis -> persist -> 'apa yang harus dilakukan?'.
Run from project root: python -m examples.context_demo
Uses DATABASE_URL; writes a synthetic grant, context and audit only, no outbound messages.
"""
import json
import uuid
from telco.db import connect
from telco.tools import authorized_diagnose,save_context,follow_up


def main():
    principal='SYN-DEMO-PRINCIPAL'
    context_id='SYN-DEMO-'+str(uuid.uuid4())
    with connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        # Provision demo only with trusted owner; production provision is admin/IAM driven.
        conn.execute("INSERT INTO telco.principal_grant(principal_id,scope_type,entity_id) VALUES(%s,'outlet',1) ON CONFLICT DO NOTHING",(principal,))
        evidence=authorized_diagnose(conn,principal,'outlet',1,'sell_out','2026-06-01','2026-07-01','2026-09-01','2026-10-01')
        save_context(conn,context_id,principal,evidence)
    with connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        if not conn.execute("SELECT telco.can_access(%s,'outlet',1)",(principal,)).fetchone()[0]:
            raise PermissionError('Grant revoked')
        answer=follow_up(conn,context_id,principal)
        print(json.dumps(answer,indent=2,ensure_ascii=False,default=str))


if __name__=='__main__': main()

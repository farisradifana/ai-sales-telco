"""Generate importable n8n 2.35.7 workflow JSON (node typeVersions verified with `n8n export:nodes`).

  python n8n/build_workflows.py [--api-base http://api:8000] [--out n8n/workflows]

Rules: no credentials inline (only credential *references* by stable id), no SQL nodes, no user text interpolated into
anything but JSON bodies sent to the API. Credential ids to create/bind (see n8n/README.md):
  telcoApiService     httpHeaderAuth  Authorization: Bearer <N8N_SERVICE_TOKEN>
  telcoDispatchHeader httpHeaderAuth  X-Telco-Dispatch-Token: <N8N_DISPATCH_SECRET>
  telcoRecapJwt       httpHeaderAuth  Authorization: Bearer <JWT of recap principal>   (workflow 07)
  telcoAdminJwt       httpHeaderAuth  Authorization: Bearer <JWT of admin principal>   (workflow 08)
  googleSheetsRecap   googleSheetsOAuth2Api                                            (workflow 07)
"""
import argparse
import json
from pathlib import Path

RECAP_SHEET_ID = '1XAlDtkIA5gkdodi-fZip5x7Lbyri-YRk4dRJLOHPwRg'
CRED = {'svc': {'httpHeaderAuth': {'id': 'telcoApiService', 'name': 'TELCO API service token'}},
        'dispatch': {'httpHeaderAuth': {'id': 'telcoDispatchHeader', 'name': 'TELCO dispatch header'}},
        'recap': {'httpHeaderAuth': {'id': 'telcoRecapJwt', 'name': 'TELCO recap principal JWT'}},
        'admin': {'httpHeaderAuth': {'id': 'telcoAdminJwt', 'name': 'TELCO admin principal JWT'}},
        'sheets': {'googleSheetsOAuth2Api': {'id': 'googleSheetsRecap', 'name': 'Google Sheets recap'}}}
REF = "={{ JSON.stringify({ provider: $('Event').first().json.provider, message_id: $('Event').first().json.message_id }) }}"
SETTINGS = {'executionOrder': 'v1', 'timezone': 'Asia/Jakarta', 'saveDataSuccessExecution': 'none',
            'saveDataErrorExecution': 'all', 'saveManualExecutions': False, 'callerPolicy': 'workflowsFromSameOwner'}


def http(name, pos, path, api, body=REF, method='POST', cred='svc', retry=True, timeout=60000):
    node = {'id': name.lower().replace(' ', '-'), 'name': name, 'type': 'n8n-nodes-base.httpRequest', 'typeVersion': 4.2,
            'position': pos, 'credentials': CRED[cred],
            'parameters': {'method': method, 'url': api + path, 'authentication': 'genericCredentialType',
                           'genericAuthType': 'httpHeaderAuth', 'options': {'timeout': timeout}}}
    if method == 'POST':
        node['parameters'].update({'sendBody': True, 'specifyBody': 'json', 'jsonBody': body})
    if retry:
        node.update({'retryOnFail': True, 'maxTries': 3, 'waitBetweenTries': 3000})
    return node


def chain(names):
    return {a: {'main': [[{'node': b, 'type': 'main', 'index': 0}]]} for a, b in zip(names, names[1:])}


def sub_trigger(pos=(0, 0)):
    return {'id': 'event', 'name': 'Event', 'type': 'n8n-nodes-base.executeWorkflowTrigger', 'typeVersion': 1.1,
            'position': list(pos), 'parameters': {'inputSource': 'passthrough'}}


def wf(wid, name, nodes, connections, active=False):
    return {'id': wid, 'name': name, 'active': active, 'nodes': nodes, 'connections': connections, 'settings': SETTINGS,
            'pinData': {}, 'tags': [], 'meta': {'templateCredsSetupCompleted': False, 'telcoGeneratedBy': 'n8n/build_workflows.py'}}


def build(api: str) -> dict:
    out = {}
    # 01: internal webhook from the API dispatcher (signature already verified at ingress). ACK immediately; in queue
    # mode the execution runs on a worker. Only provider+message_id travel; principal is resolved by the API.
    nodes = [
        {'id': 'inbound', 'name': 'Inbound', 'type': 'n8n-nodes-base.webhook', 'typeVersion': 2, 'position': [0, 0],
         'webhookId': 'telco-inbound', 'credentials': CRED['dispatch'],
         'parameters': {'httpMethod': 'POST', 'path': 'telco-inbound', 'authentication': 'headerAuth', 'responseMode': 'onReceived',
                        'responseCode': 202, 'options': {}}},
        {'id': 'event', 'name': 'Event', 'type': 'n8n-nodes-base.set', 'typeVersion': 3.4, 'position': [220, 0],
         'parameters': {'mode': 'manual', 'includeOtherFields': False, 'assignments': {'assignments': [
             {'id': 'p', 'name': 'provider', 'type': 'string', 'value': '={{ $json.body.provider }}'},
             {'id': 'm', 'name': 'message_id', 'type': 'string', 'value': '={{ $json.body.message_id }}'}]}, 'options': {}}},
        http('Claim', [440, 0], '/internal/v1/events/claim', api),
        {'id': 'claimed', 'name': 'Claimed?', 'type': 'n8n-nodes-base.if', 'typeVersion': 2.2, 'position': [660, 0],
         'parameters': {'conditions': {'options': {'caseSensitive': True, 'typeValidation': 'strict', 'version': 2},
                                       'conditions': [{'id': 'c1', 'leftValue': '={{ $json.status }}', 'rightValue': 'claimed',
                                                       'operator': {'type': 'string', 'operation': 'equals'}}], 'combinator': 'and'},
                        'options': {}}},
        http('Intent', [880, -100], '/internal/v1/events/intent', api),
        {'id': 'ref', 'name': 'Ref', 'type': 'n8n-nodes-base.set', 'typeVersion': 3.4, 'position': [1000, -100],
         'parameters': {'mode': 'manual', 'includeOtherFields': False, 'assignments': {'assignments': [
             {'id': 'p', 'name': 'provider', 'type': 'string', 'value': "={{ $('Event').first().json.provider }}"},
             {'id': 'm', 'name': 'message_id', 'type': 'string', 'value': "={{ $('Event').first().json.message_id }}"},
             {'id': 'i', 'name': 'intent', 'type': 'string', 'value': '={{ $json.name }}'}]}, 'options': {}}},
        {'id': 'route', 'name': 'Route', 'type': 'n8n-nodes-base.switch', 'typeVersion': 3.2, 'position': [1100, -100],
         'parameters': {'mode': 'expression', 'numberOutputs': 4,
                        'output': "={{ ({sales_input:0, confirm:0, cancel:0, insight:1, follow_up:2})[$json.intent] ?? 3 }}", 'options': {}}},
        {'id': 'sales', 'name': '02 Sales input', 'type': 'n8n-nodes-base.executeWorkflow', 'typeVersion': 1.2, 'position': [1340, -300],
         'parameters': {'source': 'database', 'workflowId': {'__rl': True, 'mode': 'id', 'value': 'telco02salesinput'},
                        'workflowInputs': {'mappingMode': 'defineBelow', 'value': {}}, 'mode': 'once', 'options': {'waitForSubWorkflow': True}}},
        {'id': 'insight', 'name': '03 Insight', 'type': 'n8n-nodes-base.executeWorkflow', 'typeVersion': 1.2, 'position': [1340, -150],
         'parameters': {'source': 'database', 'workflowId': {'__rl': True, 'mode': 'id', 'value': 'telco03insight0001'},
                        'workflowInputs': {'mappingMode': 'defineBelow', 'value': {}}, 'mode': 'once', 'options': {'waitForSubWorkflow': True}}},
        {'id': 'followup', 'name': '04 Follow-up', 'type': 'n8n-nodes-base.executeWorkflow', 'typeVersion': 1.2, 'position': [1340, 0],
         'parameters': {'source': 'database', 'workflowId': {'__rl': True, 'mode': 'id', 'value': 'telco04followup01'},
                        'workflowInputs': {'mappingMode': 'defineBelow', 'value': {}}, 'mode': 'once', 'options': {'waitForSubWorkflow': True}}},
        http('Act (other)', [1340, 150], '/internal/v1/events/act', api),
        http('Reply (other)', [1560, 150], '/internal/v1/events/reply', api),
        http('Complete (other)', [1780, 150], '/internal/v1/events/complete', api),
    ]
    sub_body = "={{ JSON.stringify({ provider: $('Event').first().json.provider, message_id: $('Event').first().json.message_id }) }}"
    conns = {'Inbound': {'main': [[{'node': 'Event', 'type': 'main', 'index': 0}]]},
             'Event': {'main': [[{'node': 'Claim', 'type': 'main', 'index': 0}]]},
             'Claim': {'main': [[{'node': 'Claimed?', 'type': 'main', 'index': 0}]]},
             'Claimed?': {'main': [[{'node': 'Intent', 'type': 'main', 'index': 0}], []]},
             'Intent': {'main': [[{'node': 'Ref', 'type': 'main', 'index': 0}]]},
             'Ref': {'main': [[{'node': 'Route', 'type': 'main', 'index': 0}]]},
             'Route': {'main': [[{'node': '02 Sales input', 'type': 'main', 'index': 0}], [{'node': '03 Insight', 'type': 'main', 'index': 0}],
                                [{'node': '04 Follow-up', 'type': 'main', 'index': 0}], [{'node': 'Act (other)', 'type': 'main', 'index': 0}]]},
             **chain(['Act (other)', 'Reply (other)', 'Complete (other)'])}
    out['01_inbound_router'] = wf('telco01inbound001', 'TELCO 01 Inbound router', nodes, conns)

    def sub(wid, name, steps):
        ns = [sub_trigger()]
        x = 220
        names = ['Event']
        for label, path in steps:
            ns.append(http(label, [x, 0], path, api, body=sub_body))
            names.append(label)
            x += 220
        return wf(wid, name, ns, chain(names))

    out['02_sales_input'] = sub('telco02salesinput', 'TELCO 02 Sales input (draft/confirm)',
                                [('Act', '/internal/v1/events/act'), ('Reply', '/internal/v1/events/reply'), ('Complete', '/internal/v1/events/complete')])
    out['03_insight'] = sub('telco03insight0001', 'TELCO 03 Insight (tool -> Hermes -> chart)',
                            [('Act (scoped tool)', '/internal/v1/events/act'), ('Narrate (Hermes)', '/internal/v1/events/narrate'),
                             ('Reply (+signed chart)', '/internal/v1/events/reply'), ('Complete', '/internal/v1/events/complete')])
    out['04_follow_up'] = sub('telco04followup01', 'TELCO 04 Follow-up (persisted context)',
                              [('Act (context)', '/internal/v1/events/act'), ('Reply', '/internal/v1/events/reply'), ('Complete', '/internal/v1/events/complete')])

    def sched(wid, name, rule, label, path, body):
        ns = [{'id': 'schedule', 'name': 'Schedule', 'type': 'n8n-nodes-base.scheduleTrigger', 'typeVersion': 1.2, 'position': [0, 0],
               'parameters': {'rule': {'interval': [rule]}}},
              http(label, [220, 0], path, api, body=body, retry=False)]
        return wf(wid, name, ns, chain(['Schedule', label]))

    out['05_daily_target'] = sched('telco05dailytarget', 'TELCO 05 Daily target nudges (WIB)',
                                   {'field': 'cronExpression', 'expression': '0 5 7-20 * * 1-6'}, 'Run nudges', '/internal/v1/targets/nudges', '={{ "{}" }}')
    out['06_outbox_dispatcher'] = sched('telco06outbox00001', 'TELCO 06 Outbox dispatcher', {'field': 'seconds', 'secondsInterval': 20},
                                        'Dispatch batch', '/internal/v1/outbox/dispatch',
                                        '={{ JSON.stringify({ limit: 20, lease_seconds: 60, worker_id: "n8n-" + $execution.id }) }}')

    # 07: scoped export snapshot -> Google Sheet recap (overwrite tab). Source of truth stays in PostgreSQL.
    recap_nodes = [
        {'id': 'schedule', 'name': 'Schedule', 'type': 'n8n-nodes-base.scheduleTrigger', 'typeVersion': 1.2, 'position': [0, 0],
         'parameters': {'rule': {'interval': [{'field': 'cronExpression', 'expression': '0 30 6 * * *'}]}}},
        {'id': 'params', 'name': 'Recap params', 'type': 'n8n-nodes-base.set', 'typeVersion': 3.4, 'position': [220, 0],
         'parameters': {'mode': 'manual', 'includeOtherFields': False, 'assignments': {'assignments': [
             {'id': 's', 'name': 'scope', 'type': 'string', 'value': 'province'},
             {'id': 'e', 'name': 'entity_id', 'type': 'number', 'value': 1},  # Provinsi Jawa Timur
             {'id': 'c', 'name': 'channel', 'type': 'string', 'value': 'sell_out'},
             {'id': 'a', 'name': 'start', 'type': 'string', 'value': "={{ $now.setZone('Asia/Jakarta').minus({days: 30}).toISODate() }}"},
             {'id': 'b', 'name': 'end', 'type': 'string', 'value': "={{ $now.setZone('Asia/Jakarta').toISODate() }}"}]}, 'options': {}}},
        {**http('Export CSV', [440, 0], '/v1/exports/sales.csv', api, method='GET', cred='recap'),
         'parameters': {'method': 'GET', 'url': api + '/v1/exports/sales.csv', 'authentication': 'genericCredentialType',
                        'genericAuthType': 'httpHeaderAuth', 'sendQuery': True, 'specifyQuery': 'keypair',
                        'queryParameters': {'parameters': [{'name': k, 'value': '={{ $json.%s }}' % k} for k in ('scope', 'entity_id', 'channel', 'start', 'end')]},
                        'options': {'response': {'response': {'responseFormat': 'text', 'outputPropertyName': 'csv'}}, 'timeout': 60000}}},
        # Overwrite, not accumulate: the tab always holds exactly one snapshot (with its watermark/total metadata).
        {'id': 'clear', 'name': 'Clear Rekap tab', 'type': 'n8n-nodes-base.googleSheets', 'typeVersion': 4.5, 'position': [660, -160],
         'credentials': CRED['sheets'], 'executeOnce': True,
         'parameters': {'authentication': 'oAuth2', 'resource': 'sheet', 'operation': 'clear',
                        'documentId': {'__rl': True, 'mode': 'id', 'value': RECAP_SHEET_ID},
                        'sheetName': {'__rl': True, 'mode': 'name', 'value': 'Rekap'}, 'clear': 'wholeSheet', 'keepFirstRow': False}},
        {'id': 'rows', 'name': 'CSV to rows', 'type': 'n8n-nodes-base.code', 'typeVersion': 2, 'position': [660, 0],
         'parameters': {'language': 'javaScript', 'mode': 'runOnceForAllItems', 'jsCode': (
             "// Metadata lines (# key: value) become a header block; data rows are kept as exported (no recalculation).\n"
             "const text = $('Export CSV').first().json.csv || '';\n"
             "const lines = text.split('\\n').filter(l => l.length);\n"
             "const meta = lines.filter(l => l.startsWith('# ')).map(l => { const i = l.indexOf(': '); return { kolom_a: l.slice(2, i), kolom_b: l.slice(i + 2) }; });\n"
             "const data = lines.filter(l => !l.startsWith('# '));\n"
             "const header = data.shift().split(',');\n"
             "const rows = data.map(l => { const v = l.split(','); return Object.fromEntries(header.map((h, i) => [h, v[i] ?? ''])); });\n"
             "return [...meta.map(m => ({ json: { day_wib: '#' + m.kolom_a, outlet_code: m.kolom_b } })), ...rows.map(r => ({ json: r }))];")}},
        {'id': 'sheet', 'name': 'Write Google Sheet', 'type': 'n8n-nodes-base.googleSheets', 'typeVersion': 4.5, 'position': [880, 0],
         'credentials': CRED['sheets'],
         'parameters': {'authentication': 'oAuth2', 'resource': 'sheet', 'operation': 'append',
                        'documentId': {'__rl': True, 'mode': 'id', 'value': RECAP_SHEET_ID},
                        'sheetName': {'__rl': True, 'mode': 'name', 'value': 'Rekap'},
                        'columns': {'mappingMode': 'autoMapInputData', 'value': {}, 'matchingColumns': [], 'schema': []},
                        'options': {}}},
    ]
    out['07_spreadsheet_recap'] = wf('telco07recap000001', 'TELCO 07 Spreadsheet recap (export snapshot)', recap_nodes,
                                     chain(['Schedule', 'Recap params', 'Export CSV', 'Clear Rekap tab', 'CSV to rows', 'Write Google Sheet']))

    # 08: admin uploads a recap CSV -> API staging (hash, validation, preview). Posting needs admin confirm in dashboard.
    imp_nodes = [
        {'id': 'upload', 'name': 'Upload', 'type': 'n8n-nodes-base.webhook', 'typeVersion': 2, 'position': [0, 0], 'webhookId': 'telco-import',
         'credentials': CRED['admin'],
         'parameters': {'httpMethod': 'POST', 'path': 'telco-import', 'authentication': 'headerAuth', 'responseMode': 'lastNode',
                        'options': {'binaryPropertyName': 'file'}}},
        {**http('Stage import', [220, 0], '/v1/imports', api, cred='admin', retry=False),
         'parameters': {'method': 'POST', 'url': api + '/v1/imports', 'authentication': 'genericCredentialType', 'genericAuthType': 'httpHeaderAuth',
                        'sendBody': True, 'contentType': 'multipart-form-data',
                        'bodyParameters': {'parameters': [{'parameterType': 'formBinaryData', 'name': 'file', 'inputDataFieldName': 'file0'}]},
                        'options': {'timeout': 120000}}},
    ]
    out['08_import_staging'] = wf('telco08import00001', 'TELCO 08 Spreadsheet import -> staging', imp_nodes, chain(['Upload', 'Stage import']))
    return out


LANES = [  # key, title, description, sticky color (n8n 1..7), active by default
    ('router', '01 · Pesan masuk WhatsApp (router)',
     'Event dari API ingress (signature sudah diverifikasi) → klaim → kenali niat → arahkan ke jalur di bawah. '
     'Hanya provider + message_id yang lewat; akun pengirim ditentukan API.', 5, True),
    ('sales', '02 · Input penjualan',
     'Draft penjualan → ringkasan konfirmasi → balasan YA/BATAL → posting atomik ke stok & ledger.', 4, True),
    ('insight', '03 · Insight omzet (AI)',
     'Tool analitik terotorisasi → narasi Hermes dari evidence → balasan + tautan grafik bertanda tangan.', 6, True),
    ('follow', '04 · Follow-up "apa yang harus dilakukan?"',
     'Pakai konteks tersimpan (outlet/channel/periode sama), cek izin ulang, hitung ulang bila data berubah.', 2, True),
    ('other', '01b · Perintah lain (stok, target, peringkat, opt-in)',
     'Stok yang dipegang, target harian, peringkat kecamatan/kab-kota, STOP/MULAI TARGET.', 7, True),
    ('target', '05 · Pengingat target harian (NONAKTIF)',
     'Jam kerja WIB, hanya yang opt-in, sekali per hari. Aktifkan setelah template WhatsApp Utility disetujui Meta.', 1, False),
    ('outbox', '06 · Pengirim pesan keluar (outbox)',
     'Tiap 20 detik: ambil antrean (SKIP LOCKED + lease), kirim, retry bertahap, dead-letter bila gagal permanen.', 3, True),
    ('recap', '07 · Rekap Google Sheet (NONAKTIF)',
     'Export snapshot ter-scope (watermark, channel, satuan, total) → tab Rekap. Aktifkan setelah credential Google di-bind.', 4, False),
    ('import', '08 · Import CSV → staging (NONAKTIF)',
     'Upload CSV admin → validasi & preview di API; posting hanya setelah konfirmasi admin di dashboard.', 2, False),
]
LANE_H, NODE_W = 260, 220


def build_combined(parts: dict, enable: frozenset = frozenset()) -> dict:
    """One canvas: every flow in its own horizontal lane with a coloured sticky note (title + function).
    Router outputs connect straight to the lanes (no sub-workflows). Do NOT activate together with 01-08 (same webhook)."""
    src = {
        'router': parts['01_inbound_router'], 'sales': parts['02_sales_input'], 'insight': parts['03_insight'],
        'follow': parts['04_follow_up'], 'target': parts['05_daily_target'], 'outbox': parts['06_outbox_dispatcher'],
        'recap': parts['07_spreadsheet_recap'], 'import': parts['08_import_staging'],
    }
    prefix = {'sales': 'Sales · ', 'insight': 'Insight · ', 'follow': 'Follow-up · ', 'other': 'Lainnya · ', 'target': 'Target · ',
              'outbox': 'Outbox · ', 'recap': 'Rekap · ', 'import': 'Import · '}
    nodes, conns, first = [], {}, {}
    for lane_i, (key, title, desc, color, active) in enumerate(LANES):
        y = lane_i * LANE_H
        if key in enable and not active:
            active, title = True, title.replace(' (NONAKTIF)', ' (AKTIF)')
        if key == 'router':
            lane_nodes = [dict(n) for n in src['router']['nodes'] if n['type'] != 'n8n-nodes-base.executeWorkflow'
                          and not n['name'].endswith('(other)')]
            lane_conns = {k: v for k, v in src['router']['connections'].items() if not k.endswith('(other)')}
            rename = {}
        elif key == 'other':
            lane_nodes = [dict(n) for n in src['router']['nodes'] if n['name'].endswith('(other)')]
            rename = {n['name']: prefix['other'] + n['name'].replace(' (other)', '') for n in lane_nodes}
            lane_conns = {k: v for k, v in src['router']['connections'].items() if k.endswith('(other)')}
        else:
            lane_nodes = [dict(n) for n in src[key]['nodes'] if n['type'] != 'n8n-nodes-base.executeWorkflowTrigger']
            rename = {n['name']: prefix[key] + n['name'] for n in lane_nodes}
            lane_conns = {k: v for k, v in src[key]['connections'].items() if k != 'Event'}
        ordered = lane_nodes  # generator already lists nodes in flow order
        for x_i, n in enumerate(ordered):
            n['name'] = rename.get(n['name'], n['name'])
            # Keep expression references ($('Node')) valid after lane prefixes are added.
            params = json.dumps(n.get('parameters', {}), ensure_ascii=False)
            for old, new in rename.items():
                params = params.replace(f"$('{old}')", f"$('{new}')")
            n['parameters'] = json.loads(params)
            n['id'] = f'{key}-{x_i}'
            n['position'] = [80 + x_i * NODE_W, y + 110]
            if not active and n['type'] in ('n8n-nodes-base.scheduleTrigger', 'n8n-nodes-base.webhook'):
                n['disabled'] = True
            nodes.append(n)
        if ordered:
            first[key] = ordered[0]['name']
        for a, spec in lane_conns.items():
            conns[rename.get(a, a)] = {'main': [[{**c, 'node': rename.get(c['node'], c['node'])} for c in branch]
                                                for branch in spec['main']]}
        nodes.append({'id': f'note-{key}', 'name': f'Catatan {title}', 'type': 'n8n-nodes-base.stickyNote', 'typeVersion': 1,
                      'position': [40, y], 'parameters': {'content': f'## {title}\n{desc}', 'color': color,
                                                          'width': 80 + max(1, len(ordered)) * NODE_W + 40, 'height': LANE_H - 20}})
    # Router branches -> lane entry nodes.
    conns['Route'] = {'main': [[{'node': first[k], 'type': 'main', 'index': 0}] for k in ('sales', 'insight', 'follow', 'other')]}
    return wf('telco00allinone01', 'TELCO 00 — Semua alur (satu tampilan)', nodes, conns)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--api-base', default='http://api:8000')
    ap.add_argument('--out', default='n8n/workflows')
    ap.add_argument('--sheets-cred-id', help='bind the Google Sheets OAuth2 credential created in the n8n UI (its id)')
    ap.add_argument('--enable', default='', help='comma list of normally-off lanes to enable in 00: target,recap,import')
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.sheets_cred_id:
        CRED['sheets'] = {'googleSheetsOAuth2Api': {'id': a.sheets_cred_id, 'name': 'Google Sheets recap'}}
    parts = build(a.api_base.rstrip('/'))
    parts['00_telco_all_in_one'] = build_combined(parts, frozenset(x for x in a.enable.split(',') if x))
    for name, w in parts.items():
        (out / f'{name}.json').write_text(json.dumps(w, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print('wrote', len(list(out.glob('*.json'))), 'workflows to', out)


if __name__ == '__main__':
    main()

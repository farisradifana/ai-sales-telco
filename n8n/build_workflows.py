"""Generate importable n8n 2.35.7 workflow JSON (node typeVersions verified with `n8n export:nodes`).

  python n8n/build_workflows.py [--api-base http://api:8000] [--out n8n/workflows]

Rules: no credentials inline (only credential *references* by stable id), no SQL nodes, no user text interpolated into
anything but JSON bodies sent to the API. Credential ids to create/bind (see n8n/README.md):
  telcoApiService     httpHeaderAuth  Authorization: Bearer <N8N_SERVICE_TOKEN>
  telcoDispatchHeader httpHeaderAuth  X-Telco-Dispatch-Token: <N8N_DISPATCH_SECRET>
  telcoRecapJwt       httpHeaderAuth  Authorization: Bearer <JWT of recap principal>   (workflow 07)
  telcoAdminJwt       httpHeaderAuth  Authorization: Bearer <JWT of admin principal>   (workflow 08)
  googleSheetsRecap   googleSheetsOAuth2Api                                            (workflow 07)
  telcoRouterOpenAi   openAiApi       Base URL http://<9router>/v1 + 9Router API key     (workflow 03, AI Agent model)
  telcoAgentMemory    postgres        login telco_agent_memory_login (schema agent_memory only) (workflow 03, memory)
"""
import argparse
import json
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent / 'code'


def js(name: str) -> str:
    return (CODE_DIR / name).read_text(encoding='utf-8')


RECAP_SHEET_ID = '1XAlDtkIA5gkdodi-fZip5x7Lbyri-YRk4dRJLOHPwRg'
CRED = {'svc': {'httpHeaderAuth': {'id': 'telcoApiService', 'name': 'TELCO API service token'}},
        'dispatch': {'httpHeaderAuth': {'id': 'telcoDispatchHeader', 'name': 'TELCO dispatch header'}},
        'recap': {'httpHeaderAuth': {'id': 'telcoRecapJwt', 'name': 'TELCO recap principal JWT'}},
        'admin': {'httpHeaderAuth': {'id': 'telcoAdminJwt', 'name': 'TELCO admin principal JWT'}},
        'sheets': {'googleSheetsOAuth2Api': {'id': 'googleSheetsRecap', 'name': 'Google Sheets recap'}},
        'router': {'openAiApi': {'id': 'telcoRouterOpenAi', 'name': '9Router (OpenAI-compatible)'}},
        'memory': {'postgres': {'id': 'telcoAgentMemory', 'name': 'TELCO agent memory (Postgres)'}}}
LC = '@n8n/n8n-nodes-langchain.'
START = "$('Agent start').item.json"
OPT_DATES = ', '.join(f"{k}: $fromAI('{k}', '{d}', 'string', '') || null" for k, d in (
    ('baseline_start', 'Opsional. Awal periode pembanding YYYY-MM-DD'), ('baseline_end', 'Opsional. Akhir periode pembanding (eksklusif)'),
    ('current_start', 'Opsional. Awal periode yang dianalisis YYYY-MM-DD'), ('current_end', 'Opsional. Akhir periode dianalisis (eksklusif)')))
CHANNEL = "channel: $fromAI('channel', 'sell_out (outlet ke pelanggan) atau sell_in (sales ke outlet)', 'string', 'sell_out')"
# name -> (description for the model, JSON body fields). The ticket travels in a header set by n8n, never via the model.
AGENT_TOOLS = {
    'resolve_entity': ('Cari outlet/kecamatan/kabupaten-kota/provinsi di wilayah akses pengguna; hasilkan entity_id + scope. '
                       'Panggil ini dulu bila pengguna menyebut nama wilayah/outlet.',
                       "query: $fromAI('query', 'Nama atau kode, mis. Kebonsari, Kabupaten Madiun, OUT-0003', 'string'), "
                       "scope: $fromAI('scope', 'Opsional: outlet, district, regency, atau province', 'string', '') || null"),
    'diagnose': ('Analisis perubahan omzet satu outlet/kecamatan/kab-kota/provinsi: total, kontribusi SKU, volume vs harga/mix, '
                 'kunjungan, saran. Butuh scope + entity_id dari resolve_entity.',
                 "scope: $fromAI('scope', 'outlet, district, regency, atau province', 'string'), "
                 "entity_id: $fromAI('entity_id', 'entity_id dari resolve_entity', 'number'), " + CHANNEL + ', ' + OPT_DATES),
    'rank_districts': ('Peringkat kecamatan di wilayah akses pengguna berdasarkan perubahan omzet.', CHANNEL + ', ' + OPT_DATES),
    'rank_regencies': ('Peringkat kabupaten/kota (yang seluruh kecamatannya dalam akses pengguna) berdasarkan perubahan omzet.',
                       CHANNEL + ', ' + OPT_DATES),
    'stock': ('Stok yang dipegang pengguna (account self) atau stok satu outlet (account outlet + outlet_id dari resolve_entity). '
              'PCS dan saldo IDR tidak dijumlahkan.',
              "account: $fromAI('account', 'self (stok saya) atau outlet', 'string', 'self'), "
              "outlet_id: $fromAI('outlet_id', 'Wajib bila account outlet: entity_id outlet dari resolve_entity', 'number', 0) || null"),
    'target': ('Realisasi vs target harian sales (milik pengguna) pada satu channel.',
               "channel: $fromAI('channel', 'sell_in atau sell_out', 'string', 'sell_in'), "
               "target_date: $fromAI('target_date', 'Opsional YYYY-MM-DD; kosong = hari ini', 'string', '') || null"),
}
REF = "={{ JSON.stringify({ provider: $('Event').first().json.provider, message_id: $('Event').first().json.message_id }) }}"
# 'none' leaves multi-trigger executions stuck in 'running' on n8n 2.35.7 (verified locally); keep 'all' + 24h pruning.
SETTINGS = {'executionOrder': 'v1', 'timezone': 'Asia/Jakarta', 'saveDataSuccessExecution': 'all',
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


def build(api: str, model_id: str = 'ai-sales') -> dict:
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
         # Deterministic (free, no model): sales input + YA/BATAL, STOP/MULAI TARGET, empty/non-text. Everything else is a
         # natural-language request for the AI Agent (counts against the daily chat quota).
         'parameters': {'mode': 'expression', 'numberOutputs': 3,
                        'output': "={{ ({sales_input:0, confirm:0, cancel:0, target_opt_in:2, target_opt_out:2, unsupported:2})[$json.intent] ?? 1 }}",
                        'options': {}}},
        {'id': 'sales', 'name': '02 Sales input', 'type': 'n8n-nodes-base.executeWorkflow', 'typeVersion': 1.2, 'position': [1340, -300],
         'parameters': {'source': 'database', 'workflowId': {'__rl': True, 'mode': 'id', 'value': 'telco02salesinput'},
                        'workflowInputs': {'mappingMode': 'defineBelow', 'value': {}}, 'mode': 'once', 'options': {'waitForSubWorkflow': True}}},
        {'id': 'agent', 'name': '03 AI Agent', 'type': 'n8n-nodes-base.executeWorkflow', 'typeVersion': 1.2, 'position': [1340, -100],
         'parameters': {'source': 'database', 'workflowId': {'__rl': True, 'mode': 'id', 'value': 'telco03aiagent0001'},
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
             'Route': {'main': [[{'node': '02 Sales input', 'type': 'main', 'index': 0}], [{'node': '03 AI Agent', 'type': 'main', 'index': 0}],
                                [{'node': 'Act (other)', 'type': 'main', 'index': 0}]]},
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
    out['03_ai_agent'] = agent_workflow(api, model_id, sub_body)

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
        {'id': 'clearinfo', 'name': 'Clear Info tab', 'type': 'n8n-nodes-base.googleSheets', 'typeVersion': 4.5, 'position': [660, 160],
         'credentials': CRED['sheets'], 'executeOnce': True,
         'parameters': {'authentication': 'oAuth2', 'resource': 'sheet', 'operation': 'clear',
                        'documentId': {'__rl': True, 'mode': 'id', 'value': RECAP_SHEET_ID},
                        'sheetName': {'__rl': True, 'mode': 'name', 'value': 'Info'}, 'clear': 'wholeSheet', 'keepFirstRow': False}},
        {'id': 'rows', 'name': 'Data rows', 'type': 'n8n-nodes-base.code', 'typeVersion': 2, 'position': [880, 0],
         'parameters': {'language': 'javaScript', 'mode': 'runOnceForAllItems', 'jsCode': js('recap_rows.js')}},
        {'id': 'sheet', 'name': 'Write Rekap tab', 'type': 'n8n-nodes-base.googleSheets', 'typeVersion': 4.5, 'position': [1100, 0],
         'credentials': CRED['sheets'],
         'parameters': {'authentication': 'oAuth2', 'resource': 'sheet', 'operation': 'append',
                        'documentId': {'__rl': True, 'mode': 'id', 'value': RECAP_SHEET_ID},
                        'sheetName': {'__rl': True, 'mode': 'name', 'value': 'Rekap'},
                        'columns': {'mappingMode': 'autoMapInputData', 'value': {}, 'matchingColumns': [], 'schema': []},
                        'options': {}}},
        {'id': 'info', 'name': 'Info rows', 'type': 'n8n-nodes-base.code', 'typeVersion': 2, 'position': [1320, 0], 'executeOnce': True,
         'parameters': {'language': 'javaScript', 'mode': 'runOnceForAllItems', 'jsCode': js('recap_info.js')}},
        {'id': 'sheetinfo', 'name': 'Write Info tab', 'type': 'n8n-nodes-base.googleSheets', 'typeVersion': 4.5, 'position': [1540, 0],
         'credentials': CRED['sheets'],
         'parameters': {'authentication': 'oAuth2', 'resource': 'sheet', 'operation': 'append',
                        'documentId': {'__rl': True, 'mode': 'id', 'value': RECAP_SHEET_ID},
                        'sheetName': {'__rl': True, 'mode': 'name', 'value': 'Info'},
                        'columns': {'mappingMode': 'autoMapInputData', 'value': {}, 'matchingColumns': [], 'schema': []},
                        'options': {}}},
    ]
    out['07_spreadsheet_recap'] = wf('telco07recap000001', 'TELCO 07 Spreadsheet recap (export snapshot)', recap_nodes,
                                     chain(['Schedule', 'Recap params', 'Export CSV', 'Clear Rekap tab', 'Clear Info tab', 'Data rows',
                                            'Write Rekap tab', 'Info rows', 'Write Info tab']))

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


def agent_workflow(api: str, model_id: str, ref_body: str) -> dict:
    """03: natural-language chat. API start (quota + one-turn ticket + system prompt) -> n8n AI Agent (9Router combo model,
    Postgres chat memory, ticket-bound HTTP tools) -> API finish (number verification, id scrub, chart link) -> reply."""
    finish_body = ("={{ JSON.stringify({ provider: $('Event').first().json.provider, message_id: $('Event').first().json.message_id, "
                   "text: $json.output || null, failed: !$json.output }) }}")
    nodes = [sub_trigger(),
             http('Agent start', [220, 0], '/internal/v1/agent/start', api, body=ref_body),
             {'id': 'useai', 'name': 'Kuota ada?', 'type': 'n8n-nodes-base.if', 'typeVersion': 2.2, 'position': [440, 0],
              'parameters': {'conditions': {'options': {'caseSensitive': True, 'typeValidation': 'strict', 'version': 2},
                                            'conditions': [{'id': 'c1', 'leftValue': '={{ $json.next }}', 'rightValue': 'agent',
                                                            'operator': {'type': 'string', 'operation': 'equals'}}], 'combinator': 'and'},
                             'options': {}}},
             {'id': 'aiagent', 'name': 'AI Agent', 'type': LC + 'agent', 'typeVersion': 3.1, 'position': [660, -100],
              'onError': 'continueRegularOutput',  # model down -> finish(failed) -> safe reply + quota refund
              'parameters': {'promptType': 'define', 'text': '={{ %s.question }}' % START,
                             'options': {'systemMessage': '={{ %s.system_prompt }}' % START, 'maxIterations': 8}}},
             http('Agent finish', [1000, 0], '/internal/v1/agent/finish', api, body=finish_body),
             http('Reply', [1220, 0], '/internal/v1/events/reply', api, body=ref_body),
             http('Complete', [1440, 0], '/internal/v1/events/complete', api, body=ref_body),
             {'id': 'model', 'name': 'Model combo (9Router)', 'type': LC + 'lmChatOpenAi', 'typeVersion': 1.3, 'position': [440, 220],
              'credentials': CRED['router'],
              'parameters': {'model': {'__rl': True, 'mode': 'id', 'value': model_id}, 'responsesApiEnabled': False,
                             'options': {'temperature': 0.2, 'maxRetries': 1, 'timeout': 90000}}},
             {'id': 'memory', 'name': 'Memory (Postgres)', 'type': LC + 'memoryPostgresChat', 'typeVersion': 1.3, 'position': [600, 220],
              'credentials': CRED['memory'],
              'parameters': {'sessionIdType': 'customKey', 'sessionKey': '={{ %s.session_key }}' % START,
                             'tableName': 'agent_memory.chat_history', 'contextWindowLength': 6}}]
    conns = {**chain(['Event', 'Agent start', 'Kuota ada?']),
             'Kuota ada?': {'main': [[{'node': 'AI Agent', 'type': 'main', 'index': 0}], [{'node': 'Reply', 'type': 'main', 'index': 0}]]},
             **chain(['AI Agent', 'Agent finish', 'Reply', 'Complete']),
             'Model combo (9Router)': {'ai_languageModel': [[{'node': 'AI Agent', 'type': 'ai_languageModel', 'index': 0}]]},
             'Memory (Postgres)': {'ai_memory': [[{'node': 'AI Agent', 'type': 'ai_memory', 'index': 0}]]}}
    for i, (name, (desc, body)) in enumerate(AGENT_TOOLS.items()):
        label = f'Tool {name}'
        nodes.append({'id': f'tool-{name}', 'name': label, 'type': 'n8n-nodes-base.httpRequestTool', 'typeVersion': 4.2,
                      'position': [760 + i * 140, 220], 'credentials': CRED['svc'],
                      'parameters': {'toolDescription': desc, 'descriptionType': 'manual', 'method': 'POST',
                                     'url': f'{api}/internal/v1/agent-tools/{name}', 'authentication': 'genericCredentialType',
                                     'genericAuthType': 'httpHeaderAuth', 'sendHeaders': True,
                                     'headerParameters': {'parameters': [{'name': 'X-Agent-Ticket', 'value': '={{ %s.ticket }}' % START}]},
                                     'sendBody': True, 'specifyBody': 'json', 'jsonBody': '={{ JSON.stringify({ %s }) }}' % body,
                                     # API errors (scope denied, ambiguous name) go back to the model as JSON, not as a crash.
                                     'options': {'response': {'response': {'neverError': True}}, 'timeout': 60000}}})
        conns[label] = {'ai_tool': [[{'node': 'AI Agent', 'type': 'ai_tool', 'index': 0}]]}
    return wf('telco03aiagent0001', 'TELCO 03 AI Agent (bahasa natural, kuota per jabatan)', nodes, conns)


LANES = [  # key, title, description, sticky color (n8n 1..7), active by default
    ('router', '01 · Pesan masuk WhatsApp (router)',
     'Event dari API ingress (signature sudah diverifikasi) → klaim → kenali niat → arahkan ke jalur di bawah. '
     'Hanya provider + message_id yang lewat; akun pengirim ditentukan API.', 5, True),
    ('sales', '02 · Input penjualan (tanpa AI, tanpa kuota)',
     'Draft penjualan → ringkasan konfirmasi → balasan YA/BATAL → posting atomik ke stok & ledger.', 4, True),
    ('agent', '03 · AI Agent – bahasa natural (kuota chat per jabatan)',
     'Agent start (cek kuota harian: Sales/TM 3, HoB 5, HoS 10, HoR 20; tiket satu giliran) → AI Agent (model combo 9Router, '
     'memory Postgres, tool analitik ber-tiket) → Agent finish (verifikasi angka vs hasil tool, hapus ID teknis, tautan grafik) → balas. '
     'Kuota habis → jawaban format standar tanpa AI.', 6, True),
    ('other', '01b · Perintah lain (STOP/MULAI TARGET)',
     'Opt-in / opt-out pengingat target harian dan pesan non-teks. Tanpa AI.', 7, True),
    ('target', '05 · Pengingat target harian (NONAKTIF)',
     'Jam kerja WIB, hanya yang opt-in, sekali per hari. Aktifkan setelah template WhatsApp Utility disetujui Meta.', 1, False),
    ('outbox', '06 · Pengirim pesan keluar (outbox)',
     'Tiap 20 detik: ambil antrean (SKIP LOCKED + lease), kirim, retry bertahap, dead-letter bila gagal permanen.', 3, True),
    ('recap', '07 · Rekap Google Sheet (NONAKTIF)',
     'Export snapshot ter-scope → tab Rekap (tabel data) + tab Info (watermark, channel, satuan, total). Aktifkan setelah credential Google di-bind.', 4, False),
    ('import', '08 · Import CSV → staging (NONAKTIF)',
     'Upload CSV admin → validasi & preview di API; posting hanya setelah konfirmasi admin di dashboard.', 2, False),
]
LANE_H, NODE_W = 260, 220
LANE_H_BY_KEY = {'agent': 520}  # room for the AI Agent's model / memory / tool sub-nodes under the main row
SUB_TYPES = ('ai_languageModel', 'ai_memory', 'ai_tool')


def build_combined(parts: dict, enable: frozenset = frozenset()) -> dict:
    """One canvas: every flow in its own horizontal lane with a coloured sticky note (title + function).
    Router outputs connect straight to the lanes (no sub-workflows). Do NOT activate together with 01-08 (same webhook)."""
    src = {
        'router': parts['01_inbound_router'], 'sales': parts['02_sales_input'], 'agent': parts['03_ai_agent'],
        'target': parts['05_daily_target'], 'outbox': parts['06_outbox_dispatcher'],
        'recap': parts['07_spreadsheet_recap'], 'import': parts['08_import_staging'],
    }
    prefix = {'sales': 'Sales · ', 'agent': 'AI · ', 'other': 'Lainnya · ', 'target': 'Target · ',
              'outbox': 'Outbox · ', 'recap': 'Rekap · ', 'import': 'Import · '}
    nodes, conns, first = [], {}, {}
    y = 0
    for key, title, desc, color, active in LANES:
        lane_h = LANE_H_BY_KEY.get(key, LANE_H)
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
        sub_names = {a for a, spec in lane_conns.items() if any(t in spec for t in SUB_TYPES)}
        row = [n for n in lane_nodes if n['name'] not in sub_names]  # generator lists nodes in flow order
        subs = [n for n in lane_nodes if n['name'] in sub_names]
        agent_x = next((i for i, n in enumerate(row) if n['type'].endswith('.agent')), 0)
        for x_i, n in enumerate(row + subs):
            old_name = n['name']
            n['name'] = rename.get(old_name, old_name)
            # Keep expression references ($('Node')) valid after lane prefixes are added.
            params = json.dumps(n.get('parameters', {}), ensure_ascii=False)
            for old, new in rename.items():
                params = params.replace(f"$('{old}')", f"$('{new}')")
            n['parameters'] = json.loads(params)
            n['id'] = f'{key}-{x_i}'
            if x_i < len(row):
                n['position'] = [80 + x_i * NODE_W, y + 110]
            else:  # sub-node: under the AI Agent, left to right (model, memory, tools)
                s_i = x_i - len(row)
                n['position'] = [80 + agent_x * NODE_W - 260 + s_i * 150, y + 330]
            if not active and n['type'] in ('n8n-nodes-base.scheduleTrigger', 'n8n-nodes-base.webhook'):
                n['disabled'] = True
            nodes.append(n)
        if row:
            first[key] = row[0]['name']
        for a, spec in lane_conns.items():
            conns[rename.get(a, a)] = {t: [[{**c, 'node': rename.get(c['node'], c['node'])} for c in branch] for branch in branches]
                                       for t, branches in spec.items()}
        width = max(80 + max(1, len(row)) * NODE_W + 40, 80 + agent_x * NODE_W - 260 + len(subs) * 150 + 120 if subs else 0)
        nodes.append({'id': f'note-{key}', 'name': f'Catatan {title}', 'type': 'n8n-nodes-base.stickyNote', 'typeVersion': 1,
                      'position': [40, y], 'parameters': {'content': f'## {title}\n{desc}', 'color': color,
                                                          'width': width, 'height': lane_h - 20}})
        y += lane_h
    # Router branches -> lane entry nodes.
    conns['Route'] = {'main': [[{'node': first[k], 'type': 'main', 'index': 0}] for k in ('sales', 'agent', 'other')]}
    return wf('telco00allinone01', 'TELCO 00 — Semua alur (satu tampilan)', nodes, conns)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--api-base', default='http://api:8000')
    ap.add_argument('--out', default='n8n/workflows')
    ap.add_argument('--sheets-cred-id', help='bind the Google Sheets OAuth2 credential created in the n8n UI (its id)')
    ap.add_argument('--enable', default='', help='comma list of normally-off lanes to enable in 00: target,recap,import')
    ap.add_argument('--model-id', default='ai-sales', help='9Router model or combo id used by the AI Agent (workflow 03)')
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.sheets_cred_id:
        CRED['sheets'] = {'googleSheetsOAuth2Api': {'id': a.sheets_cred_id, 'name': 'Google Sheets recap'}}
    parts = build(a.api_base.rstrip('/'), a.model_id)
    parts['00_telco_all_in_one'] = build_combined(parts, frozenset(x for x in a.enable.split(',') if x))
    for name, w in parts.items():
        (out / f'{name}.json').write_bytes((json.dumps(w, indent=2, ensure_ascii=False) + '\n').encode('utf-8'))  # LF on every OS
    print('wrote', len(list(out.glob('*.json'))), 'workflows to', out)


if __name__ == '__main__':
    main()

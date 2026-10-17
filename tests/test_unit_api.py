"""Pure unit tests (no database)."""
import hashlib
import hmac
from datetime import date
from decimal import Decimal

import pytest

from telco_api import agent_tools, errors, intents, narrative, outbox, periods
from telco_api.agent import CircuitBreaker
from telco_api.charts import waterfall_points
from telco_api.money import rupiah
from telco_api.schemas import DiagnoseRequest, DraftRequest


def test_default_period_from_watermark_equal_days():
    b0, b1, c0, c1, policy = periods.resolve(DiagnoseRequest(scope='outlet', entity_id=1), date(2026, 9, 30))
    assert (b0, b1, c0, c1) == (date(2026, 6, 1), date(2026, 7, 1), date(2026, 9, 1), date(2026, 10, 1))
    assert 'watermark' in policy


def test_incomplete_month_watermark_uses_previous_month():
    c0, c1 = periods.last_complete_month(date(2026, 9, 20))
    assert (c0, c1) == (date(2026, 8, 1), date(2026, 9, 1))
    b0, b1 = periods.equal_length_baseline(c0, c1)
    assert (b1 - b0).days == 31 and b1 <= c0


def test_unequal_and_overlapping_periods_rejected():
    with pytest.raises(errors.AppError) as e:
        periods.validate_pair(date(2026, 8, 1), date(2026, 9, 1), date(2026, 9, 1), date(2026, 10, 1))
    assert e.value.code == 'UNEQUAL_PERIODS'
    with pytest.raises(errors.AppError):
        periods.validate_pair(date(2026, 6, 1), date(2026, 9, 2), date(2026, 9, 1), date(2026, 12, 2))


def test_partial_period_fields_rejected():
    with pytest.raises(ValueError):
        DiagnoseRequest(scope='outlet', entity_id=1, baseline_start=date(2026, 6, 1))


def test_extra_fields_forbidden_and_no_principal_in_body():
    with pytest.raises(ValueError):
        DiagnoseRequest(scope='outlet', entity_id=1, principal_id='someone-else')
    with pytest.raises(ValueError):
        DraftRequest(channel='sell_in', outlet_id=1, lines=[{'sku': 'SIM-NOVA-8', 'quantity': 1}], sales_id=3)


def test_money_float_and_fraction_quantity_rejected():
    with pytest.raises(ValueError):
        DraftRequest(channel='sell_in', outlet_id=1, lines=[{'sku': 'SIM-NOVA-8', 'quantity': 1.5}])
    with pytest.raises(ValueError):
        DraftRequest(channel='sell_in', outlet_id=1, lines=[{'sku': 'SIM-NOVA-8', 'quantity': 1, 'unit_price': 22500.5}])


def test_rupiah_format():
    assert rupiah(Decimal('-5173100.00')) == '-Rp5.173.100'
    assert rupiah(Decimal('1234.50')) == 'Rp1.234,50'


EVID = {'evidence_id': '11111111-2222-3333-4444-555555555555', 'channel': 'sell_out', 'scope': 'outlet', 'entity_id': 1,
        'entity': {'type': 'outlet', 'id': 1, 'code': 'OUT-0001', 'name': 'Outlet Fiktif 0001'},
        'baseline': {'start': '2026-06-01', 'end_exclusive': '2026-07-01', 'days': 30},
        'current': {'start': '2026-09-01', 'end_exclusive': '2026-10-01', 'days': 30},
        'baseline_label': 'Juni 2026', 'current_label': 'September 2026', 'data_ready': True,
        'revenue_baseline_idr': '7332900.00', 'revenue_current_idr': '2159800.00', 'delta_idr': '-5173100.00', 'growth': '-0.705464',
        'drivers': [{'product_id': 4, 'product': 'Voucher Fiktif Nova 8GB 30 Hari', 'delta_idr': '-1870000.00', 'r0': '1870000.00',
                     'r1': '0.00', 'volume_effect_idr': '0', 'price_mix_effect_idr': '0', 'entry_exit_effect_idr': '-1870000.00'},
                    {'product_id': 3, 'product': 'Voucher Fiktif Nova 3GB 7 Hari', 'delta_idr': '-3303100.00', 'r0': '5462900.00',
                     'r1': '2159800.00', 'volume_effect_idr': '-3000000.00', 'price_mix_effect_idr': '-303100.00', 'entry_exit_effect_idr': '0'}],
        'coverage': {'visits_planned': 8, 'visits_done': 0},
        'recommendation': {'actions': [{'action': 'Jadwalkan kunjungan verifikasi.', 'owner': 'Sales Supervisor', 'review_in_days': 7,
                                        'success_metric': 'Visit completion >= 80%'}]}}


def test_template_narrative_passes_verifier_and_mentions_evidence():
    text = narrative.diagnose_text(EVID)
    assert narrative.verify(text, [EVID]) == []
    assert EVID['evidence_id'] not in text and 'Evidence ID' not in text and 'Juni 2026' in text and '-Rp5.173.100' in text and '(-70,55%)' in text


def test_verifier_rejects_invented_numbers():
    text = narrative.diagnose_text(EVID) + ' Uplift diperkirakan Rp987.654.321 dengan confidence 87%.'
    bad = narrative.verify(text, [EVID])
    assert any('987' in b for b in bad) and any('87' in b for b in bad)


def test_zero_base_growth_text():
    e = dict(EVID, growth=None)
    assert 'tidak dihitung' in narrative.diagnose_text(e)


def test_waterfall_reconciles_to_delta():
    pts = waterfall_points(EVID)
    deltas = sum(p['value'] for p in pts if p['kind'] == 'delta')
    assert abs(deltas - (pts[-1]['value'] - pts[0]['value'])) <= Decimal('0.01')


@pytest.mark.parametrize('text,name', [
    ('SELLIN OUT-0001 SIM-NOVA-8 5, BAL-NOVA-IDR 100000', 'sales_input'),
    ('YA 1A2B3C', 'confirm'), ('batal abcdef', 'cancel'),
    ('kenapa omzet OUT-0001 turun?', 'insight'), ('apa yang harus dilakukan?', 'follow_up'),
    ('solusinya?', 'follow_up'), ('stok saya', 'cmd_stock'), ('target sell-in hari ini', 'target'),
    ('Sell in', 'cmd_sell_in'), ('sell-out', 'cmd_sell_out'), ('peringkat outlet', 'cmd_rank_outlets'),
    ('Peringkat kecamatan?', 'cmd_rank_districts'), ('peringkat kabupaten', 'cmd_rank_regencies'), ('menu', 'cmd_menu'), ('list produk', 'cmd_products'), ('Daftar produk?', 'cmd_products'),
    ('OUT-0001 SIM-NOVA-8 5', 'sales_input'), ('outlet mana yang stock nya paling rendah?', 'stock'),
    ('outlet mana yang sell in nya paling bagus?', 'help'),
    ('STOP TARGET', 'target_opt_out'), ('kecamatan mana yang turun?', 'rank_districts'),
    ('kabupaten mana yang turun?', 'rank_regencies'), ('peringkat kab/kota sell-in', 'rank_regencies'),
    ('kenapa omzet kabupaten madiun turun?', 'insight'),
])
def test_intents(text, name):
    assert intents.parse(text).name == name


def test_sales_input_is_always_sell_in():
    assert intents.parse('SELLIN SIM-NOVA-8 5').clarification  # outlet missing
    it = intents.parse('out-0001 sim-nova-8 5, VCR-NOVA-3 x2')
    assert it.slots == {'channel': 'sell_in', 'outlet_code': 'OUT-0001',
                        'lines': [{'sku': 'SIM-NOVA-8', 'quantity': 5}, {'sku': 'VCR-NOVA-3', 'quantity': 2}]}
    assert intents.parse('OUT-0001 SIM-NOVA-8').slots['lines'] == [{'sku': 'SIM-NOVA-8', 'quantity': 1}]
    assert intents.parse('catat SIM-NOVA-8 5 OUT-0001').slots['channel'] == 'sell_in'
    for t in ('SELLOUT OUT-0001 SIM-NOVA-8 5', 'catat sellout OUT-0001 SIM-NOVA-8 2'):
        assert 'hanya untuk sell-in' in intents.parse(t).clarification


def test_area_insight_slots():
    assert intents.parse('kenapa omzet kabupaten madiun turun?').slots == {'query': 'Kabupaten Madiun', 'scope': 'regency', 'channel': 'sell_out'}
    assert intents.parse('analisa sell-in kota surabaya').slots == {'query': 'Kota Surabaya', 'scope': 'regency', 'channel': 'sell_in'}
    assert intents.parse('performa provinsi jawa timur').slots['scope'] == 'province'


def test_injection_text_is_just_text():
    it = intents.parse("kenapa omzet OUT-0001 turun'; DROP TABLE telco.sell_out; --")
    assert it.name == 'insight' and it.slots['query'] == 'OUT-0001'


def test_agent_tool_allowlist_has_no_write_or_sql():
    names = set(agent_tools.TOOLS)
    assert names == {'get_evidence', 'diagnose', 'rank_districts', 'rank_regencies', 'stock', 'target', 'resolve_entity', 'report'}
    for n in names:
        assert agent_tools.input_schema(n)['additionalProperties'] is False


def test_mcp_bridge_schema_matches_api_models():
    import asyncio
    from telco_mcp.server import mcp
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert set(tools) == set(agent_tools.TOOLS)
    for n in tools:
        assert set(tools[n].input_schema['properties']) == set(agent_tools.input_schema(n)['properties']), n
        assert tools[n].annotations.read_only_hint is True


def test_webhook_signature():
    from telco_api.settings import Settings
    from telco_api.webhook import verify_signature
    s = Settings(reader_database_url='postgresql://x', writer_database_url='postgresql://x', wa_app_secret='topsecret')
    raw = b'{"object":"whatsapp_business_account"}'
    sig = 'sha256=' + hmac.new(b'topsecret', raw, hashlib.sha256).hexdigest()
    verify_signature(s, raw, sig)
    with pytest.raises(errors.AppError):
        verify_signature(s, raw + b' ', sig)
    with pytest.raises(errors.AppError):
        verify_signature(s, raw, None)


def test_backoff_bounded_with_jitter():
    for a in range(1, 30):
        v = outbox.backoff_seconds(a)
        assert 0 < v <= 3600


def test_circuit_breaker_opens_and_half_opens():
    cb = CircuitBreaker(2, 0.05)
    cb.failure(); cb.failure()
    assert not cb.allow()
    import time
    time.sleep(0.3)  # generous margin: Windows timer granularity / loaded CI must not make this flaky
    assert cb.allow()


def test_live_mode_requires_allowlist():
    from telco_api.settings import Settings
    s = Settings(reader_database_url='postgresql://x', writer_database_url='postgresql://x', wa_send_mode='live')
    with pytest.raises(outbox.SendError) as e:
        outbox.WhatsAppSender(s)._guard('deadbeef')
    assert e.value.permanent
    s2 = Settings(reader_database_url='postgresql://x', writer_database_url='postgresql://x', wa_send_mode='mock',
                  wa_graph_base_url='https://graph.facebook.com')
    with pytest.raises(outbox.SendError):
        outbox.WhatsAppSender(s2)._guard('x')


def test_scrub_ids_removes_technical_ids():
    from telco_api.agent import scrub_ids
    out = scrub_ids('Turun -Rp5.173.100.\nEvidence ID 11111111-2222-3333-4444-555555555555.\nSaran: kunjungi outlet.')
    assert '1111' not in out and 'Evidence' not in out and '-Rp5.173.100' in out and 'Saran' in out


def test_chart_is_readable_and_traceable():
    from telco_api.charts import render_waterfall, short_rp
    svg = render_waterfall(EVID)
    assert 'Turun Rp5,2 jt' in svg and 'Juni 2026 (pembanding)' in svg and 'September 2026 (sekarang)' in svg
    assert '-Rp5.173.100' in svg and 'Rp7.332.900' in svg  # exact values kept in tooltips
    assert (short_rp('2159800'), short_rp('-990000'), short_rp('1250000000'), short_rp('500')) == ('Rp2,2 jt', '-Rp990 rb', 'Rp1,3 M', 'Rp500')


def test_agent_reply_uses_whatsapp_markup():
    from telco_api.chat_agent import whatsapp_format
    assert whatsapp_format('## Ringkasan\n**Omzet:** Rp 1.000\n* poin\n__catatan__') == 'Ringkasan\n*Omzet:* Rp 1.000\n- poin\n_catatan_'


def test_interactive_choice_becomes_command_text():
    from telco_api.webhook import WAMessage, normalize
    m = WAMessage.model_validate({'id': 'wamid.X1', 'from': '0099123', 'timestamp': '1792200000', 'type': 'interactive',
                                  'interactive': {'type': 'list_reply', 'list_reply': {'id': 'peringkat outlet', 'title': 'Peringkat outlet'}}})
    n = normalize(m)
    assert n['text'] == 'peringkat outlet' and n['choice_title'] == 'Peringkat outlet'
    assert intents.parse(n['text']).name == 'cmd_rank_outlets'
    for t in ('Halo', 'halo kak', 'Hallo!'):
        assert intents.parse(t).name == 'cmd_greeting', t
    assert [intents.parse(t).name for t in ('menu', 'Menu', 'bantuan')] == ['cmd_menu'] * 3
    assert intents.parse('selamat pagi').name not in ('cmd_greeting', 'cmd_menu')  # only "Halo" greets
    assert intents.parse('halo, stok saya berapa?').name != 'cmd_menu'  # a real question still goes to the AI

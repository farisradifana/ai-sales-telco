"""Indonesian narrative built ONLY from evidence (user-facing text carries no evidence id; ids stay in audit records), plus a verifier that rejects any model narrative containing a
number that is not present in the evidence of the same turn."""
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .money import dec, pct_id, rupiah

CHANNEL_ID = {'sell_out': 'sell-out', 'sell_in': 'sell-in'}


def _entity_label(e: dict) -> str:
    ent = e.get('entity') or {}
    return f"{ent.get('name', e.get('scope'))} ({ent.get('code', e.get('entity_id'))})"


def diagnose_text(e: dict) -> str:
    ch = CHANNEL_ID[e['channel']]
    cur, base = e['current'], e['baseline']
    end_ex = date.fromisoformat(cur['end_exclusive'])
    delta = dec(e['delta_idr'])
    growth = e.get('growth')
    g = f" ({pct_id(growth)})" if growth is not None else ' (persentase tidak dihitung karena baseline nol/negatif)'
    lines = []
    if not e.get('data_ready'):
        lines.append('PROVISIONAL: kelengkapan input/watermark belum memenuhi syarat ('
                     + ', '.join(e.get('issues', [])) + '); angka di bawah belum boleh dipakai untuk menyimpulkan tren.')
    lines.append(f"Dalam {e['current_label']} vs {e['baseline_label']} (masing-masing {cur['days']} hari, WIB, akhir eksklusif "
                 f"{end_ex.day} {_month(end_ex)} {end_ex.year}), {ch} {_entity_label(e)} berubah {rupiah(delta)}{g}: "
                 f"{rupiah(e['revenue_baseline_idr'])} → {rupiah(e['revenue_current_idr'])}.")
    drivers = sorted(e.get('drivers', []), key=lambda d: dec(d['delta_idr']))
    if drivers:
        d = drivers[0] if delta < 0 else drivers[-1]
        lines.append(f"Kontribusi terbesar SKU {d['product']} {rupiah(d['delta_idr'])}; volume {rupiah(d['volume_effect_idr'])}, "
                     f"harga/mix {rupiah(d['price_mix_effect_idr'])}, masuk/keluar {rupiah(d['entry_exit_effect_idr'])} "
                     f"(dekomposisi akuntansi, bukan sebab).")
    areas = sorted(e.get('district_contributions') or [], key=lambda d: dec(d['delta_idr']))
    if areas:
        a = areas[0] if delta < 0 else areas[-1]
        lines.append(f"Kecamatan dengan kontribusi terbesar: {a['name']} {rupiah(a['delta_idr'])} "
                     f"({a['outlets']} outlet); drill-down ke kecamatan/outlet perlu izin per entitas.")
    c = e.get('coverage') or {}
    if c.get('visits_planned'):
        lines.append(f"Kunjungan tercatat {c.get('visits_done')} dari {c.get('visits_planned')} rencana; ini asosiasi, bukan bukti sebab.")
    acts = (e.get('recommendation') or {}).get('actions') or []
    if acts:
        a = acts[0]
        lines.append(f"Saran: {a['action']} (pemilik: {a['owner']}, tinjau {a['review_in_days']} hari; ukuran: {a['success_metric']}).")
    lines.append('Batas bukti: stockout tidak diukur, data kompetitor/jaringan tidak tersedia; tindakan perlu konfirmasi manusia.')
    return '\n'.join(lines)


def recommendation_text(e: dict) -> str:
    rec = e.get('recommendation') or {}
    out = [f"Konteks tetap: {CHANNEL_ID[e['channel']]} {_entity_label(e)}, {e['current_label']} vs {e['baseline_label']}."]
    for i, a in enumerate(rec.get('actions', []), 1):
        fact = a.get('fact') or ''
        hyp = a.get('hypothesis')
        out.append(f"{i}. {a['action']} Fakta: {fact}" + (f" Hipotesis: {hyp}" if hyp else '')
                   + f" Pemilik: {a['owner']}. Tinjau: {a['review_in_days']} hari. Sukses: {a['success_metric']}."
                   + (f" Guardrail: {a['guardrail']}" if a.get('guardrail') else ''))
    out.append('Semua tindakan adalah rencana; eksekusi stok/harga/kampanye butuh konfirmasi manusia.')
    return '\n'.join(out)


def ranking_text(e: dict) -> str:
    out = [f"Peringkat {CHANNEL_ID[e['channel']]} kecamatan yang Anda akses, {e['current_label']} vs {e['baseline_label']}:"]
    for i, d in enumerate(e['districts'][:5], 1):
        g = f" ({pct_id(d['growth'])})" if d.get('growth') is not None else ''
        out.append(f"{i}. {d['district_name']}: {rupiah(d['delta_idr'])}{g}")
    out.append(e['next_step'])
    return '\n'.join(out)


def regency_ranking_text(e: dict) -> str:
    out = [f"Peringkat {CHANNEL_ID[e['channel']]} kabupaten/kota yang Anda akses, {e['current_label']} vs {e['baseline_label']}:"]
    for i, r in enumerate(e['regencies'][:5], 1):
        g = f" ({pct_id(r['growth'])})" if r.get('growth') is not None else ''
        out.append(f"{i}. {r['regency_name']}: {rupiah(r['delta_idr'])}{g}")
    out.append(e['next_step'])
    return '\n'.join(out)


def stock_text(e: dict) -> str:
    out = ['Stok yang Anda pegang:' if e['holder']['type'] == 'sales' else f"Stok outlet {e['holder']['id']}:"]
    for i in e['items']:
        q = dec(i['quantity'])
        unit = 'pcs' if i['stock_unit'] == 'PCS' else 'IDR saldo'
        qs = f"{int(q):,}".replace(',', '.') if q == q.to_integral() else str(q)
        out.append(f"- {i['sku']}: {qs} {unit}")
    return '\n'.join(out)


def target_text(e: dict, motivational: bool = False) -> str:
    ch = CHANNEL_ID[e['channel']]
    if not e.get('target_available'):
        return f"Target {ch} untuk {e['target_date']} belum tersedia."
    ratio = e.get('achievement_ratio')
    head = f"Target {ch} {e['target_date']}: realisasi {rupiah(e['actual_idr'])} dari {rupiah(e['target_idr'])}"
    head += f" ({pct_id(ratio, 1)})" if ratio is not None else ''
    gap = dec(e['gap_idr'])
    tail = f"; sisa {rupiah(gap)}." if gap > 0 else '; target hari ini sudah tercapai.'
    msg = head + tail
    if motivational:
        msg += (' Semangat, fokus ke outlet aktif dengan kebutuhan nyata; jangan menimbun stok hanya untuk target.' if gap > 0
                else ' Terima kasih, pertahankan kualitas input.')
    if e.get('intraday'):
        msg += ' (angka berjalan hari ini, belum final)'
    return msg


def _month(d: date) -> str:
    from .periods import ID_MONTHS
    return ID_MONTHS[d.month - 1]


# ---------------------------------------------------------------- verification
NUM_RE = re.compile(r'(-?)\s?Rp\s?(-?)([\d.]+(?:,\d+)?)|(-?\d[\d.,]*)(%?)')
UUID_RE = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
ISO_DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')


def _to_dec(tok: str, currency: bool) -> Decimal | None:
    t = tok.strip().rstrip('.,')
    if not t:
        return None
    try:
        if currency or re.fullmatch(r'\d{1,3}(\.\d{3})+(,\d+)?', t):
            return Decimal(t.replace('.', '').replace(',', '.'))
        if ',' in t:
            return Decimal(t.replace('.', '').replace(',', '.'))
        return Decimal(t)
    except InvalidOperation:
        return None


def _leaves(obj, acc: set):
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, Decimal)):
        acc.add(Decimal(obj))
    elif isinstance(obj, str):
        if re.fullmatch(r'-?\d+(\.\d+)?', obj):
            acc.add(Decimal(obj))
        for m in ISO_DATE_RE.findall(obj):
            y, mo, d = m.split('-')
            acc.update({Decimal(y), Decimal(int(mo)), Decimal(int(d))})
        # Numbers inside evidence-generated text (e.g. success_metric "Visit completion >= 80%").
        for tok in re.findall(r'\d+(?:\.\d+)?', ISO_DATE_RE.sub(' ', obj)):
            acc.add(Decimal(tok))
    elif isinstance(obj, dict):
        for v in obj.values():
            _leaves(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _leaves(v, acc)


def allowed_numbers(evidences: list[dict]) -> set[Decimal]:
    raw: set[Decimal] = set()
    for e in evidences:
        _leaves(e, raw)
    allowed = set()
    for v in raw:
        a = abs(v)
        for places in (0, 1, 2):
            q = Decimal(1).scaleb(-places)
            allowed.add(a.quantize(q, rounding=ROUND_HALF_UP))
            if a <= 10:  # ratios -> percentages
                allowed.add((a * 100).quantize(q, rounding=ROUND_HALF_UP))
        allowed.add(a)
    allowed.update(Decimal(i) for i in range(0, 32))
    return allowed


def scrub_identifiers(text: str, evidences: list[dict]) -> str:
    text = UUID_RE.sub(' ', text)
    names = set()
    for e in evidences:
        _collect_names(e, names)
    for n in sorted(names, key=len, reverse=True):
        if len(n) >= 3:
            text = text.replace(n, ' ')
    return ISO_DATE_RE.sub(lambda m: m.group(0).replace('-', ' '), text)


def _collect_names(obj, acc: set):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ('product', 'name', 'district_name', 'code', 'sku', 'district_code', 'regency_name', 'regency_code',
                     'province_name') and isinstance(v, str):
                acc.add(v)
            else:
                _collect_names(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _collect_names(v, acc)


def verify(text: str, evidences: list[dict]) -> list[str]:
    """Return offending number tokens (empty list = every number traces to evidence)."""
    allowed = allowed_numbers(evidences)
    clean = scrub_identifiers(text, evidences)
    bad = []
    for m in NUM_RE.finditer(clean):
        if m.group(3) is not None:
            v = _to_dec(m.group(3), currency=True)
        else:
            v = _to_dec(m.group(4), currency=False)
        if v is None:
            continue
        v = abs(v)
        if v not in allowed and v.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) not in allowed:
            bad.append(m.group(0).strip())
    return bad


def days_label(n: int) -> str:
    return f'{n} hari'


def end_inclusive(end_exclusive: str) -> str:
    return (date.fromisoformat(end_exclusive) - timedelta(days=1)).isoformat()

"""Deterministic Indonesian intent/slot parser. No LLM decides writes; text never becomes SQL."""
import re
from dataclasses import dataclass, field

SKU_RE = re.compile(r'\b((?:SIM|VCR|BAL)-[A-Z0-9-]+)\s*[x×:=]?\s*(\d[\d.]*)\b', re.I)
OUTLET_RE = re.compile(r'\b(OUT-\d{1,6})\b', re.I)
DISTRICT_RE = re.compile(r'\b(FX-D\d{2})\b|\bkecamatan\s+(simulasi\s+\d{2}|\d{1,2})\b', re.I)
# Jawa Timur kab/kota names are single words ("Kabupaten Madiun", "Kota Batu"); the word after it is the name.
REGENCY_RE = re.compile(r'\b(kabupaten|kab\.?|kota)\s+([a-z]{3,20})\b', re.I)
PROVINCE_RE = re.compile(r'\bprovinsi\b|\bjawa timur\b|\bjatim\b', re.I)
CONFIRM_RE = re.compile(r'^\s*(YA|BATAL)\s+([A-F0-9]{6})\s*$', re.I)
CHANNEL_IN = re.compile(r'sell[- ]?in|jual ke outlet|distribusi', re.I)
CHANNEL_OUT = re.compile(r'sell[- ]?out|jual ke (pelanggan|konsumen)', re.I)
INSIGHT = re.compile(r'kenapa|mengapa|turun|naik|omzet|insight|analisa|analisis|performa|penurunan|kenaikan', re.I)
FOLLOWUP = re.compile(r'apa yang harus|solusi|saran|rekomendasi|langkah|tindakan|kecamatannya|kalau kecamatan|kabupatennya|kotanya|kalau kabupaten|bulan lalu|lalu bagaimana', re.I)
STOCK = re.compile(r'\bstok\b|\bstock\b', re.I)
TARGET = re.compile(r'\btarget\b', re.I)
RANK = re.compile(r'kecamatan mana|peringkat|ranking', re.I)
RANK_REGENCY = re.compile(r'(kabupaten|kab/kota|kota) mana|(peringkat|ranking) (kabupaten|kab|kota)', re.I)
OPT_OUT = re.compile(r'^\s*STOP\s+TARGET\s*$', re.I)
OPT_IN = re.compile(r'^\s*MULAI\s+TARGET(\s+(SELL[- ]?IN|SELL[- ]?OUT))?\s*$', re.I)
_END = r'\s*[.!?]*\s*$'
COMMANDS = [
    ('cmd_menu', re.compile(r'^\s*(menu|bantuan|help|perintah)' + _END, re.I)),
    ('cmd_products', re.compile(r'^\s*(list|daftar|katalog)\s+(produk|product|barang)' + _END, re.I)),
    ('cmd_stock', re.compile(r'^\s*(cek\s+)?(stok|stock)(\s+(saya|ku|aku))?' + _END, re.I)),
    ('cmd_sell_in', re.compile(r'^\s*(cek\s+|laporan\s+)?sell[- ]?in' + _END, re.I)),
    ('cmd_sell_out', re.compile(r'^\s*(cek\s+|laporan\s+)?sell[- ]?out' + _END, re.I)),
    ('cmd_rank_outlets', re.compile(r'^\s*(peringkat|ranking|rank)\s+outlet' + _END, re.I)),
    ('cmd_rank_districts', re.compile(r'^\s*(peringkat|ranking|rank)\s+kecamatan' + _END, re.I)),
    ('cmd_rank_regencies', re.compile(r'^\s*(peringkat|ranking|rank)\s+(kabupaten|kab|kota|kab/kota|kabupaten/kota)' + _END, re.I)),
]
_SKU = r'(?:SIM|VCR|BAL)-[A-Z0-9-]+'
QUICK_LINE = re.compile(rf'({_SKU})\s*[x×:=]?\s*(\d[\d.]*)?', re.I)
QUICK_SALE = re.compile(rf'^\s*(?:(catat|input|jual|sell[- ]?in|sell[- ]?out)\s+)?((?:OUT)-\d{{1,6}})\s+((?:{_SKU}(?:\s*[x×:=]?\s*\d[\d.]*)?[\s,;]*)+)$', re.I)
SALE_VERB = re.compile(r'^\s*(catat|input|jual|sellin|sell-in|sell in|sellout|sell-out|sell out)\b', re.I)


@dataclass
class Intent:
    name: str
    slots: dict = field(default_factory=dict)
    clarification: str | None = None


def _qty(tok: str) -> int | None:
    t = tok.replace('.', '')
    return int(t) if t.isdigit() and int(t) > 0 else None


def parse(text: str | None) -> Intent:
    if not text or not text.strip():
        return Intent('unsupported', clarification='Pesan kosong/non-teks belum didukung. Kirim teks.')
    t = text.strip()
    m = CONFIRM_RE.match(t)
    if m:
        return Intent('confirm' if m.group(1).upper() == 'YA' else 'cancel', {'code': m.group(2).upper()})
    if OPT_OUT.match(t):
        return Intent('target_opt_out')
    m = OPT_IN.match(t)
    if m:
        ch = 'sell_in' if m.group(2) and 'IN' in m.group(2).upper() else 'sell_out'
        return Intent('target_opt_in', {'channel': ch})
    for name, rx in COMMANDS:  # exact quick commands: fixed reports, no model, no quota
        if rx.match(t):
            return Intent(name)
    m = QUICK_SALE.match(t)
    if m:  # "OUT-0001 SIM-NOVA-8 5[, VCR-NOVA-3 2]": WhatsApp input is always sell-in (quantity defaults to 1)
        if m.group(1) and re.match(r'sell[- ]?out', m.group(1), re.I):
            return Intent('sales_input', {}, 'Input lewat WhatsApp hanya untuk sell-in (sales → outlet). Contoh: OUT-0001 SIM-NOVA-8 5')
        pairs = [(s.upper(), _qty(q) if q else 1) for s, q in QUICK_LINE.findall(m.group(3))]
        if any(q is None for _, q in pairs):
            return Intent('sales_input', {'channel': 'sell_in'}, 'Jumlah harus bilangan bulat positif (PCS atau rupiah saldo).')
        return Intent('sales_input', {'channel': 'sell_in', 'outlet_code': m.group(2).upper(),
                                      'lines': [{'sku': s, 'quantity': q} for s, q in pairs]})
    channel = 'sell_in' if CHANNEL_IN.search(t) else 'sell_out' if CHANNEL_OUT.search(t) else None
    lines = [(s.upper(), _qty(q)) for s, q in SKU_RE.findall(t)]
    outlet = OUTLET_RE.search(t)
    if SALE_VERB.match(t) and lines:
        if channel == 'sell_out':
            return Intent('sales_input', {}, 'Input lewat WhatsApp hanya untuk sell-in (sales → outlet). Contoh: OUT-0001 SIM-NOVA-8 5')
        channel = 'sell_in'
        if not outlet:
            return Intent('sales_input', {'channel': channel, 'lines': lines}, 'Sebutkan kode outlet, contoh OUT-0001.')
        if any(q is None for _, q in lines):
            return Intent('sales_input', {'channel': channel}, 'Jumlah harus bilangan bulat positif (PCS atau rupiah saldo).')
        return Intent('sales_input', {'channel': channel, 'outlet_code': outlet.group(1).upper(),
                                      'lines': [{'sku': s, 'quantity': q} for s, q in lines]})
    if FOLLOWUP.search(t) and not OUTLET_RE.search(t):
        return Intent('follow_up', {'question': t})
    if STOCK.search(t):
        return Intent('stock', {'outlet_code': outlet.group(1).upper() if outlet else None})
    if TARGET.search(t):
        return Intent('target', {'channel': channel or 'sell_in'})
    if RANK_REGENCY.search(t):
        return Intent('rank_regencies', {'channel': channel or 'sell_out'})
    if RANK.search(t):
        return Intent('rank_districts', {'channel': channel or 'sell_out'})
    if INSIGHT.search(t):
        d = DISTRICT_RE.search(t)
        if outlet:
            return Intent('insight', {'query': outlet.group(1).upper(), 'scope': 'outlet', 'channel': channel or 'sell_out'})
        if d:
            q = d.group(1).upper() if d.group(1) else 'Kecamatan ' + d.group(2).title()
            return Intent('insight', {'query': q, 'scope': 'district', 'channel': channel or 'sell_out'})
        r = REGENCY_RE.search(t)
        if r and r.group(2).lower() not in ('mana', 'yang', 'ini', 'itu'):
            kind = 'Kota' if r.group(1).lower() == 'kota' else 'Kabupaten'
            return Intent('insight', {'query': f'{kind} {r.group(2).title()}', 'scope': 'regency', 'channel': channel or 'sell_out'})
        if PROVINCE_RE.search(t):
            return Intent('insight', {'query': 'Jawa Timur', 'scope': 'province', 'channel': channel or 'sell_out'})
        return Intent('insight', {'channel': channel or 'sell_out'},
                      'Outlet, kecamatan atau kabupaten/kota mana? Sebutkan kode, misal OUT-0001 atau "Kabupaten Madiun".')
    return Intent('help', clarification=('Ketik *menu* untuk perintah cepat: "stok saya", "sell in", "sell out", "peringkat outlet", '
                                         'atau input sell-in "OUT-0001 SIM-NOVA-8 5".'))

"""Deterministic Indonesian intent/slot parser. No LLM decides writes; text never becomes SQL."""
import re
from dataclasses import dataclass, field

SKU_RE = re.compile(r'\b((?:SIM|VCR|BAL)-[A-Z0-9-]+)\s*[x×:=]?\s*(\d[\d.]*)\b', re.I)
OUTLET_RE = re.compile(r'\b(OUT-\d{1,6})\b', re.I)
DISTRICT_RE = re.compile(r'\b(FX-D\d{2})\b|\bkecamatan\s+(simulasi\s+\d{2}|\d{1,2})\b', re.I)
CONFIRM_RE = re.compile(r'^\s*(YA|BATAL)\s+([A-F0-9]{6})\s*$', re.I)
CHANNEL_IN = re.compile(r'sell[- ]?in|jual ke outlet|distribusi', re.I)
CHANNEL_OUT = re.compile(r'sell[- ]?out|jual ke (pelanggan|konsumen)', re.I)
INSIGHT = re.compile(r'kenapa|mengapa|turun|naik|omzet|insight|analisa|analisis|performa|penurunan|kenaikan', re.I)
FOLLOWUP = re.compile(r'apa yang harus|solusi|saran|rekomendasi|langkah|tindakan|kecamatannya|kalau kecamatan|bulan lalu|lalu bagaimana', re.I)
STOCK = re.compile(r'\bstok\b|\bstock\b', re.I)
TARGET = re.compile(r'\btarget\b', re.I)
RANK = re.compile(r'kecamatan mana|peringkat|ranking', re.I)
OPT_OUT = re.compile(r'^\s*STOP\s+TARGET\s*$', re.I)
OPT_IN = re.compile(r'^\s*MULAI\s+TARGET(\s+(SELL[- ]?IN|SELL[- ]?OUT))?\s*$', re.I)
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
    channel = 'sell_in' if CHANNEL_IN.search(t) else 'sell_out' if CHANNEL_OUT.search(t) else None
    lines = [(s.upper(), _qty(q)) for s, q in SKU_RE.findall(t)]
    outlet = OUTLET_RE.search(t)
    if SALE_VERB.match(t) and lines:
        if channel is None:
            return Intent('sales_input', {'lines': lines}, 'Sebutkan channel: SELLIN (sales→outlet) atau SELLOUT (outlet→pelanggan).')
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
    if RANK.search(t):
        return Intent('rank_districts', {'channel': channel or 'sell_out'})
    if INSIGHT.search(t):
        d = DISTRICT_RE.search(t)
        if outlet:
            return Intent('insight', {'query': outlet.group(1).upper(), 'scope': 'outlet', 'channel': channel or 'sell_out'})
        if d:
            q = d.group(1).upper() if d.group(1) else 'Kecamatan ' + d.group(2).title()
            return Intent('insight', {'query': q, 'scope': 'district', 'channel': channel or 'sell_out'})
        return Intent('insight', {'channel': channel or 'sell_out'}, 'Outlet atau kecamatan mana? Sebutkan kode, misal OUT-0001.')
    return Intent('help', clarification=('Format: "SELLIN OUT-0001 SIM-NOVA-8 5" / "SELLOUT OUT-0001 BAL-NOVA-IDR 100000", '
                                         '"stok", "target sell-in", "kenapa omzet OUT-0001 turun?", lalu "apa yang harus dilakukan?".'))

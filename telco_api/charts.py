"""Server-side SVG chart rendered strictly from one evidence payload (labels, data points and totals identical to the
JSON). Shared via short-lived HMAC-signed URLs bound to evidence_id + principal."""
import hmac
import time
from decimal import ROUND_HALF_UP, Decimal
from html import escape

from . import errors
from .money import dec, rupiah
from .security import sign


def signed_url(s, evidence_id: str, principal_id: str) -> str:
    if not s.chart_signing_key:
        raise errors.unavailable('CHART_NOT_CONFIGURED', 'Kunci tanda tangan chart belum dikonfigurasi.')
    exp = str(int(time.time()) + s.chart_url_ttl_s)
    sig = sign(s.chart_signing_key.get_secret_value().encode(), evidence_id, principal_id, exp)
    # Principal is part of the MAC but not of the URL (owner is looked up server-side when the link is opened).
    return f"{s.public_base_url.rstrip('/')}/charts/{evidence_id}.svg?exp={exp}&sig={sig}"


def check_signature(s, evidence_id: str, principal_id: str, exp: str, sig: str) -> None:
    if not s.chart_signing_key or not exp.isdigit() or int(exp) < time.time():
        raise errors.AppError(403, 'LINK_EXPIRED', 'Tautan chart kedaluwarsa.')
    good = sign(s.chart_signing_key.get_secret_value().encode(), evidence_id, principal_id, exp)
    if not hmac.compare_digest(good, sig):
        raise errors.AppError(403, 'LINK_INVALID', 'Tautan chart tidak valid.')


def waterfall_points(e: dict) -> list[dict]:
    """Baseline -> per-SKU delta -> current. Sum of deltas reconciles to current-baseline (checked in tests)."""
    pts = [{'label': 'Baseline', 'value': dec(e['revenue_baseline_idr']), 'kind': 'total'}]
    for d in sorted(e.get('drivers', []), key=lambda d: dec(d['delta_idr'])):
        pts.append({'label': d['product'], 'value': dec(d['delta_idr']), 'kind': 'delta', 'sku_id': d['product_id']})
    pts.append({'label': 'Current', 'value': dec(e['revenue_current_idr']), 'kind': 'total'})
    return pts


def short_rp(value) -> str:
    """Compact rupiah for chart labels: Rp2,2 jt / Rp990 rb / Rp1,25 M (exact value stays in the SVG <title>)."""
    v = dec(value)
    sign, a = ('-' if v < 0 else ''), abs(v)
    for unit, div in (('M', Decimal(10) ** 9), ('jt', Decimal(10) ** 6), ('rb', Decimal(10) ** 3)):
        if a >= div:
            q = (a / div).quantize(Decimal('0.1') if a / div < 100 else Decimal('1'), rounding=ROUND_HALF_UP)
            return f"{sign}Rp{str(q).replace('.', ',').removesuffix(',0')} {unit}"
    return f'{sign}{rupiah(a)}'


def _pct(r0, r1) -> str:
    r0, r1 = dec(r0), dec(r1)
    if r0 == 0:
        return 'baru' if r1 > 0 else '0%'
    v = ((r1 - r0) / r0 * 100).quantize(Decimal('1'))
    return f"{'+' if v > 0 else ''}{v}%".replace('-', '−')


def render_waterfall(e: dict) -> str:
    """One panel per channel: a 'chart_pair' evidence stacks sell-out then sell-in for the same entity and periods."""
    panels = e.get('panels')
    if not panels:
        return _panel(e)[0]
    parts, y = [], 0
    for i, pe in enumerate(panels):
        svg, h = _panel(pe)
        if i:
            parts.append(f'<rect x="0" y="{y}" width="900" height="10" fill="#f3f4f6"/>')
            y += 10
        parts.append(svg.replace('<svg xmlns="http://www.w3.org/2000/svg" ', f'<svg x="0" y="{y}" width="900" height="{h}" ', 1))
        y += h
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 {y}" role="img" aria-label="Sell-out dan sell-in">{"".join(parts)}</svg>'


def paired_chart_url(db, s, p, e: dict, correlation_id: str) -> str:
    """Signed link to a chart with BOTH channels for the diagnosed entity and periods (the other channel is computed with
    the same scope and dates, authorized again). Falls back to the single-channel chart if the other side is unavailable."""
    from . import analytics, evidence
    from .money import stringify_money
    from .schemas import DiagnoseRequest
    other = 'sell_in' if e['channel'] == 'sell_out' else 'sell_out'
    try:
        o = analytics.diagnose(db, p, DiagnoseRequest(scope=e['scope'], entity_id=e['entity_id'], channel=other,
                                                      baseline_start=e['baseline']['start'], baseline_end=e['baseline']['end_exclusive'],
                                                      current_start=e['current']['start'], current_end=e['current']['end_exclusive']),
                               correlation_id)
    except errors.AppError:
        return signed_url(s, e['evidence_id'], p.principal_id)
    so, si = (e, o) if e['channel'] == 'sell_out' else (o, e)
    pair = {'tool': 'chart_pair', 'query_version': 'chart-pair-v1', 'scope': e['scope'], 'entity_id': e['entity_id'],
            'source_evidence_ids': [so['evidence_id'], si['evidence_id']], 'panels': [so, si]}
    stored = stringify_money(db.write(lambda conn: evidence.store(conn, p, 'chart_pair', {'evidence_ids': pair['source_evidence_ids']},
                                                                  pair, correlation_id)))
    return signed_url(s, stored['evidence_id'], p.principal_id)


def _panel(e: dict) -> tuple[str, int]:
    """Field-friendly comparison chart (name kept for the endpoints): per product, grey bar = comparison period,
    blue bar = current period, coloured change on the left; big up/down summary on top. Every number comes from the
    evidence; exact rupiah values are kept in <title> tooltips so the SVG stays traceable to the JSON."""
    drivers = sorted(e.get('drivers', []), key=lambda d: dec(d['delta_idr']))
    b_lbl, c_lbl = e['baseline_label'], e['current_label']
    base, cur, delta = dec(e['revenue_baseline_idr']), dec(e['revenue_current_idr']), dec(e['delta_idr'])
    ch = 'Sell-out' if e['channel'] == 'sell_out' else 'Sell-in'
    name = e.get('entity', {}).get('name', '')
    w, left, bar_x, bar_w, row_h, top = 900, 24, 330, 420, 66, 150
    h = top + row_h * max(1, len(drivers)) + 70
    hi = max([dec(d['r0']) for d in drivers] + [dec(d['r1']) for d in drivers] + [Decimal(1)])
    sx = lambda v: max(2.0, float(dec(v) / hi) * bar_w)  # noqa: E731
    up = delta >= 0
    tone = '#15803d' if up else '#b91c1c'
    head = f"{'Naik' if up else 'Turun'} {short_rp(abs(delta))} ({_pct(base, cur)})"
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img" aria-label="{escape(ch)} {escape(name)}">',
           '<style>text{font-family:system-ui,Segoe UI,sans-serif;fill:#111827}.s{font-size:15px}.m{font-size:13px;fill:#4b5563}'
           '.t{font-size:22px;font-weight:700}.b{font-size:26px;font-weight:700}.p{font-size:16px;font-weight:600}</style>',
           f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
           f'<text class="t" x="{left}" y="38">{escape(ch)} {escape(name)}</text>',
           f'<text class="s" x="{left}" y="64">{escape(c_lbl)} dibanding {escape(b_lbl)}</text>',
           f'<text class="b" x="{left}" y="104" style="fill:{tone}">{"▲" if up else "▼"} {escape(head)}'
           f'<title>{escape(rupiah(delta))}</title></text>',
           f'<text class="s" x="{left}" y="130">Total: {escape(short_rp(base))} → {escape(short_rp(cur))}'
           f'<title>{escape(rupiah(base))} → {escape(rupiah(cur))}</title></text>']
    for i, d in enumerate(drivers):
        y = top + i * row_h
        dd = dec(d['delta_idr'])
        col = '#15803d' if dd > 0 else '#b91c1c' if dd < 0 else '#4b5563'
        arrow = '▲' if dd > 0 else '▼' if dd < 0 else '•'
        out.append(f'<line x1="{left}" x2="{w - left}" y1="{y - 8}" y2="{y - 8}" stroke="#e5e7eb"/>')
        out.append(f'<text class="p" x="{left}" y="{y + 16}">{escape(d["product"].replace("Fiktif ", "")[:34])}</text>')
        out.append(f'<text class="s" x="{left}" y="{y + 40}" style="fill:{col};font-weight:600">{arrow} {escape(short_rp(dd))} '
                   f'({_pct(d["r0"], d["r1"])})<title>{escape(rupiah(dd))}</title></text>')
        for j, (key, fill, lbl) in enumerate((('r0', '#9ca3af', b_lbl), ('r1', '#2563eb', c_lbl))):
            by = y + 2 + j * 24
            out.append(f'<rect x="{bar_x}" y="{by}" width="{sx(d[key]):.1f}" height="20" rx="3" fill="{fill}">'
                       f'<title>{escape(lbl)}: {escape(rupiah(d[key]))}</title></rect>')
            out.append(f'<text class="m" x="{bar_x + sx(d[key]) + 8:.1f}" y="{by + 15}">{escape(short_rp(d[key]))}</text>')
    ly = h - 40
    out.append(f'<rect x="{left}" y="{ly - 12}" width="14" height="14" rx="2" fill="#9ca3af"/>'
               f'<text class="m" x="{left + 20}" y="{ly}">{escape(b_lbl)} (pembanding)</text>')
    out.append(f'<rect x="{left + 250}" y="{ly - 12}" width="14" height="14" rx="2" fill="#2563eb"/>'
               f'<text class="m" x="{left + 270}" y="{ly}">{escape(c_lbl)} (sekarang)</text>')
    note = 'Data belum lengkap — angka sementara' if not e.get('data_ready') else 'Perubahan omzet per produk (bukan penyebab)'
    out.append(f'<text class="m" x="{left}" y="{h - 14}">{escape(note)}</text></svg>')
    return ''.join(out), h

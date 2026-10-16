"""Server-side SVG chart rendered strictly from one evidence payload (labels, data points and totals identical to the
JSON). Shared via short-lived HMAC-signed URLs bound to evidence_id + principal."""
import hmac
import time
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


def render_waterfall(e: dict) -> str:
    pts = waterfall_points(e)
    w, h, left, top, bottom = 760, 380, 230, 50, 30
    running, bars = 0, []
    for p in pts:
        if p['kind'] == 'total':
            start, end = 0, p['value']
            running = p['value']
        else:
            start, end = running, running + p['value']
            running = end
        bars.append((p, start, end))
    hi = max([max(b[1], b[2]) for b in bars] + [1])
    lo = min([min(b[1], b[2]) for b in bars] + [0])
    span = (hi - lo) or 1
    bar_h = (h - top - bottom) / len(bars)
    sx = lambda v: left + float((dec(v) - lo) / span) * (w - left - 20)
    title = f"{'Sell-out' if e['channel'] == 'sell_out' else 'Sell-in'} {e.get('entity', {}).get('name', '')}: " \
            f"{e['baseline_label']} → {e['current_label']}"
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img" aria-label="{escape(title)}">',
           '<style>text{font:12px system-ui,sans-serif;fill:#1f2937}.t{font-weight:600;font-size:14px}</style>',
           f'<rect width="{w}" height="{h}" fill="#ffffff"/>', f'<text class="t" x="12" y="24">{escape(title)}</text>']
    for i, (p, a, b) in enumerate(bars):
        y = top + i * bar_h
        x0, x1 = sorted((sx(a), sx(b)))
        color = '#2563eb' if p['kind'] == 'total' else ('#dc2626' if p['value'] < 0 else '#16a34a')
        out.append(f'<text x="8" y="{y + bar_h * .62:.1f}">{escape(p["label"][:34])}</text>')
        out.append(f'<rect x="{x0:.1f}" y="{y + 4:.1f}" width="{max(1.0, x1 - x0):.1f}" height="{bar_h - 8:.1f}" fill="{color}"/>')
        out.append(f'<text x="{x1 + 4:.1f}" y="{y + bar_h * .62:.1f}">{escape(rupiah(p["value"]))}</text>')
    out.append(f'<text x="8" y="{h - 8}">Evidence {escape(e["evidence_id"])} · dekomposisi akuntansi, bukan sebab · '
               f'{"PROVISIONAL" if not e.get("data_ready") else "final"}</text></svg>')
    return ''.join(out)

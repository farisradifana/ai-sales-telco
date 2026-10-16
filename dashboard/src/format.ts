// Display helpers. Values arrive as decimal strings; conversion to number happens only for chart geometry.
export function rupiah(v: string | null | undefined): string {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  const sign = n < 0 ? '-' : '';
  return `${sign}Rp${Math.abs(n).toLocaleString('id-ID', { maximumFractionDigits: 2 })}`;
}

export function pct(ratio: string | null | undefined, digits = 1): string {
  if (ratio === null || ratio === undefined) return 'n/a';
  return `${(Number(ratio) * 100).toLocaleString('id-ID', { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
}

export function qty(v: string, unit: 'PCS' | 'IDR'): string {
  const n = Number(v);
  return unit === 'PCS' ? `${n.toLocaleString('id-ID')} pcs` : `${rupiah(v)} saldo`;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun', 'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des'];
export function dayLabel(iso: string): string {
  const [, m, d] = iso.split('-');
  return `${Number(d)} ${MONTHS[Number(m) - 1]}`;
}

export function inclusiveEnd(endExclusive: string): string {
  const d = new Date(endExclusive + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
}

export function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 86400000);
}

export const CHANNEL_LABEL = { sell_in: 'Sell-in (sales → outlet)', sell_out: 'Sell-out (outlet → pelanggan)' } as const;

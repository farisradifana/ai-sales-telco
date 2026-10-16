import { Bar, BarChart, CartesianGrid, Cell, ComposedChart, Legend, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { Dashboard, RankRow, RegencyRankRow } from '../api';
import { dayLabel, pct, rupiah } from '../format';

const C = { base: '#64748b', cur: '#2563eb', neg: '#dc2626', pos: '#16a34a', miss: '#cbd5e1' };
const compact = (v: number) => (Math.abs(v) >= 1e6 ? `${(v / 1e6).toLocaleString('id-ID', { maximumFractionDigits: 1 })} jt` : v.toLocaleString('id-ID'));

function DataTable({ caption, head, rows }: { caption: string; head: string[]; rows: (string | number)[][] }) {
  return (
    <details className="datatable">
      <summary>Lihat data tabel</summary>
      <table>
        <caption>{caption}</caption>
        <thead><tr>{head.map((h) => <th key={h} scope="col">{h}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
      </table>
    </details>
  );
}

export function TrendChart({ d }: { d: Dashboard }) {
  const max = Math.max(1, ...d.trend.map((t) => Number(t.revenue_idr ?? t.observed_partial_idr ?? 0)));
  const data = d.trend.map((t) => ({
    day: t.day, label: dayLabel(t.day),
    baseline: t.period === 'baseline' && t.revenue_idr !== null ? Number(t.revenue_idr) : null,
    current: t.period === 'current' && t.revenue_idr !== null ? Number(t.revenue_idr) : null,
    missing: t.revenue_idr === null ? max * 0.04 : null,
  }));
  const missing = d.trend.filter((t) => t.revenue_idr === null);
  return (
    <section className="card wide" aria-labelledby="trend-h">
      <h2 id="trend-h">Tren harian ({d.baseline_label} & {d.current_label})</h2>
      <p className="muted">Hari dengan input belum lengkap ditandai abu-abu (bukan nol). {missing.length > 0 && <strong>{missing.length} hari belum lengkap.</strong>}</p>
      <div className="chart" role="img" aria-label={`Tren omzet harian ${d.channel}, ${missing.length} hari tanpa data lengkap`}>
        <ResponsiveContainer width="100%" height={260}>
          <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
            <XAxis dataKey="label" interval="preserveStartEnd" minTickGap={24} tick={{ fontSize: 11 }} />
            <YAxis tickFormatter={compact} width={56} tick={{ fontSize: 11 }} />
            <Tooltip formatter={(v, n) => [n === 'missing' ? 'data belum lengkap' : rupiah(String(v)), n === 'baseline' ? 'Baseline' : n === 'current' ? 'Current' : 'Missing']} />
            <Legend />
            <Bar dataKey="baseline" name="Baseline" fill={C.base} isAnimationActive={false} />
            <Bar dataKey="current" name="Current" fill={C.cur} isAnimationActive={false} />
            <Bar dataKey="missing" name="Belum lengkap" fill={C.miss} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <DataTable caption="Omzet harian (WIB)" head={['Tanggal', 'Periode', 'Omzet', 'Status']}
        rows={d.trend.map((t) => [t.day, t.period, rupiah(t.revenue_idr), t.status])} />
    </section>
  );
}

export function Waterfall({ d }: { d: Dashboard }) {
  let run = Number(d.revenue_baseline_idr);
  const drivers = [...d.drivers].sort((a, b) => Number(a.delta_idr) - Number(b.delta_idr));
  const data = [{ name: 'Baseline', base: 0, value: run, kind: 'total' }];
  for (const x of drivers) {
    const v = Number(x.delta_idr);
    data.push({ name: x.product, base: v < 0 ? run + v : run, value: Math.abs(v), kind: v < 0 ? 'neg' : 'pos' });
    run += v;
  }
  data.push({ name: 'Current', base: 0, value: Number(d.revenue_current_idr), kind: 'total' });
  return (
    <section className="card wide" aria-labelledby="wf-h">
      <h2 id="wf-h">Kontribusi SKU (waterfall)</h2>
      <p className="muted">{d.decomposition}</p>
      <div className="chart" role="img" aria-label={`Waterfall omzet dari ${rupiah(d.revenue_baseline_idr)} ke ${rupiah(d.revenue_current_idr)}`}>
        <ResponsiveContainer width="100%" height={Math.max(220, 46 * data.length)}>
          <BarChart data={data} layout="vertical" margin={{ left: 8, right: 24 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
            <XAxis type="number" tickFormatter={compact} tick={{ fontSize: 11 }} />
            <YAxis type="category" dataKey="name" width={170} tick={{ fontSize: 11 }} />
            <Tooltip formatter={(v, n, p) => (n === 'base' ? [null, null] : [rupiah(String(p.payload.kind === 'neg' ? -Number(v) : v)), 'Nilai'])} />
            <Bar dataKey="base" stackId="w" fill="transparent" isAnimationActive={false} />
            <Bar dataKey="value" stackId="w" isAnimationActive={false}>
              {data.map((x, i) => <Cell key={i} fill={x.kind === 'total' ? C.cur : x.kind === 'neg' ? C.neg : C.pos} />)}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="tablewrap">
        <table>
          <caption>Dekomposisi volume / harga-mix per SKU (satuan per SKU, tidak dijumlah lintas satuan)</caption>
          <thead><tr><th scope="col">SKU</th><th scope="col">Qty base→cur</th><th scope="col">Δ omzet</th><th scope="col">Volume</th><th scope="col">Harga/mix</th><th scope="col">Masuk/keluar</th></tr></thead>
          <tbody>
            {drivers.map((x) => (
              <tr key={x.product_id}>
                <td>{x.product}</td><td>{Number(x.q0).toLocaleString('id-ID')} → {Number(x.q1).toLocaleString('id-ID')} {x.stock_unit}</td>
                <td className={Number(x.delta_idr) < 0 ? 'neg' : 'pos'}>{rupiah(x.delta_idr)}</td>
                <td>{rupiah(x.volume_effect_idr)}</td><td>{rupiah(x.price_mix_effect_idr)}</td><td>{rupiah(x.entry_exit_effect_idr)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot><tr><th scope="row">Total</th><td /><td>{rupiah(d.delta_idr)}</td><td colSpan={3}>Residual rekonsiliasi {rupiah(d.reconciliation_residual_idr)}</td></tr></tfoot>
        </table>
      </div>
    </section>
  );
}

export interface RankItem { id: number; name: string; delta_idr: string; growth: string | null }
export const districtItems = (rows: RankRow[]): RankItem[] =>
  rows.map((r) => ({ id: r.district_id, name: r.district_name, delta_idr: r.delta_idr, growth: r.growth }));
export const regencyItems = (rows: RegencyRankRow[]): RankItem[] =>
  rows.map((r) => ({ id: r.regency_id, name: r.regency_name, delta_idr: r.delta_idr, growth: r.growth }));

// One ranking chart for kecamatan and kab/kota; rows are already filtered server-side to what the account may see.
export function Ranking({ rows, onPick, current, unit = 'kecamatan' }:
  { rows: RankItem[]; onPick: (id: number) => void; current?: number; unit?: 'kecamatan' | 'kab/kota' }) {
  const hid = unit === 'kecamatan' ? 'rk-h' : 'rkr-h';
  if (!rows.length) {
    return <section className="card" aria-labelledby={hid}><h2 id={hid}>Peringkat {unit}</h2><p className="empty">Tidak ada {unit} yang diizinkan untuk akun ini.</p></section>;
  }
  const data = rows.map((r) => ({ ...r, delta: Number(r.delta_idr) }));
  return (
    <section className="card" aria-labelledby={hid}>
      <h2 id={hid}>Peringkat {unit} (yang Anda akses)</h2>
      <p className="muted">Peringkat bukan dasar tindakan: diagnosis coverage dan kontribusi {unit === 'kecamatan' ? 'outlet' : 'kecamatan'} dulu. Klik untuk drill-down.</p>
      <div className="chart" role="img" aria-label={`Perubahan omzet per ${unit}`}>
        <ResponsiveContainer width="100%" height={Math.max(160, 28 * data.length)}>
          <BarChart data={data} layout="vertical" margin={{ left: 8, right: 16 }}>
            <XAxis type="number" tickFormatter={compact} tick={{ fontSize: 11 }} />
            <YAxis type="category" dataKey="name" width={150} tick={{ fontSize: 11 }} />
            <ReferenceLine x={0} stroke="#94a3b8" />
            <Tooltip formatter={(v) => [rupiah(String(v)), 'Δ omzet']} />
            <Bar dataKey="delta" isAnimationActive={false} onClick={(x) => onPick((x as unknown as RankItem).id)} cursor="pointer">
              {data.map((r) => <Cell key={r.id} fill={r.id === current ? '#7c3aed' : r.delta < 0 ? C.neg : C.pos} />)}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <ul className="linklist">
        {rows.map((r) => (
          <li key={r.id}>
            <button type="button" className="link" onClick={() => onPick(r.id)} aria-label={`Drill-down ${unit} ${r.name}`}>
              {r.name}</button>: {rupiah(r.delta_idr)} ({pct(r.growth)})
          </li>
        ))}
      </ul>
    </section>
  );
}

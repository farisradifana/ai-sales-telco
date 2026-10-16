import { useEffect, useState } from 'react';
import { api, ApiError, type Dashboard, type PendingAction, type StockEvidence, type TargetEvidence, type Channel, type Me } from '../api';
import { CHANNEL_LABEL, inclusiveEnd, pct, qty, rupiah } from '../format';

export function Banner({ d, stale }: { d: Dashboard; stale: boolean }) {
  const cls = !d.data_ready ? 'banner warn' : stale ? 'banner info' : 'banner ok';
  return (
    <div className={cls} role="status" aria-live="polite">
      <strong>{d.data_ready ? 'Data final' : 'PROVISIONAL'}</strong>
      <span>Watermark {d.channel}: {d.watermark ?? 'tidak tersedia'} · batch {d.source_batch_key ?? '—'}</span>
      <span>Kelengkapan input: current {pct(d.current_complete_ratio)} · baseline {pct(d.baseline_complete_ratio)}</span>
      {!d.data_ready && <span>Masalah: {d.issues.join(', ')}. Perbaiki data dulu sebelum menyimpulkan tren.</span>}
      {stale && <span>Snapshot berumur &gt; 15 menit; muat ulang untuk angka terbaru.</span>}
    </div>
  );
}

export function Kpis({ d, target }: { d: Dashboard; target: TargetEvidence | null }) {
  return (
    <section className="kpis" aria-label="Ringkasan KPI">
      <div className="kpi"><span className="kpi-label">Baseline · {d.baseline_label}</span><span className="kpi-val">{rupiah(d.revenue_baseline_idr)}</span>
        <span className="muted">{d.baseline.start} – {inclusiveEnd(d.baseline.end_exclusive)} ({d.baseline.days} hari)</span></div>
      <div className="kpi"><span className="kpi-label">Current · {d.current_label}</span><span className="kpi-val">{rupiah(d.revenue_current_idr)}</span>
        <span className="muted">{d.current.start} – {inclusiveEnd(d.current.end_exclusive)} ({d.current.days} hari)</span></div>
      <div className="kpi"><span className="kpi-label">Perubahan</span>
        <span className={`kpi-val ${Number(d.delta_idr) < 0 ? 'neg' : 'pos'}`}>{rupiah(d.delta_idr)}</span>
        <span className="muted">Growth {d.growth === null ? 'n/a (baseline 0/negatif)' : pct(d.growth, 2)}</span></div>
      <div className="kpi"><span className="kpi-label">Target hari ini ({target ? CHANNEL_LABEL[target.channel].split(' ')[0] : '—'})</span>
        {target && target.target_available
          ? <><span className="kpi-val">{pct(target.achievement_ratio)}</span>
            <span className="muted">{rupiah(target.actual_idr)} / {rupiah(target.target_idr)}{target.intraday ? ' · berjalan' : ''}</span></>
          : <span className="muted">{target ? 'Target belum tersedia' : 'Hanya untuk akun sales'}</span>}
      </div>
    </section>
  );
}

export function Contributors({ d, onPick }: { d: Dashboard; onPick: (outletId: number) => void }) {
  const rows = [...d.outlet_contributions].sort((a, b) => Number(a.delta_idr) - Number(b.delta_idr));
  return (
    <section className="card" aria-labelledby="ct-h">
      <h2 id="ct-h">Kontributor outlet</h2>
      <p className="muted">{d.population.note} Outlet di cakupan: {d.population.outlets_in_scope}; buka selama perbandingan: {d.population.opened_during_comparison}; tutup: {d.population.closed_during_comparison}.</p>
      {rows.length === 0 ? <p className="empty">Tidak ada outlet pada cakupan ini.</p> : (
        <div className="tablewrap"><table>
          <thead><tr><th scope="col">Outlet</th><th scope="col">Baseline</th><th scope="col">Current</th><th scope="col">Δ</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.outlet_id}>
              <td>{d.scope === 'district'
                ? <button type="button" className="link" onClick={() => onPick(r.outlet_id)}>{r.name}</button> : r.name}</td>
              <td>{rupiah(r.baseline_idr)}</td><td>{rupiah(r.current_idr)}</td><td className={Number(r.delta_idr) < 0 ? 'neg' : 'pos'}>{rupiah(r.delta_idr)}</td>
            </tr>))}</tbody>
        </table></div>)}
    </section>
  );
}

export function Actions({ d }: { d: Dashboard }) {
  const r = d.recommendation;
  return (
    <section className="card" aria-labelledby="ac-h">
      <h2 id="ac-h">Rencana tindakan</h2>
      <p className="muted">{r.execution_policy}</p>
      <ol className="actions">
        {r.actions.map((a) => (
          <li key={a.priority + a.action} className="action">
            <p className="action-title">{a.action}</p>
            {a.fact && <p><strong>Fakta:</strong> {a.fact}</p>}
            {a.hypothesis && <p><strong>Hipotesis:</strong> {a.hypothesis}</p>}
            <p><strong>Pemilik:</strong> {a.owner} · <strong>Tinjau:</strong> {a.review_in_days} hari · <strong>Sukses:</strong> {a.success_metric}</p>
            {a.guardrail && <p><strong>Guardrail:</strong> {a.guardrail}</p>}
            <p className="muted">Kualitas bukti: {a.confidence} · bukti: {a.evidence.join(', ')}</p>
          </li>
        ))}
      </ol>
      <details><summary>Batas bukti</summary><ul>{r.limitations.map((l) => <li key={l}>{l}</li>)}<li>{d.stockout}</li></ul></details>
      <p className="evid">Evidence <a href={`/v1/evidence/${d.evidence_id}`} target="_blank" rel="noreferrer">{d.evidence_id}</a> ·
        <a href={`/v1/evidence/${d.diagnose_evidence_id}/chart.svg`} target="_blank" rel="noreferrer"> grafik SVG</a> · digest {d.evidence_digest.slice(0, 12)}…</p>
    </section>
  );
}

function useLoad<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [state, set] = useState<{ data?: T; error?: string; loading: boolean }>({ loading: true });
  useEffect(() => {
    let live = true;
    set({ loading: true });
    fn().then((data) => live && set({ data, loading: false })).catch((e: ApiError) => live && set({ error: e.message, loading: false }));
    return () => { live = false; };
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  return state;
}

export function StockPanel({ me, outletId }: { me: Me; outletId: number | null }) {
  const body = me.role === 'sales' ? { account: 'self' } : outletId ? { account: 'outlet', outlet_id: outletId } : null;
  const s = useLoad<StockEvidence | null>(() => (body ? api.stock(body) : Promise.resolve(null)), [JSON.stringify(body)]);
  return (
    <section className="card" aria-labelledby="st-h">
      <h2 id="st-h">Stok {me.role === 'sales' ? 'yang Anda pegang' : outletId ? 'outlet terpilih' : ''}</h2>
      {s.loading ? <p className="loading">Memuat stok…</p> : s.error ? <p className="error" role="alert">{s.error}</p> : !s.data ? <p className="empty">Pilih outlet untuk melihat stok.</p> : (
        <>
          <div className="tablewrap"><table>
            <caption>Per SKU dan satuan; PCS dan IDR tidak dijumlahkan</caption>
            <thead><tr><th scope="col">SKU</th><th scope="col">Produk</th><th scope="col">Saldo</th></tr></thead>
            <tbody>{s.data.items.map((i) => <tr key={i.product_id}><td>{i.sku}</td><td>{i.product}</td><td>{qty(i.quantity, i.stock_unit)}</td></tr>)}</tbody>
          </table></div>
          <p className="evid">Evidence {s.data.evidence_id} · mutasi terakhir {s.data.last_movement_at ?? '—'}</p>
        </>
      )}
    </section>
  );
}

export function useTarget(me: Me | null, channel: Channel) {
  return useLoad<TargetEvidence | null>(() => (me?.role === 'sales' ? api.target(channel) : Promise.resolve(null)), [me?.principal_id, channel]);
}

export function PendingPanel({ me }: { me: Me }) {
  const [tick, setTick] = useState(0);
  const [msg, setMsg] = useState('');
  const s = useLoad<{ items: PendingAction[] }>(() => api.actions(), [tick]);
  if (me.role === 'sales' && !s.data?.items.length) return null;
  const decide = async (id: string, decision: 'approve' | 'reject') => {
    if (!window.confirm(decision === 'approve' ? 'Setujui dan posting aksi ini? Tindakan tidak bisa diedit; koreksi lewat reversal.' : 'Tolak aksi ini?')) return;
    try { await api.decide(id, decision); setMsg('Keputusan tercatat.'); } catch (e) { setMsg((e as ApiError).message); }
    setTick((t) => t + 1);
  };
  return (
    <section className="card" aria-labelledby="pd-h">
      <h2 id="pd-h">Persetujuan reversal / transfer</h2>
      {msg && <p role="status">{msg}</p>}
      {s.loading ? <p className="loading">Memuat…</p> : s.error ? <p className="error" role="alert">{s.error}</p> :
        !s.data?.items.length ? <p className="empty">Tidak ada aksi menunggu.</p> : (
          <ul className="pending">{s.data.items.map((a) => (
            <li key={a.action_id}><p>{a.summary}</p><p className="muted">Diajukan {a.requested_by} · {new Date(a.created_at).toLocaleString('id-ID')}</p>
              {a.can_decide && <div className="row"><button type="button" onClick={() => decide(a.action_id, 'approve')}>Setujui</button>
                <button type="button" className="secondary" onClick={() => decide(a.action_id, 'reject')}>Tolak</button></div>}</li>))}</ul>)}
    </section>
  );
}

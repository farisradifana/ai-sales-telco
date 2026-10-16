import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError, type Channel, type Dashboard, type Me, type PeriodInput, type ScopeType } from './api';
import { Ranking, TrendChart, Waterfall } from './components/Charts';
import { Actions, Banner, Contributors, Kpis, PendingPanel, StockPanel, useTarget } from './components/Panels';
import { CHANNEL_LABEL, daysBetween } from './format';

interface ViewState { scope: ScopeType; id: number; channel: Channel; period: PeriodInput | null }

// Drill-through state lives in the URL hash so links keep scope/period/channel.
function readHash(): Partial<ViewState> {
  const p = new URLSearchParams(window.location.hash.slice(1));
  const period = p.get('bs') ? { baseline_start: p.get('bs')!, baseline_end: p.get('be')!, current_start: p.get('cs')!, current_end: p.get('ce')! } : null;
  const scope = p.get('scope');
  return { scope: scope === 'district' || scope === 'outlet' ? scope : undefined, id: Number(p.get('id')) || undefined,
           channel: p.get('ch') === 'sell_in' ? 'sell_in' : p.get('ch') === 'sell_out' ? 'sell_out' : undefined, period };
}
function writeHash(v: ViewState) {
  const p = new URLSearchParams({ scope: v.scope, id: String(v.id), ch: v.channel });
  if (v.period) { p.set('bs', v.period.baseline_start); p.set('be', v.period.baseline_end); p.set('cs', v.period.current_start); p.set('ce', v.period.current_end); }
  window.history.replaceState(null, '', '#' + p.toString());
}

function Login({ onDone }: { onDone: () => void }) {
  const [pid, setPid] = useState('');
  const [err, setErr] = useState('');
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try { await api.devLogin(pid.trim()); onDone(); } catch (x) { setErr((x as ApiError).status === 404 ? 'Login lokal tidak aktif di lingkungan ini; gunakan SSO perusahaan.' : (x as ApiError).message); }
  };
  return (
    <main className="login">
      <h1>TELCO Sales Insight</h1>
      <p>Masuk untuk melihat cakupan outlet/kecamatan yang diizinkan untuk akun Anda.</p>
      <form onSubmit={submit}>
        <label htmlFor="pid">ID akun (demo lokal)</label>
        <input id="pid" value={pid} onChange={(e) => setPid(e.target.value)} placeholder="mis. hob-35.19 / tm-35.19-01 / sls-35.19.01" autoComplete="username" required />
        <button type="submit">Masuk</button>
      </form>
      {err && <p className="error" role="alert">{err}</p>}
      <p className="muted">Data demo sepenuhnya fiktif untuk outlet, sales dan transaksi; wilayah administrasi Jawa Timur nyata.</p>
    </main>
  );
}

function PeriodPicker({ value, onApply }: { value: PeriodInput | null; onApply: (p: PeriodInput | null) => void }) {
  const [p, setP] = useState<PeriodInput>(value ?? { baseline_start: '', baseline_end: '', current_start: '', current_end: '' });
  const bd = p.baseline_start && p.baseline_end ? daysBetween(p.baseline_start, p.baseline_end) : 0;
  const cd = p.current_start && p.current_end ? daysBetween(p.current_start, p.current_end) : 0;
  const complete = Object.values(p).every(Boolean);
  const problem = !complete ? '' : bd <= 0 || cd <= 0 ? 'Tanggal akhir harus setelah tanggal awal.' : bd !== cd ? `Jumlah hari harus sama (${bd} vs ${cd}).` :
    p.baseline_end > p.current_start ? 'Baseline harus selesai sebelum current.' : '';
  const field = (k: keyof PeriodInput, label: string) => (
    <label>{label}<input type="date" value={p[k]} onChange={(e) => setP({ ...p, [k]: e.target.value })} /></label>);
  return (
    <fieldset className="period">
      <legend>Periode perbandingan (akhir eksklusif, WIB)</legend>
      {field('baseline_start', 'Baseline mulai')}{field('baseline_end', 'Baseline akhir (eksklusif)')}
      {field('current_start', 'Current mulai')}{field('current_end', 'Current akhir (eksklusif)')}
      <div className="row">
        <button type="button" disabled={!complete || !!problem} onClick={() => onApply(p)}>Terapkan</button>
        <button type="button" className="secondary" onClick={() => onApply(null)}>Default watermark</button>
      </div>
      {problem && <p className="error" role="alert">{problem}</p>}
    </fieldset>
  );
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [authState, setAuth] = useState<'loading' | 'anon' | 'ok' | 'error'>('loading');
  const [authErr, setAuthErr] = useState('');
  const [view, setView] = useState<ViewState | null>(null);
  const [dash, setDash] = useState<{ data?: Dashboard; error?: ApiError; loading: boolean; at?: number }>({ loading: false });
  const [now, setNow] = useState(Date.now());

  const loadMe = useCallback(() => {
    setAuth('loading');
    api.me().then((m) => {
      setMe(m); setAuth('ok');
      const h = readHash();
      const firstOutlet = m.outlets[0]?.id, firstDistrict = m.districts[0]?.id;
      const scope: ScopeType = h.scope ?? (firstDistrict && m.role !== 'sales' ? 'district' : 'outlet');
      const id = h.id ?? (scope === 'district' ? firstDistrict : firstOutlet);
      if (id) setView({ scope, id, channel: h.channel ?? 'sell_out', period: h.period ?? null });
    }).catch((e: ApiError) => { if (e.status === 401) setAuth('anon'); else { setAuthErr(e.message); setAuth('error'); } });
  }, []);
  useEffect(loadMe, [loadMe]);
  // Shared/bookmarked drill-through links: a changed hash re-applies scope/channel/period (writeHash uses replaceState,
  // so our own updates do not loop back here).
  useEffect(() => {
    const onHash = () => {
      const h = readHash();
      setView((v) => (v && h.id && h.scope ? { scope: h.scope, id: h.id, channel: h.channel ?? v.channel, period: h.period ?? null } : v));
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 60000); return () => clearInterval(t); }, []);

  useEffect(() => {
    if (!view) return;
    writeHash(view);
    let live = true;
    setDash((d) => ({ ...d, loading: true, error: undefined }));
    api.dashboard(view.scope, view.id, view.channel, view.period ?? undefined)
      .then((data) => live && setDash({ data, loading: false, at: Date.now() }))
      .catch((error: ApiError) => live && setDash({ error, loading: false }));
    return () => { live = false; };
  }, [view]);

  const target = useTarget(me, view?.channel ?? 'sell_out');
  const options = useMemo(() => (view?.scope === 'district' ? me?.districts ?? [] : me?.outlets ?? []), [me, view?.scope]);

  if (authState === 'loading') return <p className="loading page">Memuat sesi…</p>;
  if (authState === 'error') return (
    <main className="page"><div className="error card" role="alert"><p>Layanan belum dapat dihubungi: {authErr}</p>
      <button type="button" onClick={loadMe}>Coba lagi</button></div></main>);
  if (authState === 'anon' || !me) return <Login onDone={loadMe} />;
  if (!view) return <main className="page"><p className="empty">Akun ini belum memiliki cakupan outlet/kecamatan. Hubungi admin untuk provisioning izin.</p></main>;

  const d = dash.data;
  const stale = !!dash.at && now - dash.at > 15 * 60000;
  const exportHref = d ? api.exportUrl(d.scope, d.entity_id, d.channel, d.current.start, d.current.end_exclusive) : undefined;

  return (
    <div className="page">
      <header className="top">
        <div><h1>TELCO Sales Insight</h1><p className="muted">{me.display_name}{me.org?.sales_area ? ` · Area ${me.org.sales_area}` : ''}{me.org?.manager_title ? ` · melapor ke ${me.org.manager_title}` : ''} · {me.districts.length} kecamatan · {me.outlets.length} outlet</p></div>
        <button type="button" className="secondary" onClick={() => api.logout().then(() => { setMe(null); setAuth('anon'); })}>Keluar</button>
      </header>

      <form className="filters" onSubmit={(e) => e.preventDefault()} aria-label="Filter cakupan">
        <label>Cakupan
          <select value={view.scope} onChange={(e) => {
            const scope = e.target.value as ScopeType;
            const first = scope === 'district' ? me.districts[0]?.id : me.outlets[0]?.id;
            if (first) setView({ ...view, scope, id: first });
          }}>
            <option value="outlet" disabled={!me.outlets.length}>Outlet</option>
            <option value="district" disabled={!me.districts.length}>Kecamatan</option>
          </select>
        </label>
        <label>{view.scope === 'district' ? 'Kecamatan' : 'Outlet'}
          <select value={view.id} onChange={(e) => setView({ ...view, id: Number(e.target.value) })}>
            {options.map((o) => <option key={o.id} value={o.id}>{o.code} · {o.name}</option>)}
          </select>
        </label>
        <fieldset className="channel">
          <legend>Channel (wajib dipilih; tidak digabung)</legend>
          {(['sell_out', 'sell_in'] as Channel[]).map((c) => (
            <label key={c} className="radio"><input type="radio" name="ch" checked={view.channel === c} onChange={() => setView({ ...view, channel: c })} />{CHANNEL_LABEL[c]}</label>))}
        </fieldset>
        {exportHref && <a className="button" href={exportHref} download>Export CSV (current)</a>}
      </form>
      <PeriodPicker key={JSON.stringify(view.period)} value={view.period} onApply={(period) => setView({ ...view, period })} />

      {dash.loading && <p className="loading" role="status">Menghitung evidence…</p>}
      {dash.error && (
        <div className="error card" role="alert">
          <p><strong>{dash.error.code}</strong>: {dash.error.message}</p>
          {dash.error.correlationId && <p className="muted">ID korelasi {dash.error.correlationId}</p>}
          {dash.error.status === 401 && <button type="button" onClick={loadMe}>Masuk ulang</button>}
        </div>)}
      {d && !dash.error && (
        <main>
          <Banner d={d} stale={stale} />
          <p className="crumbs">{d.scope === 'outlet' ? `Outlet ${d.entity.code} · ${d.entity.name}${d.entity.district_name ? ' · Kec. ' + d.entity.district_name : ''}` : `Kecamatan ${d.entity.name} (${d.entity.code})`}
            {' '}· {CHANNEL_LABEL[d.channel]} · {d.current_label} vs {d.baseline_label}
            {d.scope === 'outlet' && d.entity.district_id && me.districts.some((x) => x.id === d.entity.district_id) &&
              <> · <button type="button" className="link" onClick={() => setView({ ...view, scope: 'district', id: d.entity.district_id! })}>lihat kecamatan</button></>}
          </p>
          <Kpis d={d} target={target.data ?? null} />
          <div className="grid">
            <TrendChart d={d} />
            <Waterfall d={d} />
            <Contributors d={d} onPick={(id) => me.outlets.some((o) => o.id === id) && setView({ ...view, scope: 'outlet', id })} />
            <Ranking rows={d.district_ranking} current={d.scope === 'district' ? d.entity_id : undefined}
              onPick={(id) => setView({ ...view, scope: 'district', id })} />
            <Actions d={d} />
            <StockPanel me={me} outletId={d.scope === 'outlet' ? d.entity_id : null} />
            <PendingPanel me={me} />
          </div>
        </main>)}
    </div>
  );
}

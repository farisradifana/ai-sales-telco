// Tab "Info": the export metadata as a tidy two-column table (Keterangan | Nilai), human-readable WIB dates.
const text = $('Export CSV').first().json.csv || '';
const meta = {};
for (const l of text.split('\n')) {
  if (!l.startsWith('# ')) continue;
  const i = l.indexOf(': ');
  meta[l.slice(2, i)] = l.slice(i + 2);
}
const tgl = (iso) => {
  if (!iso) return '';
  const [y, m, d] = iso.slice(0, 10).split('-');
  return `${d}-${m}-${y}`;
};
const minusOneDay = (iso) => {
  const t = new Date(iso + 'T00:00:00Z');
  t.setUTCDate(t.getUTCDate() - 1);
  return t.toISOString().slice(0, 10);
};
const wib = (iso) => {
  const t = new Date(new Date(iso).getTime() + 7 * 3600 * 1000).toISOString();
  return `${tgl(t)} ${t.slice(11, 16)} WIB`;
};
const channel = { sell_out: 'Sell-out (outlet → pelanggan)', sell_in: 'Sell-in (sales → outlet)' }[meta.channel] || meta.channel;
const rows = [
  ['Judul', 'Rekap penjualan (snapshot otomatis)'],
  ['Cakupan', meta.scope_name || `${meta.scope} ${meta.entity_id}`],
  ['Channel', channel],
  ['Periode', `${tgl(meta.period_start)} s.d. ${tgl(minusOneDay(meta.period_end_exclusive))}`],
  ['Data lengkap sampai (watermark)', tgl(meta.watermark)],
  ['Total omzet (IDR)', Number(meta.total_revenue_idr)],
  ['Total kuantitas fisik (PCS)', Number(meta.quantity_pcs || 0)],
  ['Total saldo terjual (IDR nominal)', Number(meta.quantity_idr || 0)],
  ['Jumlah baris di tab Rekap', Number(meta.rows)],
  ['Satuan omzet', meta.revenue_unit],
  ['Waktu ambil data', wib(meta.generated_at)],
  ['Diambil oleh', meta.principal],
  ['Batch sumber', meta.source_batch_key],
  ['Versi format', meta.export_version],
  ['Catatan', meta.note],
  ['Catatan periode', 'Hari setelah tanggal watermark berisi transaksi live yang mungkin belum lengkap.'],
];
return rows.map(([k, v]) => ({ json: { Keterangan: k, Nilai: v } }));

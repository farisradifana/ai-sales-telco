// Tab "Rekap": data table only (Indonesian column headers, numbers as numbers). Metadata goes to tab "Info".
const text = $('Export CSV').first().json.csv || '';
const lines = text.split('\n').filter((l) => l.length && !l.startsWith('# '));
const header = lines.shift().split(',');
const label = {
  day_wib: 'Tanggal (WIB)', outlet_code: 'Kode Outlet', outlet_name: 'Nama Outlet', sku: 'SKU', stock_unit: 'Satuan',
  quantity: 'Kuantitas', revenue_idr: 'Omzet (IDR)', net_lines: 'Jumlah Transaksi Bersih',
};
const numeric = new Set(['quantity', 'revenue_idr', 'net_lines']);
return lines
  .filter((l) => !l.startsWith('TOTAL,'))
  .map((l) => {
    const v = l.split(',');
    const row = {};
    header.forEach((h, i) => { row[label[h] || h] = numeric.has(h) ? Number(v[i]) : (v[i] ?? ''); });
    return { json: row };
  });

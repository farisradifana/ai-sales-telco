# KPI, decomposition, evidence dan keputusan

Sumber otoritatif demo: tabel transaksi posted + reversal; views SQL turunannya. Bukan spreadsheet atau jawaban LLM. Kalender bisnis WIB; minggu/hari mengikuti tanggal lokal. Tahun/kuartal diperoleh `extract(year from day)` dan `extract(quarter from day)`; Jan–Mar Q1, Apr–Jun Q2, Jul–Sep Q3, Okt–Des Q4. Rentang mencakup 2025Q1–2026Q3. Views bulanan adalah agregasi baris yang ada; grafik tren harus memakai calendar scaffold dan menandai missing coverage sebagai unknown, bukan otomatis nol.

| KPI | Formula dan populasi | Penggunaan / batas |
|---|---|---|
| Sell-out omzet net IDR | SUM(sell_out.net_revenue) minus full reversal pada tanggal reversal | KPI utama permintaan teramati outlet; bukan margin; channel wajib |
| Sell-in omzet net IDR | SUM(sell_in.net_revenue) minus reversal pada tanggal reversal | KPI utama distribusi sales; jangan tambahkan sell-out karena barang yang sama melewati dua tahap |
| Pencapaian target | actual sales-day-channel / target sales-day-channel | actual fisik+saldo pada **channel yang sama**, denominasi semua IDR; baseline target demo asumsi planning |
| Gap target | target−actual, capped 0 untuk pesan motivasi | bukan alasan menekan pengisian stok berlebih; pesan opt-in dan jam kerja |
| Growth | (current−baseline)/baseline; null jika baseline=0 | bandingkan hari setara; nol/negatif baseline perlu label khusus bukan persentase menyesatkan |
| Quantity per SKU | SUM(quantity) net; PCS atau IDR sesuai SKU | jangan menjumlahkan lintas satuan; saldo IDR tidak sama dengan satu transaksi |
| Effective price per SKU | revenue_idr / net quantity jika quantity>0 | mengandung discount dan efek mix dalam SKU; net returns bisa membuat rasio tak bermakna |
| Active outlets | count distinct outlet dengan positive gross posted sell-out lines | hitung dari gross_lines, bukan net_lines; pendapatan outlet 0 tetap bagian populasi yang buka |
| Outlet-day completeness | complete outlet-days / expected open outlet-days | watermark dan log kelengkapan diperlukan; tidak ada transaksi != tidak ada input |
| Visit completion | visits_done / visits_planned; null bila planned=0 | metrik pelaksanaan, tidak membuktikan kausalitas penjualan |
| Stock on hand | SUM ledger delta per account+SKU / balance cache | historical dari ledger <= waktu cutoff; tidak boleh dianggap stockout sepanjang hari |
| Stockout observed minutes | SUM durasi observasi yang benar-benar diukur; coverage observasi dilaporkan | NULL artinya unknown. Dataset ini tidak mengukur stockout; jangan buat klaim lost sales |

Pilih tiga outcome: sell-out net untuk outlet, sell-in net untuk distribusi, achievement untuk target sales. Driver: volume/harga per SKU dan kunjungan. Guardrails: kelengkapan, stok negatif/reconciliation, serta biaya diskon (COGS belum tersedia). Target IDR3,5 juta/hari sell-in dan IDR4 juta/hari sell-out per sales pada generator hanyalah parameter fiktif untuk menguji flow; bukan benchmark telco. Target perlu validasi owner bisnis sebelum produksi; target dapat musiman dan tidak harus dicapai setiap hari.

## Diagnosis

1. Resolve entitas dengan ID/kode; nama ambigu wajib diklarifikasi. Tetapkan channel (default sell-out untuk omzet outlet/kecamatan), periode, baseline, timezone, cakupan izin.
2. Periksa ingestion watermark **dan** coverage kedua periode. Data readiness gagal jika ada expected outlet-day belum lengkap. Fakta hitungan tetap dapat ditampilkan dengan label provisional; jangan menyatakan penurunan bisnis final atau memberi tindakan stok/harga.
3. Hitung revenue baseline/current/delta dan ranking kontribusi per produk. Untuk kecamatan, pertahankan seluruh outlet termasuk yang tidak bertransaksi; lakukan drill-down outlet untuk mencari konsentrasi dan cohort buka/tutup.
4. Untuk SKU dengan q0,q1>0, p0=r0/q0 dan p1=r1/q1:
   - efek volume = `(q1−q0)*(p0+p1)/2`;
   - efek harga/mix = `(p1−p0)*(q0+q1)/2`.
   Keduanya menjumlah tepat ke `r1−r0` (symmetric/Shapley dua faktor). Jika salah satu q<=0, seluruh delta SKU masuk entry/exit/nonpositive bucket. Jangan memaksakan interpretasi harga untuk retur net negatif. Dengan kategori produk, kontribusi dijumlah dari SKU, bukan harga rata-rata PCS+IDR.
5. Jumlah semua efek harus sama dengan delta total; residual numeric maksimum Rp0,01. Kontribusi negatif/positif bisa lebih dari 100% bila saling offset, jadi tampilkan nilai IDR lebih dulu.
6. Cocokkan pola dengan observasi lain: kunjungan dan harga pada demo adalah asosiasi. Kompetitor, gangguan jaringan, promosi kompetitor, pelanggan hilang dan demand yang tidak terpenuhi tidak diukur; label unknown. Jangan menggunakan scenario_truth sebagai bukti sebab.
7. Rekomendasi memuat action, pemilik, bukti terukur, prioritas, batas ketidakpastian, batas biaya dan waktu review. Contoh: verifikasi kunjungan yang tidak terlaksana, audit SKU penyumbang penurunan, uji intervensi 7 hari terhadap outlet pembanding. Tidak ada perkiraan uplift atau confidence interval yang dibuat-buat.

Confidence output berupa **kualitas bukti** (`accounting verified`, `hypothesis`, `insufficient_data`), bukan probabilitas kausal. Jika ingin mengukur dampak kunjungan, desain eksperimen acak atau kontrol matched dengan pretrend, sample size, dan guardrail diskon. Dataset skenario ini tidak membuktikan dampak intervensi dunia nyata.

Untuk follow-up “apa yang harus dilakukan?”, pakai kembali entity, channel, dua periode dan evidence digest dari konteks yang valid; jangan berubah ke kecamatan/channel/periode lain tanpa menyebutkannya. Reauthorize principal setiap kali. Bila watermark/batch berubah, ulangi diagnosis dan beri tahu angka diperbarui. Jika TTL habis atau konteks ambigu, minta cakupan singkat. Periode terakhir default harus didasarkan complete_through, bukan tanggal hari ini (dataset berhenti September 2026).

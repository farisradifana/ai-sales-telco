# TELCO Sales AI — database-first

Paket runnable untuk demo sell-in/sell-out, **bukan data perusahaan nyata**. Semua nama wilayah, outlet, pemilik, pelanggan, alamat, produk, observasi kunjungan dan koordinat adalah fiktif. Kode wilayah `FX-*` bukan kode administrasi resmi. Koordinat adalah titik simulasi dalam bounding-box yang konsisten per kecamatan; tautan peta tidak menyatakan ada outlet pada titik tersebut. Tidak ada nomor telepon pelanggan nyata ataupun nomor acak yang bisa salah terkirim ke orang sungguhan.

## Hasil dan batas cakupan

- 500.000 **baris transaksi satu produk** = 100.000 sell-in + 400.000 sell-out, 1 Jan 2025–30 Sep 2026, waktu bisnis Asia/Jakarta (WIB). Bukan 500.000 per channel, bukan jumlah header invoice.
- 2 provinsi fiktif → 4 kabupaten → 12 kecamatan → 120 outlet; 12 sales; 6 SKU perdana/voucher/saldo.
- 100.000 receipt eksternal, 700.000 baris ledger otomatis, 15.312 target sales-harian-channel, 76.560 outlet-days di luar 500.000 baris transaksi.
- Stok sales/outlet; sell-in memindahkan stok sales→outlet; sell-out mengonsumsi stok outlet; saldo memakai satuan IDR, produk fisik PCS. Jangan menjumlahkan unit PCS dan IDR.
- DDL/migrasi, generator, validasi, analytical views/functions, adapter tools Python, konteks follow-up, tes, dan prompt infrastruktur siap tempel. Infrastruktur WhatsApp/n8n/Hermes/9Router/API/dashboard **dispesifikasikan untuk dibangun Claude Code**, belum dideploy di cloud.

## Mulai tanpa database

Python 3.12 direkomendasikan untuk reproduksi byte identik. Perintah dari folder ini:

```sh
python -m unittest discover -s tests -v
# data/full sudah disertakan. Untuk menghasilkan ulang, pilih folder kosong:
python -m telco.generate --out data/regenerated --seed 42 --rows 500000
python -m telco.validate_csv data/full --report examples/csv_verification.json
```

Pengujian PostgreSQL tersemat opsional (Node.js 20+, download dependency development):

```sh
npm ci --ignore-scripts
npm run verify:pg-wasm
```

Ini menjalankan semua migrations dan audit pada 500.000 transaksi, lalu tes constraint, reversal, kontribusi driver, coverage per channel dan role permissions. Data tetap dalam database memori sementara; CSV tidak diubah.

Generator menolak menimpa dataset. Seed, jumlah, versi RNG, jumlah per tabel, ukuran dan SHA-256 seluruh CSV ada di `data/full/manifest.json`. Untuk memverifikasi determinisme penuh, jalankan ke folder kosong dengan seed dan Python yang sama, lalu bandingkan dua manifest. `scenario_truth.json` hanya untuk penguji, tidak untuk evidence agent. Data sintetis memakai pola 1 receipt → 1 sell-in → 4 sell-out; ini membatasi realisme dan tidak cocok untuk melatih model kausal atau proyeksi bisnis produksi.

## PostgreSQL lokal

Prasyarat Python 3.12, Docker Compose v2, akses pemasangan dependency. Gunakan database kosong khusus demo. Contoh Bash (Linux/cloud):

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export POSTGRES_PASSWORD='replace-with-a-local-secret'
docker compose up -d --wait
export DATABASE_URL="postgresql://telco_owner:${POSTGRES_PASSWORD}@127.0.0.1:5432/telco"
python -m telco.db migrate
python -m telco.db load --data data/full
python -m telco.db validate
python -m telco.tools --scope outlet --id 1 --baseline 2026-06-01 2026-07-01 --current 2026-09-01 2026-10-01
python -m telco.tools --scope district --id 1
```

PowerShell: aktivasi `.\.venv\Scripts\Activate.ps1`; set variabel dengan `$env:POSTGRES_PASSWORD='...'` dan `$env:DATABASE_URL='postgresql://...'`. URI password yang memuat karakter khusus harus di-URL-encode. Jangan salin secret ke riwayat bersama; cloud memakai secret manager. Docker bind localhost saja.

Migrations satu transaksi, advisory lock, dan checksum pada `public.telco_migration`; aman dijalankan ulang, perubahan file yang sudah diaplikasikan ditolak. Load CSV juga satu transaksi; FK/trigger/constraints tetap aktif. Jangan menonaktifkan trigger untuk mempercepat load. Kegagalan mengembalikan seluruh batch; ulangi sesudah memperbaiki masalah. Load kedua ditolak jika schema sudah berisi master/transaksi. Untuk dataset baru gunakan database baru, tidak ada perintah reset/drop otomatis. `sql/security.sql` merupakan provision opsional setelah load oleh administrator CREATEROLE; bukan migrasi otomatis, bukan untuk akun LLM.

## Cara membaca hasil analisis

Tanggal akhir **eksklusif**. Contoh Juni `[2026-06-01,2026-07-01)` versus September `[2026-09-01,2026-10-01)`, masing-masing 30 hari. Agustus vs September ditolak karena 31 vs 30 hari; gunakan MTD setara, revenue/day terpisah, atau baseline 30 hari yang jelas. Pengguna harus diberi tahu bulan perbandingan yang dipilih. End date `2026-10-01` bukan transaksi Oktober: dataset berakhir 30 September.

```sql
SELECT * FROM telco.v_monthly_outlet WHERE outlet_id=1 AND channel='sell_out' ORDER BY month;
SELECT * FROM telco.rank_districts('sell_out','2026-06-01','2026-07-01','2026-09-01','2026-10-01');
SELECT telco.diagnose('district',1,'sell_out','2026-06-01','2026-07-01','2026-09-01','2026-10-01');
SELECT * FROM telco.v_target_achievement WHERE sales_id=1 AND channel='sell_in';
SELECT * FROM telco.v_stock WHERE account_id=1;
```

`diagnose` mengembalikan angka baseline/current, kontribusi tiap produk, rekonsiliasi driver, coverage, watermark dan versi query. Python `recommend` memberi action owner, evidence path, waktu review, indikator keberhasilan dan ketidakpastian. Contoh keluaran nyata ada di `examples/`; bukan template angka karangan. Ranking kecamatan wajib diikuti pemeriksaan coverage dan diagnosis pada kecamatan yang dipilih sebelum rekomendasi. Penurunan omzet tidak dengan sendirinya membuktikan stok kosong, kalah kompetitor, atau sales kurang bekerja.

Contoh persist/follow-up dengan akun operator demo: `python -m examples.context_demo` (menulis grant fiktif, context dan audit pada database demo). Di API produksi, principal berasal autentikasi server dan grant diprovision admin; jangan mengekspos demo provisioning ini sebagai endpoint.

## Tes dan bukti verifikasi

`examples/csv_verification.json`, `examples/postgres_verification.json`, `examples/python_tests.txt`, dan `docs/VERIFICATION.md` mencatat pengujian yang benar-benar dijalankan. SQL diuji dengan PGlite (mesin PostgreSQL WASM) bila server lokal tidak tersedia; pengujian ini bukan bukti TLS cloud atau konkurensi dua koneksi. Tes integration server dan cara mengulang ada di `tests/test_postgres.py`. Jalankan pada database demo kosong/khusus, bukan produksi:

```sh
python -m unittest discover -s tests -v
```

Dengan `DATABASE_URL` terpasang dan dependency psycopg tersedia, tes PostgreSQL aktif dan menggunakan fixture rollback; **tes konkurensi membuat fixture permanen di database tes bernama telco_test_* saja**. Lihat persyaratannya pada file tes. Tanpa variabel itu tes server di-skip secara eksplisit.

## Navigasi

- [ERD dan kontrak tabel](docs/ERD.md)
- [Definisi KPI dan aturan diagnosis](docs/KPI_AND_ANALYSIS.md)
- [Runbook cloud](docs/CLOUD.md)
- [Kontrak API/tools dan follow-up](docs/TOOL_CONTRACTS.md)
- [Prompt lengkap Claude Code](docs/CLAUDE_CODE_PROMPT.md)
- [Bukti dan batas pengujian](docs/VERIFICATION.md)

Paket ini satu perusahaan (single tenant). Pembatasan akses wilayah dilakukan API yang tepercaya; belum ada isolasi multi-tenant/RLS. Jangan membuka PostgreSQL atau role reader ke pengguna, dashboard browser, n8n, ataupun agent. Master wilayah/outlet/produk diperlakukan immutable untuk demo; produksi perlu migrasi historisasi master bila pemetaan wilayah/produk berubah.

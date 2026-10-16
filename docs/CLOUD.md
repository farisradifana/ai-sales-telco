# Runbook cloud

## Database dan migrasi

1. Buat PostgreSQL terkelola versi 17 yang didukung provider, database `telco`; pilih region dekat operasional. Compose/CI dipin 17.11 sesuai [minor release resmi](https://www.postgresql.org/support/versioning/) yang tersedia saat penyusunan; perbarui patch sesuai kebijakan provider sebelum deployment. Mulai kapasitas kecil dan ukur hasil EXPLAIN/latency; jangan menganggap ukuran CPU/RAM tertentu sudah tervalidasi. Izinkan koneksi hanya dari job migrasi dan API private network, TLS `sslmode=verify-full` dengan CA provider. Pisahkan database n8n dari database bisnis; user/password dan backup terpisah.
2. Simpan URI owner/migrator di secret manager untuk job deploy. App tidak boleh memakai owner. Install requirements dalam virtual environment/image build, jalankan `python -m telco.db migrate`. Jangan edit migration terpasang; tambah file `004_...sql`. Snapshot sebelum perubahan; tidak disediakan destructive down migration. Rollback operasional dengan restore snapshot ke database baru atau forward fix teruji.
3. Salin CSV dan manifest ke job private (object storage terenkripsi atau image artefak terbatas). Jalankan `python -m telco.validate_csv data/full`, lalu `python -m telco.db load --data data/full`, dan `python -m telco.db validate`. COPY menggunakan koneksi client, tidak membutuhkan akses filesystem server/cloud superuser atau extension. Loader menahan satu transaksi besar untuk menjaga atomisitas demo; produksi gunakan staging validated + batch publish, bukan mengurangi integrity checks.
4. Provision `sql/security.sql` dengan akun admin bila diizinkan CREATEROLE. Buat LOGIN terpisah untuk read API dan write API menggunakan secret manager; grant group telco_reader / telco_writer sesuai tugas. API insight hanya SELECT/functions. Untuk follow-up persistence, gunakan koneksi writer terpisah yang tidak mengekspos arbitrary SQL. Atur `statement_timeout` (contoh 15s, ukur dahulu), `lock_timeout` (contoh 3s) dan connection pool kecil; retry transient 40001/40P01 dengan exponential backoff terbatas.
5. Jalankan tes dua koneksi di database disposable bernama `telco_test_*` menggunakan `tests/test_postgres.py`; jangan arahkan tes ke database bisnis. Buktikan scoped auth di API, backup restore, request dedup, SQL injection, timestamp WIB, missing ingestion, pemulihan worker, dan outbox retries sebelum koneksi WhatsApp nyata.

## Stok, koreksi dan ingestion produksi

Semua penulisan domain satu transaksi database. Insert sell-in/sell-out otomatis membuat ledger dan saldo; FK serta balance>=0 tetap aktif. Jalur standar mengunci account secara urut dan update saldo atomik; oversell serentak gagal constraint, bukan saldo minus. `external_key` unik per channel adalah dedup level transaksi. API menyimpan payload hash untuk membedakan retry identik (kembalikan hasil asal) dari key sama payload berbeda (409). Jika beberapa SKU ditulis bersamaan, lock semua account terlibat dalam urutan yang sama dan retry deadlock seluruh transaksi.

Persediaan pada saat ini tidak cukup membuktikan stok kronologis ketika posting backdate. API produksi wajib menolak transaksi backdate sebelum watermark/batas posting terakhir, atau menjalankan jalur koreksi khusus yang mengunci account, menyimulasikan seluruh historical running balance, menjalankan `validate.sql`, lalu commit. Loader demo memvalidasi histori sesudah load. DDL menjaga balance saat insert; **DDL saja tidak menolak setiap kasus historis backdate**. Ini batas yang harus ditangani service sebelum go-live.

Mutasi master melalui job admin. Penggantian penugasan harus merencanakan batas interval: assignment append-only membuat jadwal historis tidak bisa diam-diam ditimpa. Untuk koreksi jadwal yang sudah terpasang, migration terkontrol (termasuk validasi ulang transaksi), bukan UPDATE oleh agent. Perubahan kecamatan outlet memerlukan SCD/territory snapshot agar histori laporan tak bergeser.

Top-up / receipt eksternal memerlukan bukti sumber. Rekap spreadsheet adalah read/export atau import staging dengan dedup dan persetujuan; tidak boleh menimpa balance. Reversal penuh append-only mengurangi revenue pada tanggal reversal dan membalik stok. Refund saldo/operator memerlukan bukti penyelesaian dari sistem operator. Pajak/COGS/komisi bukan cakupan model; lakukan integrasi akuntansi terpisah.

## Observability dan pemulihan

Log correlation_id, event_id, context_id, SQL tool version, execution time, coverage dan evidence digest; redaksi message body dan token. Pantau error rate, queue age, outbox attempts, ingestion lag, constraint failures dan selisih ledger/balance. Watermark dimajukan hanya setelah batch lengkap dan validasi berhasil dalam publish transaction; MAX(tx_date) bukan watermark.

Aktifkan backup otomatis + PITR sesuai kemampuan provider; sasaran awal operasional RPO <=15 menit dan RTO <=2 jam adalah **target**, belum diuji. Lakukan restore drill, ulang validasi SQL, sample checksum, dan uji WhatsApp dedup sebelum menyatakan target terpenuhi. Gunakan database read replica hanya jika freshness watermark ikut direplikasi; beri label lag dan jangan menggabungkan evidence dari snapshot yang berbeda.

500.000 baris tidak memerlukan partitioning untuk demo. Views biasa menjaga kesederhanaan. Jika query lambat, ukur dengan `EXPLAIN (ANALYZE, BUFFERS)` di staging; pilih aggregate materialized harian dengan freshness/cutover atomik, bukan menambahkan index tanpa bukti. Tabel sudah memiliki index scope-time dan ledger account-product-time.

## Integrasi eksternal

Dokumentasi dicek 16 Oktober 2026; versi/image/commit integrasi harus dipin dan endpoint diuji ulang saat implementasi:

- [PostgreSQL explicit locking](https://www.postgresql.org/docs/17/explicit-locking.html) mendasari row locking dan retry service.
- [n8n queue mode](https://docs.n8n.io/hosting/scaling/queue-mode/) menjelaskan Redis, database dan pembagian worker; shared encryption key untuk worker adalah secret deployment. Pilih env webhook sesuai versi yang dipin.
- [Hermes Agent resmi](https://github.com/NousResearch/hermes-agent) dan [API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server): gunakan adapter dengan capability probe terhadap versi terpasang; tool access dibatasi ke API bisnis.
- [9Router repository](https://github.com/decolua/9router): asumsi proyek yang dimaksud adalah repository ini. Konfigurasikan upstream model yang berizin dengan kredensial sendiri; base URL/model/auth adapter configurable, tanpa hardcode route yang belum diuji.

Jika Hermes atau 9Router yang dimaksud merupakan proyek berbeda, ubah adapter setelah memeriksa kontrak proyek tersebut; database dan definisi KPI tetap sama. Paket ini tidak mengasumsikan lisensi, kuota, atau layanan model gratis.

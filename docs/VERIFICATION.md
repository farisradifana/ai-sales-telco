# Verifikasi dan keterbatasan

Pengujian lokal pada 16 Oktober 2026 menggunakan Python 3.12 dan Node.js 24; tidak ada PostgreSQL server atau Docker di host. Karena itu SQL benar-benar dieksekusi memakai **PGlite 0.3.14 (engine PostgreSQL 17.5 WASM)**, bukan sekadar di-lint atau diterjemahkan ke SQLite. Deployment reference PostgreSQL 17.11 belum dijalankan sebagai server di host ini. Pengujian SQLite di bawah adalah pemeriksaan independen tambahan atas CSV, bukan pengganti PostgreSQL.

| Pengujian | Hasil / artefak |
|---|---|
| Dua generator penuh seed42 | identik metadata dan 14 SHA-256 CSV; `examples/determinism_verification.json` |
| Dataset penuh | 100.000 sell-in + 400.000 sell-out, seluruh 21 bulan dan dua tanggal ujung; manifest |
| CSV validator independen | 58 cek lulus; relasi/hierarki, id/key, unit/harga/tanggal, ledger rekonstruksi dan stok kronologis; `examples/csv_verification.json` |
| Python unittest | 7 lulus; 2 tes server eksplisit skip tanpa DATABASE_URL; `examples/python_tests.txt` |
| PostgreSQL WASM dataset penuh | migrations, load, 700.000 ledger, audit stok/KPI, oversell, immutable events, assignment overlap, duplicate, PCS, reversal dan diagnosis; `examples/postgres_verification.json` |
| Final SQL smoke | hash seluruh file migration final, formula volume/harga fixture tepat, entry/exit, kontribusi outlet, batch lineage, ranking, coverage missing dan role reader/writer; `examples/final_sql_smoke.json` |
| Constraint/security tambahan | coverage sell-in/sell-out terpisah, scope default-deny, writer tidak bisa grant diri, reader tidak bisa ubah saldo; tercantum dalam laporan penuh |

SQL audit memastikan full receipt/movement tersedia, saldo cache=ledger, saldo kronologis tidak negatif, atribusi sumber sesuai dan KPI channel tidak terduplikasi. PGlite mendukung PL/pgSQL/triggers/constraints yang dipakai. Ia tidak membuktikan perilaku server multi-koneksi, cloud IAM, TLS, concurrent deployment locks, psycopg network COPY, n8n/Hermes/9Router/WhatsApp, atau backup restore. Dua tes integration server dan CI PostgreSQL 17 sudah disediakan; belum dijalankan di server cloud dalam sesi ini. Jangan menganggap skip sebagai lulus.

`npm ci --ignore-scripts && npm run verify:pg-wasm` mengulang audit SQL penuh dari file final. `npm run test:pg-smoke` memberi cek cepat file SQL final dan mencatat SHA-256-nya. Full test berlangsung beberapa menit tergantung host; statements COPY dibatasi 1.000 rows agar update hot balance tidak menumpuk versi row besar dalam WASM.

## Contoh hasil yang direproduksi

Perbandingan sell-out Juni 2026 (30 hari) dengan September 2026 (30 hari), bukan Agustus 31 hari:

- Outlet Fiktif OUT-0001: Rp7.332.900 → Rp2.159.800, delta −Rp5.173.100.
- Kecamatan Simulasi 01: Rp92.161.000 → Rp17.164.900, delta −Rp74.996.100.

Lihat JSON evidence, product drivers dan ranking di `examples/`. Angka ini keluaran data skenario seed42, bukan klaim pasar. Volume/harga/mix adalah rekonsiliasi aritmetika. Kunjungan yang berkurang hanya asosiasi; stockout, kompetisi, gangguan jaringan dan lost demand tidak dibuktikan. Recommendation JSON mengacu evidence yang sama; tidak mengeksekusi tindakan eksternal.

## Hal yang perlu diuji sebelum produksi

Jalankan CI server, authorization API dan follow-up, semantik idempotency retry vs payload berbeda, historical backdate policy, full role grants, multi-SKU deadlock retry, precision serialisasi uang, dashboard chart/totals, workflow n8n versi dipin, provider signature serta outbox crash scenarios. Infrastructure cloud belum dibangun oleh paket ini; prompt Claude Code mendefinisikan acceptance tests dan langkah deploy. Bukti verifikasi terpisah agar batas paket database dan implementasi infrastruktur jelas.

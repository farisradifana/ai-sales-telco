# Verifikasi dan keterbatasan (16 Oktober 2026)

Host: Windows 11, Python 3.12.1, Node 24.18, **PostgreSQL 17.11 server nyata** (binari resmi EDB, cluster disposable di
`127.0.0.1:55432`), n8n 2.35.7, Hermes Agent 0.20.5 (`14c59f0`). Tidak ada Docker/Redis di host ini. Semua database tes bernama
`telco_test_*`. Tidak ada pesan WhatsApp nyata dan tidak ada panggilan ke vendor model nyata.

| Pengujian | Hasil | Artefak |
|---|---|---|
| Generator v3 seed 42 dua kali ke folder kosong | identik; 100.000 sell-in + 400.000 sell-out; 938 posisi organisasi | `reports/determinism_seed42_v3.json`, `examples/manifest_seed42_v3.json` |
| Validator CSV independen (SQLite) | 72 cek lulus (relasi, kunci, unit, stok kronologis, nesting kode wilayah, hierarki organisasi) | `reports/csv_validation_v3.json` |
| Migrate 001–008 dua kali, load 500k, validate, load kedua | migrate idempoten; load 7–12 menit; `validate.sql` + `validate_platform.sql` OK; load kedua **ditolak** | `reports/build_template_jatim_v3.log` |
| Migration runner | checksum berubah ditolak; dua runner bersamaan pada DB kosong aman (lock diambil sebelum tabel bookkeeping — bug race diperbaiki) | `tests/test_migrations_pg.py` |
| Tes reference server (`tests/test_postgres.py`) | 2 lulus (oversell konkuren, isolasi/versi/TTL konteks) | `reports/pytest_reference_server.txt` |
| Suite platform pytest (clone disposable dari template 500k) | **78 lulus**, 2 skip (= tes reference di atas, dijalankan terpisah) | `reports/pytest_full.txt` |
| E2E proses nyata | 8/8 cek lulus: webhook bertanda tangan → API → n8n → API → **Hermes asli** → MCP → PG → outbox → mock Graph | `reports/e2e_local.json` |
| Backup/restore drill | `pg_dump -Fc` 4 dtk → restore ke DB baru 34 dtk → audit SQL lulus → sidik jari identik | `reports/restore_drill.json` |
| Hermes capability probe | 0 dari 28 toolset bawaan aktif; model hanya melihat 6 tool `mcp__telco__*` | `docs/AGENT_STACK.md` |
| Import workflow n8n | 8 workflow + credential stub ter-import, 5 di-publish dan aktif (webhook + schedule) | `n8n/README.md` |
| Dashboard | `tsc` strict + build lulus; inspeksi visual desktop & 375 px (tanpa scroll horizontal halaman), fokus keyboard terlihat, label lengkap, state loading/error/empty/provisional/stale | bagian di bawah |

Isi suite platform: otorisasi lintas principal (outlet/kecamatan/konteks/stok/target/evidence/tautan chart), grant dicabut berlaku
pada follow-up, tidak ada permukaan SQL & injeksi inert, nama ambigu → klarifikasi, growth null untuk baseline 0, periode tidak
setara ditolak, coverage kurang → provisional + aksi data-quality saja, oversell konkuren dua koneksi (diulang 3×), idempotensi
(replay vs 409), kebijakan harga/diskon, reversal mengembalikan stok & omzet, aturan backdate + jalur koreksi yang membuktikan
saldo kronologis, validitas assignment, immutability event, draft→konfirmasi→kedaluwarsa, follow-up mempertahankan konteks,
perubahan scope butuh izin ulang, TTL, konflik versi 409, recompute saat watermark berubah, chart/narasi = evidence, snapshot
dashboard tunggal dengan gap, export CSV ber-metadata, E2E WhatsApp in-process (duplikat, hash mismatch 409, signature invalid
tidak tersimpan, pengirim tak terdaftar tidak dibalas, out-of-order, crash sebelum/ sesudah kirim, rekonsiliasi status,
retry/dead-letter), fallback narasi (Hermes palsu/turun/toolset terlarang → router; tool/DB gagal → 503 tanpa angka), nudge
target sekali per hari, serta hierarki organisasi (cakupan tiap level, isolasi area, atasan, persetujuan hanya dalam area).

## Temuan yang diperbaiki selama pengujian

- Writer API tidak punya hak `SELECT … FOR UPDATE` → migration 007 fungsi kunci sempit (tanpa memberi UPDATE tabel).
- Waktu posting diambil sebelum lock → transaksi bersamaan/draft yang dikonfirmasi belakangan bisa dianggap backdate → waktu
  server diambil dari jam DB **setelah** lock.
- Race runner migration pertama kali (CREATE TABLE sebelum advisory lock) → urutan diperbaiki.
- Hermes 0.20.5: tool MCP default di balik `tool_search` → dimatikan agar katalog tetap; gerbang `trust: untrusted` membaca
  `readOnlyHint` camelCase sedangkan SDK mcp 2.0 memakai `read_only_hint` (bug upstream) → bridge first-party diberi
  `trust: full` (alasan dicatat di `deploy/hermes/render_home.py`).
- Dashboard: error jaringan dianggap logout; link hash tidak diterapkan → diperbaiki.

## Belum diuji / batas yang jujur

- **Tidak ada deploy cloud.** Compose staging, TLS (Caddy), private networking cloud, managed PostgreSQL, PITR/RPO/RTO belum
  dijalankan (butuh akun cloud, domain, secret).
- n8n **queue mode** (Redis + worker) belum dijalankan; E2E memakai mode reguler.
- Image Docker belum dibangun di host ini (tidak ada Docker); tag image sudah diverifikasi ada di Docker Hub.
- WhatsApp Cloud API nyata dan provider model nyata via 9Router belum diuji (butuh kredensial berizin dan nomor tujuan yang
  disetujui). 9Router hanya diverifikasi dari paket (versi, lisensi, CLI, rute); belum dijalankan sebagai proses di tes.
- Mock LLM bersifat skrip; kualitas narasi model sungguhan belum dievaluasi (verifikator angka tetap berlaku).
- Rate limiter per proses; multi-replica perlu limiter di gateway.
- Pembagian Head of Sales East/West dan parameter target adalah default yang perlu disetujui pemilik bisnis.
- Contoh di `examples/*_evidence.json` berasal dari dataset v1; angka level outlet identik di v3 (transaksi sama), angka level
  kecamatan berbeda karena wilayah sekarang nyata dan satu outlet per kecamatan.

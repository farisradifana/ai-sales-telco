# TELCO Sales AI — Jawa Timur (demo sintetis)

Platform penjualan sell-in/sell-out untuk sales lapangan dan manajemen: input penjualan via WhatsApp dengan konfirmasi,
stok yang dipegang, target harian, insight outlet/kecamatan berbasis bukti, follow-up "apa yang harus dilakukan?" dengan
konteks tetap, dashboard untuk keputusan, dan rekap spreadsheet. **Wilayah administrasi Jawa Timur nyata**
(Kepmendagri 300.2.2-2430/2025, 1 provinsi, 38 kabupaten/kota, 666 kecamatan); **outlet, pemilik, alamat, sales, transaksi
dan pelanggan fiktif**. Outlet tidak memiliki koordinat. Tidak ada nomor telepon nyata; ID WhatsApp demo diawali `0099`
(bukan E.164 yang valid). Tidak ada pesan WhatsApp nyata yang dikirim selama pengujian.

## Arsitektur

```
WhatsApp Cloud API ─► API ingress (/webhooks/whatsapp: HMAC raw body, skema, dedup, 409 hash mismatch)
                         │  satu commit: inbound_event + outbox(n8n_dispatch)        ▲ status webhook → rekonsiliasi outbox
                         ▼                                                            │
                  n8n 2.35.7 (queue mode: main/webhook/worker, Redis, PG terpisah) ──┤ 01 router → 02 sales / 03 insight / 04 follow-up
                         │  hanya provider+message_id                                 │ 05 target harian, 06 outbox dispatcher, 07 rekap, 08 import
                         ▼                                                            │
                  API domain (FastAPI) ── authz default-deny (principal_grant/can_access) ── PostgreSQL 17
                         │  ticket satu-turn                      ▲ tool read-only via ticket
                         ▼                                         │
                  Hermes Agent 0.20.5 ──► MCP bridge (6 tool bisnis, tanpa kredensial DB)
                         │
                         ▼
                  9Router 0.5.99 ──► provider model berizin (MODEL_ID dikonfigurasi pemilik akun)
Dashboard React/TS ─► API (satu evidence snapshot untuk semua chart)        Spreadsheet ◄─ export API (bukan sumber stok)
```

## Organisasi dan hak akses

| Level | Jumlah | Cakupan | Contoh nama | Peran API |
|---|---|---|---|---|
| Head of Region | 1 | Provinsi Jawa Timur | Head of Region Jawa Timur | area_manager |
| Head of Sales | 2 | 19 kab/kota masing-masing (`data/reference/jatim_sales_area.csv`) | Head of Sales East / West | area_manager |
| Head of Branch | 38 | 1 kab/kota | Head of Branch Madiun, Head of Branch Kota Madiun | supervisor |
| Territory Manager | 231 | ≤3 kecamatan dalam 1 kab/kota (urut kode) | Territory Manager Madiun Satu | supervisor |
| Sales | 666 | 1 kecamatan | Sales_Kebonsari (nama kecamatan ganda: Sales_Gondang_Nganjuk) | sales |

Hierarki di `telco.org_position` / `org_territory` (migration 008, trigger validasi level atasan & cakupan). Grant
`principal_grant` diturunkan dari cakupan (`v_org_coverage`) oleh `python -m telco_api.provision demo`; otorisasi tetap
default-deny dan **single tenant** (tidak ada RLS multi-tenant). Pembagian East/West adalah default geografis yang perlu
divalidasi pemilik bisnis; ubah CSV lalu generate ulang. Reversal/transfer disetujui supervisor dengan grant pada outlet
(four-eyes: pemohon tidak boleh menyetujui sendiri).

## Dataset

- 500.000 baris transaksi satu produk = 100.000 sell-in + 400.000 sell-out, 1 Jan 2025–30 Sep 2026 (WIB), seed 42,
  generator v3 deterministik (`examples/manifest_seed42_v3.json`). Ledger/supply/target/master bukan bagian dari 500k.
- 120 outlet tersebar di 38 kab/kota (masing-masing di kecamatan berbeda); tiap outlet dipegang sales kecamatannya.
- Saldo memakai satuan IDR, produk fisik PCS; keduanya tidak pernah dijumlahkan sebagai kuantitas.
- Target harian fiktif per outlet yang dipegang (sell-in Rp350.000, sell-out Rp400.000) — parameter uji, bukan benchmark.

## Menjalankan lokal

Prasyarat: Python 3.12, PostgreSQL 17 (lokal/Docker), Node 20+ untuk dashboard/n8n.

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install --require-hashes -r requirements-dev.lock
```

```bash
python -m telco.generate --out data/full --seed 42 --rows 500000
```

```bash
python -m telco.validate_csv data/full --report reports/csv_validation_v3.json
```

Database disposable lengkap (migrate → load → validate → security → role LOGIN → demo principal):

```bash
DB=telco_test_dev bash scripts/build_template.sh
```

API, dashboard dan stack mock: lihat `docs/RUNBOOK.md` (bagian 7). Login dashboard lokal memakai `/v1/auth/dev-login`
(hanya `APP_ENV=local|test`), contoh ID: `hob-35.19` (Head of Branch Madiun), `tm-35.19-01`, `sls-35.19.01`,
`hos-west`, `hor-35`.

## Pengujian

```bash
TEST_ADMIN_DATABASE_URL=postgresql://owner:…@127.0.0.1:55432/postgres TEST_TEMPLATE_DB=telco_test_jatim_v3 .venv/Scripts/python -m pytest tests -v
```

Tes server meng-clone template ke `telco_test_clone_*` dan hanya men-drop clone tersebut. Ringkasan hasil yang benar-benar
dijalankan ada di `docs/VERIFICATION.md` dan `reports/`.

## Navigasi

- [Runbook operasional, deploy, backup/restore, rollback](docs/RUNBOOK.md)
- [Hermes / 9Router / batas tool](docs/AGENT_STACK.md)
- [Workflow n8n dan binding credential](n8n/README.md)
- [Kontrak API/tools](docs/TOOL_CONTRACTS.md) · [KPI dan diagnosis](docs/KPI_AND_ANALYSIS.md) · [ERD](docs/ERD.md)
- [Bukti verifikasi dan batas](docs/VERIFICATION.md) · [Sumber data wilayah](data/reference/SOURCE.md)

## Prinsip yang ditegakkan

- KPI omzet selalu per channel; sell-in dan sell-out tidak dijumlahkan; growth `null` bila baseline ≤ 0.
- Periode default = bulan lengkap terakhir menurut watermark ingestion vs bulan sebelumnya dengan jumlah hari sama
  (contoh: Juni vs September 2026, 30 hari, akhir eksklusif).
- Penjualan posted immutable; koreksi = reversal penuh + pengganti; backdate ditolak kecuali jalur koreksi supervisor yang
  mengunci akun, membuktikan saldo kronologis ≥ 0 dan diaudit.
- Semua angka di jawaban berasal dari evidence tool; narasi model yang memuat angka di luar evidence dibuang.
- Hermes tidak punya shell/file/web/SQL/kredensial DB; hanya 6 tool bisnis read-only yang terikat ticket satu-turn.

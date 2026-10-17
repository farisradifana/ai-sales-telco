"""Blueprint content (single source for HTML, TXT and DOCX). Facts as built and verified on 17 Oktober 2026.
Blocks: ('p', text) | ('ul', [items]) | ('ol', [items]) | ('table', [headers], [[cells]]) | ('code', text) | ('h3', text) | ('note', text)
No secrets, tokens or phone numbers belong here."""

TITLE = 'AI Sales Intelligence Telco'
SUBTITLE = 'Blueprint sistem: database, roles, tools, langkah build, alur, dan hasil'
META = [('Versi dokumen', '1.0 · 17 Oktober 2026'), ('Repo', 'github.com/farisradifana/ai-sales-telco'),
        ('Wilayah', 'Provinsi Jawa Timur (38 kab/kota, 666 kecamatan; nama wilayah riil, orang & outlet fiktif)'),
        ('Kanal pengguna', 'WhatsApp Cloud API (nomor bisnis nyata) + dashboard web'),
        ('Status', 'Berjalan live di PC pemilik via Cloudflare named tunnel; CI GitHub hijau; 125 tes lulus')]

SECTIONS = [
 ('ringkasan', '1. Ringkasan', [
  ('p', 'AI Sales Intelligence Telco adalah asisten penjualan untuk tim distribusi produk telekomunikasi (perdana, voucher, saldo) '
        'di Jawa Timur. Sales dan manajer cukup chat di WhatsApp: mencatat sell-in ke outlet, melihat stok, target, achievement, '
        'peringkat outlet/kecamatan/kabupaten, atau bertanya bebas ("kenapa sell-out Kebonsari turun?") yang dijawab AI dengan angka '
        'yang diverifikasi terhadap database.'),
  ('table', ['Aspek', 'Ringkasan'], [
   ['Masalah', 'Data penjualan tersebar; manajer lapangan butuh angka cepat di HP tanpa membuka dashboard; jawaban AI generik rawan mengarang angka.'],
   ['Solusi', 'Satu sumber kebenaran di PostgreSQL + perintah cepat tanpa AI + AI Agent ber-tool yang angkanya wajib berasal dari tool dan diverifikasi.'],
   ['Pengguna', '938 posisi organisasi: 1 Head of Region, 2 Head of Sales (East/West), 38 Head of Branch, 231 Territory Manager, 666 Sales.'],
   ['Data', '500.000 transaksi sintetis (100.000 sell-in + 400.000 sell-out) 1 Jan 2025 – 30 Sep 2026, seed 42, deterministik.'],
   ['Keamanan', 'Default-deny per wilayah, role database least-privilege, nomor WA terenkripsi, tiket AI satu giliran, ledger immutable.'],
   ['Hasil', '125 tes otomatis lulus, CI hijau, uji live WhatsApp end-to-end, laporan < 1 detik, jawaban AI terverifikasi 14–26 detik.']]),
 ]),
 ('arsitektur', '2. Arsitektur & stack', [
  ('code', 'HP (WhatsApp) ──▶ Meta Cloud API ──▶ Cloudflare named tunnel (telco-wa.<domain>)\n'
           '      ▲                                      │\n'
           '      │                                 Edge lokal :8089 (hanya /webhooks/whatsapp, /charts/*)\n'
           '      │                                      ▼\n'
           '      │                     FastAPI :8000  ── verifikasi HMAC, dedup, identitas terenkripsi\n'
           '      │                          │  outbox (dispatch)            ▲ tools ber-tiket\n'
           '      │                          ▼                               │\n'
           '      │                     n8n :5688  ── router → perintah cepat / input sell-in / AI Agent\n'
           '      │                          │            AI Agent ── 9Router :20128 (combo ai-sales + ai-sales-backup)\n'
           '      │                          │            Memory  ── PostgreSQL schema agent_memory\n'
           '      │                          └── Google Sheets (tab Rekap + Info)\n'
           '      └──── outbox → WhatsApp sender (allowlist live) ◀── PostgreSQL 17 :55432 (telco, telco_identity, agent_memory)'),
  ('table', ['Komponen', 'Versi / teknologi', 'Peran'], [
   ['Database', 'PostgreSQL 17.11', 'Sumber kebenaran: master data, transaksi, ledger stok, evidence, antrean, kuota, memory AI'],
   ['API', 'Python 3.12, FastAPI 0.143, psycopg 3.2, Pydantic 2.14', 'Domain API, webhook WhatsApp, laporan, otorisasi, outbox, verifikasi angka'],
   ['Orkestrasi', 'n8n 2.35.7', 'Router pesan, lane perintah cepat, input sell-in + Google Sheet, AI Agent, outbox 20 dtk'],
   ['AI Agent', 'n8n AI Agent (Tools Agent) + OpenAI-compatible model', 'Tanya jawab bahasa natural dengan 5 tool ber-tiket, memory Postgres, fallback model'],
   ['Model gateway', '9Router 0.5.99, combo ai-sales + ai-sales-backup', 'Routing ke provider berizin (API key), fallback combo'],
   ['Agent alternatif', 'Hermes Agent 0.20.5 + MCP bridge (mcp 2.3)', 'Jalur narasi kedua (toolset bawaan dimatikan), dipakai fallback Python & tes'],
   ['Kanal', 'WhatsApp Cloud API (Graph v23.0)', 'Pesan teks, list message, reply button; webhook bertanda tangan'],
   ['Publik', 'Cloudflare named tunnel + edge lokal', 'URL webhook tetap; hanya 2 path yang diteruskan'],
   ['Dashboard', 'React 19, TypeScript 5.9, Vite 7', 'Analitik dan aksi supervisor (login JWT)'],
   ['Rekap', 'Google Sheets', 'Tab Rekap (baris sell-in) dan Info (log/metadata)'],
   ['Operasi', 'VBScript + Git Bash watchdog, GitHub Actions', 'Autostart saat logon, restart otomatis, CI PostgreSQL 17']]),
 ]),
 ('database', '3. Database', [
  ('p', 'Satu database PostgreSQL 17 dengan tiga schema. Migrasi berurutan, immutable, dengan checksum; runner mengambil advisory '
        'lock sebelum tabel bookkeeping sehingga aman dijalankan bersamaan.'),
  ('table', ['Schema', 'Isi', 'Akses'], [
   ['telco', 'Master data, transaksi, ledger, analitik, platform (evidence, outbox, kuota)', 'reader / writer / ops sesuai grant'],
   ['telco_identity', 'Nomor WhatsApp terenkripsi AES-256-GCM + blind index HMAC', 'Hanya role identity'],
   ['agent_memory', 'Riwayat chat AI Agent (6 pesan terakhir per akun, dihapus > 7 hari)', 'Hanya role memory n8n; API hanya boleh menghapus']]),
  ('h3', 'Migrasi'),
  ('table', ['File', 'Isi'], [
   ['001_core', 'Wilayah, sales, outlet, produk, assignment, stok, supply, sell-in/out, reversal, target, outlet_day, watermark, ledger + trigger'],
   ['002_analytics', 'View v_commercial_event, v_daily_sales, v_monthly_*, v_stock, v_target_achievement; fungsi diagnose'],
   ['003_reporting', 'View kalender, kuartalan, outlet aktif harian, omzet harian'],
   ['004_access', 'app_principal, principal_grant, can_access (default-deny)'],
   ['005_platform', 'evidence_snapshot, inbound_event, outbox, pending_action, posting_request, konteks, import, audit'],
   ['006_platform_ids', 'Sekuens/ID platform'],
   ['007_lock_helper', 'lock_stock_accounts (SECURITY DEFINER) agar writer bisa mengunci tanpa hak UPDATE'],
   ['008_org', 'org_position, org_territory, v_org_coverage, v_org_chain + guard hierarki'],
   ['009_area_scope', 'can_access_area, diagnose_area, rank_districts, rank_regencies (kab/kota & provinsi)'],
   ['010_chat_agent', 'chat_quota_policy, chat_quota_usage, schema agent_memory.chat_history']]),
  ('h3', 'Tabel utama dan volume data'),
  ('table', ['Kelompok', 'Tabel', 'Baris'], [
   ['Wilayah', 'province / regency / district', '1 / 38 / 666'],
   ['Organisasi', 'org_position (+ org_territory)', '938 posisi'],
   ['Master', 'sales / outlet / product / assignment', '666 / 120 / 6 / 120'],
   ['Transaksi', 'sell_in / sell_out / supply', '100.008 / 400.000 / 100.720'],
   ['Ledger', 'stock_ledger (append-only) + stock_balance', '700.736'],
   ['Target & operasional', 'sales_target / outlet_day', '175.200 / 76.560'],
   ['Akses', 'app_principal / principal_grant', '941 / 3.998'],
   ['Platform', 'evidence_snapshot, inbound_event, outbox, pending_action, posting_request, agent_turn, chat_quota_*', 'tumbuh per pemakaian']]),
  ('h3', 'Jaminan integritas'),
  ('ul', ['Ledger dan event immutable: trigger reject_mutation menolak UPDATE/DELETE; koreksi lewat reversal/correction yang diaudit.',
          'Stok tidak pernah negatif: CHECK quantity >= 0, kunci akun stok dalam satu transaksi, uji oversell konkuren dua koneksi.',
          'Idempotensi posting: posting_request (hash payload) — replay mengembalikan hasil lama, isi berbeda → 409.',
          'Backdate dibatasi watermark + masa tenggang; waktu posting diambil dari jam DB setelah lock.',
          'Uang sebagai NUMERIC/Decimal (tidak pernah float); sell-in dan sell-out tidak pernah dijumlah; PCS dan saldo IDR dipisah.',
          'Setiap angka yang ditampilkan berasal dari evidence_snapshot (immutable, ber-digest) milik principal yang meminta.',
          'validate.sql + validate_platform.sql: audit relasi, kunci, stok kronologis, leg transfer, posting tanpa transaksi, dll.']),
 ]),
 ('roles', '4. Roles', [
  ('h3', '4.1 Hierarki organisasi, cakupan, dan kuota'),
  ('table', ['Jabatan', 'Jumlah', 'Cakupan', 'Kuota chat AI/hari', 'Perintah cepat khusus'], [
   ['Head of Region', '1', 'Provinsi Jawa Timur', '20', 'peringkat kecamatan, peringkat kabupaten'],
   ['Head of Sales (East/West)', '2', '19 kab/kota masing-masing', '10', 'peringkat kecamatan, peringkat kabupaten'],
   ['Head of Branch', '38', '1 kab/kota', '5', 'peringkat kecamatan'],
   ['Territory Manager', '231', 'maks. 3 kecamatan dalam 1 kab/kota', '3', 'peringkat kecamatan'],
   ['Sales', '666', '1 kecamatan (Sales_<kecamatan>)', '3', 'stok saya, input sell-in, cara input'],
   ['Tanpa jabatan (admin/viewer)', '—', 'sesuai grant', '0', '—']]),
  ('p', 'Semua jabatan mendapat: Halo, menu, sell in (dengan progres tim dari akumulasi sales di bawahnya), sell out, '
        'peringkat outlet, list produk. Kuota hanya untuk chat AI; perintah cepat gratis. Kuota reset 00.00 WIB, '
        'retry n8n tidak dihitung dua kali, dan kuota dikembalikan bila model gagal sebelum tool terpanggil.'),
  ('h3', '4.2 Role aplikasi'),
  ('table', ['Role', 'Dipakai oleh', 'Hak'], [
   ['sales', 'Sales', 'Data kecamatan sendiri, stok sendiri, draft & konfirmasi sell-in'],
   ['supervisor', 'Territory Manager, Head of Branch, viewer', 'Analitik dalam cakupan, persetujuan aksi dalam area'],
   ['area_manager', 'Head of Sales, Head of Region', 'Analitik area luas (kab/kota, provinsi)'],
   ['admin', 'Operasional', 'Import CSV, aksi admin; tidak otomatis melihat semua wilayah']]),
  ('h3', '4.3 Role database (least privilege)'),
  ('table', ['Login role', 'Grup', 'Boleh'], [
   ['telco_api_reader', 'telco_reader', 'SELECT data analitik; tidak bisa membaca inbound_event/outbox/konteks'],
   ['telco_api_writer', 'telco_writer', 'Posting, evidence, outbox, kuota; prune memory AI; tidak pernah owner/DDL'],
   ['telco_api_identity', 'telco_identity_rw', 'Hanya schema telco_identity + baca app_principal'],
   ['telco_ops_login', 'telco_ops', 'Provisioning principal, grant, harga, target; tanpa DDL'],
   ['telco_agent_memory_login', 'telco_agent_memory', 'Hanya schema agent_memory (n8n memory); ditolak di schema telco/identity']]),
  ('h3', '4.4 Identitas layanan'),
  ('ul', ['n8n → API: Bearer N8N_SERVICE_TOKEN; API → n8n: header dispatch + tanda tangan HMAC.',
          'AI tools: service token + tiket satu giliran di header X-Agent-Ticket (model tidak pernah melihat tiket).',
          'Rekap Google Sheet: principal svc-recap (JWT 30 hari) + OAuth2 Google milik pemilik sheet.',
          'Principal tidak pernah dikirim di body request; selalu diturunkan server dari token/identitas terverifikasi.']),
 ]),
 ('tools', '5. Tools', [
  ('h3', '5.1 Perintah cepat WhatsApp (tanpa AI, tanpa kuota)'),
  ('table', ['Ketik / pilih', 'Balasan'], [
   ['Halo', 'Sapaan dengan nama jabatan (bold) + tombol Menu'],
   ['menu / tombol Menu', 'List message interaktif sesuai jabatan; menekan baris = mengetik perintahnya'],
   ['stok saya', 'Stok per produk yang dipegang (Sales)'],
   ['sell in', 'Target, realisasi, achievement, kekurangan, vs bulan lalu, per produk, progres tim (terendah dulu), progres hari ini'],
   ['sell out', 'Sell-out per produk (pcs, omzet, pertumbuhan)'],
   ['peringkat outlet', 'Top 5 sell-out & sell-in (+3 terbawah bila > 10 outlet)'],
   ['peringkat kecamatan', 'SO & SI + pertumbuhan vs 1/2/3 bulan lalu; top 10 + 5 terbawah (TM ke atas)'],
   ['peringkat kabupaten', 'Sama, per kab/kota (Head of Sales, Head of Region)'],
   ['list produk', 'SKU + harga sell-in/sell-out + diskon maks; legenda & cara input hanya untuk Sales'],
   ['OUT-0001 SIM-NOVA-8 5[, …]', 'Draft sell-in → ringkasan → balas YA <kode> / BATAL <kode>'],
   ['cara input', 'Panduan input sell-in'],
   ['MULAI / STOP TARGET', 'Opt-in / opt-out pengingat target harian']]),
  ('p', 'Setiap balasan perintah cepat membawa tombol "Pilih Perintah" (dalam satu pesan bila ≤ 1000 karakter; bila lebih, '
        'pesan kedua yang ditahan outbox sampai laporannya terkirim).'),
  ('h3', '5.2 Tool AI Agent (read-only, ber-tiket)'),
  ('table', ['Tool', 'Fungsi'], [
   ['resolve_entity', 'Cari outlet/kecamatan/kab-kota/provinsi di dalam cakupan pengguna (nama ambigu → klarifikasi)'],
   ['report', 'Laporan baku: sell_in, sell_out, rank_outlets, rank_districts, rank_regencies (bulan vs 1/2/3 bulan lalu)'],
   ['diagnose', 'Analisis perubahan omzet: kontribusi produk/outlet, volume vs harga/mix, kunjungan, saran'],
   ['stock', 'Stok milik sendiri atau satu outlet'],
   ['target', 'Realisasi vs target harian sales']]),
  ('h3', '5.3 API (FastAPI)'),
  ('table', ['Kelompok', 'Endpoint'], [
   ['Kesehatan', 'GET /health/live, GET /health/ready'],
   ['Auth', 'POST /v1/auth/dev-login (lokal), POST /v1/auth/logout, GET /v1/me, PUT /v1/me/notifications'],
   ['Analitik', 'POST /v1/tools/{diagnose, rank-districts, rank-regencies, stock, target, resolve-entity, dashboard}'],
   ['Evidence & grafik', 'GET /v1/evidence/{id}, GET /v1/evidence/{id}/chart.svg, GET /charts/{id}.svg (tautan bertanda tangan)'],
   ['Konteks', 'POST /v1/contexts, GET /v1/contexts/{id}, POST /v1/contexts/{id}/follow-up'],
   ['Transaksi', 'POST /v1/transactions/drafts (+confirm/cancel), sell-in, sell-out, /v1/reversals, /v1/transfers, /v1/corrections/backdated-sale'],
   ['Aksi', 'GET /v1/actions, POST /v1/actions/{id}/decision'],
   ['Export / import', 'GET /v1/exports/sales.csv, POST /v1/imports, GET /v1/imports/{id}, POST /v1/imports/{id}/confirm'],
   ['WhatsApp', 'GET /webhooks/whatsapp (verifikasi), POST /webhooks/whatsapp (pesan & status)'],
   ['Internal n8n', 'POST /internal/v1/events/{claim, intent, act, narrate, reply, complete, fail}, /outbox/dispatch, /outbox/stats, /targets/nudges'],
   ['Internal AI', 'POST /internal/v1/agent/start, /agent/finish, GET|POST /internal/v1/agent-tools/{name}']]),
  ('h3', '5.4 Workflow n8n (satu kanvas, lane berwarna)'),
  ('table', ['Lane', 'Isi', 'Status'], [
   ['01 Router', 'Webhook internal → klaim → kenali niat → arahkan', 'aktif'],
   ['02 Input sell-in', 'Draft → YA/BATAL → posting → tambah baris ke tab Rekap + log ke tab Info', 'aktif'],
   ['03 AI Agent', 'Agent start (kuota+tiket) → AI Agent (model combo, cadangan, memory, 5 tool) → Agent finish (verifikasi) → balas', 'aktif'],
   ['01b Perintah cepat', 'menu, laporan, list produk, opt-in/out', 'aktif'],
   ['05 Pengingat target', 'Jam kerja WIB, hanya opt-in, butuh template Meta', 'nonaktif'],
   ['06 Outbox', 'Tiap 20 dtk: SKIP LOCKED + lease, retry + jitter, dead-letter', 'aktif'],
   ['07 Rekap Google Sheet', 'Snapshot 30 hari; jadwal 06:30 dinonaktifkan, tersedia tombol "Jalankan sekarang"', 'manual'],
   ['08 Import CSV', 'Upload admin → staging → validasi → konfirmasi', 'nonaktif']]),
  ('h3', '5.5 Skrip & alat bantu'),
  ('ul', ['telco.generate (dataset seed 42), telco.validate_csv (72 cek), telco.db migrate/load/validate.',
          'telco_api.provision (roles, demo principals & grants, recap-bot, mint-jwt).',
          'scripts/wa_live_setup.py (init, check, register-number, subscribe, register penerima terenkripsi).',
          'n8n/build_workflows.py (generator workflow, --model-id, --fallback-model-id, --sheets-cred-id, --enable).',
          'scripts/windows/telco-startup.vbs + telco_startup.sh (autostart + watchdog), telco_stop.sh.',
          'scripts/restore_drill.sh, scripts/e2e_local.py, scripts/edge_local.py.']),
 ]),
 ('steps', '6. Langkah build (dari nol)', [
  ('ol', ['Siapkan PostgreSQL 17 dan secret lokal (scripts/dev_secrets.py → .local/dev.env, tidak masuk git).',
          'Ambil data wilayah Jawa Timur (scripts/extract_jatim.py) lalu generate dataset: python -m telco.generate --out data/full --seed 42.',
          'Validasi CSV independen: python -m telco.validate_csv data/full (72 cek).',
          'Buat database, jalankan migrasi dua kali (idempoten), load 500k baris, validate; load kedua harus ditolak.',
          'Terapkan sql/security.sql + sql/security_platform.sql, lalu python -m telco_api.provision roles dan demo (principal & grant dari hierarki).',
          'Jalankan tes: pytest (clone disposable dari template) → 125 lulus.',
          'Setup WhatsApp Cloud API: app Meta, nomor bisnis, token, verify token, subscribe WABA; daftarkan penerima uji (terenkripsi + allowlist).',
          'Buat combo di 9Router (ai-sales + ai-sales-backup) dengan provider berizin ber-API key.',
          'Generate workflow n8n, buat credential (service token, dispatch header, recap JWT, Google Sheets OAuth2, 9Router, memory Postgres), import & publish.',
          'Siapkan domain di Cloudflare, buat named tunnel telco-wa → edge :8089, set Callback URL di Meta sekali.',
          'Pasang autostart: salin telco-startup.vbs ke folder Startup; watchdog menyalakan ulang layanan yang mati.',
          'Uji end-to-end dari HP: Halo → Menu → laporan → input sell-in → YA → cek Google Sheet.']),
 ]),
 ('flow', '7. Alur', [
  ('h3', '7.1 Pesan masuk (semua jenis)'),
  ('ol', ['Meta mengirim webhook → tunnel → edge → API memverifikasi tanda tangan HMAC raw body.',
          'Nomor pengirim di-hash (blind index) → dicocokkan ke principal; nomor asli tidak pernah disimpan polos.',
          'inbound_event disimpan (dedup per message_id; isi berbeda dengan id sama → 409); pengirim tak terdaftar tidak dibalas.',
          'Outbox mengirim dispatch ke n8n; n8n klaim event (lease) lalu API mengenali niat.',
          'Router: perintah cepat → lane 01b; input sell-in / YA / BATAL → lane 02; selain itu → AI Agent (lane 03).']),
  ('h3', '7.2 Perintah cepat'),
  ('ol', ['API menjalankan laporan deterministik dalam cakupan grant principal (default-deny) dan menyimpan evidence.',
          'Teks disusun untuk layar HP; balasan + tombol "Pilih Perintah" masuk outbox (reply_to = nomor pengirim).',
          'Lane outbox mengirim via WhatsApp Cloud API; mode live hanya ke nomor di allowlist.']),
  ('h3', '7.3 Input sell-in'),
  ('ol', ['"OUT-0061 SIM-NOVA-18 2, VCR-NOVA-3 10" → draft (cek outlet dalam cakupan, harga dari price_policy, stok cukup).',
          'Ringkasan + "Balas *YA kode* / *BATAL kode*", berlaku 30 menit.',
          'YA → posting atomik: kunci akun stok, ledger dua sisi, idempotensi; balasan "Tercatat" dengan total rupiah.',
          'n8n menambahkan baris ke tab Rekap dan satu baris log ke tab Info (gagal sheet tidak menghambat balasan).']),
  ('h3', '7.4 AI Agent'),
  ('ol', ['Agent start: cek & potong kuota harian per jabatan (advisory lock), buat tiket satu giliran (TTL 3 menit, maks 8 panggilan), system prompt sesuai jabatan.',
          'AI Agent n8n: model combo ai-sales (cadangan ai-sales-backup bila gagal), memory 6 pesan, tool HTTP dengan tiket di header.',
          'Setiap tool dicek ulang oleh API terhadap principal pemilik tiket dan disimpan sebagai evidence giliran itu.',
          'Agent finish: hapus ID teknis, ubah Markdown ke format WhatsApp, verifikasi setiap angka terhadap evidence giliran ini.',
          'Lolos → kirim jawaban (+ tautan grafik sell-out & sell-in bertanda tangan untuk diagnosis). Gagal → laporan baku dari evidence; tanpa tool → permintaan maaf + kuota dikembalikan.']),
  ('h3', '7.5 Outbox & pengiriman'),
  ('ul', ['Klaim SKIP LOCKED + lease, kirim tanpa transaksi terbuka, ack/fail; retry bertahap dengan jitter, dead-letter.',
          'Status Meta (sent/delivered/read) direkonsiliasi lewat biz_opaque_callback_data.',
          'Pesan lanjutan (Pilih Perintah) ditahan sampai pesan induknya terkirim.']),
 ]),
 ('keamanan', '8. Keamanan & tata kelola', [
  ('ul', ['Otorisasi default-deny di SQL (can_access / can_access_area); data di luar cakupan tidak pernah keluar dari database.',
          'Tidak ada permukaan SQL bebas; teks pengguna tidak pernah menjadi query; injeksi di nama outlet/pesan inert.',
          'Model AI tidak bisa menulis transaksi; tool read-only; tiket terikat principal, allowlist tool, batas panggilan, TTL.',
          'Verifikator angka menolak angka yang tidak ada di evidence giliran itu (termasuk angka dari memory chat).',
          'Nomor WhatsApp: AES-256-GCM + HMAC; log hanya berisi ID korelasi; allowlist untuk mode live.',
          'Secret di .local/ (gitignored) dan secret manager; tidak ada secret di repo, workflow, atau dokumen.',
          'Retensi: memory AI 7 hari, eksekusi n8n 24 jam, tautan grafik bertanda tangan berumur pendek.',
          'Kebijakan privasi: docs/PRIVACY.md (kontak resmi tercantum).']),
 ]),
 ('hasil', '9. Hasil & verifikasi', [
  ('table', ['Uji', 'Hasil'], [
   ['Tes otomatis (PostgreSQL 17 nyata, clone dari template 500k)', '125 lulus, 2 skip (tes reference dijalankan terpisah)'],
   ['CI GitHub Actions', 'Hijau: generate → load 500k → migrate/security/provision → tes → build dashboard'],
   ['Dataset seed 42 dua kali', 'Identik; validator 72 cek lulus'],
   ['Backup/restore drill', 'pg_dump 4 dtk → restore 34 dtk → audit lulus → sidik jari identik'],
   ['Live WhatsApp', 'Halo → Menu → pilih laporan → input sell-in → YA → Google Sheet terisi, semua dari HP nyata'],
   ['Kecepatan', 'Laporan perintah cepat < 1 dtk di API (± 6–12 dtk end-to-end WA); jawaban AI terverifikasi 14–26 dtk'],
   ['Fallback model', 'Model utama sengaja dibuat gagal → jawaban tetap terverifikasi lewat combo cadangan'],
   ['Ketahanan', 'Watchdog menghidupkan ulang API yang dimatikan dalam 18 dtk; URL webhook tetap (named tunnel)']]),
  ('h3', 'Contoh hasil'),
  ('code', '*Sell-in Head of Branch Madiun*\nPeriode: September 2026 (data lengkap s.d. 30 Sep 2026)\n\n'
           '🎯 Target: Rp31.500.000\n✅ Realisasi: Rp25.493.600\n📈 Achievement: *80,9%* (kurang Rp6.006.400)\nVs Agustus 2026: −22,3%\n\n'
           '*Progres tim (3), terendah dulu:*\n- Territory Manager Madiun Satu: 65,2% (Rp6,8 jt / Rp10,5 jt)\n'
           '- Territory Manager Madiun Dua: 68,4% (Rp7,2 jt / Rp10,5 jt)\n- Territory Manager Madiun Empat: 109,2% (Rp11,5 jt / Rp10,5 jt)'),
  ('h3', 'Bug nyata yang ditemukan & diperbaiki saat pengujian'),
  ('ul', ['Race migrasi pertama kali → advisory lock sebelum tabel bookkeeping.',
          'Waktu posting diambil sebelum lock → transaksi bersamaan dianggap backdate → jam DB setelah lock.',
          'Balasan dikirim ke nomor terbaru akun (nomor demo) → balasan kini ke nomor pengirim.',
          '"Bulan lalu" dibandingkan dengan Juni (aturan periode sama panjang) → tool laporan bulanan untuk AI.',
          'AI menyalin angka dari memory → aturan wajib tool per giliran + verifikator menolak.',
          'Pesan menu bisa mendahului laporan saat retry → outbox menahan pesan lanjutan.']),
 ]),
 ('batasan', '10. Batasan & langkah berikutnya', [
  ('ul', ['Saat ini berjalan di PC pemilik (autostart + watchdog); untuk produksi: deploy compose staging ke cloud dengan PostgreSQL terkelola, Redis + n8n queue mode.',
          'Data organisasi, outlet, dan transaksi sintetis; wilayah riil Jawa Timur.',
          'Pengingat target harian butuh template WhatsApp Utility yang disetujui Meta.',
          'Login dashboard produksi perlu IdP perusahaan (JWKS).']),
 ]),
]

# Workflow n8n (versi dipin 2.35.7)

File di `workflows/` dihasilkan oleh `python n8n/build_workflows.py` (jangan edit manual; ubah generator lalu generate ulang).
Tidak ada credential inline—hanya **referensi ID credential** yang stabil. Tidak ada node SQL; teks pengguna tidak pernah
diinterpolasi ke query (n8n hanya mengirim `provider` + `message_id` ke API).

| File | Fungsi |
|---|---|
| 01_inbound_router | Webhook internal `POST /webhook/telco-inbound` (Header Auth) dari dispatcher API → ACK 202 → claim → intent → routing |
| 02_sales_input | act (draft/konfirmasi/batal) → reply → complete |
| 03_ai_agent | **AI Agent n8n** untuk pertanyaan bahasa natural: Agent start (kuota + tiket) → AI Agent (model combo 9Router, Postgres Chat Memory, 6 HTTP tool) → Agent finish (verifikasi angka) → reply → complete |
| 05_daily_target | Cron WIB 07–20 Senin–Sabtu → `/internal/v1/targets/nudges` (opt-in, jam, hari kerja, idempotent per hari) |
| 06_outbox_dispatcher | Tiap 20 detik → `/internal/v1/outbox/dispatch` (SKIP LOCKED, lease, retry+jitter, dead-letter) |
| 07_spreadsheet_recap | 06:30 WIB → export CSV **sell-in** ter-scope (Provinsi Jawa Timur, 30 hari) → Google Sheet `1XAlDtkIA5gkdodi-fZip5x7Lbyri-YRk4dRJLOHPwRg` tab `Rekap` + `Info` |
| 08_import_staging | Upload CSV admin → `/v1/imports` (hash, validasi, preview). Posting hanya setelah konfirmasi admin |

Validasi signature WhatsApp (raw body HMAC), dedup dan penolakan signature invalid terjadi di **ingress API**
`/webhooks/whatsapp` sebelum apa pun masuk antrean; n8n hanya menerima event yang sudah terverifikasi.

## Credential yang harus dibuat (ID harus sama persis)

| ID | Tipe | Isi |
|---|---|---|
| `telcoApiService` | Header Auth | `Authorization` = `Bearer <N8N_SERVICE_TOKEN>` |
| `telcoDispatchHeader` | Header Auth | `X-Telco-Dispatch-Token` = `<N8N_DISPATCH_SECRET>` |
| `telcoRecapJwt` | Header Auth | `Authorization` = `Bearer <JWT principal rekap>` (principal supervisor khusus rekap, grant minimal) |
| `telcoAdminJwt` | Header Auth | `Authorization` = `Bearer <JWT admin>` |
| `googleSheetsRecap` | Google Sheets OAuth2 | dibuat & di-authorize pemilik spreadsheet di UI n8n |
| `telcoRouterOpenAi` | OpenAI | API Key = `ROUTER_API_KEY` 9Router, Base URL = `http://<9router>:20128/v1` (model/combo dari `--model-id`, default `ai-sales`) |
| `telcoAgentMemory` | Postgres | user `telco_agent_memory_login` / `TELCO_AGENT_MEMORY_PASSWORD`, DB telco; role ini hanya bisa menyentuh schema `agent_memory` |

Buat file JSON credential di luar repo (contoh bentuk: `[{"id":"telcoApiService","name":"TELCO API service token",
"type":"httpHeaderAuth","data":{"name":"Authorization","value":"Bearer …"}}]`) lalu:

```bash
n8n import:credentials --input=/secure/path/creds.json
```

```bash
n8n import:workflow --separate --input=n8n/workflows
```

```bash
n8n publish:workflow --id=telco01inbound001
```

Ulangi `publish:workflow` untuk `telco02salesinput`, `telco03aiagent0001`, `telco05dailytarget`,
`telco06outbox00001` (07/08 dipublish setelah credential Google/JWT di-bind). Base URL API default `http://api:8000`; untuk
lingkungan lain generate ulang: `python n8n/build_workflows.py --api-base https://api.internal --out /tmp/wf`.

Smoke test import yang dijalankan (n8n 2.35.7, SQLite terisolasi): `Successfully imported 4 credentials`,
`Successfully imported 8 workflows` (sebelum 03/04 digabung menjadi 03_ai_agent), `list:workflow` menampilkan 8 ID di atas.

## AI Agent (workflow 03) dan kuota chat

Router mengirim ke AI Agent **semua pesan selain** perintah cepat berikut (deterministik, tanpa model, tanpa kuota):

| Ketik | Isi balasan | Jabatan |
|---|---|---|
| `Halo` | sapaan + tombol **Menu** (pesan interaktif) | semua |
| `menu` (atau tekan tombol Menu) | daftar pilihan interaktif (list message) sesuai jabatan; menekan baris = mengetik perintahnya | semua |
| `cara input` | panduan input sell-in | Sales |
| `stok saya` | stok per produk yang dipegang | Sales |
| `OUT-0001 SIM-NOVA-8 5` (beberapa produk dipisah koma; jumlah kosong = 1) | draft **sell-in** → balas `YA <kode>` / `BATAL <kode>`; setelah YA baris transaksi langsung ditambahkan ke tab **Rekap** dan log ke tab **Info** (snapshot 06:30 menulis ulang kedua tab) | Sales |
| `sell in` | target, realisasi, achievement, per produk, vs bulan lalu, progres tim (bawahan langsung, terendah dulu), progres hari ini | semua |
| `sell out` | sell-out per produk (pcs, omzet, pertumbuhan) | semua |
| `list produk` | SKU + harga sell-in / sell-out + diskon maks; panduan baca & cara input hanya untuk Sales | semua |
| `peringkat outlet` | top 5 sell-out & sell-in (+3 terbawah bila >10 outlet) | semua |
| `peringkat kecamatan` | SO & SI + pertumbuhan vs 1/2/3 bulan lalu; top 10 + 5 terbawah | TM, HoB, HoS, HoR |
| `peringkat kabupaten` | sama, per kabupaten/kota | HoS, HoR |
| `MULAI/STOP TARGET` | pengingat target harian | semua |

Setiap balasan perintah cepat membawa tombol **Pilih Perintah** (dalam satu pesan bila ≤1000 karakter, selain itu pesan kedua yang selalu menunggu laporannya terkirim).
Periode laporan = bulan data lengkap terakhir (watermark); bulan penuh bila watermark di akhir bulan, selain itu month-to-date
dengan pembanding rentang hari yang sama. Input lewat WhatsApp selalu **sell-in**; `SELLOUT …` ditolak dengan petunjuk.
Kalimat bebas (mis. "outlet mana yang sell in nya paling bagus?") → AI Agent, yang juga punya tool `report` dengan laporan yang sama.

| Node | Isi |
|---|---|
| Agent start | `POST /internal/v1/agent/start`: cek & potong kuota harian, buat tiket satu-giliran (TTL 3 menit, maks 8 panggilan tool), system prompt sesuai jabatan |
| AI Agent | Tools Agent; prompt = pesan pengguna, system message dari API, maks 8 iterasi; error model → lanjut ke finish (`failed`) |
| Model combo (9Router) | OpenAI Chat Model, Responses API **off**, model `ai-sales` (combo 9Router) |
| Model cadangan (9Router) | input fallback AI Agent (**Enable Fallback Model**): combo `ai-sales-backup`, dipakai otomatis bila model utama gagal (`--fallback-model-id`, kosong = tanpa cadangan) |
| Memory (Postgres) | `agent_memory.chat_history`, session `wa:<principal_id>` (bukan nomor HP), 6 pesan terakhir, dihapus setelah 7 hari |
| Tool resolve_entity / diagnose / stock / target / report | HTTP Request Tool → `/internal/v1/agent-tools/<nama>`; tiket dikirim n8n di header `X-Agent-Ticket` (model tidak pernah melihatnya); error API dikembalikan ke model sebagai JSON |
| Agent finish | `POST /internal/v1/agent/finish`: hapus ID teknis, ubah Markdown ke format WhatsApp, **setiap angka harus ada di hasil tool giliran ini** (kalau tidak → jawaban template dari data tool), tautan grafik untuk diagnosis |

Kuota (tabel `telco.chat_quota_policy`, reset 00.00 WIB, 1 pesan = 1 kesempatan, retry n8n tidak dihitung dua kali):

| Jabatan | Chat AI / hari |
|---|---|
| Sales, Territory Manager | 3 |
| Head of Branch | 5 |
| Head of Sales | 10 |
| Head of Region | 20 |
| Tanpa jabatan (admin/viewer) | 0 |

Kuota habis → model tidak dipanggil; balasan berisi daftar perintah cepat sesuai jabatan (tetap gratis). Bila model gagal dan belum ada tool yang terpanggil, kuota dikembalikan.
Ubah batas (role ops): `UPDATE telco.chat_quota_policy SET daily_limit=5,updated_at=now() WHERE level='sales';`

## Queue mode

`deploy/compose.staging.yaml`: `n8n` (main), `n8n-webhook`, `n8n-worker`, PostgreSQL terpisah `n8n-db`, Redis privat ber-password,
`N8N_ENCRYPTION_KEY` identik di ketiganya, eksekusi sukses disimpan lalu dipangkas setelah 24 jam (`EXECUTIONS_DATA_MAX_AGE=24`). Setelan `none` tidak dipakai: di 2.35.7 eksekusi workflow multi-trigger tertahan berstatus *running*. Data eksekusi hanya berisi ID pesan, narasi, dan tautan bertanda tangan; tanpa nomor telepon atau token. Uji E2E lokal di host
pengembang berjalan dalam mode reguler karena Redis/Docker tidak tersedia; queue mode belum diuji di sesi ini.

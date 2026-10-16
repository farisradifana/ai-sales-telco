# Workflow n8n (versi dipin 2.35.7)

File di `workflows/` dihasilkan oleh `python n8n/build_workflows.py` (jangan edit manual; ubah generator lalu generate ulang).
Tidak ada credential inline—hanya **referensi ID credential** yang stabil. Tidak ada node SQL; teks pengguna tidak pernah
diinterpolasi ke query (n8n hanya mengirim `provider` + `message_id` ke API).

| File | Fungsi |
|---|---|
| 01_inbound_router | Webhook internal `POST /webhook/telco-inbound` (Header Auth) dari dispatcher API → ACK 202 → claim → intent → routing |
| 02_sales_input | act (draft/konfirmasi/batal) → reply → complete |
| 03_insight | act (tool terotorisasi) → narrate (Hermes) → reply (+tautan chart bertanda tangan) → complete |
| 04_follow_up | act (konteks tersimpan, reauthorize, recompute bila watermark berubah) → reply → complete |
| 05_daily_target | Cron WIB 07–20 Senin–Sabtu → `/internal/v1/targets/nudges` (opt-in, jam, hari kerja, idempotent per hari) |
| 06_outbox_dispatcher | Tiap 20 detik → `/internal/v1/outbox/dispatch` (SKIP LOCKED, lease, retry+jitter, dead-letter) |
| 07_spreadsheet_recap | 06:30 WIB → export CSV ter-scope → Google Sheet `1XAlDtkIA5gkdodi-fZip5x7Lbyri-YRk4dRJLOHPwRg` tab `Rekap` |
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

Ulangi `publish:workflow` untuk `telco02salesinput`, `telco03insight0001`, `telco04followup01`, `telco05dailytarget`,
`telco06outbox00001` (07/08 dipublish setelah credential Google/JWT di-bind). Base URL API default `http://api:8000`; untuk
lingkungan lain generate ulang: `python n8n/build_workflows.py --api-base https://api.internal --out /tmp/wf`.

Smoke test import yang dijalankan (n8n 2.35.7, SQLite terisolasi): `Successfully imported 4 credentials`,
`Successfully imported 8 workflows`, `list:workflow` menampilkan 8 ID di atas.

## Queue mode

`deploy/compose.staging.yaml`: `n8n` (main), `n8n-webhook`, `n8n-worker`, PostgreSQL terpisah `n8n-db`, Redis privat ber-password,
`N8N_ENCRYPTION_KEY` identik di ketiganya, `EXECUTIONS_DATA_SAVE_ON_SUCCESS=none` (minimasi PII). Uji E2E lokal di host
pengembang berjalan dalam mode reguler karena Redis/Docker tidak tersedia; queue mode belum diuji di sesi ini.

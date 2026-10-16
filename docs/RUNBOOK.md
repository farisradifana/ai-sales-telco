# Runbook operasional

Semua perintah dari root repository. Data demo: wilayah administrasi Jawa Timur nyata (Kepmendagri 2025), sedangkan outlet,
sales, transaksi dan pelanggan fiktif. Jangan mengarahkan perintah ke database bisnis nyata kecuali disebut eksplisit.

## 1. Konfigurasi dan secret

1. Salin `.env.example` → `deploy/.env.staging` (di luar git; `.gitignore` sudah mengecualikan `.env*` lokal). Isi dari secret
   manager cloud. Generate nilai acak awal: `python scripts/dev_secrets.py deploy/.env.staging.generated` lalu salin ke secret manager.
2. Secret yang **memblokir deploy nyata** dan harus disediakan pemilik akun: hostname publik + DNS, akses cloud/registry,
   WhatsApp Cloud API (`WA_APP_SECRET`, `WA_VERIFY_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_ACCESS_TOKEN`), API key provider model berizin
   untuk 9Router + `MODEL_ID`, IdP perusahaan (`JWT_JWKS_URL`) untuk login dashboard produksi, credential Google untuk rekap.
3. `N8N_ENCRYPTION_KEY` harus identik di n8n main/webhook/worker dan tidak boleh diganti setelah credential tersimpan.
4. `WA_SEND_MODE`: `disabled` (default, tidak mengirim), `mock` (hanya host mock lokal), `live` (hanya ke penerima di
   `WA_LIVE_ALLOWLIST_HMACS`). Jangan pernah mengaktifkan `live` untuk demo ke nomor sungguhan tanpa persetujuan pemiliknya.

## 2. Database (PostgreSQL 17)

```bash
DATABASE_URL="$OWNER_URL" python -m telco.db migrate
```

Urutan instalasi pertama (database kosong khusus): `migrate` → `python -m telco.validate_csv data/full` →
`DATABASE_URL=… python -m telco.db load --data data/full` → `… validate` → `psql -f sql/security.sql` →
`psql -f sql/security_platform.sql` → `psql -f sql/validate_platform.sql` → `python -m telco_api.provision roles`
(ADMIN_DATABASE_URL, password LOGIN dari env) → `python -m telco_api.provision demo` (hanya lingkungan demo).
Migration sudah terpasang **immutable**: perubahan selalu file baru `00N_*.sql`; runner menolak checksum berubah dan
memakai advisory lock sebelum menyentuh tabel bookkeeping. `security*.sql` boleh dijalankan ulang (idempoten) setelah
migration baru agar grant tabel/sequence/fungsi baru ikut diberikan.

Cloud managed PostgreSQL: versi 17 minor terbaru dari provider, private networking, `sslmode=verify-full` dengan CA provider,
tanpa extension tambahan (skema tidak memerlukan extension). User API **bukan** owner: `telco_api_reader`, `telco_api_writer`,
`telco_api_identity`, `telco_ops_login` (tanpa CREATE/SUPERUSER/CREATEROLE).

## 3. Build dan menjalankan (staging, provider-neutral)

```bash
docker compose -f deploy/compose.staging.yaml --env-file deploy/.env.staging build
```

```bash
docker compose -f deploy/compose.staging.yaml --env-file deploy/.env.staging --profile jobs run --rm migrate
```

```bash
docker compose -f deploy/compose.staging.yaml --env-file deploy/.env.staging up -d
```

Lalu import + publish workflow n8n (lihat `n8n/README.md`), konfigurasi provider berizin di dashboard 9Router (jaringan privat;
akses lewat port-forward admin, bukan publik), dan daftarkan URL webhook `https://<PUBLIC_HOSTNAME>/webhooks/whatsapp` +
verify token di Meta App. Image dipin: postgres 17.11, redis 8.2.10-alpine, n8nio/n8n 2.35.7, python 3.12.15-slim,
node 24.22.0-trixie-slim, nginx 1.29.8-alpine, caddy 2.10.2-alpine, Hermes dari commit `14c59f0`.
Container-ready untuk Cloud Run / ECS / Container Apps / Kubernetes: API stateless (port 8000), MCP bridge (8765), Hermes
(8642, volume HERMES_HOME), n8n main/webhook/worker, dashboard (8080). Gunakan managed PostgreSQL & Redis privat.

## 4. Healthcheck dan readiness

- `GET /health/live` — proses hidup (dipakai Docker HEALTHCHECK dan uptime eksternal).
- `GET /health/ready` — koneksi reader/writer DB + status Hermes (capability probe, circuit) + mode kirim WhatsApp.
  Mengembalikan 503 bila DB tidak siap. Tidak memuat secret.
- Hermes: `GET :8642/health`; MCP bridge: `GET :8765/health`; n8n: `/healthz` (queue health aktif).

## 5. Observability

- Setiap respons membawa `X-Correlation-Id`; log API hanya method, path (digit mirip nomor HP diredaksi), status, correlation id.
  Body pesan, token dan nomor tidak pernah dilog. n8n tidak menyimpan eksekusi sukses.
- Metrik yang dipantau: `GET /internal/v1/outbox/stats` (jumlah per state, umur pesan tertua), event `failed` di
  `telco.inbound_event`, dead-letter `outbox.state='failed'`, `ingestion_watermark` lag vs hari ini, error 5xx, circuit Hermes
  `open`, `validate.sql` + `validate_platform.sql` harian (harus lulus).
- Evidence & audit: `evidence_snapshot` (digest, query_version, watermark, batch), `analysis_audit`, `action_audit`,
  `correction_audit`, `grant_audit`.

## 6. Backup, restore, rollback

- Aktifkan backup otomatis + PITR provider. Target awal RPO ≤ 15 menit / RTO ≤ 2 jam adalah **target**, belum terbukti di cloud.
- Drill logis lokal (sudah dijalankan, lihat `reports/restore_drill.json`):

```bash
SRC=telco_test_jatim_clean bash scripts/restore_drill.sh
```

  Drill: `pg_dump -Fc` → restore ke database **baru** → `validate.sql` + `validate_platform.sql` → bandingkan sidik jari
  (jumlah baris, sum revenue, md5 sell_out, checksum migration, grant, identitas).
- Rollback aplikasi: deploy ulang image tag sebelumnya (API/MCP/dashboard stateless). Rollback skema: **tidak ada down
  migration destruktif**; perbaiki maju dengan migration baru atau restore snapshot ke database baru lalu alihkan koneksi.
- Rollback workflow n8n: import ulang JSON dari commit sebelumnya lalu `publish:workflow`.
- Insiden WhatsApp: set `WA_SEND_MODE=disabled` (pesan tertahan di outbox, tidak hilang), selidiki, aktifkan kembali.

## 7. E2E lokal (mock, tanpa WhatsApp/model nyata)

Prasyarat: PostgreSQL lokal (`.local/pgsql`), DB disposable `telco_test_dev` (clone template), n8n 2.35.7, Hermes 0.20.5.
Proses: mock WA `python -m tests.mock_servers wa --port 8790`, mock LLM `… llm --port 8791`, MCP bridge
`python -m telco_mcp.server`, Hermes `HERMES_HOME=.local/hermes-home hermes gateway` (render via
`deploy/hermes/render_home.py`), API `bash scripts/run_api_local.sh`, n8n `n8n start` dengan workflow hasil
`build_workflows.py --api-base http://127.0.0.1:8000`. Lalu:

```bash
python scripts/e2e_local.py --report reports/e2e_local.json
```

## 8. Operasi admin

- Kuota chat AI per jabatan: `telco.chat_quota_policy` (role ops boleh `UPDATE`); pemakaian hari ini per akun di
  `telco.chat_quota_usage` (baris `refunded_at` terisi = dikembalikan karena model gagal). Detail: `n8n/README.md`.
- Setelah migrasi 010: jalankan ulang `sql/security_platform.sql`, lalu `python -m telco_api.provision roles` (membuat login
  `telco_agent_memory_login` dari `TELCO_AGENT_MEMORY_PASSWORD`), lalu buat credential n8n `telcoRouterOpenAi` + `telcoAgentMemory`.

- Cabut izin: `python -m telco_api.provision revoke --principal <id> --scope district --entity <id> --reason "<alasan>"`
  (berlaku langsung, termasuk follow-up konteks lama dan tautan chart).
- Penerimaan stok (supply) hanya via job admin dengan bukti sumber; tidak ada endpoint publik/agent.
- Koreksi backdate: endpoint supervisor `/v1/corrections/backdated-sale` (lock, cek saldo kronologis, audit). Periode yang sudah
  ditutup watermark tidak bisa diposting.

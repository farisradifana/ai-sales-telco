# Mengaktifkan WhatsApp sungguhan (nomor AI Agent)

Langkah 1–6 dilakukan **pemilik akun** di Meta (Claude tidak membuat akun, tidak memegang password, tidak menyalin token).
Semua nilai rahasia diisi ke `.local/live.env` (gitignored), **jangan** ditempel di chat atau commit.

## A. Meta WhatsApp Cloud API

1. **Meta Business Account** — buka business.facebook.com, buat/pilih bisnis. Verifikasi bisnis disarankan (dibutuhkan
   untuk batas kirim lebih tinggi dan display name resmi).
2. **Aplikasi Meta** — developers.facebook.com → My Apps → Create App → tipe *Business* → tambahkan produk **WhatsApp**,
   hubungkan ke Business Account di atas.
3. **Daftarkan nomor AI Agent** — WhatsApp → API Setup → *Add phone number*. Nomor **tidak boleh** sedang aktif di aplikasi
   WhatsApp/WhatsApp Business biasa; jika aktif, cadangkan chat lalu hapus akun WhatsApp di HP tersebut terlebih dulu.
   Verifikasi kode SMS/telepon, isi display name (perlu persetujuan Meta).
4. **ID** — di API Setup salin **Phone number ID** → `WA_PHONE_NUMBER_ID`, dan **WhatsApp Business Account ID** → `WA_WABA_ID`.
5. **Token permanen** — Business Settings → Users → **System users** → Add (peran Admin) → *Assign assets*: aplikasi tadi dan
   WhatsApp account (full control) → *Generate new token* → pilih aplikasi → izin `whatsapp_business_messaging` dan
   `whatsapp_business_management` → masa berlaku *Never* → salin ke `WA_ACCESS_TOKEN`. (Token sementara 24 jam dari API Setup
   hanya untuk coba-coba.)
6. **App Secret** — App settings → Basic → *App secret* (Show) → `WA_APP_SECRET`. Dipakai API untuk memverifikasi
   `X-Hub-Signature-256` pada raw body; pesan tanpa signature valid ditolak dan tidak masuk antrean.

`WA_VERIFY_TOKEN` sudah dibuat acak oleh `python scripts/wa_live_setup.py init`; salin nilainya dari file ke Meta di langkah C.

## B. Penerima uji (allowlist)

Mode `live` hanya mengirim ke nomor yang terdaftar di allowlist. Daftarkan nomor HP Anda (atau sales uji yang sudah setuju)
dan petakan ke satu akun — nomor diketik di prompt tersembunyi, disimpan terenkripsi, tidak pernah dicetak:

```bash
DB=telco_test_dev3 . scripts/devenv.sh && python scripts/wa_live_setup.py register --principal sls-35.19.01
```

Periksa kredensial (read-only, tidak mengirim pesan):

```bash
python scripts/wa_live_setup.py check
```

## C. Webhook via Cloudflare Tunnel (PC ini)

Jalankan berurutan (masing-masing di terminal sendiri): API mode live, edge lokal (hanya `/webhooks/whatsapp` dan `/charts/*`
yang diteruskan), lalu tunnel:

```bash
LIVE=1 bash scripts/run_api_local.sh
```

```bash
python scripts/edge_local.py --port 8089
```

```bash
.local/download/cloudflared/cloudflared.exe tunnel --no-autoupdate --url http://127.0.0.1:8089
```

Tunnel mencetak URL `https://<acak>.trycloudflare.com`. Di Meta: WhatsApp → Configuration → Callback URL
`https://<acak>.trycloudflare.com/webhooks/whatsapp`, Verify token = isi `WA_VERIFY_TOKEN` → *Verify and save* → subscribe field
**messages**. Set juga `PUBLIC_BASE_URL=https://<acak>.trycloudflare.com` agar tautan chart dapat dibuka penerima.
URL quick tunnel **berubah setiap restart** dan bisa mati sendiri ("Tunnel not found"); untuk URL tetap gunakan named tunnel.

### C2. Named tunnel (URL tetap)

Syarat: domain aktif di Cloudflare (nameserver Cloudflare sudah tercatat di registry; record lama disalin sebagai *DNS only*).

```bash
cloudflared tunnel login                      # pilih zone, Authorize -> ~/.cloudflared/cert.pem
cloudflared tunnel create telco-wa            # kredensial ~/.cloudflared/<uuid>.json (rahasia, di luar repo)
cloudflared tunnel route dns telco-wa telco-wa.<domain>
```

Config `.local/cloudflared/telco-wa.yml` (di luar git):

```yaml
tunnel: <uuid>
credentials-file: C:\Users\<user>\.cloudflared\<uuid>.json
ingress:
  - hostname: telco-wa.<domain>
    service: http://127.0.0.1:8089   # edge lokal: hanya /webhooks/whatsapp dan /charts/*
  - service: http_status:404
```

Jalankan `cloudflared tunnel --config .local/cloudflared/telco-wa.yml run telco-wa`, set
`PUBLIC_BASE_URL=https://telco-wa.<domain>`, lalu Callback URL Meta `https://telco-wa.<domain>/webhooks/whatsapp` (cukup sekali).
Instalasi PC ini: tunnel `telco-wa`, hostname `telco-wa.farisradifana.my.id`.

n8n (workflow 01–04, 06) dan Hermes harus berjalan seperti E2E lokal (`docs/RUNBOOK.md` bagian 7).

## D. Uji

Dari HP yang terdaftar, kirim ke nomor AI Agent: `stok`, lalu `SELLIN OUT-0061 SIM-NOVA-8 1` → balas `YA <kode>`, lalu
`kenapa omzet OUT-0061 turun?` dan `apa yang harus dilakukan?`. Balasan atas pesan masuk (jendela 24 jam) tidak perlu template.
Nudge target harian adalah pesan inisiatif bisnis → butuh **template Utility yang disetujui Meta** dan metode pembayaran di
WhatsApp Manager; sampai template tersedia, workflow 05 dibiarkan nonaktif.

## Pengaman yang tetap berlaku

- Pengirim yang tidak terdaftar disimpan untuk audit, tidak diproses dan tidak dibalas.
- Kirim hanya ke allowlist; `LIVE=1` menolak start jika kredensial atau allowlist kosong.
- `WA_SEND_MODE=disabled` menghentikan pengiriman seketika (pesan tertahan di outbox, tidak hilang).
- Error umum Graph: `190` token kedaluwarsa/tidak valid; `131030` penerima tidak diizinkan (mode development); `131047`
  di luar jendela 24 jam (butuh template); `131056` rate limit pasangan nomor.

## Docker & Redis (opsional, untuk n8n queue mode)

Docker Desktop harus dipasang oleh pengguna (installer resmi docker.com, membutuhkan admin + WSL2:
`wsl --install` lalu restart). Agar data Docker berada di proyek ini: Docker Desktop → Settings → Resources → Advanced →
*Disk image location* → `F:\ai-sales-telco\.local\docker`. Setelah itu:

```bash
docker compose -f deploy/compose.local-queue.yaml --env-file .local/dev.env up -d
```

(Redis 8.2.10 + PostgreSQL n8n + n8n main/webhook/worker, semua data di `.local/`, port hanya 127.0.0.1.)

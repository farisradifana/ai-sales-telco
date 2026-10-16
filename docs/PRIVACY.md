# Kebijakan Privasi — AI Sales Intellegence (TELCO Sales AI)

Berlaku sejak: 16 Oktober 2026

Kebijakan ini menjelaskan bagaimana layanan **AI Sales Intellegence** ("Layanan") — asisten penjualan berbasis WhatsApp
dan dashboard internal untuk tim penjualan — memproses data. Layanan ini saat ini adalah **prototipe/uji coba internal**
dan hanya digunakan oleh personel penjualan yang telah didaftarkan secara eksplisit oleh administrator.

## 1. Siapa yang menggunakan Layanan

Hanya pengguna internal yang terdaftar (sales, territory manager, head of branch, head of sales, head of region). Pesan dari
nomor WhatsApp yang tidak terdaftar tidak diproses dan tidak dibalas; metadata minimum (ID pesan, waktu, hash isi) dicatat
untuk audit keamanan.

## 2. Data yang kami proses

| Data | Tujuan | Catatan |
|---|---|---|
| Nomor WhatsApp pengguna terdaftar | Mengenali pengguna dan mengirim balasan | Disimpan **terenkripsi** (AES-256-GCM) dengan indeks HMAC terpisah; tidak ditampilkan di log |
| Isi pesan yang Anda kirim ke Layanan | Menjalankan perintah (input penjualan, stok, target, insight) | Disimpan dalam bentuk ternormalisasi tanpa nomor telepon |
| Data penjualan, stok, target | Fungsi inti Layanan | Data uji coba bersifat fiktif/sintetis |
| Log teknis | Keamanan, audit, pemecahan masalah | Berisi ID korelasi; nomor telepon dan token disamarkan |

Kami **tidak** mengumpulkan kontak, lokasi, media, atau data pelanggan akhir. Kami **tidak** menjual atau membagikan data
untuk iklan.

## 3. Dasar dan cara pemrosesan

Data diproses untuk menjalankan fungsi kerja yang diminta pengguna terdaftar. Jawaban analitik dibuat dari data penjualan
internal; model AI hanya menerima ringkasan bukti (evidence) yang diizinkan untuk akun Anda, tanpa nomor telepon.
Tindakan yang berdampak (posting penjualan, reversal, transfer) selalu memerlukan konfirmasi manusia.

## 4. Pihak ketiga

- **Meta Platforms (WhatsApp Business Platform / Cloud API)** — penyampaian pesan WhatsApp, tunduk pada kebijakan Meta.
- **Penyedia infrastruktur/hosting dan penyedia model AI** yang dikonfigurasi administrator, hanya sebatas yang diperlukan
  untuk menjalankan Layanan.

## 5. Penyimpanan dan keamanan

Akses data dibatasi berdasarkan peran dan wilayah (default ditolak). Kredensial disimpan di luar kode sumber. Data
percakapan analitik memiliki masa berlaku (TTL) dan log eksekusi otomatis yang berhasil tidak disimpan. Data uji coba
dapat dihapus setelah masa uji coba berakhir.

## 6. Hak Anda

Pengguna terdaftar dapat meminta: akses ke data yang terkait dengannya, koreksi, penghapusan pemetaan nomor WhatsApp, atau
berhenti menerima pengingat (balas `STOP TARGET`). Permintaan dapat diajukan melalui administrator Layanan atau melalui
halaman kontak di bawah.

## 7. Kontak

Pertanyaan atau permintaan terkait data pribadi: **farisradifana@kata10.id**.
Untuk masalah teknis non-pribadi dapat juga membuka *Issue* di https://github.com/farisradifana/ai-sales-telco/issues
(jangan menuliskan data pribadi di Issue publik).

## 8. Perubahan

Kebijakan dapat diperbarui seiring perkembangan Layanan; tanggal berlaku di atas akan diperbarui.

---

### English summary

AI Sales Intellegence is an internal pilot sales assistant on WhatsApp. It only serves pre-registered internal users;
messages from unregistered numbers are not processed or answered. Phone numbers are stored encrypted and never logged; we
do not sell data or use it for advertising. Data is shared only with Meta (WhatsApp Cloud API) and the hosting/AI providers
configured by the administrator, as needed to operate the service. Users may request access, correction or deletion via
the administrator or farisradifana@kata10.id.

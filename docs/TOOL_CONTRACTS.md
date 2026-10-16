# Kontrak API, tools dan konteks

Adapter Python `telco/tools.py` adalah backend tool reference, bukan server HTTP. Claude Code membangun API sesuai kontrak ini. Semua inputs divalidasi Pydantic/JSON Schema dengan `extra=forbid`; parameter SQL terikat. **Tidak ada tool execute_sql**. Internal `diagnose` dan CLI hanya untuk operator tepercaya. API memakai `authorized_diagnose` dengan principal hasil autentikasi server. `principal_grant` pada migration 004 memberi outlet/district explicit, default deny; grant district mencakup outlet di dalamnya. Operator provision grant, agent tidak memiliki izin menambah grant. Single tenant, role database API punya akses lintas outlet; API authorization wajib, bukan diganti klaim RLS yang belum ada.

| Endpoint / tool | Input domain | Hasil / side effect |
|---|---|---|
| GET /health/live, /health/ready | none | liveness, readiness DB/queue/upstream (tanpa secrets) |
| POST /v1/tools/diagnose | scope=outlet/district, entity_id integer, channel=sell_in/sell_out, baseline/current start/end | authorize scope; read-only repeatable-read snapshot; evidence, drivers, action hypotheses, digest |
| POST /v1/tools/rank-districts | channel, dua periode setara | hanya kecamatan yang di-grant; list baseline/current/delta/growth, lanjut diagnosis coverage sebelum keputusan |
| POST /v1/tools/stock | account_id, product_id, as_of | authorize account sales/outlet; numeric dan satuan; jangan memaparkan stok pihak lain |
| POST /v1/tools/target | tanggal, channel | principal→sales mapping server; actual/target/gap/achievement; jangan sum channel |
| POST /v1/contexts | evidence_id, opaque conversation key | backend menyimpan evidence hasil tool, bukan angka supplied LLM; TTL 24h |
| POST /v1/contexts/{id}/follow-up | question, expected_version | reauthorize principal dan scope, cek TTL/versi/watermark, kembalikan evidence pinned/recomputed |
| POST /v1/transactions/sell-in | external_key, occurred_at, outlet_id, product_id, quantity, unit_price, discount_idr | writer derive sales+assignment dari principal/tanggal, stock posting atomik |
| POST /v1/transactions/sell-out | fields sama + customer_token SYN untuk demo | scope auth; whitelist price/discount policy; atomic posting |
| POST /v1/reversals | channel, transaction_id, external_key, occurred_at, reason | supervisor authorization, explicit confirmation reference, full reversal, audit |
| GET/POST /webhooks/whatsapp | provider-specific verification / signed payload | validasi signature raw body, dedup provider+message_id+hash, enqueue lalu ACK |

`rank-districts` tidak boleh memanggil function global lalu memberikan seluruh hasil ke LLM sales terbatas. Filter daftar ID dengan grant **sebelum** serialisasi, evidence logging, cache, maupun model call. Outlet-only principal tidak otomatis berhak melihat seluruh kecamatan. Tool target/stock harus menerapkan kepemilikan yang setara. Tidak ada endpoint supply publik; receipts lewat ingestion/admin yang dibuktikan sumbernya.

Contoh request:

```json
{
  "scope": "outlet", "entity_id": 1, "channel": "sell_out",
  "baseline_start": "2026-06-01", "baseline_end": "2026-07-01",
  "current_start": "2026-09-01", "current_end": "2026-10-01"
}
```

Balasan mencakup `evidence_id` (UUID server), `query_version`, `evidence_digest`, `correlation_id`, `scope`, `entity_id`, `channel`, periode WIB, `revenue_baseline_idr`, `revenue_current_idr`, `delta_idr`, nullable `growth`, `drivers`, coverage dua periode, `watermark`, `data_ready`, action/evidence/confidence/limitations. Nilai uang API berupa decimal string, bukan float binary. Contoh JSON hasil function database ada pada `examples/outlet_evidence.json`; wrapper menambahkan envelope ID dan rekomendasi. Frontend membangun chart dari array evidence tersebut, tanpa query ulang yang berbeda scope.

Response error: 400 input/periode/entitas ambigu; 401 unauthenticated; 403 scope denied (jangan bocorkan nama outlet); 409 key/payload mismatch atau context version conflict; 422 insufficient stock/invalid assignment; 429 rate limit; 503 upstream/DB temporary. Data incomplete bukan 500: return evidence dengan data_ready=false dan label provisional. Retry hanya transient, bukan oversell atau invalid input.

## Follow-up yang benar

1. “Kenapa outlet OUT-0001 turun?” → server resolve ID=1, default sell-out, jelaskan baseline Juni 30 hari vs September 30 hari terakhir lengkap, authorize lalu `diagnose`.
2. Simpan context opaque dengan **principal dari session tepercaya**, entity/channel/period, evidence+hash, query version, watermark+batch. `save_context` menyimpan snapshot dan audit; versi optimistic melindungi tab/chat serentak.
3. “Apa yang harus dilakukan?” → retrieve menggunakan context_id dan principal yang sama; `follow_up` menolak principal lain/expired; API juga reauthorize scope lewat can_access. Jangan membuat lookup global “latest context”.
4. Jika watermark/batch/query version berubah, recompute dengan periode/scope sama, simpan memakai expected_version. Jika izin dicabut, tolak walaupun evidence pernah dibaca sebelumnya. Prompt injection dalam pesan atau nama outlet tidak dapat mengubah grant/tool allowlist.
5. “Kalau kecamatannya?” → ambil district outlet yang sama, reauthorize scope district; jika tidak berhak jangan tampilkan agregat. “Bulan lalu?” → perlu periode baru hari setara dan sebutkan tanggal aktualnya. Jika lebih dari satu referen, klarifikasi sebelum tool.

Context produksi juga perlu ringkasan percakapan seperlunya dan last turn IDs, disimpan dalam store terenkripsi terpisah tanpa PII mentah di LLM logs. Tabel context paket ini menyimpan **konteks analisis terstruktur**, bukan transkrip WhatsApp lengkap. Tambah conversation_turn via migration jika diperlukan. Auth/phone mapping dan secrets berada di service identity, tidak disimpan sebagai nomor pelanggan dalam transaksi demo.

## Idempotency dan delivery

Inbound unique provider+message_id dengan hash; transaksi memakai external_key stabil dari provider message/line index. Worker crash setelah commit tidak boleh membuat penjualan ulang. Simpan event status dan intent outbox dalam transaksi yang sama dengan penjualan/context bila perlu. Dispatcher mengklaim outbox `FOR UPDATE SKIP LOCKED`, lease/timeout via migration service, retry exponential, dan catat provider message_id. Exactly-once delivery tidak dijamin API eksternal; sebutkan kondisi crash setelah send sebelum ACK dan strategi rekonsiliasinya. Outbox tabel awal menyediakan state dasar; worker lease/dead-letter dan provider reference adalah pekerjaan infrastruktur dalam prompt, bukan klaim sudah dijalankan.

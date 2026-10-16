# Prompt siap tempel untuk Claude Code

Salin seluruh blok di bawah ke Claude Code yang membuka root paket ini. Tidak memerlukan percakapan ChatGPT asal. Isi konfigurasi cloud dan secret lewat environment/secret manager saat diminta; jangan tempel secret ke prompt.

```text
Anda adalah lead engineer untuk membangun TELCO Sales AI di cloud, dimulai dari database reference di repository ini. Kerjakan implementasi nyata, executable dan teruji, bukan hanya proposal. Bahasa UX dan README utama Indonesia. Baca README.md, sql/001_core.sql sampai 004_access.sql, telco/*.py, docs/ERD.md, docs/KPI_AND_ANALYSIS.md, docs/TOOL_CONTRACTS.md, docs/CLOUD.md, tests/ dan examples/ dahulu. Gunakan hasil lokal sebagai kontrak; jangan mengarang bagian jawaban assistant percakapan lama yang tidak tersedia.

TUJUAN PRODUK
Sales lapangan dapat menginput penjualan fisik/saldo via WhatsApp, melihat stok dipegang dan target harian, meminta insight outlet/kecamatan, kemudian bertanya “apa yang harus dilakukan?” dengan konteks tetap. Supervisor/area manager memakai dashboard berbukti untuk mengambil keputusan. Spreadsheet hanya rekap/export atau import staging tervalidasi. Semua data demo fiktif tanpa PII nyata; jangan mengirim WhatsApp nyata selama tes.

BATAS DATABASE WAJIB
1. PostgreSQL 17; migration existing immutable setelah applied. Tambahan selalu migration baru; runner checksum/advisory lock yang ada harus tetap dipakai. Jangan reset database, DROP schema, TRUNCATE atau menonaktifkan triggers tanpa instruksi eksplisit untuk database disposable.
2. Dataset 500.000 completed product lines = 100k sell-in + 400k sell-out Jan2025–Sep2026. Ledger/supply/master/target bukan bagian jumlah ini. Jalankan generator seed42 dan audit manifest. Jangan mengganti dengan data random yang melanggar stock/region/assignment.
3. Sell-in dan sell-out berbeda tahap bisnis. KPI omzet harus selalu memilih channel. Target sales harian menggabungkan fisik dan saldo dalam IDR pada channel yang sama. Jangan sum sell-in+sell-out untuk achievement atau omzet wilayah. Saldo stock unit IDR, fisik PCS; quantity lintas unit tidak boleh dijumlahkan.
4. Penjualan posted immutable; koreksi full reversal + replacement. Receipts ke sales, sell-in pindah sales→outlet, sell-out konsumsi outlet, semua atomik dengan ledger. No direct UPDATE balance. API menolak input backdate sebelum posting cutoff, kecuali jalur koreksi khusus tervalidasi running stock dengan lock dan audit. DDL balance check sendiri tidak cukup membuktikan stock historis setiap kasus backdate.
5. Dataset ini single tenant; grant outlet/district pada principal_grant dan can_access menyediakan authorization API default-deny. Tidak ada RLS multi-tenant. Jangan mengklaim ada. Jika multi-tenant dibutuhkan, lakukan desain baru teruji di seluruh FK, unique key, cache, evidence, context, outbox dan RLS sebelum launch.
6. Tidak ada real nomor pelanggan; customer_token SYN-. PII/auth mapping WhatsApp disimpan terpisah terenkripsi dengan akses terbatas. Jangan membuat nomor random yang bisa dituju. Jangan kirim demo ke nomor sungguhan.

ARSITEKTUR YANG HARUS DIBANGUN
WhatsApp Cloud API resmi -> ingress validasi signature raw-body/dedup -> n8n queue -> API domain -> Hermes Agent -> approved business tools -> PostgreSQL.
Hermes -> 9Router -> provider model berizin.
API -> dashboard React/TypeScript (chart dari evidence yang sama).
n8n memakai PostgreSQL terpisah untuk metadata/executions, Redis private, worker, encryption key konsisten, workflow export JSON version-controlled.
Spreadsheet rekap dibuat dari API/export, bukan sumber kebenaran stok.

Stack reference: Python 3.12 + FastAPI + Pydantic + psycopg3, PostgreSQL17, Redis, n8n versi dipin, React TypeScript. Pakai Docker Compose staging/local dan opsi deploy container ke cloud pengguna. Jika provider cloud belum dipilih, hasilkan deployment container provider-neutral plus managed-PG instructions; tanyakan hanya konfigurasi yang memblokir deploy nyata. Jangan mengarang endpoint/live URL/secret. Berikan .env.example dan setup secrets, healthchecks, build commands, observability, backup/restore serta rollback runbook.

HERMES / 9ROUTER
Asumsi proyek: https://github.com/NousResearch/hermes-agent dan https://github.com/decolua/9router. Verifikasi repository, lisensi, versi/commit, endpoint dan kompatibilitas sebelum implementasi; jika proyek dimaksud berbeda, konfigurasi adapter dan jelaskan asumsi. Dokumentasi referensi: https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server dan https://docs.n8n.io/hosting/scaling/queue-mode/.
Jangan menebak port/base path/model ID. Pin artifact/version/commit; configurable HERMES_BASE_URL, HERMES_API_KEY, ROUTER_BASE_URL, ROUTER_API_KEY, MODEL_ID. Capability probe terhadap versi terpasang. Gunakan upstream API yang berizin; jangan mengandalkan bypass rate limit, subscription reuse atau token pengguna lain. Timeout, circuit breaker, fallback model harus preserve tool schema dan auth boundary. Fallback tidak boleh menjawab angka ketika tool/DB gagal.
Hermes tidak diberi shell, arbitrary SQL, filesystem secrets, raw internet browsing atau database credential. Tool allowlist hanya API bisnis. Gunakan MCP/HTTP bridge sesuai fitur versi terpasang, dengan contract tests. Model routing dan agent reasoning tidak boleh melewati authorization API. Memory Hermes bukan source of truth analisis; context terstruktur database yang menentukan entity/metric/period/evidence.

API DAN IDENTITAS
- Implementasikan endpoints/tools docs/TOOL_CONTRACTS.md dengan strict typed inputs extra=forbid, decimal string uang, timestamp timezone-aware, pagination/bounds, request/correlation ID dan stable errors.
- JWT/session diverifikasi server; principal dari auth/WhatsApp identity mapping, bukan dari JSON LLM. Provision principal_grant oleh admin. can_access dicek sebelum query, logging, cache, model call dan setiap follow-up. Reader DB hanya di API internal. n8n, dashboard browser dan Hermes tidak diberi database credentials.
- authorized_diagnose wrapper adalah dasar. Scope outlet/district harus authorized. Ranking hanya boleh memberi kecamatan authorized, bukan mengembalikan ranking global terlebih dulu ke model. Sales outlet-only tidak otomatis mendapat aggregate district.
- Stok account sales/outlet dan target terikat principal identity; derive sales_id/assignment_id di server saat posting. Jangan menerima sales_id untuk impersonation. Validate price/discount policy dan quantity integer PCS. Receipt/supply bukan endpoint agent publik.
- Role read-only untuk analytical tool dan role writer terpisah untuk domain/context. Run sql/security.sql via admin provision setelah migration. Tidak pernah memakai owner/superuser untuk API. Deployment user hanya punya grant yang dibutuhkan, no CREATE schema/function.
- Model tidak boleh memanggil transaksi tulis hanya dari teks insight. Rencana tindakan stok/harga/kunjungan berbeda dari eksekusi. Input penjualan WhatsApp tampilkan ringkasan konfirmasi sebelum final posting; supervisor confirmation untuk reversal dan transfer. Catat confirmation event ID/payload hash.

WORKFLOW N8N YANG HARUS DISERTAKAN
1. Inbound: GET verification challenge sesuai provider dan POST signature HMAC raw body; schema validation, dedup provider+message_id, hash mismatch=>409, enqueue/ACK cepat. Invalid signature tidak masuk queue.
2. Input sales: resolve identity, parse/validate outlet/SKU/quantity/channel; ambigu->klarifikasi; render summary confirmation; confirmed->API transaction; duplicate identical->hasil asal; insufficient stock->pesan jelas, bukan retry tanpa batas.
3. Insight: intent extraction -> scoped API tool -> Hermes menyusun narasi dari evidence -> chart rendering URL private/signed atau image evidence -> outbound. Jangan masukkan SQL langsung ke n8n node dengan interpolation pesan pengguna.
4. Follow-up: principal+conversation opaque key -> persisted context TTL/version, reauthorize/recompute bila watermark berubah -> answer dengan scope dan periode tetap.
5. Daily target: WIB hari kerja, opt-in/opt-out, jam kerja, achievement per channel, fisik+saldo IDR; soft motivation yang menyebut actual/target/gap aktual. Hindari pengulangan tiap workflow retry. Jangan menjumlahkan kedua channel; channel target principal tersimpan explicit.
6. Outbox dispatcher: unique idempotency key, lease/claim SKIP LOCKED (tambahkan migration untuk lease/provider reference), exponential retry+jitter bounded, dead-letter, correlation ID. Akui at-least-once provider boundary; uji crash setelah commit sebelum send dan send sebelum ACK. Redaksi PII dari execution logs.
7. Rekap spreadsheet: export snapshot scoped dengan generated_at, watermark, unit, channel dan totals. Import masuk staging, file hash, validation, preview reconciliation, admin confirmation lalu domain events; tidak overwrite stock_balance. Idempotency per source-file/row.
Export JSON workflow yang benar-benar dapat diimport pada versi n8n yang dipin, tanpa credentials inline; sertakan cara bind credential IDs dan smoke-test import.

ANALYTICAL BEHAVIOR
- “Kenapa omzet outlet A turun?” default sell-out, resolve outlet ID, pilih last COMPLETE period dari ingestion watermark, bukan tanggal sistem. Sebutkan baseline/current WIB dan end exclusive. Bandingkan jumlah hari sama. Contoh June1–July1 vs Sep1–Oct1 2026; jangan diam-diam membandingkan 31 hari vs30.
- Diagnose evidence: baseline,current,delta, nullable growth jika base0, product contributions, q/price effects, entry-exit/nonpositive bucket, coverage kedua periode, watermark+batch, query_version, digest. Driver total dan efek reconcile ke delta <=Rp0,01. Jangan aggregate quantity PCS+saldo IDR.
- Price/volume formula per SKU sesuai docs. Ini accounting decomposition, bukan sebab kausal. Missing data vs zero harus dibedakan. Stockout unknown tidak diubah nol; zero closing stock tidak membuktikan lost sales. No competence/competitor/network claims tanpa sumber.
- Jika coverage kurang/watermark terlambat: label provisional dan action data-quality dulu. Ranking kecamatan ditampilkan hanya untuk scope authorized, lalu diagnosis coverage+outlet contribution sebelum tindakan. Gunakan fixed population/scaffold; tampilkan outlet buka/tutup dan input missing bila memengaruhi perbandingan.
- Recommendations: fakta terukur -> hipotesis -> tindakan berisiko rendah -> owner -> review horizon -> success/guardrail. Audit kunjungan dan SKU dominan; cek kebutuhan/ketersediaan sebelum reorder. Jangan invent uplift, p-value, confidence probability atau hasil eksperimen. Tujuan tindakan butuh human confirmation, no autonomous bulk WA/campaign/price change.
- Jawaban Indonesia ringkas: “Dalam [periode] vs [baseline], sell-out [entitas] berubah RpX (Y% jika base>0). Kontribusi terbesar SKU Z Rp...; volume... harga/mix... . Kunjungan tercatat... ; ini asosiasi. Saran... . Batas bukti... . Evidence ID...”. Semua angka harus berasal tool. Chart labels, data points dan totals sama dengan evidence JSON.
- Follow-up “apa yang harus dilakukan?” preserve entity/channel/period/digest. “Kecamatan mana?” membutuhkan scope baru dan izin ulang. Context expired/ambiguous: tanya singkat, jangan nebak. Optimistic version konflikt tidak overwrite context diam-diam.

DASHBOARD
Role-based login, outlet/district selector hanya authorized, channel toggle explicit (no combined revenue default), period/equal-day comparison, watermark+coverage banner. KPI revenue/growth/achievement, trend calendar with missing gaps, district ranking, outlet contributor table, SKU waterfall/volume-price decomposition, stock per account+unit, targets, action cards+evidence links. Drill-through menyimpan scope/period; export CSV with metadata. Responsive, accessible labels, no raw technical setup strings dalam flow sales. Semua chart pakai satu evidence snapshot; empty/loading/error/stale states wajib.

DATA DAN CONCURRENCY
Semua event ID idempotent. Insert transaction+inbound state+outbox dalam satu commit jika satu database. Jika antar database, gunakan transactional outbox, jangan klaim distributed transaction. Concurrent oversell, duplicate webhook dan late/out-of-order input harus teruji. Set statement/lock timeout, retry 40001/40P01 saja dengan batas, seluruh transaksi diulang. Backdated posting path disallow by default. Target posting calendar WIB konsisten UTC storage. Migration cloud tidak memerlukan unsupported extensions.

DELIVERABLE DAN ACCEPTANCE
1. Repository lengkap: app API, dashboard, adapters Hermes/9Router, workflows n8n, deployment files, migrations additive, tests, README dan runbooks. Simpan struktur database reference dan generator yang ada. Tambahkan version-pinned lockfiles dan .env.example tanpa secret.
2. Jalankan generator full seed42; assert100k+400k; independent CSV validator; SQL migration/load/validate di PostgreSQL server; tests/reports disimpan. Test migration rerun+checksum mismatch dan load twice refusal.
3. Real PostgreSQL tests dua koneksi membuktikan concurrent oversell tidak minus, idempotency key unik, reversal stock+revenue, historical balance, assignment validity dan immutable events.
4. API tests: principal A tidak bisa lihat outlet/district/context/stock/target B, revoked grant berlaku follow-up, injection raw SQL tidak tersedia, ambiguous name needs clarification, returns0-base growth null, differing periods rejected, missing coverage blocks strong diagnosis.
5. Conversation tests: outlet diagnosis->“solusinya?” preserves context; scope change reauthorize; stale TTL prompts; watermark changed recomputes; optimistic conflict409; evidence numbers reconcile chart/narrative.
6. Mocked E2E WhatsApp signature->queue->n8n->API->Hermes adapter->tool->Postgres->outbox->mock send; duplicate/out-of-order webhook; worker crash/retry; no real customer message. Live external smoke hanya bila user memberi credentials dan authorized destination.
7. Dashboard build+render visual inspection desktop/mobile, keyboard accessibility, empty/error/stale states, scoped filter and export totals.
8. Cloud staging deploy only after configuration and authorization available; verify health/readiness, private networking, TLS, backup restore drill. Jangan menulis “deployed/production-ready” tanpa verifikasi. Jika tools/credentials belum ada, complete local/mock paths, tulis blocker spesifik dan exact next command; jangan membuat endpoint palsu atau menghilangkan tes.
9. Selesai dengan ringkasan apa yang berubah, cara run, tes yang benar-benar lulus/skip, keputusan asumsi, batas yang belum diuji dan next deploy step. Jangan expose secret/log PII.

KERJAKAN SEKARANG
Mulai audit repository dan jalankan tes existing. Buat rencana implementasi singkat dengan urutan database verification -> API/auth -> workflows/adapters -> dashboard -> integration/deploy tests. Implementasikan tahap yang bisa dijalankan, teruskan sampai deliverable lengkap; jangan berhenti pada desain. Tanyakan hanya secret atau keputusan yang benar-benar memblokir langkah eksternal, sambil mengerjakan bagian independen yang tersedia.
```

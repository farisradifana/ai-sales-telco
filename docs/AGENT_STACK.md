# Hermes Agent, 9Router dan batas tool

Diverifikasi 16 Oktober 2026 terhadap instalasi lokal dan repository resmi.

| Komponen | Versi dipin | Lisensi | Bukti |
|---|---|---|---|
| Hermes Agent (NousResearch/hermes-agent) | 0.20.5, commit `14c59f0b505ea34fb46991784e0a996ceab70dcc` | MIT | `pyproject.toml`, `git log`, `GET /health` → `{"version":"0.20.5"}` |
| 9Router (decolua/9router) | npm `9router@0.5.99` | MIT | `package.json`, `LICENSE`; CLI `--host/--port/--no-browser/--skip-update`, default port 20128 |
| MCP Python SDK | `mcp==2.3.0` (`MCPServer`, streamable HTTP) | MIT | `requirements-api.lock` |
| n8n | `2.35.7` (image `n8nio/n8n:2.35.7`) | Sustainable Use License | `n8n --version`; typeVersion node diambil dari `n8n export:nodes` |

## Kontrak Hermes yang dipakai (dari dokumentasi di commit terpasang)

- `GET /health` (tanpa auth), `GET /v1/capabilities`, `GET /v1/toolsets`, `POST /v1/chat/completions` — Bearer `API_SERVER_KEY`, port default 8642, bind default 127.0.0.1.
- API server Hermes **secara default memberi akses penuh ke toolset agent, termasuk terminal**. Karena itu
  `deploy/hermes/render_home.py` menulis `agent.disabled_toolsets` berisi seluruh toolset bawaan (dikurangkan setelah semua
  aturan platform) dan `platform_toolsets.api_server: []`. Satu-satunya tool adalah `mcp_servers.telco` (include-list 7 tool,
  `trust: full` karena bug readOnlyHint upstream — lihat VERIFICATION, resources/prompts off). Memory Hermes dimatikan: konteks resmi ada di tabel `conversation_context`.
- **Capability probe** (`telco_api/agent.py::HermesAdapter.probe`) memanggil `/health`, `/v1/capabilities` dan `/v1/toolsets` pada
  versi yang berjalan. Jika ada toolset bawaan yang `enabled`, atau `chat_completions` tidak tersedia, API menolak memakai Hermes.
  Hasil nyata pada Hermes 0.20.5 dengan config ini: **0 dari 28 toolset bawaan aktif**; MCP bridge terhubung.

## Identitas pada panggilan tool

Hermes tidak meneruskan identitas sesi ke panggilan MCP. Maka API membuat **ticket satu-turn** (`agent_turn`, hanya hash yang
disimpan, TTL 180 detik, maks 6 panggilan, daftar tool, daftar evidence) dan menaruhnya di instruksi sistem. Bridge meneruskan
ticket + `MCP_SERVICE_TOKEN` ke `/internal/v1/agent-tools/*`; API memetakan ticket → principal dan menjalankan otorisasi
`can_access` yang sama dengan endpoint publik. Model tidak bisa memilih principal; ticket palsu/kedaluwarsa → 401.
Tidak ada tool SQL, shell, filesystem, web, atau transaksi tulis.

## Model routing dan fallback

- Hermes → 9Router (OpenAI-compatible `/v1`) → provider berizin; `MODEL_ID` wajib diisi dari model yang benar-benar dikonfigurasi
  di 9Router. Jangan memakai akun langganan orang lain, OAuth CLI yang dibagi, atau cara melewati rate limit.
- Fallback 1: API memanggil 9Router langsung dengan **skema tool yang sama** (`agent_tools.openai_tools()`), dieksekusi lewat
  ticket yang sama (batas otorisasi sama).
- Fallback 2: template deterministik dari evidence (angka tetap dari tool, bukan dari model).
- Tool/DB gagal → tidak ada jawaban berangka (langkah `act` mengembalikan 503 dan tidak di-cache; n8n retry terbatas).
- Setiap narasi model diverifikasi: semua angka harus ada di evidence turn tersebut dan Evidence ID wajib disebut; jika tidak,
  narasi dibuang dan jalur berikutnya dipakai. Timeout 45 detik, circuit breaker 3 kegagalan / 60 detik.

## Catatan lingkungan pengembang ini

Instalasi 9Router lokal milik pengguna dikonfigurasi dengan model berprefix akun langganan. Konfigurasi itu **tidak dipakai**
oleh proyek; semua tes memakai mock provider (`tests/mock_servers.py llm`). Uji live membutuhkan API key provider berizin
yang diberikan pemilik akun.

## Jalur utama WhatsApp: AI Agent n8n (sejak migrasi 010)

Pesan bahasa natural di WhatsApp kini dijawab oleh node **AI Agent n8n** (workflow 03), bukan lewat `/events/narrate` →
Hermes. Model tetap lewat 9Router (combo `ai-sales`, credential `telcoRouterOpenAi`, Responses API off). Batas keamanan sama:
tiket satu-giliran untuk tool (dikirim n8n lewat header, tidak terlihat oleh model), tool read-only ber-otorisasi, verifikasi
angka di `telco_api/chat_agent.py::ChatAgent.finish`, ID teknis dihapus. Memory = Postgres Chat Memory di schema
`agent_memory` (login khusus, tanpa akses schema `telco`). Kuota chat harian per jabatan: lihat `n8n/README.md`.
Jalur Hermes (`/internal/v1/events/narrate`, MCP bridge) tetap tersedia untuk fallback Python dan tes, tetapi tidak dipakai router n8n.
Uji nyata (2026-10-17, n8n 2.35.7 + combo `ai-sales`): pertanyaan HoB Madiun "kecamatan mana yang paling turun bulan lalu?"
→ resolve + rank_districts → jawaban `verified=True`; pertanyaan lanjutan "kenapa yang paling turun tadi?" dipahami dari memory
→ diagnose → jawaban terverifikasi + tautan grafik. Waktu respons 20–104 detik (bergantung model combo).

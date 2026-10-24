# 9Router Extra / Pemindai dan Suite Integrasi

Versi: **v0.2.0**
Proyek: 9router_extra
Path: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Ikhtisar
Repositori ini berisi ekstensi, alat, cadangan status, dan patch untuk 9Router:
1. **Jembatan penyedia WorkBuddy AI (`wb/hy3`)**
   - Integrasi langsung dengan penyelesaian WorkBuddy AI berbasis cloud (`https://www.workbuddy.ai/v2/chat/completions`).
   - Mendukung model penalaran Hunyuan `hy3` (memaksa `stream: true`, gunakan blok konten bertipe, `reasoning_effort: high`, dan `reasoning_summary: auto`).
   - Impor token lokal otomatis dari `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Mendukung penyegaran token dan pelacakan kuota.
2. **Perbaikan Antigravity Gemini 3.8 Flash dan resolusi tingkat dinamis**
   - Memperbaiki masalah 404 dari upstream: Antigravity upstream memerlukan `gemini-3.8-flash-tiered(high|medium|low)`.
   - Menambahkan pencocokan pola dinamis sehingga setiap `gemini-*-flash-(high|medium|low)` dipetakan ke format bertingkat tanpa 404.
3. **Penambahan Awal Kombinasi SAIFREN**
   - `wb/hy3` dikonfigurasi sebagai model pertama, lalu `ag/gemini-3.8-flash-high` sebagai model kedua di `SAIFREN`.
4. **Patch AgentRouter dan Cline**
   - Penyedia AgentRouter bawaan, penerusan User-Agent dari klien, resolusi URL dasar Anthropic, dan perbaikan routing Cline.
5. **Mesin Cadangan dan Migrasi Status**
   - Snapshot seluruh 36 koneksi, 13 node khusus (B.AI, Dahl, Vyce, dan lainnya), serta kombinasi.

## Struktur
- `apply-update.ps1` - Skrip pembaruan sekali klik untuk berhenti dengan aman, membuat cadangan, meningkatkan ke v0.5.65-extra, memulihkan status, lalu memulai ulang 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - Paket v0.5.65 yang sudah dibangun penuh dan dipatch, siap untuk instalasi global.
  - `9router-0.5.59-agentrouter.tgz` - Paket lama v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - Patch terpadu untuk upstream v0.5.65.
  - `9router-extra-unified.patch` - Patch terpadu untuk v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - Alat cadangan dan sinkronisasi status SQLite
    (status pribadi kini mengarah ke `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, bukan repositori ini).

## Menjalankan pengujian sumber 9Router (jebakan pemanggilan vitest, T-21)
Kode dasbor Next.js berada di `%APPDATA%\9router\source`. Alias path vitest
(`@/` → `src`, `open-sse` → `open-sse`) hanya didefinisikan di
`tests/vitest.config.js`.

**Jangan jalankan biner dari root sumber:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Gunakan pemanggilan yang memuat konfigurasi berikut:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Berkas `vitest.config.js` di root yang mengekspor ulang konfigurasi pengujian juga membuat biner root berfungsi; berkas itu disimpan di pohon sumber agar kedua pemanggilan konsisten.
`tests/unit/model-routing.test.js` mengalami masalah `EPERM` yang sudah ada sebelumnya saat membersihkan `os.tmpdir()` di lingkungan ini. Pengujian gagal dengan cara yang sama untuk kedua pemanggilan; ini bukan masalah alias.

## Model keamanan
Repositori ini bebas rahasia dan aman diserahkan kepada agen pemrograman eksternal.
Rahasia hanya berada di lapisan runtime lokal (`%LOCALAPPDATA%\9router_WatchEdit\` dan instalasi 9Router). WatchEdit berjalan dengan `SECRETS: LOCKED` secara bawaan; operasi penyedia langsung memerlukan buka kunci eksplisit (izin yang didukung OS atau vault kata sandi utama opsional).
Lihat **SECURITY_LOCAL.md** untuk model lengkap, pembangun berbagi SAFE (`python tools/build_safe_share.py`), pemindai rahasia, dan alat cadangan pribadi.
  - `data-snapshot/` - Salinan database penuh `%APPDATA%\9router\db\data.sqlite`.

## Cara menerapkan pembaruan
Saat tugas aktif Anda di 9router selesai, jalankan:
```powershell
.\apply-update.ps1
```
Perintah ini akan:
1. Menghentikan instans 9Router yang berjalan.
2. Membuat cadangan bertimestamp di `%APPDATA%\9router_backup_<timestamp>`.
3. Memasang `9router-0.5.65-extra.tgz` secara global.
4. Menyinkronkan semua koneksi dan memperbarui kombinasi SAIFREN.
5. Memulai ulang 9router dalam mode tray.

<!-- translation-model: zcode-agent translation (not local Ollama pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->

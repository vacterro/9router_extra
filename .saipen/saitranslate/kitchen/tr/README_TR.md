# 9Router Extra / Tarama ve Entegrasyon Paketi

Sürüm: **v0.2.0**
Proje: 9router_extra
Yol: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Genel bakış
Bu depo; 9Router için eklentiler, araçlar, durum yedekleri ve yamalar içerir:
1. **WorkBuddy AI (`wb/hy3`) Sağlayıcı Köprüsü**
   - WorkBuddy AI için bulut tabanlı tamamlamalarla doğrudan entegrasyon (`https://www.workbuddy.ai/v2/chat/completions`).
   - Hunyuan `hy3` akıl yürütme modelini destekler (`stream: true` kullanımını, tipli içerik bloklarını, `reasoning_effort: high` ve `reasoning_summary: auto` ayarlarını zorunlu kılar).
   - Belirteçlerin `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info` konumundan otomatik olarak yerel ortama içe aktarılması.
   - Belirteç yenileme ve kota izleme desteği.
2. **Antigravity Gemini 3.8 Flash Düzeltmesi ve Dinamik Kademe Çözümlemesi**
   - Kaynaktaki 404 sorununu giderir: Antigravity kaynağı, `gemini-3.8-flash-tiered(high|medium|low)` biçimini gerektirir.
   - Herhangi bir `gemini-*-flash-(high|medium|low)` modelinin 404 oluşturmadan kademeli biçime eşlenmesini sağlayan dinamik desen eşleştirmesi ekler.
3. **SAIFREN Kombinasyonunun Başına Ekleme**
   - `wb/hy3` modeli birinci, `ag/gemini-3.8-flash-high` modeli ikinci olacak şekilde `SAIFREN` içinde yapılandırıldı.
4. **AgentRouter ve Cline Yamaları**
   - Yerleşik AgentRouter sağlayıcısı, istemci User-Agent değerinin iletilmesi, Anthropic temel URL'si çözümlemesi ve Cline yönlendirme düzeltmeleri.
5. **Durum Yedekleme ve Geçiş Motoru**
   - 36 bağlantının, 13 özel düğümün (B.AI, Dahl, Vyce vb.) ve kombinasyonların anlık görüntüsü.

## Yapı
- `apply-update.ps1` - 9router'ı güvenli biçimde durduran, yedekleyen, v0.5.65-extra sürümüne yükselten, durumu geri yükleyen ve yeniden başlatan tek tıklamalı güncelleme betiği.
- `packages/`
  - `9router-0.5.65-extra.tgz` - Genel kuruluma hazır, tamamen oluşturulmuş ve yamalanmış v0.5.65 paketi.
  - `9router-0.5.59-agentrouter.tgz` - Eski v0.5.59 paketi.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - v0.5.65 kaynak sürümüne yönelik birleşik yama.
  - `9router-extra-unified.patch` - v0.5.59 sürümüne yönelik birleşik yama.
- `backup/`
  - `export_state.js` & `restore_state.js` - SQLite durum yedekleme ve eşitleme araçları
    (özel durum artık `%LOCALAPPDATA%\9router_WatchEdit\backups\private\` konumunu hedefler, bu depoyu hiçbir zaman hedeflemez).

## 9Router kaynak testlerini çalıştırma (vitest çağırma tuzağı, T-21)
Next.js panosunun kaynak kodu `%APPDATA%\9router\source` konumundadır. vitest
yol takma adları (`@/` → `src`, `open-sse` → `open-sse`) YALNIZCA
`tests/vitest.config.js` içinde tanımlıdır.

**İkiliyi kaynak kökünden ÇALIŞTIRMA:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Bunun yerine yapılandırmayı dikkate alan çağrıyı kullanın:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Test yapılandırmasını yeniden dışa aktaran kök düzey `vitest.config.js`, kök
ikilisinin de çalışmasını sağlar; iki çağrının da uyumlu olması için kaynak ağacında
saklanır. `tests/unit/model-routing.test.js`, bu ortamda `EPERM` hatasıyla
karşılaşır; bu hata `os.tmpdir()` temizliği sırasında bu konuyla ilgisiz, önceden
var olan bir sorundur — her iki çağrı altında da aynı biçimde başarısız olur ve bir
takma ad sorunu değildir.

## Güvenlik modeli
Bu depo gizli bilgi içermez ve dış kodlama aracızlarına güvenle teslim edilebilir.
Gizli bilgiler yalnızca yerel çalışma zamanı katmanında bulunur (`%LOCALAPPDATA%\9router_WatchEdit\` ve
9Router kurulumu). WatchEdit varsayılan olarak `SECRETS: LOCKED` modunda çalışır; canlı sağlayıcı işlemleri
açık bir kilit açma işlemi gerektirir (işletim sistemi destekli yetkilendirme veya isteğe bağlı ana parola kasası).
**SECURITY_LOCAL.md** dosyasında güvenli paylaşım oluşturucusunun
(`python tools/build_safe_share.py`), gizli bilgi tarayıcısının ve özel yedekleme araçlarının bulunduğu
tam model için bu dosyaya bakın.
  - `data-snapshot/` - `%APPDATA%\9router\db\data.sqlite` veritabanının tam kopyası.

## Güncellemeyi nasıl uygularsınız
9router'daki etkin görevleriniz tamamlandığında, yalnızca şunu çalıştırın:
```powershell
.\apply-update.ps1
```
Bu işlem şunları yapar:
1. Çalışan 9router örneğini durdurur.
2. `%APPDATA%\9router_backup_<timestamp>` konumunda zaman damgalı bir yedek oluşturur.
3. `9router-0.5.65-extra.tgz` paketini genel olarak kurar.
4. Tüm bağlantıları eşitler ve SAIFREN kombinasyonunu günceller.
5. 9router'ı sistem tepsisi modunda yeniden başlatır.

<!-- translation-model: zcode-agent translation (not local Ollama pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->

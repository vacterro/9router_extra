# 9Router Extra / szkenelési és integrációs csomag

Verzió: **v0.2.0**
Projekt: 9router_extra
Elérési út: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Áttekintés
Ez a tárhely a 9Routerhez készült kiegészítéseket, eszközöket, állapotmentéseket és javításokat tartalmaz:
1. **WorkBuddy AI (`wb/hy3`) szolgáltatási híd**
   - Közvetlen integráció a WorkBuddy AI felhőbeli válaszgeneráló szolgáltatásához (`https://www.workbuddy.ai/v2/chat/completions`).
   - Támogatja a Hunyuan `hy3` gondolkodó modellt (beállítja a `stream: true` értéket, típusos tartalmi blokkokat használ, valamint a `reasoning_effort: high` és `reasoning_summary: auto` értéket állítja be).
   - Helyi token automatikus importálása innost: `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - A token frissítésének és a kvóta követésének támogatása.
2. **Antigravity Gemini 3.8 Flash javítás és dinamikus szintfeloldás**
   - Az upstream Antigravity 404 hibáját javítja: a szolgáltatás a `gemini-3.8-flash-tiered(high|medium|low)` alakot igényli.
   - Dinamikus mintaillesztést ad hozzá, így minden `gemini-*-flash-(high|medium|low)` minta tiered formátumba képeződik le 404 hiba nélkül.
3. **Modellek beszúrása a SAIFREN kombináció elejére**
   - Beállítja a `wb/hy3` modellt az első helyre és az `ag/gemini-3.8-flash-high` modellt a második helyre a `SAIFREN` kombinációban.
4. **AgentRouter- és Cline-javítások**
   - Beépített AgentRouter-szolgáltató, a kliens User-Agent fejlécének továbbítása, az Anthropic alap-URL-jének feloldása és a Cline-irányítás javítása.
5. **Állapotmentés és migrációs motor**
   - Pillanatképet készít az összes 36 kapcsolatról, a 13 egyéni csomópontokról (B.AI, Dahl, Vyce stb.) és a kombinációkról.

## Struktúra
- `apply-update.ps1` - Egykattintásos frissítési szkript, amely biztonságosan leállítja a 9routert, biztonsági másolatot készít róla, frissíti v0.5.65-extra verzióra, visszaállítja az állapotot és újraindítja a 9routert.
- `packages/`
  - `9router-0.5.65-extra.tgz` - Teljesen felépített, javított v0.5.65 csomag, kész a globális telepítésre.
  - `9router-0.5.59-agentrouter.tgz` - Régebbi v0.5.59 csomag.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - Egyesített javítás a v0.5.65 upstream verzióhoz.
  - `9router-extra-unified.patch` - Egyesített javítás a v0.5.59 verzióhoz.
- `backup/`
  - `export_state.js` & `restore_state.js` - SQLite-állapot mentési és szinkronizáló eszközök
    (a privát állapot most ide kerül: `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, soha nem ebbe a tárhelybe).

## A 9Router forráskódjának tesztjeinek futtatása (a vitest meghívás csapdája, T-21)
A Next.js vezérlőpult forráskódja itt található: `%APPDATA%\9router\source`. A vitest útvonal-aliászok (`@/` → `src`, `open-sse` → `open-sse`) csak a
`tests/vitest.config.js` fájlban vannak definiálva.

**Ne hívja meg a futtatható állományt a forrásfájl gyökeréből:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Helyette használja a konfigurációt betöltő hívást:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

A gyökérszintű `vitest.config.js`, amely ismét exportálja a tesztek konfigurációját, szintén működőképessé teszi a gyökérben lévő futtatható állományt. A forrásfában marad, hogy mindkét hívás ugyanazt a konfigurációt használja.
A `tests/unit/model-routing.test.js` teszthez ebben a környezetben ettől független, korábban meglévő `EPERM` tartozik az `os.tmpdir()` könyvtárának eltávolításakor. A teszt mindkét hívásnál azonos módon hibázik, és nem az alias a probléma.

## Biztonsági modell
Ez a tárhely titokmentes, ezért biztonságosan átadható külső kódoló AI-ügynököknek.
A titkok kizárólag a helyi futtatási rétegben találhatók: a `%LOCALAPPDATA%\9router_WatchEdit\` mappában és a 9Router telepítésében. A WatchEdit alapértelmezés szerint `SECRETS: LOCKED` állapotban fut; a szolgáltatók éles műveleteihez explicit feloldás szükséges (az operációs rendszer által támogatott engedély vagy az opcionális főjelszó-tár használata).
A teljes modellt, a SAFE-megosztás létrehozására szolgáló eszközt
(`python tools/build_safe_share.py`), a titokszkennert és a privát biztonsági mentési eszközöket lásd a **SECURITY_LOCAL.md** fájlban.
  - `data-snapshot/` - Az `%APPDATA%\9router\db\data.sqlite` adatbázis teljes másolata.

## Frissítés alkalmazása
Ha a 9routerben futó aktív feladatok elkészültek, egyszerűen futtassa:
```powershell
.\apply-update.ps1
```
Ez a következőket teszi:
1. Leállítja a futó 9router példányt.
2. Időbélyeges biztonsági mentést készít a `%APPDATA%\9router_backup_<timestamp>` helyen.
3. Globálisan telepíti a `9router-0.5.65-extra.tgz` csomagot.
4. Szinkronizálja az összes kapcsolatot, és frissíti a SAIFREN kombinációt.
5. Újraindítja a 9routert rendszertálca-üzemmódban.

<!-- translation-model: qwen3:14b contract:structured-markdown-v2 -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->

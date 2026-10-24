# 9Router Extra / Skanneri- ja integratsioonikomplekt

Versioon: **v0.2.0**
Projekt: 9router_extra
Tee: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Ülevaade
See repositoorium sisaldab 9Routeri laiendusi, tööriistu, olekuentrhoiu ja paigutusi:
1. **WorkBuddy AI (`wb/hy3`) pakkuja sild**
   - Otsene integratsioon WorkBuddy AI pilve täitmisteenustega (`https://www.workbuddy.ai/v2/chat/completions`).
   - Toetab Hunyuani põhjendusmudelit `hy3` (sunnib `stream: true`, kasutab tüübitud sisublokke ning väärtusi `reasoning_effort: high` ja `reasoning_summary: auto`).
   - Kohaliku tokeni automaatne import failist `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Toetab tokeni värskendamist ja kvoodi jälgimist.
2. **Antigravity Gemini 3.8 Flash parandus ja dünaamiline taseme lahendamine**
   - Kõrvaldab ülesvoolu 404 tõrke: ülesvoolu Antigravity nõuab `gemini-3.8-flash-tiered(high|medium|low)`.
   - Lisab dünaamilise mustri sobitamise, nii et iga `gemini-*-flash-(high|medium|low)` kaardistub tasemeliseks vormiks ilma 404 tõrketa.
3. **SAIFRENi kombinatsiooni eesotsa lisamine**
   - Seab `wb/hy3` esimeseks ja `ag/gemini-3.8-flash-high` teiseks mudeliks kombinatsioonis `SAIFREN`.
4. **AgentRouteri ja Cline'i paigutused**
   - Sisseehitatud AgentRouteri pakkuja, kliendi User-Agent edastamine, Anthropic baas-URL-i lahendamine ja Cline'i marsutuse parandused.
5. **Olekuentruhu ja migratsiooni mootor**
   - Kõigi 36 ühenduse, 13 kohandatud sõlme (B.AI, Dahl, Vyce jm) ja kombinatsioonide hetkeseade.

## Struktuur
- `apply-update.ps1` - ühe klõpsatusega uuendusskript, mis peatab 9routeri ohutult, võtab varukoopia, uuendab versioonile v0.5.65-extra, taastab oleku ja käivitab 9routeri uuesti.
- `packages/`
  - `9router-0.5.65-extra.tgz` - täielikult ehitatud ja paigutatud v0.5.65 pakett, valmis globaalseks installimiseks.
  - `9router-0.5.59-agentrouter.tgz` - varasem v0.5.59 pakett.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - ühtne paigutus v0.5.65 ülesvoolu suhtes.
  - `9router-extra-unified.patch` - ühtne paigutus v0.5.59 suhtes.
- `backup/`
  - `export_state.js` & `restore_state.js` - SQLite oleku varundamise ja sünkroonimise tööriistad
    (privaatne olek suundub nüüdsest `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, mitte kunagi sellesse repositooriumi).

## 9Routeri lähtekoodi testide käivitamine (vitest'i kutsumise püünis, T-21)
Next.js'i juhtpaneeli lähtekood asub `%APPDATA%\9router\source`. Selle vitest'i
rajaliited (`@/` → `src`, `open-sse` → `open-sse`) on määratletud AINULT failis
`tests/vitest.config.js`.

**ÄRA käivita binaarfaili lähtekoodi juurest:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Kasuta selle asemel konfiguratsiooni laadivat kutset:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Juurtasemel asuv `vitest.config.js`, mis ekspordib testide konfiguratsiooni uuesti,
muudab töökorral ka juurtaseme binaarfaili; see hoitakse lähtekoodi puus, et mõlemad
kutsed klapiksid omavahel.
`tests/unit/model-routing.test.js`-testil on selles keskkonnas võõralt, varem
existeerinud `EPERM` `os.tmpdir()` puhastamisel. Test ebaõnnestub mõlema kutse
korral ühtemoodi ning tegemist pole aliasega.

## Turvalisusmudel
See repositoorium on saladusteta ja selle võib ohutult üle anda välistele
programmistusagentidele.
Saladused asuvad ainult kohalikus käivituskihis (`%LOCALAPPDATA%\9router_WatchEdit\` ja
9Routeri paigalduses). WatchEdit töötab vaikimisi režiimis `SECRETS: LOCKED`; pakkuja
otseoperatsioonid nõuavad selget lukustamise avamist (operatsioonisüsteemi toetatud
luba või valikuline peaparooli hoiutõbi).
Vaata **SECURITY_LOCAL.md** kogu mudeli, SAFE-jagamise ehitaja
(`python tools/build_safe_share.py`), saladuste skanneri ja privaatsete varukoopiate
tööriistade kohta.
  - `data-snapshot/` - Täielik andmebaasi koopia failist `%APPDATA%\9router\db\data.sqlite`.

## Uuenduse rakendamine
Kui sinu aktiivsed ülesanded 9routeris on lõpetatud, käivita lihtsalt:
```powershell
.\apply-update.ps1
```
See teeb järgmist:
1. Peatab töötava 9routeri eksemplari.
2. Loob ajatembliga varukoopia kataloogi `%APPDATA%\9router_backup_<timestamp>`.
3. Paigaldab `9router-0.5.65-extra.tgz` globaalselt.
4. Sünkroonib kõik ühendused ja uuendab SAIFRENi kombinatsiooni.
5. Käivitab 9routeri uuesti salvedarežiimis.

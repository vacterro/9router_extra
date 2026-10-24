# 9Router Extra / skener i integracijski paket

Verzija: **v0.2.0**
Projekt: 9router_extra
Putanja: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Pregled
Ovaj repozitorij sadrži proširenja, alate, sigurnosne kopije stanja i ispravke za 9Router:
1. **Most prema WorkBuddy AI pružatelju usluge (`wb/hy3`)**
   - Izravna integracija s WorkBuddy AI uslugom za generiranje odgovora u oblaku (`https://www.workbuddy.ai/v2/chat/completions`).
   - Podržava model za razmišljanje Hunyuan `hy3` (postavlja `stream: true`, koristi tipizirane blokove sadržaja te vrijednosti `reasoning_effort: high` i `reasoning_summary: auto`).
   - Automatski uvoz lokalnog tokena iz `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Podržava osvježavanje tokena i praćenje kvote.
2. **Ispravka problema Antigravity Gemini 3.8 Flash i dinamičko određivanje razine**
   - Ispravlja 404 pogrešku prema nadređenoj usluzi Antigravity: Antigravity zahtijeva `gemini-3.8-flash-tiered(high|medium|low)`.
   - Dodaje dinamičko prepoznavanje uzoraka tako da se svaki `gemini-*-flash-(high|medium|low)` preslikava u tiered oblik bez 404 pogreške.
3. **Dodavanje modela na početak SAIFREN kombinacije**
   - Postavlja `wb/hy3` kao prvi model i `ag/gemini-3.8-flash-high` kao drugi model u `SAIFREN` kombinaciji.
4. **Ispravke za AgentRouter i Cline**
   - Ugrađeni AgentRouter pružatelj usluge, prosljeđivanje User-Agent zaglavlja klijenta, rješavanje baznog URL-a Anthropica i ispravke Cline usmjeravanja.
5. **Sigurnosne kopije stanja i mehanizam migracije**
   - Snimka svih 36 veza, 13 prilagođenih čvorova (B.AI, Dahl, Vyce itd.) i kombinacija.

## Struktura
- `apply-update.ps1` - Skripta za nadogradnju jednim klikom koja sigurno zaustavlja 9router, stvara sigurnosnu kopiju, nadogradjuje na v0.5.65-extra, vraća stanje i ponovno pokreće 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - Potpuno izgrađen i ispravljen paket v0.5.65 spreman za globalnu instalaciju.
  - `9router-0.5.59-agentrouter.tgz` - Starija verzija paketa v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - Objedinjena zakrpa za v0.5.65.
  - `9router-extra-unified.patch` - Objedinjena zakrpa za v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - Alati za sigurnosne kopije i sinkronizaciju SQLite stanja
    (privatno stanje sada se pohranjuje u `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, nikada u ovom repozitoriju).

## Pokretanje testova izvornog koda 9Routera (zamka pri pozivu vitesta, T-21)
Izvorni kod Next.js nadzorne ploče nalazi se u `%APPDATA%\9router\source`. Njegovi vitest aliasi putanja (`@/` → `src`, `open-sse` → `open-sse`) definirani su SAMO u
`tests/vitest.config.js`.

**Ne pozivajte binarni program iz korijena izvornog koda:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Umjesto njega upotrijebite poziv koji učitava konfiguraciju:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Konfiguracija `vitest.config.js` na razini korijena koja ponovno izvozi konfiguraciju testova također omogućuje rad binarnog programa u korijenu. Čuva se u stablu izvora kako bi oba poziva koristila isto.
`tests/unit/model-routing.test.js` ima neovisnu postojeću `EPERM` pogrešku pri čišćenju direktorija `os.tmpdir()` u ovom okruženju — neuspješan je na jednak način za oba poziva i nije problem aliasa.

## Sigurnosni model
Ovaj repozitorij ne sadrži tajne te ga je sigurno proslijediti vanjskim kodnim agentima.
Tajne postoje samo u lokalnom sloju izvršavanja (`%LOCALAPPDATA%\9router_WatchEdit\` i instalaciji 9Routera). WatchEdit prema zadanim postavkama radi u stanju `SECRETS: LOCKED`; za izravan rad s pružateljima usluga potrebno je izričito otključavanje (ovlaštenje podržano operativnim sustavom ili opcionalni trezor s glavnom lozinkom).
Pogledajte **SECURITY_LOCAL.md** za cjeloviti model, alat za stvaranje dijela za dijeljenje pod oznakom SAFE
(`python tools/build_safe_share.py`), skener tajni i alate za privatne sigurnosne kopije.
  - `data-snapshot/` - Potpuna kopija baze podataka `%APPDATA%\9router\db\data.sqlite`.

## Primjena nadogradnje
Kada završe aktivni zadaci u 9Routeru, samo pokrenite:
```powershell
.\apply-update.ps1
```
Ovo će:
1. Zaustaviti trenutačnu instancu 9Routera.
2. Stvoriti sigurnosnu kopiju s vremenskom oznakom u `%APPDATA%\9router_backup_<timestamp>`.
3. Globalno instalirati `9router-0.5.65-extra.tgz`.
4. Sinkronizirati sve veze i ažurirati SAIFREN kombinaciju.
5. Ponovno pokrenuti 9Router u načinu rada putem ikone u sistemskoj traci.

<!-- translation-model: qwen3:14b contract:structured-markdown-v2 -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->

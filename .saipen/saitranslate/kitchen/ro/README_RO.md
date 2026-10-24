# 9Router Extra / Scanner și suită de integrare

Versiune: **v0.2.0**
Proiect: 9router_extra
Cale: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Prezentare generală
Acest repository conține extensii, instrumente, copii de rezervă ale stării și patch-uri pentru 9Router:
1. **Punte de legătură cu furnizorul WorkBuddy AI (`wb/hy3`)**
   - Integrare directă cu completările din cloud-ul WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - Acceptă modelul de raționament Hunyuan `hy3` (forțează `stream: true`, blocuri de conținut tipizate, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Import automat al tokenului local din `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Acceptă reîmprospătarea tokenului și urmărirea cotelor.
2. **Antigravity Gemini 3.8 Flash Fix & Dynamic Tier Resolution**
   - Corectează problema 404 din upstream: Antigravity upstream necesită `gemini-3.8-flash-tiered(high|medium|low)`.
   - Adaugă potrivire dinamică a modelelor, astfel încât orice `gemini-*-flash-(high|medium|low)` să fie asociat formatului cu niveluri, fără 404.
3. **Adăugare la începutul combo-ului SAIFREN**
   - Configurează `wb/hy3` drept primul model și `ag/gemini-3.8-flash-high` drept al doilea model în `SAIFREN`.
4. **Patch-uri pentru AgentRouter și Cline**
   - Furnizor AgentRouter nativ, forwarding User-Agent al clientului, rezolvarea URL-ului de bază Anthropic și corectări de rutare Cline.
5. **Motor de backup și migrare a stării**
   - Instantaneu al tuturor celor 36 de conexiuni, 13 noduri personalizate (B.AI, Dahl, Vyce etc.) și combinații.

## Structură
- `apply-update.ps1` - Script de actualizare cu un singur clic care oprește în siguranță procesul, creează o copie de rezervă, face upgrade la v0.5.65-extra, restaurează starea și repornește 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - Pachet v0.5.65 complet compilat și corectat, pregătit pentru instalare globală.
  - `9router-0.5.59-agentrouter.tgz` - Pachet legacy v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - Patch unificat față de upstream v0.5.65.
  - `9router-extra-unified.patch` - Patch unificat față de v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - Instrumente de backup și sincronizare a stării SQLite
    (starea privată este acum în `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, niciodată în acest repository).

## Executarea testelor sursei 9Router (capcana invocării vitest, T-21)
Sursa dashboardului Next.js se află în `%APPDATA%\9router\source`. Aliasurile de căi vitest (`@/` → `src`, `open-sse` → `open-sse`) sunt definite DOAR în `tests/vitest.config.js`.

**NU invoca binarul din rădăcina sursei:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Folosește invocarea care respectă configurația:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Un fișier `vitest.config.js` la nivelul rădăcinii, care reexportă configurația testelor, permite funcționarea și a binarului din rădăcină; este păstrat în arborele sursă pentru ca ambele invocări să fie coerente.
`tests/unit/model-routing.test.js` are o problemă `EPERM` preexistentă, fără legătură cu aceasta, la teardown-ul lui `os.tmpdir()` în acest mediu — eșuează identic la ambele invocări și nu este o problemă de alias.

## Modelul de securitate
Acest repository nu conține secrete și poate fi transmis în siguranță agenților de programare externi.
Secretele există doar în stratul local de runtime (`%LOCALAPPDATA%\9router_WatchEdit\` și instalarea 9Router). WatchEdit rulează cu `SECRETS: LOCKED` în mod implicit; operațiile live ale furnizorului necesită o deblocare explicită (permisiune susținută de sistemul de operare sau vault opțional cu parolă principală).
Consultă **SECURITY_LOCAL.md** pentru modelul complet, instrumentul SAFE de construire a pachetelor pentru partajare (`python tools/build_safe_share.py`), scannerul de secrete și instrumentele pentru backup privat.
  - `data-snapshot/` - Copie completă a bazei de date `%APPDATA%\9router\db\data.sqlite`.

## Cum se aplică actualizarea
Când sarcinile active din 9router sunt finalizate, rulează pur și simplu:
```powershell
.\apply-update.ps1
```
Acest proces va:
1. Oprește instanța 9router în execuție.
2. Creează o copie de rezervă cu marcaj de timp în `%APPDATA%\9router_backup_<timestamp>`.
3. Instalează `9router-0.5.65-extra.tgz` la nivel global.
4. Sincronizează toate conexiunile și actualizează combinația SAIFREN.
5. Repornește 9router în modul tray.
<!-- translation-model: zcode-agent translation (not local Ollama pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->

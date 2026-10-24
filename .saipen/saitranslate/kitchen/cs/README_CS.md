# 9Router Extra / Skener a integrační sada

Verze:**v0.2.0**
Projekt: 9router_extra
Cesta:`V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Přehled
Tento repozitář obsahuje rozšíření, nástroje, zálohy stavu a opravy pro 9Router:
1. **Most k poskytovateli WorkBuddy AI (`wb/hy3`)**
   - Přímá integrace s cloudu WorkBuddy AI(`https://www.workbuddy.ai/v2/chat/completions`).
   - Podporuje model rozumování Hunyuan`hy3` (vynucuje`stream: true`, typované bloky obsahu,`reasoning_effort: high`, `reasoning_summary: auto`).
   - Automatické importování místního tokenu`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Podpora pro obnovu tokenu a sledování kvót.
2. **Antigravity Gemini 3.8 Flash Fix & Dynamické řešení vrstev**
   - Oprava problému 404 v upstream: upstream Antigravity vyžaduje`gemini-3.8-flash-tiered(high|medium|low)`.
   - Přidání dynamického shodování vzorů, takže jakýkoli`gemini-*-flash-(high|medium|low)`se mapuje do vrstveného formátu bez 404.
3. **Vložení na začátek kombinace SAIFREN**
   - Nastavení`wb/hy3`jako prvního modelu a`ag/gemini-3.8-flash-high`jako druhého modelu v`SAIFREN`.
4. **Opravy pro AgentRouter a Cline**
   - Vlastní poskytovatel AgentRouter, předávání User-Agent klienta, vyřešení základní URL pro Anthropic a opravy směrování Cline.
5. **Stavová záloha a migrace**
   - Snímek všech 36 připojení, 13 vlastních uzlů(B.AI, Dahl, Vyce, atd.), a kombinace.

## Struktura
- `apply-update.ps1`- 1-klikový skript pro bezpečné zastavení, zálohu, upgrade na verzi v0.5.65-extra, obnovení stavu a restart 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz`- Plně sestavený, opravený balíček v0.5.65 připravený pro globální instalaci.
  - `9router-0.5.59-agentrouter.tgz`- Starší balíček v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch`- Jednotná oprava proti v0.5.65 upstream.
  - `9router-extra-unified.patch`- Jednotná oprava proti v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js`- Nástroje pro zálohu a synchronizaci stavu SQLite
    (soukromý stav nyní cílí na`%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, nikdy ne tento repozitář).

## Spouštění testů zdroje 9Router(zachytnutí volání vitest, T-21)
Zdrojový kód Next.js dashboardu se nachází v`%APPDATA%\9router\source`. Jeho vitest
aliasy cest(`@/` → `src`, `open-sse` → `open-sse`)jsou definovány POUZE v
`tests/vitest.config.js`.

**Nevolujte binární soubor z kořenového adresáře zdroje:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Použijte místo toho volání, které načítá konfiguraci:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Soubor na úrovni kořene`vitest.config.js`který znovu exportuje konfiguraci testů, zároveň umožňuje
fungovat kořenovému binárnímu souboru; je udržován ve stromu zdrojového kódu, aby se obě volání shodovala.
`tests/unit/model-routing.test.js`má nepříbuzný předchozí`EPERM`na
`os.tmpdir()`úklidu v tomto prostředí — selhává stejně pod oběma
voláními a není to problém s aliasem.

## Model zabezpečení
Tento repozitář je bez tajných informací a bezpečný pro předání externím kódovacím agentům.
Tajné informace existují pouze v místním vrstvě runtime(`%LOCALAPPDATA%\9router_WatchEdit\`a
9Router install). WatchEdit běží`SECRETS: LOCKED`výchozí; živé operace poskytovatele
vyžadují explicitní odemčení(Grant podporovaný operačním systémem nebo volitelný trezor s hlavním heslem).
Viz**SECURITY_LOCAL.md**pro plný model, výstavbu bezpečného sdílení,
(`python tools/build_safe_share.py`)tajný skener a nástroje pro privátní zálohování.
  - `data-snapshot/`- Plná kopie databáze`%APPDATA%\9router\db\data.sqlite`.

## Jak aplikovat aktualizaci
Když jsou vaše aktivní úkoly v 9router dokončeny, jednoduše spusťte:
```powershell
.\apply-update.ps1
```
Toto provede:
1. Zastaví běžící instanci 9router.
2. Vytvoří časově označenou zálohu v`%APPDATA%\9router_backup_<timestamp>`.
3. Nainstaluje`9router-0.5.65-extra.tgz`globálně.
4. Synchronizujte všechny připojení a aktualizujte SAIFREN kombinaci.
5. Restartujte 9router v režimu oznamovací oblasti.

<!-- translation-model: qwen3:14b contract:structured-markdown-v2 -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
